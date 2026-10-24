"""The host sync only touches what Beacon created. A2A settings an agent's owner
added (a peer, a caller, a token for an outside service) survive every sync;
Beacon's own entries are added, updated, and removed, tracked in managed.json.
A fresh install adopts nothing; `--adopt` marks the current state as Beacon's."""

import importlib.util
import json
from pathlib import Path

import pytest

SYNC_PATH = Path(__file__).parents[1] / "backend" / "drivers" / "hermes_mesh_sync.py"


def _mesh(peers=("bravo",), inbound=("bravo",), board=None):
    return {
        "agent": "alpha", "beacon": None, "listen": {"port": 9900, "host": "10.0.0.5"},
        "inbound": {"beacon": "tk-beacon", **{n: f"tk-{n}-in" for n in inbound}},
        "outbound": [{"name": n, "url": f"http://{n}.test:9900", "token": f"tk-{n}-out", "capabilities": []}
                     for n in peers],
        "board": board, "skills": [],
    }


@pytest.fixture
def host(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("hermes_mesh_sync", SYNC_PATH)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    monkeypatch.setattr(sync, "restart", lambda cfg, state: None)

    log = tmp_path / "hermes.log"
    fake = tmp_path / "hermes"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n'
                    'case "$*" in *"config unset a2a_agents.gone-already"*) echo "Config key not set: x"; exit 1;; esac\nexit 0\n')
    fake.chmod(0o755)
    beacon = tmp_path / "beacon" / "mesh"
    beacon.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    (state / "token").write_text("tok")
    env = tmp_path / ".env"
    (state / "sync.json").write_text(json.dumps({"beacon": f"file://{tmp_path / 'beacon'}", "agent": "alpha",
                                                 "hermes": [str(fake)], "env_file": str(env)}))

    class Host:
        pass

    h = Host()
    h.sync, h.state, h.env, h.log, h.beacon_dir = sync, state, env, log, beacon
    h.serve = lambda mesh: (beacon / "alpha").write_text(json.dumps({**mesh, "beacon": f"file://{tmp_path / 'beacon'}"}))
    h.calls = lambda: log.read_text().splitlines() if log.exists() else []
    h.managed = lambda: json.loads((state / "managed.json").read_text())
    h.values = lambda: dict(line.split("=", 1) for line in env.read_text().splitlines() if "=" in line)
    return h


def test_a_fresh_install_adopts_nothing_and_keeps_what_the_owner_added(host):
    host.env.write_text("A2A_PEER_TOKENS=mine:tk-mine\nA2A_TRUSTED_PEERS=mine\nA2A_TOKEN_MINE=tk-mine-out\nKEEP=1\n")
    host.serve(_mesh())

    host.sync.sync(host.state)

    values = host.values()
    assert values["A2A_PEER_TOKENS"].split(",") == ["mine:tk-mine", "beacon:tk-beacon", "bravo:tk-bravo-in"]
    assert values["A2A_TRUSTED_PEERS"].split(",") == ["mine", "beacon", "bravo"]
    assert values["A2A_TOKEN_MINE"] == "tk-mine-out" and values["KEEP"] == "1"
    assert values["A2A_TOKEN_BRAVO"] == "tk-bravo-out"
    assert host.calls() == ["config set --force a2a_agents.bravo " + json.dumps(host.sync.peer_list(_mesh())["bravo"])]
    assert host.managed()["inbound"] == ["beacon", "bravo"] and host.managed()["token_envs"] == ["A2A_TOKEN_BRAVO"]


def test_a_peer_dropped_by_beacon_is_removed_but_the_owners_peers_stay(host):
    host.env.write_text("A2A_PEER_TOKENS=mine:tk-mine\nA2A_TRUSTED_PEERS=mine\n")
    host.serve(_mesh(peers=("bravo", "charlie"), inbound=("bravo", "charlie")))
    host.sync.sync(host.state)
    host.log.unlink()

    host.serve(_mesh(peers=("bravo",), inbound=("bravo",)))  # the policy no longer lets charlie in or out
    host.sync.sync(host.state)

    assert host.calls() == ["config unset a2a_agents.charlie"]  # and nothing else, `mine` untouched
    values = host.values()
    assert values["A2A_PEER_TOKENS"].split(",") == ["mine:tk-mine", "beacon:tk-beacon", "bravo:tk-bravo-in"]
    assert "A2A_TOKEN_CHARLIE" not in values  # Beacon's own token variable goes with the peer
    assert "charlie" not in host.managed()["peers"] and "charlie" not in host.managed()["inbound"]


def test_an_unchanged_sync_writes_nothing(host):
    host.serve(_mesh())
    host.sync.sync(host.state)
    host.log.unlink()
    before = host.env.read_text()

    host.sync.sync(host.state)

    assert host.calls() == [] and host.env.read_text() == before


def test_beacon_wins_when_an_owner_entry_has_a_beacon_name(host):
    host.env.write_text("A2A_PEER_TOKENS=bravo:old-token\nA2A_TRUSTED_PEERS=bravo\n")
    host.serve(_mesh())

    host.sync.sync(host.state)

    values = host.values()
    assert values["A2A_PEER_TOKENS"].split(",") == ["beacon:tk-beacon", "bravo:tk-bravo-in"]
    assert values["A2A_TRUSTED_PEERS"].split(",") == ["beacon", "bravo"]


def test_removing_a_peer_that_is_already_gone_is_fine(host):
    host.serve(_mesh(peers=("gone-already",), inbound=()))
    host.sync.sync(host.state)
    host.serve(_mesh(peers=(), inbound=()))

    host.sync.sync(host.state)  # the fake hermes answers "Config key not set" with exit 1

    assert "gone-already" not in host.managed()["peers"]


def test_a_failed_step_is_picked_up_on_the_next_run(host, tmp_path):
    host.serve(_mesh(peers=("bravo", "charlie"), inbound=()))
    original = host.sync.run_hermes
    seen = []

    def flaky(cfg, args, tolerate=""):
        seen.append(args[-2])
        if args[-2] == "a2a_agents.charlie" and len(seen) < 3:
            raise RuntimeError("hermes failed")
        return original(cfg, args, tolerate)

    host.sync.run_hermes = flaky
    with pytest.raises(RuntimeError):
        host.sync.sync(host.state)
    assert list(host.managed()["peers"]) == ["bravo"]  # bravo was recorded before the failure

    host.sync.sync(host.state)

    assert sorted(host.managed()["peers"]) == ["bravo", "charlie"]
    assert seen.count("a2a_agents.bravo") == 1  # not redone


def test_adopt_marks_the_current_state_as_beacons_without_changing_anything(host):
    host.env.write_text("A2A_PEER_TOKENS=beacon:tk-beacon,bravo:tk-bravo-in\nA2A_TRUSTED_PEERS=beacon,bravo\n"
                        "A2A_TOKEN_BRAVO=tk-bravo-out\n")
    host.serve(_mesh())
    before = host.env.read_text()

    host.sync.adopt(host.state)

    assert host.env.read_text() == before and host.calls() == []
    assert host.managed()["peers"] == {"bravo": ""} and host.managed()["inbound"] == ["beacon", "bravo"]

    host.sync.sync(host.state)  # nothing is skipped on trust: each peer is written once
    assert host.calls() == ["config set --force a2a_agents.bravo " + json.dumps(host.sync.peer_list(_mesh())["bravo"])]
    host.log.unlink()

    host.serve(_mesh(peers=(), inbound=()))  # now that Beacon owns them, it may remove them
    host.sync.sync(host.state)
    assert host.values()["A2A_PEER_TOKENS"] == "beacon:tk-beacon"
    assert host.calls() == ["config unset a2a_agents.bravo"]
