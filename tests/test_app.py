"""FastAPI TestClient tests — the confirm-gate as callers actually see it
(a plain HTTP client, not a call into backend.drivers.hermes directly), plus
CRUD and error handling. Same fleet/fake_ssh fixtures as the driver tests,
so nothing here touches a real filesystem path outside tmp_path or a real
network.
"""

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.ssh import SSHResult


@pytest.fixture
def client(fleet, fake_ssh):
    return TestClient(app)


@pytest.fixture
def host_and_agent(client):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "desired": {}})
    return "a1"


# ---------------------------------------------------------------------------
# Host / agent CRUD
# ---------------------------------------------------------------------------


def test_host_crud(client):
    r = client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": ["prod"],
    })
    assert r.status_code == 200

    r = client.get("/api/hosts/edge-01")
    assert r.status_code == 200
    assert r.json()["address"] == "10.0.0.1"

    r = client.get("/api/hosts")
    assert len(r.json()) == 1

    r = client.delete("/api/hosts/edge-01")
    assert r.status_code == 200
    assert client.get("/api/hosts/edge-01").status_code == 404


def test_host_put_id_mismatch_returns_400(client):
    r = client.put("/api/hosts/edge-01", json={
        "id": "different-id", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": [],
    })
    assert r.status_code == 400


def test_host_bad_ssh_config_returns_422(client):
    # Both key and config_file set — Pydantic validation, caught before
    # store.py ever sees it.
    r = client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "config_file": "/tbot/ssh_config", "port": 22}, "tags": [],
    })
    assert r.status_code == 422


def test_agent_requires_existing_host(client):
    r = client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "no-such-host", "desired": {}})
    assert r.status_code == 404


def test_agent_crud(client, host_and_agent):
    r = client.get("/api/agents/a1")
    assert r.status_code == 200
    assert r.json()["host"] == "edge-01"

    r = client.delete("/api/agents/a1")
    assert r.status_code == 200
    assert client.get("/api/agents/a1").status_code == 404


def test_get_missing_agent_returns_404(client):
    assert client.get("/api/agents/nope").status_code == 404


# ---------------------------------------------------------------------------
# Tier registry endpoint
# ---------------------------------------------------------------------------


def test_tiers_endpoint(client):
    r = client.get("/api/tiers")
    assert r.status_code == 200
    body = r.json()
    assert body["status"]["tier"] == "T0"
    assert body["decommission"]["tier"] == "T4"


# ---------------------------------------------------------------------------
# Confirm-gate: the whole point of this branch — a plain HTTP call must be
# gated exactly like an MCP client always was, not just the SDK client.
# ---------------------------------------------------------------------------


def test_restart_without_confirm_does_not_touch_ssh(client, host_and_agent, fake_ssh):
    r = client.post("/api/agents/a1/restart")
    assert r.status_code == 200
    body = r.json()
    assert body["would_run"] is True
    assert body["tier"] == "T2"
    assert fake_ssh.calls == []  # the whole point: nothing ran


def test_restart_with_confirm_runs(client, host_and_agent, fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="restarted", stderr="", returncode=0)
    r = client.post("/api/agents/a1/restart?confirm=true")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(fake_ssh.calls) == 1


def test_deploy_without_confirm_does_not_touch_ssh(client, host_and_agent, fake_ssh):
    r = client.post("/api/agents/a1/deploy")
    assert r.status_code == 200
    assert r.json()["would_run"] is True
    assert fake_ssh.calls == []


def test_deploy_with_confirm_runs(client, host_and_agent, fake_ssh):
    fake_ssh.stream_lines = ["[beacon] done", "__BEACON_EXIT__0"]
    r = client.post("/api/agents/a1/deploy?confirm=true")
    assert r.status_code == 200
    assert "[beacon] done" in r.text
    assert len(fake_ssh.calls) == 1


def test_decommission_without_confirm_does_not_touch_ssh(client, host_and_agent, fake_ssh):
    r = client.post("/api/agents/a1/decommission", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["would_run"] is True
    assert body["tier"] == "T4"
    assert fake_ssh.calls == []


def test_decommission_purge_without_confirm_shows_t5(client, host_and_agent, fake_ssh):
    r = client.post("/api/agents/a1/decommission", json={"purge": True})
    assert r.json()["tier"] == "T5"
    assert fake_ssh.calls == []


def test_decommission_with_confirm_runs_and_archives(client, host_and_agent, fake_ssh):
    fake_ssh.stream_lines = ["[beacon] done", "__BEACON_EXIT__0"]
    r = client.post("/api/agents/a1/decommission", json={"confirm": True})
    assert r.status_code == 200
    assert "archived" in r.text
    assert client.get("/api/agents/a1").status_code == 404  # archived out of the live fleet


def test_apply_fix_without_confirm_does_not_touch_ssh(client, host_and_agent, fake_ssh):
    r = client.post("/api/agents/a1/reconcile", json={"fix": "start"})
    assert r.status_code == 200
    assert r.json()["would_run"] is True
    assert fake_ssh.calls == []


def test_apply_fix_with_confirm_runs(client, host_and_agent, fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="started", stderr="", returncode=0)
    r = client.post("/api/agents/a1/reconcile", json={"fix": "start", "confirm": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_push_config_without_confirm_does_not_touch_ssh(client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "desired": {"config": {"agent": {"max_turns": 42}}}})

    r = client.post("/api/agents/a1/config-diff")
    assert r.status_code == 200
    assert r.json()["would_run"] is True
    assert fake_ssh.calls == []


def test_update_plugin_without_confirm_does_not_touch_ssh(client, host_and_agent, fake_ssh):
    r = client.post("/api/agents/a1/plugins/deltachat-platform/update")
    assert r.status_code == 200
    assert r.json()["would_run"] is True
    assert fake_ssh.calls == []


def test_update_agent_without_confirm_does_not_touch_ssh(client, host_and_agent, fake_ssh):
    r = client.post("/api/agents/a1/update")
    assert r.status_code == 200
    assert r.json()["would_run"] is True
    assert fake_ssh.calls == []


# ---------------------------------------------------------------------------
# Read-only endpoints stay ungated
# ---------------------------------------------------------------------------


def test_status_never_gated(client, host_and_agent, fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="ActiveState=active\nSubState=running\nMainPID=1\nLoadState=loaded", stderr="", returncode=0)
    r = client.get("/api/agents/a1/status")
    assert r.status_code == 200
    assert r.json()["state"] == "active"


def test_reconcile_check_never_gated(client, host_and_agent, fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="", returncode=None)
    r = client.get("/api/agents/a1/reconcile")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "unreachable"
