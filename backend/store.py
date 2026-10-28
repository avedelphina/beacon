"""The fleet: hosts, agents, config templates, cron jobs and the access policy.

They live in the operational store (state/beacon.db, backend/opstore.py), one
row per record, so a write is one transaction and two writers cannot lose
each other's changes. YAML is for moving the fleet in and out, not for
running it (backend/fleet.py: `import` reads a fleet/ directory, `export`
writes one). Every change to a record is kept in `fleet_history` with who
made it, so "what did this look like before" has an answer.

Records are stored as JSON of their pydantic model (a template as its dict,
the policy as the text exactly as written, comments and all), and validated
again on every read: a record is never trusted because it was stored.
"""

import contextvars
import json
import time
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from . import opstore
from .schemas import ID_RE, Agent, Host

# Where `python -m backend.fleet import` and the first start look for the
# YAML fleet of an install that predates the database.
FLEET_DIR = Path(__file__).resolve().parent.parent / "fleet"

# Who is making the change, for fleet_history. The API sets it per request;
# a CLI or a test leaves "beacon".
ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("fleet_actor", default="beacon")
HISTORY_KEEP = 100  # per record
# Cron jobs carry runtime fields (last_run_*) that change on every tick, so
# their changes are not history.
UNLOGGED = ("cron",)


class NotFound(Exception):
    pass


class InvalidId(Exception):
    pass


def check_id(id_: str) -> None:
    if not ID_RE.match(id_):
        raise InvalidId(f"id {id_!r} must match {ID_RE.pattern}")


# --- records: kind + id -> text ---------------------------------------------


def put(db, kind: str, id_: str, body: str, action: str = "set", actor: str | None = None) -> bool:
    """Store `body`; False (and nothing written) if it is what is already there."""
    row = db.execute("SELECT body FROM fleet_records WHERE kind = ? AND id = ?", (kind, id_)).fetchone()
    if row is not None and row["body"] == body:
        return False
    now = time.time()
    db.execute("INSERT INTO fleet_records (kind, id, body, revision, updated_at) VALUES (?, ?, ?, 1, ?) "
               "ON CONFLICT (kind, id) DO UPDATE SET body = excluded.body, revision = revision + 1, "
               "updated_at = excluded.updated_at", (kind, id_, body, now))
    log(db, kind, id_, action, body, actor)
    return True


def log(db, kind: str, id_: str, action: str, body: str | None, actor: str | None) -> None:
    if kind in UNLOGGED:
        return
    db.execute("INSERT INTO fleet_history (kind, rid, action, body, actor, at) VALUES (?, ?, ?, ?, ?, ?)",
               (kind, id_, action, body, actor or ACTOR.get(), time.time()))
    db.execute("DELETE FROM fleet_history WHERE kind = ? AND rid = ? AND id NOT IN "
               "(SELECT id FROM fleet_history WHERE kind = ? AND rid = ? ORDER BY id DESC LIMIT ?)",
               (kind, id_, kind, id_, HISTORY_KEEP))


def drop(db, kind: str, id_: str, action: str = "delete", actor: str | None = None) -> None:
    if db.execute("DELETE FROM fleet_records WHERE kind = ? AND id = ?", (kind, id_)).rowcount == 0:
        raise NotFound(id_)
    log(db, kind, id_, action, None, actor)


def find_record(kind: str, id_: str) -> str | None:
    with opstore.connect() as db:
        row = db.execute("SELECT body FROM fleet_records WHERE kind = ? AND id = ?", (kind, id_)).fetchone()
    return None if row is None else row["body"]


def records(kind: str) -> dict[str, str]:
    """id -> stored text, in id order."""
    with opstore.connect() as db:
        return {r["id"]: r["body"] for r in
                db.execute("SELECT id, body FROM fleet_records WHERE kind = ? ORDER BY id", (kind,))}


def save_record(kind: str, id_: str, body: str, actor: str | None = None) -> None:
    with opstore.connect() as db:
        put(db, kind, id_, body, actor=actor)


def delete_record(kind: str, id_: str) -> None:
    with opstore.connect() as db:
        drop(db, kind, id_)


def history(kind: str, id_: str, limit: int = 20) -> list[dict]:
    """The latest changes to one record, newest first: who, when, what it
    became (null: it was deleted)."""
    with opstore.connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT action, actor, at, body FROM fleet_history WHERE kind = ? AND rid = ? ORDER BY id DESC LIMIT ?",
            (kind, id_, limit))]


# --- pydantic records --------------------------------------------------------

M = TypeVar("M", bound=BaseModel)


def encode(record: BaseModel) -> str:
    return json.dumps(record.model_dump(mode="json"))


def list_of(kind: str, model: type[M]) -> list[M]:
    return [model.model_validate(json.loads(body)) for body in records(kind).values()]


def load(kind: str, id_: str, model: type[M]) -> M:
    body = find_record(kind, id_)
    if body is None:
        raise NotFound(id_)
    return model.model_validate(json.loads(body))


def list_hosts() -> list[Host]:
    return list_of("host", Host)


def get_host(id_: str) -> Host:
    return load("host", id_, Host)


def upsert_host(host: Host) -> Host:
    check_id(host.id)
    save_record("host", host.id, encode(host))
    return host


def delete_host(id_: str) -> None:
    delete_record("host", id_)


def list_agents() -> list[Agent]:
    return list_of("agent", Agent)


def get_agent(id_: str, resolved: bool = False) -> Agent:
    """`resolved=True` returns the agent with `desired` replaced by the merge
    of its templates and its own overrides (see backend/templates.py). The
    stored record is never touched — this copy is for reads and driver calls.
    """
    agent = load("agent", id_, Agent)
    if resolved:
        from . import templates  # lazy: templates imports store.NotFound

        agent = agent.model_copy(update={"desired": templates.resolve(agent)})
    return agent


def upsert_agent(agent: Agent) -> Agent:
    check_id(agent.id)
    with opstore.connect() as db:
        if db.execute("SELECT 1 FROM fleet_records WHERE kind = 'host' AND id = ?", (agent.host,)).fetchone() is None:
            raise NotFound(f"host {agent.host!r} not in the fleet")
        put(db, "agent", agent.id, encode(agent))
    return agent


def delete_agent(id_: str) -> None:
    delete_record("agent", id_)


def list_decommissioned() -> list[Agent]:
    return list_of("decommissioned", Agent)


def archive_agent(id_: str) -> None:
    """Move an agent's record out of the live fleet into the decommissioned
    ones — kept for history rather than deleted outright.
    """
    with opstore.connect() as db:
        row = db.execute("SELECT body FROM fleet_records WHERE kind = 'agent' AND id = ?", (id_,)).fetchone()
        if row is None:
            raise NotFound(id_)
        put(db, "decommissioned", id_, row["body"], action="archive")
        drop(db, "agent", id_, action="archive")
