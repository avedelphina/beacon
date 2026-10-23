"""A2A mesh: derived pairwise tokens, the same-network peer rule, and the
Hermes host script. Pure functions plus the fleet fixture — no host contact."""

import json
import re
import shlex

import pytest

from backend import mesh, store
from backend.schemas import Agent, Host


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")


def _agent(id_, network="tailnet", endpoint=None, profile=None, reach=True):
    record = {"id": id_, "type": "hermes", "host": "h1", "profile": profile}
    if reach:
        record["reach"] = {"endpoint": endpoint or f"http://{id_}.test:9900", "network": network,
                           "capabilities": ["chat"]}
    return Agent.model_validate(record)


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


def _settings(script: str) -> dict[str, str]:
    """.env heredoc lines and `config set KEY VALUE` lines -> {KEY: VALUE}."""
    out = {}
    for line in script.splitlines():
        if re.match(r"^A2A_[A-Z0-9_]+=", line):
            key, value = line.split("=", 1)
            out[key] = value
        elif " config set " in line:
            words = shlex.split(line)
            out[words[-2]] = words[-1]
    return out


def test_hermes_script_wires_inbound_and_outbound(fleet, secret):
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1",
                                           "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("anais", endpoint="http://100.1.1.1:9900", profile="anais"), _agent("anikke"),
                  _agent("lan-only", network="lan")):
        store.upsert_agent(agent)

    script = mesh.render("anais")
    settings = _settings(script)

    assert "hermes -p anais config set" in script
    assert settings["A2A_PORT"] == "9900"
    assert settings["A2A_HOST"] == "100.1.1.1"
    assert settings["A2A_AGENT_NAME"] == "anais"
    assert settings["A2A_PEER_TOKENS"] == (
        f"beacon:{mesh.token('beacon', 'anais')},anikke:{mesh.token('anikke', 'anais')}")
    assert settings["A2A_TRUSTED_PEERS"] == "beacon,anikke"
    assert settings["A2A_TOKEN_ANIKKE"] == mesh.token("anais", "anikke")
    assert json.loads(settings["a2a_agents"]) == {"anikke": {
        "url": "http://anikke.test:9900",
        "auth": {"type": "bearer", "token": "${env:A2A_TOKEN_ANIKKE}"},
        "capabilities": ["chat"],
    }}
    # config.yaml carries refs only; the outbound secret appears once, in .env.
    assert script.count(mesh.token("anais", "anikke")) == 1


def test_loopback_endpoint_skips_a2a_host(fleet, secret):
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1",
                                           "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(_agent("solo", endpoint="http://127.0.0.1:9901", network=None))

    settings = _settings(mesh.render("solo"))

    assert "A2A_HOST" not in settings
    assert settings["A2A_PORT"] == "9901"
    assert settings["A2A_TRUSTED_PEERS"] == "beacon"
    assert json.loads(settings["a2a_agents"]) == {}


def test_cli_render_errors_cleanly(fleet, secret, capsys):
    assert mesh.main(["render", "nope"]) == 1
    assert "error:" in capsys.readouterr().err


def test_hermes_script_writes_env_via_heredoc_not_argv(fleet, secret, tmp_path):
    """Run the script for real against a fake `hermes`: .env gets the A2A
    lines (stale ones replaced, others kept), tokens never reach argv."""
    import os
    import subprocess

    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1",
                                           "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(_agent("anais", endpoint="http://100.1.1.1:9900", profile="anais"))
    store.upsert_agent(_agent("anikke"))
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    (home / ".env").write_text("OPENAI_API_KEY=keep-me\nA2A_TOKEN_GONE=stale\nA2A_PORT=1\n")
    # PREAMBLE puts $HOME/.local/bin first on PATH, so the fake lives there
    # and HOME points at tmp_path — a real `hermes` must never be reached.
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    argv_log = tmp_path / "argv.log"
    fake = bin_dir / "hermes"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {argv_log}\n'
                    f'[ "$3 $4" = "config path" ] && echo {home / "config.yaml"}\nexit 0\n')
    fake.chmod(0o755)

    subprocess.run(["bash", "-s"], input=mesh.render("anais"), text=True, check=True,
                   env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})

    env = (home / ".env").read_text()
    assert "OPENAI_API_KEY=keep-me" in env
    assert "A2A_TOKEN_GONE" not in env and "A2A_PORT=1\n" not in env
    assert f"A2A_TOKEN_ANIKKE={mesh.token('anais', 'anikke')}" in env
    assert env.count("A2A_PORT=") == 1
    argv = argv_log.read_text()
    assert "config set --force a2a_agents" in argv
    assert mesh.token("anais", "anikke") not in argv
