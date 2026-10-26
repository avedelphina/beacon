"""A host without SSH settings: Beacon never logs in there and only talks to
its agents over A2A. The mesh works as for any agent; driver actions refuse."""

from backend import mesh, store
from backend.schemas import Agent, Host


def test_an_agent_on_a_host_without_ssh_is_in_the_mesh_but_not_driven(client, fleet, fake_ssh, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    assert client.put("/api/hosts/outside", json={"id": "outside", "address": "192.0.2.7", "ssh": None}).status_code == 200
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent_id, host in (("alpha", "h1"), ("outsider", "outside")):
        store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": host,
            "reach": {"endpoint": f"http://{agent_id}.test:9900", "network": "lan"}}))

    assert [p.id for p in mesh.peers(store.get_agent("alpha"), store.list_agents())] == ["outsider"]
    assert mesh.config("outsider")["agent"] == "outsider"

    status = client.get("/api/agents/outsider/status").json()
    assert status["state"] == "unknown" and "no SSH settings" in status["detail"]
    logs = client.get("/api/agents/outsider/logs")
    assert logs.status_code == 409 and "A2A only" in logs.json()["detail"]
    assert fake_ssh.calls == []
