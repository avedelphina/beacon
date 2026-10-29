"""Chat exposure: the host sync reports which settings open an agent's chat to
anyone (names only), with every pull; Beacon withholds the fleet board from
such an agent on its own, in the mesh config and at /board/mcp. An older
sync reports nothing, and then reach.board stands."""

import importlib.util
import json
import types
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import mesh, store
from backend.app import app
from backend.schemas import Agent, Host

SYNC_PATH = Path(__file__).parents[1] / "backend" / "drivers" / "hermes_mesh_sync.py"
PUBLIC = "http://beacon.test:8642"


@pytest.fixture
def sync():
    spec = importlib.util.spec_from_file_location("hermes_mesh_sync", SYNC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def on_board(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, PUBLIC)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(Agent.model_validate({"id": "alpha", "type": "hermes", "host": "h1",
        "reach": {"endpoint": "http://alpha.test:9900", "network": "tailnet", "board": True}}))


# --- what the sync sees -----------------------------------------------------


def test_sync_finds_allow_all_flags_in_env_and_config(sync, tmp_path):
    env = tmp_path / ".env"
    env.write_text("DELTACHAT_ALLOW_ALL_USERS=true\nexport GATEWAY_ALLOW_ALL_USERS=\"Yes\"\n"
                   "TELEGRAM_ALLOW_ALL_USERS=false\nTELEGRAM_ALLOWED_USERS=123\n# SLACK_ALLOW_ALL_USERS=true\n")
    cfg = {"env_file": str(env), "hermes": ["/usr/bin/hermes"]}
    assert sync.open_chats(cfg) == ["DELTACHAT_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS"]

    env.write_text("TELEGRAM_ALLOWED_USERS=123\n")
    assert sync.open_chats(cfg) == []
    (tmp_path / "config.yaml").write_text("gateway:\n  allow_all_users: true  # open\n")
    assert sync.open_chats(cfg) == ["allow_all_users"]


def test_sync_reads_a_docker_agents_container_environment(sync, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sync.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="HOME=/opt/data\nDELTACHAT_ALLOW_ALL_USERS=1\n", stderr=""))
    cfg = {"env_file": str(tmp_path / ".env"),
           "hermes": ["/usr/bin/docker", "exec", "-u", "hermes", "nas-agent", "hermes"]}
    assert sync.open_chats(cfg) == ["DELTACHAT_ALLOW_ALL_USERS"]
    assert calls == [["/usr/bin/docker", "exec", "-u", "hermes", "nas-agent", "env"]]


def test_sync_sends_the_report_with_its_pull(sync, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "token").write_text("tok")
    env = tmp_path / ".env"
    env.write_text("GATEWAY_ALLOW_ALL_USERS=true\n")
    (state / "sync.json").write_text(json.dumps({"beacon": "http://beacon.test", "agent": "alpha",
                                                 "hermes": ["hermes"], "env_file": str(env)}))
    sent = []

    def urlopen(request, timeout=None):
        sent.append((request.get_header("X-beacon-open-chat"), request.get_header("X-beacon-restart")))
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(sync.urllib.request, "urlopen", urlopen)
    sync.sync(state)
    env.write_text("")
    (state / "restart.pending").touch()  # an earlier run asked for a restart that has not gone through
    sync.sync(state)
    assert sent == [("GATEWAY_ALLOW_ALL_USERS", "none"), ("none", "pending")]


# --- what Beacon does with it -----------------------------------------------


def _pull(c, report=None):
    headers = {"Authorization": f"Bearer {mesh.token('alpha', mesh.BEACON_PEER)}", "X-Beacon-Sync-Version": "2"}
    if report is not None:
        headers["X-Beacon-Open-Chat"] = report
    response = c.get("/mesh/alpha", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _board_status(c):
    return c.post("/board/mcp", headers={"Authorization": f"Bearer {mesh.token('alpha', mesh.BEACON_PEER)}",
                                         "Accept": "application/json, text/event-stream",
                                         "Content-Type": "application/json"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).status_code


def test_an_open_chat_withholds_the_board_until_it_is_locked(on_board, caplog):
    with TestClient(app) as c:
        assert _pull(c)["board"] is not None  # an older sync: reach.board stands
        assert c.get("/api/agents/alpha").json()["chat_open"] is None

        assert _pull(c, "DELTACHAT_ALLOW_ALL_USERS")["board"] is None
        assert _board_status(c) == 401
        assert "board withheld from alpha" in caplog.text
        assert c.get("/api/agents").json()[0]["chat_open"] == ["DELTACHAT_ALLOW_ALL_USERS"]

        assert _pull(c, "none")["board"] == {"url": f"{PUBLIC}/board/mcp"}
        assert _board_status(c) == 200
        assert c.get("/api/agents/alpha").json()["chat_open"] == []


def test_a_garbled_report_still_counts_as_open(on_board):
    mesh.record_open_chat("alpha", "<script>, ")
    assert mesh.open_chats() == {"alpha": ["unrecognized"]}
    assert not mesh.board_allowed(store.get_agent("alpha"))
