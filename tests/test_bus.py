"""Agent bus: one agent calls another by name over the Hermes API hop.

Nothing here opens a socket. httpx.MockTransport stands in for the target
agent, and the fleet fixture keeps YAML in tmp_path.
"""

import json

import httpx
import pytest

from backend import bus
from backend.schemas import Agent


_RealClient = httpx.Client


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealClient(*args, **kwargs)

    return factory


def _completion(text: str, status: int = 200) -> httpx.Response:
    body = {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]
    }
    return httpx.Response(status, json=body)


def _reach(**overrides) -> dict:
    reach = {
        "transport": "hermes_api",
        "endpoint": "http://agent.test:8644",
        "api_key_env": "BEACON_AGENT_A1_KEY",
        "capabilities": ["calendar"],
    }
    reach.update(overrides)
    return reach


@pytest.fixture
def reachable(client, monkeypatch):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={
        "id": "a1", "type": "hermes", "host": "edge-01", "desired": {}, "reach": _reach(),
    })
    monkeypatch.setenv("BEACON_AGENT_A1_KEY", "test-agent-key")
    return "a1"


def test_registry_hides_endpoint_and_key(client, reachable):
    client.put("/api/agents/idle", json={"id": "idle", "type": "hermes", "host": "edge-01", "desired": {}})

    body = client.get("/api/agents/registry").json()

    by_id = {row["id"]: row for row in body}
    assert by_id["a1"] == {"id": "a1", "callable": True, "capabilities": ["calendar"]}
    assert by_id["idle"]["callable"] is False
    assert "endpoint" not in json.dumps(body)
    assert "api_key_env" not in json.dumps(body)


def test_unknown_agent_is_404(client):
    response = client.post("/api/agents/nope/call", json={"message": "ping"})
    assert response.status_code == 404


def test_agent_without_reach_is_not_callable(client):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "desired": {}})

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "no reach block" in response.json()["detail"]


def test_missing_api_key_does_not_call_out(client, monkeypatch):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={
        "id": "a1", "type": "hermes", "host": "edge-01", "desired": {}, "reach": _reach(),
    })
    monkeypatch.delenv("BEACON_AGENT_A1_KEY", raising=False)

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "not set" in response.json()["detail"]


def test_successful_call_returns_reply_and_sends_context(client, reachable, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return _completion("thursday is free")

    monkeypatch.setattr(bus.httpx, "Client", _client_factory(handler))

    response = client.post("/api/agents/a1/call", json={"message": "is thursday free?", "context": "from alice"})

    assert response.status_code == 200
    assert response.json() == {"agent": "a1", "reply": "thursday is free"}
    assert seen["url"] == "http://agent.test:8644/v1/chat/completions"
    assert seen["authorization"] == "Bearer test-agent-key"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "from alice"},
        {"role": "user", "content": "is thursday free?"},
    ]


def test_empty_message_is_rejected(client, reachable):
    response = client.post("/api/agents/a1/call", json={"message": "   "})
    assert response.status_code == 422


def test_upstream_http_error_is_502(client, reachable, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    monkeypatch.setattr(bus.httpx, "Client", _client_factory(handler))

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "HTTP 401" in response.json()["detail"]
    assert "test-agent-key" not in response.text


def test_upstream_transport_error_is_502(client, reachable, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bus.httpx, "Client", _client_factory(handler))

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "did not answer" in response.json()["detail"]


def test_malformed_upstream_body_is_502(client, reachable, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    monkeypatch.setattr(bus.httpx, "Client", _client_factory(handler))

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "cannot read" in response.json()["detail"]


def test_call_requires_auth_when_enabled(reachable, monkeypatch):
    from backend import auth
    from backend.app import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    open_client = TestClient(app)

    response = open_client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 401


def test_mcp_bearer_may_call(reachable, mcp_client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("ack")

    monkeypatch.setattr(bus.httpx, "Client", _client_factory(handler))

    response = mcp_client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 200
    assert response.json()["reply"] == "ack"


def test_wrong_bearer_is_rejected_when_auth_enabled(reachable, monkeypatch):
    from backend import auth
    from backend.app import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")
    caller = TestClient(app, headers={"Authorization": "Bearer wrong-token"})

    response = caller.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 401


def test_reach_rejects_path_and_unknown_transport():
    with pytest.raises(ValueError):
        Agent.model_validate({
            "id": "a1", "type": "hermes", "host": "edge-01",
            "reach": _reach(endpoint="http://agent.test:8644/v1"),
        })
    with pytest.raises(ValueError):
        Agent.model_validate({
            "id": "a1", "type": "hermes", "host": "edge-01",
            "reach": _reach(transport="delta_chat"),
        })


def test_bus_call_refuses_agent_without_reach():
    agent = Agent(id="a1", type="hermes", host="edge-01")
    with pytest.raises(bus.Unreachable, match="no reach block"):
        bus.call(agent, "ping")
