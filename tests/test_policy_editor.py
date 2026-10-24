"""Policy editor API: preview (read-only), save (human session only, confirmed,
validated, history kept, refuses a stale base), and the agent form's tags."""

import pytest
from fastapi.testclient import TestClient

from backend import auth, mesh, policy, store
from backend.app import app
from backend.schemas import Agent, Host

ALLOW_ALL = 'rules:\n  - {from: ["*"], to: ["*"]}\n'
FAMILIES = """# two families
tags: [family-a, family-b]
rules:
  - {from: [tag:family-a], to: [tag:family-a]}
  - {from: [tag:family-b], to: [tag:family-b]}
"""


@pytest.fixture
def fleet3(fleet, monkeypatch):
    monkeypatch.setattr(policy, "HISTORY_DIR", fleet / "policy-history")
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent_id, tag in (("a-1", "family-a"), ("a-2", "family-a"), ("b-1", "family-b")):
        store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": "h1", "tags": [tag],
            "reach": {"endpoint": f"http://{agent_id}.test:9900", "network": "tailnet"}}))
    return fleet


def _hash(client):
    return client.get("/api/policy").json()["hash"]


def test_get_returns_the_text_for_the_editor(client, fleet3):
    body = client.get("/api/policy").json()
    assert body["text"] == ALLOW_ALL and body["hash"] == policy.text_hash(ALLOW_ALL)


def test_preview_shows_what_a_change_would_do_without_saving(client, fleet3):
    result = client.post("/api/policy/preview", json={"text": FAMILIES}).json()

    assert result["errors"] == []
    assert result["removed"] == [["a-1", "b-1"], ["a-2", "b-1"], ["b-1", "a-1"], ["b-1", "a-2"]]
    assert result["added"] == []
    assert result["restarts"] == ["a-1", "a-2", "b-1"]  # each loses an allowed caller
    assert result["matrix"]["a-1"] == ["a-2"]
    assert policy.read_text() == ALLOW_ALL  # nothing saved


def test_preview_reports_invalid_text_as_errors(client, fleet3):
    for bad in ("rules: [", "- just\n- a list\n", "rules:\n  - {from: [], to: [x]}\n", "tags: ['Not Valid']\n"):
        result = client.post("/api/policy/preview", json={"text": bad}).json()
        assert result["errors"] and result["matrix"] is None


def test_preview_flags_likely_mistakes_but_still_shows_the_result(client, fleet3):
    result = client.post("/api/policy/preview", json={"text": "tags: [family-a]\nrules:\n  - {from: [tag:famly-a], to: ['*']}\n"}).json()
    assert result["errors"] == [] and any("famly-a" in p for p in result["problems"])


def test_save_needs_confirmation_then_writes_with_history(client, fleet3):
    request = {"text": FAMILIES, "base_hash": _hash(client)}

    asked = client.put("/api/policy", json={**request, "confirm": False}).json()
    assert asked["would_run"] is True and asked["tier"] == "T4"
    assert "4 removed" in asked["detail"] and asked["restarts"] == ["a-1", "a-2", "b-1"]
    assert policy.read_text() == ALLOW_ALL  # still not saved

    done = client.put("/api/policy", json={**request, "confirm": True}).json()

    assert done["saved"] is True and done["hash"] == policy.text_hash(FAMILIES)
    assert policy.read_text() == FAMILIES  # comments and layout kept exactly
    assert policy.load().tags == ["family-a", "family-b"]  # the cache saw the change
    kept = list((fleet3 / "policy-history").glob("*.yaml"))
    assert len(kept) == 1 and kept[0].read_text() == ALLOW_ALL
    assert not list(fleet3.glob(".policy-*.tmp"))  # the temp file is gone


def test_save_refuses_invalid_text_and_a_stale_base(client, fleet3):
    h = _hash(client)
    bad = client.put("/api/policy", json={"text": "rules: [", "base_hash": h, "confirm": True})
    assert bad.status_code == 422

    (fleet3 / "policy.yaml").write_text(FAMILIES)  # someone else edited the file
    stale = client.put("/api/policy", json={"text": ALLOW_ALL, "base_hash": h, "confirm": True})
    assert stale.status_code == 409 and policy.read_text() == FAMILIES


def test_the_mcp_token_cannot_change_the_policy_but_can_read_and_preview(fleet3, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")
    mcp = TestClient(app, headers={"Authorization": "Bearer test-mcp-token"})
    h = policy.text_hash(ALLOW_ALL)

    refused = mcp.put("/api/policy", json={"text": FAMILIES, "base_hash": h, "confirm": True})

    assert refused.status_code == 403 and "human browser session" in refused.json()["detail"]
    assert policy.read_text() == ALLOW_ALL
    assert mcp.get("/api/policy").status_code == 200
    assert mcp.post("/api/policy/preview", json={"text": FAMILIES}).status_code == 200


def test_history_keeps_only_the_newest(fleet3, monkeypatch):
    monkeypatch.setattr(policy, "HISTORY_KEEP", 3)
    # Names are "<second>-<author>"; distinct authors keep six saves in one second apart.
    for i in range(6):
        policy.save(ALLOW_ALL + f"# edit {i}\n", f"user{i}")
    kept = sorted(p.name.split("-", 1)[1] for p in (fleet3 / "policy-history").glob("*.yaml"))
    assert kept == ["user3.yaml", "user4.yaml", "user5.yaml"]


def test_saved_policy_changes_the_peer_lists(client, fleet3):
    client.put("/api/policy", json={"text": FAMILIES, "base_hash": _hash(client), "confirm": True})
    assert {p["name"] for p in mesh.config("a-1")["outbound"] if "-mailbox" not in p["name"]} == {"a-2"}
