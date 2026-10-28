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
the same as down. No probe and no recent pull is `offline`. Agents without
a reach block are `no-a2a`. Observation only: nothing here is on the
message path, and a Beacon outage changes nothing for the agents.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import bus, opstore
from .schemas import Agent

PROBE_TIMEOUT = httpx.Timeout(4, connect=3)
# The sync runs every 5 minutes; three missed runs make the host stale.
SYNC_FRESH_SECONDS = 15 * 60
MAX_PROBES = 16


def record_pull(agent_id: str) -> None:
    with opstore.connect() as conn:
        conn.execute("INSERT INTO agent_seen (agent, last_pull, pulls) VALUES (?, ?, 1) "
                     "ON CONFLICT (agent) DO UPDATE SET last_pull = excluded.last_pull, pulls = pulls + 1",
                     (agent_id, time.time()))


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


def classify(agent: Agent, answered: bool | None, detail: str, pulled: float | None, now: float) -> dict:
    age = None if pulled is None else max(0, int(now - pulled))
    base = {"last_pull_seconds_ago": age}
    if agent.reach is None:
        return {**base, "state": "no-a2a", "detail": "no reach block: Beacon has no A2A endpoint for this agent"}
    if answered:
        return {**base, "state": "online", "detail": ""}
    if age is not None and age <= SYNC_FRESH_SECONDS:
        return {**base, "state": "sync-only",
                "detail": f"Beacon cannot reach {agent.reach.endpoint} ({detail}); its host synced {age // 60} min ago"}
    seen = "never synced" if age is None else f"last sync {age // 60} min ago"
    return {**base, "state": "offline", "detail": f"no answer from {agent.reach.endpoint} ({detail}); {seen}"}


def check_all(agents: list[Agent]) -> dict[str, dict]:
    """agent id -> availability, probing in parallel so one dead endpoint
    costs one timeout, not one per agent."""
    now, pulls = time.time(), last_pulls()
    reachable = [a for a in agents if a.reach is not None]
    with ThreadPoolExecutor(max_workers=min(MAX_PROBES, len(reachable) or 1)) as pool:
        results = dict(zip((a.id for a in reachable), pool.map(probe, reachable)))
    return {a.id: classify(a, *results.get(a.id, (None, "")), pulls.get(a.id), now) for a in agents}
