"""A blocked card says why: unclear, waiting or cannot. Each is handled differently:
unclear is reminded to consult and try, waiting is watched not re-asked, and cannot is
a person's to hear about at once."""

from types import SimpleNamespace

import pytest

from backend import auth, board, board_mcp, mesh, notify, opstore, policy, store, workflow
from backend.schemas import Agent, Host

DAY = 86400


def _agent(id_):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1",
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": True}})


@pytest.fixture
def crew(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("alpha"), _agent("bravo")):
        store.upsert_agent(agent)
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n', "test")
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"op@example.com"}))
    monkeypatch.setattr(workflow.notify, "workflow_created", lambda *a, **k: None)
    monkeypatch.setattr(workflow.notify, "workflow_answered", lambda *a, **k: None)
    emails = []
    monkeypatch.setattr(notify, "_tell", lambda to, subject, body: emails.append((sorted(to), subject, body)))
    return emails


def _request(card_id):
    binding = workflow.plan_binding({"card": card_id})
    return workflow.create(card_id=card_id, kind="decision", requester="beacon", eligible_responder="beacon",
                           capability="ask", tier=1, title="Pick", summary="Pick one", options=["a"], target={}, **binding), binding


def _quiet(card_id, at=0.0):
    """As if the card last changed `at`, and nothing happened since."""
    with opstore.connect() as db:
        db.execute("UPDATE cards SET created_at = ?, updated_at = ? WHERE id = ?", (at, at, card_id))
        db.execute("DELETE FROM mailbox")


def _blocked(reason, waiting_on=None, assignee="alpha", at=0.0):
    card = board.create_card("beacon", "Fix backup", assignee=assignee)
    board.update_card("beacon", card["id"], state="blocked", blocked_reason=reason, waiting_on=waiting_on)
    _quiet(card["id"], at)
    return card["id"]


def _reminders(agent):
    with opstore.connect() as db:
        return [r["body"] for r in db.execute("SELECT body FROM mailbox WHERE recipient = ? AND body LIKE"
                                               " 'Reminder from the Beacon board%' ORDER BY id", (agent,))]


# --- what a blocked card must say -------------------------------------------------------


def test_a_card_cannot_move_to_blocked_without_a_reason(fleet):
    card = board.create_card("beacon", "Fix backup")
    for kwargs in ({}, {"blocked_reason": "stuck"}, {"blocked_reason": ""}):
        with pytest.raises(ValueError, match="blocked_reason"):
            board.update_card("beacon", card["id"], state="blocked", **kwargs)
    assert board.get_card(card["id"])["state"] == "todo"


def test_unclear_needs_nothing_more_and_forgets_a_wait(fleet):
    card = board.create_card("beacon", "Fix backup")
    got = board.update_card("beacon", card["id"], state="blocked", blocked_reason="unclear", waiting_on="ignored")
    assert (got["state"], got["blocked_reason"], got["waiting_on"]) == ("blocked", "unclear", None)


@pytest.mark.parametrize("reason", ["waiting", "cannot"])
def test_waiting_and_cannot_say_what(fleet, reason):
    card = board.create_card("beacon", "Fix backup")
    with pytest.raises(ValueError, match="waiting_on"):
        board.update_card("beacon", card["id"], state="blocked", blocked_reason=reason)
    with pytest.raises(ValueError, match="at most 200"):
        board.update_card("beacon", card["id"], state="blocked", blocked_reason=reason, waiting_on="x" * 201)


def test_waiting_on_a_person_goes_through_a_workflow_on_this_card(crew):
    card, other = board.create_card("beacon", "One"), board.create_card("beacon", "Two")
    mine, _ = _request(card["id"])
    theirs, _ = _request(other["id"])

    for person in ("dana@example.com", "human", "HUMAN"):
        with pytest.raises(ValueError, match="workflow request"):
            board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on=person)
    with pytest.raises(ValueError, match="not a request on this card"):
        board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on=f"workflow:{theirs['id']}")
    with pytest.raises(ValueError, match="not a request on this card"):
        board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on="workflow:999")
    with pytest.raises(ValueError, match="workflow:<id>"):
        board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on="workflow:abc")

    ok = board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on=f"workflow:{mine['id']}")
    assert ok["waiting_on"] == f"workflow:{mine['id']}"


def test_a_workflow_that_has_ended_is_nothing_to_wait_for(crew):
    card = board.create_card("beacon", "One")
    request, binding = _request(card["id"])
    workflow.respond(request["id"], actor="beacon", human=True, action="rejected", response={}, **binding)
    with pytest.raises(ValueError, match="nothing left to wait for"):
        board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on=f"workflow:{request['id']}")


def test_an_agent_or_something_outside_beacon_is_a_fine_thing_to_wait_for(fleet):
    card = board.create_card("beacon", "Fix backup")
    board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on="bravo")
    got = board.update_card("beacon", card["id"], blocked_reason="waiting", waiting_on="CI run on the forge")
    assert got["waiting_on"] == "CI run on the forge" and got["state"] == "blocked"


def test_a_new_reason_needs_its_own_wait_and_leaving_blocked_clears_both(fleet):
    card = board.create_card("beacon", "Fix backup")
    board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on="bravo")
    with pytest.raises(ValueError, match="waiting_on"):
        board.update_card("beacon", card["id"], blocked_reason="cannot")  # bravo is not what is missing
    changed = board.update_card("beacon", card["id"], blocked_reason="cannot", waiting_on="write access to the vault")
    assert (changed["blocked_reason"], changed["waiting_on"]) == ("cannot", "write access to the vault")

    done = board.update_card("beacon", card["id"], state="doing")
    assert (done["blocked_reason"], done["waiting_on"]) == (None, None)
    with pytest.raises(ValueError, match="belong to a blocked card"):
        board.update_card("beacon", card["id"], blocked_reason="unclear")


def test_the_history_says_why_once(fleet):
    card = board.create_card("beacon", "Fix backup")
    board.update_card("beacon", card["id"], state="blocked", blocked_reason="waiting", waiting_on="bravo",
                      note="needs the key", context_id="ctx-1")
    board.update_card("beacon", card["id"], waiting_on="charlie")
    board.update_card("beacon", card["id"], state="doing")

    events = [(e["kind"], e["body"], e["context_id"]) for e in board.get_card(card["id"])["events"][1:]]
    assert events == [
        ("state", "todo -> blocked: needs the key", "ctx-1"),
        ("edit", "blocked: waiting, bravo: needs the key", "ctx-1"),
        ("edit", "waiting on: charlie", None),
        ("state", "blocked -> doing", None),
    ]


def test_a_card_blocked_before_reasons_existed_keeps_working(fleet):
    card = board.create_card("beacon", "Old card", state="blocked")
    board.update_card("beacon", card["id"], note="still waiting on the vault")
    board.update_card("beacon", card["id"], next_action="ask again")
    old = board.get_card(card["id"])
    assert (old["state"], old["blocked_reason"]) == ("blocked", None)
    classified = board.update_card("beacon", card["id"], blocked_reason="unclear")
    assert classified["blocked_reason"] == "unclear"


def test_rest_and_mcp_ask_for_the_reason_too(client, fleet, monkeypatch):
    card = board.create_card("beacon", "Fix backup")
    url = f"/api/board/cards/{card['id']}"
    assert client.patch(url, json={"state": "blocked"}).status_code == 400
    ok = client.patch(url, json={"state": "blocked", "blocked_reason": "cannot", "waiting_on": "write access to the vault"})
    assert ok.status_code == 200 and ok.json()["blocked_reason"] == "cannot"

    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(_agent("reader"))
    monkeypatch.setattr(board_mcp, "author_for", lambda token: "reader")
    ctx = SimpleNamespace(headers={"authorization": "Bearer t"})
    tool = board_mcp.build()._tool_manager.get_tool("card_update").fn
    with pytest.raises(ValueError, match="blocked_reason"):
        tool(ctx=ctx, card_id=card["id"], state="done", blocked_reason="unclear")
    moved = tool(ctx=ctx, card_id=card["id"], blocked_reason="waiting", waiting_on="bravo")
    assert (moved["blocked_reason"], moved["waiting_on"]) == ("waiting", "bravo")


# --- what Beacon does about each reason -------------------------------------------------


def test_an_unclear_card_is_reminded_to_consult_then_people_hear(crew):
    card = _blocked("unclear")
    board.remind("alpha", now=1.1 * DAY)
    first, = _reminders("alpha")
    assert f"#{card} " in first and "challenge your approach" in first and "never for work you can do yourself" in first
    board.remind("alpha", now=2.2 * DAY)
    board.remind("bravo", now=3.3 * DAY)
    assert len(_reminders("alpha")) == 2
    assert len(crew) == 1 and "alpha was reminded and nothing changed" in crew[0][2]


def test_a_card_waiting_on_an_agent_reminds_that_agent_not_its_owner(crew):
    card = _blocked("waiting", "bravo", assignee="alpha")
    board.remind("alpha", now=1.1 * DAY)
    assert _reminders("alpha") == []
    board.remind("bravo", now=1.1 * DAY)
    notice, = _reminders("bravo")
    assert f"#{card} " in notice and "it waits on you (assigned to alpha)" in notice


def test_a_card_waiting_on_an_open_request_is_left_to_the_request(crew):
    card = board.create_card("beacon", "Fix backup", assignee="alpha")["id"]
    request, binding = _request(card)
    board.update_card("beacon", card, state="blocked", blocked_reason="waiting", waiting_on=f"workflow:{request['id']}")
    _quiet(card)

    board.remind("alpha", now=5 * DAY)
    assert _reminders("alpha") == [] and crew == []

    workflow.respond(request["id"], actor="beacon", human=True, action="rejected", response={}, **binding)
    _quiet(card)
    board.remind("alpha", now=5 * DAY)
    notice, = _reminders("alpha")
    assert f"workflow:{request['id']}, which has ended" in notice


def test_a_card_waiting_on_something_outside_is_chased_by_people_again_and_again(crew):
    _blocked("waiting", "CI run on the forge")
    board.remind("alpha", now=1.1 * DAY)
    assert _reminders("alpha") == []
    assert len(crew) == 1 and "it has waited 1 d on CI run on the forge" in crew[0][2]
    board.remind("alpha", now=1.6 * DAY)
    assert len(crew) == 1                          # not before another remind_after
    board.remind("bravo", now=2.2 * DAY)
    assert len(crew) == 2 and "it has waited 2 d on CI run on the forge" in crew[1][2]


def test_a_card_that_cannot_go_on_reaches_people_at_once_and_is_repeated(crew):
    card = _blocked("cannot", "write access to the vault")
    board.remind("bravo", now=0.01 * DAY)          # no waiting for a remind_after
    assert _reminders("alpha") == []
    assert len(crew) == 1
    assert crew[0][0] == ["op@example.com"] and "lacks: write access to the vault" in crew[0][2]
    board.remind("bravo", now=0.5 * DAY)
    assert len(crew) == 1
    board.remind("bravo", now=1.2 * DAY)
    assert len(crew) == 2
    kinds = [e["kind"] for e in board.get_card(card)["events"] if e["kind"] in ("reminder", "escalation")]
    assert kinds == ["escalation", "escalation"]


def test_any_change_to_a_cannot_card_starts_over(crew):
    card = _blocked("cannot", "write access to the vault")
    board.remind("bravo", now=0.01 * DAY)
    board.update_card("beacon", card, note="asked the operator")
    with opstore.connect() as db:
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (0.3 * DAY, card))
    board.remind("bravo", now=0.31 * DAY)          # it changed after people were told: told again, at once
    assert len(crew) == 2
