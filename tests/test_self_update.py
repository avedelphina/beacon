"""The host sync replaces itself with the copy Beacon serves, only where the
policy says `update_sync`; and any member may hand cards to the agents the
policy opens to members (`members_may_assign`)."""

import hashlib
import importlib.util
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backend import availability, mesh, policy, store
from backend.schemas import Agent
from tests.test_members import ANN, BOB, TOM, as_person, people  # noqa: F401  (the people fixture)

SYNC_PATH = Path(__file__).parents[1] / "backend" / "drivers" / "hermes_mesh_sync.py"
NEWER = SYNC_PATH.read_text() + "\n# a newer copy\n"


class FakeBeacon:
    def __init__(self, body: bytes, status=200):
        outer = self
        self.hits, self.auth = 0, []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                outer.auth.append(self.headers.get("Authorization"))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def host(tmp_path):
    """An installed sync: its own copy of the script, and a state directory."""
    script = tmp_path / "sync.py"
    shutil.copy(SYNC_PATH, script)
    script.chmod(0o750)
    spec = importlib.util.spec_from_file_location("installed_sync", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "state").mkdir()
    return module, script, tmp_path / "state"


def _offer(body: bytes, url: str, digest=None):
    return {"sha256": digest or hashlib.sha256(body).hexdigest(), "url": f"{url}/mesh/a/sync.py"}


@pytest.fixture
def beacon(request):
    made = []

    def make(body: bytes, status=200):
        made.append(FakeBeacon(body, status))
        return made[-1]

    yield make
    for b in made:
        b.close()


def test_a_newer_copy_replaces_the_script_and_keeps_the_old_one(host, beacon):
    module, script, state = host
    old = script.read_bytes()
    fake = beacon(NEWER.encode())
    module.self_update({"beacon": fake.url}, state, "tok", _offer(NEWER.encode(), fake.url))
    assert script.read_text() == NEWER and script.stat().st_mode & 0o777 == 0o750
    assert (script.parent / "sync.py.prev").read_bytes() == old and not (script.parent / "sync.py.new").exists()
    assert fake.auth == ["Bearer tok"]
    module.self_update({"beacon": fake.url}, state, "tok", _offer(NEWER.encode(), fake.url, module.own_hash()))
    assert fake.hits == 1  # already what Beacon offers: nothing fetched


def test_a_copy_that_is_not_what_was_announced_is_refused(host, beacon):
    module, script, state = host
    fake = beacon(NEWER.encode())
    module.self_update({"beacon": fake.url}, state, "tok", _offer(NEWER.encode(), fake.url, "0" * 64))
    assert script.read_text() == SYNC_PATH.read_text() and not (script.parent / "sync.py.new").exists()
    module.self_update({"beacon": fake.url}, state, "tok", _offer(NEWER.encode(), fake.url, "0" * 64))
    assert fake.hits == 1  # not tried again within the hour


def test_a_copy_that_does_not_start_is_never_swapped_in(host, beacon):
    module, script, state = host
    broken = b"def sync(:\n"
    fake = beacon(broken)
    module.self_update({"beacon": fake.url}, state, "tok", _offer(broken, fake.url))
    assert script.read_text() == SYNC_PATH.read_text()
    assert not (script.parent / "sync.py.prev").exists() and (state / "update.failed").exists()


def test_the_pull_token_goes_only_to_beacons_own_address(host, beacon):
    module, script, state = host
    elsewhere = beacon(NEWER.encode())
    module.self_update({"beacon": "https://beacon.invalid"}, state, "tok", _offer(NEWER.encode(), elsewhere.url))
    module.self_update({"beacon": "https://beacon.invalid"}, state, "tok", None)
    assert elsewhere.hits == 0 and script.read_text() == SYNC_PATH.read_text()


def test_a_failed_download_is_retried_after_an_hour(host, beacon, monkeypatch):
    module, script, state = host
    fake = beacon(b"", status=404)
    offer = _offer(NEWER.encode(), fake.url)
    module.self_update({"beacon": fake.url}, state, "tok", offer)
    module.self_update({"beacon": fake.url}, state, "tok", offer)
    assert fake.hits == 1
    clock = module.time.time() + module.UPDATE_RETRY_SECONDS + 1
    monkeypatch.setattr(module.time, "time", lambda: clock)
    module.self_update({"beacon": fake.url}, state, "tok", offer)
    assert fake.hits == 2


# --- Beacon: who is offered an update, and what it knows of each host --------


def _agent(id_, tags=()):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "tags": list(tags),
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": True}})


@pytest.fixture
def served(fleet, monkeypatch):
    from backend.schemas import Host
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, "http://beacon.test:8642")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("on"), _agent("off", ["frozen"])):
        store.upsert_agent(agent)
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\nhealth:\n  - {for: ["*"], update_sync: true}\n'
                '  - {for: ["tag:frozen"], update_sync: false}\n', "test")


def _get(client, agent, path="", **headers):
    return client.get(f"/mesh/{agent}{path}", headers={"Authorization": f"Bearer {mesh.token(agent, mesh.BEACON_PEER)}",
                                                       **headers})


def test_the_offer_follows_the_policy_and_is_off_by_default(client, served):
    digest = hashlib.sha256(SYNC_PATH.read_bytes()).hexdigest()
    assert _get(client, "on").json()["sync"] == {"sha256": digest, "url": "http://beacon.test:8642/mesh/on/sync.py"}
    assert _get(client, "off").json()["sync"] is None
    policy.save("rules: []\n", "test")  # no health entry: nothing offered
    assert _get(client, "on").json()["sync"] is None


def test_the_script_is_served_only_to_its_agent_and_only_when_offered(client, served):
    ok = _get(client, "on", "/sync.py")
    assert ok.status_code == 200 and ok.content == SYNC_PATH.read_bytes()
    assert _get(client, "off", "/sync.py").status_code == 404
    assert client.get("/mesh/on/sync.py").status_code == 401
    assert client.get("/mesh/on/sync.py", headers={"Authorization": f"Bearer {mesh.token('off', mesh.BEACON_PEER)}"}
                      ).status_code == 401


def test_beacon_knows_which_copy_each_host_runs(client, served):
    digest = hashlib.sha256(SYNC_PATH.read_bytes()).hexdigest()
    _get(client, "on", **{"X-Beacon-Sync-Hash": digest})
    _get(client, "off", **{"X-Beacon-Sync-Hash": "ab" * 32})
    result = availability.check_all(store.list_agents())
    assert (result["on"]["sync"], result["off"]["sync"]) == ("current", "outdated")
    assert availability.sync_status(store.get_agent("on"), None) == "unknown"  # a sync too old to say


# --- members may hand cards to the agents the policy opens to them -----------


def test_members_may_assign_to_agents_the_policy_opens_to_them(people):
    ann, bob, tom = people["ann"], people["bob"], people["tom"]
    assert ann.get("/api/board/assignees").json() == ["a1", "a2"]
    assert tom.get("/api/board/assignees").json() == ["a1", "a2", "b1", "n1"]
    assert ann.post("/api/board/cards", json={"title": "x", "assignee": "b1"}).status_code == 403

    policy.save('members_may_assign: ["b1", "tag:coordinator"]\n', "test")
    assert ann.get("/api/board/assignees").json() == ["a1", "a2", "b1"]
    card = ann.post("/api/board/cards", json={"title": "Organise this", "assignee": "b1"}).json()
    assert card["assignee"] == "b1"
    assert ann.post("/api/board/cards", json={"title": "x", "assignee": "n1"}).status_code == 403  # still not open

    store.upsert_agent(store.get_agent("n1").model_copy(update={"tags": ["coordinator"]}))
    assert ann.get("/api/board/assignees").json() == ["a1", "a2", "b1", "n1"]


def test_a_member_may_keep_a_cards_assignee_when_editing_something_else(people):
    # A card Ann made, which an operator then gave to Bob's agent.
    mine = people["ann"].post("/api/board/cards", json={"title": "Mine", "assignee": "a1"}).json()["id"]
    people["tom"].patch(f"/api/board/cards/{mine}", json={"assignee": "b1"})
    edited = people["ann"].patch(f"/api/board/cards/{mine}", json={"assignee": "b1", "note": "more detail"})
    assert edited.status_code == 200
    assert people["ann"].patch(f"/api/board/cards/{mine}", json={"assignee": "n1"}).status_code == 403


def test_the_policy_checks_members_may_assign_and_shows_who_it_opens(served):
    fleet = store.list_agents()
    shown = policy.preview('members_may_assign: ["on", "tag:frozen", "ghost"]\n', fleet)
    assert shown["members_may_assign"] == ["off", "on"]
    assert any("ghost" in p for p in shown["problems"])
    with pytest.raises(ValueError):
        policy.parse('members_may_assign: ["not a selector!"]\n')
