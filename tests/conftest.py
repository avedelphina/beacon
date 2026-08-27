"""Shared fixtures.

Two things every driver/store test needs and neither should ever touch for
real: the filesystem (fleet/ YAML) and the network (ssh). `fleet` redirects
store.py's file paths into a tmp dir; `fake_ssh` replaces the `ssh` module
backend/drivers/hermes.py calls through with something that records what it
was asked to run and hands back a canned result — this is the "fake SSH
target" the roadmap calls for, just simpler than a real sshd: what matters
for catching bugs like a too-short timeout or a wrong command string is the
command Beacon builds, not a real round trip.
"""

import pytest

from backend import store
from backend.schemas import Agent, Host, SSHConfig
from backend.ssh import SSHResult


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Points backend.store at an empty, throwaway fleet/ directory."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(store, "HOSTS_FILE", tmp_path / "hosts.yaml")
    monkeypatch.setattr(store, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(store, "DECOMMISSIONED_DIR", tmp_path / "decommissioned")
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

    def run(self, host, command: str, timeout: int = 10) -> SSHResult:
        self.calls.append(command)
        return self.result

    def stream_script(self, host, script: str, timeout: int = 900):
        self.calls.append(script)
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
