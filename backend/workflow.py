"""Durable, typed governance requests for board work.

Workflow rows are operational state, not desired fleet YAML. The plan snapshot is
kept on the request so an approval cannot silently authorize a later config or
target change. Responses are restricted to the declared actor and recorded as
append-only events alongside the current lifecycle state.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from . import mesh, notify, opstore, store

REQUEST_KINDS = ("decision", "review", "approval")
STATES = (
    "pending", "approved", "claimed", "rejected", "changes_requested", "expired", "withdrawn",
    "stale", "executed", "verified", "failed", "execution_uncertain",
)
RESPONSE_ACTIONS = ("approved", "rejected", "changes_requested", "withdrawn", "choose_option")


class WorkflowError(ValueError):
    """Invalid workflow input or a request that cannot transition."""


class WorkflowNotFound(store.NotFound):
    pass


class UnauthorizedResponder(PermissionError):
    pass


class UnauthorizedWorkflowAccess(PermissionError):
    pass


class HumanApprovalRequired(PermissionError):
    pass


class StalePlan(WorkflowError):
    pass


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def plan_binding(payload: dict[str, Any], revision: str | None = None) -> dict[str, str]:
    """Return a stable revision/hash pair for an exact action plan."""
    digest = hashlib.sha256(json_text(payload).encode()).hexdigest()
    return {"plan_revision": revision or f"desired:{digest[:16]}", "plan_hash": digest}


def _decode(row: Any) -> dict:
    result = dict(row)
    for column, fallback in (("options_json", []), ("response_json", None), ("target_json", {})):
        raw = result.pop(column, None)
        if raw is None:
            result[column.removesuffix("_json")] = fallback
        else:
            result[column.removesuffix("_json")] = json.loads(raw)
    return result


def _is_responder(actor: str, eligible_responder: str) -> bool:
    """`actor` is the named responder. Emails differ only in case between a
    login and an owner field, so they compare without case; agent ids are lowercase."""
    return eligible_responder != "human" and actor.casefold() == eligible_responder.casefold()


def _visible(request: dict, actor: str, human: bool = False) -> bool:
    eligible_responder = request["eligible_responder"]
    return (
        actor == request["requester"]
        or _is_responder(actor, eligible_responder)
        or (human and eligible_responder == "human")
    )


def _event(db, request_id: int, actor: str, kind: str, from_state: str | None,
           to_state: str | None, payload: Any = None) -> None:
    db.execute(
        "INSERT INTO workflow_events (request_id, actor, kind, from_state, to_state, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (request_id, actor, kind, from_state, to_state, json_text(payload or {}), time.time()),
    )


def _expire_due(db, now: float) -> None:
    rows = db.execute(
        "SELECT id, state FROM workflow_requests WHERE state IN ('pending', 'approved') AND expires_at <= ?", (now,)
    ).fetchall()
    for row in rows:
        db.execute(
            "UPDATE workflow_requests SET state = 'expired', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        _event(db, row["id"], "beacon", "expired", row["state"], "expired")


def _validate_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise WorkflowError("actor must not be empty")
    return actor.strip()


def _validate_kind(kind: str) -> str:
    if kind not in REQUEST_KINDS:
        raise WorkflowError(f"kind must be one of {', '.join(REQUEST_KINDS)}")
    return kind


def validate_kind_for_tier(kind: str, tier: int) -> None:
    """Keep the request type aligned with the oversight required by its tier."""
    _validate_kind(kind)
    if not isinstance(tier, int) or tier < 0 or tier > 5:
        raise WorkflowError("tier must be an integer from 0 to 5")
    required = "approval" if tier >= 4 else "review" if tier == 3 else None
    if required and kind != required:
        raise WorkflowError(f"tier T{tier} actions require a {required} request")


def _eligible(request: dict, actor: str, human: bool) -> bool:
    eligible_responder = request["eligible_responder"]
    # `human` is an eligibility sentinel, never an agent identity. A browser
    # session is required even if an untrusted caller uses the same string.
    if eligible_responder == "human":
        return human and not (request["tier"] >= 3 and actor == request["requester"])
    if _is_responder(actor, eligible_responder):
        return not (request["tier"] >= 3 and actor.casefold() == request["requester"].casefold())
    return False


def _validate_requester_responder(requester: str, eligible_responder: str, tier: int) -> None:
    # A review/approval is an independent control. Beacon may create a request,
    # but the requester cannot satisfy its own T3+ gate.
    if tier >= 3 and requester == eligible_responder:
        raise WorkflowError("T3+ workflow requester and responder must be different actors")


def known_people() -> set[str]:
    """Who a request can be addressed to by email: the operators, and every
    agent's owner. Lowercase."""
    from . import auth  # not at import: auth reads its settings when it loads
    return set(auth.ALLOWED_EMAILS) | {a.owner.casefold() for a in store.list_agents() if a.owner}


def owner_of(agent_id: str) -> str | None:
    try:
        return store.get_agent(agent_id).owner or None
    except (store.NotFound, store.InvalidId):
        return None


def validate_participants(requester: str, eligible_responder: str) -> None:
    """Who may ask, and whom. The responder must be one a request can reach:
    before, any string was accepted, so a request addressed to a name nobody
    has (a first name, say) sat pending where no person and no agent could see
    or answer it until it expired."""
    try:
        source = store.get_agent(requester) if requester != mesh.BEACON_PEER else None
    except (store.NotFound, store.InvalidId):
        source = None  # a person (a REST call from the page), not an agent
    if source is not None and (not source.reach or not source.reach.board):
        raise UnauthorizedWorkflowAccess(f"{requester} is not authorized for workflow board access")
    if eligible_responder in {"human", requester}:
        return
    if "@" in eligible_responder:
        if eligible_responder.casefold() not in known_people():
            raise WorkflowError(
                f"{eligible_responder!r} is neither an operator nor an agent's owner. Use \"human\" for any "
                "operator, or the owner's email (agent_owner tells you whose it is)")
        return
    try:
        target = store.get_agent(eligible_responder)
    except (store.NotFound, store.InvalidId):
        raise WorkflowError(
            f"{eligible_responder!r} is not someone a request can be addressed to. Use \"human\" for any "
            "operator, an agent id, or the email of an operator or of an agent's owner (agent_owner tells "
            "you who owns an agent)") from None
    if source is not None and not mesh.may_call(requester, target):
        raise UnauthorizedWorkflowAccess(f"{eligible_responder} does not accept workflow requests from {requester}")


def create(*, card_id: int, kind: str, requester: str, eligible_responder: str,
           capability: str, tier: int, title: str, summary: str,
           options: list[str] | None, target: dict[str, Any] | None,
           plan_revision: str, plan_hash: str, expires_at: float | None = None,
           now: float | None = None) -> dict:
    """Create one pending request and its initial history event."""
    if kind not in REQUEST_KINDS:
        raise WorkflowError(f"kind must be one of {', '.join(REQUEST_KINDS)}")
    requester = _validate_actor(requester)
    eligible_responder = _validate_actor(eligible_responder)
    if "@" in eligible_responder:
        eligible_responder = eligible_responder.casefold()
    if not isinstance(capability, str) or not capability.strip():
        raise WorkflowError("capability must not be empty")
    if not isinstance(tier, int) or tier < 0 or tier > 5:
        raise WorkflowError("tier must be an integer from 0 to 5")
    validate_kind_for_tier(kind, tier)
    _validate_requester_responder(requester, eligible_responder, tier)
    validate_participants(requester, eligible_responder)
    if not isinstance(title, str) or not title.strip():
        raise WorkflowError("title must not be empty")
    if not isinstance(summary, str) or not summary.strip():
        raise WorkflowError("summary must not be empty")
    if not isinstance(plan_revision, str) or not plan_revision.strip():
        raise WorkflowError("plan_revision must not be empty")
    if not isinstance(plan_hash, str) or not plan_hash.strip():
        raise WorkflowError("plan_hash must not be empty")
    options = options or []
    if not isinstance(options, list) or any(not isinstance(option, str) or not option.strip() for option in options):
        raise WorkflowError("options must be a list of non-empty strings")
    if len(set(options)) != len(options):
        raise WorkflowError("options must not contain duplicates")
    if kind == "decision" and not options:
        raise WorkflowError("decision requests require at least one option")
    target = target or {}
    if not isinstance(target, dict):
        raise WorkflowError("target must be an object")
    now = time.time() if now is None else now
    expires_at = now + 24 * 60 * 60 if expires_at is None else expires_at
    if not isinstance(expires_at, (int, float)) or expires_at <= now:
        raise WorkflowError("expires_at must be in the future")

    with opstore.connect() as db:
        if db.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone() is None:
            raise WorkflowNotFound(f"card {card_id} not found")
        request_id = db.execute(
            "INSERT INTO workflow_requests (card_id, kind, state, requester, eligible_responder, capability, tier, "
            "title, summary, options_json, target_json, plan_revision, plan_hash, expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (card_id, kind, "pending", requester, eligible_responder, capability.strip(), tier, title.strip(), summary.strip(),
             json_text(options), json_text(target), plan_revision.strip(), plan_hash.strip(), expires_at, now, now),
        ).lastrowid
        _event(db, request_id, requester, "created", None, "pending", {
            "kind": kind, "capability": capability.strip(), "tier": tier,
        })
    created = get(request_id)
    notify.workflow_created(created)  # after the commit: a notice must never undo the request
    return created


def list_requests(*, card_id: int | None = None, state: str | None = None,
                  include_done: bool = True, actor: str | None = None, human: bool = False) -> list[dict]:
    with opstore.connect() as db:
        _expire_due(db, time.time())
        clauses, params = [], []
        if card_id is not None:
            clauses.append("card_id = ?")
            params.append(card_id)
        if state:
            if state not in STATES:
                raise WorkflowError(f"unknown workflow state {state!r}")
            clauses.append("state = ?")
            params.append(state)
        if not include_done:
            clauses.append("state NOT IN ('executed', 'verified', 'failed', 'execution_uncertain', 'rejected', 'withdrawn', 'expired', 'stale')")
        if actor is not None:
            if human:
                clauses.append("(requester = ? OR lower(eligible_responder) = lower(?) OR eligible_responder = 'human')")
            else:
                clauses.append("(requester = ? OR (eligible_responder != 'human' AND lower(eligible_responder) = lower(?)))")
            params.extend((actor, actor))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = db.execute(f"SELECT * FROM workflow_requests {where} ORDER BY updated_at DESC, id DESC", params).fetchall()
    return [_decode(row) for row in rows]


def get(request_id: int, actor: str | None = None, human: bool = False) -> dict:
    with opstore.connect() as db:
        _expire_due(db, time.time())
        row = db.execute("SELECT * FROM workflow_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"workflow request {request_id} not found")
        result = _decode(row)
        if actor is not None and not _visible(result, actor, human):
            raise UnauthorizedWorkflowAccess(f"{actor} cannot access workflow request {request_id}")
        events = db.execute("SELECT * FROM workflow_events WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
    result["events"] = []
    for event in events:
        item = dict(event)
        item["payload"] = json.loads(item.pop("payload_json"))
        result["events"].append(item)
    return result


def respond(request_id: int, *, actor: str, human: bool, action: str,
            response: Any, plan_revision: str, plan_hash: str) -> dict:
    """Record a response if actor, plan binding, and lifecycle all permit it."""
    actor = _validate_actor(actor)
    if action not in RESPONSE_ACTIONS:
        raise WorkflowError(f"action must be one of {', '.join(RESPONSE_ACTIONS)}")
    with opstore.connect() as db:
        _expire_due(db, time.time())
        row = db.execute("SELECT * FROM workflow_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"workflow request {request_id} not found")
        request = _decode(row)
        if request["state"] != "pending":
            if request["state"] == "expired":
                db.commit()
            raise WorkflowError(f"workflow request {request_id} is {request['state']}")
        if not _eligible(request, actor, human):
            raise UnauthorizedResponder(f"{actor} is not eligible to respond to workflow request {request_id}")
        if request["tier"] >= 4 and not human:
            raise HumanApprovalRequired("T4/T5 workflow responses require a human browser session")
        if plan_revision != request["plan_revision"] or plan_hash != request["plan_hash"]:
            now = time.time()
            db.execute("UPDATE workflow_requests SET state = 'stale', updated_at = ? WHERE id = ?", (now, request_id))
            _event(db, request_id, actor, "state_changed", "pending", "stale", {
                "reason": "plan binding mismatch", "plan_revision": plan_revision, "plan_hash": plan_hash,
            })
            db.commit()
            raise StalePlan("plan revision/hash no longer matches this workflow request")
        if request["kind"] == "decision":
            option = response.get("option") if isinstance(response, dict) else response
            if action == "choose_option" and option not in request["options"]:
                raise WorkflowError(f"option must be one of {', '.join(request['options'])}")
            if action == "approved" and isinstance(response, dict) and "option" in response and response["option"] not in request["options"]:
                raise WorkflowError(f"option must be one of {', '.join(request['options'])}")
        if action == "choose_option":
            state = "approved"
        elif action in ("approved", "rejected", "changes_requested", "withdrawn"):
            state = action
        else:  # defensive; action is validated above
            raise WorkflowError(f"unsupported response action {action!r}")
        now = time.time()
        db.execute(
            "UPDATE workflow_requests SET state = ?, response_json = ?, responded_at = ?, updated_at = ? WHERE id = ?",
            (state, json_text(response), now, now, request_id),
        )
        _event(db, request_id, actor, "responded", "pending", state, {
            "action": action, "plan_revision": plan_revision, "plan_hash": plan_hash, "response": response,
        })
    answered = get(request_id)
    notify.workflow_answered(answered, actor)
    return answered


def authorize(*, request_id: int | None, capability: str, tier: int, actor: str,
               human: bool, target: dict[str, Any], plan_revision: str,
               plan_hash: str) -> dict | None:
    """Atomically claim an approved, exact-bound request for one execution."""
    if tier < 3:
        return None
    actor = _validate_actor(actor)
    if request_id is None:
        raise WorkflowError(
            f"tier T{tier} execution requires an approved workflow request; "
            f"create one with plan_revision={plan_revision!r} and plan_hash={plan_hash!r}"
        )
    with opstore.connect() as db:
        _expire_due(db, time.time())
        row = db.execute("SELECT * FROM workflow_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"workflow request {request_id} not found")
        request = _decode(row)
        if not (
            actor == request["requester"]
            or _is_responder(actor, request["eligible_responder"])
            or (human and request["eligible_responder"] == "human")
        ):
            raise UnauthorizedWorkflowAccess(f"{actor} cannot execute workflow request {request_id}")
        if request["state"] != "approved":
            if request["state"] == "expired":
                db.commit()
            raise WorkflowError(f"workflow request {request_id} is {request['state']}, not approved")
        if request["capability"] != capability or request["tier"] != tier:
            raise WorkflowError("workflow request capability or tier does not match the action")
        if request["target"] != target:
            now = time.time()
            changed = db.execute("UPDATE workflow_requests SET state = 'stale', updated_at = ? WHERE id = ? AND state = 'approved'", (now, request_id))
            if changed.rowcount == 1:
                _event(db, request_id, actor, "state_changed", "approved", "stale", {"reason": "execution target changed"})
            raise StalePlan("approved workflow request target does not match the current action")
        if request["plan_revision"] != plan_revision or request["plan_hash"] != plan_hash:
            now = time.time()
            changed = db.execute("UPDATE workflow_requests SET state = 'stale', updated_at = ? WHERE id = ? AND state = 'approved'", (now, request_id))
            if changed.rowcount == 1:
                _event(db, request_id, actor, "state_changed", "approved", "stale", {"reason": "plan changed"})
            raise StalePlan("approved workflow request is stale for the current plan")
        if tier >= 4 and not human:
            raise HumanApprovalRequired("T4/T5 actions require a human browser session; MCP callers cannot execute them")
        now = time.time()
        claimed = db.execute(
            "UPDATE workflow_requests SET state = 'claimed', updated_at = ? WHERE id = ? AND state = 'approved'",
            (now, request_id),
        )
        if claimed.rowcount != 1:
            raise WorkflowError(f"workflow request {request_id} was already claimed")
        _event(db, request_id, actor, "state_changed", "approved", "claimed", {
            "capability": capability, "tier": tier, "plan_revision": plan_revision, "plan_hash": plan_hash,
        })
        request["state"] = "claimed"
    return request


def claim_approved(request_id: int, *, actor: str, capability: str) -> dict:
    """Claim a person-approved request for its own requester, with no browser
    session at claim time (authorize() wants one for T4). Safe only for a
    capability whose result goes to the requester and does nothing until a
    person said yes: respond() already made that yes a human browser session,
    and the target is what that person saw, so it is read from the request."""
    actor = _validate_actor(actor)
    with opstore.connect() as db:
        _expire_due(db, time.time())
        row = db.execute("SELECT * FROM workflow_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"workflow request {request_id} not found")
        request = _decode(row)
        if request["requester"] != actor:
            raise UnauthorizedWorkflowAccess(f"{actor} cannot claim workflow request {request_id}")
        if request["capability"] != capability or request["tier"] != 4 or request["eligible_responder"] != "human":
            raise WorkflowError(f"workflow request {request_id} is not a person-approved {capability} request")
        if request["state"] != "approved":
            if request["state"] == "expired":
                db.commit()
            raise WorkflowError(f"workflow request {request_id} is {request['state']}, not approved")
        claimed = db.execute("UPDATE workflow_requests SET state = 'claimed', updated_at = ? WHERE id = ? AND state = 'approved'",
                             (time.time(), request_id))
        if claimed.rowcount != 1:
            raise WorkflowError(f"workflow request {request_id} was already claimed")
        _event(db, request_id, actor, "state_changed", "approved", "claimed", {"capability": capability})
        request["state"] = "claimed"
    return request


def mark_stale(request_id: int, actor: str, reason: str) -> None:
    with opstore.connect() as db:
        row = db.execute("SELECT state FROM workflow_requests WHERE id = ?", (request_id,)).fetchone()
        if row and row["state"] == "approved":
            now = time.time()
            db.execute("UPDATE workflow_requests SET state = 'stale', updated_at = ? WHERE id = ?", (now, request_id))
            _event(db, request_id, actor, "state_changed", "approved", "stale", {"reason": reason})


def mark_executed(request_id: int, actor: str, verified: bool = False) -> dict:
    """Finalize a claimed request after a driver action has completed."""
    next_state = "verified" if verified else "executed"
    with opstore.connect() as db:
        row = db.execute("SELECT state FROM workflow_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"workflow request {request_id} not found")
        if row["state"] == next_state:
            return get(request_id)
        if row["state"] != "claimed":
            raise WorkflowError(f"workflow request {request_id} is {row['state']}")
        now = time.time()
        changed = db.execute("UPDATE workflow_requests SET state = ?, updated_at = ? WHERE id = ? AND state = 'claimed'", (next_state, now, request_id))
        if changed.rowcount != 1:
            raise WorkflowError(f"workflow request {request_id} was finalized concurrently")
        _event(db, request_id, actor, "state_changed", "claimed", next_state)
    return get(request_id)


def mark_execution_outcome(request_id: int, actor: str, state: str) -> dict:
    if state not in {"failed", "execution_uncertain"}:
        raise WorkflowError(f"invalid execution outcome {state!r}")
    with opstore.connect() as db:
        row = db.execute("SELECT state FROM workflow_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"workflow request {request_id} not found")
        if row["state"] == state:
            return get(request_id)
        if row["state"] != "claimed":
            raise WorkflowError(f"workflow request {request_id} is {row['state']}")
        now = time.time()
        changed = db.execute("UPDATE workflow_requests SET state = ?, updated_at = ? WHERE id = ? AND state = 'claimed'", (state, now, request_id))
        if changed.rowcount != 1:
            raise WorkflowError(f"workflow request {request_id} was finalized concurrently")
        _event(db, request_id, actor, "execution_outcome", "claimed", state)
    return get(request_id)
