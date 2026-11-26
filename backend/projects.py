"""Projects: long-term work with an owner and a stated goal.

A card says what to do next; nothing said why, and nobody noticed when every card was closed
and the goal was not reached. A project has a goal, a done-when, an owner (a person or an
agent) who is responsible for delivery, and a status note the owner keeps current. Cards point
at it by name (`cards.project`, free text before this), so older clients keep working.

Beacon only coordinates: the evidence lives in tickets and repositories, and a project marked
done is the owner's claim, not something Beacon checked.

Who may do what, and why:
- Nobody approves a project. Agents are meant to start and run them without a person.
- Behind every owner there is a person: the owner itself, an agent's human `owner`, else the
  operators. That person is told when a project is paused, finished or abandoned, not asked.
- The owner side (owner, the person behind it, operators) steers: name, state, status, owner.
- The goal and done-when change at the hands of whoever set them, so the one who has to clear
  the bar cannot lower it. A goal a person set changes only by that person or an operator;
  an agent proposes (a workflow question to them) and they edit it.
- Pause is the brake for agents that make work in circles: what a person paused, only a person
  restarts. "beacon" (the MCP bearer) is not a person: agents hold it.
- Everyone who has no part reads: agents see all projects, as they see the board. A member
  sees only the ones they or their agents take part in (visible_to).
"""

import difflib
import json
import threading
import time

from . import mailbox, mesh, opstore, store

STATES = ("active", "paused", "done", "abandoned")
CLOSED = ("done", "abandoned")
MAX_NAME = 80
MAX_TEXT = 4000
DEFAULT = object()  # create(): "the owner was not given", as opposed to None, "the operators"


class NotAllowed(Exception):
    """This caller may not do that to this project."""


# --- who is who ---------------------------------------------------------------


def _same(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.casefold() == b.casefold()


def _is_person(name: str | None) -> bool:
    return bool(name) and "@" in name and not name.startswith("intake:")


def _is_operator_person(name: str) -> bool:
    from . import workflow
    return _is_person(name) and workflow._is_operator(name)


def _is_member(name: str) -> bool:
    return _is_person(name) and not _is_operator_person(name)


def _agent(name: str | None):
    if not name or _is_person(name):
        return None
    try:
        return store.get_agent(name)
    except (store.NotFound, store.InvalidId):
        return None


def person_behind(name: str | None) -> str | None:
    """The person answerable for an owner or a goal: None means the operators."""
    if _is_person(name):
        return name.casefold()
    agent = _agent(name)
    if agent is None:
        return None
    person = agent.client.acts_for if agent.client is not None else agent.owner
    return person.casefold() if person else None


def owner_side(actor: str, project: dict) -> bool:
    """Who steers: the owner, the person behind it, operators, and the client that started it
    (a client never owns a project, so without this it could not run what it started)."""
    if actor == mesh.BEACON_PEER or _is_operator_person(actor) or _same(actor, project["owner"]):
        return True
    if _is_person(actor):
        return _same(actor, person_behind(project["owner"]))
    agent = _agent(actor)
    return agent is not None and agent.client is not None and _same(actor, project["created_by"])


def may_set_goal(actor: str, project: dict) -> bool:
    if _is_operator_person(actor):
        return True
    if not project["goal"]:
        return owner_side(actor, project)
    setter = project["goal_by"]
    if _same(actor, setter):
        return True
    if _is_person(setter):
        return False
    return owner_side(actor, project) or (_is_person(actor) and _same(actor, person_behind(setter)))


def _check_owner(actor: str, owner: str | None) -> str | None:
    """A project hands its owner responsibility, so who may be named is limited the way
    handing out work is."""
    from . import members, workflow
    if owner is None:
        if actor != mesh.BEACON_PEER and not _is_operator_person(actor):
            raise NotAllowed("only an operator may leave a project to the operators; name an owner")
        return None
    owner = owner.strip()
    if _is_person(owner):
        owner = owner.casefold()
        if not (workflow._is_operator(owner) or members.owns_any(owner)):
            raise ValueError(f"{owner} cannot log in to Beacon, so they cannot own a project")
        if _is_member(actor) and not _same(actor, owner):
            raise NotAllowed("a member can make only themselves or their own agents an owner")
        agent = _agent(actor)
        if agent is not None and not _same(owner, person_behind(actor)):
            raise NotAllowed("an agent can name only the person it works for as owner")
        return owner
    agent = _agent(owner)
    if agent is None:
        raise ValueError(f"unknown owner {owner!r}: an agent id or an email")
    if agent.client is not None:
        raise ValueError(f"{owner} is an operator's client: it does not own projects")
    if _is_member(actor):
        if not members.owns(actor, agent):
            raise NotAllowed("a member can make only themselves or their own agents an owner")
    elif _agent(actor) is not None and not _same(actor, owner) and not mesh.may_call(actor, agent):
        raise NotAllowed(f"{owner} does not accept work from {actor}")
    return owner


# --- reading ------------------------------------------------------------------


def _decode(row) -> dict:
    return {**dict(row), "links": json.loads(row["links"] or "[]")}


def _rows(db) -> list[dict]:
    return [_decode(r) for r in db.execute("SELECT * FROM projects ORDER BY updated_at DESC")]


def visible_to(email: str) -> set[str]:
    """The exact names (as cards store them) of the projects a member takes part in: those whose owner side they
    are on, and those where a card is theirs or their agent's."""
    from . import members
    mine = members.owned_ids(email)
    with opstore.connect() as db:
        projects = _rows(db)
        cards = db.execute("SELECT project, assignee, created_by FROM cards WHERE project IS NOT NULL").fetchall()
    taking_part = {c["project"] for c in cards if not c["created_by"].startswith("intake:")
                   and (_same(c["created_by"], email) or c["assignee"] in mine)}
    return taking_part | {p["name"] for p in projects if owner_side(email, p)}


def _may_see(actor: str, name: str, seen: set[str] | None) -> bool:
    return seen is None or name in seen


def _seen_by(actor: str) -> set[str] | None:
    return visible_to(actor) if _is_member(actor) else None


def _view(actor: str, project: dict) -> dict:
    """The project with what this caller may do to it, so a page offers only that."""
    return {**project, "can": {"steer": owner_side(actor, project), "goal": may_set_goal(actor, project)}}


def list_projects(actor: str, include_closed: bool = False) -> list[dict]:
    seen = _seen_by(actor)
    with opstore.connect() as db:
        rows = _rows(db)
        counts = {}
        for r in db.execute("SELECT project, state, count(*) AS n FROM cards WHERE project IS NOT NULL"
                            " GROUP BY project, state"):
            counts.setdefault(r["project"].casefold(), {})[r["state"]] = r["n"]
    return [_view(actor, p) | {"cards": counts.get(p["name"].casefold(), {})} for p in rows
            if _may_see(actor, p["name"], seen) and (include_closed or p["state"] not in CLOSED)]


def _row(db, ref) -> dict:
    """By number or by name."""
    column = "id" if isinstance(ref, int) or str(ref).isdigit() else "name"
    row = db.execute(f"SELECT * FROM projects WHERE {column} = ?", (ref,)).fetchone()
    if row is None:
        raise store.NotFound(f"project {ref} not found")
    return _decode(row)


def find(ref) -> dict:
    with opstore.connect() as db:
        return _row(db, ref)


def get(actor: str, ref) -> dict:
    """A project with its history and its cards."""
    from . import board
    project = find(ref)
    if not _may_see(actor, project["name"], _seen_by(actor)):
        raise store.NotFound(f"project {ref} not found")
    with opstore.connect() as db:
        events = [dict(r) for r in db.execute("SELECT * FROM project_events WHERE project_id = ? ORDER BY id",
                                              (project["id"],))]
    cards = board.list_cards(project=project["name"], include_done=True)
    return _view(actor, project) | {"events": events, "card_list": cards}


def for_card(name: str | None) -> dict | None:
    if not name:
        return None
    try:
        return find(name)
    except store.NotFound:
        return None


def resolve(actor: str, name: str) -> str:
    """The stored name of the project a card is being put in. An unknown name is refused with
    the similar ones: free text made two projects out of one typo."""
    seen = _seen_by(actor)
    with opstore.connect() as db:
        rows = [p for p in _rows(db) if _may_see(actor, p["name"], seen)]
    for project in rows:
        if _same(project["name"], name.strip()):
            if project["state"] in CLOSED:
                raise ValueError(f"project {project['name']!r} is {project['state']}: reopen it before adding cards")
            return project["name"]
    similar = _similar(name, rows)
    raise ValueError(f"there is no project {name.strip()!r}."
                     + (f" Similar: {', '.join(p['name'] for p in similar)}." if similar else "")
                     + " Create it first (project_create), with its goal and done-when.")


def _similar(name: str, projects: list[dict]) -> list[dict]:
    by_name = {p["name"].casefold(): p for p in projects if p["state"] not in CLOSED}
    wanted = name.strip().casefold()
    close = difflib.get_close_matches(wanted, by_name, n=3, cutoff=0.6)
    close += [n for n in by_name if n not in close and (n in wanted or wanted in n) and min(len(n), len(wanted)) >= 4]
    return [by_name[n] for n in close[:3]]


# --- changing -----------------------------------------------------------------


def _text(value, what: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must not be empty")
    if len(value.strip()) > limit:
        raise ValueError(f"{what} must be at most {limit} characters")
    return value.strip()


def _event(db, project_id: int, author: str, kind: str, body: str) -> None:
    db.execute("INSERT INTO project_events (project_id, author, kind, body, created_at) VALUES (?, ?, ?, ?, ?)",
               (project_id, author, kind, body, time.time()))


def create(actor: str, name: str, goal: str, done_when: str, owner=DEFAULT, links: list | None = None,
           confirm_new: bool = False) -> dict:
    from . import board
    name = _text(name, "name", MAX_NAME)
    if name.isdigit():
        raise ValueError("name must not be only digits")
    goal, done_when = _text(goal, "goal"), _text(done_when, "done_when")
    links = board._links(links or [])
    if owner is DEFAULT:
        client = _agent(actor)
        if actor == mesh.BEACON_PEER:
            owner = None
        elif client is not None and client.client is not None:
            owner = client.client.acts_for
        else:
            owner = actor
    owner = _check_owner(actor, owner)
    seen = _seen_by(actor)
    with opstore.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = _rows(db)
        if any(_same(p["name"], name) for p in rows):
            raise ValueError(f"the name {name!r} is taken")
        similar = _similar(name, [p for p in rows if _may_see(actor, p["name"], seen)])
        if similar and not confirm_new:
            raise ValueError("there are similar open projects: "
                             + "; ".join(f"{p['name']} (goal: {p['goal'][:120] or 'none set'})" for p in similar)
                             + ". Add your cards to one of them, or pass confirm_new=true if yours is different.")
        now = time.time()
        project_id = db.execute(
            "INSERT INTO projects (name, goal, done_when, owner, goal_by, state, links, created_by, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
            (name, goal, done_when, owner, actor, json.dumps(links), actor, now, now)).lastrowid
        _event(db, project_id, actor, "created", f"goal: {goal}\ndone when: {done_when}\nowner: {owner or 'operators'}")
    project = find(project_id)
    if owner and not _same(owner, actor):
        _tell_agent(project, owner, actor, "You are now the owner of this project.")
    return get(actor, project_id)


def update(actor: str, ref, *, name: str | None = None, goal: str | None = None, done_when: str | None = None,
           owner=DEFAULT, state: str | None = None, status: str | None = None, links: list | None = None,
           note: str | None = None) -> dict:
    """Change what is given. Every check happens before the first write."""
    from . import board
    project = find(ref)
    if not _may_see(actor, project["name"], _seen_by(actor)):
        raise store.NotFound(f"project {ref} not found")
    changes: list[tuple[str, object, str, str]] = []  # column, value, event kind, event text
    for column, value in (("goal", goal), ("done_when", done_when)):
        if value is None or value.strip() == (project[column] or ""):
            continue
        if not may_set_goal(actor, project):
            setter = project["goal_by"] if _is_person(project["goal_by"]) else None
            raise NotAllowed(
                f"{setter or 'someone else'} set the goal of {project['name']!r}, so you cannot change it. Propose the "
                f"change: workflow_create (kind question) to {setter or 'the project owner'} on a card of this project, "
                "saying what you would change and why. They edit the goal if they agree.")
        value = _text(value, column)
        changes.append((column, value, "goal", f"{column.replace('_', ' ')}: {value}"))
    goal_changed = bool(changes)
    steered = []
    if name is not None and name.strip() != project["name"]:
        steered.append(("name", _text(name, "name", MAX_NAME), "edit", f"renamed from {project['name']}"))
    if owner is not DEFAULT and not _same(owner or None, project["owner"]) and (owner or None) != project["owner"]:
        owner = _check_owner(actor, owner or None)
        steered.append(("owner", owner, "owner", f"owner: {project['owner'] or 'operators'} -> {owner or 'operators'}"))
    if status is not None and status.strip() != (project["status"] or ""):
        status = status.strip()[:MAX_TEXT] or None
        steered.append(("status", status, "status", f"status: {status or '-'}"))
    if links is not None and (links := board._links(links)) != project["links"]:
        steered.append(("links", json.dumps(links), "edit", "links changed"))
    new_state = state if state is not None and state != project["state"] else None
    if new_state:
        if new_state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        if project["state"] == "paused" and _is_person(project["paused_by"]) and not _is_person(actor):
            raise NotAllowed(f"{project['paused_by']} paused this project: only a person can restart or close it")
        steered.append(("state", new_state, "state", f"{project['state']} -> {new_state}"))
        steered.append(("paused_by", actor if new_state == "paused" else None, "", ""))
    if steered and not owner_side(actor, project):
        raise NotAllowed(f"only the owner of {project['name']!r}, the person behind it or an operator can do that")
    changes += steered
    if not changes and not note:
        return get(actor, project["id"])
    now = time.time()
    with opstore.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if new_state == "done" and db.execute(
                "SELECT 1 FROM cards WHERE project = ? COLLATE NOCASE AND state != 'done'", (project["name"],)).fetchone():
            raise ValueError("it still has cards that are not done: finish them, or move them out of the project")
        renamed = next((v for c, v, *_ in changes if c == "name"), None)
        if renamed and any(_same(p["name"], renamed) for p in _rows(db) if p["id"] != project["id"]):
            raise ValueError(f"the name {renamed!r} is taken")
        for column, value, kind, text in changes:
            db.execute(f"UPDATE projects SET {column} = ?, updated_at = ? WHERE id = ?", (value, now, project["id"]))
            if column == "status":
                db.execute("UPDATE projects SET status_at = ? WHERE id = ?", (now, project["id"]))
            if kind:
                _event(db, project["id"], actor, kind, text + (f" ({note})" if note else ""))
        if goal_changed:
            db.execute("UPDATE projects SET goal_by = ? WHERE id = ?", (actor, project["id"]))
        if renamed:
            db.execute("UPDATE cards SET project = ? WHERE project = ? COLLATE NOCASE", (renamed, project["name"]))
        if note and not changes:
            _event(db, project["id"], actor, "comment", note)
    changed = find(project["id"])
    if new_state:
        _announce(changed, actor, note)
    elif any(c == "owner" for c, *_ in changes) and changed["owner"]:
        _tell_agent(changed, changed["owner"], actor, "You are now the owner of this project.")
    elif goal_changed and changed["owner"]:
        _tell_agent(changed, changed["owner"], actor, "Its goal or done-when changed.")
    return get(actor, project["id"])


def comment(actor: str, ref, text: str) -> dict:
    project = find(ref)
    if not _may_see(actor, project["name"], _seen_by(actor)):
        raise store.NotFound(f"project {ref} not found")
    text = _text(text, "comment")
    with opstore.connect() as db:
        _event(db, project["id"], actor, "comment", text)
        db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (time.time(), project["id"]))
    if project["owner"]:
        _tell_agent(project, project["owner"], actor, f"{actor} commented:\n{text[:600]}")
    return get(actor, project["id"])


# --- telling ------------------------------------------------------------------

_WHAT = {
    "paused": "is paused. Stop work on it: do not start or continue its cards until it is active again.",
    "active": "is active again.",
    "done": "is marked done by its owner's side.",
    "abandoned": "is abandoned. Stop work on it.",
}


def _announce(project: dict, actor: str, note: str | None) -> None:
    """A state change reaches the agents doing the work through the mailbox, and the person
    behind the owner by push and email: informed, not asked."""
    from . import notify
    with opstore.connect() as db:
        agents = {r["assignee"] for r in db.execute(
            "SELECT DISTINCT assignee FROM cards WHERE project = ? COLLATE NOCASE AND state != 'done'"
            " AND assignee IS NOT NULL", (project["name"],))}
    why = f"\n{note[:600]}" if note else ""
    for agent_id in agents | ({project["owner"]} if project["owner"] else set()):
        _tell_agent(project, agent_id, actor, f"The project {_WHAT[project['state']]}{why}")
    if project["state"] != "active":
        notify.project_changed(project, actor, person_behind(project["owner"]), note)


def _tell_agent(project: dict, agent_id: str, actor: str, text: str) -> None:
    agent = _agent(agent_id)
    if agent is None or agent.reach is None or _same(agent_id, actor):
        return
    mailbox.enqueue(mesh.BEACON_PEER, agent_id, "notice",
                    f"Beacon project #{project['id']} {project['name']!r} (by {actor}).\n{text}\n"
                    f"Goal: {project['goal'][:600] or 'none set'}")
    threading.Thread(target=mailbox.deliver_pending, args=(agent_id,), daemon=True).start()
