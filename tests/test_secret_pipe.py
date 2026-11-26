"""Secrets pass through Beacon into and out of files (backend/secret_pipe.py).

The host scripts run here in a real shell with HOME in a temp directory: what
matters is what lands on disk, with which mode, and that the value is never in
a command, the audit or the store.
"""

import base64
import os
import stat
import subprocess

import pytest
from conftest import make_agent, make_host

from backend import audit, mesh, notify, opstore, secret_pipe, store
from backend.schemas import SecretError
from backend.ssh import SSHResult

VALUE = "s3cret value\nwith 'quotes' and $dollars"


@pytest.fixture
def hosts(fleet, fake_ssh, tmp_path, monkeypatch):
    """Two agents whose "SSH" is a local bash, one HOME each."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    for id_ in ("alpha", "bravo"):
        store.upsert_host(make_host(id=f"{id_}-host", user="deploy"))
        store.upsert_agent(make_agent(id=id_, host=f"{id_}-host"))
        (tmp_path / id_).mkdir()
    monkeypatch.setenv("BEACON_A2A_SECRET", "test")

    def run(host, command, timeout=10, input=None):
        fake_ssh.calls.append(command)
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / host.id.removesuffix("-host"))}
        proc = subprocess.run(["bash", "-c", command], input=input, capture_output=True, text=True, env=env)
        return SSHResult(ok=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)

    monkeypatch.setattr(fake_ssh, "run", run)
    return {id_: tmp_path / id_ / ".hermes" / "beacon-secrets" for id_ in ("alpha", "bravo")}


def _leave(box, name, value=VALUE):
    (box / "outbox").mkdir(parents=True, exist_ok=True)
    (box / "outbox" / name).write_text(value)


def _nowhere_but_on_hosts(fake_ssh):
    encoded = base64.b64encode(VALUE.encode()).decode()
    with opstore.connect() as db:
        dump = "\n".join(db.iterdump())
    for text in (*fake_ssh.calls, dump):
        assert VALUE not in text and encoded not in text


def test_put_writes_a_private_file_and_keeps_nothing(hosts, fake_ssh):
    path = secret_pipe.put("alpha", "api-key", VALUE.encode())
    target = hosts["alpha"] / "inbox" / "api-key"
    assert path.endswith("beacon-secrets/inbox/api-key") and target.read_text() == VALUE
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert list(target.parent.iterdir()) == [target]  # no temp file left
    _nowhere_but_on_hosts(fake_ssh)


def test_a_bad_name_never_reaches_a_host(hosts, fake_ssh):
    for name in ("../x", ".hidden", "a b", "a;rm", "x" * 65):
        with pytest.raises(SecretError):
            secret_pipe.put("alpha", name, b"v")
    assert fake_ssh.calls == []


def test_a_symlink_is_not_followed(hosts, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("private")
    (hosts["alpha"] / "outbox").mkdir(parents=True)
    (hosts["alpha"] / "outbox" / "link").symlink_to(outside)
    assert secret_pipe.names("alpha") == []
    with pytest.raises(SecretError, match="no such secret"):
        secret_pipe.read("alpha", "link")


def test_send_moves_it_to_the_peer_and_records_names_only(hosts, fake_ssh):
    _leave(hosts["alpha"], "token")
    path = secret_pipe.send("alpha", "bravo", "token")
    assert path.endswith("inbox/from-alpha.token")
    assert (hosts["bravo"] / "inbox" / "from-alpha.token").read_text() == VALUE
    assert not (hosts["alpha"] / "outbox" / "token").exists()
    assert audit.recent()[0]["capability"] == "secret_send" and audit.recent()[0]["outcome"] == "succeeded"
    _nowhere_but_on_hosts(fake_ssh)


def test_send_keeps_the_senders_file_when_the_peer_cannot_take_it(hosts):
    _leave(hosts["alpha"], "token")
    (hosts["bravo"] / "inbox" / "from-alpha.token").mkdir(parents=True)
    with pytest.raises(SecretError):
        secret_pipe.send("alpha", "bravo", "token")
    assert (hosts["alpha"] / "outbox" / "token").read_text() == VALUE
    assert audit.recent()[0]["outcome"] == "failed"


def test_send_follows_the_access_policy(hosts, monkeypatch):
    _leave(hosts["alpha"], "token")
    monkeypatch.setattr(mesh, "may_call", lambda caller, target: False)
    with pytest.raises(SecretError, match="may not send"):
        secret_pipe.send("alpha", "bravo", "token")
    assert (hosts["alpha"] / "outbox" / "token").exists()


def test_offer_tells_people_the_name(hosts, monkeypatch):
    told = []
    monkeypatch.setattr(notify, "secret_offered", lambda agent, name: told.append((agent, name)))
    with pytest.raises(SecretError):
        secret_pipe.offer("alpha", "token")
    _leave(hosts["alpha"], "token")
    secret_pipe.offer("alpha", "token")
    assert told == [("alpha", "token")]


def test_a_person_pastes_and_reveals_once(hosts, human_client, fake_ssh):
    headers = {"origin": "http://testserver"}
    asked = human_client.post("/api/agents/alpha/secrets", json={"name": "k", "value": VALUE}, headers=headers).json()
    assert asked["would_run"] and not hosts["alpha"].exists()
    assert human_client.post("/api/agents/alpha/secrets", json={"name": "k", "value": VALUE, "confirm": True},
                             headers=headers).json()["ok"]
    _leave(hosts["alpha"], "back")
    assert human_client.get("/api/agents/alpha/secrets").json() == {"names": ["back"]}
    shown = human_client.post("/api/agents/alpha/secrets/back/reveal?confirm=true", headers=headers)
    assert shown.json()["value"] == VALUE and shown.headers["cache-control"] == "no-store"
    assert not (hosts["alpha"] / "outbox" / "back").exists()
    _nowhere_but_on_hosts(fake_ssh)


def test_the_mcp_token_gets_nothing(hosts, mcp_client):
    _leave(hosts["alpha"], "back")
    assert mcp_client.get("/api/agents/alpha/secrets").status_code == 403
    assert mcp_client.post("/api/agents/alpha/secrets", json={"name": "k", "value": "v", "confirm": True}).status_code == 403
    assert mcp_client.post("/api/agents/alpha/secrets/back/reveal?confirm=true").status_code == 403


def test_a_container_agent_gets_it_inside_the_container(hosts, tmp_path, monkeypatch):
    # A stand-in docker that runs what `docker exec -i -u hermes box ...` would run, with a HERMES_HOME.
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text('#!/bin/sh\n[ "$1" = version ] && exit 0\nshift 5\n'
                      f'HERMES_HOME="{tmp_path}/data" exec "$@"\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{docker.parent}{os.pathsep}{os.environ['PATH']}")
    store.upsert_agent(make_agent(id="alpha", host="alpha-host", desired={"container": "box"}))
    assert secret_pipe.put("alpha", "k", VALUE.encode()) .endswith("data/beacon-secrets/inbox/k")
    assert (tmp_path / "data" / "beacon-secrets" / "inbox" / "k").read_text() == VALUE
    _leave(tmp_path / "data" / "beacon-secrets", "out")
    assert secret_pipe.names("alpha") == ["out"]
    assert secret_pipe.read("alpha", "out") == VALUE.encode()
