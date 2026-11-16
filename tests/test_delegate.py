"""A client agent reads what its operator reads and answers, for them, up to T3: never T4, never a
request it or its operator made, never one sent to a person. What it answered is recorded under both names."""

from types import SimpleNamespace

import pytest

from backend import auth, board, board_mcp, clients, mesh, notify, opstore, policy, store, workflow
from backend.schemas import Agent, Host

TOM, ANN = "tom@example.com", "ann@example.com"


def _fleet_agent(id_):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1",
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": True}})


@pytest.fixture
def told(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_tell", lambda to, subject, body: sent.append((sorted(to), subject, body)))
    return sent


@pytest.fixture
def world(fleet, monkeypatch, told):
    monkeypatch.setenv(mesh.SECRET_ENV, "delegate-test-secret")
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM, ANN}))
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for id_ in ("asker", "other"):
        store.upsert_agent(_fleet_agent(id_))
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n', "test")
    clients.create("claude", TOM, 3)
    clients.create("claude", ANN, 3)
    return board.create_card("beacon", "Push the config")["id"]


def _ask(card, responder="human", tier=2, requester="asker", kind=None):
    kind = kind or ("review" if tier == 3 else "approval" if tier >= 4 else "decision")
    binding = workflow.plan_binding({"card": card, "to": responder, "tier": tier, "by": requester, "n": id(object())})
    return workflow.create(card_id=card, kind=kind, requester=requester, eligible_responder=responder,
                           capability="push_config", tier=tier, title=f"Request T{tier} for {responder}",
                           summary="Exact plan", options=["a", "b"] if kind == "decision" else [], target={},
                           **binding), binding


def _answer(request, binding, actor="claude-tom", action="approved", **kw):
    return workflow.respond(request["id"], actor=actor, human=False, action=action,
                            response=kw.pop("response", {}), **binding, **kw)


def _set_client(id_, **changes):
    agent = store.get_agent(id_)
    store.upsert_agent(agent.model_copy(update={"client": agent.client.model_copy(update=changes)}))


# --- reading ---------------------------------------------------------------------------------------


def test_a_client_reads_what_its_operator_reads_and_not_what_another_operator_reads(world):
    mine, _ = _ask(world, TOM)
    shared, _ = _ask(world, "human")
    hers, _ = _ask(world, ANN)
    made, _ = _ask(world, ANN, requester="claude-tom")

    tom_sees = {r["id"] for r in workflow.list_requests(actor="claude-tom")}
    ann_sees = {r["id"] for r in workflow.list_requests(actor="claude-ann")}
    assert tom_sees == {mine["id"], shared["id"], made["id"]}
    assert ann_sees == {hers["id"], shared["id"], made["id"]}
    assert workflow.get(mine["id"], actor="claude-tom")["delegate"]["acting_for"] == TOM
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        workflow.get(hers["id"], actor="claude-tom")
    assert "delegate" not in workflow.get(mine["id"], actor=TOM, human=True)        # a person's view is as it was
    assert {r["id"] for r in workflow.list_requests(actor="asker")} == {mine["id"], shared["id"], hers["id"]}
    assert all("delegate" not in r for r in workflow.list_requests(actor="asker"))


def test_the_view_says_what_the_client_may_answer_and_why_not(world):
    t2, _ = _ask(world, TOM)
    t3, _ = _ask(world, TOM, tier=3)
    t4, _ = _ask(world, TOM, tier=4)
    own, _ = _ask(world, "human", requester="claude-tom")
    operators, _ = _ask(world, "human", requester=TOM)
    notes = {r["id"]: r["delegate"] for r in workflow.list_requests(actor="claude-tom")}

    assert notes[t2["id"]]["can_answer"] and notes[t3["id"]]["can_answer"]
    assert not notes[t4["id"]]["can_answer"] and "tier 4 and above need your operator" in notes[t4["id"]]["why_not"]
    assert "independent reviewer" in notes[own["id"]]["why_not"] and "independent reviewer" in notes[operators["id"]]["why_not"]
    _set_client("claude-tom", max_tier=1)
    assert "ceiling (T1)" in workflow.get(t2["id"], actor="claude-tom")["delegate"]["why_not"]


# --- answering -------------------------------------------------------------------------------------


def test_a_client_answers_for_its_operator_and_both_names_are_recorded(world, told):
    request, binding = _ask(world, TOM, tier=2)
    done = _answer(request, binding, action="choose_option", response={"option": "a"}, context_id="ctx-1")

    assert done["state"] == "approved" and (done["answered_by"], done["on_behalf_of"]) == ("claude-tom", TOM)
    responded = [e for e in done["events"] if e["kind"] == "responded"][0]
    assert responded["actor"] == "claude-tom" and responded["payload"]["on_behalf_of"] == TOM
    history = [e for e in board.get_card(world)["events"] if e["kind"] == "workflow"]
    assert history[-1]["author"] == "claude-tom" and history[-1]["context_id"] == "ctx-1"
    assert f"approved (by claude-tom for {TOM}): a" in history[-1]["body"]
    to, subject, body = next(t for t in told if "answered for you" in t[1])        # the operator hears what was answered
    assert to == [TOM] and "claude-tom" in subject and "You did not see it first" in body
    with opstore.connect() as db:                                                  # and so does the requester
        assert "claude-tom" in db.execute("SELECT body FROM mailbox WHERE recipient = 'asker'").fetchone()["body"]


def test_a_client_answers_a_t3_review_addressed_to_any_operator(world):
    request, binding = _ask(world, "human", tier=3)
    assert _answer(request, binding)["state"] == "approved"
    other, other_binding = _ask(world, "human", tier=3)
    assert _answer(other, other_binding, actor="claude-ann", action="changes_requested",
                   response={"note": "add a canary"})["state"] == "changes_requested"


def test_tier_four_is_not_the_clients_to_answer_and_the_operator_is_told(world, told):
    request, binding = _ask(world, TOM, tier=4)
    told.clear()
    with pytest.raises(workflow.HumanApprovalRequired, match="web page"):
        _answer(request, binding)
    assert workflow.get(request["id"])["state"] == "pending"
    to, subject, body = next(t for t in told if "needs you" in t[1])
    assert to == [TOM] and "tier 4" in subject and "claude-tom saw it and cannot answer" in body.replace("Your client ", "")


def test_a_client_cannot_make_noise_about_a_request_that_is_not_its_operators(world, told):
    request, binding = _ask(world, ANN, tier=4)                       # addressed to the other operator
    told.clear()
    with pytest.raises(workflow.UnauthorizedResponder):
        _answer(request, binding, actor="claude-tom")
    assert told == []


def test_the_ceiling_a_client_was_given_holds(world):
    _set_client("claude-tom", max_tier=1)
    low, low_binding = _ask(world, TOM, tier=1)
    high, high_binding = _ask(world, TOM, tier=2)
    assert _answer(low, low_binding, action="choose_option", response={"option": "a"})["state"] == "approved"
    with pytest.raises(workflow.UnauthorizedResponder, match="ceiling"):
        _answer(high, high_binding, action="choose_option", response={"option": "a"})


def test_it_cannot_approve_what_it_or_its_operator_asked(world):
    own, own_binding = _ask(world, TOM, requester="claude-tom", tier=3)
    theirs, theirs_binding = _ask(world, "human", requester=TOM, tier=3)
    for request, binding in ((own, own_binding), (theirs, theirs_binding)):
        with pytest.raises(workflow.UnauthorizedResponder, match="independent reviewer"):
            _answer(request, binding)
    # Another operator's client is independent of both.
    assert _answer(theirs, theirs_binding, actor="claude-ann")["state"] == "approved"


def test_it_cannot_answer_for_another_operator(world):
    request, binding = _ask(world, ANN, tier=2)
    with pytest.raises(workflow.UnauthorizedResponder, match="not addressed to"):
        _answer(request, binding, action="choose_option", response={"option": "a"})


@pytest.mark.parametrize("change", ["off", "unbound", "operator_gone"])
def test_a_client_that_is_off_unbound_or_whose_operator_is_gone_has_no_powers(world, monkeypatch, change):
    request, binding = _ask(world, TOM, tier=2)
    if change == "off":
        _set_client("claude-tom", enabled=False)
    elif change == "unbound":
        _set_client("claude-tom", acts_for=None)
    else:
        monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({ANN}))
    assert workflow.list_requests(actor="claude-tom") == []
    with pytest.raises(workflow.UnauthorizedResponder):
        _answer(request, binding, action="choose_option", response={"option": "a"})
    assert workflow.get(request["id"])["state"] == "pending"


# --- "I am not sure" -------------------------------------------------------------------------------


def test_a_client_that_is_unsure_sends_it_to_a_person_and_then_only_a_person_answers(world):
    request, binding = _ask(world, TOM, tier=3)
    workflow.escalate(request["id"], actor="claude-tom", human=False, reason="I cannot judge the host change")
    assert workflow.get(request["id"], actor="claude-tom")["delegate"]["can_answer"] is False
    with pytest.raises(workflow.HumanApprovalRequired):
        _answer(request, binding)
    assert workflow.respond(request["id"], actor=TOM, human=True, action="approved", response={}, **binding)["state"] == "approved"
    assert workflow.get(request["id"])["answered_by"] is None                       # a person answered, not the client


def test_a_person_answering_is_unchanged_and_records_no_client(world):
    request, binding = _ask(world, TOM, tier=2)
    done = workflow.respond(request["id"], actor=TOM, human=True, action="choose_option", response={"option": "b"}, **binding)
    assert done["state"] == "approved" and done["answered_by"] is None and done["on_behalf_of"] is None


# --- the doors a client comes through ---------------------------------------------------------------


def test_over_mcp_a_client_lists_and_answers_with_its_own_token(world, monkeypatch):
    request, binding = _ask(world, "human", tier=2)
    token = clients.mint("claude-tom", "test")[1]
    ctx = SimpleNamespace(headers={"authorization": f"Bearer {token}"})
    tools = board_mcp.build()._tool_manager
    listed = tools.get_tool("workflow_list").fn(ctx=ctx)
    assert [r["id"] for r in listed] == [request["id"]] and listed[0]["delegate"]["can_answer"] is True
    done = tools.get_tool("workflow_respond").fn(ctx=ctx, request_id=request["id"], action="choose_option",
                                                 plan_revision=binding["plan_revision"], plan_hash=binding["plan_hash"],
                                                 response={"option": "a"})
    assert done["answered_by"] == "claude-tom"
    sure = _ask(world, TOM, tier=3)[0]
    assert tools.get_tool("workflow_require_human").fn(ctx=ctx, request_id=sure["id"], reason="not sure")["require_human"] is True


def test_over_rest_with_the_clients_token(world, client):
    request, binding = _ask(world, TOM, tier=2)
    headers = {"Authorization": f"Bearer {clients.mint('claude-tom', 'test')[1]}"}
    assert [r["id"] for r in client.get("/api/workflows", headers=headers).json()] == [request["id"]]
    done = client.post(f"/api/workflows/{request['id']}/respond", headers=headers, json={
        "action": "choose_option", "response": {"option": "a"}, **binding})
    assert done.status_code == 200 and done.json()["answered_by"] == "claude-tom"
    t4, t4_binding = _ask(world, TOM, tier=4)
    refused = client.post(f"/api/workflows/{t4['id']}/respond", headers=headers, json={"action": "approved", "response": {}, **t4_binding})
    assert refused.status_code == 403 and workflow.get(t4["id"])["state"] == "pending"


def test_a_t4_email_says_so_and_requests_from_before_get_the_fields(fleet, world, told):
    t4, _ = _ask(world, TOM, tier=4)
    assert "only you, in the web page" in notify._created_body(workflow.get(t4["id"]))
    t2, _ = _ask(world, TOM, tier=2)
    assert "only you, in the web page" not in notify._created_body(workflow.get(t2["id"]))
    assert (workflow.get(t2["id"])["answered_by"], workflow.get(t2["id"])["on_behalf_of"]) == (None, None)
