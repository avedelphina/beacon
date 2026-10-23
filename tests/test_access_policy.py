"""Access policy (reach.callers): one rule, mesh.may_call, applied on every
path — the target's inbound tokens (direct calls), the mailbox, delivery of
messages queued before a change, and board assignments."""

import types

import httpx
import pytest

from backend import board, bus, mailbox, mesh, opstore, store
from backend.schemas import Agent, Host


def _agent(id_, callers=None):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1",
        "reach": {"endpoint": f"http://{id_}.test:9900", "network": "tailnet", "callers": callers}})


@pytest.fixture
def fleet3(fleet, monkeypatch):
    """alpha accepts anyone on the network; bravo only charlie."""
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, "http://beacon.test:8642")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(_agent("alpha"))
    store.upsert_agent(_agent("bravo", callers=["charlie"]))
    store.upsert_agent(_agent("charlie"))
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["authorization"]))
        task = {"id": "t", "contextId": "c", "status": {"state": "TASK_STATE_COMPLETED",
                "message": {"role": "ROLE_AGENT", "parts": [{"text": "ok"}]}}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {"task": task}})

    real = httpx.Client
    monkeypatch.setattr(bus.httpx, "Client",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    # Deliver assignment notices inline. Replace board's own reference, not the
    # global threading module, so nothing else in the process is affected.
    monkeypatch.setattr(board, "threading", types.SimpleNamespace(
        Thread=lambda target, args, daemon: types.SimpleNamespace(start=lambda: target(*args))))
    return seen


def test_may_call_rule():
    open_agent, closed = _agent("alpha"), _agent("bravo", callers=["charlie"])
    assert mesh.may_call("anyone", open_agent)
    assert mesh.may_call("charlie", closed) and not mesh.may_call("alpha", closed)
    assert mesh.may_call(mesh.BEACON_PEER, closed)  # operators are never locked out


def test_policy_shapes_inbound_and_outbound(fleet3):
    bravo = mesh.config("bravo")
    assert set(bravo["inbound"]) == {"beacon", "charlie"}  # alpha's token is not accepted

    alpha_peers = {p["name"] for p in mesh.config("alpha")["outbound"]}
    assert "bravo" not in alpha_peers and "bravo-mailbox" not in alpha_peers  # no dead entries
    assert {"charlie", "charlie-mailbox"} <= alpha_peers
    assert {"bravo", "bravo-mailbox"} <= {p["name"] for p in mesh.config("charlie")["outbound"]}


def test_mailbox_refuses_visibly(client, fleet3):
    body = {"jsonrpc": "2.0", "id": "r", "method": "SendMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "hi"}], "messageId": "m"}}}

    refused = client.post("/a2a/bravo", json=body, headers={"Authorization": f"Bearer {mesh.token('alpha', 'bravo')}"})
    allowed = client.post("/a2a/bravo", json=body, headers={"Authorization": f"Bearer {mesh.token('charlie', 'bravo')}"})

    assert refused.status_code == 403
    assert refused.json()["error"]["message"] == "bravo does not accept calls from alpha"
    assert allowed.status_code == 200 and fleet3 == [("bravo.test", f"Bearer {mesh.token('charlie', 'bravo')}")]


def test_message_queued_before_a_policy_change_is_not_delivered(fleet3):
    message_id = mailbox.enqueue("alpha", "charlie", "message", "queued while allowed")
    store.upsert_agent(_agent("charlie", callers=["bravo"]))  # charlie now refuses alpha

    mailbox.deliver_pending("charlie")

    with opstore.connect() as db:
        row = db.execute("SELECT state, last_error FROM mailbox WHERE id = ?", (message_id,)).fetchone()
    assert tuple(row) == ("failed", "charlie does not accept calls from alpha")
    assert fleet3 == []


def test_board_assignment_respects_the_policy(fleet3):
    with pytest.raises(ValueError, match="bravo does not accept work from alpha"):
        board.create_card("alpha", "for bravo", assignee="bravo")
    card = board.create_card("alpha", "unassigned")
    with pytest.raises(ValueError, match="does not accept work"):
        board.update_card("alpha", card["id"], assignee="bravo")

    # Allowed callers, people and Beacon may assign.
    assert board.create_card("charlie", "ok", assignee="bravo")["assignee"] == "bravo"
    assert board.create_card("someone@example.com", "ok", assignee="bravo")["assignee"] == "bravo"
    assert board.update_card("beacon", card["id"], assignee="bravo")["assignee"] == "bravo"


def test_callers_must_be_agent_ids():
    with pytest.raises(ValueError):
        _agent("x", callers=["Not An Id"])
