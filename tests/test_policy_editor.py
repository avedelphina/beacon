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
    saved, before = store.history(policy.KIND, policy.RECORD)  # newest first, with who made each
    assert (saved["body"], saved["actor"]) == (FAMILIES, "beacon") and before["body"] == ALLOW_ALL


def test_save_refuses_invalid_text_and_a_stale_base(client, fleet3):
    h = _hash(client)
    bad = client.put("/api/policy", json={"text": "rules: [", "base_hash": h, "confirm": True})
    assert bad.status_code == 422

    policy.save(FAMILIES, "someone-else")  # someone else edited the policy
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
    monkeypatch.setattr(store, "HISTORY_KEEP", 3)
    for i in range(6):
        policy.save(ALLOW_ALL + f"# edit {i}\n", f"user{i}")
    assert [h["actor"] for h in store.history(policy.KIND, policy.RECORD)] == ["user5", "user4", "user3"]


def test_saved_policy_changes_the_peer_lists(client, fleet3):
    client.put("/api/policy", json={"text": FAMILIES, "base_hash": _hash(client), "confirm": True})
    assert {p["name"] for p in mesh.config("a-1")["outbound"] if "-mailbox" not in p["name"]} == {"a-2"}


def _record(**changes):
    base = {"id": "a-1", "type": "hermes", "host": "h1", "desired": {}, "tags": ["family-a"],
            "reach": {"endpoint": "http://a-1.test:9900", "network": "tailnet", "callers": None, "board": False}}
    reach = {**base["reach"], **changes.pop("reach", {})}
    return {**base, **changes, "reach": reach}


def test_the_mcp_token_cannot_change_tags_callers_or_board_but_can_edit_the_rest(fleet3, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")
    mcp = TestClient(app, headers={"Authorization": "Bearer test-mcp-token"})

    for change in ({"tags": ["family-b"]}, {"tags": []}, {"reach": {"callers": ["a-2"]}}, {"reach": {"board": True}},
                   {"owner": "someone"}, {"member_logs": True}):  # an owner is a member's way in
        refused = mcp.put("/api/agents/a-1", json=_record(**change))
        assert refused.status_code == 403, change
        assert "human browser session" in refused.json()["detail"]

    assert mcp.put("/api/agents/a-1", json=_record(notes="edited")).status_code == 200  # not access-related
    assert store.get_agent("a-1").tags == ["family-a"] and store.get_agent("a-1").notes == "edited"
    assert mcp.put("/api/agents/new-one", json={**_record(), "id": "new-one"}).status_code == 403  # a new agent arrives with tags


def test_a_person_can_change_tags(client, fleet3):
    response = client.put("/api/agents/a-1", json=_record(tags=["family-a", "extra"]))
    assert response.status_code == 200 and store.get_agent("a-1").tags == ["family-a", "extra"]
