"""Bringing an existing agent into the mesh: a single-use enrollment code that
hands the host its install script (and so its pull token) without the token
passing through a browser; the --schedule flag of that script; and the
checklist the UI shows while the host catches up."""

import os
import stat
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from backend import auth, availability, board, mesh, onboarding, opstore, store, workflow
from backend.app import app
from backend.drivers import hermes
from backend.schemas import Agent, Host

PUBLIC = "https://beacon.test"


@pytest.fixture
def joined(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, PUBLIC)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "192.0.2.1"}))  # no SSH needed
    store.upsert_agent(Agent.model_validate({"id": "alpha", "type": "hermes", "host": "h1", "profile": "alpha",
        "reach": {"endpoint": "http://127.0.0.1:9", "network": "lan"}}))


def _mint(client, **body):
    return client.post("/api/agents/alpha/enroll", json={"confirm": True, **body})


# --- the code ---------------------------------------------------------------


def test_a_code_hands_over_the_install_script_once(joined, human_client):
    made = _mint(human_client).json()
    assert made["command"].startswith(f"curl -fsS {PUBLIC}/enroll/") and made["command"].endswith("| bash -s -- --schedule")
    code = made["command"].split("/enroll/")[1].split(" ")[0]

    with TestClient(app) as anyone:  # the host has no session
        script = anyone.get(f"/enroll/{code}")
        assert script.status_code == 200 and mesh.token("alpha", mesh.BEACON_PEER) in script.text
        assert anyone.get(f"/enroll/{code}").status_code == 404            # used
        assert anyone.get("/enroll/not-a-code").status_code == 404          # unknown
        assert anyone.get(f"/enroll/{code}").json() == anyone.get("/enroll/not-a-code").json()  # no hint which

    with opstore.connect() as db:  # only a hash is stored
        assert code not in str(dict(db.execute("SELECT * FROM enrollments").fetchone()))


def test_a_docker_agent_runs_as_root_and_a_code_expires(joined, human_client):
    made = _mint(human_client, docker="hermes-alpha", schedule=False).json()
    assert made["command"].endswith("| sudo bash -s") and "--schedule" not in made["command"]
    code = made["command"].split("/enroll/")[1].split(" ")[0]
    with opstore.connect() as db:
        db.execute("UPDATE enrollments SET expires_at = ?", (time.time() - 1,))
    assert TestClient(app).get(f"/enroll/{code}").status_code == 404


def test_only_a_person_can_mint_a_code_and_it_asks_first(joined, human_client, monkeypatch):
    asked = human_client.post("/api/agents/alpha/enroll", json={}).json()
    assert asked["would_run"] is True and asked["tier"] == "T4"
    with opstore.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] == 0

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")
    mcp = TestClient(app, headers={"Authorization": "Bearer test-mcp-token"})
    assert _mint(mcp).status_code == 403


def test_a_code_that_could_not_work_is_never_handed_out(joined, human_client, monkeypatch):
    monkeypatch.delenv(mesh.PUBLIC_URL_ENV)
    assert _mint(human_client).status_code == 400
    store.upsert_agent(Agent.model_validate({"id": "plain", "type": "hermes", "host": "h1"}))
    assert human_client.post("/api/agents/plain/enroll", json={"confirm": True}).status_code == 400


def test_the_enroll_path_needs_no_session_when_login_is_on(joined, human_client, monkeypatch):
    code = _mint(human_client).json()["command"].split("/enroll/")[1].split(" ")[0]
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    assert TestClient(app).get(f"/enroll/{code}").status_code == 200


# --- asked for by an agent, approved by a person ----------------------------


@pytest.fixture
def card(joined):
    store.upsert_agent(Agent.model_validate({"id": "beta", "type": "hermes", "host": "h1", "profile": "beta",
        "reach": {"endpoint": "http://127.0.0.1:9", "network": "lan", "board": True}}))
    return board.create_card("beacon", "Enroll alpha", "", None, None)["id"]


def _approve(client, request, action="approved"):
    return client.post(f"/api/workflows/{request['id']}/respond", json={
        "action": action, "plan_revision": request["plan_revision"], "plan_hash": request["plan_hash"]})


def test_an_approved_request_hands_its_requester_the_command_once(card, human_client):
    request = onboarding.request_enrollment("beacon", card, "alpha", None, "hermes", True)
    assert request["state"] == "pending" and request["tier"] == 4 and request["eligible_responder"] == "human"
    with pytest.raises(workflow.WorkflowError, match="pending, not approved"):
        onboarding.claim(request["id"], "beacon")

    assert _approve(human_client, request).status_code == 200
    made = onboarding.claim(request["id"], "beacon")
    assert made["command"].startswith(f"curl -fsS {PUBLIC}/enroll/") and made["command"].endswith("-- --schedule")
    code = made["command"].split("/enroll/")[1].split(" ")[0]
    assert TestClient(app).get(f"/enroll/{code}").status_code == 200
    assert workflow.get(request["id"])["state"] == "executed"
    with pytest.raises(workflow.WorkflowError, match="executed, not approved"):
        onboarding.claim(request["id"], "beacon")  # once


def test_a_rejected_request_gives_nothing_and_no_one_else_can_claim(card, human_client):
    rejected = onboarding.request_enrollment("beacon", card, "alpha", None, "hermes", True)
    _approve(human_client, rejected, "rejected")
    with pytest.raises(workflow.WorkflowError):
        onboarding.claim(rejected["id"], "beacon")

    request = onboarding.request_enrollment("beacon", card, "alpha", None, "hermes", True)
    _approve(human_client, request)
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        onboarding.claim(request["id"], "alpha")  # approved for beacon's ask, not alpha's
    with opstore.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] == 0


def test_an_agent_can_ask_only_for_itself(card, human_client):
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        onboarding.request_enrollment("beta", card, "alpha", None, "hermes", True)
    request = onboarding.request_enrollment("beta", card, "beta", None, "hermes", True)
    _approve(human_client, request)
    assert "/enroll/" in onboarding.claim(request["id"], "beta")["command"]


def test_an_agent_cannot_approve_or_forge_its_own_request(card, human_client):
    request = onboarding.request_enrollment("beta", card, "beta", None, "hermes", True)
    with pytest.raises(workflow.UnauthorizedResponder):  # only a person in the browser answers
        workflow.respond(request["id"], actor="beta", human=False, action="approved", response=None,
                         plan_revision=request["plan_revision"], plan_hash=request["plan_hash"])

    # Made by hand with another agent as the target and reassuring words: refused at claim.
    forged_target = {"agent": "alpha", "docker": None, "docker_user": "hermes", "schedule": True}
    binding = workflow.plan_binding({"x": 1})
    forged = workflow.create(card_id=card, kind="approval", requester="beta", eligible_responder="human",
                             capability="enroll_agent", tier=4, title="Enroll beta", summary="routine", options=None,
                             target=forged_target, **binding)
    _approve(human_client, forged)
    with pytest.raises(workflow.UnauthorizedWorkflowAccess):
        onboarding.claim(forged["id"], "beta")
    assert workflow.get(forged["id"])["state"] == "failed"

    # Its own target but other words than the ones Beacon writes: also refused.
    own = {"agent": "beta", "docker": None, "docker_user": "hermes", "schedule": True}
    reworded = workflow.create(card_id=card, kind="approval", requester="beta", eligible_responder="human",
                               capability="enroll_agent", tier=4, title="Enroll beta", summary="routine", options=None,
                               target=own, **binding)
    _approve(human_client, reworded)
    with pytest.raises(workflow.WorkflowError, match="not made by enroll_request"):
        onboarding.claim(reworded["id"], "beta")


# --- the script's --schedule ------------------------------------------------


@pytest.fixture
def fake_crontab(tmp_path):
    """A crontab that keeps its table in a file and, like the real one, reads
    all of stdin before replacing it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    table = tmp_path / "table"
    tool = bin_dir / "crontab"
    tool.write_text(f'#!/bin/sh\nif [ "$1" = "-l" ]; then [ -f {table} ] && cat {table} || exit 1\n'
                    f'else cat > {table}.new && mv {table}.new {table}; fi\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return bin_dir, table


def _run_tail(args, bin_dir):
    shell = "py=/usr/bin/python3; dir=/home/h/beacon-mesh\n" + "\n".join(hermes._schedule_lines("alpha", "for this user"))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(["bash", "-e", "-c", shell, "bash", *args], capture_output=True, text=True, env=env).stdout


def test_without_the_flag_the_script_only_prints_the_line(fake_crontab):
    bin_dir, table = fake_crontab
    out = _run_tail([], bin_dir)
    assert "To keep it in sync, schedule" in out and "*/5 * * * *" in out and not table.exists()


def test_with_the_flag_it_schedules_once_and_leaves_other_lines(fake_crontab):
    bin_dir, table = fake_crontab
    table.write_text('MAILTO=me\n0 1 * * * other\n*/5 * * * * "/old" "/home/h/beacon-mesh/sync.py" x\n')
    for _ in range(2):
        assert "scheduled every 5 minutes" in _run_tail(["--schedule"], bin_dir)
    lines = table.read_text().splitlines()
    assert lines[:2] == ["MAILTO=me", "0 1 * * * other"] and len(lines) == 3
    assert lines[2] == '*/5 * * * * "/usr/bin/python3" "/home/h/beacon-mesh/sync.py" "/home/h/beacon-mesh" >> "/home/h/beacon-mesh/sync.log" 2>&1'


def test_with_the_flag_and_no_crontab_yet_it_still_schedules(fake_crontab):
    bin_dir, table = fake_crontab
    assert not table.exists()  # `crontab -l` fails here, as on a fresh account
    assert "scheduled every 5 minutes" in _run_tail(["--schedule"], bin_dir)
    assert "sync.py" in table.read_text()


def test_with_the_flag_and_no_crontab_command_it_prints_the_line(tmp_path):
    shell = "py=/usr/bin/python3; dir=/d\n" + "\n".join(hermes._schedule_lines("alpha", "for this user"))
    empty = tmp_path / "empty"
    empty.mkdir()
    out = subprocess.run(["/bin/bash", "-e", "-c", shell, "bash", "--schedule"], capture_output=True, text=True,
                         env={"PATH": str(empty)}).stdout
    assert "To keep it in sync, schedule" in out and "*/5 * * * *" in out


# --- the checklist ----------------------------------------------------------


def _done(agent="alpha"):
    return {s["key"]: s["done"] for s in onboarding.status(agent)["steps"]}


def test_the_checklist_follows_the_host_as_it_catches_up(joined, human_client, monkeypatch):
    monkeypatch.setattr(availability, "probe", lambda agent: (True, ""))
    assert _done() == {"record": True, "command": False, "sync": False, "schedule": False, "a2a": True, "chat": False}
    assert onboarding.status("alpha")["enrollment"] == "none"

    code = _mint(human_client).json()["command"].split("/enroll/")[1].split(" ")[0]
    assert onboarding.status("alpha")["enrollment"] == "waiting"
    onboarding.redeem(code)
    assert onboarding.status("alpha")["enrollment"] == "used" and _done()["command"] and not _done()["sync"]

    headers = {"Authorization": f"Bearer {mesh.token('alpha', mesh.BEACON_PEER)}", "X-Beacon-Open-Chat": "none"}
    with TestClient(app) as c:
        c.get("/mesh/alpha", headers=headers)          # the install script's own sync
        assert _done()["sync"] and not _done()["schedule"]
        c.get("/mesh/alpha", headers=headers)          # the cron job's first run
    result = onboarding.status("alpha")
    assert result["done"] is True and all(s["done"] for s in result["steps"])


def test_pulls_before_the_enrollment_do_not_count_toward_it(joined, human_client, monkeypatch):
    monkeypatch.setattr(availability, "probe", lambda agent: (True, ""))
    availability.record_pull("alpha")
    availability.record_pull("alpha")                  # an old install that already syncs
    assert _done()["schedule"]
    code = _mint(human_client).json()["command"].split("/enroll/")[1].split(" ")[0]
    onboarding.redeem(code)
    assert not _done()["sync"] and not _done()["schedule"]  # the re-run script has to show itself


def test_an_open_chat_is_said_plainly(joined):
    mesh.record_open_chat("alpha", "DELTACHAT_ALLOW_ALL_USERS")
    chat = [s for s in onboarding.status("alpha")["steps"] if s["key"] == "chat"][0]
    assert chat["done"] and "board stays withheld" in chat["detail"]
