"""The fleet lives in the store; YAML only moves it in and out. Import and
export are inverses, a first start imports the old fleet/ once and never again,
a bad record never lands half-way, and every change keeps who made it."""

import yaml
from fastapi.testclient import TestClient

from backend import cron_store, policy, store, templates
from backend import fleet as fleet_io
from backend.app import app
from backend.schemas import CronJob
from tests.conftest import make_agent, make_host

POLICY = "# everyone talks\nrules:\n  - {from: ['*'], to: ['*']}\n"


def _write_fleet(src, agent_host="edge-01"):
    (src / "agents").mkdir(parents=True)
    (src / "templates").mkdir()
    (src / "cron_jobs").mkdir()
    (src / "decommissioned").mkdir()
    (src / "hosts.yaml").write_text(yaml.safe_dump({"hosts": [make_host(id="edge-01").model_dump()]}))
    (src / "agents" / "a1.yaml").write_text(yaml.safe_dump(make_agent(id="a1", host=agent_host).model_dump()))
    (src / "decommissioned" / "old.yaml").write_text(yaml.safe_dump(make_agent(id="old", host="edge-01").model_dump()))
    (src / "templates" / "stack.yaml").write_text(yaml.safe_dump({"config": {"model": {"default": "m"}}}))
    (src / "cron_jobs" / "j1.yaml").write_text(yaml.safe_dump(CronJob(
        id="j1", schedule="0 * * * *", command={"action": "status"}, target_agent_ids=["a1"]).model_dump()))
    (src / "policy.yaml").write_text(POLICY)


def test_import_then_export_gives_back_the_same_fleet(fleet, tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    _write_fleet(src)

    result = fleet_io.import_dir(src)
    assert result["errors"] == [] and result["imported"]["agent"] == 1

    assert [a.id for a in store.list_agents()] == ["a1"]
    assert [a.id for a in store.list_decommissioned()] == ["old"]
    assert templates.load_template("stack") == {"config": {"model": {"default": "m"}}}
    assert [j.id for j in cron_store.list_cron_jobs()] == ["j1"]
    assert policy.read_text() == POLICY  # comments survive

    fleet_io.export_dir(out)
    for name in ("hosts.yaml", "agents/a1.yaml", "decommissioned/old.yaml", "templates/stack.yaml",
                 "cron_jobs/j1.yaml", "policy.yaml"):
        assert yaml.safe_load((out / name).read_text()) == yaml.safe_load((src / name).read_text()), name
    assert (out / "policy.yaml").read_text() == POLICY


def test_a_strict_import_writes_nothing_when_anything_is_wrong(fleet, tmp_path):
    src = tmp_path / "in"
    _write_fleet(src, agent_host="nowhere")  # a1's host is not in the fleet

    result = fleet_io.import_dir(src)
    assert any("host 'nowhere'" in e for e in result["errors"])
    assert store.list_hosts() == [] and store.list_agents() == []

    # Told to skip the bad one, it imports the rest.
    result = fleet_io.import_dir(src, strict=False)
    assert result["errors"] and [h.id for h in store.list_hosts()] == ["edge-01"] and store.list_agents() == []


def test_an_invalid_file_is_reported_by_name(fleet, tmp_path):
    src = tmp_path / "in"
    _write_fleet(src)
    (src / "agents" / "bad.yaml").write_text("id: Bad Id\ntype: hermes\nhost: edge-01\n")
    (src / "templates" / "odd.yaml").write_text("secrets: {a: 1}\n")
    errors = fleet_io.import_dir(src)["errors"]
    assert any(e.startswith("agents/bad.yaml") for e in errors) and any(e.startswith("templates/odd.yaml") for e in errors)


def test_the_first_start_imports_the_yaml_fleet_once_and_never_again(fleet, tmp_path):
    _write_fleet(store.FLEET_DIR)
    with TestClient(app):  # the lifespan runs the bootstrap
        assert [a.id for a in store.list_agents()] == ["a1"]
        store.delete_agent("a1")
    # A stale directory on the next start must not put old records back.
    with TestClient(app):
        assert store.list_agents() == []
    fleet_io.bootstrap()
    assert store.list_agents() == []


def test_history_records_who_changed_what(fleet, human_client):
    store.upsert_host(make_host(id="h1"))
    body = make_agent(id="a1", host="h1").model_dump()
    assert human_client.put("/api/agents/a1", json=body).status_code == 200
    assert human_client.put("/api/agents/a1", json={**body, "notes": "second"}).status_code == 200
    assert human_client.put("/api/agents/a1", json={**body, "notes": "second"}).status_code == 200  # no change: no entry

    newest, oldest = store.history("agent", "a1")
    assert (oldest["actor"], newest["actor"]) == ("tom@example.com", "tom@example.com")
    assert '"second"' in newest["body"] and '"second"' not in oldest["body"]

    human_client.delete("/api/agents/a1")
    assert store.history("agent", "a1")[0]["body"] is None  # the delete is on record


def test_cron_runs_are_not_history(fleet):
    store.upsert_host(make_host(id="h1"))
    job = CronJob(id="j1", schedule="0 * * * *", command={"action": "status"}, target_agent_ids=["a1"])
    cron_store.upsert_cron_job(job)
    cron_store.upsert_cron_job(job.model_copy(update={"last_run_status": "ok"}))
    assert store.history("cron", "j1") == []
    assert cron_store.get_cron_job("j1").last_run_status == "ok"


def test_template_routes_validate_gate_and_protect_templates_in_use(client, fleet):
    store.upsert_host(make_host(id="h1"))
    content = {"config": {"model": {"default": "m"}}}

    asked = client.put("/api/templates/stack", json={"content": content}).json()
    assert asked["would_run"] is True and asked["tier"] == "T2" and templates.list_templates() == []
    assert client.put("/api/templates/stack", json={"content": {"nonsense": 1}, "confirm": True}).status_code == 400

    assert client.put("/api/templates/stack", json={"content": content, "confirm": True}).json()["ok"] is True
    assert client.get("/api/templates/stack").json()["content"] == content

    store.upsert_agent(make_agent(id="a1", host="h1", templates=["stack"]))
    refused = client.delete("/api/templates/stack", params={"confirm": True})
    assert refused.status_code == 409 and "a1" in refused.json()["detail"]

    store.upsert_agent(make_agent(id="a1", host="h1"))
    assert client.delete("/api/templates/stack", params={"confirm": True}).json() == {"ok": True}
    assert client.get("/api/templates/stack").status_code == 404


def test_the_cli_round_trips(fleet, tmp_path, capsys):
    src, out = tmp_path / "in", tmp_path / "out"
    _write_fleet(src)
    assert fleet_io.main(["import", str(src)]) == 0
    assert fleet_io.main(["export", str(out)]) == 0
    assert "1 agents" in capsys.readouterr().out and (out / "agents" / "a1.yaml").exists()
    (src / "agents" / "bad.yaml").write_text("id: x\n")
    assert fleet_io.main(["import", str(src)]) == 1  # strict: refused
