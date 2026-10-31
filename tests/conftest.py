"""Shared fixtures.

Two things every driver/store test needs and neither should ever touch for
real: the fleet store (state/beacon.db) and the network (ssh). `fleet` points
the store at a throwaway directory; `fake_ssh` replaces the `ssh` module
backend/drivers/hermes.py calls through with something that records what it
was asked to run and hands back a canned result — this is the "fake SSH
target" the roadmap calls for, just simpler than a real sshd: what matters
for catching bugs like a too-short timeout or a wrong command string is the
command Beacon builds, not a real round trip.
"""

import base64
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import store
from backend.app import app
from backend.schemas import Agent, Host, SSHConfig
from backend.ssh import SSHResult

# The operational store must never be the real one, for any test. A test that
# leaves a thread running (board assignment notices deliver in one) can write
# after its fixtures are undone, and "undone" used to mean the real state/
# directory. Pointing the default somewhere disposable, once, for the whole
# session closes that.
from backend import opstore as _opstore  # noqa: E402

_opstore.STATE_DIR = Path(tempfile.mkdtemp(prefix="beacon-test-state-"))
# And the YAML fleet that a first start imports must never be the real one.
store.FLEET_DIR = Path(tempfile.mkdtemp(prefix="beacon-test-fleet-"))


@pytest.fixture(autouse=True)
def _board_notices_run_inline(monkeypatch):
    """Assignment notices are delivered from a background thread in production.
    In tests that thread can outlive the test and run after its patches and
    environment are gone, where an error becomes a stray warning or, before
    STATE_DIR was fixed, a write to the real store. Running it inline makes a
    test's result depend only on what the test did."""
    import types

    from backend import board, notify

    inline = types.SimpleNamespace(
        Thread=lambda target, args=(), daemon=False: types.SimpleNamespace(start=lambda: target(*args)),
        Lock=__import__("threading").Lock)
    monkeypatch.setattr(board, "threading", inline)
    monkeypatch.setattr(notify, "threading", inline)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """An empty fleet in a throwaway store; returns the directory, which also
    serves as the (empty) YAML fleet a first start would import.
    """
    from backend import opstore, policy

    monkeypatch.setattr(opstore, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(store, "FLEET_DIR", tmp_path / "fleet")
    # Tests start from "everyone may call everyone"; the default with no
    # policy is Beacon-only, and the policy tests delete it to check that.
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n', "test")
    return tmp_path


def make_host(id="h1", user="deploy", key="~/.ssh/id_ed25519", config_file=None, port=22, **kw) -> Host:
    return Host(id=id, address="10.0.0.1", ssh=SSHConfig(user=user, key=key, config_file=config_file, port=port), **kw)


def make_agent(id="a1", host="h1", type="hermes", profile=None, desired=None, **kw) -> Agent:
    return Agent(id=id, type=type, host=host, profile=profile, desired=desired or {}, **kw)


class FakeSSH:
    """Drop-in replacement for backend.ssh: same two entry points
    (run, stream_script), records every command it was handed instead of
    executing anything.
    """

    def __init__(self):
        self.calls: list[str] = []
        self.result: SSHResult = SSHResult(ok=True, stdout="", stderr="", returncode=0)
        self.stream_lines: list[str] = []
        self.stream_error: Exception | None = None

    def run(self, host, command: str, timeout: int = 10) -> SSHResult:
        self.calls.append(command)
        return self.result

    def stream_script(self, host, script: str, timeout: int = 900):
        self.calls.append(script)
        if self.stream_error is not None:
            raise self.stream_error
        yield from self.stream_lines

    @property
    def last_command(self) -> str:
        return self.calls[-1]


@pytest.fixture
def fake_ssh(monkeypatch):
    from backend.drivers import hermes as hermes_driver

    fake = FakeSSH()
    monkeypatch.setattr(hermes_driver, "ssh", fake)
    return fake


@pytest.fixture
def client(fleet, fake_ssh):
    return TestClient(app)


@pytest.fixture
def human_client(fleet, fake_ssh, monkeypatch):
    """A separate TestClient carrying a real signed browser session."""
    from backend import auth

    # Login is re-checked on every request, so this person is an operator.
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"tom@example.com"}))

    payload = base64.b64encode(json.dumps({"user": {"email": "tom@example.com"}}).encode()).decode()
    cookie = TimestampSigner(auth.SESSION_SECRET).sign(payload.encode()).decode()
    human = TestClient(app)
    human.cookies.set("session", cookie)
    return human


@pytest.fixture
def mcp_client(fleet, fake_ssh, monkeypatch):
    # Match the production bearer-token path without requiring a live OIDC
    # provider. This must remain distinct from an authenticated browser user.
    from backend import auth

    # This restriction must also hold in open/local mode: a configured MCP
    # token is still an LLM-facing service credential, never human approval.
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")
    return TestClient(app, headers={"Authorization": "Bearer test-mcp-token"})
