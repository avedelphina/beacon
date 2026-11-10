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
"""

import json
import threading
import time
from urllib.parse import urlsplit

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
    return [_card(row) for row in rows]


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
    return {**_card(card), "events": out_events}


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
                note: str | None = None, context_id: str | None = None) -> dict:
    """Change what's given; each change is its own event. `assignee=""`
    unassigns, `next_action=""`/`done_when=""` clear, `links` replaces the list.
    `note` explains the change and is recorded with it."""
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

    Cards waiting on an agent that never pulls wait too: its quiet notice
    already tells people it is gone."""
    from . import notify, policy
    now = time.time() if now is None else now
    rules, fleet, exposure = policy.load(), {a.id: a for a in store.list_agents()}, mesh.open_chats()
    with opstore.connect() as db:
        cards = [dict(row) for row in db.execute("SELECT * FROM cards WHERE state != 'done' ORDER BY id")]
    mine, stuck = [], []
    for card in cards:
        who = _responsible(card)
        agent = fleet.get(who or "")
        agent = agent if agent and agent.reach else None
        subject = agent or fleet.get(card["assignee"] or "")  # whose policy sets the wait
        wait = (policy.health_for(rules, subject) if subject else policy.HEALTH_DEFAULTS)["remind_after"]
        if wait is None or now - card["updated_at"] < wait:
            continue
        this_round = card["reminded_at"] is not None and card["reminded_at"] >= card["updated_at"]
        sent = card["reminders"] if this_round else 0
        if this_round and now - card["reminded_at"] < wait:
            continue
        if agent and sent < REMINDERS and mesh.board_allowed(agent, exposure):
            if agent.id == puller and _claim(card, "reminded_at = ?, reminders = ?", (now, sent + 1), "reminded_at"):
                mine.append({**card, "reminders": sent + 1})
        elif (card["escalated_at"] is None or card["escalated_at"] < card["updated_at"]) \
                and _claim(card, "escalated_at = ?", (now,), "escalated_at"):
            stuck.append((card, who, _why_stuck(card, who, agent, sent)))
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


def _why_stuck(card: dict, who: str | None, agent, sent: int) -> str:
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
            " blocked, review or done with a note, or card_comment with what you wait for and from whom. If"
            " you cannot do it, say so on the card. Do not close a card you did not finish. After"
            f" {REMINDERS} reminders without a change, Beacon tells a person.")
