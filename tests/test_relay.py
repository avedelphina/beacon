"""Cross-network relay (C1): agents that can't dial each other directly get
one peer entry under the plain name, pointing at Beacon's relay; every agent
accepts callers from any network (they arrive through Beacon with their own
token); the access policy still decides who."""

import pytest

from backend import mesh, store
from backend.schemas import Agent, Host

PUBLIC = "http://beacon.test:8642"


def _agent(id_, network, callers=None):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1",
        "reach": {"endpoint": f"http://{id_}.test:9900", "network": network, "callers": callers}})


@pytest.fixture
def two_networks(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, PUBLIC)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("alpha", "tailnet"), _agent("bravo", "tailnet"),
                  _agent("lima", "office-lan"), _agent("solo", None)):
        store.upsert_agent(agent)


def _peers(agent_id):
    return {p["name"]: p for p in mesh.config(agent_id)["outbound"]}


def test_other_networks_are_reached_through_the_relay_by_plain_name(two_networks):
    peers = _peers("alpha")
    assert peers["bravo"]["url"] == "http://bravo.test:9900"  # same network: direct
    assert "bravo-mailbox" in peers
    assert peers["lima"]["url"] == f"{PUBLIC}/a2a/lima"  # other network: relay, plain name
    assert "relay" in peers["lima"]["capabilities"]
    assert "lima-mailbox" not in peers  # the relay entry already is the mailbox
    assert peers["lima"]["token"] == mesh.token("alpha", "lima")


def test_an_agent_without_a_network_is_relayed_to_everyone(two_networks):
    peers = _peers("solo")
    assert set(peers) == {"alpha", "bravo", "lima"}
    assert all(p["url"].startswith(f"{PUBLIC}/a2a/") for p in peers.values())


def test_callers_from_every_network_are_accepted(two_networks):
    assert set(mesh.config("lima")["inbound"]) == {"beacon", "alpha", "bravo", "solo"}


def test_policy_applies_to_relayed_paths(two_networks):
    store.upsert_agent(_agent("lima", "office-lan", callers=["alpha"]))
    assert "lima" in _peers("alpha") and "lima" not in _peers("bravo")
    assert set(mesh.config("lima")["inbound"]) == {"beacon", "alpha"}


def test_no_relay_without_a_public_url(two_networks, monkeypatch):
    monkeypatch.delenv(mesh.PUBLIC_URL_ENV)
    assert set(_peers("alpha")) == {"bravo"}
    assert _peers("solo") == {}


def test_relay_call_carries_the_callers_identity(client, two_networks, monkeypatch):
    import httpx

    from backend import bus

    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["authorization"]))
        task = {"id": "t", "contextId": "c", "status": {"state": "TASK_STATE_COMPLETED",
                "message": {"role": "ROLE_AGENT", "parts": [{"text": "pong"}]}}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {"task": task}})

    real = httpx.Client
    monkeypatch.setattr(bus.httpx, "Client",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    body = {"jsonrpc": "2.0", "id": "r", "method": "SendMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "ping"}], "messageId": "m"}}}

    response = client.post("/a2a/lima", json=body, headers={"Authorization": f"Bearer {mesh.token('alpha', 'lima')}"})

    assert response.json()["result"]["task"]["status"]["message"]["parts"][0]["text"] == "pong"
    assert seen == [("lima.test", f"Bearer {mesh.token('alpha', 'lima')}")]
