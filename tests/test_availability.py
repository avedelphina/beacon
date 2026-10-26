"""Availability over A2A: a probe of the agent's /health, plus the heartbeat
of its host sync's pulls. SSH plays no part (a host without SSH settings is
fine). A real local server answers the probe; no Hermes."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from backend import availability, mesh, opstore, store
from backend.app import app
from backend.schemas import Agent, Host


@pytest.fixture
def health_server():
    """An A2A-like /health on a local port; `answer` picks what it says."""
    answer = {"code": 200, "body": {"status": "ok", "agent": "alpha"}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(answer["body"]).encode()
            self.send_response(answer["code"] if self.path == "/health" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port, answer
    server.shutdown()


def _agent(id_, port=None, reach=True):
    record = {"id": id_, "type": "hermes", "host": "nossh"}
    if reach:
        record["reach"] = {"endpoint": f"http://127.0.0.1:{port or 9}", "network": "lan"}
    return Agent.model_validate(record)


@pytest.fixture
def fleet_with_ports(fleet, monkeypatch, health_server):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    port, answer = health_server
    # No SSH on the host: availability must not need it.
    store.upsert_host(Host.model_validate({"id": "nossh", "address": "192.0.2.1"}))
    store.upsert_agent(_agent("alpha", port))
    store.upsert_agent(_agent("dead"))        # nothing listens on port 9
    store.upsert_agent(_agent("quiet", reach=False))
    return answer


def test_an_answering_agent_is_online_without_any_ssh(fleet_with_ports):
    result = availability.check_all(store.list_agents())
    assert result["alpha"]["state"] == "online"
    assert result["quiet"]["state"] == "no-a2a"


def test_an_unreachable_agent_is_offline_until_its_host_syncs(fleet_with_ports):
    assert availability.check_all(store.list_agents())["dead"]["state"] == "offline"
    availability.record_pull("dead")
    result = availability.check_all(store.list_agents())["dead"]
    assert result["state"] == "sync-only" and "synced 0 min ago" in result["detail"]
    # A pull that is too old stops counting.
    with opstore.connect() as db:
        db.execute("UPDATE agent_seen SET last_pull = ? WHERE agent = 'dead'",
                   (time.time() - availability.SYNC_FRESH_SECONDS - 60,))
    assert availability.check_all(store.list_agents())["dead"]["state"] == "offline"


def test_only_an_ok_health_answer_counts(fleet_with_ports):
    fleet_with_ports.update(code=200, body={"hello": "some other web server"})
    assert availability.check_all(store.list_agents())["alpha"]["state"] == "offline"
    fleet_with_ports.update(code=503, body={"status": "ok"})
    assert "HTTP 503" in availability.check_all(store.list_agents())["alpha"]["detail"]


def test_the_api_and_a_mesh_pull_feed_it(fleet_with_ports):
    with TestClient(app) as c:
        assert c.get("/api/availability").json()["dead"]["state"] == "offline"
        pull = c.get("/mesh/dead", headers={"Authorization": f"Bearer {mesh.token('dead', mesh.BEACON_PEER)}"})
        assert pull.status_code == 200
        assert c.get("/api/availability").json()["dead"]["state"] == "sync-only"
