"""Typed workflow request records and gates."""

import sqlite3
import time

import pytest

from backend import board, opstore, workflow
from backend.ssh import SSHResult


def _card():
    return board.create_card("beacon", "Push config")


def _binding():
    return workflow.plan_binding({"capability": "push_config", "target": {"agent_id": "a1"}, "desired": {"config": {"agent": {"max_turns": 42}}}})


def test_existing_operational_database_gets_workflow_tables(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    db_path = state / "beacon.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE cards (id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                project TEXT, assignee TEXT, state TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE card_events (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL,
                author TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, context_id TEXT, created_at REAL NOT NULL);
        """)
    monkeypatch.setattr(opstore, "STATE_DIR", state)

    with opstore.connect() as db:
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"cards", "card_events", "workflow_requests", "workflow_events"} <= tables


def test_create_list_get_and_respond_records_history():
    card = _card()
    binding = _binding()
    request = workflow.create(
        card_id=card["id"], kind="review", requester="agent-a", eligible_responder="reviewer@example.com",
        capability="push_config", tier=3, title="Review config", summary="Review the exact config plan",
        options=[], target={"agent_id": "a1"}, **binding,
    )
    assert request["state"] == "pending"
    assert workflow.list_requests(card_id=card["id"])[0]["id"] == request["id"]

    responded = workflow.respond(
        request["id"], actor="reviewer@example.com", human=True, action="approved", response={"note": "looks good"}, **binding,
    )
    assert responded["state"] == "approved"
    assert [event["kind"] for event in responded["events"]] == ["created", "responded"]
    assert responded["events"][-1]["payload"]["action"] == "approved"


def test_unauthorized_responder_is_rejected():
    request = workflow.create(
        card_id=_card()["id"], kind="approval", requester="beacon", eligible_responder="human",
        capability="set_policy", tier=4, title="Approve policy", summary="Approve policy change",
        options=[], target={"scope": "fleet"}, **_binding(),
    )
    try:
        workflow.respond(request["id"], actor="agent-a", human=False, action="approved", response={}, **_binding())
    except workflow.UnauthorizedResponder:
        pass
    else:
        raise AssertionError("unauthorized actor was accepted")


def test_decision_option_validation_and_choice():
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="decision", requester="beacon", eligible_responder="beacon",
        capability="push_config", tier=2, title="Choose rollout", summary="Choose one option",
        options=["canary", "all"], target={"agent_id": "a1"}, **binding,
    )
    try:
        workflow.respond(request["id"], actor="beacon", human=True, action="choose_option", response={"option": "later"}, **binding)
    except workflow.WorkflowError as exc:
        assert "canary" in str(exc)
    else:
        raise AssertionError("invalid decision option was accepted")
    chosen = workflow.respond(request["id"], actor="beacon", human=True, action="choose_option", response={"option": "canary"}, **binding)
    assert chosen["state"] == "approved"


def test_human_sentinel_requires_browser_session_for_agent_named_human():
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="review", requester="agent-a", eligible_responder="human",
        capability="push_config", tier=3, title="Review", summary="Exact plan",
        options=[], target={}, **binding,
    )
    with pytest.raises(workflow.UnauthorizedResponder):
        workflow.respond(request["id"], actor="human", human=False, action="approved", response={}, **binding)
    assert workflow.get(request["id"])["state"] == "pending"


def test_expiry_and_stale_plan_are_visible():
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="review", requester="beacon", eligible_responder="human",
        capability="push_config", tier=3, title="Expired review", summary="Short-lived review",
        options=[], target={"agent_id": "a1"}, expires_at=time.time() + 0.01, **binding,
    )
    time.sleep(0.02)
    assert workflow.get(request["id"])["state"] == "expired"

    fresh = workflow.create(
        card_id=_card()["id"], kind="review", requester="beacon", eligible_responder="human",
        capability="push_config", tier=3, title="Stale review", summary="Changed plan",
        options=[], target={"agent_id": "a1"}, **binding,
    )
    changed = workflow.plan_binding({"capability": "push_config", "target": {"agent_id": "a1"}, "desired": {"config": {"agent": {"max_turns": 99}}}})
    try:
        workflow.respond(fresh["id"], actor="human", human=True, action="approved", response={}, **changed)
    except workflow.StalePlan:
        pass
    else:
        raise AssertionError("changed plan was accepted")
    assert workflow.get(fresh["id"])["state"] == "stale"


def test_t3_push_config_requires_approved_exact_workflow(client, human_client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01",
                                       "desired": {"config": {"agent": {"max_turns": 42}}}})
    diff = client.get("/api/agents/a1/config-diff")
    fake_ssh.calls.clear()
    binding = diff.json()["workflow"]
    card = client.post("/api/board/cards", json={"title": "Config review"}).json()
    body = {
        "kind": "review", "eligible_responder": "human", "capability": "push_config", "tier": 3,
        "title": "Review push", "summary": "Review config", "target": binding["target"],
        "plan_revision": binding["plan_revision"], "plan_hash": binding["plan_hash"],
    }
    request = client.post(f"/api/board/cards/{card['id']}/workflows", json=body).json()
    blocked = client.post("/api/agents/a1/config-diff?confirm=true")
    assert blocked.status_code == 400 and fake_ssh.calls == []
    approved = human_client.post(f"/api/workflows/{request['id']}/respond", json={"action": "approved", "response": {}, "plan_revision": binding["plan_revision"], "plan_hash": binding["plan_hash"]})
    assert approved.status_code == 200
    fake_ssh.stream_lines = ["[beacon] done", "__BEACON_EXIT__0"]
    executed = client.post(f"/api/agents/a1/config-diff?confirm=true&workflow_request_id={request['id']}")
    assert executed.status_code == 200 and "done" in executed.text
    assert workflow.get(request["id"])["state"] == "executed"


def test_t4_and_t5_workflow_responses_are_human_only(mcp_client, fake_ssh):
    for tier, kind, capability in ((4, "approval", "set_policy"), (5, "approval", "decommission")):
        request = workflow.create(
            card_id=_card()["id"], kind=kind, requester="beacon", eligible_responder="human",
            capability=capability, tier=tier, title="Approval", summary="Human approval required",
            options=[], target={}, **_binding(),
        )
        response = mcp_client.post(f"/api/workflows/{request['id']}/respond", json={
            "action": "approved", "response": {}, **_binding(),
        })
        assert response.status_code == 403
        assert fake_ssh.calls == []


def test_t3_requester_cannot_self_approve():
    import pytest
    with pytest.raises(workflow.WorkflowError, match="different actors"):
        workflow.create(
            card_id=_card()["id"], kind="review", requester="beacon", eligible_responder="beacon",
            capability="push_config", tier=3, title="Review", summary="Independent review required",
            options=[], target={}, **_binding(),
        )


def test_workflow_plan_binding_includes_host_destination(client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01",
                                       "desired": {"config": {"agent": {"max_turns": 42}}}})
    target = client.get("/api/agents/a1/config-diff").json()["workflow"]["target"]
    assert target["host_id"] == "edge-01" and target["host_address"] == "10.0.0.1"


def test_api_human_workflow_visibility_is_scoped(human_client):
    hidden = workflow.create(
        card_id=_card()["id"], kind="review", requester="agent-a", eligible_responder="agent-b",
        capability="push_config", tier=3, title="Private review", summary="Agent-only",
        options=[], target={}, **_binding(),
    )
    visible = workflow.create(
        card_id=_card()["id"], kind="review", requester="agent-a", eligible_responder="human",
        capability="push_config", tier=3, title="Human review", summary="Browser review",
        options=[], target={}, **_binding(),
    )
    listed = human_client.get("/api/workflows")
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} == {visible["id"]}
    assert human_client.get(f"/api/workflows/{hidden['id']}").status_code == 403


def test_open_mode_without_session_cannot_self_approve_t3(client, fake_ssh):
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="review", requester="beacon", eligible_responder="human",
        capability="push_config", tier=3, title="Review", summary="Requires a real operator", options=[], target={},
        plan_revision=binding["plan_revision"], plan_hash=binding["plan_hash"],
    )
    response = client.post(f"/api/workflows/{request['id']}/respond", json={
        "action": "approved", "response": {}, **binding,
    })
    assert response.status_code == 403


def test_t3_requester_cannot_self_approve_human_slot():
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="review", requester="requester@example.com", eligible_responder="human",
        capability="push_config", tier=3, title="Review", summary="Independent review required",
        options=[], target={}, **binding,
    )
    import pytest
    with pytest.raises(workflow.UnauthorizedResponder):
        workflow.respond(request["id"], actor="requester@example.com", human=True, action="approved", response={}, **binding)


def test_third_actor_cannot_execute_approved_request():
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="review", requester="requester@example.com", eligible_responder="reviewer@example.com",
        capability="push_config", tier=3, title="Review", summary="Exact plan",
        options=[], target={}, **binding,
    )
    workflow.respond(request["id"], actor="reviewer@example.com", human=False, action="approved", response={}, **binding)
    import pytest
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        workflow.authorize(request_id=request["id"], capability="push_config", tier=3, actor="third-party", human=False,
                           target={}, **binding)


def test_concurrent_authorize_only_claims_once():
    import concurrent.futures
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="review", requester="requester@example.com", eligible_responder="reviewer@example.com",
        capability="push_config", tier=3, title="Review", summary="Exact plan",
        options=[], target={}, **binding,
    )
    workflow.respond(request["id"], actor="reviewer@example.com", human=False, action="approved", response={}, **binding)

    def claim():
        try:
            workflow.authorize(request_id=request["id"], capability="push_config", tier=3, actor="requester@example.com", human=False,
                               target={}, **binding)
            return "claimed"
        except workflow.WorkflowError:
            return "blocked"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == ["blocked", "claimed"]


    import pytest
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="review", requester="requester@example.com", eligible_responder="reviewer@example.com",
        capability="push_config", tier=3, title="Review", summary="Exact plan",
        options=[], target={}, **binding,
    )
    workflow.respond(request["id"], actor="reviewer@example.com", human=False, action="approved", response={}, **binding)
    workflow.authorize(request_id=request["id"], capability="push_config", tier=3, actor="requester@example.com", human=False,
                       target={}, **binding)
    with pytest.raises(workflow.WorkflowError, match="claimed"):
        workflow.authorize(request_id=request["id"], capability="push_config", tier=3, actor="requester@example.com", human=False,
                           target={}, **binding)
    workflow.mark_executed(request["id"], "requester@example.com")
    assert workflow.get(request["id"])["state"] == "executed"


def test_concurrent_push_posts_run_driver_once(client, human_client, fake_ssh):
    from concurrent.futures import ThreadPoolExecutor

    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01",
                                       "desired": {"config": {"agent": {"max_turns": 42}}}})
    binding = client.get("/api/agents/a1/config-diff").json()["workflow"]
    card = client.post("/api/board/cards", json={"title": "Review"}).json()
    request = client.post(f"/api/board/cards/{card['id']}/workflows", json={
        "kind": "review", "eligible_responder": "human", "capability": "push_config", "tier": 3,
        "title": "Review", "summary": "Review exact plan", "target": binding["target"],
        "plan_revision": binding["plan_revision"], "plan_hash": binding["plan_hash"],
    }).json()
    human_client.post(f"/api/workflows/{request['id']}/respond", json={"action": "approved", "response": {}, **binding})
    fake_ssh.stream_lines = ["done", "__BEACON_EXIT__0"]
    fake_ssh.calls.clear()
    path = f"/api/agents/a1/config-diff?confirm=true&workflow_request_id={request['id']}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(path), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 400]
    assert len(fake_ssh.calls) == 1
    assert workflow.get(request["id"])["state"] == "executed"



def test_workflow_stream_closes_nested_iterators(monkeypatch):
    import asyncio
    from backend import app

    class Closable:
        def __init__(self):
            self.closed = False
        def __iter__(self):
            return self
        def __next__(self):
            if self.closed:
                raise StopIteration
            return "first"
        def close(self):
            self.closed = True

    source = Closable()
    monkeypatch.setattr(workflow, "mark_execution_outcome", lambda *args: None)
    response = app._workflow_stream(source, 999999, "actor")
    body = response.body_iterator
    async def first_and_close():
        await body.__anext__()
        await body.aclose()

    asyncio.run(first_and_close())
    assert source.closed


def test_push_plan_binds_ssh_key_profile_and_type(client, human_client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/old", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "profile": "old-profile",
                                       "desired": {"config": {"agent": {"max_turns": 42}}}})
    old = client.get("/api/agents/a1/config-diff").json()["workflow"]
    card = client.post("/api/board/cards", json={"title": "Review"}).json()
    request = client.post(f"/api/board/cards/{card['id']}/workflows", json={
        "kind": "review", "eligible_responder": "human", "capability": "push_config", "tier": 3,
        "title": "Review", "summary": "Review exact plan", "target": old["target"],
        "plan_revision": old["plan_revision"], "plan_hash": old["plan_hash"],
    }).json()
    human_client.post(f"/api/workflows/{request['id']}/respond", json={"action": "approved", "response": {}, **old})
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/new", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "profile": "new-profile",
                                       "desired": {"config": {"agent": {"max_turns": 42}}}})
    response = client.post(f"/api/agents/a1/config-diff?confirm=true&workflow_request_id={request['id']}&plan_revision={old['plan_revision']}&plan_hash={old['plan_hash']}")
    assert response.status_code == 409
    assert workflow.get(request["id"])["state"] == "stale"


def test_caller_cannot_substitute_an_old_plan_binding(client, human_client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01",
                                       "desired": {"config": {"agent": {"max_turns": 42}}}})
    old = client.get("/api/agents/a1/config-diff").json()["workflow"]
    card = client.post("/api/board/cards", json={"title": "Review"}).json()
    request = client.post(f"/api/board/cards/{card['id']}/workflows", json={
        "kind": "review", "eligible_responder": "human", "capability": "push_config", "tier": 3,
        "title": "Review", "summary": "Review exact plan", "target": old["target"],
        "plan_revision": old["plan_revision"], "plan_hash": old["plan_hash"],
    }).json()
    human_client.post(f"/api/workflows/{request['id']}/respond", json={"action": "approved", "response": {}, **old})
    fake_ssh.calls.clear()
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01",
                                       "desired": {"config": {"agent": {"max_turns": 99}}}})
    response = client.post(f"/api/agents/a1/config-diff?confirm=true&workflow_request_id={request['id']}&plan_revision={old['plan_revision']}&plan_hash={old['plan_hash']}")
    assert response.status_code == 409 and fake_ssh.calls == []
    assert workflow.get(request["id"])["state"] == "stale"


def test_expired_response_persists_expiry_event():
    binding = _binding()
    request = workflow.create(
        card_id=_card()["id"], kind="review", requester="beacon", eligible_responder="reviewer@example.com",
        capability="push_config", tier=3, title="Expiring review", summary="Short-lived review",
        options=[], target={"agent_id": "a1"}, expires_at=time.time() + 0.01, **binding,
    )
    time.sleep(0.02)
    import pytest
    with pytest.raises(workflow.WorkflowError, match="expired"):
        workflow.respond(request["id"], actor="reviewer@example.com", human=True,
                         action="approved", response={}, **binding)
    stored = workflow.get(request["id"])
    assert stored["state"] == "expired" and stored["events"][-1]["kind"] == "expired"


def test_failed_and_uncertain_execution_consume_approval(client, human_client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01",
                                       "desired": {"config": {"agent": {"max_turns": 42}}}})
    binding = client.get("/api/agents/a1/config-diff").json()["workflow"]
    card = client.post("/api/board/cards", json={"title": "Review"}).json()
    request = client.post(f"/api/board/cards/{card['id']}/workflows", json={
        "kind": "review", "eligible_responder": "human", "capability": "push_config", "tier": 3,
        "title": "Review", "summary": "Review exact plan", "target": binding["target"],
        "plan_revision": binding["plan_revision"], "plan_hash": binding["plan_hash"],
    }).json()
    human_client.post(f"/api/workflows/{request['id']}/respond", json={"action": "approved", "response": {}, **binding})
    fake_ssh.stream_lines = ["failed", "__BEACON_EXIT__7"]
    response = client.post(f"/api/agents/a1/config-diff?confirm=true&workflow_request_id={request['id']}")
    assert response.status_code == 200 and workflow.get(request["id"])["state"] == "failed"

    second = workflow.create(
        card_id=card["id"], kind="review", requester="beacon", eligible_responder="human",
        capability="push_config", tier=3, title="Review 2", summary="Review exact plan",
        options=[], target=binding["target"], plan_revision=binding["plan_revision"], plan_hash=binding["plan_hash"],
    )
    human_client.post(f"/api/workflows/{second['id']}/respond", json={"action": "approved", "response": {}, **binding})
    fake_ssh.stream_lines = ["partial"]
    response = client.post(f"/api/agents/a1/config-diff?confirm=true&workflow_request_id={second['id']}")
    assert response.status_code == 200 and workflow.get(second["id"])["state"] == "execution_uncertain"


def test_driver_stream_exception_consumes_approval(client, human_client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01",
                                       "desired": {"config": {"agent": {"max_turns": 42}}}})
    binding = client.get("/api/agents/a1/config-diff").json()["workflow"]
    card = client.post("/api/board/cards", json={"title": "Review"}).json()
    request = client.post(f"/api/board/cards/{card['id']}/workflows", json={
        "kind": "review", "eligible_responder": "human", "capability": "push_config", "tier": 3,
        "title": "Review", "summary": "Review exact plan", "target": binding["target"],
        "plan_revision": binding["plan_revision"], "plan_hash": binding["plan_hash"],
    }).json()
    human_client.post(f"/api/workflows/{request['id']}/respond", json={"action": "approved", "response": {}, **binding})
    fake_ssh.stream_error = RuntimeError("driver failed before first output")
    response = client.post(f"/api/agents/a1/config-diff?confirm=true&workflow_request_id={request['id']}")
    assert response.status_code == 502
    assert workflow.get(request["id"])["state"] == "execution_uncertain"
