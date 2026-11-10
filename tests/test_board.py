"""Fleet board: cards with history, authored by whoever changed them; agents
reach it over MCP at /board/mcp with their own token; assignment notifies
through the mailbox; the host sync wires the MCP server only for agents
with reach.board."""

import types
import json

import pytest
from fastapi.testclient import TestClient

from backend import auth, board, mailbox, mesh, opstore, store
from backend.app import app
from backend.schemas import Agent, Host

PUBLIC = "http://beacon.test:8642"


@pytest.fixture
def fleet_agents(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, PUBLIC)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent_id, on_board in (("alpha", True), ("bravo", False)):
        store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": "h1",
            "reach": {"endpoint": f"http://{agent_id}.test:9900", "network": "tailnet", "board": on_board}}))
    delivered = []
    monkeypatch.setattr(mailbox, "deliver_pending", delivered.append)
    return delivered


def _notices():
    with opstore.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM mailbox WHERE kind = 'notice' ORDER BY id")]


def _wait_for(predicate, timeout=3.0):
    import time
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


# --- REST, as the UI uses it ------------------------------------------------


def test_card_lifecycle_records_every_change(client, fleet_agents):
    card = client.post("/api/board/cards", json={"title": "Rotate keys", "project": "ops"}).json()
    assert card["state"] == "todo" and card["created_by"] == "beacon"

    client.patch(f"/api/board/cards/{card['id']}", json={"state": "doing", "note": "starting now"})
    client.post(f"/api/board/cards/{card['id']}/comments", json={"text": "half done", "context_id": "ctx-9"})
    done = client.patch(f"/api/board/cards/{card['id']}", json={"state": "done"}).json()

    assert [(e["kind"], e["body"]) for e in done["events"]] == [
        ("created", "Rotate keys"), ("state", "todo -> doing: starting now"),
        ("comment", "half done"), ("state", "doing -> done")]
    assert done["events"][2]["context_id"] == "ctx-9"
    assert client.get("/api/board").json() == []  # done cards are hidden by default
    assert [c["id"] for c in client.get("/api/board", params={"include_done": True}).json()] == [card["id"]]


def test_board_validates_input(client, fleet_agents):
    assert client.post("/api/board/cards", json={"title": "  "}).status_code == 400
    card = client.post("/api/board/cards", json={"title": "x"}).json()
    assert client.patch(f"/api/board/cards/{card['id']}", json={"state": "someday"}).status_code == 400
    assert client.get("/api/board/cards/999").status_code == 404


def test_card_says_what_moves_it_and_where_it_points(client, fleet_agents):
    plain = client.post("/api/board/cards", json={"title": "x"}).json()
    assert (plain["next_action"], plain["done_when"], plain["links"]) == (None, None, [])

    card = client.post("/api/board/cards", json={
        "title": "Ship the fix", "next_action": " open the PR ", "done_when": "merged and tagged",
        "links": [{"url": "https://git.example/org/repo/pulls/12", "label": "PR #12"}]}).json()
    assert card["next_action"] == "open the PR" and card["done_when"] == "merged and tagged"
    url = f"/api/board/cards/{card['id']}"

    changed = client.patch(url, json={"next_action": "wait for CI", "links": [], "note": "PR open"}).json()
    assert changed["next_action"] == "wait for CI" and changed["links"] == []
    assert [(e["kind"], e["body"]) for e in changed["events"][1:]] == [
        ("edit", "next action: wait for CI: PR open"), ("edit", "links changed: PR open")]

    cleared = client.patch(url, json={"done_when": ""}).json()
    assert cleared["done_when"] is None and cleared["events"][-1]["body"] == "done when: -"
    same = client.patch(url, json={"next_action": "wait for CI", "links": []}).json()
    assert len(same["events"]) == len(cleared["events"])  # nothing changed, nothing recorded


@pytest.mark.parametrize("links", [
    [{"url": "javascript:alert(1)"}], [{"url": "data:text/html,x"}], [{"url": "https://"}],
    [{"url": "https://ok.example", "label": 5}], ["https://ok.example"],
    [{"url": f"https://ok.example/{i}"} for i in range(board.MAX_LINKS + 1)],
])
def test_links_must_be_web_urls(client, fleet_agents, links):
    card = client.post("/api/board/cards", json={"title": "x"}).json()
    # 422: the request model already refuses a non-object; 400: board._links refuses the rest.
    assert client.post("/api/board/cards", json={"title": "y", "links": links}).status_code in (400, 422)
    assert client.patch(f"/api/board/cards/{card['id']}", json={"links": links}).status_code in (400, 422)
    with pytest.raises(ValueError):
        board.update_card("alpha", card["id"], links=links)  # what MCP calls, without the request model
    assert board.get_card(card["id"])["links"] == []


def test_notice_and_reminder_carry_the_next_action(client, fleet_agents):
    card = board.create_card("beacon", "Check backups", assignee="bravo",
                             next_action="run the restore test", done_when="a restore worked")
    notice, = _notices()
    assert "Next action: run the restore test" in notice["body"] and "Done when: a restore worked" in notice["body"]
    text = board._reminder_text([board.get_card(card["id"])], card["updated_at"] + 90000)
    assert "Next action on the card: run the restore test" in text


def test_cards_from_before_the_plan_fields_get_them(fleet, monkeypatch, tmp_path):
    import sqlite3
    monkeypatch.setattr(opstore, "STATE_DIR", tmp_path / "old")
    (tmp_path / "old").mkdir()
    with sqlite3.connect(tmp_path / "old" / "beacon.db") as db:
        db.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                   " body TEXT NOT NULL DEFAULT '', project TEXT, assignee TEXT, state TEXT NOT NULL,"
                   " created_by TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        db.execute("INSERT INTO cards (title, state, created_by, created_at, updated_at)"
                   " VALUES ('old', 'todo', 'beacon', 1, 1)")
    card = board.get_card(1)
    assert (card["next_action"], card["done_when"], card["links"]) == (None, None, [])


def test_assigning_an_agent_sends_it_a_notice(client, fleet_agents):
    card = client.post("/api/board/cards", json={"title": "Check backups", "body": "All hosts.",
                                                 "assignee": "bravo"}).json()

    notice, = _notices()
    assert notice["recipient"] == "bravo" and notice["sender"] == "beacon"
    assert f"card #{card['id']}" in notice["body"] and "All hosts." in notice["body"]
    assert _wait_for(lambda: fleet_agents == ["bravo"])


def test_no_notice_for_people_or_self_assignment(client, fleet_agents):
    client.post("/api/board/cards", json={"title": "x", "assignee": "someone@example.com"})
    board.create_card("alpha", "mine", assignee="alpha")
    assert _notices() == []


def test_a_comment_on_an_assigned_card_reaches_the_assignee(client, fleet_agents):
    card = client.post("/api/board/cards", json={"title": "Check backups", "assignee": "bravo"}).json()
    assert len(_notices()) == 1  # the assignment

    client.post(f"/api/board/cards/{card['id']}/comments", json={"text": "Skip the NAS."})
    board.update_card("beacon", card["id"], note="Also skip the lab host.")  # a note alone is a comment too
    notices = _notices()
    assert [n["recipient"] for n in notices] == ["bravo"] * 3
    assert "Skip the NAS." in notices[1]["body"] and "Also skip the lab host." in notices[2]["body"]
    kinds = [e["kind"] for e in board.get_card(card["id"])["events"]]
    assert kinds.count("comment_notice") == 2

    board.comment("bravo", card["id"], "On it.")  # the assignee's own words are not sent back
    board.comment("alpha", board.create_card("alpha", "unassigned")["id"], "hello")  # nobody to tell
    assert len(_notices()) == 3


# --- MCP, as agents use it --------------------------------------------------

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _mcp(c, method, params=None, token=None, req_id=1):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return c.post("/board/mcp", headers=headers,
                  json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})


def _tool(c, name, arguments, token):
    _mcp(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "0"}}, token)
    response = _mcp(c, "tools/call", {"name": name, "arguments": arguments}, token, req_id=2)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_agent_writes_to_the_board_as_itself(fleet_agents):
    with TestClient(app) as c:
        token = mesh.token("alpha", mesh.BEACON_PEER)
        card = _tool(c, "card_create", {"title": "Plan migration", "assignee": "bravo"}, token)
        assert card["created_by"] == "alpha"
        tools = _mcp(c, "tools/list", token=token).json()["result"]["tools"]

        assert sorted(t["name"] for t in tools) == [
            "agent_owner", "board_card", "board_list", "card_comment", "card_create", "card_update", "enroll_claim", "enroll_request",
            "workflow_create", "workflow_get", "workflow_list", "workflow_respond", "workflow_revise",
        ]
    notice, = _notices()
    assert notice["sender"] == "alpha" and notice["recipient"] == "bravo"

    with TestClient(app) as c:
        updated = _tool(c, "card_update", {"card_id": card["id"], "next_action": "draft the plan",
                                           "links": [{"url": "https://git.example/issues/3", "label": "issue"}]}, token)
    assert updated["next_action"] == "draft the plan" and updated["links"][0]["label"] == "issue"


def test_board_mcp_refuses_strangers_and_other_tokens(fleet_agents, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")
    with TestClient(app) as c:
        assert _mcp(c, "tools/list").status_code == 401
        assert _mcp(c, "tools/list", token="wrong").status_code == 401
        # An agent's A2A token is not its board token.
        assert _mcp(c, "tools/list", token=mesh.token("alpha", "bravo")).status_code == 401
        # bravo is not opted in (reach.board false): its own pull token is refused too.
        assert _mcp(c, "tools/list", token=mesh.token("bravo", mesh.BEACON_PEER)).status_code == 401
        card = _tool(c, "card_create", {"title": "from the operator"}, "test-mcp-token")
    assert card["created_by"] == "beacon"


# --- mesh config and host sync ----------------------------------------------


def test_board_access_is_opt_in(fleet_agents):
    assert mesh.config("alpha")["board"] == {"url": f"{PUBLIC}/board/mcp"}
    assert mesh.config("bravo")["board"] is None


def test_sync_wires_the_board_server_and_restarts_once(fleet_agents, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "hermes_mesh_sync", Path(__file__).parents[1] / "backend" / "drivers" / "hermes_mesh_sync.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    calls = []
    # sync.subprocess is the global module: patch through monkeypatch so it is restored.
    monkeypatch.setattr(sync.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(sync, "restart", lambda cfg, state: calls.append(["RESTART"]))

    beacon_dir = tmp_path / "beacon" / "mesh"
    beacon_dir.mkdir(parents=True)
    state = tmp_path / "state-dir"
    state.mkdir()
    (state / "token").write_text(mesh.token("alpha", mesh.BEACON_PEER))
    env = tmp_path / ".env"
    (state / "sync.json").write_text(json.dumps({"beacon": f"file://{tmp_path / 'beacon'}", "agent": "alpha",
                                                 "hermes": ["hermes"], "env_file": str(env)}))

    def serve(cfg):
        cfg = {**cfg, "beacon": f"file://{tmp_path / 'beacon'}"}
        (beacon_dir / "alpha").write_text(json.dumps(cfg))

    serve(mesh.config("alpha"))
    sync.sync(state)
    board_set = [c for c in calls if "mcp_servers.beacon-board" in c]
    assert len(board_set) == 1 and json.loads(board_set[0][-1]) == {
        "url": f"{PUBLIC}/board/mcp", "headers": {"Authorization": "Bearer ${env:BEACON_BOARD_TOKEN}"}}
    assert f"BEACON_BOARD_TOKEN={mesh.token('alpha', mesh.BEACON_PEER)}" in env.read_text()
    assert calls.count(["RESTART"]) == 1

    calls.clear()
    sync.sync(state)
    assert calls == []  # nothing changed, nothing done

    cfg = mesh.config("alpha")
    cfg["board"] = None
    serve(cfg)
    sync.sync(state)
    disabled = [c for c in calls if "mcp_servers.beacon-board" in c]
    assert json.loads(disabled[0][-1])["enabled"] is False
    assert "BEACON_BOARD_TOKEN" not in env.read_text()
    assert calls.count(["RESTART"]) == 1


def test_agent_asks_a_person_without_inventing_a_plan_hash(fleet_agents):
    from backend import workflow

    with TestClient(app) as c:
        token = mesh.token("alpha", mesh.BEACON_PEER)
        card = _tool(c, "card_create", {"title": "Clean up logs"}, token)
        ask = {"card_id": card["id"], "kind": "decision", "eligible_responder": "human", "capability": "ask", "tier": 1,
               "title": "Which logs?", "summary": "Delete logs older than 30 days, or 90?", "options": ["30", "90"]}
        request = _tool(c, "workflow_create", ask, token)
        # Beacon bound the request to its own text, so a response must name that plan.
        expected = workflow.plan_binding({"capability": "ask", "tier": 1, "title": "Which logs?",
                                          "summary": ask["summary"], "options": ["30", "90"], "target": {}})
        assert (request["plan_revision"], request["plan_hash"]) == (expected["plan_revision"], expected["plan_hash"])
        assert request["requester"] == "alpha" and request["state"] == "pending"

        # Half a binding is a mistake, not a default.
        _mcp(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "0"}}, token)
        half = _mcp(c, "tools/call", {"name": "workflow_create", "arguments": {**ask, "plan_hash": "abc"}}, token, req_id=3)
        assert half.json()["result"]["isError"] is True
