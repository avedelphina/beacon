"""A2A mesh: which agents call each other directly, and with which token.

Tailscale-style: agents that declare the same `reach.network` call each
other directly, and Beacon only acts as the coordination server that hands
out peer lists. Each agent's host pulls its own config (GET /mesh/{agent})
every few minutes and applies it, so Beacon is never on the message path:
a Beacon outage leaves every agent on its last config, still talking. An
agent without a network is reachable only through Beacon.

Tokens are pairwise and derived — HMAC(BEACON_A2A_SECRET, "caller>target")
— so Beacon keeps one secret instead of N² stored tokens. A host that leaks
its tokens exposes only calls into itself, never another pair. An agent
pulls its config with token("<agent>", "beacon"), which Beacon recomputes
rather than stores. Rotating the secret rotates every token at once and
cuts every agent off from pulling; re-run `install` for each agent.

CLI (runs where Beacon's env is; the output contains a token):
    python -m backend.mesh install <agent-id>   # one-time host bootstrap script
    python -m backend.mesh install <agent-id> --docker <container>   # same, run on the container's host as root
"""

import argparse
import hashlib
import hmac
import os
import sys
from urllib.parse import urlsplit

from . import store
from .drivers import get_driver
from .schemas import Agent

SECRET_ENV = "BEACON_A2A_SECRET"
# The URL agents' hosts reach Beacon on for pulls, e.g. http://100.64.0.1:8642.
PUBLIC_URL_ENV = "BEACON_PUBLIC_URL"
BEACON_PEER = "beacon"


def public_url() -> str:
    """Beacon's URL as agents' hosts see it, or "" when not configured."""
    return os.environ.get(PUBLIC_URL_ENV, "").rstrip("/")


class MissingSecret(RuntimeError):
    """No mesh secret on this Beacon process, so no token can be derived."""


def token(caller: str, target: str) -> str:
    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise MissingSecret(f"{SECRET_ENV} is not set on this Beacon process")
    # Hex, not base64: a token passed as a CLI argument must never start with "-".
    return hmac.new(secret.encode(), f"{caller}>{target}".encode(), hashlib.sha256).hexdigest()


def may_pull(agent_id: str, authorization: str | None) -> bool:
    """True only for the agent's own pull token — the config holds secrets."""
    expected = f"Bearer {token(agent_id, BEACON_PEER)}"
    return isinstance(authorization, str) and hmac.compare_digest(authorization, expected)


def may_call(caller: str, target: Agent) -> bool:
    """The access policy, in one place: Beacon (operators) always; otherwise
    the target's `reach.callers` if it has one, else anyone on its network.
    Applied to direct calls (the target's inbound tokens), the mailbox, and
    board assignments, so no path routes around it."""
    if caller == BEACON_PEER:
        return True
    callers = target.reach.callers if target.reach else None
    return callers is None or caller in callers


def peers(agent: Agent, fleet: list[Agent]) -> list[Agent]:
    """Agents `agent` calls directly: same declared network, reachable, not itself."""
    network = agent.reach.network if agent.reach else None
    if network is None:
        return []
    return [a for a in fleet if a.id != agent.id and a.reach is not None and a.reach.network == network]


def config(agent_id: str) -> dict:
    """Everything `agent_id` needs to sit in the mesh, agent-type neutral:
    where to listen, who may call in with which token, and whom to call.
    """
    agent = store.get_agent(agent_id)
    if agent.reach is None:
        raise ValueError(f"agent {agent_id!r} has no reach block; it is not in the mesh")
    fleet = store.list_agents()
    direct = peers(agent, fleet)
    direct_ids = {p.id for p in direct}
    # Everyone else with a reach block: on another network, or on none.
    relayed = [a for a in fleet if a.id != agent.id and a.reach is not None and a.id not in direct_ids]
    endpoint = urlsplit(agent.reach.endpoint)
    listen = urlsplit(f"//{agent.reach.bind}") if agent.reach.bind else endpoint
    loopback = listen.hostname in ("127.0.0.1", "localhost", "::1")
    beacon_url = public_url()
    outbound = []
    # Only peers that will accept this agent: no peer-list entries that can
    # only ever be refused.
    for p in (p for p in direct if may_call(agent.id, p)):
        outbound.append({"name": p.id, "url": p.reach.endpoint, "token": token(agent.id, p.id),
                         "capabilities": p.reach.capabilities})
        if beacon_url:
            # Same token: the mailbox checks it and delivers with it, so the
            # peer still sees who wrote (backend/mailbox.py).
            outbound.append({"name": f"{p.id}-mailbox", "url": f"{beacon_url}/a2a/{p.id}",
                             "token": token(agent.id, p.id), "capabilities": [*p.reach.capabilities, "mailbox"]})
    if beacon_url:
        # No direct path to these: one entry under the plain name, through
        # Beacon's relay. Agents call a peer by its id either way; Beacon carries it.
        for p in (p for p in relayed if may_call(agent.id, p)):
            outbound.append({"name": p.id, "url": f"{beacon_url}/a2a/{p.id}", "token": token(agent.id, p.id),
                             "capabilities": [*p.reach.capabilities, "relay"]})
    return {
        "agent": agent.id,
        # How peers reach this agent; its own sync delivers held messages here.
        "endpoint": agent.reach.endpoint,
        # Where to pull from next time: lets Beacon move hosts without anyone
        # touching each agent's host.
        "beacon": beacon_url or None,
        "listen": {
            "port": listen.port or (443 if listen.scheme == "https" else 80),
            "host": None if loopback else listen.hostname,
        },
        # Callers from every network: a relayed caller arrives through Beacon
        # with its own token. The access policy still decides who.
        "inbound": {name: token(name, agent.id)
                    for name in (BEACON_PEER, *(p.id for p in [*direct, *relayed] if may_call(p.id, agent)))},
        "outbound": outbound,
        # The agent signs in to the board with its pull token, which it has.
        "board": {"url": f"{beacon_url}/board/mcp"} if agent.reach.board and beacon_url else None,
    }


def install_script(agent_id: str, docker: str | None = None, docker_user: str = "hermes") -> str:
    """`docker` names the agent's container: the script then runs on the
    container's host as root instead of inside the agent's environment."""
    agent = store.get_agent(agent_id)
    if agent.reach is None:
        raise ValueError(f"agent {agent_id!r} has no reach block; nothing to install")
    beacon_url = public_url()
    if not beacon_url:
        raise ValueError(f"{PUBLIC_URL_ENV} is not set; agents need it to reach Beacon")
    driver = get_driver(agent.type)
    pull_token = token(agent.id, BEACON_PEER)
    if docker:
        return driver.mesh_install_docker_script(agent, beacon_url, pull_token, docker, docker_user)
    return driver.mesh_install_script(agent, beacon_url, pull_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.mesh", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    install = sub.add_parser("install", help="print the one-time host bootstrap script for one agent")
    install.add_argument("agent_id")
    install.add_argument("--docker", metavar="CONTAINER", help="agent runs in this container; script runs on its host as root")
    install.add_argument("--docker-user", default="hermes", help="user Hermes runs as in the container (default: hermes)")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(install_script(args.agent_id, args.docker, args.docker_user))
    except (store.NotFound, ValueError, MissingSecret) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
