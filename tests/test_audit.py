"""The audit of host actions: written at the confirm gate, finished by the stream."""

import pytest
from fastapi.testclient import TestClient

from backend import audit, auth
from backend.app import app
from tests.test_members import as_person, people  # noqa: F401  (the `people` fixture lives there)


def _rows(**kw):
    return audit.recent(**kw)


@pytest.fixture
def agent(client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": []})
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "desired": {}})
    return "a1"


def test_an_action_is_recorded_only_once_it_passes_the_gate(client, agent, fake_ssh):
    fake_ssh.stream_lines = ["__BEACON_EXIT__0"]
    assert client.post("/api/agents/a1/deploy").json()["would_run"] is True   # the plan, not the act
    assert _rows() == []
    client.post("/api/agents/a1/deploy?confirm=true")
    row, = _rows()
    assert (row["capability"], row["tier"], row["target"], row["via"]) == ("deploy", "T3", "a1", "open")
    assert row["actor"] == "beacon" and "deploy 'a1'" in row["detail"]


def test_a_streamed_action_learns_how_it_ended(client, agent, fake_ssh):
    for lines, outcome in ((["ok", "__BEACON_EXIT__0"], "succeeded"),
                           (["bad", "__BEACON_EXIT__2"], "failed"),
                           (["hung", "__BEACON_EXIT__none"], "did not finish")):
        fake_ssh.stream_lines = lines
        client.post("/api/agents/a1/deploy?confirm=true")
        assert _rows(limit=1)[0]["outcome"] == outcome


def test_decommission_records_its_outcome_too(client, agent, fake_ssh):
    fake_ssh.stream_lines = ["__BEACON_EXIT__0"]
    client.post("/api/agents/a1/decommission", json={"confirm": True})
    row, = _rows()
    assert row["capability"] == "decommission" and row["target"] == "a1" and row["outcome"] == "succeeded"


def test_who_asked_is_the_person_or_the_service(fleet, fake_ssh, monkeypatch):
    from backend.schemas import Agent
    from backend import store
    from tests.conftest import make_host
    store.upsert_host(make_host("h1"))
    store.upsert_agent(Agent.model_validate({"id": "a1", "type": "hermes", "host": "h1"}))
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"tom@example.com"}))
    monkeypatch.setattr(auth, "MCP_TOKEN", "svc")
    fake_ssh.stream_lines = ["__BEACON_EXIT__0"]
    tom = as_person("tom@example.com")
    tom.post("/api/agents/a1/deploy?confirm=true")
    TestClient(app, headers={"Authorization": "Bearer svc"}).post("/api/agents/a1/deploy?confirm=true")
    mcp_row, tom_row = _rows()  # newest first
    assert (tom_row["actor"], tom_row["via"]) == ("tom@example.com", "session")
    assert (mcp_row["actor"], mcp_row["via"]) == ("beacon", "mcp")


def test_the_log_is_readable_by_operators_and_filterable_but_not_by_members(people):
    people["tom"].post("/api/agents/a1/restart?confirm=true")
    people["tom"].post("/api/agents/b1/restart?confirm=true")
    assert [r["target"] for r in people["tom"].get("/api/audit").json()] == ["b1", "a1"]
    assert [r["target"] for r in people["tom"].get("/api/audit?target=a1").json()] == ["a1"]
    assert people["ann"].get("/api/audit").status_code in (403, 404)  # not on a member's route list
