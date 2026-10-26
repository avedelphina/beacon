"""MCP server exposing Beacon's fleet over the Model Context Protocol.

A thin client of Beacon's own HTTP API — same contract the web GUI uses,
just a different caller. Read tools (status, logs, list, reconcile-check,
config-diff) are unrestricted: an agent asking "what's broken" is exactly
what Beacon is for. T4/T5 actions that require human approval are deliberately
not exposed through MCP; use the authenticated browser UI until Beacon has a
real out-of-band approval mechanism.
"""

import os
import re
from urllib.parse import quote

import httpx
from auth import is_valid_bearer
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

BEACON_URL = os.environ.get("BEACON_URL", "http://beacon:8642")
# A static shared-secret bearer token, not OIDC — MCP clients are typically
# configured with a fixed API key, not walked through a browser login.
# Unset, this runs open (same opt-in-via-env convention as backend/auth.py).
BEACON_MCP_TOKEN = os.environ.get("BEACON_MCP_TOKEN")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PLUGIN_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _path_identifier(value: str, pattern: re.Pattern[str], kind: str) -> str:
    """Validate then encode an MCP-supplied API path component."""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{kind} {value!r} must match {pattern.pattern}")
    return quote(value, safe="")


def _agent_path_id(agent_id: str) -> str:
    return _path_identifier(agent_id, ID_RE, "agent id")


def _template_path_name(name: str) -> str:
    return _path_identifier(name, ID_RE, "template name")


def _plugin_path_name(plugin: str) -> str:
    return _path_identifier(plugin, PLUGIN_NAME_RE, "plugin name")


mcp = MCPServer("beacon", instructions=__doc__)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


# Same token as BearerAuthMiddleware requires of MCP's own callers, reused
# here as MCP's credential when it in turn calls Beacon — Beacon's own
# AuthMiddleware accepts it exactly the same way (see backend/auth.py).
_BEACON_HEADERS = {"Authorization": f"Bearer {BEACON_MCP_TOKEN}"} if BEACON_MCP_TOKEN else {}


async def _get(path: str, **params) -> object:
    async with httpx.AsyncClient(base_url=BEACON_URL, timeout=30, headers=_BEACON_HEADERS) as client:
        r = await client.get(path, params=params)
    return _unwrap(r)


async def _post(path: str, json: dict | None = None, timeout: float = 120) -> object:
    async with httpx.AsyncClient(base_url=BEACON_URL, timeout=timeout, headers=_BEACON_HEADERS) as client:
        r = await client.post(path, json=json or {})
    return _unwrap(r)


def _unwrap(r: httpx.Response) -> object:
    if r.is_success:
        ctype = r.headers.get("content-type", "")
        return r.json() if "application/json" in ctype else r.text
    try:
        detail = r.json().get("detail", r.text)
    except Exception:
        detail = r.text
    return f"[error {r.status_code}] {detail}"


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def list_hosts() -> object:
    """List every host in the fleet registry (id, address, ssh config, tags)."""
    return await _get("/api/hosts")


@mcp.tool(annotations=READ_ONLY)
async def list_agents() -> object:
    """List every agent in the fleet registry (id, type, host, profile, desired state)."""
    return await _get("/api/agents")


@mcp.tool(annotations=READ_ONLY)
async def agent_registry() -> object:
    """Who can be called through the agent bus, and the capabilities each one
    declares. Does not include endpoints or credentials — call_agent names an
    agent, it does not learn how to reach it."""
    return await _get("/api/agents/registry")


@mcp.tool()
async def call_agent(agent_id: str, message: str, context_id: str | None = None,
                     queue_if_offline: bool = False) -> object:
    """Send one message to a registered agent over A2A and return its reply,
    task state, and context_id. Pass a previous reply's context_id to continue
    that thread; omit it to start a new one. The agent may take minutes to
    answer. Agents without a reach block are reported as not callable.
    Use queue_if_offline for anything that may take more than a minute or
    two, or for an agent that may be offline: the message goes through the
    Beacon mailbox, which returns at once with a task id (mbx-N) when the
    answer is not ready (state WORKING) or the agent is unreachable
    (SUBMITTED). Read the answer later with `mailbox_task`."""
    body = {"message": message}
    if context_id:
        body["context_id"] = context_id
    if queue_if_offline:
        body["queue_if_offline"] = True
    # Beacon waits up to 330s on the agent; outlast it so its verdict arrives.
    return await _post(f"/api/agents/{_agent_path_id(agent_id)}/call", json=body, timeout=360)


@mcp.tool(annotations=READ_ONLY)
async def mailbox(limit: int = 20) -> object:
    """Recent Beacon mailbox traffic, newest first: messages held for offline
    agents, their delivery state, and replies to messages you queued with
    call_agent(queue_if_offline=True) — those have recipient "beacon"."""
    return await _get("/api/mailbox", limit=limit)


@mcp.tool(annotations=READ_ONLY)
async def mailbox_task(task_id: str) -> object:
    """Get a durable mailbox task by id, such as ``mbx-12``. Returns the
    A2A state plus Beacon delivery details and any late reply."""
    return await _get(f"/api/mailbox/tasks/{_path_identifier(task_id, re.compile(r'^mbx-[0-9]+$'), 'task id')}")


@mcp.tool(annotations=READ_ONLY)
async def get_policy() -> object:
    """The access policy: the file's text, its problems, and the who-may-call-whom
    table. Changing it is not an MCP action: an operator edits it in the Policy tab."""
    return await _get("/api/policy")


@mcp.tool(annotations=READ_ONLY)
async def preview_policy(text: str) -> object:
    """Check a proposed fleet/policy.yaml without saving it: validation errors,
    likely mistakes, the new table, which calls it adds or removes, and which
    agents would restart. Read-only; saving needs a human in the browser."""
    return await _post("/api/policy/preview", json={"text": text})


@mcp.tool(annotations=READ_ONLY)
async def get_agent(agent_id: str) -> object:
    """Get one agent's full record, including its desired.config/env_keys."""
    return await _get(f"/api/agents/{_agent_path_id(agent_id)}")


@mcp.tool(annotations=READ_ONLY)
async def get_status(agent_id: str) -> object:
    """Live status of an agent: running/stopped/failed/crash-looping/not-installed, PID, uptime."""
    return await _get(f"/api/agents/{_agent_path_id(agent_id)}/status")


@mcp.tool(annotations=READ_ONLY)
async def get_logs(agent_id: str, lines: int = 200) -> object:
    """Recent log tail for an agent."""
    result = await _get(f"/api/agents/{_agent_path_id(agent_id)}/logs", lines=lines)
    return result["text"] if isinstance(result, dict) else result


@mcp.tool(annotations=READ_ONLY)
async def reconcile_check(agent_id: str) -> object:
    """Diagnose an agent for known drift/breakage patterns (dry run — makes no changes).
    Each finding names a `fix` id, if one exists, to pass to apply_fix."""
    return await _get(f"/api/agents/{_agent_path_id(agent_id)}/reconcile")


@mcp.tool(annotations=READ_ONLY)
async def config_diff(agent_id: str) -> object:
    """Compare an agent's desired config against what's actually on the host.
    Secret values are never included — only whether an expected .env key is present."""
    return await _get(f"/api/agents/{_agent_path_id(agent_id)}/config-diff")


@mcp.tool(annotations=READ_ONLY)
async def list_workflows(include_done: bool = False) -> object:
    """List decision, review, and approval requests visible to this MCP caller."""
    return await _get("/api/workflows", include_done=str(include_done).lower())


@mcp.tool(annotations=READ_ONLY)
async def get_workflow(request_id: int) -> object:
    """Get one workflow request, including its plan binding and event history."""
    return await _get(f"/api/workflows/{request_id}")


@mcp.tool(annotations=DESTRUCTIVE)
async def respond_workflow(request_id: int, action: str, plan_revision: str,
                          plan_hash: str, response: object = None) -> object:
    """Respond to an eligible workflow request. Human-only T4/T5 responses
    remain restricted to the authenticated browser UI.
    """
    return await _post(f"/api/workflows/{request_id}/respond", json={
        "action": action, "response": response, "plan_revision": plan_revision, "plan_hash": plan_hash,
    })


@mcp.tool()
async def create_workflow(card_id: int, kind: str, eligible_responder: str,
                          capability: str, tier: int, title: str, summary: str,
                          plan_revision: str, plan_hash: str, options: list[str] | None = None,
                          target: dict | None = None, expires_at: float | None = None) -> object:
    """Create a pending decision, review, or approval request on a board card.
    The request is attributed to Beacon's MCP service identity; participants and
    tier rules are enforced by the Beacon API.
    """
    body = {"kind": kind, "eligible_responder": eligible_responder, "capability": capability,
            "tier": tier, "title": title, "summary": summary, "plan_revision": plan_revision,
            "plan_hash": plan_hash}
    if options is not None:
        body["options"] = options
    if target is not None:
        body["target"] = target
    if expires_at is not None:
        body["expires_at"] = expires_at
    return await _post(f"/api/board/cards/{card_id}/workflows", json=body)


@mcp.tool(annotations=READ_ONLY)
async def list_templates() -> object:
    """Config templates (fleet/templates/*.yaml) — shared `config`/`env_keys`
    fragments merged under many agents' `desired`. Each entry lists the agent
    ids currently using it. Pass a name to get_template for its content."""
    return await _get("/api/templates")


@mcp.tool(annotations=READ_ONLY)
async def get_template(name: str) -> object:
    """One config template's content plus the agent ids using it."""
    return await _get(f"/api/templates/{_template_path_name(name)}")


@mcp.tool(annotations=READ_ONLY)
async def list_tiers() -> object:
    """The tier registry: every gated capability, its autonomy tier (T0-T5),
    and the rationale for that assignment. Beacon enforces the confirm=true
    step for T2+ capabilities today; T3+ (review) and T4/T5 (human approval)
    are named here but not yet mechanically enforced beyond that."""
    return await _get("/api/tiers")


# ---------------------------------------------------------------------------
# Mutating — confirm=True required, otherwise describes the action instead
# of taking it.
# ---------------------------------------------------------------------------


@mcp.tool(annotations=DESTRUCTIVE)
async def deploy(agent_id: str, confirm: bool = False) -> str:
    """Install/bring up an agent's gateway service on its host. Runs installer
    commands, may create profiles or OS users depending on desired.install_mode.
    Requires confirm=true; without it, returns what would run instead of running it."""
    if not confirm:
        agent = await _get(f"/api/agents/{_agent_path_id(agent_id)}")
        mode = agent.get("desired", {}).get("install_mode", "simple") if isinstance(agent, dict) else "?"
        return f"Would deploy {agent_id!r} (install_mode={mode}). Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{_agent_path_id(agent_id)}/deploy?confirm=true")


@mcp.tool(annotations=DESTRUCTIVE)
async def apply_fix(agent_id: str, fix: str, confirm: bool = False) -> object:
    """Apply one fix named by reconcile_check's findings (e.g. "restart-failed",
    "uninstall-orphan"). Requires confirm=true."""
    if not confirm:
        return f"Would apply fix {fix!r} to {agent_id!r}. Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{_agent_path_id(agent_id)}/reconcile", json={"fix": fix, "confirm": True})


@mcp.tool(annotations=DESTRUCTIVE)
async def push_config(agent_id: str, confirm: bool = False,
                      workflow_request_id: int | None = None,
                      plan_revision: str | None = None,
                      plan_hash: str | None = None) -> str:
    """Push the exact approved config plan to the host. Requires confirm=true
    plus the workflow request id and plan binding returned by config_diff."""
    if not confirm:
        return f"Would push desired.config to {agent_id!r} and restart its gateway if active. Call again with confirm=true and workflow_request_id/plan_revision/plan_hash to actually run it."
    params = {
        "confirm": "true",
        "workflow_request_id": workflow_request_id,
        "plan_revision": plan_revision,
        "plan_hash": plan_hash,
    }
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items() if value is not None)
    return await _post(f"/api/agents/{_agent_path_id(agent_id)}/config-diff?{query}")


@mcp.tool(annotations=DESTRUCTIVE)
async def apply_template(name: str, agent_ids: list[str], confirm: bool = False) -> object:
    """Add config template `name` to each agent in `agent_ids` (fleet YAML
    only — touches no host; the config it implies still goes through
    push_config's own gate). Requires confirm=true."""
    if not confirm:
        return f"Would add template {name!r} to {len(agent_ids)} agent(s): {', '.join(agent_ids) or '(none)'}. Call again with confirm=true to actually run it."
    return await _post(f"/api/templates/{_template_path_name(name)}/apply", json={"agent_ids": agent_ids, "confirm": True})


@mcp.tool(annotations=READ_ONLY)
async def list_plugins(agent_id: str) -> object:
    """List an agent's installed plugins — name, version, enabled/disabled, source."""
    return await _get(f"/api/agents/{_agent_path_id(agent_id)}/plugins")


@mcp.tool(annotations=DESTRUCTIVE)
async def restart_agent(agent_id: str, confirm: bool = False) -> object:
    """Hard-restart a whole agent: its Docker container if it has one, else its systemd unit
    (no graceful drain). Drops live sessions; prefer `restart` first. Requires confirm=true."""
    if not confirm:
        return f"Would hard-restart {agent_id!r}. Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{_agent_path_id(agent_id)}/restart-agent?confirm=true")


@mcp.tool(annotations=DESTRUCTIVE)
async def restart(agent_id: str, confirm: bool = False) -> object:
    """Restart an agent's gateway service. Requires confirm=true."""
    if not confirm:
        return f"Would restart {agent_id!r}. Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{_agent_path_id(agent_id)}/restart?confirm=true")


@mcp.tool(annotations=DESTRUCTIVE)
async def update_plugin(agent_id: str, plugin: str, confirm: bool = False) -> object:
    """Update one installed plugin (git pull) by name — see list_plugins for names.
    Requires confirm=true."""
    if not confirm:
        return f"Would update plugin {plugin!r} on {agent_id!r}. Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{_agent_path_id(agent_id)}/plugins/{_plugin_path_name(plugin)}/update?confirm=true")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not BEACON_MCP_TOKEN:
            return await call_next(request)
        if not is_valid_bearer(request.headers.get("authorization"), BEACON_MCP_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    import uvicorn

    if not BEACON_MCP_TOKEN:
        print("[mcp] WARNING: BEACON_MCP_TOKEN not set — running with no auth", flush=True)

    http_app = mcp.streamable_http_app(host="0.0.0.0")
    http_app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(http_app, host="0.0.0.0", port=8643)
