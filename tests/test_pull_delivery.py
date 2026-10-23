"""Pull delivery: held messages ride along with the mesh config to the
agent's own sync, which delivers them on its host and reports back — so
Beacon never needs to reach the agent. Older syncs (no version header) keep
the Beacon-calls-the-agent path."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backend import mailbox, mesh, opstore, store
from backend.schemas import Agent, Host

SYNC_PATH = Path(__file__).parents[1] / "backend" / "drivers" / "hermes_mesh_sync.py"
V2 = {"X-Beacon-Sync-Version": "2"}


@pytest.fixture
def pair(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent_id in ("alpha", "bravo"):
        store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": "h1",
            "reach": {"endpoint": f"http://{agent_id}.test:9900", "network": "tailnet"}}))
    pushed = []
    monkeypatch.setattr(mailbox, "deliver_pending", pushed.append)
    return pushed


def _auth(agent_id):
    return {"Authorization": f"Bearer {mesh.token(agent_id, mesh.BEACON_PEER)}"}


def _state(message_id):
    with opstore.connect() as db:
        return db.execute("SELECT state, last_error FROM mailbox WHERE id = ?", (message_id,)).fetchone()["state"]


def test_a_v2_sync_gets_held_messages_instead_of_a_push(client, pair):
    message_id = mailbox.enqueue("alpha", "bravo", "message", "are you there?", "ctx-1")

    config = client.get("/mesh/bravo", headers={**_auth("bravo"), **V2}).json()

    item, = config["inbox"]
    assert item["id"] == message_id and item["context_id"] == "ctx-1"
    assert item["token"] == mesh.token("alpha", "bravo")
    assert "alpha sent this" in item["text"] and item["text"].endswith("are you there?")
    assert config["endpoint"] == "http://bravo.test:9900"
    assert _state(message_id) == "delivering" and pair == []  # handed over, not pushed


def test_an_older_sync_still_gets_the_push(client, pair):
    mailbox.enqueue("alpha", "bravo", "message", "hi")
    config = client.get("/mesh/bravo", headers=_auth("bravo")).json()
    assert "inbox" not in config and pair == ["bravo"]


def test_a_delivered_report_passes_the_reply_back(client, pair):
    message_id = mailbox.enqueue("alpha", "bravo", "message", "status?")
    client.get("/mesh/bravo", headers={**_auth("bravo"), **V2})

    response = client.post(f"/mesh/bravo/inbox/{message_id}", headers=_auth("bravo"),
                           json={"ok": True, "reply": "all good"})

    assert response.status_code == 200 and _state(message_id) == "delivered"
    with opstore.connect() as db:
        reply = db.execute("SELECT * FROM mailbox WHERE reply_to = ?", (message_id,)).fetchone()
    assert (reply["sender"], reply["recipient"], reply["body"], reply["state"]) == ("bravo", "alpha", "all good", "queued")
    assert pair == ["alpha"]  # Beacon tries alpha at once; else alpha's own pull


@pytest.mark.parametrize("transient, expected", [(True, "queued"), (False, "failed")])
def test_a_failed_report_requeues_only_when_safe(client, pair, transient, expected):
    message_id = mailbox.enqueue("alpha", "bravo", "message", "x")
    client.get("/mesh/bravo", headers={**_auth("bravo"), **V2})

    client.post(f"/mesh/bravo/inbox/{message_id}", headers=_auth("bravo"),
                json={"ok": False, "transient": transient, "error": "boom"})

    assert _state(message_id) == expected


def test_reports_must_match_a_claim_by_that_agent(client, pair):
    message_id = mailbox.enqueue("alpha", "bravo", "message", "x")
    assert client.post(f"/mesh/bravo/inbox/{message_id}", headers=_auth("bravo"), json={"ok": True}).status_code == 404
    client.get("/mesh/bravo", headers={**_auth("bravo"), **V2})
    assert client.post(f"/mesh/bravo/inbox/{message_id}", headers=_auth("alpha"), json={"ok": True}).status_code == 401
    assert client.post(f"/mesh/alpha/inbox/{message_id}", headers=_auth("alpha"), json={"ok": True}).status_code == 404


def test_the_policy_still_applies_to_pulled_messages(client, pair):
    message_id = mailbox.enqueue("alpha", "bravo", "message", "x")
    store.upsert_agent(Agent.model_validate({"id": "bravo", "type": "hermes", "host": "h1",
        "reach": {"endpoint": "http://bravo.test:9900", "network": "tailnet", "callers": []}}))

    assert client.get("/mesh/bravo", headers={**_auth("bravo"), **V2}).json()["inbox"] == []
    assert _state(message_id) == "failed"


# --- the host side, against real local HTTP servers --------------------------


class FakeNet:
    """One local HTTP server playing Beacon (GET /mesh/alpha, POST
    /mesh/alpha/inbox/<id>) and the agent itself (POST /agent)."""

    def __init__(self, config):
        self.config, self.reports, self.agent_calls, self.agent_up = config, [], [], True
        net = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                net.version = self.headers.get("X-Beacon-Sync-Version")
                self._json(200, net.config)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.startswith("/mesh/alpha/inbox/"):
                    net.reports.append((int(self.path.rsplit("/", 1)[1]), body))
                    return self._json(200, {"ok": True})
                net.agent_calls.append((self.headers["Authorization"], body["params"]["message"]))
                task = {"id": "t", "contextId": "c", "status": {"state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": "alpha here"}]}}}
                self._json(200, {"jsonrpc": "2.0", "id": body["id"], "result": {"task": task}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def host(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("hermes_mesh_sync", SYNC_PATH)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    restarts = []
    monkeypatch.setattr(sync.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(sync, "restart", lambda cfg, state: restarts.append(1))
    config = {"agent": "alpha", "beacon": None, "listen": {"port": 9900, "host": None}, "inbound": {},
              "outbound": [], "board": None,
              "inbox": [{"id": 7, "text": "[Delivered by the Beacon mailbox] hi", "token": "tok-bravo-alpha",
                         "context_id": "ctx-1"}]}
    net = FakeNet(config)
    config["beacon"] = net.url
    config["endpoint"] = f"{net.url}/agent"
    state = tmp_path / "state"
    state.mkdir()
    (state / "token").write_text("pull-token")
    env = tmp_path / ".env"
    env.write_text("A2A_PORT=9900\nA2A_AGENT_NAME=alpha\nA2A_PEER_TOKENS=\nA2A_TRUSTED_PEERS=\n")
    (state / "sync.json").write_text(json.dumps({"beacon": net.url, "agent": "alpha", "hermes": ["hermes"],
                                                 "env_file": str(env)}))
    yield sync, state, net, restarts
    net.server.shutdown()


def test_sync_delivers_held_messages_locally_and_reports(host):
    sync, state, net, restarts = host

    sync.sync(state)

    assert net.version == "2"
    (auth, message), = net.agent_calls
    assert auth == "Bearer tok-bravo-alpha"  # delivered as the original sender
    assert message["parts"][0]["text"].endswith("hi") and message["contextId"] == "ctx-1"
    assert net.reports == [(7, {"ok": True, "reply": "alpha here"})]


def test_an_agent_that_is_down_gets_it_handed_back(host):
    sync, state, net, restarts = host
    net.config["endpoint"] = "http://127.0.0.1:9/agent"  # nothing listens on port 9

    sync.sync(state)

    (message_id, report), = net.reports
    assert message_id == 7 and report["ok"] is False and report["transient"] is True


def test_a_restarting_agent_gets_nothing_this_run(host):
    sync, state, net, restarts = host
    net.config["inbound"] = {"bravo": "new-token"}  # changes who may call in: restart

    sync.sync(state)

    assert restarts == [1] and net.agent_calls == []
    assert net.reports == [(7, {"ok": False, "transient": True, "error": "agent restarting"})]
