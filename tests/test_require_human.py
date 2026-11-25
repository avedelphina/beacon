"""A reviewer that is not sure sends a request to a person: one way, the plan and clearance unchanged,
and from then on only a person in the browser may answer it."""

import sqlite3
from types import SimpleNamespace

import pytest

from backend import auth, board, board_mcp, mailbox, notify, opstore, policy, store, workflow
from backend.schemas import Agent, Host

OP, OTHER = "op@example.com", "other@example.com"


def _agent(id_):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1",
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": True}})


@pytest.fixture
def told(monkeypatch):
    """The notices people would get: (recipients, subject, body)."""
    sent = []
    monkeypatch.setattr(notify, "_tell", lambda to, subject, body: sent.append((sorted(to), subject, body)))
    return sent


@pytest.fixture
def crew(fleet, monkeypatch, told):
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("asker"), _agent("reviewer")):
        store.upsert_agent(agent)
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n', "test")
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({OP, OTHER}))
    monkeypatch.setattr(mailbox, "deliver_pending", lambda agent: None)
    return board.create_card("beacon", "Push the config")["id"]


def _ask(card, responder="reviewer", clearance=3, kind="review", requester="asker"):
    binding = workflow.plan_binding({"card": card, "clearance": clearance})
    request = workflow.create(card_id=card, kind=kind, requester=requester, eligible_responder=responder,
                              capability="push_config", clearance=clearance, title="Review the push", summary="Exact plan",
                              options=[], target={}, **binding)
    return request, binding


def _history(card):
    return [e["body"] for e in board.get_card(card)["events"] if e["kind"] == "workflow"]


def test_a_reviewer_sends_a_request_to_a_person_and_nothing_else_changes(crew):
    request, binding = _ask(crew)
    done = workflow.escalate(request["id"], actor="reviewer", human=False, reason="I cannot judge the host change",
                             context_id="ctx-1")

    assert done["state"] == "pending" and done["require_human"] is True and done["clearance"] == 3
    assert (done["plan_revision"], done["plan_hash"]) == (binding["plan_revision"], binding["plan_hash"])
    assert (done["escalated_by"], done["escalated_reason"]) == ("reviewer", "I cannot judge the host change")
    assert _history(crew)[-1].endswith(": needs a person: I cannot judge the host change")
    assert board.get_card(crew)["events"][-1]["context_id"] == "ctx-1"
    assert workflow.get(request["id"])["events"][-1]["kind"] == "escalated"


def test_after_that_only_a_person_in_the_browser_can_answer(crew):
    request, binding = _ask(crew)
    workflow.escalate(request["id"], actor="reviewer", human=False, reason="not sure")

    with pytest.raises(workflow.HumanApprovalRequired, match="not sure"):
        workflow.respond(request["id"], actor="reviewer", human=False, action="approved", response={}, **binding)
    with pytest.raises(workflow.UnauthorizedResponder):                 # the requester never could
        workflow.respond(request["id"], actor="asker", human=True, action="approved", response={}, **binding)
    done = workflow.respond(request["id"], actor=OP, human=True, action="approved", response={}, **binding)
    assert done["state"] == "approved"


def test_a_request_to_an_agent_becomes_visible_and_answerable_by_any_operator(crew):
    request, binding = _ask(crew)
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        workflow.get(request["id"], actor=OP, human=True)                # before: it was the agent's alone
    assert workflow.list_requests(actor=OP, human=True) == []

    workflow.escalate(request["id"], actor="reviewer", human=False, reason="not sure")
    assert [r["id"] for r in workflow.list_requests(actor=OP, human=True)] == [request["id"]]
    assert workflow.get(request["id"], actor=OTHER, human=True)["require_human"] is True
    assert workflow.respond(request["id"], actor=OTHER, human=True, action="rejected", response={}, **binding)["state"] == "rejected"


def test_a_request_to_a_named_person_stays_theirs(crew):
    request, binding = _ask(crew, responder=OP)
    workflow.escalate(request["id"], actor=OP, human=True, reason="I want to look at the host first")
    with pytest.raises(workflow.UnauthorizedResponder):
        workflow.respond(request["id"], actor=OTHER, human=True, action="approved", response={}, **binding)
    assert workflow.respond(request["id"], actor=OP, human=True, action="approved", response={}, **binding)["state"] == "approved"


def test_only_someone_who_could_answer_may_send_it_and_only_while_it_waits(crew):
    request, binding = _ask(crew)
    for actor, human in (("asker", False), ("stranger", False), ("asker", True)):
        with pytest.raises(workflow.UnauthorizedResponder):
            workflow.escalate(request["id"], actor=actor, human=human, reason="x")
    for reason in ("", "  ", "x" * 301, None):
        with pytest.raises(workflow.WorkflowError, match="why"):
            workflow.escalate(request["id"], actor="reviewer", human=False, reason=reason)
    workflow.respond(request["id"], actor="reviewer", human=False, action="changes_requested", response={"note": "n"}, **binding)
    with pytest.raises(workflow.WorkflowError, match="changes_requested"):
        workflow.escalate(request["id"], actor="reviewer", human=False, reason="too late")
    with pytest.raises(workflow.WorkflowNotFound):
        workflow.escalate(9999, actor="reviewer", human=False, reason="x")


def test_t4_already_needs_a_person_and_a_repeat_records_nothing_more(crew):
    t4, _ = _ask(crew, responder="human", clearance=4, kind="approval")
    with pytest.raises(workflow.WorkflowError, match="already need a person"):
        workflow.escalate(t4["id"], actor=OP, human=True, reason="x")

    request, _ = _ask(crew)
    workflow.escalate(request["id"], actor="reviewer", human=False, reason="first")
    again = workflow.escalate(request["id"], actor="reviewer", human=False, reason="second")
    assert again["escalated_reason"] == "first"
    assert len([e for e in workflow.get(request["id"])["events"] if e["kind"] == "escalated"]) == 1


def test_the_flag_survives_a_revision(crew):
    request, binding = _ask(crew, responder=OP)
    workflow.escalate(request["id"], actor=OP, human=True, reason="look closer")
    workflow.respond(request["id"], actor=OP, human=True, action="changes_requested", response={"note": "add a canary"}, **binding)
    revised = workflow.revise(request["id"], actor="asker", summary="With a canary", **workflow.plan_binding({"r": 1}))
    assert revised["state"] == "pending" and revised["require_human"] is True


def test_the_people_who_can_answer_and_the_requester_are_told(crew, told):
    request, _ = _ask(crew)
    told.clear()
    workflow.escalate(request["id"], actor="reviewer", human=False, reason="not sure")

    to, subject, body = told[0]
    assert to == sorted([OP, OTHER]) and "needs a person" in subject
    assert "reviewer sent" in body and "Why: not sure" in body
    with opstore.connect() as db:
        notice = db.execute("SELECT body FROM mailbox WHERE recipient = 'asker'").fetchone()
    assert "was sent to a person by reviewer" in notice["body"] and "Only a person can answer it now" in notice["body"]


def test_rest_and_mcp(crew, human_client, monkeypatch):
    request, _ = _ask(crew, responder="human")
    sent = human_client.post(f"/api/workflows/{request['id']}/require-human",
                             json={"reason": "check the host", "context_id": "ctx-2"})
    assert sent.status_code == 200 and sent.json()["require_human"] is True
    assert human_client.post(f"/api/workflows/{request['id']}/require-human", json={"reason": ""}).status_code == 400

    other, _ = _ask(crew)
    monkeypatch.setattr(board_mcp, "author_for", lambda token: "reviewer")
    ctx = SimpleNamespace(headers={"authorization": "Bearer t"})
    tool = board_mcp.build()._tool_manager.get_tool("workflow_require_human").fn
    assert tool(ctx=ctx, request_id=other["id"], reason="looks odd")["require_human"] is True
    with pytest.raises(workflow.HumanApprovalRequired):
        board_mcp.build()._tool_manager.get_tool("workflow_respond").fn(
            ctx=ctx, request_id=other["id"], action="approved", plan_revision=other["plan_revision"],
            plan_hash=other["plan_hash"], response={})


def test_requests_from_before_this_get_the_flag_off(fleet, monkeypatch, tmp_path):
    monkeypatch.setattr(opstore, "STATE_DIR", tmp_path / "old")
    (tmp_path / "old").mkdir()
    with sqlite3.connect(tmp_path / "old" / "beacon.db") as db:
        db.execute("CREATE TABLE workflow_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, card_id INTEGER NOT NULL,"
                   " kind TEXT NOT NULL, state TEXT NOT NULL, requester TEXT NOT NULL, eligible_responder TEXT NOT NULL,"
                   " capability TEXT NOT NULL, clearance INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,"
                   " options_json TEXT NOT NULL DEFAULT '[]', response_json TEXT, target_json TEXT NOT NULL DEFAULT '{}',"
                   " plan_revision TEXT NOT NULL, plan_hash TEXT NOT NULL, expires_at REAL NOT NULL,"
                   " created_at REAL NOT NULL, updated_at REAL NOT NULL, responded_at REAL)")
        db.execute("INSERT INTO workflow_requests (card_id, kind, state, requester, eligible_responder, capability, clearance,"
                   " title, summary, plan_revision, plan_hash, expires_at, created_at, updated_at)"
                   " VALUES (1, 'review', 'pending', 'a', 'human', 'ask', 3, 't', 's', 'r', 'h', 9e9, 1, 1)")
    old = workflow.get(1)
    assert (old["require_human"], old["escalated_by"], old["escalated_reason"]) == (False, None, None)
