"""Clients get random, revocable tokens, made from the page by the operator they act for."""

import base64
import json

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import auth, board_mcp, clients, mesh, opstore, store
from backend.app import app
from backend.schemas import Agent

TOM, ANN = "tom@example.com", "ann@example.com"


def _session(email):
    payload = base64.b64encode(json.dumps({"user": {"email": email}}).encode()).decode()
    person = TestClient(app)
    person.cookies.set("session", TimestampSigner(auth.SESSION_SECRET).sign(payload.encode()).decode())
    return person


@pytest.fixture
def ops(fleet, fake_ssh, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "tokens-test-secret")
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM, ANN}))
    return SimpleNs(tom=_session(TOM), ann=_session(ANN))


class SimpleNs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _make(person, **body):
    done = person.post("/api/clients", json={"confirm": True, **body})
    assert done.status_code == 200, done.text
    return done.json()


def test_an_operator_makes_their_own_client_and_gets_its_token_once(ops):
    plan = ops.tom.post("/api/clients", json={"kind": "claude"}).json()
    assert plan["would_run"] is True and "claude-tom" in plan["detail"]
    assert store.list_agents() == []                                   # nothing was made by the plan

    made = _make(ops.tom, kind="claude", label="laptop")
    assert made["agent"]["id"] == "claude-tom" and made["token"].startswith("bcl_")
    assert (made["agent"]["acts_for"], made["agent"]["max_clearance"], made["agent"]["enabled"]) == (TOM, 3, True)
    assert "claude mcp add" in made["settings"] and made["token"] in made["settings"]
    agent = store.get_agent("claude-tom")
    assert agent.type == "client" and agent.host == "clients" and agent.reach is None and agent.owner == TOM
    assert store.get_host("clients").ssh is None                       # a place where nothing runs

    assert clients.resolve(made["token"]) == "claude-tom"
    listed = ops.ann.get("/api/clients").json()
    assert [c["id"] for c in listed] == ["claude-tom"] and made["token"] not in json.dumps(listed)
    assert listed[0]["tokens"][0]["label"] == "laptop" and listed[0]["tokens"][0]["created_by"] == TOM
    with opstore.connect() as db:                                      # Beacon holds a hash, not the token
        assert db.execute("SELECT count(*) FROM client_tokens WHERE token_hash = ?", (made["token"],)).fetchone()[0] == 0
        actions = [r["capability"] for r in db.execute("SELECT capability FROM host_actions")]
    assert "create_client" in actions                                  # who made it is on record


def test_a_second_client_of_the_same_kind_and_a_bad_request_are_refused(ops):
    _make(ops.tom)
    assert ops.tom.post("/api/clients", json={"confirm": True}).status_code == 400
    assert _make(ops.tom, kind="codex")["agent"]["id"] == "codex-tom"
    assert ops.ann.post("/api/clients", json={"kind": "nonsense", "confirm": True}).status_code == 400
    assert ops.ann.post("/api/clients", json={"max_clearance": 4, "confirm": True}).status_code == 400
    assert ops.ann.post("/api/clients", json={"max_clearance": -1, "confirm": True}).status_code == 400
    assert _make(ops.ann, max_clearance=2)["agent"]["max_clearance"] == 2


def test_only_a_person_in_a_browser_makes_one(ops, mcp_client, client):
    assert mcp_client.post("/api/clients", json={"confirm": True}).status_code == 403      # the service token is not a person
    assert client.post("/api/clients", json={"confirm": True}).status_code == 400          # open mode has nobody to bind it to
    assert store.list_agents() == []


def test_the_operator_it_acts_for_mints_more_tokens_and_rotates(ops):
    first = _make(ops.tom)["token"]
    second = ops.tom.post("/api/clients/claude-tom/tokens", json={"label": "desktop", "confirm": True}).json()["token"]
    assert clients.resolve(first) == clients.resolve(second) == "claude-tom"
    third = ops.tom.post("/api/clients/claude-tom/tokens", json={"revoke_others": True, "confirm": True}).json()["token"]
    assert clients.resolve(first) is None and clients.resolve(second) is None and clients.resolve(third) == "claude-tom"
    assert ops.ann.post("/api/clients/claude-tom/tokens", json={"confirm": True}).status_code == 403   # not hers
    assert ops.tom.post("/api/clients/nobody/tokens", json={"confirm": True}).status_code == 404


def test_any_operator_can_revoke_and_turn_a_client_off_but_only_its_operator_turns_it_on(ops):
    made = _make(ops.tom)
    token, token_id = made["token"], made["token_id"]

    assert ops.ann.delete(f"/api/clients/claude-tom/tokens/{token_id}").json()["would_run"] is True
    assert clients.resolve(token) == "claude-tom"                                           # the plan changed nothing
    assert ops.ann.delete(f"/api/clients/claude-tom/tokens/{token_id}?confirm=true").status_code == 200
    assert clients.resolve(token) is None
    assert ops.ann.delete(f"/api/clients/claude-tom/tokens/{token_id}?confirm=true").status_code == 404

    fresh = ops.tom.post("/api/clients/claude-tom/tokens", json={"confirm": True}).json()["token"]
    off = ops.ann.patch("/api/clients/claude-tom", json={"enabled": False, "confirm": True})
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert clients.resolve(fresh) is None and board_mcp.author_for(f"Bearer {fresh}") is None
    assert ops.ann.patch("/api/clients/claude-tom", json={"enabled": True, "confirm": True}).status_code == 403
    assert ops.tom.patch("/api/clients/claude-tom", json={"enabled": True, "confirm": True}).status_code == 200
    assert clients.resolve(fresh) == "claude-tom"                                           # the same token, on again


def test_max_clearance_is_lowered_by_anyone_and_raised_only_by_its_operator_never_past_three(ops):
    _make(ops.tom, max_clearance=2)
    assert ops.ann.patch("/api/clients/claude-tom", json={"max_clearance": 1, "confirm": True}).json()["max_clearance"] == 1
    assert ops.ann.patch("/api/clients/claude-tom", json={"max_clearance": 3, "confirm": True}).status_code == 403
    assert ops.tom.patch("/api/clients/claude-tom", json={"max_clearance": 3, "confirm": True}).json()["max_clearance"] == 3
    assert ops.tom.patch("/api/clients/claude-tom", json={"max_clearance": 4, "confirm": True}).status_code == 400
    assert ops.tom.patch("/api/clients/claude-tom", json={"confirm": True}).status_code == 400


def test_the_agent_record_route_cannot_rebind_a_client(ops):
    _make(ops.tom)
    record = ops.ann.get("/api/agents/claude-tom").json()
    assert ops.ann.put("/api/agents/claude-tom", json={**record, "notes": "hello"}).status_code == 200      # other edits are fine
    hijack = {**record, "client": {**record["client"], "acts_for": ANN}}
    assert ops.ann.put("/api/agents/claude-tom", json=hijack).status_code == 400
    assert store.get_agent("claude-tom").client.acts_for == TOM
    new = {**record, "id": "claude-ann", "owner": ANN, "client": {"board": True, "acts_for": ANN, "max_clearance": 3}}
    assert ops.ann.put("/api/agents/claude-ann", json=new).status_code == 400                               # not made by hand


def test_use_is_recorded_and_a_deleted_client_loses_its_tokens(ops):
    made = _make(ops.tom)
    assert ops.tom.get("/api/clients").json()[0]["tokens"][0]["last_used_at"] is None
    clients.resolve(made["token"])
    assert ops.tom.get("/api/clients").json()[0]["tokens"][0]["last_used_at"] is not None
    with opstore.connect() as db:
        db.execute("DELETE FROM fleet_records WHERE id = 'claude-tom'")
    assert clients.resolve(made["token"]) is None


def test_the_acts_for_field_must_be_an_email_and_the_ceiling_is_three():
    for bad in ("tom", "a b@c", ""):
        with pytest.raises(ValueError):
            Agent.model_validate({"id": "x", "type": "client", "host": "clients", "client": {"acts_for": bad}})
    with pytest.raises(ValueError, match="0 to 3"):
        Agent.model_validate({"id": "x", "type": "client", "host": "clients", "client": {"max_clearance": 4}})
    assert Agent.model_validate({"id": "x", "type": "client", "host": "clients",
                                 "client": {"acts_for": " Tom@Example.COM "}}).client.acts_for == "tom@example.com"
