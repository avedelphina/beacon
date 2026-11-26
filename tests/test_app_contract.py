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

from backend import auth, board, mesh, store, workflow
from backend.app import app
from backend.schemas import Agent
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
def answers(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "contract-secret")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"tom@example.com"}))
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    monkeypatch.setattr(workflow.time, "time", lambda: NOW)
    store.upsert_host(make_host("h1"))
    store.upsert_agent(Agent.model_validate({"id": "alpha", "type": "hermes", "host": "h1", "owner": "ann@example.com",
                                             "reach": {"endpoint": "http://alpha.test:9900", "board": True}}))
    client = TestClient(app)
    payload = base64.b64encode(json.dumps({"user": {"email": "tom@example.com"}}).encode()).decode()
    client.cookies.set("session", TimestampSigner(auth.SESSION_SECRET).sign(payload.encode()).decode())
    client.headers["Origin"] = "http://testserver"
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
    }
    assert all(r.status_code == 200 for r in answers.values()), {k: r.text for k, r in answers.items()}
    return {k: r.json() for k, r in answers.items()}


@pytest.mark.parametrize("name", ["workflow.json", "workflows.json", "card.json", "version.json"])
def test_the_apps_contract_files_match_the_api(answers, name):
    path = CONTRACT / name
    if os.environ.get("BEACON_UPDATE_CONTRACT"):
        path.write_text(json.dumps(answers[name], indent=2, sort_keys=True) + "\n")
    assert path.exists(), f"{path} is missing: run with BEACON_UPDATE_CONTRACT=1"
    assert _shape(json.loads(path.read_text())) == _shape(answers[name]), (
        f"the API's answer for {name} changed shape: regenerate apps/contract (see this module's docstring)")
