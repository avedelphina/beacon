"""Agent bus: call one registered agent by name over A2A.

A2A v1.0 JSON-RPC (the Linux Foundation agent-to-agent standard) rather than
a Beacon-specific protocol, so any A2A agent can be a target — Hermes is the
first, not the only one. The caller names an agent; it does not learn the
endpoint or the token. One blocking `SendMessage` per call: Hermes holds the
request open until the agent answers or its A2A_REPLY_TIMEOUT (300s default)
passes, then answers with a terminal task either way. Threads ride on A2A's
own `contextId`. Streaming, push notifications, and relaying for agents on
another network are later steps, not this one.
"""

import os
import uuid

import httpx

from .schemas import Agent

# Hermes's own reply window plus headroom, so Beacon reports the agent's
# timeout verdict instead of cutting the call off first.
DEFAULT_TIMEOUT = httpx.Timeout(330, connect=10)


class Unreachable(Exception):
    """The target is registered but this call cannot be delivered."""


def call(agent: Agent, message: str, context_id: str | None = None, *, client: httpx.Client | None = None) -> dict:
    """Send one user message to `agent`; return the task's id, thread, state and reply text.

    `context_id` continues an earlier thread — pass back the one a previous
    call returned. Without it the agent starts a new thread.
    """
    msg = {"role": "ROLE_USER", "parts": [{"text": message}], "messageId": uuid.uuid4().hex}
    if context_id:
        msg["contextId"] = context_id
    return _task_view(agent.id, _rpc(agent, "SendMessage", {"message": msg}, client))


def _rpc(agent: Agent, method: str, params: dict, client: httpx.Client | None) -> dict:
    if agent.reach is None:
        raise Unreachable(f"agent {agent.id!r} has no reach block; it is inventory-only")
    reach = agent.reach
    token = os.environ.get(reach.token_env)
    if not token:
        raise Unreachable(f"agent {agent.id!r} token env {reach.token_env} is not set on this Beacon process")

    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
    headers = {"Authorization": f"Bearer {token}", "A2A-Version": "1.0"}
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT)
    try:
        response = client.post(reach.endpoint, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise Unreachable(f"agent {agent.id!r} did not answer: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    try:
        payload = response.json()
    except ValueError:
        payload = None
    # Hermes sends JSON-RPC errors (bad token, untrusted peer, rate limit)
    # with a 4xx status too; its message says more than the status does.
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        raise Unreachable(
            f"agent {agent.id!r} refused the call: {str(error.get('message'))[:200]} (A2A error {error.get('code')})"
        )
    if not response.is_success:
        raise Unreachable(f"agent {agent.id!r} returned HTTP {response.status_code}")
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise Unreachable(f"agent {agent.id!r} returned a response Beacon cannot read")
    return payload["result"]


def _task_view(agent_id: str, result: dict) -> dict:
    # v1.0 wraps the answer as exactly one of {"task"} / {"message"};
    # pre-1.0 peers return the bare Task or Message.
    obj = result.get("task") or result.get("message") or result
    if not isinstance(obj, dict):
        raise Unreachable(f"agent {agent_id!r} returned a response Beacon cannot read")
    if isinstance(obj.get("status"), dict):
        status = obj["status"]
        reply = _text(status.get("message")) or "\n".join(_text(a) for a in obj.get("artifacts") or [])
        return {"task_id": obj.get("id"), "context_id": obj.get("contextId"), "state": status.get("state"), "reply": reply}
    # A bare Message is a complete answer with no task lifecycle behind it.
    return {"task_id": obj.get("taskId"), "context_id": obj.get("contextId"), "state": None, "reply": _text(obj)}


def _text(obj: object) -> str:
    parts = obj.get("parts") if isinstance(obj, dict) else None
    return "\n".join(p["text"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("text"), str))
