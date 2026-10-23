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
BOARD_TOKEN_KEY = "BEACON_BOARD_TOKEN"
BOARD_SERVER = "beacon-board"
MAX_LOG_BYTES = 1_000_000


def log(message: str) -> None:
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def token_env(peer: str) -> str:
    return "A2A_TOKEN_" + peer.upper().replace("-", "_")


def owned(key: str) -> bool:
    return key in INBOUND_KEYS or key.startswith("A2A_TOKEN_") or key == BOARD_TOKEN_KEY


def env_settings(mesh: dict, pull_token: str = "") -> dict[str, str]:
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
    if mesh.get("board") and pull_token:
        settings[BOARD_TOKEN_KEY] = pull_token
    return settings


def board_server(mesh: dict) -> dict | None:
    """The `mcp_servers` entry for the fleet board, or None when this agent
    has no board access. The token stays in .env; config.yaml holds a ref."""
    board = mesh.get("board")
    if not board:
        return None
    return {"url": board["url"], "headers": {"Authorization": "Bearer ${env:%s}" % BOARD_TOKEN_KEY}}


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


def restart(cfg: dict, state: Path) -> None:
    # sync.json "restart" overrides the command. Docker agents need it: under
    # an s6-supervised image, `hermes gateway restart` stops the platforms but
    # the process never exits, leaving the agent half down. There the sync
    # runs on the host and restarts the container instead.
    command = cfg.get("restart") or cfg["hermes"] + ["gateway", "restart"]
    env = dict(os.environ)
    runtime_dir = f"/run/user/{os.getuid()}"
    # `systemctl --user` (Linux) needs this; cron doesn't set it.
    if "XDG_RUNTIME_DIR" not in env and os.path.isdir(runtime_dir):
        env["XDG_RUNTIME_DIR"] = runtime_dir
    # Detached: the restart waits for running turns, which can take many minutes.
    with open(state / "restart.log", "a") as out:
        subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    log(f"who may call in, or the board server, changed; restart requested: {' '.join(command)}")


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

        # Beacon announces where it lives; follow it, so moving Beacon needs
        # no visit to each host. The old Beacon points here until all moved.
        if mesh.get("beacon") and mesh["beacon"] != cfg["beacon"]:
            log(f"Beacon moved: {cfg['beacon']} -> {mesh['beacon']}")
            cfg["beacon"] = mesh["beacon"]
            (state / "sync.json").write_text(json.dumps(cfg, indent=2))

        env_path = Path(cfg["env_file"])
        kept, current = read_env(env_path)
        settings = env_settings(mesh, pull_token)
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

        # Hermes connects MCP servers when the gateway starts: a change to the
        # board server needs a restart, like an inbound change does.
        board = board_server(mesh)
        board_stamp = state / "board.json"
        previous = json.loads(board_stamp.read_text()) if board_stamp.exists() else None
        board_changed = board != previous
        if board_changed and (board or previous):
            entry = board or {**previous, "enabled": False}
            subprocess.run(cfg["hermes"] + ["config", "set", "--force", f"mcp_servers.{BOARD_SERVER}",
                                            json.dumps(entry)], check=True, capture_output=True)
            board_stamp.write_text(json.dumps(board))
            log(f"board MCP server {'set' if board else 'disabled'}")

        if any(settings.get(k) != current.get(k) for k in INBOUND_KEYS) or (board_changed and (board or previous)):
            restart(cfg, state)
    return 0


if __name__ == "__main__":
    sys.exit(sync(Path(sys.argv[1])))
