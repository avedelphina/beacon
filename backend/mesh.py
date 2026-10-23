"""A2A mesh: which agents call each other directly, and with which token.

Tailscale-style: agents that declare the same `reach.network` call each
other directly, and Beacon only acts as the coordination server that hands
out peer lists. Nothing here sits on the message path, so a Beacon outage
leaves direct pairs talking. An agent without a network is reachable only
through Beacon.

Tokens are pairwise and derived — HMAC(BEACON_A2A_SECRET, "caller>target")
— so Beacon keeps one secret instead of N² stored tokens. A host that leaks
its tokens exposes only calls into itself, never another pair. Rotating the
secret rotates every token at once; re-render and re-apply every agent.

CLI (runs where Beacon's env is; the output contains tokens):
    python -m backend.mesh render <agent-id>   # host-side script for that agent
"""

import argparse
import hashlib
import hmac
import os
import sys

from . import store
from .drivers import get_driver
from .schemas import Agent

SECRET_ENV = "BEACON_A2A_SECRET"
BEACON_PEER = "beacon"


class MissingSecret(RuntimeError):
    """No mesh secret on this Beacon process, so no token can be derived."""


def token(caller: str, target: str) -> str:
    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise MissingSecret(f"{SECRET_ENV} is not set on this Beacon process")
    # Hex, not base64: a token passed as a CLI argument must never start with "-".
    return hmac.new(secret.encode(), f"{caller}>{target}".encode(), hashlib.sha256).hexdigest()


def peers(agent: Agent, fleet: list[Agent]) -> list[Agent]:
    """Agents `agent` calls directly: same declared network, reachable, not itself."""
    network = agent.reach.network if agent.reach else None
    if network is None:
        return []
    return [a for a in fleet if a.id != agent.id and a.reach is not None and a.reach.network == network]


def render(agent_id: str) -> str:
    agent = store.get_agent(agent_id)
    if agent.reach is None:
        raise ValueError(f"agent {agent_id!r} has no reach block; nothing to render")
    direct = peers(agent, store.list_agents())
    inbound = {name: token(name, agent.id) for name in (BEACON_PEER, *(p.id for p in direct))}
    outbound = [(peer, token(agent.id, peer.id)) for peer in direct]
    return get_driver(agent.type).a2a_script(agent, inbound, outbound)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.mesh", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="print the host-side A2A script for one agent").add_argument("agent_id")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(render(args.agent_id))
    except (store.NotFound, ValueError, MissingSecret) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
