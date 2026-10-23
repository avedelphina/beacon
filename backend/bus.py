"""First hop of the agent bus: call one registered agent by name.

v1 speaks exactly one transport, `hermes_api` — Hermes's own OpenAI-compatible
API server (`POST {endpoint}/v1/chat/completions`, non-streaming). The caller
names an agent and sends a message; it does not learn the endpoint, the
profile, or the key. Streaming, ACP, MCP, and Delta Chat are later transports,
not this one.
"""

import os

import httpx

from .schemas import Agent

DEFAULT_TIMEOUT = 120


class Unreachable(Exception):
    """The target is registered but this call cannot be delivered."""


def call(agent: Agent, message: str, context: str | None = None, *, client: httpx.Client | None = None) -> str:
    """Send one user message to `agent` and return the assistant text.

    `context`, when given, rides along as a system message so the caller can
    bound the turn without inventing a second protocol field.
    """
    if agent.reach is None:
        raise Unreachable(f"agent {agent.id!r} has no reach block; it is inventory-only")
    reach = agent.reach
    if reach.transport != "hermes_api":
        raise Unreachable(f"agent {agent.id!r} transport {reach.transport!r} is not supported")

    key = os.environ.get(reach.api_key_env)
    if not key:
        raise Unreachable(
            f"agent {agent.id!r} api key env {reach.api_key_env} is not set on this Beacon process"
        )

    messages = []
    if context:
        messages.append({"role": "system", "content": context})
    messages.append({"role": "user", "content": message})
    body = {"model": "hermes-agent", "messages": messages, "stream": False}

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT)
    try:
        response = client.post(
            f"{reach.endpoint}/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
    except httpx.HTTPError as exc:
        raise Unreachable(f"agent {agent.id!r} did not answer: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if not response.is_success:
        raise Unreachable(f"agent {agent.id!r} returned HTTP {response.status_code}")
    return _assistant_text(agent.id, response)


def _assistant_text(agent_id: str, response: httpx.Response) -> str:
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise Unreachable(f"agent {agent_id!r} returned a response Beacon cannot read") from exc
    if not isinstance(content, str) or not content:
        raise Unreachable(f"agent {agent_id!r} returned no assistant text")
    return content
