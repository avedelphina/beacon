"""Agent bus: Beacon calls an agent by name over A2A v1.0 JSON-RPC.

Nothing here opens a socket. httpx.MockTransport stands in for the target
agent, and the fleet fixture keeps YAML in tmp_path. Response shapes mirror
Hermes v0.21's A2A adapter (plugins/platforms/a2a/protocol.py).
"""

import json

import httpx
import pytest

from backend import bus, mesh
from backend.schemas import Agent


_RealClient = httpx.Client


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealClient(*args, **kwargs)

    return factory


def _task(text: str, state: str = "TASK_STATE_COMPLETED", context_id: str = "ctx-1") -> dict:
    message = {"role": "ROLE_AGENT", "parts": [{"text": text, "mediaType": "text/plain"}],
               "messageId": "m1", "contextId": context_id}
    return {"id": "task-1", "contextId": context_id,
            "status": {"state": state, "timestamp": "2026-10-23T10:00:00.000Z", "message": message},
            "artifacts": [{"artifactId": "a1", "parts": [{"text": text}]}]}


def _rpc_result(result: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"jsonrpc": "2.0", "id": "x", "result": result})


def _rpc_error(code: int, message: str, status: int) -> httpx.Response:
    return httpx.Response(status, json={"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}})


def _reach(**overrides) -> dict:
    reach = {
        "transport": "a2a",
        "endpoint": "http://agent.test:9900",
        "network": "tailnet",
        "capabilities": ["calendar"],
    }
    reach.update(overrides)
    return reach


def _host(client):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": [],
    })


@pytest.fixture
def reachable(client, monkeypatch):
    _host(client)
    client.put("/api/agents/a1", json={
        "id": "a1", "type": "hermes", "host": "edge-01", "desired": {}, "reach": _reach(),
    })
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    return "a1"


def _answer_with(monkeypatch, handler):
    monkeypatch.setattr(bus.httpx, "Client", _client_factory(handler))


def test_registry_hides_endpoint_and_network(client, reachable):
    client.put("/api/agents/idle", json={"id": "idle", "type": "hermes", "host": "edge-01", "desired": {}})

    body = client.get("/api/agents/registry").json()

    by_id = {row["id"]: row for row in body}
    assert by_id["a1"] == {"id": "a1", "callable": True, "capabilities": ["calendar"]}
    assert by_id["idle"]["callable"] is False
    assert "endpoint" not in json.dumps(body)
    assert "tailnet" not in json.dumps(body)


def test_unknown_agent_is_404(client):
    response = client.post("/api/agents/nope/call", json={"message": "ping"})
    assert response.status_code == 404


def test_agent_without_reach_is_not_callable(client):
    _host(client)
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "desired": {}})

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "no reach block" in response.json()["detail"]


def test_missing_mesh_secret_does_not_call_out(client, monkeypatch):
    _host(client)
    client.put("/api/agents/a1", json={
        "id": "a1", "type": "hermes", "host": "edge-01", "desired": {}, "reach": _reach(),
    })
    monkeypatch.delenv(mesh.SECRET_ENV, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call out without a token")

    _answer_with(monkeypatch, handler)

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "not set" in response.json()["detail"]


def test_call_sends_a2a_send_message_and_returns_task(client, reachable, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return _rpc_result({"task": _task("thursday is free")})

    _answer_with(monkeypatch, handler)

    response = client.post("/api/agents/a1/call", json={"message": "is thursday free?"})

    assert response.status_code == 200
    assert response.json() == {
        "agent": "a1", "task_id": "task-1", "context_id": "ctx-1",
        "state": "TASK_STATE_COMPLETED", "reply": "thursday is free",
    }
    assert seen["url"] == "http://agent.test:9900"
    assert seen["headers"]["authorization"] == f"Bearer {mesh.token('beacon', 'a1')}"
    assert seen["headers"]["a2a-version"] == "1.0"
    body = seen["body"]
    assert body["jsonrpc"] == "2.0" and body["method"] == "SendMessage"
    message = body["params"]["message"]
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": "is thursday free?"}]
    assert "contextId" not in message


def test_context_id_continues_the_thread(client, reachable, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["message"] = json.loads(request.content)["params"]["message"]
        return _rpc_result({"task": _task("still free", context_id="ctx-7")})

    _answer_with(monkeypatch, handler)

    response = client.post("/api/agents/a1/call", json={"message": "and friday?", "context_id": "ctx-7"})

    assert seen["message"]["contextId"] == "ctx-7"
    assert response.json()["context_id"] == "ctx-7"


def test_endpoint_path_is_posted_to_as_is(client, monkeypatch):
    # Hermes serves extra local profiles under a path segment.
    _host(client)
    client.put("/api/agents/a1", json={
        "id": "a1", "type": "hermes", "host": "edge-01", "desired": {},
        "reach": _reach(endpoint="http://agent.test:9900/foxtrot/"),
    })
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _rpc_result({"task": _task("hi")})

    _answer_with(monkeypatch, handler)

    assert client.post("/api/agents/a1/call", json={"message": "ping"}).status_code == 200
    assert seen["url"] == "http://agent.test:9900/foxtrot/"


def test_failed_task_is_returned_not_raised(client, reachable, monkeypatch):
    _answer_with(monkeypatch, lambda request: _rpc_result(
        {"task": _task("[agent did not reply in time]", state="TASK_STATE_FAILED")}))

    response = client.post("/api/agents/a1/call", json={"message": "long job"})

    assert response.status_code == 200
    assert response.json()["state"] == "TASK_STATE_FAILED"
    assert response.json()["reply"] == "[agent did not reply in time]"


def test_bare_message_and_legacy_task_answers_are_read(client, reachable, monkeypatch):
    # Non-Hermes and pre-1.0 peers answer with an unwrapped Message or Task.
    _answer_with(monkeypatch, lambda request: _rpc_result(
        {"role": "agent", "parts": [{"kind": "text", "text": "pong"}], "messageId": "m", "contextId": "ctx-9"}))
    assert client.post("/api/agents/a1/call", json={"message": "ping"}).json() == {
        "agent": "a1", "task_id": None, "context_id": "ctx-9", "state": None, "reply": "pong",
    }

    legacy = _task("pong", state="completed")
    del legacy["status"]["message"]  # reply only in artifacts
    _answer_with(monkeypatch, lambda request: _rpc_result(legacy))
    assert client.post("/api/agents/a1/call", json={"message": "ping"}).json()["reply"] == "pong"


def test_empty_message_is_rejected(client, reachable):
    response = client.post("/api/agents/a1/call", json={"message": "   "})
    assert response.status_code == 422


def test_jsonrpc_error_is_502_with_reason_and_no_token(client, reachable, monkeypatch):
    _answer_with(monkeypatch, lambda request: _rpc_error(-32052, "peer 'beacon' not trusted", 403))

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "not trusted" in response.json()["detail"]
    assert "-32052" in response.json()["detail"]
    assert mesh.token("beacon", "a1") not in response.text


def test_non_json_http_error_is_502(client, reachable, monkeypatch):
    _answer_with(monkeypatch, lambda request: httpx.Response(503, text="upstream down"))

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "HTTP 503" in response.json()["detail"]


def test_upstream_transport_error_is_502(client, reachable, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _answer_with(monkeypatch, handler)

    response = client.post("/api/agents/a1/call", json={"message": "ping"})

    assert response.status_code == 502
    assert "did not answer" in response.json()["detail"]


def test_malformed_upstream_body_is_502(client, reachable, monkeypatch):
    _answer_with(monkeypatch, lambda request: httpx.Response(200, json={"jsonrpc": "2.0", "id": "x"}))

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
    _answer_with(monkeypatch, lambda request: _rpc_result({"task": _task("ack")}))

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


@pytest.mark.parametrize("override", [
    {"endpoint": "http://agent.test:9900/?x=1"},
    {"endpoint": "ftp://agent.test:9900"},
    {"endpoint": "http://agent.test:port"},
    {"endpoint": "http:///nohost"},
    {"transport": "hermes_api"},
    {"network": "Not A Label"},
])
def test_reach_rejects_bad_values(override):
    with pytest.raises(ValueError):
        Agent.model_validate({"id": "a1", "type": "hermes", "host": "edge-01", "reach": _reach(**override)})


def test_bus_call_refuses_agent_without_reach():
    agent = Agent(id="a1", type="hermes", host="edge-01")
    with pytest.raises(bus.Unreachable, match="no reach block"):
        bus.call(agent, "ping")
