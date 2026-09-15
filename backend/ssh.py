import os
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

from .schemas import Host


def _base_cmd(host: Host) -> list[str]:
    common = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if host.ssh.config_file:
        return ["ssh", "-F", host.ssh.config_file, *common, "--", f"{host.ssh.user}@{host.address}"]
    known_hosts = os.environ.get("BEACON_KNOWN_HOSTS", os.path.expanduser("~/.ssh/known_hosts"))
    return [
        "ssh",
        "-i", host.ssh.key,
        "-p", str(host.ssh.port),
        *common,
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "--",
        f"{host.ssh.user}@{host.address}",
    ]


@dataclass
class SSHResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None


def run(host: Host, command: str, timeout: int = 10) -> SSHResult:
    cmd = _base_cmd(host) + [command]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return SSHResult(ok=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)
    except subprocess.TimeoutExpired:
        return SSHResult(ok=False, stdout="", stderr="ssh timed out", returncode=None)
    except FileNotFoundError:
        return SSHResult(ok=False, stdout="", stderr="ssh binary not found on this machine", returncode=None)


def stream_script(host: Host, script: str, timeout: int = 900) -> Iterator[str]:
    """Pipe a script over SSH, yielding output while enforcing a silent timeout.

    A reader thread prevents a blocking stdout iterator from hiding the deadline.
    The process is always terminated and reaped when the stream ends early,
    times out, or completes normally.
    """
    cmd = _base_cmd(host) + ["bash -s"]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError:
        yield "[beacon] ssh binary not found on this machine"
        yield "__BEACON_EXIT__none"
        return

    assert proc.stdin and proc.stdout
    proc.stdin.write(script)
    proc.stdin.close()

    events: queue.Queue[tuple[str, str | int | None]] = queue.Queue()

    def read_output() -> None:
        try:
            for line in proc.stdout:
                events.put(("line", line.rstrip("\n")))
        finally:
            events.put(("eof", None))

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    terminated = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                terminated = True
                yield f"[beacon] deploy exceeded {timeout}s timeout, killed"
                yield "__BEACON_EXIT__none"
                return
            try:
                kind, value = events.get(timeout=remaining)
            except queue.Empty:
                proc.kill()
                terminated = True
                yield f"[beacon] deploy exceeded {timeout}s timeout, killed"
                yield "__BEACON_EXIT__none"
                return
            if kind == "line":
                yield value  # type: ignore[misc]
            else:
                yield f"__BEACON_EXIT__{proc.wait(timeout=5)}"
                return
    finally:
        if not terminated and proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=1)
