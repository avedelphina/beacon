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

A notice alone was not enough: an agent acts only during a turn, so a card it
was told about once could sit in todo (or doing, blocked, review) for days.
remind() runs on every pull and asks whoever has to move a card that nothing
has changed on for the agent's `remind_after`, then tells people by email when
reminders did not help (see remind()).

A card also says what moves it on (`next_action`) and when it is finished
(`done_when`), because the notice and the reminder are what an agent acts on,
and "nothing changed for a day" is no help without the next step. `links`
point out to PRs and issues elsewhere. They are labels only: Beacon never
fetches them, copies their state, or treats one as approval for anything.

`references` are the structured form of the same idea: provider, kind, external
id, URL and label, optionally tied to a workflow on the same card, with a
`last_seen_state` that whoever added the reference typed by hand. The state is a
hint with an age, not Beacon's knowledge: nothing fetches it, nothing checks it,
and no decision rests on it. Provider and kind are free text, so a new tracker
needs no schema change. `links` stay as they were, for clients that use them.
"""

import json
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from . import mailbox, mesh, opstore, store

STATES = ("todo", "doing", "blocked", "review", "done")
# Why a card is blocked. Each needs different handling, so each is reminded differently (remind()):
# unclear: the agent does not know how to go on, so it consults a peer and tries; waiting: the next
# step depends on a named thing, which is watched, not re-asked; cannot: the agent lacks a capability
# or access, and a person is told now.
BLOCKED_REASONS = ("unclear", "waiting", "cannot")
NOTICE_BODY = 600
ANSWER_CHARS = 1500


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
        delivery = _deliveries(db)
        open_deps = {r["card_id"]: r["n"] for r in db.execute(
            "SELECT d.card_id, count(*) AS n FROM card_dependencies d JOIN cards c ON c.id = d.depends_on_id"
            " WHERE c.state != 'done' GROUP BY d.card_id")}
    return [_card(row) | {"delivery": delivery.get(row["id"]), "blocked_by_count": open_deps.get(row["id"], 0)}
            for row in rows]


def _deliveries(db) -> dict[int, dict]:
    """Per card, its latest notice and whether the agent answered it: so the
    board shows at a glance whether an agent still has to sync, is working on
    it, or answered, without opening each card."""
    out = {}
    for row in db.execute(
            "SELECT e.card_id, m.recipient, m.state, m.created_at, m.last_error FROM card_events e"
            " JOIN mailbox m ON m.id = e.notice_id WHERE e.id IN"
            " (SELECT max(id) FROM card_events WHERE notice_id IS NOT NULL GROUP BY card_id)"):
        out[row["card_id"]] = {"to": row["recipient"], "state": row["state"], "sent_at": row["created_at"],
                               "error": row["last_error"], "answered_at": None}
    for row in db.execute("SELECT card_id, max(created_at) AS at FROM card_events WHERE kind = 'answer'"
                          " GROUP BY card_id"):
        if row["card_id"] in out and row["at"] >= out[row["card_id"]]["sent_at"]:
            out[row["card_id"]]["answered_at"] = row["at"]
    return out


def record_answer(notice_id: int, agent: str, text: str) -> None:
    """What an agent said when a board notice (assignment, comment, reminder)
    reached it: its acknowledgement, kept on each card the notice was about, so
    a person sees that it arrived and what the agent made of it. Not a change
    to the card (updated_at stays): an agent that only says "will do" is still
    reminded when nothing else happens."""
    with opstore.connect() as db:
        for row in db.execute("SELECT DISTINCT card_id FROM card_events WHERE notice_id = ?", (notice_id,)).fetchall():
            _event(db, row["card_id"], agent, "answer", text.strip()[:ANSWER_CHARS])


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
        references = [dict(r) for r in db.execute(
            "SELECT * FROM card_references WHERE card_id = ? ORDER BY id", (card_id,))]
        parent = db.execute("SELECT id, title, state, assignee, created_by FROM cards WHERE id = ?",
                            (card["parent_id"],)).fetchone() if card["parent_id"] else None
        children = [dict(r) for r in db.execute(
            "SELECT id, title, state, assignee, created_by FROM cards WHERE parent_id = ? ORDER BY id", (card_id,))]
        depends_on = [dict(r) for r in db.execute(
            "SELECT c.id, c.title, c.state, c.assignee, c.created_by FROM card_dependencies d"
            " JOIN cards c ON c.id = d.depends_on_id WHERE d.card_id = ? ORDER BY c.id", (card_id,))]
        blocks = [dict(r) for r in db.execute(
            "SELECT c.id, c.title, c.state, c.assignee, c.created_by FROM card_dependencies d"
            " JOIN cards c ON c.id = d.card_id WHERE d.depends_on_id = ? ORDER BY c.id", (card_id,))]
        notice_ids = [e["notice_id"] for e in events if e["notice_id"] is not None]
        notices = {}
        if notice_ids:
            placeholders = ",".join("?" * len(notice_ids))
            for row in db.execute(f"SELECT * FROM mailbox WHERE id IN ({placeholders})", notice_ids):
                notices[row["id"]] = {"task_id": f"mbx-{row['id']}", "to": row["recipient"], "state": row["state"],
                                      "attempts": row["attempts"], "last_error": row["last_error"],
                                      "updated_at": row["updated_at"]}
    out_events = []
    for e in events:
        event = dict(e)
        if e["notice_id"] is not None:
            event["notice"] = notices.get(e["notice_id"])
        out_events.append(event)
    return {**_card(card), "references": references, "events": out_events,
            "parent": dict(parent) if parent else None, "children": children, "depends_on": depends_on,
            # Computed on every read from the dependencies and their cards' states, never stored:
            # it is gone the moment the last unfinished dependency reaches done.
            "blocked_by": [c for c in depends_on if c["state"] != "done"], "blocks": blocks}


def create_card(author: str, title: str, body: str = "", project: str | None = None,
                assignee: str | None = None, state: str = "todo", *, next_action: str | None = None,
                done_when: str | None = None, links: list | None = None) -> dict:
    title = _required(title, "title")
    _check_state(state)
    links = _links(links or [])
    if assignee:
        _check_may_assign(author, assignee)
    now = time.time()
    with opstore.connect() as db:
        card_id = db.execute(
            "INSERT INTO cards (title, body, project, assignee, state, next_action, done_when, links,"
            " created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, body or "", project or None, assignee or None, state, _text(next_action), _text(done_when),
             json.dumps(links), author, now, now)).lastrowid
        _event(db, card_id, author, "created", title)
    if assignee:
        _notify_assignee(card_id, author, assignee)
    return get_card(card_id)


def update_card(author: str, card_id: int, *, state: str | None = None, assignee: str | None = None,
                title: str | None = None, body: str | None = None, project: str | None = None,
                next_action: str | None = None, done_when: str | None = None, links: list | None = None,
                note: str | None = None, context_id: str | None = None, blocked_reason: str | None = None,
                waiting_on: str | None = None) -> dict:
    """Change what's given; each change is its own event. `assignee=""`
    unassigns, `next_action=""`/`done_when=""` clear, `links` replaces the list.
    `note` explains the change and is recorded with it. Moving a card to blocked
    needs `blocked_reason` (and `waiting_on` for waiting and cannot): see _blocked_fields."""
    card = get_card(card_id)
    changes = []
    if state is not None and state != card["state"]:
        _check_state(state)
        changes.append(("state", "state", state, f"{card['state']} -> {state}"))
    reason, on = _blocked_fields(card, state if state is not None else card["state"], blocked_reason, waiting_on)
    if reason != card["blocked_reason"]:
        changes.append(("blocked_reason", "edit", reason, _blocked_summary(reason, on)))
        if on != card["waiting_on"]:
            changes.append(("waiting_on", "edit", on, None))  # said in the reason's event
    elif on != card["waiting_on"]:
        changes.append(("waiting_on", "edit", on, f"waiting on: {on or '-'}"))
    if assignee is not None and (assignee or None) != card["assignee"]:
        if assignee:
            _check_may_assign(author, assignee)
        changes.append(("assignee", "assign", assignee or None, f"{card['assignee'] or '-'} -> {assignee or '-'}"))
    for column, value in (("title", title), ("body", body), ("project", project)):
        if value is not None and value != (card[column] or ""):
            changes.append((column, "edit", value if column != "title" else _required(value, "title"),
                            f"{column} changed"))
    # The new text goes into the event: the history then shows how the plan changed.
    for column, value in (("next_action", next_action), ("done_when", done_when)):
        if value is not None and _text(value) != card[column]:
            changes.append((column, "edit", _text(value), f"{column.replace('_', ' ')}: {_text(value) or '-'}"))
    if links is not None and (links := _links(links)) != card["links"]:
        changes.append(("links", "edit", json.dumps(links), "links changed"))
    if not changes and not note:
        return card
    now = time.time()
    with opstore.connect() as db:
        for column, kind, value, summary in changes:
            db.execute(f"UPDATE cards SET {column} = ?, updated_at = ? WHERE id = ?", (value, now, card_id))
            if summary is not None:
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


# --- relationships: a parent, and what a card depends on ----------------------


def restrict(card: dict, may_see) -> dict:
    """The card with the related cards its viewer may not see taken out. A related card
    is only a title and a state, but a title is still something to keep from someone who
    may not read that card. `may_see` gets each related card's id, title, state, assignee
    and creator."""
    out = {**card}
    for key in ("children", "depends_on", "blocked_by", "blocks"):
        out[key] = [c for c in card[key] if may_see(c)]
    if card["parent"] is not None and not may_see(card["parent"]):
        out["parent"], out["parent_id"] = None, None
    return out


def set_parent(author: str, card_id: int, parent_id: int | None, context_id: str | None = None) -> dict:
    """Make a card part of another (`parent_id`), or free it (None). A card has at most one
    parent, and a card cannot sit under itself or under anything below it."""
    card = get_card(card_id)
    if parent_id is None:
        if card["parent_id"] is None:
            return card
        body = f"no longer part of #{card['parent_id']}"
    else:
        parent_id = _card_number(parent_id, "parent_id")
        if parent_id == card_id:
            raise ValueError("a card cannot be its own parent")
        parent = get_card(parent_id)  # not found, before anything else
        if parent_id == card["parent_id"]:
            return card
        body = f"part of #{parent_id}: {parent['title'][:80]}"
    now = time.time()
    with opstore.connect() as db:
        if parent_id is not None and card_id in _ancestors(db, parent_id):
            raise ValueError(f"#{parent_id} is already part of #{card_id}: that would make a loop")
        db.execute("UPDATE cards SET parent_id = ?, updated_at = ? WHERE id = ?", (parent_id, now, card_id))
        _event(db, card_id, author, "relation", body, context_id)
    return get_card(card_id)


def add_dependency(author: str, card_id: int, depends_on_id: int, context_id: str | None = None) -> dict:
    """The card cannot finish until `depends_on_id` is done. Beacon computes the card's
    `blocked_by` from this; the card's own state is not touched."""
    get_card(card_id)
    depends_on_id = _card_number(depends_on_id, "depends_on")
    if depends_on_id == card_id:
        raise ValueError("a card cannot depend on itself")
    target = get_card(depends_on_id)
    now = time.time()
    with opstore.connect() as db:
        if db.execute("SELECT 1 FROM card_dependencies WHERE card_id = ? AND depends_on_id = ?",
                      (card_id, depends_on_id)).fetchone():
            raise ValueError(f"#{card_id} already depends on #{depends_on_id}")
        if card_id in _dependencies_of(db, depends_on_id):
            raise ValueError(f"#{depends_on_id} already depends on #{card_id}, directly or through other cards: "
                             "that would make a loop")
        db.execute("INSERT INTO card_dependencies (card_id, depends_on_id, created_by, created_at) VALUES (?, ?, ?, ?)",
                   (card_id, depends_on_id, author, now))
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (now, card_id))
        _event(db, card_id, author, "relation", f"depends on #{depends_on_id}: {target['title'][:80]}", context_id)
    return get_card(card_id)


def remove_dependency(author: str, card_id: int, depends_on_id: int, context_id: str | None = None) -> dict:
    get_card(card_id)
    depends_on_id = _card_number(depends_on_id, "depends_on")
    now = time.time()
    with opstore.connect() as db:
        if db.execute("DELETE FROM card_dependencies WHERE card_id = ? AND depends_on_id = ?",
                      (card_id, depends_on_id)).rowcount == 0:
            raise store.NotFound(f"#{card_id} does not depend on #{depends_on_id}")
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (now, card_id))
        _event(db, card_id, author, "relation", f"no longer depends on #{depends_on_id}", context_id)
    return get_card(card_id)


def _card_number(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be a card number")
    return value


def _ancestors(db, card_id: int) -> set[int]:
    """The card and every card above it, following parents."""
    seen, cursor = set(), card_id
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        row = db.execute("SELECT parent_id FROM cards WHERE id = ?", (cursor,)).fetchone()
        cursor = row["parent_id"] if row else None
    return seen


def _dependencies_of(db, card_id: int) -> set[int]:
    """Every card this one depends on, directly or through others."""
    seen, todo = set(), [card_id]
    while todo:
        for row in db.execute("SELECT depends_on_id FROM card_dependencies WHERE card_id = ?", (todo.pop(),)):
            if row["depends_on_id"] not in seen:
                seen.add(row["depends_on_id"])
                todo.append(row["depends_on_id"])
    return seen


# --- references: structured pointers to work elsewhere -----------------------

MAX_REFERENCES = 50
_REFERENCE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_REFERENCE_FIELDS = ("provider", "kind", "external_id", "url", "label", "workflow_id",
                     "last_seen_state", "last_seen_at")


def add_reference(author: str, card_id: int, *, provider: str, kind: str, external_id: str, url: str,
                  label: str, workflow_id: int | None = None, last_seen_state: str | None = None,
                  last_seen_at: float | None = None, context_id: str | None = None) -> dict:
    """Add a pointer to work elsewhere. The card's history records it with the author
    and the A2A context it was made under, like every other change. A hint state
    with no time is dated now: a hint without an age cannot be judged."""
    get_card(card_id)  # a missing card is "not found", before anything else is checked
    now = time.time()
    fields = _reference_fields(provider, kind, external_id, url, label, last_seen_state, last_seen_at, now)
    with opstore.connect() as db:
        _check_reference_workflow(db, card_id, workflow_id)
        held = db.execute("SELECT count(*) FROM card_references WHERE card_id = ?", (card_id,)).fetchone()[0]
        if held >= MAX_REFERENCES:
            raise ValueError(f"a card holds at most {MAX_REFERENCES} references")
        try:
            ref_id = db.execute(
                "INSERT INTO card_references (card_id, provider, kind, external_id, url, label, workflow_id,"
                " last_seen_state, last_seen_at, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (card_id, fields["provider"], fields["kind"], fields["external_id"], fields["url"], fields["label"],
                 workflow_id, fields["last_seen_state"], fields["last_seen_at"], author, now, now)).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"the card already has a {fields['provider']} {fields['kind']} "
                             f"{fields['external_id']} reference") from None
        _event(db, card_id, author, "reference", f"added {_describe_reference(fields)}", context_id)
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (now, card_id))
        return dict(db.execute("SELECT * FROM card_references WHERE id = ?", (ref_id,)).fetchone())


def update_reference(author: str, card_id: int, ref_id: int, *, context_id: str | None = None, **changes) -> dict:
    """Change the fields given; a field left out stays. None clears `workflow_id` and the
    `last_seen_*` hint. Setting a new `last_seen_state` without `last_seen_at` dates it now."""
    unknown = set(changes) - set(_REFERENCE_FIELDS)
    if unknown:
        raise ValueError(f"unknown reference field(s): {', '.join(sorted(unknown))}")
    now = time.time()
    with opstore.connect() as db:
        row = db.execute("SELECT * FROM card_references WHERE id = ? AND card_id = ?", (ref_id, card_id)).fetchone()
        if row is None:
            raise store.NotFound(f"reference {ref_id} not found on card {card_id}")
        current = dict(row)
        merged = {**current, **changes}
        if "last_seen_state" in changes and "last_seen_at" not in changes:
            merged["last_seen_at"] = None  # dated now below, if there is a state
        merged.update(_reference_fields(merged["provider"], merged["kind"], merged["external_id"], merged["url"],
                                        merged["label"], merged["last_seen_state"], merged["last_seen_at"], now))
        if "workflow_id" in changes:
            _check_reference_workflow(db, card_id, merged["workflow_id"])
        changed = [name for name in _REFERENCE_FIELDS if merged[name] != current[name]]
        if not changed:
            return current
        try:
            db.execute(
                "UPDATE card_references SET provider = ?, kind = ?, external_id = ?, url = ?, label = ?,"
                " workflow_id = ?, last_seen_state = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
                (merged["provider"], merged["kind"], merged["external_id"], merged["url"], merged["label"],
                 merged["workflow_id"], merged["last_seen_state"], merged["last_seen_at"], now, ref_id))
        except sqlite3.IntegrityError:
            raise ValueError("the card already has a reference with that provider, kind and external id") from None
        _event(db, card_id, author, "reference",
               f"changed {_describe_reference(merged)} ({', '.join(changed)})", context_id)
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (now, card_id))
        return dict(db.execute("SELECT * FROM card_references WHERE id = ?", (ref_id,)).fetchone())


def remove_reference(author: str, card_id: int, ref_id: int, context_id: str | None = None) -> dict:
    """Remove one reference; the history keeps what it was. Returns the removed reference."""
    with opstore.connect() as db:
        row = db.execute("SELECT * FROM card_references WHERE id = ? AND card_id = ?", (ref_id, card_id)).fetchone()
        if row is None:
            raise store.NotFound(f"reference {ref_id} not found on card {card_id}")
        removed = dict(row)
        db.execute("DELETE FROM card_references WHERE id = ?", (ref_id,))
        _event(db, card_id, author, "reference", f"removed {_describe_reference(removed)}", context_id)
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (time.time(), card_id))
    return removed


def _describe_reference(ref: dict) -> str:
    return f"{ref['provider']} {ref['kind']} {ref['external_id']}: {ref['label']}"[:300]


def _check_reference_workflow(db, card_id: int, workflow_id) -> None:
    """A reference may name a workflow, but only one that belongs to the same card: a
    pointer across cards would show one card's decision on another."""
    if workflow_id is None:
        return
    if not isinstance(workflow_id, int) or isinstance(workflow_id, bool):
        raise ValueError("workflow_id must be an integer")
    row = db.execute("SELECT card_id FROM workflow_requests WHERE id = ?", (workflow_id,)).fetchone()
    if row is None:
        raise ValueError(f"workflow {workflow_id} does not exist")
    if row["card_id"] != card_id:
        raise ValueError(f"workflow {workflow_id} belongs to another card")


def _reference_fields(provider, kind, external_id, url, label, last_seen_state, last_seen_at, now: float) -> dict:
    """The checked, normalised form of a reference's own fields (not its workflow, which
    needs the database). Provider and kind are free text, lowercased: an unknown one is fine."""
    state = None
    if last_seen_state is not None:
        if not isinstance(last_seen_state, str):
            raise ValueError("last_seen_state must be text")
        state = _text(last_seen_state)
        if state is not None and len(state) > 100:
            raise ValueError("last_seen_state must be at most 100 characters")
    if last_seen_at is not None and (isinstance(last_seen_at, bool)
                                     or not isinstance(last_seen_at, (int, float)) or last_seen_at < 0):
        raise ValueError("last_seen_at must be a Unix time")
    if state is not None and last_seen_at is None:
        last_seen_at = now
    external_id, label = _required(external_id, "external_id"), _required(label, "label")
    if len(external_id) > 200 or len(label) > 200:
        raise ValueError("external_id and label must be at most 200 characters")
    return {"provider": _reference_name(provider, "provider"), "kind": _reference_name(kind, "kind"),
            "external_id": external_id, "url": _reference_url(url), "label": label,
            "last_seen_state": state, "last_seen_at": None if last_seen_at is None else float(last_seen_at)}


def _reference_name(value, what: str) -> str:
    name = value.strip().lower() if isinstance(value, str) else ""
    if not _REFERENCE_NAME.match(name):
        raise ValueError(f"{what} must be 1 to 40 characters: letters, digits, '.', '_' or '-'")
    return name


def _reference_url(url) -> str:
    """http(s) only, and no credentials in it: the page renders it as a link, a
    `javascript:` or `data:` URL would run in a viewer's session, and a `user:password@`
    URL would keep a secret on the card."""
    if not isinstance(url, str):
        raise ValueError("url must be text")
    url = url.strip()
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.netloc or len(url) > 2000
            or parts.username or parts.password):
        raise ValueError(f"url {url[:80]!r} must be an http(s) URL without credentials")
    return url


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
            f"Title: {card['title']}\nProject: {card['project'] or '-'}\nState: {card['state']}\n"
            + "".join(f"{label}: {card[column][:NOTICE_BODY]}\n" for column, label in
                      (("next_action", "Next action"), ("done_when", "Done when")) if card[column])
            + f"\n{card['body'][:NOTICE_BODY]}")
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


def _blocked_fields(card: dict, new_state: str, blocked_reason: str | None,
                    waiting_on: str | None) -> tuple[str | None, str | None]:
    """The reason and the wait the card has afterwards. Only a blocked card has them. A card
    moving into blocked must say why; one that was blocked before reasons existed keeps working
    until someone touches its reason. Waiting on a person is refused: a person's answer is a
    workflow request, which has an owner, an expiry and an answer that moves the card, so the
    wait is `workflow:<id>` on an open request of this card."""
    if new_state != "blocked":
        if blocked_reason or waiting_on:
            raise ValueError("blocked_reason and waiting_on belong to a blocked card")
        return None, None
    if card["state"] == "blocked" and card["blocked_reason"] is None and blocked_reason is None and waiting_on is None:
        return None, card["waiting_on"]  # blocked before reasons existed: left as it is
    reason = blocked_reason if blocked_reason is not None else card["blocked_reason"]
    if reason not in BLOCKED_REASONS:
        raise ValueError("a blocked card says why: blocked_reason is unclear (you do not know how to go on), "
                         "waiting (the next step depends on something named) or cannot (you lack a capability or access)")
    if waiting_on is not None:
        on = _text(waiting_on)
    else:
        on = card["waiting_on"] if reason == card["blocked_reason"] else None  # a new reason needs its own wait
    if reason == "unclear":
        return reason, None
    if not on:
        raise ValueError("waiting_on says what the card waits on (waiting) or what is missing (cannot)" if reason == "cannot"
                         else "waiting_on says what the card waits on: an agent, workflow:<id>, or something outside Beacon")
    if len(on) > 200:
        raise ValueError("waiting_on must be at most 200 characters")
    if reason == "waiting":
        _check_wait(card["id"], on)
    return reason, on


def _check_wait(card_id: int, on: str) -> None:
    if on.startswith("workflow:"):
        try:
            request_id = int(on[len("workflow:"):])
        except ValueError:
            raise ValueError("a workflow wait is workflow:<id>") from None
        with opstore.connect() as db:
            row = db.execute("SELECT card_id, state FROM workflow_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None or row["card_id"] != card_id:
            raise ValueError(f"workflow {request_id} is not a request on this card")
        if row["state"] not in ("pending", "changes_requested", "approved"):
            raise ValueError(f"workflow {request_id} is {row['state']}, so there is nothing left to wait for")
    elif "@" in on or on.casefold() == "human":
        raise ValueError("waiting on a person goes through a workflow request: ask with workflow_create, "
                         "then set waiting_on to workflow:<id>")


def _blocked_summary(reason: str | None, on: str | None) -> str | None:
    if reason is None:
        return None  # the state event says the card is no longer blocked
    return f"blocked: {reason}" + (f", {on}" if on else "")


def _check_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"state {state!r} must be one of {', '.join(STATES)}")


MAX_LINKS = 20


def _card(row) -> dict:
    card = dict(row)
    card["links"] = json.loads(card["links"] or "[]")
    return card


def _text(value: str | None) -> str | None:
    return (value or "").strip() or None


def _links(links: list) -> list[dict]:
    """[{url, label}], http(s) only: the UI renders each as a link, so a
    `javascript:` or `data:` URL here would run in a viewer's session."""
    if not isinstance(links, list) or len(links) > MAX_LINKS:
        raise ValueError(f"links must be a list of at most {MAX_LINKS}")
    out = []
    for link in links:
        if not isinstance(link, dict) or not isinstance(link.get("url"), str) \
                or not isinstance(link.get("label") or "", str):
            raise ValueError("each link is {url, label}")
        url, label = link["url"].strip(), (link.get("label") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or len(url) > 2000 or len(label) > 200:
            raise ValueError(f"link {url[:80]!r} must be an http(s) URL (labels up to 200 characters)")
        out.append({"url": url, "label": label})
    return out


def _required(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must not be empty")
    return value.strip()


# --- reminders: cards nobody moves -------------------------------------------

REMINDERS = 2  # reminders to an agent before people are told


def _responsible(card: dict) -> str | None:
    """Who has to move the card next: the assignee while it is being worked on;
    in review, or with nobody assigned, whoever made it (to check it, or to give
    it to someone). None for a card its own assignee made and put in review:
    only a person can check that."""
    if card["state"] != "review" and card["assignee"]:
        return card["assignee"]
    if card["state"] == "review" and card["created_by"] == card["assignee"]:
        return None
    return card["created_by"]


def remind(puller: str, now: float | None = None) -> None:
    """Called on every mesh pull: the puller is online right now, so a reminder
    to it starts a turn soon (it rides this very pull's inbox). Each stale card
    gets at most REMINDERS reminders, one per `remind_after` of no change, and
    all of one agent's cards go in one notice, so a reminder costs one turn.
    After that, or when no agent can move the card (a person has to, or the
    agent has no board tools), people are told by email, once until the card
    changes. Any change to the card (state, comment, edit) starts over.

    A blocked card is handled by its reason (see BLOCKED_REASONS). `unclear`: its agent is
    reminded to consult a peer and try, then people hear. `waiting`: nobody is reminded to
    check again. An agent it waits on is reminded; a workflow it waits on is left to the
    request's own notices and expiry, and once the request has ended the assignee is
    reminded to carry on; anything else is people's to chase. `cannot`: no agent can move
    it, so people are told at once. Where people are the only ones left, they are told
    again each `remind_after` while the card stays as it is, not once.

    Cards waiting on an agent that never pulls wait too: its quiet notice
    already tells people it is gone."""
    from . import notify, policy
    now = time.time() if now is None else now
    rules, fleet, exposure = policy.load(), {a.id: a for a in store.list_agents()}, mesh.open_chats()
    with opstore.connect() as db:
        cards = [dict(row) for row in db.execute("SELECT * FROM cards WHERE state != 'done' ORDER BY id")]
        open_requests = {row["id"] for row in db.execute(
            "SELECT id FROM workflow_requests WHERE state IN ('pending', 'changes_requested', 'approved')")}
    mine, stuck = [], []
    for card in cards:
        who = _responsible(card)
        reason = card["blocked_reason"] if card["state"] == "blocked" else None
        note = None  # which reminder the agent gets, when it is not the card's ordinary one
        if reason == "waiting":
            target = card["waiting_on"] or ""
            if target.startswith("workflow:"):
                if target[len("workflow:"):].isdigit() and int(target[len("workflow:"):]) in open_requests:
                    continue  # the request tells its responder and expires: nothing to remind
                who, note = card["assignee"], "ended"
            elif fleet.get(target) is not None and fleet[target].reach is not None:
                who, note = target, "waits-on-you"
            else:
                who = None  # a person, or something outside Beacon: people chase it
        elif reason == "cannot":
            who = None  # no agent can move it: a person has to, and hears now
        agent = fleet.get(who or "")
        agent = agent if agent and agent.reach else None
        subject = agent or fleet.get(card["assignee"] or "")  # whose policy sets the wait
        wait = (policy.health_for(rules, subject) if subject else policy.HEALTH_DEFAULTS)["remind_after"]
        if wait is None or now - card["updated_at"] < (0 if reason == "cannot" else wait):
            continue
        again = reason in ("waiting", "cannot") and agent is None  # people only: repeated, not once
        this_round = card["reminded_at"] is not None and card["reminded_at"] >= card["updated_at"]
        sent = card["reminders"] if this_round else 0
        if this_round and now - card["reminded_at"] < wait:
            continue
        if agent and sent < REMINDERS and mesh.board_allowed(agent, exposure):
            if agent.id == puller and _claim(card, "reminded_at = ?, reminders = ?", (now, sent + 1), "reminded_at"):
                mine.append({**card, "reminders": sent + 1, "_note": note})
        elif (card["escalated_at"] is None or card["escalated_at"] < card["updated_at"]
              or (again and now - card["escalated_at"] >= wait)) \
                and _claim(card, "escalated_at = ?", (now,), "escalated_at"):
            stuck.append((card, who, _why_stuck(card, who, agent, sent, now)))
    if mine:
        notice_id = mailbox.enqueue(mesh.BEACON_PEER, puller, "notice", _reminder_text(mine, now))
        with opstore.connect() as db:
            for card in mine:
                _event(db, card["id"], mesh.BEACON_PEER, "reminder",
                       f"reminded {puller} ({card['reminders']} of {REMINDERS})", notice_id=notice_id)
    if stuck:
        with opstore.connect() as db:
            for card, _, why in stuck:
                _event(db, card["id"], mesh.BEACON_PEER, "escalation", f"people told by email: {why}")
        notify.cards_stuck(stuck)


def _claim(card: dict, assignments: str, values: tuple, column: str) -> bool:
    """Only the pull that changes the row acts on it, and not if the card
    changed since it was read."""
    with opstore.connect() as db:
        return db.execute(f"UPDATE cards SET {assignments} WHERE id = ? AND updated_at = ? AND {column} IS ?",
                          (*values, card["id"], card["updated_at"], card[column])).rowcount == 1


def _why_stuck(card: dict, who: str | None, agent, sent: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    reason = card["blocked_reason"] if card["state"] == "blocked" else None
    if reason == "cannot":
        return f"the agent cannot go on and says it lacks: {card['waiting_on']}"
    if reason == "waiting" and agent is None:
        return f"it has waited {_idle(now - card['updated_at'])} on {card['waiting_on']}"
    if agent and sent:
        return f"{who} was reminded and nothing changed"
    if agent:
        return f"{who} has no board tools, so Beacon cannot remind it"
    if card["state"] == "review":
        return "it waits for a person to check it"
    if not card["assignee"]:
        return "nobody is assigned"
    return f"it waits for {card['assignee']}"


def _idle(seconds: float) -> str:
    return f"{int(seconds // 86400)} d" if seconds >= 86400 else f"{int(seconds // 3600)} h"


def _reminder_text(cards: list[dict], now: float) -> str:
    lines = []
    for card in cards:
        title, idle = card["title"][:120], _idle(now - card["updated_at"])
        if card["state"] == "review":
            what = (f"{card['assignee'] or 'it'} finished it and it waits for your check: move it to done,"
                    " or back to doing with a note that says what is missing")
        elif not card["assignee"]:
            what = "nobody is assigned: assign it to someone, do it yourself, or close it"
        elif card["state"] == "todo":
            what = "assigned to you and not started"
        elif card.get("_note") == "waits-on-you":
            what = (f"blocked, and it waits on you (assigned to {card['assignee'] or 'nobody'}): read the card"
                    " with board_card, then do what it needs, or say on the card what stops you")
        elif card.get("_note") == "ended":
            what = (f"blocked, waiting on {card['waiting_on']}, which has ended: read it with workflow_get,"
                    " then carry on, or say on the card what is still missing")
        elif card["state"] == "blocked" and card["blocked_reason"] == "unclear":
            what = ("blocked because you did not know how to go on. Do not wait: ask a peer you may call to"
                    " challenge your approach (a2a_call, then card_comment with what they said and the thread's"
                    " context_id), try what they suggest, and ask a person only for a decision or an authority"
                    " only a person has, never for work you can do yourself")
        elif card["state"] == "blocked":
            what = ("blocked: if what you waited for has come, carry on; if not, make sure whoever you wait"
                    " for knows, and say on the card who it is")
        else:
            what = "in progress, with no update"
        lines.append(f"- #{card['id']} \"{title}\" ({card['state']}, no change for {idle}): {what}."
                     + (f" Next action on the card: {card['next_action'][:200]}" if card["next_action"] else ""))
    return ("Reminder from the Beacon board: nothing has changed on these cards for a while, and they wait"
            " for you.\n\n" + "\n".join(lines) + "\n\n"
            "For each card, do the next step now, or make the card say what is true: card_update to doing,"
            " blocked (with blocked_reason unclear, waiting or cannot, and waiting_on for the last two),"
            " review or done with a note, or card_comment with what you wait for and from whom. If"
            " you cannot do it, say so on the card. Do not close a card you did not finish. After"
            f" {REMINDERS} reminders without a change, Beacon tells a person.")
