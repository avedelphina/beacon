"""Beacon mailbox: relay and voicemail for the A2A mesh.

`POST /a2a/{agent}` speaks A2A on the agent's behalf. If the agent answers,
the call is simply forwarded (relay). If it can't be reached, the message is
kept and delivered the next time the agent's host pulls its mesh config —
that pull is the liveness signal, so Beacon needs no scheduler of its own.

The agent's reply then goes back to the original caller as a *new message*
from the agent. Hermes's A2A tools can't poll a task for a late result, so a
late answer has to arrive the way every other message does. Replies to
Beacon itself (an MCP caller leaving voicemail) are kept for GET
/api/mailbox instead. Replies are never answered again, so nothing loops.

Long work. A Hermes caller gives up after 120 s and a Hermes agent drops an
answer it finds after A2A_REPLY_TIMEOUT, so nothing longer than a short
exchange used to come back. The mailbox therefore waits only HANDOFF_SECONDS
(under the caller's limit). A slower answer is handed off: the caller gets a
durable `mbx-N` task right away, the agent (whose reply window the host sync
raises to mesh.REPLY_WINDOW_SECONDS) POSTs the finished task to Beacon (A2A
push notification), and the answer reaches the caller as a new message. Every
delivery path carries the push URL, and whichever arrives first, the response
or the push, records the answer; the other is ignored.

Row states: queued (waiting), delivering (an attempt is running), relaying (a
live relay; its caller is still waiting), working (the agent has it; the answer
comes by push), delivered, and the terminal failed, uncertain (no answer and no
way to receive one), expired.

Callers keep their identity end to end: each presents its pairwise token
for the target, Beacon checks it and delivers with that same token, so the
target sees who really wrote. Beacon's own token is never accepted from an
agent, so no agent can pose as Beacon.
"""

import hmac
import time

from . import bus, mesh, opstore, store

TTL_SECONDS = 72 * 3600
# A delivery claimed this long ago without finishing died with its worker.
STALE_DELIVERY_SECONDS = 15 * 60
SNIPPET = 200
# How long anyone waits for an answer. Below Hermes's a2a_call timeout (120 s),
# so a slow answer is handed off before the caller gives up and loses it.
HANDOFF_SECONDS = 90
# A handed-off message with no answer by then will not get one: the agent's
# own reply window has passed.
WORK_DEADLINE_SECONDS = mesh.REPLY_WINDOW_SECONDS + 15 * 60
# Hermes cuts the text in a push here; a longer answer is fetched with GetTask.
PUSH_TEXT_LIMIT = 2000
FINAL_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_FAILED",
                "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}

# A mailbox task is durable even when the target is offline. These are the
# wire states used by A2A tasks/get and tasks/subscribe.
STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
STATE_WORKING = "TASK_STATE_WORKING"
STATE_COMPLETED = "TASK_STATE_COMPLETED"
STATE_FAILED = "TASK_STATE_FAILED"


def caller_for(target_id: str, authorization: str | None) -> str | None:
    """Which fleet agent presented its token for `target_id`, if any."""
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    presented = authorization.removeprefix("Bearer ")
    for agent in store.list_agents():
        if agent.reach and agent.id != target_id and hmac.compare_digest(presented, mesh.token(agent.id, target_id)):
            return agent.id
    return None


def enqueue(sender: str, recipient: str, kind: str, body: str, context_id: str | None = None,
            reply_to: int | None = None, state: str = "queued") -> int:
    with opstore.connect() as db:
        return _insert(db, sender, recipient, kind, body, context_id, reply_to, state)


def _insert(db, sender, recipient, kind, body, context_id=None, reply_to=None, state="queued") -> int:
    now = time.time()
    return db.execute(
        "INSERT INTO mailbox (sender, recipient, kind, body, context_id, reply_to, state, created_at,"
        " updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sender, recipient, kind, body, context_id, reply_to, state, now, now, now + TTL_SECONDS),
    ).lastrowid


def push_key(recipient_id: str, message_id: int) -> str:
    # ":" never occurs in an agent id, so this can't equal any pairwise token.
    return mesh.token(f"push:{message_id}", recipient_id)


def push_url(recipient_id: str, message_id: int) -> str | None:
    """Where `recipient_id` POSTs the finished task for this message. None
    without BEACON_PUBLIC_URL: the agent would have nowhere to send it."""
    base = mesh.public_url()
    return f"{base}/a2a/{recipient_id}/push/{message_id}?k={push_key(recipient_id, message_id)}" if base else None


def send(caller: str, target_id: str, text: str, context_id: str | None) -> dict:
    """Relay `text` and return the answer if it comes within HANDOFF_SECONDS.
    A slower answer is handed off (a durable `mbx-N` task now, the answer
    later as a new message); an unreachable target gets voicemail. A relay
    that is answered keeps nothing. Returns a task view."""
    agent = store.get_agent(target_id)
    message_id = enqueue(caller, target_id, "message", text, context_id, state="relaying")
    url = push_url(target_id, message_id)
    try:
        view = bus.call(agent, text, context_id, token=mesh.token(caller, target_id),
                        timeout=HANDOFF_SECONDS, push_url=url)
    except bus.Unreachable as exc:
        if exc.transient:
            _move(message_id, "relaying", "queued", str(exc))
            return _ticket(message_id, target_id, context_id, "TASK_STATE_SUBMITTED",
                           f"{target_id} is unreachable right now. Beacon is holding the message and will deliver "
                           f"it when {target_id} is back online; the reply will reach you later as a new message.")
        if not (exc.uncertain and url):
            _forget(message_id)
            raise
        if _move(message_id, "relaying", "working", str(exc)):
            return _ticket(message_id, target_id, context_id, "TASK_STATE_WORKING",
                           f"{target_id} has the message and is still working on it. Beacon will pass the answer "
                           f"back to you as a new message when it is ready (task mbx-{message_id}).")
        view = None  # its push beat the timeout: answered after all
    if view is None:
        view = {"task_id": f"mbx-{message_id}", "context_id": context_id, "state": "TASK_STATE_COMPLETED",
                "reply": _reply_body(message_id)}
    _forget(message_id)
    return view


def _ticket(message_id: int, target_id: str, context_id: str | None, state: str, text: str) -> dict:
    return {"task_id": f"mbx-{message_id}", "context_id": context_id or f"mbx-ctx-{message_id}",
            "state": state, "reply": text}


def receive_push(recipient_id: str, message_id: int, payload: dict) -> str | None:
    """`recipient_id` POSTed the finished task for a message Beacon delivered
    (the URL's key already proved which one). Returns the original sender when
    its answer now needs delivering."""
    update = payload.get("statusUpdate") if isinstance(payload, dict) else None
    status = update.get("status") if isinstance(update, dict) else None
    if not isinstance(status, dict) or status.get("state") not in FINAL_STATES:
        return None
    text = bus._text(status.get("message"))
    if len(text) >= PUSH_TEXT_LIMIT and update.get("taskId"):
        try:
            text = bus.get_task(store.get_agent(recipient_id), str(update["taskId"]))["reply"] or text
        except (bus.Unreachable, store.NotFound):
            pass  # keep the cut text rather than nothing
    return _record_result(message_id, text or f"(no text; task state {status['state']})")


def recent(limit: int = 50) -> list[dict]:
    with opstore.connect() as db:
        rows = db.execute("SELECT * FROM mailbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def task_view(task_id: str, *, requester: str | None = None, target: str | None = None) -> dict:
    """Return an A2A Task-shaped snapshot for a durable mailbox task.

    ``mbx-N`` identifies a message or a notice (a board assignment's). A
    completed reply is stored as a separate mailbox row linked by
    ``reply_to``; it is folded into the task snapshot so callers can poll
    without knowing Beacon's internal schema. A2A callers are restricted to the
    original sender and target.
    """
    if not isinstance(task_id, str) or not task_id.startswith("mbx-"):
        raise store.NotFound(f"task {task_id!r} not found")
    try:
        message_id = int(task_id.removeprefix("mbx-"))
    except ValueError as exc:
        raise store.NotFound(f"task {task_id!r} not found") from exc
    with opstore.connect() as db:
        row = db.execute("SELECT * FROM mailbox WHERE id = ? AND kind IN ('message', 'notice')",
                         (message_id,)).fetchone()
        if row is None:
            raise store.NotFound(f"task {task_id!r} not found")
        if requester is not None and requester not in {row["sender"], row["recipient"]}:
            raise store.NotFound(f"task {task_id!r} not found")
        if target is not None and row["recipient"] != target:
            raise store.NotFound(f"task {task_id!r} not found")
        reply = db.execute(
            "SELECT * FROM mailbox WHERE reply_to = ? AND kind = 'reply' ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
    state = _task_state(row, bool(reply))
    text = (reply["body"] if reply else "") or _task_message(row)
    return {
        "id": task_id,
        "contextId": row["context_id"] or f"mbx-ctx-{row['id']}",
        "status": {"state": state, "message": {"role": "ROLE_AGENT", "parts": [{"text": text}]}},
        "mailbox": {"message_id": row["id"], "state": row["state"], "attempts": row["attempts"],
                    "last_error": row["last_error"], "updated_at": row["updated_at"],
                    "expires_at": row["expires_at"], "reply_id": reply["id"] if reply else None},
    }


def _task_state(row, has_reply: bool) -> str:
    if has_reply or (row["kind"] == "notice" and row["state"] == "delivered"):
        return STATE_COMPLETED
    if row["state"] in {"failed", "expired", "uncertain"}:
        return STATE_FAILED
    if row["state"] in {"delivering", "relaying", "working", "delivered"}:
        return STATE_WORKING
    return STATE_SUBMITTED


def _task_message(row) -> str:
    recipient, state = row["recipient"], row["state"]
    if state == "delivered":
        return f"Delivered to {recipient}."
    if state == "working":
        return (f"{recipient} has it and is still working on it; the answer comes back when it is ready "
                f"(up to {mesh.REPLY_WINDOW_SECONDS // 3600} h).")
    if state == "uncertain":
        return (f"{recipient} gave no answer within {HANDOFF_SECONDS} s, and Beacon had no way to receive a "
                f"later one. It may have done the work; Beacon did not send it again, to avoid doing it twice.")
    if state in {"failed", "expired"}:
        return row["last_error"] or f"Delivery to {recipient} {state}."
    if state in {"delivering", "relaying"}:
        return f"Beacon is delivering it to {recipient}."
    return f"Beacon is holding it for {recipient}."


def deliver_pending(recipient_id: str) -> None:
    """Deliver everything waiting for `recipient_id`, oldest first, by calling
    it. Used when its host's sync can't deliver locally (an older sync), and
    for an original sender as soon as its reply exists (it is probably
    online — it just sent a message)."""
    _expire()
    try:
        agent = store.get_agent(recipient_id)
    except store.NotFound:
        return
    senders_with_replies = set()
    while (row := _claim(recipient_id)) is not None:
        if not _allowed(row, agent):
            continue
        is_message = row["kind"] == "message"
        url = push_url(recipient_id, row["id"]) if is_message else None
        try:
            view = bus.call(agent, _framed(row), row["context_id"] if is_message else None,
                            token=_delivery_token(row, recipient_id), timeout=HANDOFF_SECONDS, push_url=url)
        except bus.Unreachable as exc:
            if exc.transient:
                _finish(row["id"], "queued", str(exc))
                break  # gone again; the next pull retries
            if exc.uncertain:
                _handed_off(row, bool(url), str(exc))  # it arrived: never send it twice
            else:
                _finish(row["id"], "failed", str(exc))
            continue
        if sender := _delivered(row, view["reply"] or f"(no text; task state {view['state']})"):
            senders_with_replies.add(sender)
    for sender in senders_with_replies:
        deliver_pending(sender)


def claim_for_pull(recipient_id: str, limit: int = 10) -> list[dict]:
    """Pull delivery: hand what waits for `recipient_id` to its own host's
    sync, which delivers to the agent locally and reports back (`report`).
    Beacon then never needs to reach the agent — the Tailscale-DERP shape for
    agents behind NAT or off the network. Claimed items that are never
    reported go back to the queue after STALE_DELIVERY_SECONDS."""
    _expire()
    try:
        agent = store.get_agent(recipient_id)
    except store.NotFound:
        return []
    items = []
    while len(items) < limit and (row := _claim(recipient_id)) is not None:
        if _allowed(row, agent):
            is_message = row["kind"] == "message"
            items.append({"id": row["id"], "text": _framed(row), "token": _delivery_token(row, recipient_id),
                          "context_id": row["context_id"] if is_message else None,
                          "push_url": push_url(recipient_id, row["id"]) if is_message else None})
    return items


def report(recipient_id: str, message_id: int, ok: bool, reply: str = "", transient: bool = False,
           error: str = "", *, uncertain: bool = False, working: bool = False) -> str | None:
    """The sync's result for one claimed item. Returns an original sender
    whose reply should now be delivered (call deliver_pending for it).
    `working`: no answer in time, but the agent has it and will push the
    answer; `uncertain`: the same without a push to wait for (older syncs)."""
    with opstore.connect() as db:
        row = db.execute("SELECT * FROM mailbox WHERE id = ? AND recipient = ?"
                         " AND state IN ('delivering', 'working', 'delivered')", (message_id, recipient_id)).fetchone()
    if row is None:
        raise store.NotFound(f"no delivery in progress for message {message_id} to {recipient_id}")
    if ok:
        return _delivered(row, reply or "(no text)")
    if row["state"] != "delivering":
        return None  # the agent's push settled it first
    if transient:
        _finish(row["id"], "queued", error[:500] or "local delivery failed")
    elif working or uncertain:
        _handed_off(row, working, error[:500] or "no answer in time")
    else:
        _finish(row["id"], "failed", error[:500] or "local delivery failed")
    return None


def _delivery_token(row, recipient_id: str) -> str:
    """A message arrives as its sender. A reply or notice arrives from Beacon,
    which every agent trusts: the policy is one-way, and answering a call that
    was allowed must not need the answerer to be allowed to call back."""
    sender = row["sender"] if row["kind"] == "message" else mesh.BEACON_PEER
    return mesh.token(sender, recipient_id)


def _allowed(row, agent) -> bool:
    # Replies and notices answer something already allowed; only a message is
    # checked, and checked again at delivery: the policy may have changed.
    if row["kind"] != "message" or mesh.may_call(row["sender"], agent):
        return True
    _finish(row["id"], "failed", f"{agent.id} does not accept calls from {row['sender']}")
    return False


def _delivered(row, reply: str) -> str | None:
    """Mark delivered; for a message, keep the reply for the original sender.
    Returns that sender when it is an agent (its reply now needs delivery)."""
    if row["kind"] != "message":
        _finish(row["id"], "delivered")
        return None
    return _record_result(row["id"], reply)


def _record_result(message_id: int, text: str) -> str | None:
    """Store a message's answer exactly once, whichever arrives first: the
    delivery's own response or the agent's push. Returns the original sender
    when the answer now has to be delivered to it. The state change and the
    reply row are one transaction, so a relay that sees "delivered" also sees
    the answer."""
    now = time.time()
    with opstore.connect() as db:
        # Write first: an UPDATE takes the write lock, so the read below is current.
        prior = next((s for s in ("relaying", "delivering", "working")
                      if db.execute("UPDATE mailbox SET state = 'delivered', last_error = NULL, updated_at = ?"
                                    " WHERE id = ? AND kind = 'message' AND state = ?",
                                    (now, message_id, s)).rowcount), None)
        if prior is None:
            # Answered already through the other channel. Keep the longer text:
            # a push carries only the first PUSH_TEXT_LIMIT characters.
            db.execute("UPDATE mailbox SET body = ? WHERE reply_to = ? AND kind = 'reply' AND length(body) < ?",
                       (text, message_id, len(text)))
            return None
        row = db.execute("SELECT * FROM mailbox WHERE id = ?", (message_id,)).fetchone()
        # A live relay that is still waiting hands the answer over itself.
        waiting = prior == "relaying" and now - row["created_at"] < HANDOFF_SECONDS + 60
        handed = waiting or row["sender"] == mesh.BEACON_PEER
        _insert(db, row["recipient"], row["sender"], "reply", text, reply_to=message_id,
                state="delivered" if handed else "queued")
    return None if handed else row["sender"]


def _handed_off(row, pushed: bool, error: str) -> None:
    """No answer within the wait, but it arrived, so it is never sent again. A
    message waits for the agent's push (or is uncertain without one); a notice
    or reply needs no answer, so it counts as delivered."""
    state = "delivered" if row["kind"] != "message" else "working" if pushed else "uncertain"
    _move(row["id"], "delivering", state, error)


def _move(message_id: int, from_state: str, to_state: str, error: str | None = None) -> bool:
    """Change state only from `from_state`; False if something else changed it first."""
    with opstore.connect() as db:
        return db.execute("UPDATE mailbox SET state = ?, last_error = ?, updated_at = ? WHERE id = ? AND state = ?",
                          (to_state, error, time.time(), message_id, from_state)).rowcount == 1


def _reply_body(message_id: int) -> str:
    with opstore.connect() as db:
        row = db.execute("SELECT body FROM mailbox WHERE reply_to = ? AND kind = 'reply' ORDER BY id DESC LIMIT 1",
                         (message_id,)).fetchone()
    return row["body"] if row else ""


def _forget(message_id: int) -> None:
    """A relay that was answered keeps nothing, as before there was a mailbox."""
    with opstore.connect() as db:
        db.execute("DELETE FROM mailbox WHERE id = ? OR reply_to = ?", (message_id, message_id))


def _claim(recipient_id: str):
    """Atomically take the oldest queued item, so two pulls never deliver it twice."""
    now = time.time()
    with opstore.connect() as db:
        db.execute("UPDATE mailbox SET state = 'queued' WHERE state = 'delivering' AND updated_at < ?",
                   (now - STALE_DELIVERY_SECONDS,))
        row = db.execute("SELECT * FROM mailbox WHERE recipient = ? AND state = 'queued' ORDER BY id LIMIT 1",
                         (recipient_id,)).fetchone()
        if row is None:
            return None
        claimed = db.execute("UPDATE mailbox SET state = 'delivering', attempts = attempts + 1, updated_at = ?"
                             " WHERE id = ? AND state = 'queued'", (now, row["id"])).rowcount
    return row if claimed else _claim(recipient_id)


def _finish(message_id: int, state: str, error: str | None = None) -> None:
    with opstore.connect() as db:
        db.execute("UPDATE mailbox SET state = ?, last_error = ?, updated_at = ? WHERE id = ?",
                   (state, error, time.time(), message_id))


def _expire() -> None:
    """Expire what waited too long, and tell each sender: held messages after
    TTL_SECONDS undelivered, handed-off ones when no answer came in time."""
    now = time.time()
    with opstore.connect() as db:
        rows = db.execute("SELECT * FROM mailbox WHERE (state = 'queued' AND expires_at < ?)"
                          " OR (state IN ('working', 'relaying') AND updated_at < ?)",
                          (now, now - WORK_DEADLINE_SECONDS)).fetchall()
        for row in rows:
            undelivered = row["state"] == "queued"
            why = (f"Your message to {row['recipient']} from {_when(row['created_at'])} expired undelivered"
                   if undelivered else
                   f"{row['recipient']} did not answer your message from {_when(row['created_at'])} within "
                   f"{mesh.REPLY_WINDOW_SECONDS // 3600} h")
            if not db.execute("UPDATE mailbox SET state = 'expired', last_error = ?, updated_at = ?"
                              " WHERE id = ? AND state = ?", (why, now, row["id"], row["state"])).rowcount:
                continue  # another worker expired it first
            if row["kind"] == "message":
                state = "delivered" if row["sender"] == mesh.BEACON_PEER else "queued"
                _insert(db, mesh.BEACON_PEER, row["sender"], "notice", f"{why}: \"{row['body'][:SNIPPET]}\"",
                        reply_to=row["id"], state=state)


def _framed(row) -> str:
    if row["kind"] == "message":
        return (f"[Delivered by the Beacon mailbox: {row['sender']} sent this at {_when(row['created_at'])} while "
                f"you were unreachable. Answer normally, however long the work takes; Beacon passes your answer "
                f"back to {row['sender']}.]\n\n{row['body']}")
    if row["kind"] == "reply":
        with opstore.connect() as db:
            original = db.execute("SELECT * FROM mailbox WHERE id = ?", (row["reply_to"],)).fetchone()
        about = f" to your message of {_when(original['created_at'])}: \"{original['body'][:SNIPPET]}\"" if original else ""
        return (f"[Reply from {row['sender']} via the Beacon mailbox{about}. No answer is needed; "
                f"anything you say here is not passed on.]\n\n{row['body']}")
    return f"[Notice from the Beacon mailbox]\n\n{row['body']}"


def _when(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(timestamp))
