"""What Beacon did on hosts, and who asked.

`fleet_history` records who changed a fleet record. This records the other half
of the story: the actions that touch a host or hand someone access (deploy,
push, restart, decommission, ...). Every action that passes the confirm gate
(`app._confirm_gate`, the one chokepoint for tier T2 and up) leaves a row, so a
misuse of the MCP token or a browser session can at least be seen afterwards.

A row is written when the action is allowed to run, with outcome "requested".
Streamed operations (deploy, update, push, decommission) learn how they ended
and say so; for the others "requested" is all Beacon knows. The cron scheduler
(`python -m backend.scheduler`) calls drivers itself and is not recorded here.
Append-only by convention: nothing in the app updates a row except its outcome.
"""

from __future__ import annotations

import contextvars
import time

from . import opstore

# How the request was authenticated, set once per request (app._record_actor): a
# person's browser session, the MCP service token, an agent's own token, or
# "open" (no auth configured).
VIA: contextvars.ContextVar[str] = contextvars.ContextVar("audit_via", default="open")
# The row the current request wrote, so a stream it starts can finish it.
CURRENT: contextvars.ContextVar[int | None] = contextvars.ContextVar("audit_current", default=None)

OUTCOMES = ("requested", "succeeded", "failed", "did not finish", "interrupted")


def record(actor: str, via: str, capability: str, tier: str, target: str | None, detail: str) -> int:
    with opstore.connect() as db:
        action_id = db.execute(
            "INSERT INTO host_actions (at, actor, via, capability, tier, target, detail, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'requested')",
            (time.time(), actor, via, capability, tier, target, detail),
        ).lastrowid
    CURRENT.set(action_id)
    return action_id


def finish(action_id: int | None, outcome: str) -> None:
    if action_id is None:
        return
    with opstore.connect() as db:
        db.execute("UPDATE host_actions SET outcome = ? WHERE id = ?", (outcome, action_id))


def outcome_for(exit_code: str | None) -> str:
    """The outcome of a streamed script from the exit code its stream carried."""
    if exit_code == "0":
        return "succeeded"
    if exit_code is None or exit_code == "none":
        return "did not finish"
    return "failed"


def recent(limit: int = 100, target: str | None = None, actor: str | None = None) -> list[dict]:
    clauses, params = [], []
    if target:
        clauses.append("target = ?")
        params.append(target)
    if actor:
        clauses.append("actor = ?")
        params.append(actor)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with opstore.connect() as db:
        rows = db.execute(f"SELECT * FROM host_actions {where} ORDER BY id DESC LIMIT ?",
                          (*params, min(max(limit, 1), 1000))).fetchall()
    return [dict(r) for r in rows]
