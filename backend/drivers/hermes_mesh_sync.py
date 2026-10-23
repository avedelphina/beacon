"""Beacon A2A mesh sync for one Hermes profile.

Installed on the agent's host by `python -m backend.mesh install` and run
from cron. Pulls this agent's mesh config from Beacon (GET {beacon}/mesh/
{agent}, authenticated with the agent's own pull token) and applies it:
A2A_* settings into the profile's .env, the peer list into config.yaml's
`a2a_agents` via `hermes config set` — token *references* only, so
config.yaml never holds a secret.

Tailscale-style resilience: if Beacon is unreachable nothing changes, and
the mesh keeps running on the last applied config. Outbound changes go
live on the agent's next turn (Hermes re-reads .env and config.yaml).
Inbound ones — who may call in — are read once when the gateway starts, so
only those trigger a graceful `hermes gateway restart`, which waits for
running turns. Stdlib only: it runs on whatever python3 the host has.

Usage: python3 sync.py <state-dir>   (holds sync.json, token, logs)
"""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

# Read once, when Hermes's A2A adapter starts; a change needs a restart.
INBOUND_KEYS = ("A2A_PORT", "A2A_HOST", "A2A_AGENT_NAME", "A2A_PEER_TOKENS", "A2A_TRUSTED_PEERS")
MAX_LOG_BYTES = 1_000_000


def log(message: str) -> None:
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def token_env(peer: str) -> str:
    return "A2A_TOKEN_" + peer.upper().replace("-", "_")


def owned(key: str) -> bool:
    return key in INBOUND_KEYS or key.startswith("A2A_TOKEN_")


def env_settings(mesh: dict) -> dict[str, str]:
    settings = {
        "A2A_PORT": str(mesh["listen"]["port"]),
        "A2A_AGENT_NAME": mesh["agent"],
        "A2A_PEER_TOKENS": ",".join(f"{name}:{tok}" for name, tok in mesh["inbound"].items()),
        "A2A_TRUSTED_PEERS": ",".join(mesh["inbound"]),
    }
    if mesh["listen"].get("host"):
        settings["A2A_HOST"] = mesh["listen"]["host"]
    for peer in mesh["outbound"]:
        settings[token_env(peer["name"])] = peer["token"]
    return settings


def peer_list(mesh: dict) -> dict:
    return {
        peer["name"]: {
            "url": peer["url"],
            "auth": {"type": "bearer", "token": "${env:%s}" % token_env(peer["name"])},
            "capabilities": peer.get("capabilities", []),
        }
        for peer in mesh["outbound"]
    }


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    """(lines this script doesn't own, current values of the ones it does)."""
    kept, current = [], {}
    for line in path.read_text().splitlines() if path.exists() else []:
        key, sep, value = line.partition("=")
        if sep and owned(key):
            current[key] = value
        else:
            kept.append(line)
    return kept, current


def write_env(path: Path, kept: list[str], settings: dict[str, str]) -> None:
    if not path.exists():
        path.touch(mode=0o600)
    # Rewritten in place, so the file keeps its owner and mode.
    path.write_text("\n".join(kept + [f"{k}={v}" for k, v in settings.items()]) + "\n")


def restart(hermes: list[str], state: Path) -> None:
    env = dict(os.environ)
    runtime_dir = f"/run/user/{os.getuid()}"
    # `systemctl --user` (Linux) needs this; cron doesn't set it.
    if "XDG_RUNTIME_DIR" not in env and os.path.isdir(runtime_dir):
        env["XDG_RUNTIME_DIR"] = runtime_dir
    # Detached: the restart waits for running turns, which can take many minutes.
    with open(state / "restart.log", "a") as out:
        subprocess.Popen(hermes + ["gateway", "restart"], env=env, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    log("inbound peers changed; graceful gateway restart requested")


def sync(state: Path) -> int:
    cfg = json.loads((state / "sync.json").read_text())
    log_file = state / "sync.log"
    if log_file.exists() and log_file.stat().st_size > MAX_LOG_BYTES:
        log_file.write_text("")
    with open(state / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # a previous run is still going
        pull_token = (state / "token").read_text().strip()
        request = urllib.request.Request(f"{cfg['beacon']}/mesh/{cfg['agent']}",
                                         headers={"Authorization": f"Bearer {pull_token}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                mesh = json.load(response)
        except Exception as exc:
            log(f"Beacon unreachable, keeping the last applied config: {exc}")
            return 0

        env_path = Path(cfg["env_file"])
        kept, current = read_env(env_path)
        settings = env_settings(mesh)
        if settings != current:
            write_env(env_path, kept, settings)
            log(f"wrote {len(settings)} A2A settings to {env_path}")

        peers = peer_list(mesh)
        digest = hashlib.sha256(json.dumps(peers, sort_keys=True).encode()).hexdigest()
        stamp = state / "peers.sha256"
        if not stamp.exists() or stamp.read_text() != digest:
            subprocess.run(cfg["hermes"] + ["config", "set", "--force", "a2a_agents", json.dumps(peers)],
                           check=True, capture_output=True)
            stamp.write_text(digest)
            log(f"peer list: {', '.join(peers) or '(none)'}")

        if any(settings.get(k) != current.get(k) for k in INBOUND_KEYS):
            restart(cfg["hermes"], state)
    return 0


if __name__ == "__main__":
    sys.exit(sync(Path(sys.argv[1])))
