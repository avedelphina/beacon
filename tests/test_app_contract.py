"""The JSON shapes the native apps decode, kept in apps/contract/ (docs/ios-app-plan.md, 3.4).

Each file is a real answer from the API, made here with fixed values. The test fails when a shape
changes (a field added, removed or changed in type) and the files were not regenerated, so a change
that could break an app is seen in the same PR. Regenerate with:

    BEACON_UPDATE_CONTRACT=1 .venv/bin/pytest tests/test_app_contract.py

and run the app's tests (apps/apple) against the new files. A change that breaks an app also raises
`api` (backend/app_devices.py).
"""

import base64
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import auth, availability, board, mesh, opstore, secret_pipe, store, workflow
from backend.app import app
from backend.schemas import Agent
from backend.ssh import SSHResult
from tests.conftest import make_host

CONTRACT = Path(__file__).resolve().parent.parent / "apps" / "contract"
NOW = 1_795_000_000.0


def _shape(value):
    """Keys and types, not values: ids and times differ from run to run."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


@pytest.fixture
def answers(fleet, fake_ssh, monkeypatch):
    monkeypatch.setattr(availability, "probe", lambda agent: (True, ""))
    fake_ssh.result = SSHResult(ok=True, returncode=0, stderr="", stdout=(
        "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=4321\nActiveEnterTimestamp=Thu 2026-11-26 08:00:00 UTC\n"))
    monkeypatch.setenv(mesh.SECRET_ENV, "contract-secret")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"tom@example.com"}))
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    monkeypatch.setattr(workflow.time, "time", lambda: NOW)
    store.upsert_host(make_host("h1"))
    store.upsert_agent(Agent.model_validate({"id": "alpha", "type": "hermes", "host": "h1", "owner": "ann@example.com",
                                             "tags": ["family-a"], "notes": "Kitchen assistant",
                                             "reach": {"endpoint": "http://alpha.test:9900", "board": True}}))
    with opstore.connect() as db:
        db.execute("INSERT INTO health_events (agent, state, detail, at) VALUES ('alpha', 'restarted', 'no answer twice', ?)", (NOW,))
    client = TestClient(app)
    payload = base64.b64encode(json.dumps({"user": {"email": "tom@example.com"}}).encode()).decode()
    client.cookies.set("session", TimestampSigner(auth.SESSION_SECRET).sign(payload.encode()).decode())
    client.headers["Origin"] = "http://testserver"
    client.post("/api/projects", json={"name": "infra", "goal": "Keys stay fresh", "done_when": "none expires"})
    card = client.post("/api/board/cards", json={"title": "Rotate the DNS key", "body": "Old key expires Friday.",
                                                 "assignee": "alpha", "project": "infra"}).json()
    client.post(f"/api/board/cards/{card['id']}/references", json={
        "provider": "gitea", "kind": "pull_request", "external_id": "42", "url": "https://git.example.com/infra/pulls/42",
        "label": "Rotate key"})
    binding = workflow.plan_binding({"rotate": "dns"})
    request = workflow.create(
        card_id=card["id"], kind="decision", requester="alpha", eligible_responder="human", capability="ask",
        clearance=2, title="Which key size?", summary="**P-256** or P-384?", options=["P-256", "P-384"], target={},
        consulted=[{"agent": "beta", "said": "P-256 is enough", "context_id": "ctx-1"}], urgent_reason="expires Friday",
        **binding)
    answers = {
        "workflow.json": client.get(f"/api/workflows/{request['id']}"),
        "workflows.json": client.get("/api/workflows", params={"state": "pending"}),
        "card.json": client.get(f"/api/board/cards/{card['id']}"),
        "version.json": client.get("/api/version"),
        "board.json": client.get("/api/board"),
        "agents.json": client.get("/api/agents"),
        "availability.json": client.get("/api/availability"),
        "health-events.json": client.get("/api/agents/alpha/health-events"),
        "status.json": client.get("/api/agents/alpha/status"),
        "logs.json": client.get("/api/agents/alpha/logs"),
        "restart-gate.json": client.post("/api/agents/alpha/restart"),
        "restart.json": client.post("/api/agents/alpha/restart?confirm=true"),
    }
    monkeypatch.setattr(secret_pipe, "names", lambda agent_id: ["db-password"])
    monkeypatch.setattr(secret_pipe, "put", lambda agent_id, name, data: f"beacon-secrets/inbox/{name}")
    monkeypatch.setattr(secret_pipe, "read", lambda agent_id, name: b"correct horse")
    monkeypatch.setattr(secret_pipe, "remove", lambda agent_id, name: None)
    answers |= {
        "secrets.json": client.get("/api/agents/alpha/secrets"),
        "secret-put.json": client.post("/api/agents/alpha/secrets", json={"name": "api-key", "value": "x", "confirm": True}),
        "secret-reveal.json": client.post("/api/agents/alpha/secrets/db-password/reveal?confirm=true"),
    }
    # A member gets trimmed views (backend/members.py); the app must decode those too.
    member = TestClient(app)
    member_payload = base64.b64encode(json.dumps({"user": {"email": "ann@example.com"}}).encode()).decode()
    member.cookies.set("session", TimestampSigner(auth.SESSION_SECRET).sign(member_payload.encode()).decode())
    member.headers["Origin"] = "http://testserver"
    answers |= {
        "agents-member.json": member.get("/api/agents"),
        "availability-member.json": member.get("/api/availability"),
        "status-member.json": member.get("/api/agents/alpha/status"),
    }
    assert all(r.status_code == 200 for r in answers.values()), {k: r.text for k, r in answers.items()}
    return {k: r.json() for k, r in answers.items()}


def test_the_notification_payloads_match_what_beacon_sends():
    """What APNs delivers to the app (backend/apns.py): the app picks the category's actions and the
    request to open from these. Values matter here, not only shapes: the category names must be the
    ones the app registers."""
    from backend import apns
    sent = {
        "approve": apns.payload("alpha needs your answer", about={"workflow": 17, "clearance": 3, "kind": "approval", "card": 4}),
        "human": apns.payload("URGENT: alpha needs your answer", urgent=True,
                              about={"workflow": 18, "clearance": 4, "kind": "approval", "card": 4}),
        "open": apns.payload("alpha needs your answer", about={"workflow": 19, "clearance": 2, "kind": "decision", "card": 5}),
        "plain": apns.payload("alpha has not synced for 2 hours"),
        "secret": apns.payload("alpha has a secret for you", about={"agent": "alpha"}),
    }
    path = CONTRACT / "apns.json"
    if os.environ.get("BEACON_UPDATE_CONTRACT"):
        path.write_text(json.dumps(sent, indent=2, sort_keys=True) + "\n")
    assert json.loads(path.read_text()) == sent, "Beacon's APNs payloads changed: regenerate apps/contract"


@pytest.mark.parametrize("name", [
    "workflow.json", "workflows.json", "card.json", "version.json", "board.json", "agents.json", "availability.json",
    "health-events.json", "status.json", "logs.json", "restart-gate.json", "restart.json", "agents-member.json",
    "secrets.json", "secret-put.json", "secret-reveal.json",
    "availability-member.json", "status-member.json"])
def test_the_apps_contract_files_match_the_api(answers, name):
    path = CONTRACT / name
    if os.environ.get("BEACON_UPDATE_CONTRACT"):
        path.write_text(json.dumps(answers[name], indent=2, sort_keys=True) + "\n")
    assert path.exists(), f"{path} is missing: run with BEACON_UPDATE_CONTRACT=1"
    assert _shape(json.loads(path.read_text())) == _shape(answers[name]), (
        f"the API's answer for {name} changed shape: regenerate apps/contract (see this module's docstring)")
