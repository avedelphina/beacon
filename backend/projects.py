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
- Milestones follow the same rule as the goal: each changes or goes at the hands of whoever
  set it. Closing one, or the project, takes a link to the ticket, pull request or commit, or
  the reason there is none: a claim others can follow up, never checked here.
- A project with nothing unfinished and no goal reached is stalled: the scheduler reminds the
  owner, then tells the person behind it, then pauses (check_stalled).
- Everyone who has no part reads: agents see all projects, as they see the board. A member
  sees only the ones they or their agents take part in (visible_to).
"""

import datetime
import difflib
import json
import threading
import time

from . import mailbox, mesh, opstore, store, workhours

STATES = ("active", "paused", "done", "abandoned")
CLOSED = ("done", "abandoned")
MAX_NAME = 80
MAX_TEXT = 4000
MAX_MILESTONES = 20
# Working days with nothing unfinished: remind the owner, tell the person behind it, pause.
STALL_DAYS = (1, 3, 8)
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
    if not project["goal"]:
        return owner_side(actor, project)
    return _may_move_bar(actor, project, project["goal_by"])


def _may_move_bar(actor: str, project: dict, setter: str) -> bool:
    """Whether `actor` may change what `setter` required: a goal, or a milestone."""
    if _is_operator_person(actor) or _same(actor, setter):
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
    return {**dict(row), "links": json.loads(row["links"] or "[]"),
            "evidence": json.loads(row["evidence"]) if row["evidence"] else None}


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
        milestones = [dict(r) | {"evidence": json.loads(r["evidence"]) if r["evidence"] else None,
                                 "can_change": _may_move_bar(actor, project, r["set_by"])} for r in db.execute(
            "SELECT * FROM project_milestones WHERE project_id = ? ORDER BY id", (project["id"],))]
    cards = board.list_cards(project=project["name"], include_done=True)
    return _view(actor, project) | {"events": events, "milestones": milestones, "card_list": cards}


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
            "INSERT INTO projects (name, goal, done_when, owner, goal_by, state, links, active_at, created_by,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (name, goal, done_when, owner, actor, json.dumps(links), now, actor, now, now)).lastrowid
        _event(db, project_id, actor, "created", f"goal: {goal}\ndone when: {done_when}\nowner: {owner or 'operators'}")
    project = find(project_id)
    if owner and not _same(owner, actor):
        _tell_agent(project, owner, actor, "You are now the owner of this project.")
    return get(actor, project_id)


def update(actor: str, ref, *, name: str | None = None, goal: str | None = None, done_when: str | None = None,
           owner=DEFAULT, state: str | None = None, status: str | None = None, links: list | None = None,
           note: str | None = None, evidence_links: list | None = None,
           no_evidence_because: str | None = None) -> dict:
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
        if project["state"] == "done":
            steered.append(("evidence", None, "", ""))
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
        if new_state == "done" and db.execute(
                "SELECT 1 FROM project_milestones WHERE project_id = ? AND state = 'open'", (project["id"],)).fetchone():
            raise ValueError("it still has open milestones: close them with their evidence first")
        if new_state == "done":  # asked for last: first say what else stands in the way
            evidence = _evidence(evidence_links, no_evidence_because)
            changes.append(("evidence", evidence, "state", f"evidence: {_describe_evidence(evidence)}"))
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
        # The stall clock starts over when work could not have been expected before: resumed, reopened,
        # or only now given a goal or an owner (a migrated project has neither).
        if new_state == "active" or goal_changed or any(c == "owner" for c, *_ in changes):
            db.execute("UPDATE projects SET active_at = ? WHERE id = ?", (now, project["id"]))
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


# --- milestones ---------------------------------------------------------------


def _evidence(links: list | None, none_because: str | None) -> str:
    from . import board
    links = board._links(links or [])
    reason = (none_because or "").strip()[:MAX_TEXT]
    if not links and not reason:
        raise ValueError("closing needs evidence: evidence_links to the ticket, pull request or commit, "
                         "or no_evidence_because")
    return json.dumps({"links": links, "none_because": reason or None})


def _describe_evidence(evidence: str) -> str:
    value = json.loads(evidence)
    return ", ".join(link["url"] for link in value["links"]) or f"none, because {value['none_because']}"


def _due(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        return datetime.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise ValueError("due must be a date, YYYY-MM-DD") from None


def _open_project(actor: str, ref) -> dict:
    project = find(ref)
    if not _may_see(actor, project["name"], _seen_by(actor)):
        raise store.NotFound(f"project {ref} not found")
    if project["state"] in CLOSED:
        raise ValueError(f"the project is {project['state']}: reopen it first")
    return project


def add_milestone(actor: str, ref, title: str, done_when: str, due: str | None = None) -> dict:
    project = _open_project(actor, ref)
    if not owner_side(actor, project):
        raise NotAllowed(f"only the owner side of {project['name']!r} plans its milestones")
    title, done_when, due = _text(title, "title", 200), _text(done_when, "done_when"), _due(due)
    now = time.time()
    with opstore.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT count(*) AS n FROM project_milestones WHERE project_id = ?",
                      (project["id"],)).fetchone()["n"] >= MAX_MILESTONES:
            raise ValueError(f"a project has at most {MAX_MILESTONES} milestones")
        milestone_id = db.execute(
            "INSERT INTO project_milestones (project_id, title, done_when, due, set_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)", (project["id"], title, done_when, due, actor, now)).lastrowid
        _event(db, project["id"], actor, "milestone", f"added #{milestone_id} {title} (done when: {done_when})")
        db.execute("UPDATE projects SET updated_at = ?, active_at = ? WHERE id = ?", (now, now, project["id"]))
    return get(actor, project["id"])


def change_milestone(actor: str, ref, milestone_id: int, *, title: str | None = None, done_when: str | None = None,
                     due: str | None = None, state: str | None = None, remove: bool = False,
                     evidence_links: list | None = None, no_evidence_because: str | None = None) -> dict:
    """Edit, close (state="done"), reopen or remove one. Changing what it requires, and removing
    it, is for whoever set it; closing, reopening and dating it is for the owner side."""
    project = _open_project(actor, ref)
    with opstore.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM project_milestones WHERE id = ? AND project_id = ?",
                         (milestone_id, project["id"])).fetchone()
        if row is None:
            raise store.NotFound(f"milestone {milestone_id} not found in this project")
        values, said = {}, []
        for column, value, limit in (("title", title, 200), ("done_when", done_when, MAX_TEXT)):
            if value is not None and value.strip() != row[column]:
                values[column] = _text(value, column, limit)
                said.append(f"{column.replace('_', ' ')}: {values[column]}")
        if (values or remove) and not _may_move_bar(actor, project, row["set_by"]):
            raise NotAllowed(f"{row['set_by']} set this milestone, so you cannot change or remove it: ask them "
                             "(a question workflow on a card of this project)")
        if values:
            values["set_by"] = actor
        new_state = state if state is not None and state != row["state"] else None
        new_due = _due(due) if due is not None else row["due"]
        if (new_state or new_due != row["due"]) and not owner_side(actor, project):
            raise NotAllowed(f"only the owner side of {project['name']!r} closes or dates its milestones")
        if new_due != row["due"]:
            values["due"] = new_due
            said.append(f"due: {new_due or '-'}")
        if new_state == "done":
            if db.execute("SELECT 1 FROM cards WHERE milestone_id = ? AND state != 'done'", (milestone_id,)).fetchone():
                raise ValueError("it still has cards that are not done")
            evidence = _evidence(evidence_links, no_evidence_because)
            values |= {"state": "done", "evidence": evidence, "closed_by": actor, "closed_at": time.time()}
            said.append(f"done, evidence: {_describe_evidence(evidence)}")
        elif new_state == "open":
            values |= {"state": "open", "evidence": None, "closed_by": None, "closed_at": None}
            said.append("reopened")
        elif new_state:
            raise ValueError("state must be open or done")
        now = time.time()
        if remove:
            db.execute("UPDATE cards SET milestone_id = NULL WHERE milestone_id = ?", (milestone_id,))
            db.execute("DELETE FROM project_milestones WHERE id = ?", (milestone_id,))
            said = [f"removed ({row['title']})"]
        elif values:
            db.execute(f"UPDATE project_milestones SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                       (*values.values(), milestone_id))
        if said:
            _event(db, project["id"], actor, "milestone", f"#{milestone_id} " + "; ".join(said))
            db.execute("UPDATE projects SET updated_at = ?, active_at = ? WHERE id = ?", (now, now, project["id"]))
    return get(actor, project["id"])


def check_milestone(project_name: str | None, milestone_id: int) -> int:
    """For the board: the milestone a card is being pointed at must be an open one of its project."""
    project = for_card(project_name)
    if project is None:
        raise ValueError("a milestone needs the card to be in its project")
    with opstore.connect() as db:
        row = db.execute("SELECT state FROM project_milestones WHERE id = ? AND project_id = ?",
                         (milestone_id, project["id"])).fetchone()
    if row is None:
        raise ValueError(f"project {project['name']!r} has no milestone {milestone_id}")
    if row["state"] != "open":
        raise ValueError(f"milestone {milestone_id} is done: reopen it before adding cards")
    return milestone_id


# --- stalled projects -----------------------------------------------------------


def check_stalled(now: float | None = None) -> int:
    """For the scheduler. A project with a goal and an owner and no unfinished card is waiting for
    nobody: after one working day the owner is reminded to plan the next step, after three the person
    behind it is told, after eight it is paused. Anything that happens to its cards or plan starts
    over. Blocked cards count as unfinished: the board chases those itself. Returns how many steps
    were taken."""
    now = time.time() if now is None else now
    day = workhours.day_seconds()
    with opstore.connect() as db:
        rows = db.execute(
            "SELECT p.*, (SELECT max(c.updated_at) FROM cards c WHERE c.project = p.name) AS card_at FROM projects p"
            " WHERE p.state = 'active' AND p.goal != '' AND p.owner IS NOT NULL AND NOT EXISTS"
            " (SELECT 1 FROM cards c WHERE c.project = p.name AND c.state != 'done')").fetchall()
    taken = 0
    for row in rows:
        project, since = _decode(row), max(row["active_at"], row["card_at"] or 0)
        step = project["stall_step"] if project["stall_at"] and project["stall_at"] >= since else 0
        if step >= len(STALL_DAYS) or now < workhours.after(since, STALL_DAYS[step] * day):
            continue
        with opstore.connect() as db:
            if db.execute("UPDATE projects SET stall_step = ?, stall_at = ? WHERE id = ? AND state = 'active'"
                          " AND stall_step = ? AND stall_at IS ?", (step + 1, now, project["id"],
                          project["stall_step"], project["stall_at"])).rowcount != 1:
                continue  # another tick has it
            _event(db, project["id"], mesh.BEACON_PEER, "state" if step == 2 else "edit", (
                f"nothing planned: reminded {project['owner']}", "nothing planned: told the person behind it",
                "active -> paused: nothing was planned")[step])
            if step == 2:
                db.execute("UPDATE projects SET state = 'paused', paused_by = ?, updated_at = ? WHERE id = ?",
                           (mesh.BEACON_PEER, now, project["id"]))
        taken += 1
        _stalled(find(project["id"]), step)
    return taken


def _stalled(project: dict, step: int) -> None:
    from . import notify
    owner, person = project["owner"], person_behind(project["owner"])
    if step == 2:
        _announce(project, mesh.BEACON_PEER, "Nothing was planned for it. Resume it when there is a next step.")
    elif step == 0 and not _is_person(owner):
        _tell_agent(project, owner, mesh.BEACON_PEER, "Nothing is planned for it and its goal is not marked reached. "
                    "Create the next card, or mark it done with evidence, or abandon it with why.")
    elif step == 0 or not _same(owner, person):
        notify.project_stalled(project, person)


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
