import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass

from .schemas import Host


def _base_cmd(host: Host) -> list[str]:
    common = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if host.ssh.config_file:
        # The config file's own Host block owns auth (e.g. a Teleport
        # ProxyCommand) and host-key verification — don't force our own
        # StrictHostKeyChecking on top of a trust model we don't control.
        return ["ssh", "-F", host.ssh.config_file, *common, f"{host.ssh.user}@{host.address}"]
    return [
        "ssh",
        "-i", host.ssh.key,
        "-p", str(host.ssh.port),
        *common,
        "-o", "StrictHostKeyChecking=accept-new",
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
    """Pipe `script` to `bash -s` on the remote host over stdin, yielding output
    line by line as it arrives. Piping via stdin (rather than embedding the
    script in argv) sidesteps shell-quoting entirely for arbitrarily complex
    scripts. Final yielded line is always `__BEACON_EXIT__<returncode|none>`.
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

    deadline = time.monotonic() + timeout
    for line in proc.stdout:
        yield line.rstrip("\n")
        if time.monotonic() > deadline:
            proc.kill()
            yield f"[beacon] deploy exceeded {timeout}s timeout, killed"
            yield "__BEACON_EXIT__none"
            return

    returncode = proc.wait(timeout=5)
    yield f"__BEACON_EXIT__{returncode}"
