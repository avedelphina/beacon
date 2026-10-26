from .. import store
from ..schemas import Agent, Host
from . import hermes

DRIVERS = {"hermes": hermes}


def get_driver(agent_type: str):
    try:
        return DRIVERS[agent_type]
    except KeyError:
        raise ValueError(f"no driver for agent type {agent_type!r}")


class NoSSH(Exception):
    """The agent's host has no SSH settings: Beacon only talks to it over A2A."""


def for_agent(agent_id: str, resolved: bool = False) -> tuple[Agent, Host, object]:
    """(agent, its host, its driver) — what every driver call needs, and every
    driver call goes over SSH, so a host without SSH settings stops here.
    `resolved` merges templates into desired (see store.get_agent)."""
    agent = store.get_agent(agent_id, resolved=resolved)
    host = store.get_host(agent.host)
    if host.ssh is None:
        raise NoSSH(f"host {host.id!r} has no SSH settings: Beacon reaches {agent.id} over A2A only")
    return agent, host, get_driver(agent.type)
