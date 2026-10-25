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
    now = time.time()
    with opstore.connect() as db:
        return db.execute(
            "INSERT INTO mailbox (sender, recipient, kind, body, context_id, reply_to, state, created_at,"
            " updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sender, recipient, kind, body, context_id, reply_to, state, now, now, now + TTL_SECONDS),
        ).lastrowid


def send(caller: str, target_id: str, text: str, context_id: str | None) -> dict:
    """Relay `text` now if the target answers; otherwise keep it. Returns a task view."""
    try:
        return bus.call(store.get_agent(target_id), text, context_id, token=mesh.token(caller, target_id))
    except bus.Unreachable as exc:
        if not exc.transient:
            raise
    message_id = enqueue(caller, target_id, "message", text, context_id)
    return {
        "task_id": f"mbx-{message_id}",
        "context_id": context_id or f"mbx-ctx-{message_id}",
        "state": "TASK_STATE_SUBMITTED",
        "reply": (f"{target_id} is unreachable right now. Beacon is holding the message and will deliver it "
                  f"when {target_id} is back online; the reply will reach you later as a new message."),
    }


def recent(limit: int = 50) -> list[dict]:
    with opstore.connect() as db:
        rows = db.execute("SELECT * FROM mailbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def task_view(task_id: str, *, requester: str | None = None, target: str | None = None) -> dict:
    """Return an A2A Task-shaped snapshot for a durable mailbox task.

    ``mbx-N`` identifies the original message. A completed reply is stored as
    a separate mailbox row linked by ``reply_to``; it is folded into the task
    snapshot so callers can poll without knowing Beacon's internal schema.
    A2A callers are restricted to the original sender and target.
    """
    if not isinstance(task_id, str) or not task_id.startswith("mbx-"):
        raise store.NotFound(f"task {task_id!r} not found")
    try:
        message_id = int(task_id.removeprefix("mbx-"))
    except ValueError as exc:
        raise store.NotFound(f"task {task_id!r} not found") from exc
    with opstore.connect() as db:
        row = db.execute("SELECT * FROM mailbox WHERE id = ? AND kind = 'message'", (message_id,)).fetchone()
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
    state = _task_state(row["state"], bool(reply))
    text = (reply["body"] if reply else "") or _task_message(row["state"], row["recipient"])
    return {
        "id": task_id,
        "contextId": row["context_id"] or f"mbx-ctx-{row['id']}",
        "status": {"state": state, "message": {"role": "ROLE_AGENT", "parts": [{"text": text}]}},
        "mailbox": {"message_id": row["id"], "state": row["state"], "attempts": row["attempts"],
                    "last_error": row["last_error"], "updated_at": row["updated_at"],
                    "expires_at": row["expires_at"], "reply_id": reply["id"] if reply else None},
    }


def _task_state(mailbox_state: str, has_reply: bool) -> str:
    if has_reply:
        return STATE_COMPLETED
    if mailbox_state in {"failed", "expired"}:
        return STATE_FAILED
    if mailbox_state in {"delivering", "delivered"}:
        return STATE_WORKING
    return STATE_SUBMITTED


def _task_message(mailbox_state: str, recipient: str) -> str:
    if mailbox_state == "uncertain":
        return f"Delivery to {recipient} is uncertain; Beacon will not retry automatically."
    if mailbox_state == "failed":
        return f"Delivery to {recipient} failed."
    if mailbox_state == "expired":
        return f"Delivery to {recipient} expired before it was delivered."
    if mailbox_state == "delivering":
        return f"Beacon is delivering the message to {recipient}."
    return f"Beacon is holding the message for {recipient}."


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
        try:
            view = bus.call(agent, _framed(row), row["context_id"] if row["kind"] == "message" else None,
                            token=_delivery_token(row, recipient_id))
        except bus.Unreachable as exc:
            state = "queued" if exc.transient else ("uncertain" if exc.uncertain else "failed")
            _finish(row["id"], state, str(exc))
            if exc.transient or exc.uncertain:
                break  # retry only known-not-arrived work; uncertain delivery must not duplicate
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
            items.append({"id": row["id"], "text": _framed(row), "token": _delivery_token(row, recipient_id),
                          "context_id": row["context_id"] if row["kind"] == "message" else None})
    return items


def report(recipient_id: str, message_id: int, ok: bool, reply: str = "", transient: bool = False,
           error: str = "", *, uncertain: bool = False) -> str | None:
    """The sync's result for one claimed item. Returns an original sender
    whose reply should now be delivered (call deliver_pending for it)."""
    with opstore.connect() as db:
        row = db.execute("SELECT * FROM mailbox WHERE id = ? AND recipient = ? AND state = 'delivering'",
                         (message_id, recipient_id)).fetchone()
    if row is None:
        raise store.NotFound(f"no delivery in progress for message {message_id} to {recipient_id}")
    if not ok:
        state = "queued" if transient else "uncertain" if uncertain else "failed"
        _finish(row["id"], state, error[:500] or "local delivery failed")
        return None
    return _delivered(row, reply or "(no text)")


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
    _finish(row["id"], "delivered")
    if row["kind"] != "message":
        return None
    if row["sender"] == mesh.BEACON_PEER:
        enqueue(row["recipient"], mesh.BEACON_PEER, "reply", reply, reply_to=row["id"], state="delivered")
        return None
    enqueue(row["recipient"], row["sender"], "reply", reply, reply_to=row["id"])
    return row["sender"]


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
    """Expire what waited too long, and tell each sender."""
    now = time.time()
    with opstore.connect() as db:
        rows = db.execute("SELECT * FROM mailbox WHERE state = 'queued' AND expires_at < ?", (now,)).fetchall()
        db.executemany("UPDATE mailbox SET state = 'expired', updated_at = ? WHERE id = ?",
                       [(now, row["id"]) for row in rows])
    for row in rows:
        if row["kind"] != "message":
            continue
        notice = (f"Your message to {row['recipient']} from {_when(row['created_at'])} expired undelivered: "
                  f"\"{row['body'][:SNIPPET]}\"")
        state = "delivered" if row["sender"] == mesh.BEACON_PEER else "queued"
        enqueue(mesh.BEACON_PEER, row["sender"], "notice", notice, reply_to=row["id"], state=state)


def _framed(row) -> str:
    if row["kind"] == "message":
        return (f"[Delivered by the Beacon mailbox: {row['sender']} sent this at {_when(row['created_at'])} while "
                f"you were unreachable. Answer normally; Beacon passes your answer back to {row['sender']}.]\n\n"
                f"{row['body']}")
    if row["kind"] == "reply":
        with opstore.connect() as db:
            original = db.execute("SELECT * FROM mailbox WHERE id = ?", (row["reply_to"],)).fetchone()
        about = f" to your message of {_when(original['created_at'])}: \"{original['body'][:SNIPPET]}\"" if original else ""
        return (f"[Reply from {row['sender']} via the Beacon mailbox{about}. No answer is needed; "
                f"anything you say here is not passed on.]\n\n{row['body']}")
    return f"[Notice from the Beacon mailbox]\n\n{row['body']}"


def _when(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(timestamp))
