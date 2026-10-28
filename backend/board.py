"""The fleet board: shared cards for work that outlives one conversation.

Hermes Kanban stops at the host boundary, and A2A caps one thread at a few
turns, so multi-step work between agents needs a durable record: a card
with a title, an assignee, a state, a project, and a history of who did
what — including the A2A context_ids it was worked under. Cards live in
the operational store (backend/opstore.py), never in fleet YAML.

Every change is recorded with its author: an agent (through its own token
on /board/mcp), "beacon" (MCP bearer), or a logged-in person. Assigning a
card to an agent sends that agent a notice through the mailbox, so it
hears about the work even while offline.
"""

import threading
import time

from . import mailbox, mesh, opstore, store

STATES = ("todo", "doing", "blocked", "review", "done")
NOTICE_BODY = 600


def list_cards(project: str | None = None, assignee: str | None = None, state: str | None = None,
               include_done: bool = False) -> list[dict]:
    clauses, params = [], []
    for column, value in (("project", project), ("assignee", assignee), ("state", state)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if not include_done and state != "done":
        clauses.append("state != 'done'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with opstore.connect() as db:
        rows = db.execute(f"SELECT * FROM cards {where} ORDER BY updated_at DESC", params).fetchall()
    return [dict(row) for row in rows]


def get_card(card_id: int) -> dict:
    """A card with its full history. An `assign_notice` event carries the
    mailbox delivery state live (queued/delivering/delivered/failed/uncertain/expired,
    attempts, last_error) under `notice`, separate from the card mutation
    itself — the assign event records succeeded the instant the card
    changed; the notice may still be queued, retried, or have failed."""
    with opstore.connect() as db:
        card = db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if card is None:
            raise store.NotFound(f"card {card_id} not found")
        events = db.execute("SELECT * FROM card_events WHERE card_id = ? ORDER BY id", (card_id,)).fetchall()
        notice_ids = [e["notice_id"] for e in events if e["notice_id"] is not None]
        notices = {}
        if notice_ids:
            placeholders = ",".join("?" * len(notice_ids))
            for row in db.execute(f"SELECT * FROM mailbox WHERE id IN ({placeholders})", notice_ids):
                notices[row["id"]] = {"task_id": f"mbx-{row['id']}", "state": row["state"],
                                      "attempts": row["attempts"], "last_error": row["last_error"],
                                      "updated_at": row["updated_at"]}
    out_events = []
    for e in events:
        event = dict(e)
        if e["notice_id"] is not None:
            event["notice"] = notices.get(e["notice_id"])
        out_events.append(event)
    return {**dict(card), "events": out_events}


def create_card(author: str, title: str, body: str = "", project: str | None = None,
                assignee: str | None = None, state: str = "todo") -> dict:
    title = _required(title, "title")
    _check_state(state)
    if assignee:
        _check_may_assign(author, assignee)
    now = time.time()
    with opstore.connect() as db:
        card_id = db.execute(
            "INSERT INTO cards (title, body, project, assignee, state, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, body or "", project or None, assignee or None, state, author, now, now)).lastrowid
        _event(db, card_id, author, "created", title)
    if assignee:
        _notify_assignee(card_id, author, assignee)
    return get_card(card_id)


def update_card(author: str, card_id: int, *, state: str | None = None, assignee: str | None = None,
                title: str | None = None, body: str | None = None, project: str | None = None,
                note: str | None = None, context_id: str | None = None) -> dict:
    """Change what's given; each change is its own event. `assignee=""`
    unassigns. `note` explains the change and is recorded with it."""
    card = get_card(card_id)
    changes = []
    if state is not None and state != card["state"]:
        _check_state(state)
        changes.append(("state", "state", state, f"{card['state']} -> {state}"))
    if assignee is not None and (assignee or None) != card["assignee"]:
        if assignee:
            _check_may_assign(author, assignee)
        changes.append(("assignee", "assign", assignee or None, f"{card['assignee'] or '-'} -> {assignee or '-'}"))
    for column, value in (("title", title), ("body", body), ("project", project)):
        if value is not None and value != (card[column] or ""):
            changes.append((column, "edit", value if column != "title" else _required(value, "title"),
                            f"{column} changed"))
    if not changes and not note:
        return card
    now = time.time()
    with opstore.connect() as db:
        for column, kind, value, summary in changes:
            db.execute(f"UPDATE cards SET {column} = ?, updated_at = ? WHERE id = ?", (value, now, card_id))
            _event(db, card_id, author, kind, f"{summary}" + (f": {note}" if note else ""), context_id)
        if note and not changes:
            _event(db, card_id, author, "comment", note, context_id)
            db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (now, card_id))
    new_assignee = next((value for column, _, value, _ in changes if column == "assignee"), None)
    if new_assignee:
        _notify_assignee(card_id, author, new_assignee)
    elif note and not changes:
        _notify_comment(card_id, author, note)
    return get_card(card_id)


def comment(author: str, card_id: int, text: str, context_id: str | None = None) -> dict:
    get_card(card_id)
    with opstore.connect() as db:
        _event(db, card_id, author, "comment", _required(text, "comment"), context_id)
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (time.time(), card_id))
    _notify_comment(card_id, author, text)
    return get_card(card_id)


def _event(db, card_id: int, author: str, kind: str, body: str, context_id: str | None = None,
          notice_id: int | None = None) -> None:
    db.execute("INSERT INTO card_events (card_id, author, kind, body, context_id, notice_id, created_at)"
               " VALUES (?, ?, ?, ?, ?, ?, ?)",
               (card_id, author, kind, body, context_id or None, notice_id, time.time()))


def _notify_assignee(card_id: int, author: str, assignee: str) -> None:
    """Tell an assigned agent through the mailbox — it may be offline, and it
    may not have the board tools, so the notice carries the card itself.

    The card mutation that triggered this already committed before this runs
    (create_card/update_card call it after their own `with opstore.connect()`
    block), so assignment is durable and visible immediately regardless of
    how long mailbox delivery takes. The `assign_notice` event records the
    mailbox row id so the card can show the notice's own delivery task id and
    state (queued/delivered/failed) without conflating it with the assignment
    itself. Only called when the assignee actually changed, so a repeated
    update with the same assignee and no other change never re-enqueues."""
    card = get_card(card_id)
    text = (f"You were assigned card #{card_id} on the Beacon board by {author}.\n"
            f"Title: {card['title']}\nProject: {card['project'] or '-'}\nState: {card['state']}\n\n"
            f"{card['body'][:NOTICE_BODY]}")
    _send_notice(card_id, author, assignee, "assign_notice", text)


def _notify_comment(card_id: int, author: str, text: str) -> None:
    """A comment on a card assigned to an agent reaches that agent too, or the
    comment would sit in the card's history unseen by the one who has to act on
    it. Not when the assignee wrote it, so a reply never notifies its writer."""
    card = get_card(card_id)
    if not card["assignee"]:
        return
    _send_notice(card_id, author, card["assignee"], "comment_notice",
                 f"{author} commented on card #{card_id} on the Beacon board, which is assigned to you.\n"
                 f"Title: {card['title']}\nState: {card['state']}\n\n{text[:NOTICE_BODY]}")


def _send_notice(card_id: int, author: str, assignee: str, kind: str, text: str) -> None:
    if assignee == author:
        return
    try:
        agent = store.get_agent(assignee)
    except (store.NotFound, store.InvalidId):
        return  # a person, or not in the fleet: nobody to message
    if agent.reach is None:
        return
    sender = author if _is_agent(author) else mesh.BEACON_PEER
    notice_id = mailbox.enqueue(sender, assignee, "notice", text)
    with opstore.connect() as db:
        _event(db, card_id, mesh.BEACON_PEER, kind, f"notice queued for {assignee}", notice_id=notice_id)
    # Deliver now if it's online; otherwise its next mesh pull does. This may
    # be slow or fail transiently; the assignment above already returned.
    threading.Thread(target=mailbox.deliver_pending, args=(assignee,), daemon=True).start()


def _check_may_assign(author: str, assignee: str) -> None:
    """Handing an agent work is a way of calling it: an agent may assign
    only to agents that accept calls from it (mesh.may_call). People and
    Beacon are not limited."""
    if not _is_agent(author) or not _is_agent(assignee):
        return
    if not mesh.may_call(author, store.get_agent(assignee)):
        raise ValueError(f"{assignee} does not accept work from {author}")


def _is_agent(name: str) -> bool:
    try:
        return store.get_agent(name).reach is not None
    except (store.NotFound, store.InvalidId):
        return False


def _check_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"state {state!r} must be one of {', '.join(STATES)}")


def _required(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must not be empty")
    return value.strip()
