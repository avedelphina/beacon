import threading

from backend import ssh
from backend.ssh import _base_cmd
from tests.conftest import make_host


class _FakePipe:
    def __init__(self, lines=()):
        self.lines = iter(lines)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.lines)

    def write(self, _value):
        return None

    def close(self):
        return None


class _BlockingPipe(_FakePipe):
    def __next__(self):
        if not hasattr(self, "yielded"):
            self.yielded = True
            return "first line\n"
        threading.Event().wait(10)
        raise StopIteration


class _FakeProcess:
    def __init__(self, stdout=None):
        self.stdin = _FakePipe()
        self.stdout = stdout or _FakePipe()
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True

    def poll(self):
        return 0 if self.waited else None

    def wait(self, timeout=None):
        self.waited = True
        return 0


class _BlockingProcess(_FakeProcess):
    def __init__(self):
        super().__init__(_BlockingPipe())


def test_base_cmd_uses_key_and_requires_pinned_host_keys(monkeypatch):
    monkeypatch.setenv("BEACON_KNOWN_HOSTS", "/run/secrets/fleet_known_hosts")
    cmd = _base_cmd(make_host(key="~/.ssh/id_ed25519", port=2222))
    assert "-i" in cmd and "~/.ssh/id_ed25519" in cmd
    assert "-p" in cmd and "2222" in cmd
    joined = " ".join(cmd)
    assert "StrictHostKeyChecking=yes" in joined
    assert "UserKnownHostsFile=/run/secrets/fleet_known_hosts" in joined
    assert "accept-new" not in joined
    assert "-F" not in cmd


def test_base_cmd_uses_config_file_and_does_not_force_strict_host_key_checking():
    cmd = _base_cmd(make_host(key=None, config_file="/tbot-data/ssh_config"))
    assert "-F" in cmd and "/tbot-data/ssh_config" in cmd
    assert "-i" not in cmd
    assert "-p" not in cmd
    assert "StrictHostKeyChecking" not in " ".join(cmd)


def test_base_cmd_always_sets_batch_mode_and_connect_timeout():
    for host in (make_host(key="~/.ssh/id"), make_host(key=None, config_file="/x")):
        joined = " ".join(_base_cmd(host))
        assert "BatchMode=yes" in joined
        assert "ConnectTimeout=8" in joined


def test_stream_script_kills_silent_process_after_timeout(monkeypatch):
    proc = _BlockingProcess()
    monkeypatch.setattr(ssh.subprocess, "Popen", lambda *args, **kwargs: proc)

    output = list(ssh.stream_script(make_host(), "sleep 100", timeout=0.02))

    assert proc.killed
    assert proc.waited
    assert output[-1] == "__BEACON_EXIT__none"
    assert any("timeout" in line for line in output)


def test_stream_script_reaps_process_when_generator_is_closed(monkeypatch):
    proc = _BlockingProcess()
    monkeypatch.setattr(ssh.subprocess, "Popen", lambda *args, **kwargs: proc)

    stream = ssh.stream_script(make_host(), "sleep 100", timeout=10)
    assert next(stream) == "first line"
    stream.close()

    assert proc.killed
    assert proc.waited
