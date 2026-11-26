"""Moving a secret between a person and an agent, or between two agents, without keeping it.

Beacon is a pipe: the value passes through memory, over the SSH it already has, into or out of a
file in the agent's own `beacon-secrets/` directory. Nothing here writes it to the store, the
mailbox, a log or an error. That is why there is no queue: if a host is unreachable the transfer
fails and is tried again, because holding the value would be storing it.

An agent never handles the value in a message: it writes and reads the files with shell
redirection and talks about names and paths only.
"""

from __future__ import annotations

import base64
import re

from . import audit, mesh, notify, store
from .drivers import for_agent
from .schemas import Agent, SecretError

MAX_BYTES = 64 * 1024
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _check_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise SecretError("a secret's name is 1-64 letters, digits, dots, dashes or underscores, "
                          "starting with a letter or digit")


def _write(agent_id: str, name: str, data: bytes) -> str:
    agent, host, driver = for_agent(agent_id)
    return driver.secret_put(agent, host, name, base64.b64encode(data).decode())


def put(agent_id: str, name: str, data: bytes) -> str:
    """Into the agent's inbox; returns the file's path on its host."""
    _check_name(name)
    if not data or len(data) > MAX_BYTES:
        raise SecretError(f"a secret is 1 to {MAX_BYTES} bytes")
    return _write(agent_id, name, data)


def names(agent_id: str) -> list[str]:
    """What waits in the agent's outbox."""
    agent, host, driver = for_agent(agent_id)
    return sorted(n for n in driver.secret_names(agent, host) if NAME_RE.fullmatch(n))


def read(agent_id: str, name: str) -> bytes:
    """From the agent's outbox, leaving the file until `remove`."""
    _check_name(name)
    agent, host, driver = for_agent(agent_id)
    return base64.b64decode(driver.secret_get(agent, host, name, MAX_BYTES))


def remove(agent_id: str, name: str) -> None:
    # ponytail: a file replaced between read and remove is lost with it; compare checksums if that happens.
    _check_name(name)
    agent, host, driver = for_agent(agent_id)
    driver.secret_remove(agent, host, name)


def _fleet_agent(agent_id: str) -> Agent:
    try:
        agent = store.get_agent(agent_id)
    except (store.NotFound, store.InvalidId):
        raise SecretError(f"no agent {agent_id!r}")
    if agent.client is not None:
        raise SecretError(f"{agent_id!r} is a client: it has no host to hold a secret")
    return agent


def send(sender: str, recipient: str, name: str) -> str:
    """Agent to agent: sender's outbox/<name> becomes recipient's inbox/from-<sender>.<name>, so a
    peer can never overwrite what a person or another agent put there. The sender's file is removed
    only once the recipient has it."""
    _check_name(name)
    _fleet_agent(sender)
    target = _fleet_agent(recipient)
    if sender == recipient or not mesh.may_call(sender, target):
        raise SecretError(f"you may not send a secret to {recipient!r}")
    if recipient in mesh.open_chats():
        raise SecretError(f"{recipient!r} has a chat open to anyone: it does not get secrets from agents")
    action = audit.record(sender, "agent", "secret_send", "CL2", recipient,
                          f"hand secret {name!r} from {sender} to {recipient}")
    try:
        path = _write(recipient, f"from-{sender}.{name}", read(sender, name))
        remove(sender, name)
    except Exception:
        audit.finish(action, "failed")
        raise
    audit.finish(action, "succeeded")
    return path


def offer(agent_id: str, name: str) -> None:
    """Tell the agent's people that outbox/<name> waits to be revealed."""
    _check_name(name)
    _fleet_agent(agent_id)
    if name not in names(agent_id):
        raise SecretError(f"there is no {name!r} in your beacon-secrets/outbox")
    notify.secret_offered(agent_id, name)
