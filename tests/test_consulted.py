"""An agent that asks a person says whether it asked a peer first. A request with neither
the consultation nor a reason for skipping it is flagged for the person, never refused."""

import sqlite3
from types import SimpleNamespace

import pytest

from backend import auth, board, board_mcp, notify, opstore, policy, store, workflow
from backend.schemas import Agent, Host


def _agent(id_):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1",
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": True}})


@pytest.fixture
def crew(fleet, monkeypatch):
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("asker"), _agent("peer")):
        store.upsert_agent(agent)
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n', "test")
    monkeypatch.setattr(workflow.notify, "workflow_created", lambda *a, **k: None)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"dana@example.com"}))
    return board.create_card("beacon", "Rotate keys")["id"]


def _ask(card, requester="asker", responder="human", kind="decision", **extra):
    options = ["a", "b"] if kind == "decision" else []
    binding = workflow.plan_binding({"card": card, "kind": kind})
    return workflow.create(card_id=card, kind=kind, requester=requester, eligible_responder=responder,
                           capability="ask", clearance=1, title="Which?", summary="Pick", options=options, target={},
                           **extra, **binding)


def _history(card):
    return [e["body"] for e in board.get_card(card)["events"] if e["kind"] == "workflow"]


def test_an_agent_asking_a_person_with_no_consultation_is_flagged_not_refused(crew):
    request = _ask(crew)
    assert request["state"] == "pending" and request["consult_flag"] == "missing"
    assert request["consulted"] == [] and request["consult_skipped"] is None
    assert _history(crew)[0].endswith(": asked human (no peer consulted)")
    assert _ask(crew, responder="dana@example.com")["consult_flag"] == "missing"


def test_a_consultation_is_kept_and_clears_the_flag(crew):
    request = _ask(crew, consulted=[{"agent": " peer ", "said": "use the canary", "context_id": "ctx-9"}],
                   context_id="ctx-1")
    assert request["consult_flag"] is None
    assert request["consulted"] == [{"agent": "peer", "said": "use the canary", "context_id": "ctx-9"}]
    assert _history(crew)[0].endswith(": asked human (after consulting peer)")
    assert workflow.get(request["id"])["consulted"][0]["said"] == "use the canary"


def test_saying_why_not_also_clears_the_flag(crew):
    request = _ask(crew, consult_skipped="a plain yes or no; nobody can know the answer")
    assert request["consult_flag"] is None and request["consult_skipped"].startswith("a plain yes or no")
    assert "no peer consulted: a plain yes or no" in _history(crew)[0]


def test_only_an_agent_asking_a_person_about_more_than_a_question_is_ever_flagged(crew):
    assert _ask(crew, kind="question")["consult_flag"] is None      # a plain question is exempt
    assert _ask(crew, responder="peer")["consult_flag"] is None     # asking a peer is the consultation
    assert _ask(crew, requester="beacon")["consult_flag"] is None   # not an agent
    assert _ask(crew, requester="dana@example.com", responder="human")["consult_flag"] is None  # a person asks


@pytest.mark.parametrize("bad", [
    "peer", [{"agent": "peer"}], [{"said": "x"}], [{"agent": "", "said": "x"}], [{"agent": "peer", "said": " "}],
    [{"agent": "p", "said": "x", "context_id": 5}], [{"agent": "p", "said": "x"}] * 6, ["peer"]])
def test_a_malformed_consultation_is_refused(crew, bad):
    with pytest.raises(workflow.WorkflowError):
        _ask(crew, consulted=bad)


def test_a_long_reason_is_refused_and_a_long_answer_is_cut(crew):
    with pytest.raises(workflow.WorkflowError):
        _ask(crew, consult_skipped="x" * 301)
    request = _ask(crew, consulted=[{"agent": "peer", "said": "y" * 900}])
    assert len(request["consulted"][0]["said"]) == 500


def test_the_person_is_told_in_the_email_too(crew):
    flagged, with_peer, skipped = _ask(crew), _ask(crew, consulted=[{"agent": "peer", "said": "ok"}]), \
        _ask(crew, consult_skipped="nobody to ask")
    assert "did not consult a peer before asking you" in notify._created_body(flagged)
    assert "consulted: peer." in notify._created_body(with_peer)
    assert "did not consult a peer: nobody to ask" in notify._created_body(skipped)
    assert "consult" not in notify._created_body(_ask(crew, kind="question"))


def test_rest_and_mcp_take_it(crew, client, monkeypatch):
    posted = client.post(f"/api/board/cards/{crew}/workflows", json={
        "kind": "decision", "eligible_responder": "human", "capability": "ask", "clearance": 1, "title": "Which?",
        "summary": "Pick", "options": ["a"], "plan_revision": "r", "plan_hash": "h",
        "consulted": [{"agent": "peer", "said": "go"}], "consult_skipped": None})
    assert posted.status_code == 200 and posted.json()["consulted"][0]["agent"] == "peer"

    monkeypatch.setattr(board_mcp, "author_for", lambda token: "asker")
    ctx = SimpleNamespace(headers={"authorization": "Bearer t"})
    create = board_mcp.build()._tool_manager.get_tool("workflow_create").fn
    ask = dict(ctx=ctx, card_id=crew, kind="decision", eligible_responder="human", capability="ask", clearance=1,
               title="Which?", summary="Pick", options=["a"])
    assert create(**ask)["consult_flag"] == "missing"
    assert create(**ask, consulted=[{"agent": "peer", "said": "go"}])["consult_flag"] is None
    assert create(**ask, consult_skipped="plain choice")["consult_skipped"] == "plain choice"


def test_requests_from_before_this_field_get_it(fleet, monkeypatch, tmp_path):
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
                   " VALUES (1, 'decision', 'pending', 'a', 'human', 'ask', 1, 't', 's', 'r', 'h', 9e9, 1, 1)")
    old = workflow.get(1)
    assert (old["consulted"], old["consult_skipped"], old["consult_flag"]) == ([], None, None)
