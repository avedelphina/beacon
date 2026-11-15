"""Outside events enter as untrusted triage cards, and only when signed."""

import hashlib
import hmac
import json

import pytest

from backend import board, intake, opstore

SECRET = "intake-test-secret"


@pytest.fixture
def signed(monkeypatch):
    monkeypatch.setenv(intake.SECRET_ENV, SECRET)

    def sign(body: bytes) -> str:
        return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return sign


def _cards():
    with opstore.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM cards ORDER BY id")]


def test_intake_is_off_without_a_secret(client, monkeypatch):
    monkeypatch.delenv(intake.SECRET_ENV, raising=False)
    assert client.post("/intake/forge", content=b"{}").status_code == 404


def test_an_unsigned_or_wrongly_signed_event_is_refused_and_recorded_nowhere(client, signed):
    body = json.dumps({"action": "opened"}).encode()
    assert client.post("/intake/forge", content=body).status_code == 401
    bad = client.post("/intake/forge", content=body, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert bad.status_code == 401
    assert _cards() == []


def test_a_signed_event_becomes_a_fenced_triage_card(client, signed):
    body = json.dumps({"action": "opened", "note": "```bash\nrm -rf /\n```"}).encode()
    response = client.post("/intake/forge", content=body, headers={
        "X-Hub-Signature-256": signed(body), "X-Gitea-Event": "pull_request"})

    assert response.status_code == 202
    [card] = _cards()
    assert card["id"] == response.json()["card_id"]
    assert card["created_by"] == "intake:forge" and card["state"] == "todo" and card["assignee"] is None
    assert card["title"] == "forge: pull_request"
    assert "not triaged" in card["body"] and "someone else's text" in card["body"]
    assert "```bash" not in card["body"]  # the event's own fence cannot close the card's fence


def test_the_card_says_when_the_event_was_cut(signed, fleet):
    body = b"x" * (intake.SHOWN + 10)
    card = intake.record("forge", None, body)
    assert "[cut:" in board.get_card(card["id"])["body"]


@pytest.mark.parametrize("source", ["Forge", "-bad", "x" * 40])
def test_a_bad_source_name_is_refused(client, signed, source):
    body = b"{}"
    response = client.post(f"/intake/{source}", content=body, headers={"X-Hub-Signature-256": signed(body)})
    assert response.status_code == 400
    assert _cards() == []


def test_an_event_over_the_limit_is_refused_before_it_is_read(client, signed):
    body = b"x" * (intake.MAX_BYTES + 1)
    response = client.post("/intake/forge", content=body, headers={"X-Hub-Signature-256": signed(body)})
    assert response.status_code == 413
    assert _cards() == []


def _reader(agent_id="reader"):
    from backend import store
    from backend.schemas import Agent, Host
    store.upsert_host(Host.model_validate({"id": "h-intake", "address": "10.0.0.7", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": "h-intake",
                                             "reach": {"endpoint": f"http://{agent_id}.test:9900"}}))
    return agent_id


def test_intake_cards_are_operator_only_until_the_operators_say_otherwise(fleet):
    from backend import members
    card = intake.record("forge", "push", b"{}")
    agent = _reader()

    assert intake.visibility() == {"members": False, "agents": False}
    assert not intake.agent_may_see(agent, card)
    assert not members.card_visible("someone@example.com", card)
    assert intake.agent_may_see("beacon", card)  # an operator's own client is not filtered


def test_the_toggles_widen_members_and_agents_separately(fleet):
    from backend import members
    card = intake.record("forge", "push", b"{}")
    agent = _reader()

    intake.set_visibility("tom@example.com", members=True, agents=False)
    assert members.card_visible("someone@example.com", card)
    assert not intake.agent_may_see(agent, card)

    intake.set_visibility("tom@example.com", members=False, agents=True)
    assert not members.card_visible("someone@example.com", card)
    assert intake.agent_may_see(agent, card)


def test_ordinary_cards_are_not_affected_by_the_intake_toggles(fleet):
    from backend import members
    agent = _reader()
    card = board.create_card("beacon", "Plain card")
    assert intake.agent_may_see(agent, card)
    assert not members.card_visible("someone@example.com", card)  # as before: not theirs


def test_only_a_browser_session_can_change_who_sees_intake_cards(client, human_client):
    assert client.get("/api/intake/visibility").json() == {"members": False, "agents": False}
    refused = client.put("/api/intake/visibility", json={"members": True, "agents": True})
    assert refused.status_code == 403
    assert intake.visibility() == {"members": False, "agents": False}

    changed = human_client.put("/api/intake/visibility", json={"members": True, "agents": False})
    assert changed.status_code == 200
    assert intake.visibility() == {"members": True, "agents": False}
