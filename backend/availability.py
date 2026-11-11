"""Is the agent there? Answered over A2A, not SSH.

Whether an agent can be talked to is what the mesh cares about, and SSH
says nothing about that: a host Beacon has no account on, or no route to,
is a perfectly good A2A peer. Two independent signals:

- a probe: Beacon GETs the agent's `/health` (Hermes serves it without a
  token, on the same port as A2A), which says its A2A server answers;
- a heartbeat: every pull of /mesh/{agent} from the agent's host sync is
  recorded, which says the host and its sync are alive even where Beacon
  has no route to the agent (NAT, another network, a laptop away from home).

`online` needs the probe. A probe that fails while the sync still pulls is
`sync-only`: the host is up and Beacon cannot see the agent, which is not
the same as down. An agent that has not synced for a while but is still
within its `quiet_after` (fleet policy `health`; a laptop may sleep for days)
is `away`; past it, `offline`. Agents without a reach block are `no-a2a`.
Observation only: nothing here is on the message path, and a Beacon outage
changes nothing for the agents.

Self-healing happens on the agent's host, where the sync probes the agent
itself and restarts a gateway that stopped answering (only where the policy
says `heal`). What it found arrives with each pull and is kept here, every
change in `health_events`. Beacon's part is telling people: when healing
gave up or recovered, and when an agent went quiet. The quiet check rides on
other agents' pulls, so it needs no timer: while any agent still syncs,
Beacon notices the ones that stopped.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import bus, opstore
from .schemas import Agent

HEALTH_STATES = ("ok", "failing", "healing", "gave-up", "network")

PROBE_TIMEOUT = httpx.Timeout(4, connect=3)
# The sync runs every 5 minutes; three missed runs make the host stale.
SYNC_FRESH_SECONDS = 15 * 60
MAX_PROBES = 16


def record_pull(agent_id: str) -> bool:
    """Note the pull. True when people had been told this agent went quiet:
    it is back, and they should hear that too (only once)."""
    with opstore.connect() as conn:
        conn.execute("INSERT INTO agent_seen (agent, last_pull, pulls) VALUES (?, ?, 1) "
                     "ON CONFLICT (agent) DO UPDATE SET last_pull = excluded.last_pull, pulls = pulls + 1",
                     (agent_id, time.time()))
        return conn.execute("UPDATE agent_seen SET quiet_notified = 0 WHERE agent = ? AND quiet_notified = 1",
                            (agent_id,)).rowcount == 1


def record_sync_hash(agent_id: str, sync_hash: str) -> None:
    """Which copy of the sync script the host runs (it reports its own checksum)."""
    with opstore.connect() as conn:
        conn.execute("UPDATE agent_seen SET sync_hash = ? WHERE agent = ?", (sync_hash[:64], agent_id))


def sync_status(agent: Agent, reported: str | None) -> str:
    """`current` or `outdated` against the copy Beacon serves; `unknown` for a
    sync too old to report, or an agent type with no script."""
    from . import mesh
    served = mesh.sync_hash(agent)
    if not reported or served is None:
        return "unknown"
    return "current" if reported == served else "outdated"


def record_health(agent_id: str, state: str, detail: str) -> str | None:
    """Keep what the host sync found; log a change. Returns the previous state
    when it changed, "" for the first report, None when nothing changed."""
    if state not in HEALTH_STATES:
        return None
    detail = (detail or "")[:300]
    with opstore.connect() as conn:
        row = conn.execute("SELECT health FROM agent_seen WHERE agent = ?", (agent_id,)).fetchone()
        previous = row["health"] if row else None
        if previous == state:
            return None
        conn.execute("UPDATE agent_seen SET health = ?, health_detail = ? WHERE agent = ?", (state, detail, agent_id))
        conn.execute("INSERT INTO health_events (agent, state, detail, at) VALUES (?, ?, ?, ?)",
                     (agent_id, state, detail, time.time()))
    return previous or ""


def health_events(agent_id: str, limit: int = 50) -> list[dict]:
    with opstore.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT state, detail, at FROM health_events WHERE agent = ? ORDER BY id DESC LIMIT ?", (agent_id, limit))]


def went_quiet(agents: list[Agent], now: float | None = None) -> list[tuple[Agent, int]]:
    """Agents silent for longer than their `quiet_after` that nobody was told
    about yet, each claimed once (two pulls at the same moment won't both
    report it). Agents that never synced are not counted: they never started."""
    from . import policy
    now = time.time() if now is None else now
    rules, pulls = policy.load(), last_pulls()
    found = []
    for agent in agents:
        if agent.reach is None or agent.id not in pulls:
            continue
        quiet_after = policy.health_for(rules, agent)["quiet_after"]
        age = int(now - pulls[agent.id])
        if quiet_after is None or age <= quiet_after:
            continue
        with opstore.connect() as conn:
            if conn.execute("UPDATE agent_seen SET quiet_notified = 1 WHERE agent = ? AND quiet_notified = 0",
                            (agent.id,)).rowcount == 1:
                found.append((agent, age))
    return found


def record_restart(agent_id: str, pending: bool) -> None:
    """What the host sync says about its gateway restart (it asks for one when
    the listen address or a token changes, and retries until Hermes allows it).
    The agent keeps its old address until then, which looks like a dead agent."""
    with opstore.connect() as conn:
        conn.execute("UPDATE agent_seen SET restart_pending = ? WHERE agent = ?", (int(pending), agent_id))


def restart_pending() -> set[str]:
    with opstore.connect() as conn:
        return {r["agent"] for r in conn.execute("SELECT agent FROM agent_seen WHERE restart_pending = 1")}


def last_pulls() -> dict[str, float]:
    with opstore.connect() as conn:
        return {r["agent"]: r["last_pull"] for r in conn.execute("SELECT agent, last_pull FROM agent_seen")}


def probe(agent: Agent) -> tuple[bool, str]:
    """(answered, detail): GET <endpoint>/health. Redirects are not followed."""
    url = agent.reach.endpoint.rstrip("/") + "/health"
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT, verify=bus.tls()) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        return False, str(exc) or type(exc).__name__
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return False, "not an A2A health answer"
    return (True, "") if isinstance(body, dict) and body.get("status") == "ok" else (False, "not an A2A health answer")


RESTART_HINT = (" Its gateway restart is still pending, and until it happens the agent listens on its old address: "
                "the sync retries it on every run, or run `hermes gateway restart` on the host.")


def classify(agent: Agent, answered: bool | None, detail: str, pulled: float | None, now: float,
             restarting: bool = False, quiet_after: int | None = 30 * 60,
             health: tuple[str | None, str | None] = (None, None)) -> dict:
    result = _classify(agent, answered, detail, pulled, now, quiet_after)
    if restarting and result["state"] in ("sync-only", "offline", "away"):
        result["detail"] += RESTART_HINT
    state, health_detail = health
    result["health"] = state
    if state in ("failing", "healing", "gave-up"):
        result["detail"] = (result["detail"].rstrip(".") + ". " if result["detail"] else "") + \
            f"Its host reports: {state}" + (f" ({health_detail})" if health_detail else "") + "."
    return result


def _classify(agent: Agent, answered: bool | None, detail: str, pulled: float | None, now: float,
              quiet_after: int | None) -> dict:
    age = None if pulled is None else max(0, int(now - pulled))
    base = {"last_pull_seconds_ago": age}
    if agent.reach is None:
        return {**base, "state": "no-a2a", "detail": "no reach block: Beacon has no A2A endpoint for this agent"}
    if answered:
        return {**base, "state": "online", "detail": ""}
    if age is not None and age <= SYNC_FRESH_SECONDS:
        return {**base, "state": "sync-only",
                "detail": f"Beacon cannot reach {agent.reach.endpoint} ({detail}); its host synced {age // 60} min ago"}
    seen = "never synced" if age is None else f"last sync {_ago(age)}"
    if quiet_after is None or (age is not None and age <= quiet_after):
        return {**base, "state": "away", "detail": f"not answering, which is expected for now; {seen}"}
    return {**base, "state": "offline", "detail": f"no answer from {agent.reach.endpoint} ({detail}); {seen}"}


def _ago(seconds: int) -> str:
    if seconds < 2 * 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago" if seconds < 2 * 86400 else f"{seconds // 86400} days ago"


def health_reports() -> dict[str, tuple[str | None, str | None]]:
    with opstore.connect() as conn:
        return {r["agent"]: (r["health"], r["health_detail"])
                for r in conn.execute("SELECT agent, health, health_detail FROM agent_seen")}


def check_all(agents: list[Agent]) -> dict[str, dict]:
    """agent id -> availability, probing in parallel so one dead endpoint
    costs one timeout, not one per agent."""
    from . import policy
    now, pulls, restarting, reports, rules = time.time(), last_pulls(), restart_pending(), health_reports(), policy.load()
    reachable = [a for a in agents if a.reach is not None]
    with ThreadPoolExecutor(max_workers=min(MAX_PROBES, len(reachable) or 1)) as pool:
        results = dict(zip((a.id for a in reachable), pool.map(probe, reachable)))
    with opstore.connect() as conn:
        hashes = {r["agent"]: r["sync_hash"] for r in conn.execute("SELECT agent, sync_hash FROM agent_seen")}
    return {a.id: classify(a, *results.get(a.id, (None, "")), pulls.get(a.id), now, a.id in restarting,
                           policy.health_for(rules, a)["quiet_after"], reports.get(a.id, (None, None)))
            | {"sync": sync_status(a, hashes.get(a.id)) if a.reach else "unknown"}
            for a in agents}
