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
# The URL agents' hosts reach Beacon on for pulls, e.g. http://100.68.84.189:8642.
PUBLIC_URL_ENV = "BEACON_PUBLIC_URL"
BEACON_PEER = "beacon"


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
    direct = peers(agent, store.list_agents())
    endpoint = urlsplit(agent.reach.endpoint)
    listen = urlsplit(f"//{agent.reach.bind}") if agent.reach.bind else endpoint
    loopback = listen.hostname in ("127.0.0.1", "localhost", "::1")
    return {
        "agent": agent.id,
        "listen": {
            "port": listen.port or (443 if listen.scheme == "https" else 80),
            "host": None if loopback else listen.hostname,
        },
        "inbound": {name: token(name, agent.id) for name in (BEACON_PEER, *(p.id for p in direct))},
        "outbound": [
            {"name": p.id, "url": p.reach.endpoint, "token": token(agent.id, p.id), "capabilities": p.reach.capabilities}
            for p in direct
        ],
    }


def install_script(agent_id: str, docker: str | None = None, docker_user: str = "hermes") -> str:
    """`docker` names the agent's container: the script then runs on the
    container's host as root instead of inside the agent's environment."""
    agent = store.get_agent(agent_id)
    if agent.reach is None:
        raise ValueError(f"agent {agent_id!r} has no reach block; nothing to install")
    public_url = os.environ.get(PUBLIC_URL_ENV, "").rstrip("/")
    if not public_url:
        raise ValueError(f"{PUBLIC_URL_ENV} is not set; agents need it to reach Beacon")
    driver = get_driver(agent.type)
    pull_token = token(agent.id, BEACON_PEER)
    if docker:
        return driver.mesh_install_docker_script(agent, public_url, pull_token, docker, docker_user)
    return driver.mesh_install_script(agent, public_url, pull_token)


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
