"""Tests for fleet cron jobs: schema validation, persistence, scheduling,
and dispatch through the driver seam.
"""

import datetime

import pytest

from backend import cron_store, scheduler
from backend.schemas import CronJob
from backend.ssh import SSHResult
from tests.conftest import make_agent, make_host


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_cron_job_requires_five_field_schedule():
    with pytest.raises(ValueError):
        CronJob(id="restart-nightly", schedule="0 0 * *", command={"action": "restart"}, target_agent_ids=["a1"])


def test_cron_job_rejects_invalid_schedule_field():
    with pytest.raises(ValueError):
        CronJob(id="restart-nightly", schedule="foo * * * *", command={"action": "restart"}, target_agent_ids=["a1"])


def test_cron_job_requires_known_action():
    with pytest.raises(ValueError):
        CronJob(id="bad", schedule="0 * * * *", command={"action": "rm -rf /"}, target_agent_ids=["a1"])


def test_cron_job_requires_non_empty_targets():
    with pytest.raises(ValueError):
        CronJob(id="bad", schedule="0 * * * *", command={"action": "restart"}, target_agent_ids=[])


def test_cron_job_accepts_valid_record():
    job = CronJob(
        id="restart-nightly", enabled=True, schedule="0 2 * * *",
        command={"action": "restart"}, target_agent_ids=["a1"], owner="ops",
    )
    assert job.schedule == "0 2 * * *"


# ---------------------------------------------------------------------------
# Store CRUD
# ---------------------------------------------------------------------------


def test_cron_job_crud(fleet):
    job = CronJob(id="j1", schedule="0 * * * *", command={"action": "restart"}, target_agent_ids=["a1"])
    cron_store.upsert_cron_job(job)

    assert len(cron_store.list_cron_jobs()) == 1
    assert cron_store.get_cron_job("j1").id == "j1"

    cron_store.delete_cron_job("j1")
    assert cron_store.list_cron_jobs() == []


def test_cron_store_not_found(fleet):
    with pytest.raises(cron_store.NotFound):
        cron_store.get_cron_job("missing")


def test_cron_store_invalid_id(fleet):
    with pytest.raises(cron_store.InvalidId):
        cron_store.get_cron_job("bad id")


# ---------------------------------------------------------------------------
# Scheduler due evaluation
# ---------------------------------------------------------------------------


def test_is_due_every_minute():
    dt = datetime.datetime(2026, 1, 1, 12, 30, 0)
    assert scheduler._is_due("* * * * *", dt) is True


def test_is_due_specific_minute():
    dt = datetime.datetime(2026, 1, 1, 12, 30, 0)
    assert scheduler._is_due("30 * * * *", dt) is True
    assert scheduler._is_due("31 * * * *", dt) is False


def test_is_due_step_expression():
    dt = datetime.datetime(2026, 1, 1, 12, 30, 0)
    assert scheduler._is_due("*/10 * * * *", dt) is True
    assert scheduler._is_due("*/7 * * * *", dt) is False


def test_is_due_day_of_week():
    # 2026-01-01 is a Thursday (isoweekday 4 -> cron day-of-week 4).
    dt = datetime.datetime(2026, 1, 1, 12, 0, 0)
    assert scheduler._is_due("0 12 * * 4", dt) is True
    assert scheduler._is_due("0 12 * * 3", dt) is False


# ---------------------------------------------------------------------------
# Scheduler dispatch (uses fake_ssh via conftest)
# ---------------------------------------------------------------------------


def test_tick_runs_due_job(fleet, fake_ssh):
    store = cron_store  # uses fleet fixture
    # Create host and agent records so the driver can resolve them.
    from backend import store as fleet_store
    fleet_store.upsert_host(make_host())
    fleet_store.upsert_agent(make_agent())

    job = CronJob(id="j1", schedule="30 * * * *", command={"action": "restart"}, target_agent_ids=["a1"])
    cron_store.upsert_cron_job(job)

    fake_ssh.result = SSHResult(ok=True, stdout="restarted", stderr="", returncode=0)
    now = datetime.datetime(2026, 1, 1, 12, 30, 0)
    results = scheduler.tick(now=now)

    assert len(results) == 1
    assert results[0]["job_id"] == "j1"
    assert results[0]["status"] == "ok"
    assert fake_ssh.calls
    assert "gateway restart" in fake_ssh.last_command

    persisted = cron_store.get_cron_job("j1")
    assert persisted.last_run_status == "ok"
    assert persisted.last_run_output is not None


def test_tick_skips_not_due_job(fleet, fake_ssh):
    from backend import store as fleet_store
    fleet_store.upsert_host(make_host())
    fleet_store.upsert_agent(make_agent())

    job = CronJob(id="j1", schedule="30 * * * *", command={"action": "restart"}, target_agent_ids=["a1"])
    cron_store.upsert_cron_job(job)

    now = datetime.datetime(2026, 1, 1, 12, 31, 0)
    results = scheduler.tick(now=now)

    assert results[0]["due"] is False
    assert fake_ssh.calls == []


def test_tick_skips_disabled_job(fleet, fake_ssh):
    from backend import store as fleet_store
    fleet_store.upsert_host(make_host())
    fleet_store.upsert_agent(make_agent())

    job = CronJob(id="j1", enabled=False, schedule="* * * * *", command={"action": "restart"}, target_agent_ids=["a1"])
    cron_store.upsert_cron_job(job)

    results = scheduler.tick(now=datetime.datetime(2026, 1, 1, 12, 0, 0))
    assert fake_ssh.calls == []


def test_tick_reports_failed_run(fleet, fake_ssh):
    from backend import store as fleet_store
    fleet_store.upsert_host(make_host())
    fleet_store.upsert_agent(make_agent())

    job = CronJob(id="j1", schedule="* * * * *", command={"action": "restart"}, target_agent_ids=["a1"])
    cron_store.upsert_cron_job(job)

    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="ssh timed out", returncode=None)
    results = scheduler.tick(now=datetime.datetime(2026, 1, 1, 12, 0, 0))

    assert results[0]["status"] == "failed"
    assert cron_store.get_cron_job("j1").last_run_status == "failed"


def test_dry_run_does_not_execute(fleet, fake_ssh):
    from backend import store as fleet_store
    fleet_store.upsert_host(make_host())
    fleet_store.upsert_agent(make_agent())

    job = CronJob(id="j1", schedule="* * * * *", command={"action": "restart"}, target_agent_ids=["a1"])
    cron_store.upsert_cron_job(job)

    results = scheduler.tick(now=datetime.datetime(2026, 1, 1, 12, 0, 0), dry_run=True)

    assert results[0]["due"] is True
    assert results[0]["ran_at"] is None
    assert fake_ssh.calls == []


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


def test_api_cron_job_lifecycle(client):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "desired": {}})

    r = client.put("/api/cron-jobs/nightly-restart", json={
        "id": "nightly-restart", "enabled": True, "schedule": "0 2 * * *",
        "command": {"action": "restart"}, "target_agent_ids": ["a1"],
    })
    assert r.status_code == 200

    r = client.get("/api/cron-jobs/nightly-restart")
    assert r.status_code == 200
    assert r.json()["schedule"] == "0 2 * * *"

    r = client.get("/api/cron-jobs")
    assert len(r.json()) == 1

    r = client.delete("/api/cron-jobs/nightly-restart")
    assert r.status_code == 200
    assert client.get("/api/cron-jobs/nightly-restart").status_code == 404


def test_api_cron_job_rejects_missing_target(client):
    r = client.put("/api/cron-jobs/bad", json={
        "id": "bad", "enabled": True, "schedule": "0 2 * * *",
        "command": {"action": "restart"}, "target_agent_ids": ["no-such-agent"],
    })
    assert r.status_code == 404


def test_api_cron_job_manual_run(client, fake_ssh):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "desired": {}})
    client.put("/api/cron-jobs/manual", json={
        "id": "manual", "enabled": True, "schedule": "0 2 * * *",
        "command": {"action": "restart"}, "target_agent_ids": ["a1"],
    })
    fake_ssh.result = SSHResult(ok=True, stdout="restarted", stderr="", returncode=0)

    r = client.post("/api/cron-jobs/manual/run")

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert fake_ssh.calls


def test_api_cron_job_dry_run(client):
    client.put("/api/hosts/edge-01", json={
        "id": "edge-01", "address": "10.0.0.1",
        "ssh": {"user": "deploy", "key": "~/.ssh/id_ed25519", "port": 22}, "tags": [],
    })
    client.put("/api/agents/a1", json={"id": "a1", "type": "hermes", "host": "edge-01", "desired": {}})
    client.put("/api/cron-jobs/check", json={
        "id": "check", "enabled": True, "schedule": "30 * * * *",
        "command": {"action": "restart"}, "target_agent_ids": ["a1"],
    })

    r = client.post("/api/cron-jobs/check/dry-run")

    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "check"
    assert body["due"] is False
    assert body["ran_at"] is None
