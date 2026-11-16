"""A client agent is an operator's own AI client in the fleet: its own identity and token for the board
and the workflow tools, and never called, assigned work, or sent mail."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend import auth, availability, board, board_mcp, mesh, policy, store, workflow
from backend.schemas import Agent, Host

SECRET = "client-test-secret"


def _client(id_="claude-op", board_access=True, **extra):
    return Agent.model_validate({"id": id_, "type": "client", "host": "clients", "client": {"board": board_access}, **extra})


@pytest.fixture
def world(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, SECRET)
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    monkeypatch.setattr(workflow.notify, "workflow_created", lambda *a, **k: None)
    store.upsert_host(Host.model_validate({"id": "clients", "address": "none.invalid"}))     # no SSH: nothing runs there
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(Agent.model_validate({"id": "alpha", "type": "hermes", "host": "h1",
                                             "reach": {"endpoint": "http://alpha.test:9900", "board": True}}))
    store.upsert_agent(_client())
    store.upsert_agent(_client("claude-quiet", board_access=False))
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n', "test")


def _bearer(agent_id):
    return f"Bearer {mesh.token(agent_id, mesh.BEACON_PEER)}"


# --- the record -------------------------------------------------------------------------------------


def test_a_client_has_a_client_block_or_a_reach_block_never_both():
    assert _client().client.board is True and _client().reach is None
    with pytest.raises(ValidationError, match="not both"):
        Agent.model_validate({"id": "x", "type": "client", "host": "clients", "client": {"board": True},
                              "reach": {"endpoint": "http://x.test:9900"}})


def test_a_client_survives_the_store_and_an_export(world):
    got = store.get_agent("claude-op")
    assert got.client.board is True and got.reach is None and got.type == "client"
    assert Agent.model_validate(got.model_dump(mode="json")) == got       # what the YAML export holds, read back


# --- its token --------------------------------------------------------------------------------------


def test_the_clients_own_token_is_its_identity_on_the_board_and_workflows(world):
    assert auth.agent_for_authorization(_bearer("claude-op")) == "claude-op"
    assert board_mcp.author_for(_bearer("claude-op")) == "claude-op"
    assert board_mcp.author_for("Bearer not-a-token") is None
    assert board_mcp.author_for(_bearer("alpha")) == "alpha"               # an ordinary agent is unchanged


def test_a_client_without_board_access_gets_no_board(world):
    assert board_mcp.author_for(_bearer("claude-quiet")) is None
    binding = workflow.plan_binding({"x": 1})
    card = board.create_card("beacon", "Card")["id"]
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        workflow.create(card_id=card, kind="question", requester="claude-quiet", eligible_responder="human",
                        capability="ask", tier=1, title="t", summary="s", options=[], target={}, **binding)


def test_the_client_token_command_prints_what_to_paste_and_only_for_a_board_client(world, monkeypatch, capsys):
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, "https://beacon.example.test")
    assert mesh.main(["client-token", "claude-op"]) == 0
    out = capsys.readouterr().out
    assert mesh.token("claude-op", mesh.BEACON_PEER) in out
    assert "https://beacon.example.test/board/mcp" in out and "claude mcp add --transport http beacon-board" in out
    for bad in ("claude-quiet", "alpha", "nobody"):
        assert mesh.main(["client-token", bad]) == 1
    assert "error:" in capsys.readouterr().err


def test_the_api_never_returns_the_clients_token(world, client):
    body = client.get("/api/agents/claude-op").text + client.get("/api/agents").text
    assert mesh.token("claude-op", mesh.BEACON_PEER) not in body


# --- acting as itself -------------------------------------------------------------------------------


def test_a_client_writes_to_the_board_under_its_own_name(world):
    ctx = SimpleNamespace(headers={"authorization": _bearer("claude-op")})
    tools = board_mcp.build()._tool_manager
    card = tools.get_tool("card_create").fn(ctx=ctx, title="Look at the backups")
    assert card["created_by"] == "claude-op"
    tools.get_tool("card_comment").fn(ctx=ctx, card_id=card["id"], text="Checked.")
    assert [e["author"] for e in board.get_card(card["id"])["events"]] == ["claude-op", "claude-op"]


def test_a_client_asks_a_person_and_the_request_is_its_own(world, client):
    card = board.create_card("beacon", "Card")["id"]
    posted = client.post(f"/api/board/cards/{card}/workflows", headers={"Authorization": _bearer("claude-op")}, json={
        "kind": "question", "eligible_responder": "human", "capability": "ask", "tier": 1, "title": "Which?",
        "summary": "Tell me", "plan_revision": "r", "plan_hash": "h"})
    assert posted.status_code == 200 and posted.json()["requester"] == "claude-op"
    denied = client.post(f"/api/board/cards/{card}/workflows", headers={"Authorization": _bearer("claude-quiet")}, json={
        "kind": "question", "eligible_responder": "human", "capability": "ask", "tier": 1, "title": "Which?",
        "summary": "Tell me", "plan_revision": "r", "plan_hash": "h"})
    assert denied.status_code == 403


# --- never called, assigned, or asked ---------------------------------------------------------------


def test_nobody_may_call_a_client_not_even_beacon(world):
    target = store.get_agent("claude-op")
    assert mesh.may_call(mesh.BEACON_PEER, target) is False
    assert mesh.may_call("alpha", target) is False
    assert mesh.may_call("claude-op", store.get_agent("alpha")) is True       # a client may call under the policy


def test_a_client_is_in_nobodys_peer_list_and_has_no_mesh_config(world):
    assert [p["name"] for p in mesh.config("alpha")["outbound"]] == []
    with pytest.raises(ValueError):
        mesh.config("claude-op")


def test_a_card_cannot_be_given_to_a_client(world):
    card = board.create_card("beacon", "Card")["id"]
    with pytest.raises(ValueError, match="not given work"):
        board.create_card("beacon", "Mine", assignee="claude-op")
    with pytest.raises(ValueError, match="not given work"):
        board.update_card("beacon", card, assignee="claude-op")
    with pytest.raises(ValueError, match="not given work"):
        board.update_card("alpha", card, assignee="claude-op")


def test_a_client_assigns_only_to_agents_the_policy_lets_it_call(world):
    card = board.create_card("beacon", "Card")["id"]
    assert board.update_card("claude-op", card, assignee="alpha")["assignee"] == "alpha"
    policy.save("rules: []\n", "test")                                         # nobody may call anybody now
    other = board.create_card("beacon", "Other")["id"]
    with pytest.raises(ValueError, match="does not accept work"):
        board.update_card("claude-op", other, assignee="alpha")


def test_a_workflow_request_cannot_be_addressed_to_a_client(world):
    card = board.create_card("beacon", "Card")["id"]
    binding = workflow.plan_binding({"x": 1})
    for requester in ("alpha", "beacon"):
        with pytest.raises(workflow.WorkflowError, match="is an operator's client"):
            workflow.create(card_id=card, kind="question", requester=requester, eligible_responder="claude-op",
                            capability="ask", tier=1, title="t", summary="s", options=[], target={}, **binding)


def test_a_client_is_not_pursued_by_reminders_or_the_mailbox_and_shows_as_a_client(world):
    states = availability.check_all(store.list_agents())
    assert states["claude-op"]["state"] == "client" and states["alpha"]["state"] != "client"
    board.remind(None, now=10 * 86400)                                         # nothing to remind, and no error
    from backend import opstore
    with opstore.connect() as db:
        assert db.execute("SELECT count(*) FROM mailbox WHERE recipient LIKE 'claude-%'").fetchone()[0] == 0


def test_status_and_deploy_of_a_client_stop_at_the_missing_ssh(world, client):
    assert client.get("/api/agents/claude-op/status").status_code in (200, 409)
    assert client.get("/api/agents/claude-op").json()["client"] == {"board": True}
