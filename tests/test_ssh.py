import threading
import time

import pytest

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


# --- the real subprocess lifecycle ------------------------------------------
# The tests above use fakes. These run the real stream_script/run against a
# local `sh -s` standing in for ssh: _base_cmd is the only thing replaced, so
# the Popen, the reader thread, the deadline and the exit code are the real ones.

@pytest.fixture
def local_shell(monkeypatch):
    monkeypatch.setattr(ssh, "_base_cmd", lambda host: ["sh", "-c", "exec sh -s"])


def test_stream_script_yields_output_then_the_exit_code(local_shell):
    lines = list(ssh.stream_script(make_host(), "echo one\necho two >&2\nexit 3\n", timeout=10))
    assert lines == ["one", "two", "__BEACON_EXIT__3"]  # stderr is folded in


def test_stream_script_reports_success_as_exit_zero(local_shell):
    assert list(ssh.stream_script(make_host(), "echo ok\n", timeout=10))[-1] == "__BEACON_EXIT__0"


def test_stream_script_kills_a_silent_command_at_the_deadline(local_shell):
    started = time.monotonic()
    lines = list(ssh.stream_script(make_host(), "echo start\nsleep 30\n", timeout=0.5))
    assert time.monotonic() - started < 10  # not 30
    assert lines[0] == "start"
    assert any("timeout" in line for line in lines)
    assert lines[-1] == "__BEACON_EXIT__none"


def test_stream_script_deadline_holds_against_a_chatty_command(local_shell):
    """A command that keeps printing must not be able to outrun the deadline."""
    started = time.monotonic()
    lines = list(ssh.stream_script(make_host(), "while :; do echo x; sleep 0.01; done\n", timeout=0.5))
    assert time.monotonic() - started < 10
    assert lines[-1] == "__BEACON_EXIT__none"


def test_stream_script_says_so_when_there_is_no_ssh_binary(monkeypatch):
    monkeypatch.setattr(ssh, "_base_cmd", lambda host: ["beacon-no-such-binary"])
    assert list(ssh.stream_script(make_host(), "true", timeout=5)) == [
        "[beacon] ssh binary not found on this machine", "__BEACON_EXIT__none"]


def test_run_distinguishes_a_missing_binary_a_timeout_and_a_remote_failure(monkeypatch):
    monkeypatch.setattr(ssh, "_base_cmd", lambda host: ["beacon-no-such-binary"])
    missing = ssh.run(make_host(), "true")
    assert not missing.ok and missing.returncode is None and "not found" in missing.transport_error

    monkeypatch.setattr(ssh, "_base_cmd", lambda host: ["sh", "-c", 'eval "$0"'])
    assert ssh.run(make_host(), "sleep 5", timeout=0.3).transport_error == "ssh timed out"

    failed = ssh.run(make_host(), "echo no >&2; exit 4")
    assert not failed.ok and failed.returncode == 4 and failed.transport_error is None  # the remote command's failure

    assert ssh.run(make_host(), "exit 255").transport_error == "ssh connection failed"  # ssh's own
