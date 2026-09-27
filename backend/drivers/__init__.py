from .. import store
from ..schemas import Agent, Host
from . import hermes

DRIVERS = {"hermes": hermes}


def get_driver(agent_type: str):
    try:
        return DRIVERS[agent_type]
    except KeyError:
        raise ValueError(f"no driver for agent type {agent_type!r}")


def for_agent(agent_id: str, resolved: bool = False) -> tuple[Agent, Host, object]:
    """(agent, its host, its driver) — what every driver call needs.
    `resolved` merges templates into desired (see store.get_agent)."""
    agent = store.get_agent(agent_id, resolved=resolved)
    return agent, store.get_host(agent.host), get_driver(agent.type)
