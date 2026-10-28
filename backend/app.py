import contextlib
import datetime
import hashlib
import hmac
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import auth, availability, board, bus, cron_store, fleet, mailbox, mesh, onboarding, opstore, policy, scheduler, store, templates, workflow
from .board_mcp import board_app
from .drivers import NoSSH, for_agent
from .schemas import Agent, CronJob, Host
from .tiers import TIER_LABELS, CAPABILITY_TIERS, requires_confirm, tier_for


class FixRequest(BaseModel):
    fix: str
    confirm: bool = False


class TemplateApplyRequest(BaseModel):
    agent_ids: list[str]
    confirm: bool = False


class EnrollRequest(BaseModel):
    docker: str | None = None  # the agent's container; the command then runs on its host as root
    docker_user: str = "hermes"
    schedule: bool = True
    confirm: bool = False


class TemplateBody(BaseModel):
    content: dict
    confirm: bool = False


class DecommissionRequest(BaseModel):
    purge: bool = False
    remove_user: bool = False
    confirm: bool = False


class CallAgentRequest(BaseModel):
    message: str
    context_id: str | None = None
    # Go through the Beacon mailbox: an offline agent gets voicemail, and an
    # answer slower than mailbox.HANDOFF_SECONDS comes back later instead of
    # being lost. Either way the call returns a task id (mbx-N); the answer
    # turns up in GET /api/mailbox/tasks/{task_id} and GET /api/mailbox.
    queue_if_offline: bool = False

    @field_validator("message")
    @classmethod
    def _message_present(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty")
        return v

logger = logging.getLogger("beacon")



@contextlib.asynccontextmanager
async def lifespan(_app):
    fleet.bootstrap()  # a store with no fleet yet takes the YAML one, once
    try:
        problems = policy.problems(policy.load(), store.list_agents())
    except Exception as exc:  # an invalid policy must not stop Beacon from starting
        problems = [f"the access policy is invalid: {exc}"]
    for problem in problems:
        logger.warning("access policy: %s", problem)
    async with board_app.lifespan():
        yield


VERSION = "0.25.0"


async def _record_actor(request: Request) -> None:
    """Who changes the fleet in this request, for its history. Async so that it
    runs in the request's own context, which the route function then inherits."""
    user = request.session.get("user") if "session" in request.scope else None
    store.ACTOR.set((user or {}).get("email") or mesh.BEACON_PEER)


app = FastAPI(title="Beacon", version=VERSION, lifespan=lifespan, dependencies=[Depends(_record_actor)])


def _confirm_gate(capability: str, confirm: bool, description: str, **tier_params) -> dict | None:
    """Returns a "would run" dict if `capability` needs confirmation and
    didn't get it; None if the caller should proceed. This is the same
    plan-before-apply convention mcp/server.py has always applied to its own
    callers — enforced here too, on the resource itself, so a direct API
    call gets the same protection an MCP client always had rather than
    executing immediately.
    """
    tier, rationale = tier_for(capability, **tier_params)
    if not requires_confirm(tier) or confirm:
        return None
    return {
        "would_run": True,
        "tier": tier.name,
        "tier_label": TIER_LABELS[tier],
        "detail": f"Would {description} (tier {tier.name} — {rationale}). Call again with confirm=true to actually run it.",
    }


def _require_human_session(request: Request, capability: str, **tier_params) -> None:
    """T4/T5 actions must not be authorized by MCP's service token.

    The token is held by an LLM-facing MCP server, so `confirm=true` from that
    caller is not a human approval. Browser sessions remain the operator path
    until a real, out-of-band approval mechanism exists.
    """
    tier, _ = tier_for(capability, **tier_params)
    if tier >= 4 and getattr(request.state, "auth_via_mcp", False):
        raise HTTPException(403, f"{tier.name} action requires a human browser session; MCP callers cannot execute it")

app.include_router(auth.router)
app.add_middleware(auth.CSRFMiddleware)
app.add_middleware(auth.AuthMiddleware)
# Outermost — added last, so it runs first, request.session exists by
# the time AuthMiddleware reads it, and CSRF runs after auth state is set.
app.add_middleware(SessionMiddleware, secret_key=auth.SESSION_SECRET, max_age=8 * 60 * 60, https_only=auth.SESSION_HTTPS_ONLY)


@app.middleware("http")
async def _csp(request, call_next):
    # Defense in depth alongside frontend/app.js's own escaping (esc()) —
    # the frontend renders data that ultimately comes from remote agent
    # hosts (plugin manifests, config.yaml values), so a second layer here
    # limits what an XSS payload that slips through can actually do.
    response = await call_next(request)
    # style-src/font-src: frontend/index.html loads IBM Plex from Google
    # Fonts (the stylesheet from googleapis.com, the actual font files from
    # gstatic.com) — both need an explicit allowance or the browser silently
    # drops the request and every page falls back to a system font.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com"
    )
    return response


@app.get("/api/version")
def get_version() -> dict:
    return {"version": VERSION}


@app.get("/api/tiers")
def list_tiers() -> dict:
    """The tier registry: every gated capability, its tier, and the
    rationale for that assignment. See backend/tiers.py."""
    return {
        cap: {"tier": tier.name, "tier_label": TIER_LABELS[tier], "rationale": rationale}
        for cap, (tier, rationale) in CAPABILITY_TIERS.items()
    }


def _error_handler(status_code: int):
    def handler(_request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})
    return handler


# store.NotFound/InvalidId also cover cron_store and templates, which reuse
# them. RuntimeError is a driver's own "couldn't talk to the host" signal
# (e.g. list_plugins on an unreachable agent) — mapped to a real detail
# message instead of falling through to a bare, undetailed 500.
for _exc, _status in ((store.NotFound, 404), (store.InvalidId, 400), (ValueError, 400), (RuntimeError, 502), (bus.Unreachable, 502)):
    app.add_exception_handler(_exc, _error_handler(_status))
app.add_exception_handler(workflow.UnauthorizedResponder, _error_handler(403))
app.add_exception_handler(workflow.UnauthorizedWorkflowAccess, _error_handler(403))
app.add_exception_handler(workflow.HumanApprovalRequired, _error_handler(403))
app.add_exception_handler(workflow.StalePlan, _error_handler(409))
app.add_exception_handler(NoSSH, _error_handler(409))


def _started(lines: Iterator[str]) -> Iterator[str]:
    """Pull the first chunk synchronously while preserving close propagation."""
    try:
        first = next(lines)
    except StopIteration:
        close = getattr(lines, "close", None)
        if close is not None:
            close()
        return iter(())

    def started():
        try:
            yield first
            yield from lines
        finally:
            close = getattr(lines, "close", None)
            if close is not None:
                close()

    return started()


def _split_exit_marker(lines: Iterator[str]) -> tuple[Iterator[str], Callable[[], str | None]]:
    """ssh.stream_script always ends its stream with a `__BEACON_EXIT__<code>`
    line — internal bookkeeping, not output for a human. Returns the stream
    with that line removed, plus a getter for the code it carried (None
    until the returned iterator has been fully consumed).
    """
    code = None

    def filtered() -> Iterator[str]:
        nonlocal code
        try:
            for line in lines:
                if line.startswith("__BEACON_EXIT__"):
                    code = line.removeprefix("__BEACON_EXIT__")
                else:
                    yield line
        finally:
            close = getattr(lines, "close", None)
            if close is not None:
                close()

    return filtered(), lambda: code


def _stream(lines: Iterator[str]) -> StreamingResponse:
    filtered, _ = _split_exit_marker(_started(lines))

    def body_stream():
        try:
            yield from (line + "\n" for line in filtered)
        finally:
            close = getattr(filtered, "close", None)
            if close is not None:
                close()

    return StreamingResponse(body_stream(), media_type="text/plain")


def _workflow_stream(lines: Iterator[str], request_id: int, actor: str) -> StreamingResponse:
    """Record the outcome only after the driver stream ends.

    A nonzero exit is a failed operation; an interrupted or marker-less stream
    is execution-uncertain because the remote side may have applied part of it.
    Both outcomes consume the approval and require a fresh review.
    """
    try:
        started = _started(lines)
    except Exception:
        close = getattr(lines, "close", None)
        if close is not None:
            close()
        # Pull the first driver result before starting the HTTP response so a
        # connection/setup failure still gets a normal error status.
        workflow.mark_execution_outcome(request_id, actor, "execution_uncertain")
        raise

    def body_stream():
        filtered = None
        try:
            filtered, exit_code = _split_exit_marker(started)
            for line in filtered:
                yield line + "\n"
            code = exit_code()
            if code == "0":
                workflow.mark_executed(request_id, actor)
            else:
                workflow.mark_execution_outcome(request_id, actor, "failed" if code is not None else "execution_uncertain")
        except GeneratorExit:
            workflow.mark_execution_outcome(request_id, actor, "execution_uncertain")
            raise
        except Exception as exc:
            workflow.mark_execution_outcome(request_id, actor, "execution_uncertain")
            yield f"[beacon] execution uncertain: {exc}\n"
        finally:
            close = getattr(filtered, "close", None)
            if close is not None:
                close()
            close = getattr(started, "close", None)
            if close is not None:
                close()

    return StreamingResponse(body_stream(), media_type="text/plain")


def _used_by(name: str, agents: list[Agent]) -> list[str]:
    return sorted(a.id for a in agents if name in a.templates)


@app.get("/api/hosts")
def list_hosts() -> list[Host]:
    return store.list_hosts()


@app.get("/api/hosts/{id_}")
def get_host(id_: str) -> Host:
    return store.get_host(id_)


@app.put("/api/hosts/{id_}")
def put_host(id_: str, host: Host) -> Host:
    if host.id != id_:
        raise HTTPException(400, "body id must match URL id")
    return store.upsert_host(host)


@app.delete("/api/hosts/{id_}")
def delete_host(id_: str) -> dict:
    store.delete_host(id_)
    return {"ok": True}


@app.get("/api/agents")
def list_agents() -> list[dict]:
    # chat_open is what the agent's host last reported (display-only, like
    # effective_desired): the settings that open its chat to anyone, [] for
    # none, null when its sync never reported. Open withholds the board.
    exposure = mesh.open_chats()
    return [a.model_dump() | {"chat_open": exposure.get(a.id)} for a in store.list_agents()]


@app.get("/api/availability")
def get_availability() -> dict[str, dict]:
    """Every agent's availability over A2A (backend/availability.py), in one
    call: the Agents tab polls this instead of one SSH call per agent."""
    return availability.check_all(store.list_agents())


class PolicyText(BaseModel):
    text: str


class PolicySave(BaseModel):
    text: str
    # The hash of the text the editor loaded. If the file changed since, the
    # save is refused rather than overwriting someone else's edit.
    base_hash: str
    confirm: bool = False


@app.get("/api/policy")
def get_policy() -> dict:
    """The access policy as it stands: the file's text (for the editor), the
    rules, what looks wrong in them, and the resulting who-may-call-whom table."""
    loaded = policy.load()
    fleet = store.list_agents()
    text = policy.read_text()
    return {
        "present": loaded is not None,
        "text": text,
        "hash": policy.text_hash(text),
        "policy": loaded.model_dump(by_alias=True) if loaded else None,
        "problems": policy.problems(loaded, fleet),
        "matrix": policy.matrix(loaded, fleet),
    }


@app.post("/api/policy/preview")
def preview_policy(body: PolicyText) -> dict:
    """What saving this text would do, without saving it. Read-only."""
    return policy.preview(body.text, store.list_agents())


@app.put("/api/policy")
def put_policy(body: PolicySave, request: Request):
    """Replace the access policy with the edited text. A human browser session
    only (T4), asks for confirmation first, refuses invalid text and a policy
    that changed since the editor loaded it, and keeps the old version in
    the fleet history."""
    _require_human_session(request, "set_policy")
    checked = policy.preview(body.text, store.list_agents())
    if checked["errors"]:
        raise HTTPException(422, "; ".join(checked["errors"]))
    if policy.text_hash(policy.read_text()) != body.base_hash:
        raise HTTPException(409, "the access policy changed since you opened it; reload the editor and redo your edit")
    summary = (f"replace the access policy: {len(checked['added'])} call path(s) added, "
               f"{len(checked['removed'])} removed; restarts {', '.join(checked['restarts']) or 'no agents'}")
    if (gate := _confirm_gate("set_policy", body.confirm, summary)) is not None:
        return {**gate, **{k: checked[k] for k in ("added", "removed", "restarts", "problems")}}
    policy.save(body.text, _board_author(request))
    logger.warning("access policy replaced by %s: %s", _board_author(request), summary)
    return {"saved": True, **checked, "hash": policy.text_hash(body.text)}


@app.get("/api/agents/registry")
def agent_registry() -> list[dict]:
    """Who can be called, and as what. Endpoints and key-env names stay off
    this list — a caller names an agent, it does not learn how to reach it.
    """
    return [
        {
            "id": agent.id,
            "callable": agent.reach is not None,
            "capabilities": list(agent.reach.capabilities) if agent.reach else [],
            # Who may call it: a list of agent ids, or null for anyone on its network.
            "callers": agent.reach.callers if agent.reach else None,
            "tags": agent.tags,
        }
        for agent in store.list_agents()
    ]


@app.post("/api/agents/{id_}/call")
def call_agent(id_: str, body: CallAgentRequest) -> dict:
    """One agent calls another by name. Auth is the same session or MCP
    bearer as every other /api route; the hop itself is A2A SendMessage.
    """
    if body.queue_if_offline:
        return {"agent": id_, **mailbox.send(mesh.BEACON_PEER, id_, body.message, body.context_id)}
    return {"agent": id_, **bus.call(store.get_agent(id_), body.message, body.context_id)}


class CardCreate(BaseModel):
    title: str
    body: str = ""
    project: str | None = None
    assignee: str | None = None


class CardUpdate(BaseModel):
    state: str | None = None
    assignee: str | None = None
    title: str | None = None
    body: str | None = None
    project: str | None = None
    note: str | None = None
    context_id: str | None = None


class CardComment(BaseModel):
    text: str
    context_id: str | None = None


class WorkflowCreateRequest(BaseModel):
    kind: str
    eligible_responder: str
    capability: str
    tier: int
    title: str
    summary: str
    options: list[str] = []
    target: dict = {}
    plan_revision: str
    plan_hash: str
    expires_at: float | None = None


class WorkflowResponseRequest(BaseModel):
    action: str
    response: object = None
    plan_revision: str
    plan_hash: str


def _workflow_human(request: Request) -> bool:
    """Only an authenticated browser session counts as a human approver."""
    return (
        not getattr(request.state, "auth_via_mcp", False)
        and not getattr(request.state, "workflow_agent", None)
        and bool(request.session.get("user"))
    )


def _workflow_actor(request: Request) -> str:
    return getattr(request.state, "workflow_agent", None) or _board_author(request)


def _workflow_view_actor(request: Request) -> str:
    if getattr(request.state, "workflow_agent", None):
        return request.state.workflow_agent
    if getattr(request.state, "auth_via_mcp", False):
        return mesh.BEACON_PEER
    user = request.session.get("user") if "session" in request.scope else None
    return (user or {}).get("email") or mesh.BEACON_PEER


def _workflow_response_actor(request: Request) -> str:
    if getattr(request.state, "workflow_agent", None):
        return request.state.workflow_agent
    user = request.session.get("user") if "session" in request.scope else None
    return (user or {}).get("email") or mesh.BEACON_PEER
def _ssh_config_binding(config_file: str | None) -> dict[str, str | None]:
    """Bind config-file contents when SSH delegates routing/auth to that file."""
    if not config_file:
        return {"config_file": None, "config_file_sha256": None}
    try:
        content = Path(config_file).expanduser().read_bytes()
    except OSError as exc:
        raise workflow.WorkflowError(f"SSH config file {config_file!r} is not readable: {exc}") from exc
    return {
        "config_file": config_file,
        "config_file_sha256": hashlib.sha256(content).hexdigest(),
    }


def _workflow_plan_for_push(agent_id: str, agent, host, driver) -> tuple[dict, dict[str, str]]:
    # Bind every value the driver receives. Agent ids alone are insufficient:
    # reassignment, credentials, profile selection, type, or driver changes
    # must invalidate an earlier review.
    target = {
        "agent_id": agent_id,
        "agent_profile": agent.profile,
        "agent_type": agent.type,
        "driver": getattr(driver, "__name__", repr(driver)),
        "host_id": host.id,
        "host_address": host.address,
        "ssh": host.ssh.model_dump() | _ssh_config_binding(host.ssh.config_file),
        "agent": agent.model_dump(),
        "host": host.model_dump(),
    }
    binding = workflow.plan_binding({"capability": "push_config", "target": target})
    return target, binding


def _validate_workflow_kind(kind: str, tier: int) -> None:
    workflow.validate_kind_for_tier(kind, tier)


def _create_workflow(card_id: int, body: WorkflowCreateRequest, request: Request) -> dict:
    _validate_workflow_kind(body.kind, body.tier)
    try:
        expected_tier, _ = tier_for(body.capability)
    except KeyError:
        expected_tier = None
    if expected_tier is not None and int(expected_tier) != body.tier:
        raise workflow.WorkflowError(f"capability {body.capability!r} is tier {expected_tier.name}, not T{body.tier}")
    actor = _workflow_actor(request)
    return workflow.create(
        card_id=card_id, kind=body.kind, requester=actor,
        eligible_responder=body.eligible_responder, capability=body.capability,
        tier=body.tier, title=body.title, summary=body.summary, options=body.options,
        target=body.target, plan_revision=body.plan_revision, plan_hash=body.plan_hash,
        expires_at=body.expires_at,
    )


def _respond_workflow(request_id: int, body: WorkflowResponseRequest, request: Request) -> dict:
    return workflow.respond(
        request_id, actor=_workflow_response_actor(request), human=_workflow_human(request),
        action=body.action, response=body.response,
        plan_revision=body.plan_revision, plan_hash=body.plan_hash,
    )


def _board_author(request: Request) -> str:
    """Who is changing the board from the UI or REST: the logged-in person,
    else "beacon" (MCP bearer, or open mode). Agents use /board/mcp."""
    user = request.session.get("user") if "session" in request.scope else None
    return (user or {}).get("email") or mesh.BEACON_PEER


@app.get("/api/board")
def board_list(project: str | None = None, assignee: str | None = None, state: str | None = None,
               include_done: bool = False) -> list[dict]:
    return board.list_cards(project, assignee, state, include_done)


@app.get("/api/board/cards/{card_id}")
def board_card(card_id: int) -> dict:
    return board.get_card(card_id)


@app.post("/api/board/cards")
def board_create(body: CardCreate, request: Request) -> dict:
    return board.create_card(_board_author(request), body.title, body.body, body.project, body.assignee)


@app.patch("/api/board/cards/{card_id}")
def board_update(card_id: int, body: CardUpdate, request: Request) -> dict:
    return board.update_card(_board_author(request), card_id, **body.model_dump())


@app.post("/api/board/cards/{card_id}/comments")
def board_comment(card_id: int, body: CardComment, request: Request) -> dict:
    return board.comment(_board_author(request), card_id, body.text, body.context_id)


@app.get("/api/board/cards/{card_id}/workflows")
def card_workflows(card_id: int, request: Request, state: str | None = None) -> list[dict]:
    board.get_card(card_id)
    return workflow.list_requests(card_id=card_id, state=state, actor=_workflow_view_actor(request), human=_workflow_human(request))


@app.post("/api/board/cards/{card_id}/workflows")
def create_card_workflow(card_id: int, body: WorkflowCreateRequest, request: Request) -> dict:
    return _create_workflow(card_id, body, request)


@app.get("/api/workflows")
def workflows_list(request: Request, card_id: int | None = None, state: str | None = None,
                   include_done: bool = True) -> list[dict]:
    return workflow.list_requests(card_id=card_id, state=state, include_done=include_done,
                                  actor=_workflow_view_actor(request), human=_workflow_human(request))


@app.get("/api/workflows/{request_id}")
def workflow_get(request_id: int, request: Request) -> dict:
    return workflow.get(request_id, actor=_workflow_view_actor(request), human=_workflow_human(request))


@app.post("/api/workflows/{request_id}/respond")
def workflow_respond(request_id: int, body: WorkflowResponseRequest, request: Request) -> dict:
    return _respond_workflow(request_id, body, request)


@app.get("/api/mailbox")
def mailbox_recent(limit: int = 50) -> list[dict]:
    """What the mailbox holds, newest first: queued, delivered, and replies
    addressed to Beacon (recipient "beacon") — the voicemail MCP callers left.
    """
    return mailbox.recent(min(max(limit, 1), 500))


@app.get("/api/mailbox/tasks/{task_id}")
def mailbox_task(task_id: str) -> dict:
    """Current A2A-shaped status for a durable Beacon mailbox task (``mbx-N``)."""
    try:
        return mailbox.task_view(task_id)
    except store.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


A2A_METHODS = {"SendMessage": True, "message/send": False}  # name -> answers in the v1.0 {"task": ...} wrapper
TASK_GET_METHODS = {"GetTask", "tasks/get"}
# The mailbox card says streaming: false. A client treats a closed stream as a
# finished task, so a one-snapshot stream of a task still working would lie.
TASK_SUBSCRIBE_METHODS = {"SubscribeToTask", "tasks/subscribe"}


def _a2a_task(task_id: str, caller: str, target: str) -> dict:
    return mailbox.task_view(task_id, requester=caller, target=target)


def _a2a_error(req_id, code: int, message: str, status: int) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
                        status_code=status)


@app.post("/a2a/{id_}")
async def a2a_mailbox(id_: str, request: Request):
    """The Beacon mailbox, speaking A2A on behalf of agent `id_` (relay now,
    or hold and deliver later — backend/mailbox.py). Outside /api/: callers
    are fleet agents, authenticated by their own pairwise token for `id_`.
    """
    try:
        req = await request.json()
    except ValueError:
        return _a2a_error(None, -32700, "parse error", 400)
    req_id = req.get("id") if isinstance(req, dict) else None
    caller = mailbox.caller_for(id_, request.headers.get("authorization"))
    if caller is None:
        return _a2a_error(req_id, -32050, "unauthorized", 401)
    try:
        target = store.get_agent(id_)
    except store.NotFound:
        return _a2a_error(req_id, -32001, f"no agent {id_!r}", 404)
    if not mesh.may_call(caller, target):
        logger.warning("mailbox: refused %s -> %s (access policy: no rule allows it)", caller, id_)
        return _a2a_error(req_id, -32052, f"{id_} does not accept calls from {caller}", 403)
    method = req.get("method")
    if method in TASK_SUBSCRIBE_METHODS:
        return _a2a_error(req_id, -32004, "the Beacon mailbox does not stream; poll GetTask instead", 200)
    if method in TASK_GET_METHODS:
        params = req.get("params") or {}
        task_id = str(params.get("taskId") or params.get("id") or "")
        try:
            task = _a2a_task(task_id, caller, id_)
        except store.NotFound as exc:
            return _a2a_error(req_id, -32001, str(exc), 200)
        return {"jsonrpc": "2.0", "id": req_id, "result": task}
    if method not in A2A_METHODS:
        return _a2a_error(req_id, -32601, f"method not found: {method}", 200)
    message = (req.get("params") or {}).get("message") or {}
    text = "\n".join(p["text"] for p in message.get("parts") or [] if isinstance(p, dict) and isinstance(p.get("text"), str))
    if not text.strip():
        return _a2a_error(req_id, -32602, "message has no text", 200)
    try:
        view = await run_in_threadpool(mailbox.send, caller, id_, text, message.get("contextId"))
    except (bus.Unreachable, store.NotFound) as exc:
        return _a2a_error(req_id, -32603, str(exc), 502)
    status = {"state": view["state"], "message": {"role": "ROLE_AGENT", "parts": [{"text": view["reply"]}],
                                                  "messageId": view["task_id"] or "mbx"}}
    task = {"id": view["task_id"], "contextId": view["context_id"], "status": status}
    return {"jsonrpc": "2.0", "id": req_id, "result": {"task": task} if A2A_METHODS[method] else task}


@app.get("/a2a/{id_}/.well-known/agent-card.json")
@app.get("/a2a/{id_}/.well-known/agent.json")
def a2a_mailbox_card(id_: str, request: Request) -> dict:
    """Agent Card for the mailbox. Its URL must point back here: Hermes's
    client posts to the URL a card advertises, not the one it was given.
    """
    store.get_agent(id_)
    url = f"{mesh.public_url() or str(request.base_url).rstrip('/')}/a2a/{id_}"
    return {
        "name": f"{id_} (Beacon mailbox)",
        "description": (f"Reaches {id_} through Beacon. Use it when {id_} cannot be reached directly, or for "
                        f"work that may take longer than a minute or two: the message is delivered now if "
                        f"possible, otherwise when {id_} is back online, and a reply that is not ready at once "
                        "arrives later as a new message."),
        "url": url, "version": "1.0.0",
        "supportedInterfaces": [{"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"], "skills": [],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "security": [{"bearer": []}],
    }


class InboxReport(BaseModel):
    ok: bool
    reply: str = ""
    transient: bool = False
    uncertain: bool = False
    # No answer in time, but the agent has it and will push the answer.
    working: bool = False
    error: str = ""


@app.get("/mesh/{id_}")
def mesh_config(id_: str, background: BackgroundTasks, authorization: str | None = Header(None),
                x_beacon_sync_version: str | None = Header(None),
                x_beacon_open_chat: str | None = Header(None, max_length=2048)) -> dict:
    """An agent's own A2A mesh config, pulled by its host's sync job. Outside
    /api/ on purpose: the only credential is that agent's derived pull token —
    not a session, not the MCP bearer — because the body holds tokens no one
    else may read. Checked before the agent is looked up, so a wrong token
    can't probe which agents exist. The sync reports whether the agent's chat
    is open to anyone first, so this very response already reflects it.
    """
    if not mesh.may_pull(id_, authorization):
        raise HTTPException(401, "not authorized for this agent's mesh config")
    availability.record_pull(id_)
    if x_beacon_open_chat is not None:
        mesh.record_open_chat(id_, x_beacon_open_chat)
    config = mesh.config(id_)
    if x_beacon_sync_version == "2":
        # The sync delivers held messages to the agent on its own host and
        # reports back: Beacon never has to reach the agent (pull delivery).
        config["inbox"] = mailbox.claim_for_pull(id_)
    else:
        # An older sync: the pull proves the host is up, so Beacon delivers
        # by calling the agent, after this response.
        background.add_task(mailbox.deliver_pending, id_)
    return config


@app.post("/mesh/{id_}/inbox/{message_id}")
def mesh_inbox_report(id_: str, message_id: int, body: InboxReport, background: BackgroundTasks,
                      authorization: str | None = Header(None)) -> dict:
    """The sync's result for one held message it delivered locally."""
    if not mesh.may_pull(id_, authorization):
        raise HTTPException(401, "not authorized for this agent's mesh config")
    if sender := mailbox.report(id_, message_id, body.ok, body.reply, body.transient, body.error,
                                uncertain=body.uncertain, working=body.working):
        background.add_task(mailbox.deliver_pending, sender)
    return {"ok": True}


@app.post("/a2a/{id_}/push/{message_id}")
async def a2a_push(id_: str, message_id: int, request: Request, background: BackgroundTasks,
                   k: str = "") -> dict:
    """An agent's A2A push notification: the finished task for a message the
    mailbox delivered and stopped waiting for (backend/mailbox.py). The key in
    the URL is derived per agent and message, so only the agent Beacon gave
    it to can settle that message. Answered at once; the work runs after."""
    if not hmac.compare_digest(k, mailbox.push_key(id_, message_id)):
        raise HTTPException(401, "not a push URL Beacon issued")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "body must be JSON")
    background.add_task(_settle_push, id_, message_id, payload)
    return {"ok": True}


def _settle_push(agent_id: str, message_id: int, payload: dict) -> None:
    if sender := mailbox.receive_push(agent_id, message_id, payload):
        mailbox.deliver_pending(sender)


@app.get("/api/agents/{id_}")
def get_agent(id_: str) -> dict:
    # Raw record (own overrides only) plus the template-merged view. PUT
    # sends back the raw `desired`; `effective_desired` is display-only.
    agent = store.get_agent(id_)
    return agent.model_dump() | {"effective_desired": templates.resolve(agent),
                                 "chat_open": mesh.open_chats().get(id_)}


def _access_fields(agent: Agent | None) -> tuple:
    """What on an agent record decides who may reach what: its tags (policy
    groups), who may call it, and its board access."""
    reach = agent.reach if agent else None
    return (sorted(agent.tags) if agent else [], sorted(reach.callers) if reach and reach.callers is not None else None,
            bool(reach and reach.board))


@app.put("/api/agents/{id_}")
def put_agent(id_: str, agent: Agent, request: Request) -> Agent:
    if agent.id != id_:
        raise HTTPException(400, "body id must match URL id")
    try:
        existing = store.get_agent(id_)
    except store.NotFound:
        existing = None
    # Tags, `callers`, and `board` change who may talk to whom, which is what
    # the human-only policy editor guards. Letting the LLM-facing MCP token
    # change them would be a way around it.
    if _access_fields(existing) != _access_fields(agent):
        _require_human_session(request, "set_agent_access")
    return store.upsert_agent(agent)


@app.post("/api/agents/{id_}/enroll")
def enroll_agent(id_: str, body: EnrollRequest, request: Request) -> dict:
    """A single-use code (15 minutes) and the command that fetches this agent's
    install script with it, to run on the agent's host. Human session only: the
    script holds the agent's pull token."""
    _require_human_session(request, "enroll_agent")
    store.get_agent(id_)  # 404 first
    where = f" (Docker container {body.docker!r})" if body.docker else ""
    if (gate := _confirm_gate("enroll_agent", body.confirm, f"create a one-time enrollment command for {id_!r}{where}")) is not None:
        return gate
    made = onboarding.create(id_, body.docker, body.docker_user, _board_author(request))  # ValueError -> 400
    logger.warning("enrollment code created for %s by %s", id_, _board_author(request))
    return {"agent": id_, "expires_at": made["expires_at"],
            "command": onboarding.command(made["code"], bool(body.docker), body.schedule)}


@app.get("/api/agents/{id_}/onboarding")
def agent_onboarding(id_: str) -> dict:
    """The checklist for bringing this agent into the mesh (backend/onboarding.py)."""
    return onboarding.status(id_)


@app.get("/enroll/{code}", response_class=PlainTextResponse)
def enroll_script(code: str) -> str:
    """The install script for a valid enrollment code, once. Outside /api/ on
    purpose: the host running the command has no session; the code is the
    credential. Every refusal looks the same."""
    redeemed = onboarding.redeem(code)
    if redeemed is None:
        raise HTTPException(404, "this enrollment code is not valid")
    logger.warning("enrollment code used: install script for %s fetched", redeemed[0])
    return redeemed[1]


@app.delete("/api/agents/{id_}")
def delete_agent(id_: str) -> dict:
    store.delete_agent(id_)
    return {"ok": True}


@app.get("/api/templates")
def list_templates() -> list[dict]:
    agents = store.list_agents()
    return [{"name": name, "used_by": _used_by(name, agents)} for name in templates.list_templates()]


@app.get("/api/templates/{name}")
def get_template(name: str) -> dict:
    content = templates.load_template(name)  # NotFound -> 404, bad name/keys -> 400
    return {"name": name, "content": content, "used_by": _used_by(name, store.list_agents())}


@app.put("/api/templates/{name}")
def put_template(name: str, body: TemplateBody) -> dict:
    templates.validate(name, body.content)  # before gating or writing anything
    if (gate := _confirm_gate("set_template", body.confirm, f"save template {name!r}")) is not None:
        return gate
    templates.save_template(name, body.content)
    return {"ok": True, "name": name, "content": body.content, "used_by": _used_by(name, store.list_agents())}


@app.delete("/api/templates/{name}")
def delete_template(name: str, confirm: bool = False) -> dict:
    templates.load_template(name)  # 404 if missing
    users = _used_by(name, store.list_agents())
    if users:
        raise HTTPException(409, f"template {name!r} is still used by: {', '.join(users)}")
    if (gate := _confirm_gate("set_template", confirm, f"delete template {name!r}")) is not None:
        return gate
    templates.delete_template(name)
    return {"ok": True}


@app.post("/api/templates/{name}/apply")
def apply_template(name: str, body: TemplateApplyRequest) -> dict:
    templates.load_template(name)  # validate before gating/writing anything
    desc = f"add template {name!r} to {len(body.agent_ids)} agent(s): {', '.join(body.agent_ids) or '(none)'}"
    if (gate := _confirm_gate("apply_template", body.confirm, desc)) is not None:
        return gate
    agents = [store.get_agent(aid) for aid in body.agent_ids]  # any bad id 404s before a write
    for agent in agents:
        if name not in agent.templates:
            agent.templates.append(name)
            store.upsert_agent(agent)
    return {"ok": True, "template": name, "agents": [a.id for a in agents]}


@app.get("/api/agents/{id_}/status")
def get_agent_status(id_: str) -> dict:
    try:
        agent, host, driver = for_agent(id_)
    except NoSSH as exc:
        # Not a failure: Beacon just has no way to look. The UI polls this.
        return {"state": "unknown", "detail": str(exc)}
    return driver.status(agent, host)


@app.get("/api/agents/{id_}/logs")
def get_agent_logs(id_: str, lines: int = 200) -> dict:
    agent, host, driver = for_agent(id_)
    return {"text": driver.logs(agent, host, lines=lines)}


@app.get("/api/agents/{id_}/plugins")
def get_agent_plugins(id_: str) -> list[dict]:
    agent, host, driver = for_agent(id_)
    return driver.list_plugins(agent, host)


@app.post("/api/agents/{id_}/plugins/{plugin}/update")
def update_agent_plugin(id_: str, plugin: str, confirm: bool = False) -> dict:
    if (gate := _confirm_gate("update_plugin", confirm, f"update plugin {plugin!r} on {id_!r}")) is not None:
        return gate
    agent, host, driver = for_agent(id_)
    return driver.update_plugin(agent, host, plugin)


@app.post("/api/agents/{id_}/restart")
def restart_agent(id_: str, confirm: bool = False) -> dict:
    if (gate := _confirm_gate("restart", confirm, f"restart the gateway for {id_!r}")) is not None:
        return gate
    agent, host, driver = for_agent(id_)
    return driver.restart(agent, host)


@app.post("/api/agents/{id_}/restart-agent")
def restart_whole_agent(id_: str, confirm: bool = False) -> dict:
    if (gate := _confirm_gate("restart_agent", confirm, f"hard-restart {id_!r} (container if desired.container is set, else its systemd unit)")) is not None:
        return gate
    agent, host, driver = for_agent(id_)
    return driver.restart_agent(agent, host)


@app.post("/api/agents/{id_}/deploy")
def deploy_agent(id_: str, confirm: bool = False) -> StreamingResponse:
    if (gate := _confirm_gate("deploy", confirm, f"deploy {id_!r}")) is not None:
        return JSONResponse(gate)
    agent, host, driver = for_agent(id_)
    return _stream(driver.deploy(agent, host))


@app.post("/api/agents/{id_}/update")
def update_agent(id_: str, request: Request, confirm: bool = False) -> StreamingResponse:
    _require_human_session(request, "update_agent")
    if (gate := _confirm_gate("update_agent", confirm, f"run `hermes update` on the host for {id_!r} (affects every profile sharing that install)")) is not None:
        return JSONResponse(gate)
    agent, host, driver = for_agent(id_)
    return _stream(driver.update_agent(agent, host))


@app.get("/api/agents/{id_}/reconcile")
def reconcile_agent(id_: str) -> list[dict]:
    agent, host, driver = for_agent(id_)
    return driver.reconcile(agent, host)


@app.post("/api/agents/{id_}/reconcile")
def apply_fix(id_: str, body: FixRequest) -> dict:
    if (gate := _confirm_gate("apply_fix", body.confirm, f"apply fix {body.fix!r} to {id_!r}")) is not None:
        return gate
    agent, host, driver = for_agent(id_)
    return driver.apply_fix(agent, host, body.fix)


@app.get("/api/agents/{id_}/config-diff")
def get_config_diff(id_: str) -> dict:
    agent, host, driver = for_agent(id_, resolved=True)
    target, binding = _workflow_plan_for_push(id_, agent, host, driver)
    return driver.config_diff(agent, host) | {"workflow": {"capability": "push_config", "tier": "T3", "target": target, **binding}}


@app.post("/api/agents/{id_}/config-diff")
def push_config(id_: str, request: Request, confirm: bool = False,
                workflow_request_id: int | None = None,
                plan_revision: str | None = None, plan_hash: str | None = None) -> StreamingResponse:
    if (gate := _confirm_gate("push_config", confirm, f"push desired.config to {id_!r} and restart its gateway if active")) is not None:
        return JSONResponse(gate)
    agent, host, driver = for_agent(id_, resolved=True)
    target, binding = _workflow_plan_for_push(id_, agent, host, driver)
    if plan_revision is not None or plan_hash is not None:
        if plan_revision is None or plan_hash is None:
            raise HTTPException(400, "plan_revision and plan_hash must be supplied together")
        if plan_revision != binding["plan_revision"] or plan_hash != binding["plan_hash"]:
            if workflow_request_id is not None:
                workflow.mark_stale(workflow_request_id, _workflow_actor(request), "submitted plan binding differs from live plan")
            raise workflow.StalePlan("submitted plan binding does not match the current resolved push plan")
    workflow.authorize(
        request_id=workflow_request_id, capability="push_config", tier=int(tier_for("push_config")[0]),
        actor=_workflow_actor(request), human=_workflow_human(request), target=target,
        plan_revision=binding["plan_revision"], plan_hash=binding["plan_hash"],
    )
    if workflow_request_id is None:
        raise workflow.WorkflowError("approved workflow request id is required for tier T3 push_config")
    return _workflow_stream(driver.push_config(agent, host), workflow_request_id, _workflow_actor(request))


@app.post("/api/agents/{id_}/decommission")
def decommission_agent(id_: str, body: DecommissionRequest, request: Request) -> StreamingResponse:
    _require_human_session(request, "decommission", purge=body.purge, remove_user=body.remove_user)
    gate_desc = f"decommission {id_!r}"
    if body.purge:
        gate_desc += " and purge its profile data"
    if body.remove_user:
        gate_desc += " and delete its OS user account"
    if (gate := _confirm_gate("decommission", body.confirm, gate_desc, purge=body.purge, remove_user=body.remove_user)) is not None:
        return JSONResponse(gate)
    agent, host, driver = for_agent(id_)
    filtered, exit_code = _split_exit_marker(_started(driver.decommission(agent, host, purge=body.purge, remove_user=body.remove_user)))

    def body_stream():
        for line in filtered:
            yield line + "\n"

        # The decommission script never uses `set -e` and swallows its own
        # step failures with `|| echo`, so its exit code is 0 whenever ssh
        # actually reached the host and ran it to completion — anything else
        # (255 = ssh-level failure, none = timeout/no ssh binary) means we
        # have no real confidence teardown happened, so leave the record be.
        if exit_code() == "0":
            store.archive_agent(id_)
            yield "[beacon] record archived (kept as a decommissioned agent)\n"
        else:
            yield "[beacon] teardown didn't complete cleanly — record left in place, nothing archived\n"

    return StreamingResponse(body_stream(), media_type="text/plain")


@app.get("/api/cron-jobs")
def list_cron_jobs() -> list[CronJob]:
    return cron_store.list_cron_jobs()


@app.get("/api/cron-jobs/{id_}")
def get_cron_job(id_: str) -> CronJob:
    return cron_store.get_cron_job(id_)


@app.post("/api/cron-jobs/{id_}/run")
def run_cron_job(id_: str) -> dict:
    """Manually execute a single cron job now, regardless of schedule.

    This is the GUI's "Run now" button. It bypasses the schedule but keeps
    the same driver dispatch and last-run persistence as the scheduler.
    """
    job = cron_store.get_cron_job(id_)
    return scheduler.run_job(job, datetime.datetime.now(tz=datetime.timezone.utc))


@app.post("/api/cron-jobs/{id_}/dry-run")
def dry_run_cron_job(id_: str) -> dict:
    """Report whether this cron job is due right now without executing it."""
    job = cron_store.get_cron_job(id_)
    return scheduler.preview(job, datetime.datetime.now(tz=datetime.timezone.utc))


@app.put("/api/cron-jobs/{id_}")
def put_cron_job(id_: str, job: CronJob) -> CronJob:
    if job.id != id_:
        raise HTTPException(400, "body id must match URL id")
    # Ensure every target agent exists before persisting the job.
    for aid in job.target_agent_ids:
        store.get_agent(aid)
    return cron_store.upsert_cron_job(job)


@app.delete("/api/cron-jobs/{id_}")
def delete_cron_job(id_: str) -> dict:
    cron_store.delete_cron_job(id_)
    return {"ok": True}


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# Board tools over MCP for agents and operators (backend/board_mcp.py), before
# the frontend's catch-all.
app.mount("/board", board_app)
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
