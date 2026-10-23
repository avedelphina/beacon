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
    with opstore.connect() as db:
        card = db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if card is None:
            raise store.NotFound(f"card {card_id} not found")
        events = db.execute("SELECT * FROM card_events WHERE card_id = ? ORDER BY id", (card_id,)).fetchall()
    return {**dict(card), "events": [dict(e) for e in events]}


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
    return get_card(card_id)


def comment(author: str, card_id: int, text: str, context_id: str | None = None) -> dict:
    get_card(card_id)
    with opstore.connect() as db:
        _event(db, card_id, author, "comment", _required(text, "comment"), context_id)
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (time.time(), card_id))
    return get_card(card_id)


def _event(db, card_id: int, author: str, kind: str, body: str, context_id: str | None = None) -> None:
    db.execute("INSERT INTO card_events (card_id, author, kind, body, context_id, created_at)"
               " VALUES (?, ?, ?, ?, ?, ?)", (card_id, author, kind, body, context_id or None, time.time()))


def _notify_assignee(card_id: int, author: str, assignee: str) -> None:
    """Tell an assigned agent through the mailbox — it may be offline, and it
    may not have the board tools, so the notice carries the card itself."""
    if assignee == author:
        return
    try:
        agent = store.get_agent(assignee)
    except (store.NotFound, store.InvalidId):
        return  # a person, or not in the fleet: nobody to message
    if agent.reach is None:
        return
    card = get_card(card_id)
    sender = author if _is_agent(author) else mesh.BEACON_PEER
    text = (f"You were assigned card #{card_id} on the Beacon board by {author}.\n"
            f"Title: {card['title']}\nProject: {card['project'] or '-'}\nState: {card['state']}\n\n"
            f"{card['body'][:NOTICE_BODY]}")
    mailbox.enqueue(sender, assignee, "notice", text)
    # Deliver now if it's online; otherwise its next mesh pull does.
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
