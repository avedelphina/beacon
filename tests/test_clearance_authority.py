"""Who the human authority is. CL4 goes to the owner of the agent that asks and an operator may always answer
it; CL5 goes to an operator and nobody else; an owner who is not an operator is told when it waits."""

import pytest

from backend import auth, board, mesh, notify, opstore, policy, scheduler, store, workflow
from backend.schemas import Agent, Host

TOM, ANN, OWEN, PAT = "tom@example.com", "ann@example.com", "owen@example.com", "pat@example.com"
HOUR = 3600


def _agent(id_, owner=None):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "owner": owner,
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": True}})


@pytest.fixture
def told(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_tell", lambda to, subject, body: sent.append((sorted(to), subject, body)))
    return sent


@pytest.fixture
def world(fleet, monkeypatch, told):
    monkeypatch.setenv(mesh.SECRET_ENV, "authority-test-secret")
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM, ANN}))
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("worker", OWEN), _agent("other", PAT), _agent("tomsbot", TOM), _agent("orphan")):
        store.upsert_agent(agent)
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n', "test")
    return board.create_card("beacon", "Change the DNS")["id"]


def _ask(card, responder="human", clearance=4, requester="worker"):
    kind = "approval" if clearance >= 4 else "review" if clearance == 3 else "decision"
    binding = workflow.plan_binding({"c": card, "r": responder, "n": clearance, "by": requester, "x": id(object())})
    request = workflow.create(card_id=card, kind=kind, requester=requester, eligible_responder=responder,
                              capability="change", clearance=clearance, title=f"CL{clearance} change", summary="Exact plan",
                              options=["a"] if kind == "decision" else [], target={}, **binding)
    return request, binding


def _answer(request, binding, actor, action="approved", human=True):
    return workflow.respond(request["id"], actor=actor, human=human, action=action, response={}, **binding)


# --- where a request goes -----------------------------------------------------------------------------


def test_cl4_asked_of_any_operator_goes_to_the_agents_owner(world):
    assert _ask(world, "human")[0]["eligible_responder"] == OWEN                  # a member owns it
    assert _ask(world, "human", requester="tomsbot")[0]["eligible_responder"] == TOM
    assert _ask(world, "human", requester="orphan")[0]["eligible_responder"] == "human"   # no owner: any operator
    assert _ask(world, "human", requester="beacon")[0]["eligible_responder"] == "human"
    assert workflow.get(_ask(world, "human")[0]["id"])["events"][0]["kind"] == "created"
    assert any("asked owen@example.com" in e["body"] for e in board.get_card(world)["events"])


def test_cl4_may_name_the_owner_or_an_operator_and_nobody_else(world):
    assert _ask(world, OWEN)[0]["eligible_responder"] == OWEN
    assert _ask(world, ANN)[0]["eligible_responder"] == ANN
    for bad in (PAT, "other"):
        with pytest.raises(workflow.WorkflowError, match="clearance 4"):
            _ask(world, bad)
    with pytest.raises(workflow.WorkflowError, match=r"\(owen@example.com\)"):
        _ask(world, PAT)


def test_cl5_goes_to_an_operator_and_never_to_an_owner_or_an_agent(world):
    assert _ask(world, "human", clearance=5)[0]["eligible_responder"] == "human"
    assert _ask(world, TOM, clearance=5)[0]["eligible_responder"] == TOM
    for bad in (OWEN, "other", PAT):
        with pytest.raises(workflow.WorkflowError, match="clearance 5"):
            _ask(world, bad, clearance=5)


def test_below_cl4_nothing_is_rerouted(world):
    assert _ask(world, "human", clearance=3)[0]["eligible_responder"] == "human"
    assert _ask(world, "other", clearance=3)[0]["eligible_responder"] == "other"
    assert _ask(world, OWEN, clearance=2)[0]["eligible_responder"] == OWEN


# --- who may see and answer ---------------------------------------------------------------------------


def test_the_owner_answers_cl4_and_an_operator_always_may(world):
    request, binding = _ask(world, "human")                                       # to owen
    assert [r["id"] for r in workflow.list_requests(actor=OWEN, human=True)] == [request["id"]]
    assert [r["id"] for r in workflow.list_requests(actor=ANN, human=True)] == [request["id"]]     # an operator sees it
    assert workflow.list_requests(actor=PAT, human=True) == []                    # another owner does not
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        workflow.get(request["id"], actor=PAT, human=True)
    with pytest.raises(workflow.UnauthorizedResponder):
        _answer(request, binding, PAT)
    assert _answer(request, binding, ANN)["state"] == "approved"                  # the operator answers for the owner

    mine, mine_binding = _ask(world, "human")
    assert _answer(mine, mine_binding, OWEN)["state"] == "approved"


def test_only_an_operator_answers_cl5_even_if_it_somehow_names_an_owner(world):
    request, binding = _ask(world, "human", clearance=5)
    with opstore.connect() as db:                                                 # as if made before the rule
        db.execute("UPDATE workflow_requests SET eligible_responder = ? WHERE id = ?", (OWEN, request["id"]))
    with pytest.raises(workflow.UnauthorizedResponder):
        _answer(request, binding, OWEN)
    assert _answer(request, binding, TOM)["state"] == "approved"


def test_an_operator_cannot_answer_the_request_they_made(world):
    request, binding = _ask(world, "human", requester=TOM, clearance=5)
    with pytest.raises(workflow.UnauthorizedResponder):
        _answer(request, binding, TOM)
    assert _answer(request, binding, ANN)["state"] == "approved"


def test_cl3_and_below_are_not_widened_to_operators(world):
    request, _ = _ask(world, OWEN, clearance=3)
    assert workflow.list_requests(actor=ANN, human=True) == []
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        workflow.get(request["id"], actor=ANN, human=True)


def test_an_operators_client_sees_cl4_for_its_operator_and_is_told_it_is_not_its_to_answer(world):
    from backend import clients
    clients.create("claude", TOM, 3)
    request, _ = _ask(world, "human")
    seen = workflow.get(request["id"], actor="claude-tom")
    assert seen["delegate"]["can_answer"] is False and "CL4 and above" in seen["delegate"]["why_not"]


# --- an owner who stays silent ------------------------------------------------------------------------


def test_the_operators_are_told_once_when_cl4_waits_on_a_non_operator_owner(world, told):
    waiting, _ = _ask(world, "human")                                             # to owen, a member
    operators_own, _ = _ask(world, "human", requester="tomsbot")                  # to tom, an operator
    cl3, _ = _ask(world, OWEN, clearance=3)
    later = workflow.get(waiting["id"])["created_at"] + 5 * HOUR

    assert workflow.nudge_operators(now=later - 2 * HOUR) == 0                    # too soon
    told.clear()
    assert workflow.nudge_operators(now=later) == 1
    to, subject, body = told[0]
    assert to == sorted([TOM, ANN]) and "waiting on owen@example.com" in subject and "As an operator you can answer it" in body
    assert [e["kind"] for e in workflow.get(waiting["id"])["events"]][-1] == "operators_told"
    assert any("the operators were told" in e["body"] for e in board.get_card(world)["events"])
    assert workflow.nudge_operators(now=later + HOUR) == 0                        # once
    assert workflow.get(operators_own["id"])["events"][-1]["kind"] == "created"
    assert workflow.get(cl3["id"])["events"][-1]["kind"] == "created"


def test_an_answered_or_expired_request_is_not_nudged_and_the_tick_runs_it(world, monkeypatch):
    request, binding = _ask(world, "human")
    _answer(request, binding, OWEN)
    assert workflow.nudge_operators(now=workflow.get(request["id"])["created_at"] + 9 * HOUR) == 0
    calls = []
    monkeypatch.setattr(workflow, "nudge_operators", lambda: calls.append("nudged"))
    scheduler.housekeeping()
    assert "nudged" in calls
