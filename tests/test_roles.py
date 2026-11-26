"""Agent classes: asking for help, or for a review, of any agent of a kind. A class narrows whom a
call reaches and never widens what the access policy allows."""

import pytest

from backend import board, mesh, notify, policy, projects, workflow
from tests.test_members import ANN, TOM, people  # noqa: F401 (fixture)
from tests.test_projects import GOAL, _mail

POLICY = """
roles:
  skeptic: Reviews staged changes for risk
  steward: Housekeeping
rules:
  - {from: ["a1"], to: ["a2", "b1", "n1"]}
  - {from: ["a2"], to: ["a1"]}
"""


@pytest.fixture
def world(people, monkeypatch):  # noqa: F811
    from backend import store
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    monkeypatch.setattr(notify, "_tell", lambda *a, **kw: None)
    policy.save(POLICY, "test")
    for id_, tags in (("a1", ["skeptic"]), ("b1", ["skeptic", "steward"]), ("n1", ["skeptic"]), ("a2", [])):
        store.upsert_agent(store.get_agent(id_).model_copy(update={"tags": tags}))
    projects.create("a1", "Backups", **GOAL)
    return people | {"card": board.create_card("a1", "Roll out", project="Backups")["id"]}


def _review(card, responder="role:skeptic", clearance=3, kind="review", requester="a1"):
    binding = workflow.plan_binding({"c": card, "r": responder, "n": clearance})
    return workflow.create(card_id=card, kind=kind, requester=requester, eligible_responder=responder,
                           capability="change", clearance=clearance, title="Roll out", summary="Plan",
                           options=[], target={}, **binding), binding


def _answer(request, binding, actor, human=False):
    return workflow.respond(request["id"], actor=actor, human=human, action="approved", response={}, **binding)


def test_the_directory_says_what_each_class_is_for_and_whom_you_may_ask(world):
    assert board.role_directory("a1") == [
        {"role": "skeptic", "description": "Reviews staged changes for risk", "agents": ["b1", "n1"]},
        {"role": "steward", "description": "Housekeeping", "agents": ["b1"]}]
    assert [r["agents"] for r in board.role_directory("a2")] == [["a1"], []]
    assert world["ann"].get("/api/roles").status_code == 200


def test_a_call_for_help_can_name_classes_and_only_narrows(world):
    asked = board.update_card("a1", world["card"], help_wanted=True, help_roles=["steward"])
    assert asked["help_roles"] == ["steward"]
    assert "You are asked as steward" in _mail("b1")[-1] and not _mail("n1")
    with pytest.raises(projects.NotAllowed, match="class you are not in"):
        board.claim("n1", world["card"])
    assert board.claim("b1", world["card"])["assignee"] == "b1"


def test_an_unknown_or_unreachable_class_is_refused(world):
    with pytest.raises(ValueError, match="unknown class"):
        board.update_card("a1", world["card"], help_wanted=True, help_roles=["wizard"])
    card = board.create_card("a2", "mine", project="Backups")["id"]
    with pytest.raises(ValueError, match="no agent of class steward"):
        board.update_card("a2", card, help_wanted=True, help_roles=["steward"])
    with pytest.raises(workflow.WorkflowError, match="no agent of class"):
        workflow.validate_participants("a2", "role:steward")


def test_any_holder_of_the_class_may_review_never_the_requester(world):
    workflow.validate_participants("a1", "role:skeptic")
    request, binding = _review(world["card"])
    assert "of any skeptic" in _mail("b1")[-1] and "of any skeptic" in _mail("n1")[-1]
    assert not any("of any skeptic" in text for text in _mail("a1") + _mail("a2"))  # not itself, not outsiders
    seen = lambda actor: [r["id"] for r in workflow.list_requests(actor=actor)]  # noqa: E731
    assert seen("b1") == seen("n1") == seen("a1") == [request["id"]] and seen("a2") == []
    for actor in ("a1", "a2"):
        with pytest.raises(workflow.UnauthorizedResponder):
            _answer(request, binding, actor)
    assert _answer(request, binding, "b1")["state"] == "approved"
    with pytest.raises((workflow.WorkflowError, workflow.UnauthorizedResponder)):
        _answer(request, binding, "n1")  # the first answer counts


def test_membership_is_read_when_answering(world):
    from backend import store
    request, binding = _review(world["card"])
    store.upsert_agent(store.get_agent("b1").model_copy(update={"tags": []}))
    with pytest.raises(workflow.UnauthorizedResponder):
        _answer(request, binding, "b1")
    assert _answer(request, binding, "n1")["state"] == "approved"


def test_an_operator_sees_and_may_stand_in_but_a_member_may_not(world):
    request, binding = _review(world["card"])
    assert [r["id"] for r in workflow.list_requests(actor=TOM, human=True)] == [request["id"]]
    assert workflow.list_requests(actor=ANN, human=True) == []
    with pytest.raises(workflow.UnauthorizedResponder):
        _answer(request, binding, ANN, human=True)
    assert _answer(request, binding, TOM, human=True)["state"] == "approved"


def test_a_class_cannot_approve_what_needs_a_person_or_take_a_handoff(world):
    with pytest.raises(workflow.WorkflowError, match="clearance 4"):
        _review(world["card"], clearance=4, kind="approval")
    with pytest.raises(workflow.WorkflowError, match="handoff names one agent"):
        _review(world["card"], clearance=2, kind="handoff")


def test_the_policy_flags_a_class_nobody_holds(world):
    from backend import store
    problems = policy.problems(policy.parse("roles: {oracle: Knows things}\n"), store.list_agents())
    assert any("oracle" in p for p in problems)
    with pytest.raises(ValueError, match="needs a description"):
        policy.parse("roles: {oracle: ' '}\n")


def test_an_operator_gives_an_open_call_to_the_agent_they_choose(world):
    board.update_card("a1", world["card"], help_wanted=True, help_roles=["steward"])
    given = board.update_card(TOM, world["card"], assignee="a2")  # not a steward: the choice is the operator's
    assert given["assignee"] == "a2" and given["help_by"] is None
    assert "to a2" in _mail("a1")[-1] and "You were assigned card" in _mail("a2")[-1]
    assert any("help call closed: given to a2" in e["body"] for e in given["events"])
    with pytest.raises(ValueError, match="not asking"):
        board.claim("b1", world["card"])
