"""MCP server exposing Beacon's fleet over the Model Context Protocol.

A thin client of Beacon's own HTTP API — same contract the web GUI uses,
just a different caller. Read tools (status, logs, list, reconcile-check,
config-diff) are unrestricted: an agent asking "what's broken" is exactly
what Beacon is for. Tools that touch a host (deploy, apply_fix, push_config,
decommission) require confirm=true; called without it, they describe what
would happen instead of doing it — a forced plan-before-apply step, since a
misread instruction here is a much worse failure mode than a misclick.
"""

import os

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

BEACON_URL = os.environ.get("BEACON_URL", "http://beacon:8642")

mcp = MCPServer("beacon", instructions=__doc__)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


async def _get(path: str, **params) -> object:
    async with httpx.AsyncClient(base_url=BEACON_URL, timeout=30) as client:
        r = await client.get(path, params=params)
    return _unwrap(r)


async def _post(path: str, json: dict | None = None, timeout: float = 120) -> object:
    async with httpx.AsyncClient(base_url=BEACON_URL, timeout=timeout) as client:
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
async def get_agent(agent_id: str) -> object:
    """Get one agent's full record, including its desired.config/env_keys."""
    return await _get(f"/api/agents/{agent_id}")


@mcp.tool(annotations=READ_ONLY)
async def get_status(agent_id: str) -> object:
    """Live status of an agent: running/stopped/failed/crash-looping/not-installed, PID, uptime."""
    return await _get(f"/api/agents/{agent_id}/status")


@mcp.tool(annotations=READ_ONLY)
async def get_logs(agent_id: str, lines: int = 200) -> object:
    """Recent log tail for an agent."""
    result = await _get(f"/api/agents/{agent_id}/logs", lines=lines)
    return result["text"] if isinstance(result, dict) else result


@mcp.tool(annotations=READ_ONLY)
async def reconcile_check(agent_id: str) -> object:
    """Diagnose an agent for known drift/breakage patterns (dry run — makes no changes).
    Each finding names a `fix` id, if one exists, to pass to apply_fix."""
    return await _get(f"/api/agents/{agent_id}/reconcile")


@mcp.tool(annotations=READ_ONLY)
async def config_diff(agent_id: str) -> object:
    """Compare an agent's desired config against what's actually on the host.
    Secret values are never included — only whether an expected .env key is present."""
    return await _get(f"/api/agents/{agent_id}/config-diff")


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
        agent = await _get(f"/api/agents/{agent_id}")
        mode = agent.get("desired", {}).get("install_mode", "simple") if isinstance(agent, dict) else "?"
        return f"Would deploy {agent_id!r} (install_mode={mode}). Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{agent_id}/deploy")


@mcp.tool(annotations=DESTRUCTIVE)
async def apply_fix(agent_id: str, fix: str, confirm: bool = False) -> object:
    """Apply one fix named by reconcile_check's findings (e.g. "restart-failed",
    "uninstall-orphan"). Requires confirm=true."""
    if not confirm:
        return f"Would apply fix {fix!r} to {agent_id!r}. Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{agent_id}/reconcile", json={"fix": fix})


@mcp.tool(annotations=DESTRUCTIVE)
async def push_config(agent_id: str, confirm: bool = False) -> str:
    """Push every key declared in desired.config to the host via `hermes config set`,
    then restart the gateway if it's active. Requires confirm=true."""
    if not confirm:
        return f"Would push desired.config to {agent_id!r} and restart its gateway if active. Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{agent_id}/config-diff")


@mcp.tool(annotations=DESTRUCTIVE)
async def decommission(agent_id: str, purge: bool = False, remove_user: bool = False, confirm: bool = False) -> str:
    """Stop and uninstall an agent's gateway service, archiving its Beacon record.
    purge also deletes its profile data (memory, sessions, skills) — refused for
    the default profile. remove_user also deletes the OS account (new-user-mode
    agents only). Requires confirm=true."""
    if not confirm:
        extra = []
        if purge:
            extra.append("purge its profile data")
        if remove_user:
            extra.append("delete its OS user account")
        detail = f" and {', '.join(extra)}" if extra else ""
        return f"Would decommission {agent_id!r}{detail}. Call again with confirm=true to actually run it."
    return await _post(f"/api/agents/{agent_id}/decommission", json={"purge": purge, "remove_user": remove_user})


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8643)
