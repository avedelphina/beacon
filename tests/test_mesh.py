"""A2A mesh: derived pairwise tokens, the same-network peer rule, the pull
endpoint's auth, and the Hermes host side (bootstrap script + sync). Hosts
are faked: a shell-script `hermes` records its argv, and Beacon is a
file:// URL the sync reads like an HTTP response. No sockets, no real Hermes.
"""

import importlib.util
import json
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend import mesh, store
from backend.schemas import Agent, Host

SYNC_PATH = Path(__file__).parents[1] / "backend" / "drivers" / "hermes_mesh_sync.py"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")


def _agent(id_, network="tailnet", endpoint=None, profile=None, reach=True):
    record = {"id": id_, "type": "hermes", "host": "h1", "profile": profile}
    if reach:
        record["reach"] = {"endpoint": endpoint or f"http://{id_}.test:9900", "network": network,
                           "capabilities": ["chat"]}
    return Agent.model_validate(record)


@pytest.fixture
def two_agents(fleet, secret):
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(_agent("anais", endpoint="http://100.1.1.1:9900", profile="anais"))
    store.upsert_agent(_agent("anikke"))
    store.upsert_agent(_agent("lan-only", network="lan"))


# --- tokens and peers -------------------------------------------------------


def test_token_is_stable_pairwise_and_directional(secret):
    assert mesh.token("a", "b") == mesh.token("a", "b")
    assert len({mesh.token("a", "b"), mesh.token("b", "a"), mesh.token("a", "c")}) == 3
    assert not mesh.token("a", "b").startswith("-")


def test_token_without_secret_raises(monkeypatch):
    monkeypatch.delenv(mesh.SECRET_ENV, raising=False)
    with pytest.raises(mesh.MissingSecret, match="not set"):
        mesh.token("beacon", "a")


def test_peers_share_a_network_and_exclude_self_and_unreachable():
    fleet = [_agent("a"), _agent("b"), _agent("c", network="lan"), _agent("d", network=None), _agent("e", reach=False)]
    assert [p.id for p in mesh.peers(fleet[0], fleet)] == ["b"]
    assert mesh.peers(fleet[3], fleet) == []  # no network: Beacon-only


def test_config_lists_listen_inbound_and_outbound(two_agents):
    cfg = mesh.config("anais")

    assert cfg["agent"] == "anais"
    assert cfg["listen"] == {"port": 9900, "host": "100.1.1.1"}
    assert cfg["inbound"] == {"beacon": mesh.token("beacon", "anais"), "anikke": mesh.token("anikke", "anais")}
    assert cfg["outbound"] == [{"name": "anikke", "url": "http://anikke.test:9900",
                                "token": mesh.token("anais", "anikke"), "capabilities": ["chat"]}]


def test_loopback_endpoint_has_no_listen_host(fleet, secret):
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(_agent("solo", endpoint="http://127.0.0.1:9901", network=None))

    cfg = mesh.config("solo")

    assert cfg["listen"] == {"port": 9901, "host": None}
    assert list(cfg["inbound"]) == ["beacon"] and cfg["outbound"] == []


# --- the pull endpoint ------------------------------------------------------


def _pull(client, agent_id, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(f"/mesh/{agent_id}", headers=headers)


def test_agent_pulls_its_own_config(client, two_agents):
    response = _pull(client, "anais", mesh.token("anais", "beacon"))
    assert response.status_code == 200
    assert response.json() == mesh.config("anais")


def test_pull_refuses_anything_but_the_agents_own_token(client, two_agents):
    assert _pull(client, "anais", None).status_code == 401
    assert _pull(client, "anais", "wrong").status_code == 401
    assert _pull(client, "anais", mesh.token("anikke", "beacon")).status_code == 401  # another agent's
    assert _pull(client, "anais", mesh.token("beacon", "anais")).status_code == 401  # Beacon's A2A token


def test_pull_ignores_mcp_bearer_and_needs_no_session(two_agents, monkeypatch):
    from fastapi.testclient import TestClient

    from backend import auth
    from backend.app import app

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")
    anonymous = TestClient(app)

    assert _pull(anonymous, "anais", "test-mcp-token").status_code == 401
    assert _pull(anonymous, "anais", mesh.token("anais", "beacon")).status_code == 200


def test_pull_checks_token_before_existence(client, two_agents):
    assert _pull(client, "ghost", "wrong").status_code == 401
    assert _pull(client, "ghost", mesh.token("ghost", "beacon")).status_code == 404


# --- host side: bootstrap and sync ------------------------------------------


@pytest.fixture
def host(tmp_path):
    """A fake host: profile dir with config.yaml/.env, a recording `hermes`
    where the script's PATH looks first, and a file:// "Beacon"."""
    home = tmp_path / "home"
    profile = home / ".hermes" / "profiles" / "anais"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n")
    (profile / ".env").write_text("OPENAI_API_KEY=keep-me\nA2A_TOKEN_GONE=stale\nA2A_PORT=1\n")
    # PREAMBLE puts $HOME/.local/bin first on PATH, so the fake lives there
    # and HOME is the tmp dir — a real `hermes` must never be reached.
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    argv_log = tmp_path / "argv.log"
    fake = bin_dir / "hermes"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {argv_log}\n'
                    f'case "$*" in *"config path"*) echo {profile / "config.yaml"};; esac\nexit 0\n')
    fake.chmod(0o755)
    beacon = tmp_path / "beacon"
    (beacon / "mesh").mkdir(parents=True)
    return {"home": home, "profile": profile, "argv": argv_log, "beacon": beacon, "hermes": fake}


def _serve(host, cfg):
    (host["beacon"] / "mesh" / cfg["agent"]).write_text(json.dumps(cfg))


def _argv(host):
    return host["argv"].read_text() if host["argv"].exists() else ""


def test_install_script_bootstraps_without_scheduling(two_agents, host, monkeypatch):
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, f"file://{host['beacon']}")
    _serve(host, mesh.config("anais"))
    env = {"HOME": str(host["home"]), "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
    script = mesh.install_script("anais")

    out = subprocess.run(["bash", "-s"], input=script, text=True, env=env, capture_output=True, check=True).stdout

    state = host["profile"] / "beacon-mesh"
    token_file = state / "token"
    assert token_file.read_text().strip() == mesh.token("anais", "beacon")
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    cfg = json.loads((state / "sync.json").read_text())
    assert cfg["agent"] == "anais" and cfg["hermes"][1:] == ["-p", "anais"]
    assert cfg["env_file"] == str(host["profile"] / ".env")

    dotenv = (host["profile"] / ".env").read_text()
    assert "OPENAI_API_KEY=keep-me" in dotenv
    assert "A2A_TOKEN_GONE" not in dotenv and "A2A_PORT=1\n" not in dotenv
    assert f"A2A_TOKEN_ANIKKE={mesh.token('anais', 'anikke')}" in dotenv
    assert "A2A_HOST=100.1.1.1" in dotenv

    argv = _argv(host)
    assert "-p anais tools enable a2a --platform a2a" in argv
    assert '"token": "${env:A2A_TOKEN_ANIKKE}"' in argv
    assert mesh.token("anais", "anikke") not in argv and mesh.token("anais", "beacon") not in argv
    # It prints the schedule; adding it is the operator's step.
    assert "*/5 * * * *" in out and "crontab" not in script


def _load_sync():
    spec = importlib.util.spec_from_file_location("hermes_mesh_sync", SYNC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def synced(two_agents, host):
    """A state dir as `install` leaves it, already synced once."""
    state = host["profile"] / "beacon-mesh"
    state.mkdir()
    (state / "token").write_text(mesh.token("anais", "beacon"))
    (state / "sync.json").write_text(json.dumps({
        "beacon": f"file://{host['beacon']}", "agent": "anais",
        "hermes": [str(host["hermes"]), "-p", "anais"], "env_file": str(host["profile"] / ".env")}))
    _serve(host, mesh.config("anais"))
    sync = _load_sync()
    assert sync.sync(state) == 0
    # The first sync changes A2A_PORT, so it restarts — detached; let that
    # land before clearing the log, or it shows up in the next test's argv.
    assert _wait_for_restart(host)
    host["argv"].unlink()
    return sync, state


def _wait_for_restart(host, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "gateway restart" in _argv(host):
            return True
        time.sleep(0.05)
    return False


def test_sync_is_a_noop_when_nothing_changed(synced, host):
    sync, state = synced
    before = (host["profile"] / ".env").read_text()

    assert sync.sync(state) == 0

    assert _argv(host) == ""
    assert (host["profile"] / ".env").read_text() == before


def test_new_outbound_peer_applies_without_restart(synced, host):
    sync, state = synced
    cfg = mesh.config("anais")
    cfg["outbound"].append({"name": "saga", "url": "http://saga.test:9900", "token": "t-saga", "capabilities": []})
    _serve(host, cfg)

    sync.sync(state)

    assert "A2A_TOKEN_SAGA=t-saga" in (host["profile"] / ".env").read_text()
    assert "config set --force a2a_agents" in _argv(host) and '"saga"' in _argv(host)
    assert not _wait_for_restart(host, timeout=0.5)


def test_new_inbound_peer_triggers_graceful_restart(synced, host):
    sync, state = synced
    cfg = mesh.config("anais")
    cfg["inbound"]["saga"] = "t-saga-in"
    _serve(host, cfg)

    sync.sync(state)

    assert "saga:t-saga-in" in (host["profile"] / ".env").read_text()
    assert _wait_for_restart(host)


def test_unreachable_beacon_keeps_last_config(synced, host):
    sync, state = synced
    before = (host["profile"] / ".env").read_text()
    (host["beacon"] / "mesh" / "anais").unlink()

    assert sync.sync(state) == 0

    assert (host["profile"] / ".env").read_text() == before
    assert _argv(host) == ""


def test_overlapping_run_is_skipped(synced, host):
    import fcntl

    sync, state = synced
    with open(state / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        cfg = mesh.config("anais")
        cfg["inbound"]["saga"] = "t"
        _serve(host, cfg)
        assert sync.sync(state) == 0
    assert "saga" not in (host["profile"] / ".env").read_text()


def test_cli_install_errors_cleanly(fleet, secret, capsys):
    assert mesh.main(["install", "nope"]) == 1
    assert "error:" in capsys.readouterr().err


def test_install_needs_public_url(two_agents, monkeypatch):
    monkeypatch.delenv(mesh.PUBLIC_URL_ENV, raising=False)
    with pytest.raises(ValueError, match=mesh.PUBLIC_URL_ENV):
        mesh.install_script("anais")


def test_sync_script_parses_as_python38():
    # Hosts run it on whatever python3 they have; keep it free of 3.10+ syntax.
    import ast

    ast.parse(SYNC_PATH.read_text(), feature_version=(3, 8))
