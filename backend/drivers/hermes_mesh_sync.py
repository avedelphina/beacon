"""Beacon A2A mesh sync for one Hermes profile.

Installed on the agent's host by `python -m backend.mesh install` and run
from cron. Pulls this agent's mesh config from Beacon (GET {beacon}/mesh/
{agent}, authenticated with the agent's own pull token) and applies it:
A2A_* settings into the profile's .env, the peer list into config.yaml's
`a2a_agents` via `hermes config set` — token *references* only, so
config.yaml never holds a secret.

Tailscale-style resilience: if Beacon is unreachable nothing changes, and
the mesh keeps running on the last applied config. Messages the Beacon
mailbox held for this agent come along with the config (pull delivery): the
sync hands each one to the agent on this host and reports the result, so
Beacon never needs to reach the agent — it works behind NAT, too. Outbound changes go
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
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Read once, when Hermes's A2A adapter starts; a change needs a restart.
INBOUND_KEYS = ("A2A_PORT", "A2A_HOST", "A2A_AGENT_NAME", "A2A_PEER_TOKENS", "A2A_TRUSTED_PEERS")
BOARD_TOKEN_KEY = "BEACON_BOARD_TOKEN"
BOARD_SERVER = "beacon-board"
MAX_LOG_BYTES = 1_000_000
# Announces that this sync delivers held messages itself (pull delivery).
SYNC_VERSION = "2"
# Hermes answers within its A2A_REPLY_TIMEOUT (300s); wait a little longer.
LOCAL_REPLY_TIMEOUT = 330


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


def parts_text(obj) -> str:
    parts = obj.get("parts") if isinstance(obj, dict) else None
    return "\n".join(p["text"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("text"), str))


def deliver_locally(url: str, token: str, text: str, context_id: str | None) -> dict:
    """One A2A SendMessage to the agent on this host. Returns the report for
    Beacon: ok + reply, or the error and whether retrying later is safe. Only
    "never got there" is transient; a refusal, or a timeout while the agent
    may already be working on it, is not — retrying could deliver it twice."""
    message = {"role": "ROLE_USER", "parts": [{"text": text}], "messageId": uuid.uuid4().hex}
    if context_id:
        message["contextId"] = context_id
    body = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "SendMessage",
                       "params": {"message": message}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "A2A-Version": "1.0"})
    try:
        with urllib.request.urlopen(request, timeout=LOCAL_REPLY_TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.load(exc)
        except Exception:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return {"ok": False, "transient": False, "error": str(payload["error"].get("message"))}
        return {"ok": False, "transient": exc.code in (502, 503, 504), "error": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:  # no connection: it never got the message
        return {"ok": False, "transient": True, "error": str(exc.reason)}
    except (socket.timeout, OSError) as exc:  # waiting for the answer: it may be working on it
        return {"ok": False, "transient": False, "error": f"no answer: {exc}"}
    if not isinstance(payload, dict) or isinstance(payload.get("error"), dict):
        error = payload.get("error", {}).get("message") if isinstance(payload, dict) else "unreadable answer"
        return {"ok": False, "transient": False, "error": str(error)}
    result = payload.get("result") or {}
    answer = result.get("task") or result.get("message") or result
    status = answer.get("status") or {}
    reply = (parts_text(status.get("message")) or "\n".join(parts_text(a) for a in answer.get("artifacts") or [])
             or parts_text(answer))
    return {"ok": True, "reply": reply or f"(no text; task state {status.get('state')})"}


def post_report(cfg: dict, pull_token: str, message_id: int, report: dict) -> None:
    request = urllib.request.Request(
        f"{cfg['beacon']}/mesh/{cfg['agent']}/inbox/{message_id}", data=json.dumps(report).encode(), method="POST",
        headers={"Authorization": f"Bearer {pull_token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=20).close()
    except Exception as exc:
        # Unreported, the claim lapses on Beacon and the message is offered
        # again later — possibly a duplicate for the agent.
        log(f"could not report message {message_id} to Beacon: {exc}")


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
                                         headers={"Authorization": f"Bearer {pull_token}",
                                                  "X-Beacon-Sync-Version": SYNC_VERSION})
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

        restarting = any(settings.get(k) != current.get(k) for k in INBOUND_KEYS) or (board_changed and (board or previous))
        if restarting:
            restart(cfg, state)

        for item in mesh.get("inbox") or []:
            if restarting:  # the gateway is draining; hand it back for the next run
                report = {"ok": False, "transient": True, "error": "agent restarting"}
            else:
                report = deliver_locally(mesh["endpoint"], item["token"], item["text"], item.get("context_id"))
            post_report(cfg, pull_token, item["id"], report)
            log(f"held message {item['id']}: " + ("delivered" if report["ok"] else
                ("handed back" if report["transient"] else "failed") + f" ({report['error']})"))
    return 0


if __name__ == "__main__":
    sys.exit(sync(Path(sys.argv[1])))
