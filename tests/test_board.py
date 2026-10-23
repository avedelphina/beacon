"""Fleet board: cards with history, authored by whoever changed them; agents
reach it over MCP at /board/mcp with their own token; assignment notifies
through the mailbox; the host sync wires the MCP server only for agents
with reach.board."""

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

    assert sorted(t["name"] for t in tools) == ["board_card", "board_list", "card_comment", "card_create", "card_update"]
    notice, = _notices()
    assert notice["sender"] == "alpha" and notice["recipient"] == "bravo"


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
    monkeypatch.setattr(sync.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
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
