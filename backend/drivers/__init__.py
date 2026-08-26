from . import hermes

DRIVERS = {"hermes": hermes}


def get_driver(agent_type: str):
    try:
        return DRIVERS[agent_type]
    except KeyError:
        raise ValueError(f"no driver for agent type {agent_type!r}")
