"""Self-healing: the host sync probes its own agent and restarts a gateway that
stopped answering, where the fleet policy says `heal`; Beacon keeps what it
reports, notices agents that went quiet, and tells people."""

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backend import availability, mailbox, mesh, notify, opstore, policy, store
from backend.schemas import Agent, Host

SYNC_PATH = Path(__file__).parents[1] / "backend" / "drivers" / "hermes_mesh_sync.py"


@pytest.fixture
def sync():
    spec = importlib.util.spec_from_file_location("hermes_mesh_sync", SYNC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the probe, against real local HTTP servers -----------------------------


class FakeHermes:
    """/health and the JSON-RPC endpoint, behaving as told."""

    def __init__(self, health=None, rpc=None, rpc_status=200, delay=0.0):
        self.health = {"status": "ok"} if health is None else health
        self.rpc = {"jsonrpc": "2.0", "id": "x", "error": {"code": -32001, "message": "task not found"}} if rpc is None else rpc
        self.rpc_status, self.delay, self.calls = rpc_status, delay, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                time.sleep(fake.delay)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, fake.health)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append((self.headers["Authorization"], body["method"]))
                self._send(fake.rpc_status, fake.rpc)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def hermes():
    servers = []

    def make(**kw):
        servers.append(FakeHermes(**kw))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def test_a_healthy_agent_passes_both_checks(sync, hermes):
    agent = hermes()
    assert sync.probe_agent(agent.url, "tok") == ("ok", "")
    assert agent.calls == [("Bearer tok", "GetTask")]  # an authenticated no-op, no model turn


def test_a_gateway_on_old_settings_is_caught_by_the_token_check(sync, hermes):
    """/health is fine, but the gateway never loaded today's tokens."""
    for kw in ({"rpc_status": 401, "rpc": {"jsonrpc": "2.0", "id": "x", "error": {"code": -32050, "message": "unauthorized"}}},
               {"rpc": {"jsonrpc": "2.0", "id": "x", "error": {"code": -32052, "message": "untrusted peer"}}}):
        verdict, detail = sync.probe_agent(hermes(**kw).url, "tok")
        assert verdict == "agent" and "old settings" in detail


def test_nothing_listening_and_a_wrong_health_answer_are_the_agents_fault(sync, hermes):
    assert sync.probe_agent("http://127.0.0.1:9", "tok")[0] == "agent"
    assert sync.probe_agent(hermes(health={"hello": "web server"}).url, "tok")[0] == "agent"


def test_a_timeout_blames_the_agent_only_on_this_machine(sync, hermes, monkeypatch):
    """A laptop with its VPN down: its tailnet address times out. Restarting the
    gateway would not help, so that is the network's fault, not the agent's."""
    monkeypatch.setattr(sync, "PROBE_TIMEOUT", 0.3)
    assert sync.probe_agent(hermes(delay=1).url, "tok")[0] == "agent"  # 127.0.0.1 is this machine
    verdict, detail = sync.probe_agent("http://10.255.255.1:9900", "tok")
    assert verdict == "network" and "not this machine" in detail
    assert sync.probe_agent("http://no-such-host.invalid:9900", "tok")[0] == "network"


# --- the healing rules, on a controlled clock --------------------------------


@pytest.fixture
def ladder(sync, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    clock = {"now": 1_000_000.0}
    verdicts, restarts = [], []
    monkeypatch.setattr(sync.time, "time", lambda: clock["now"])
    monkeypatch.setattr(sync, "probe_agent", lambda e, t: verdicts.pop(0))
    monkeypatch.setattr(sync, "restart", lambda cfg, st: restarts.append(clock["now"]))

    def run(verdict, heal=True, after=300):
        clock["now"] += after
        verdicts.append(verdict)
        sync.check_health({}, state, {"endpoint": "http://x", "inbound": {"beacon": "t"}, "health": {"heal": heal}})
        return sync.load_health(state)

    run.restarts, run.state = restarts, state
    return run


FAIL, OK, NET = ("agent", "nothing listens"), ("ok", ""), ("network", "no route")


def test_two_failures_in_a_row_restart_the_gateway_once(ladder):
    assert ladder(FAIL)["state"] == "failing" and ladder.restarts == []
    assert ladder(FAIL)["state"] == "healing" and len(ladder.restarts) == 1
    assert ladder(OK) == {**ladder(OK), "state": "ok", "fails": []}


def test_failures_across_a_sleep_do_not_count_as_in_a_row(ladder):
    ladder(FAIL)
    ladder(FAIL, after=3 * 3600)  # the laptop slept between the two runs
    assert ladder.restarts == []


def test_without_heal_it_only_reports(ladder):
    ladder(FAIL, heal=False)
    health = ladder(FAIL, heal=False)
    assert health["state"] == "failing" and "healing is off" in health["detail"] and ladder.restarts == []


def test_a_network_problem_is_never_restarted(ladder):
    for _ in range(4):
        assert ladder(NET)["state"] == "network"
    assert ladder.restarts == []


def test_restarts_are_capped_then_it_gives_up(ladder):
    ladder(FAIL)
    ladder(FAIL)  # restart 1
    ladder(FAIL)
    health = ladder(FAIL)
    assert health["state"] == "failing" and "next restart allowed in" in health["detail"]
    assert len(ladder.restarts) == 1  # one an hour
    for _ in range(2):
        ladder(FAIL, after=3600), ladder(FAIL)  # restarts 2 and 3, an hour apart
    assert len(ladder.restarts) == 3
    ladder(FAIL, after=3600)
    health = ladder(FAIL)
    assert health["state"] == "gave-up" and "3 restarts today" in health["detail"] and len(ladder.restarts) == 3
    ladder(OK, after=86400)  # a day later, and working again: the budget is back
    ladder(FAIL), ladder(FAIL)
    assert len(ladder.restarts) == 4


def test_nothing_is_judged_while_a_restart_is_on_its_way(ladder):
    (ladder.state / "restart.pending").touch()
    ladder(FAIL), ladder(FAIL)
    assert ladder.restarts == [] and not (ladder.state / "health.json").exists()


def test_the_report_is_one_ascii_header_line(sync, ladder):
    ladder(("agent", "Verbindung abgelehnt:\nnichts hört zu"))
    headers = sync.health_headers(ladder.state)
    assert headers["X-Beacon-Health"] == "failing"
    assert "\n" not in headers["X-Beacon-Health-Detail"] and headers["X-Beacon-Health-Detail"].isascii()


# --- Beacon: the policy's health rules --------------------------------------


def _agent(id_, tags=(), owner=None):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "tags": list(tags), "owner": owner,
                                 "reach": {"endpoint": f"http://{id_}.test:9900"}})


RULES = """rules:
  - {from: ["*"], to: ["*"]}
health:
  - {for: ["*"], heal: true, quiet_after: 30m}
  - {for: ["tag:mobile"], quiet_after: 3d, mailbox_ttl: 7d}
  - {for: ["tag:family-*"], heal: false}
"""


def test_health_rules_merge_field_by_field_later_wins():
    rules = policy.parse(RULES)
    assert policy.health_for(rules, _agent("srv")) == {"heal": True, "quiet_after": 1800, "mailbox_ttl": 72 * 3600,
                                                       "remind_after": 86400}
    laptop = policy.health_for(rules, _agent("lap", ["mobile"]))
    assert laptop == {"heal": True, "quiet_after": 3 * 86400, "mailbox_ttl": 7 * 86400, "remind_after": 86400}
    assert policy.health_for(rules, _agent("kid", ["family-a", "mobile"]))["heal"] is False
    assert policy.health_for(None, _agent("x")) == policy.HEALTH_DEFAULTS  # no policy: nothing switched on
    never = policy.parse('health:\n  - {for: ["*"], quiet_after: never}\n')
    assert policy.health_for(never, _agent("x"))["quiet_after"] is None


def test_bad_health_rules_are_refused_and_typos_flagged(fleet):
    for bad in ('health:\n  - {for: ["*"], quiet_after: soon}\n', 'health:\n  - {for: ["*"], mailbox_ttl: 0m}\n',
                'health:\n  - {for: ["*"], heal: maybe}\n', 'health:\n  - {for: ["*"], restart: true}\n'):
        with pytest.raises(ValueError):
            policy.parse(bad)
    found = policy.problems(policy.parse('health:\n  - {for: ["tag:mobil"], quiet_after: 3d}\n'), [_agent("a", ["mobile"])])
    assert any("tag:mobil" in p for p in found)


@pytest.fixture
def fleet3(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("srv", owner="owner@example.com"), _agent("lap", ["mobile"]), _agent("other")):
        store.upsert_agent(agent)
    policy.save(RULES, "test")
    sent = []
    monkeypatch.setattr(notify, "_tell", lambda to, subject, body: sent.append((sorted(to), subject)))
    from backend import auth
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"op@example.com"}))
    return sent


def test_the_agent_gets_its_settings_and_the_preview_shows_them(fleet3):
    assert mesh.config("lap")["health"]["quiet_after"] == 3 * 86400
    preview = policy.preview(RULES, store.list_agents())
    assert preview["health"]["srv"]["heal"] is True and preview["health"]["lap"]["mailbox_ttl"] == 7 * 86400


def test_mail_waits_as_long_as_the_recipient_allows(fleet3):
    for recipient, ttl in (("lap", 7 * 86400), ("srv", 72 * 3600), ("beacon", mailbox.TTL_SECONDS)):
        message_id = mailbox.enqueue("other", recipient, "message", "hi")
        with opstore.connect() as db:
            row = db.execute("SELECT created_at, expires_at FROM mailbox WHERE id = ?", (message_id,)).fetchone()
        assert round(row["expires_at"] - row["created_at"]) == ttl


# --- Beacon: what the sync reports, and who hears ---------------------------


def _pull(client, agent_id, **headers):
    return client.get(f"/mesh/{agent_id}", headers={
        "Authorization": f"Bearer {mesh.token(agent_id, mesh.BEACON_PEER)}", **headers})


def test_healing_giving_up_and_recovering_reach_people_once(client, fleet3):
    sent = fleet3
    _pull(client, "srv", **{"X-Beacon-Health": "ok", "X-Beacon-Health-Detail": "-"})
    _pull(client, "srv", **{"X-Beacon-Health": "failing", "X-Beacon-Health-Detail": "nothing listens"})
    assert sent == []  # one failed probe is not news
    _pull(client, "srv", **{"X-Beacon-Health": "healing", "X-Beacon-Health-Detail": "nothing listens"})
    _pull(client, "srv", **{"X-Beacon-Health": "healing", "X-Beacon-Health-Detail": "nothing listens"})
    _pull(client, "srv", **{"X-Beacon-Health": "gave-up", "X-Beacon-Health-Detail": "3 restarts today did not help"})
    _pull(client, "srv", **{"X-Beacon-Health": "ok"})
    subjects = [s for _, s in sent]
    assert len(subjects) == 3, subjects
    assert "being restarted" in subjects[0] and "needs a person" in subjects[1] and "answers again" in subjects[2]
    assert sent[0][0] == ["op@example.com", "owner@example.com"]
    states = [e["state"] for e in client.get("/api/agents/srv/health-events").json()]
    assert states == ["ok", "gave-up", "healing", "failing", "ok"]  # newest first, one per change


def test_unknown_health_words_are_ignored(client, fleet3):
    _pull(client, "srv", **{"X-Beacon-Health": "exploded"})
    assert client.get("/api/agents/srv/health-events").json() == []


def test_a_quiet_agent_is_reported_once_and_its_return_too(client, fleet3):
    sent = fleet3
    for agent_id in ("srv", "lap", "other"):
        _pull(client, agent_id)
    with opstore.connect() as db:  # srv and lap went silent two hours ago
        db.execute("UPDATE agent_seen SET last_pull = ? WHERE agent IN ('srv', 'lap')", (time.time() - 7200,))

    _pull(client, "other")  # any pull looks around
    _pull(client, "other")
    assert [s for _, s in sent] == ["Beacon: srv has not synced for 2 h"]  # the laptop may sleep for 3 days

    _pull(client, "srv")
    assert [s for _, s in sent][-1] == "Beacon: srv is syncing again"
    _pull(client, "srv")
    assert len(sent) == 2


def test_the_agents_tab_says_away_within_quiet_after_and_offline_past_it(fleet3):
    now = time.time()
    lap, srv = store.get_agent("lap"), store.get_agent("srv")
    rules = policy.load()
    away = availability.classify(lap, False, "timed out", now - 7200, now, False,
                                 policy.health_for(rules, lap)["quiet_after"])
    assert away["state"] == "away" and "2 h ago" in away["detail"]
    gone = availability.classify(srv, False, "timed out", now - 7200, now, False,
                                 policy.health_for(rules, srv)["quiet_after"], ("gave-up", "3 restarts"))
    assert gone["state"] == "offline" and gone["health"] == "gave-up" and "gave-up" in gone["detail"]
