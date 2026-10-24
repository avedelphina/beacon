import contextlib
import datetime
import logging
import itertools
from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import auth, board, bus, cron_store, mailbox, mesh, policy, scheduler, store, templates
from .board_mcp import board_app
from .drivers import for_agent
from .schemas import Agent, CronJob, Host
from .tiers import TIER_LABELS, CAPABILITY_TIERS, requires_confirm, tier_for


class FixRequest(BaseModel):
    fix: str
    confirm: bool = False


class TemplateApplyRequest(BaseModel):
    agent_ids: list[str]
    confirm: bool = False


class DecommissionRequest(BaseModel):
    purge: bool = False
    remove_user: bool = False
    confirm: bool = False


class CallAgentRequest(BaseModel):
    message: str
    context_id: str | None = None
    # Agent offline? Leave it in the Beacon mailbox instead of failing; the
    # reply turns up in GET /api/mailbox once the agent is back.
    queue_if_offline: bool = False

    @field_validator("message")
    @classmethod
    def _message_present(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty")
        return v

logger = logging.getLogger("beacon")

try:
    _fleet_problems = policy.problems(policy.load(), store.list_agents())
except Exception as exc:  # an invalid policy file must not stop Beacon from starting
    _fleet_problems = [f"fleet/policy.yaml is invalid: {exc}"]
for _problem in _fleet_problems:
    logger.warning("access policy: %s", _problem)


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with board_app.lifespan():
        yield


app = FastAPI(title="Beacon", version="0.18.0", lifespan=lifespan)


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


def _started(lines: Iterator[str]) -> Iterator[str]:
    """Pull the first chunk here, outside the StreamingResponse body, so a
    driver's validation error (e.g. bad install_mode, purge on the default
    profile) raises synchronously and comes back as a clean 400 instead of
    surfacing mid-stream after a 200 already went out.
    """
    try:
        first = next(lines)
    except StopIteration:
        return iter(())
    return itertools.chain([first], lines)


def _split_exit_marker(lines: Iterator[str]) -> tuple[Iterator[str], Callable[[], str | None]]:
    """ssh.stream_script always ends its stream with a `__BEACON_EXIT__<code>`
    line — internal bookkeeping, not output for a human. Returns the stream
    with that line removed, plus a getter for the code it carried (None
    until the returned iterator has been fully consumed).
    """
    code = None

    def filtered() -> Iterator[str]:
        nonlocal code
        for line in lines:
            if line.startswith("__BEACON_EXIT__"):
                code = line.removeprefix("__BEACON_EXIT__")
            else:
                yield line

    return filtered(), lambda: code


def _stream(lines: Iterator[str]) -> StreamingResponse:
    filtered, _ = _split_exit_marker(_started(lines))
    return StreamingResponse((line + "\n" for line in filtered), media_type="text/plain")


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
def list_agents() -> list[Agent]:
    return store.list_agents()


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
    """Replace fleet/policy.yaml with the edited text. A human browser session
    only (T4), asks for confirmation first, refuses invalid text and a file
    that changed since the editor loaded it, and keeps the old version in
    fleet/policy-history/."""
    _require_human_session(request, "set_policy")
    checked = policy.preview(body.text, store.list_agents())
    if checked["errors"]:
        raise HTTPException(422, "; ".join(checked["errors"]))
    if policy.text_hash(policy.read_text()) != body.base_hash:
        raise HTTPException(409, "fleet/policy.yaml changed since you opened it; reload the editor and redo your edit")
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
    agent = store.get_agent(id_)
    try:
        return {"agent": id_, **bus.call(agent, body.message, body.context_id)}
    except bus.Unreachable as exc:
        if not (exc.transient and body.queue_if_offline):
            raise
    message_id = mailbox.enqueue(mesh.BEACON_PEER, id_, "message", body.message, body.context_id)
    return {"agent": id_, "task_id": f"mbx-{message_id}", "context_id": body.context_id,
            "state": "TASK_STATE_SUBMITTED", "reply": "", "queued": message_id}


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


@app.get("/api/mailbox")
def mailbox_recent(limit: int = 50) -> list[dict]:
    """What the mailbox holds, newest first: queued, delivered, and replies
    addressed to Beacon (recipient "beacon") — the voicemail MCP callers left.
    """
    return mailbox.recent(min(max(limit, 1), 500))


A2A_METHODS = {"SendMessage": True, "message/send": False}  # name -> answers in the v1.0 {"task": ...} wrapper


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
        "description": (f"Reaches {id_} through Beacon. Use it when {id_} cannot be reached directly: the "
                        f"message is delivered now if possible, otherwise when {id_} is back online, and the "
                        "reply then arrives later as a new message."),
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
    error: str = ""


@app.get("/mesh/{id_}")
def mesh_config(id_: str, background: BackgroundTasks, authorization: str | None = Header(None),
                x_beacon_sync_version: str | None = Header(None)) -> dict:
    """An agent's own A2A mesh config, pulled by its host's sync job. Outside
    /api/ on purpose: the only credential is that agent's derived pull token —
    not a session, not the MCP bearer — because the body holds tokens no one
    else may read. Checked before the agent is looked up, so a wrong token
    can't probe which agents exist.
    """
    if not mesh.may_pull(id_, authorization):
        raise HTTPException(401, "not authorized for this agent's mesh config")
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
    if sender := mailbox.report(id_, message_id, body.ok, body.reply, body.transient, body.error):
        background.add_task(mailbox.deliver_pending, sender)
    return {"ok": True}


@app.get("/api/agents/{id_}")
def get_agent(id_: str) -> dict:
    # Raw record (own overrides only) plus the template-merged view. PUT
    # sends back the raw `desired`; `effective_desired` is display-only.
    agent = store.get_agent(id_)
    return agent.model_dump() | {"effective_desired": templates.resolve(agent)}


@app.put("/api/agents/{id_}")
def put_agent(id_: str, agent: Agent) -> Agent:
    if agent.id != id_:
        raise HTTPException(400, "body id must match URL id")
    return store.upsert_agent(agent)


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
    agent, host, driver = for_agent(id_)
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
    return driver.config_diff(agent, host)


@app.post("/api/agents/{id_}/config-diff")
def push_config(id_: str, confirm: bool = False) -> StreamingResponse:
    if (gate := _confirm_gate("push_config", confirm, f"push desired.config to {id_!r} and restart its gateway if active")) is not None:
        return JSONResponse(gate)
    agent, host, driver = for_agent(id_, resolved=True)
    return _stream(driver.push_config(agent, host))


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
            yield "[beacon] record archived to fleet/decommissioned/\n"
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
