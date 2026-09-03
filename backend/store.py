import re
from pathlib import Path

import yaml

from .schemas import Agent, Host

FLEET_DIR = Path(__file__).resolve().parent.parent / "fleet"
HOSTS_FILE = FLEET_DIR / "hosts.yaml"
AGENTS_DIR = FLEET_DIR / "agents"
DECOMMISSIONED_DIR = FLEET_DIR / "decommissioned"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class NotFound(Exception):
    pass


class InvalidId(Exception):
    pass


def _check_id(id_: str) -> None:
    if not ID_RE.match(id_):
        raise InvalidId(f"id {id_!r} must match {ID_RE.pattern}")


def _load_hosts_raw() -> list[dict]:
    if not HOSTS_FILE.exists():
        return []
    data = yaml.safe_load(HOSTS_FILE.read_text()) or {}
    return data.get("hosts", [])


def _save_hosts_raw(hosts: list[dict]) -> None:
    HOSTS_FILE.write_text(yaml.safe_dump({"hosts": hosts}, sort_keys=False))


def list_hosts() -> list[Host]:
    return [Host(**h) for h in _load_hosts_raw()]


def get_host(id_: str) -> Host:
    for h in _load_hosts_raw():
        if h["id"] == id_:
            return Host(**h)
    raise NotFound(id_)


def upsert_host(host: Host) -> Host:
    _check_id(host.id)
    hosts = _load_hosts_raw()
    payload = host.model_dump()
    for i, h in enumerate(hosts):
        if h["id"] == host.id:
            hosts[i] = payload
            break
    else:
        hosts.append(payload)
    _save_hosts_raw(hosts)
    return host


def delete_host(id_: str) -> None:
    hosts = _load_hosts_raw()
    remaining = [h for h in hosts if h["id"] != id_]
    if len(remaining) == len(hosts):
        raise NotFound(id_)
    _save_hosts_raw(remaining)


def _agent_path(id_: str) -> Path:
    return AGENTS_DIR / f"{id_}.yaml"


def list_agents() -> list[Agent]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    agents = []
    for path in sorted(AGENTS_DIR.glob("*.yaml")):
        agents.append(Agent(**yaml.safe_load(path.read_text())))
    return agents


def get_agent(id_: str, resolved: bool = False) -> Agent:
    """`resolved=True` returns the agent with `desired` replaced by the merge
    of its templates and its own overrides (see backend/templates.py). The
    file on disk is never touched — this copy is for reads and driver calls.
    """
    path = _agent_path(id_)
    if not path.exists():
        raise NotFound(id_)
    agent = Agent(**yaml.safe_load(path.read_text()))
    if resolved:
        from . import templates  # lazy: templates imports store.NotFound

        agent = agent.model_copy(update={"desired": templates.resolve(agent)})
    return agent


def upsert_agent(agent: Agent) -> Agent:
    _check_id(agent.id)
    if not any(h.id == agent.host for h in list_hosts()):
        raise NotFound(f"host {agent.host!r} not in fleet/hosts.yaml")
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    _agent_path(agent.id).write_text(yaml.safe_dump(agent.model_dump(), sort_keys=False))
    return agent


def delete_agent(id_: str) -> None:
    path = _agent_path(id_)
    if not path.exists():
        raise NotFound(id_)
    path.unlink()


def archive_agent(id_: str) -> None:
    """Move an agent's record out of the live fleet into fleet/decommissioned/
    — kept for history (git-diffable) rather than deleted outright.
    """
    path = _agent_path(id_)
    if not path.exists():
        raise NotFound(id_)
    DECOMMISSIONED_DIR.mkdir(parents=True, exist_ok=True)
    path.rename(DECOMMISSIONED_DIR / f"{id_}.yaml")
