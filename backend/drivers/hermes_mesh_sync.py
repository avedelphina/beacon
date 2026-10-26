"""Beacon A2A mesh sync for one Hermes profile.

Installed on the agent's host by `python -m backend.mesh install` and run
from cron. Pulls this agent's mesh config from Beacon (GET {beacon}/mesh/
{agent}, authenticated with the agent's own pull token) and applies it:
A2A_* settings into the profile's .env, the peer list into config.yaml's
`a2a_agents` via `hermes config set` — token *references* only, so
config.yaml never holds a secret.

Beacon only touches what it created. A record of its own names, kept in
managed.json beside this script, says which peers, callers and token
variables are Beacon's; anything else in the agent's A2A configuration (a peer
or caller the agent's owner added, a token for an outside service) is left
exactly as it is. With no managed.json nothing is Beacon's yet, so a fresh
install adopts nothing and removes nothing. `sync.py <state> --adopt` records
what Beacon would write right now as managed, without changing anything: use it
once on an install made before managed.json existed.

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
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Read once, when Hermes's A2A adapter starts; a change needs a restart.
# Read when the gateway starts, so a change to any of them restarts it.
INBOUND_KEYS = ("A2A_PORT", "A2A_HOST", "A2A_AGENT_NAME", "A2A_PEER_TOKENS", "A2A_TRUSTED_PEERS",
                "A2A_REPLY_TIMEOUT")
BOARD_TOKEN_KEY = "BEACON_BOARD_TOKEN"
BOARD_SERVER = "beacon-board"
MAX_LOG_BYTES = 1_000_000
# Announces that this sync delivers held messages itself (pull delivery).
SYNC_VERSION = "2"
# How long to wait for the agent's answer before leaving it to the agent's
# push to Beacon (backend/mailbox.py), so one long task never holds the sync.
LOCAL_REPLY_TIMEOUT = 60
# A restart still marked as running after this long is assumed dead
# (longer than Hermes 0.21's 30-minute graceful drain cap).
RESTART_STALE_SECONDS = 40 * 60


def log(message: str) -> None:
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def token_env(peer: str) -> str:
    return "A2A_TOKEN_" + peer.upper().replace("-", "_")


def entries(value: str) -> list:
    return [e for e in value.split(",") if e]


def entry_name(entry: str) -> str:
    return entry.split(":", 1)[0]


def new_token_envs(mesh: dict) -> list:
    return [token_env(peer["name"]) for peer in mesh["outbound"]]


def owned_keys(mesh: dict, managed: dict) -> set:
    """The .env keys Beacon writes or removes: the fixed A2A settings, the
    token variables it created before or creates now, and the board token."""
    return set(INBOUND_KEYS) | set(managed["token_envs"]) | set(new_token_envs(mesh)) | {BOARD_TOKEN_KEY}


def env_settings(mesh: dict, pull_token: str, managed: dict, current: dict) -> dict:
    """The value of every key Beacon owns. The two caller lists are merged:
    entries Beacon never wrote (not in `managed`, not in this response) are
    kept as they are, then Beacon's current ones are added."""
    inbound = mesh["inbound"]
    mine = set(managed["inbound"]) | set(inbound)
    settings = {
        "A2A_PORT": str(mesh["listen"]["port"]),
        # How long the agent may work on one task before Hermes drops the answer.
        **({"A2A_REPLY_TIMEOUT": str(mesh["listen"]["reply_timeout"])} if mesh["listen"].get("reply_timeout") else {}),
        "A2A_AGENT_NAME": mesh["agent"],
        "A2A_PEER_TOKENS": ",".join(
            [e for e in entries(current.get("A2A_PEER_TOKENS", "")) if entry_name(e) not in mine]
            + [f"{name}:{tok}" for name, tok in inbound.items()]),
        "A2A_TRUSTED_PEERS": ",".join(
            [e for e in entries(current.get("A2A_TRUSTED_PEERS", "")) if e not in mine] + list(inbound)),
    }
    if mesh["listen"].get("host"):
        settings["A2A_HOST"] = mesh["listen"]["host"]
    for peer in mesh["outbound"]:
        settings[token_env(peer["name"])] = peer["token"]
    if mesh.get("board") and pull_token:
        settings[BOARD_TOKEN_KEY] = pull_token
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


def digest(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


def load_managed(state: Path) -> dict:
    path = state / "managed.json"
    base = {"peers": {}, "inbound": [], "token_envs": []}
    return {**base, **json.loads(path.read_text())} if path.exists() else base


def save_managed(state: Path, managed: dict) -> None:
    (state / "managed.json").write_text(json.dumps(managed, indent=2, sort_keys=True))


def run_hermes(cfg: dict, args: list, tolerate: str = "") -> None:
    """Run `hermes <args>`. `tolerate` is text in the output that means the
    thing is already as wanted (removing a key that is already gone)."""
    result = subprocess.run(cfg["hermes"] + args, capture_output=True, text=True)
    if result.returncode != 0 and not (tolerate and tolerate in (result.stdout + result.stderr)):
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)


def apply_peers(cfg: dict, state: Path, mesh: dict, managed: dict) -> None:
    """Make Beacon's peers in `a2a_agents` match the response, one entry at a
    time, so entries Beacon never wrote stay. The record is saved after each
    step: a run that stops halfway picks up where it left off."""
    peers = peer_list(mesh)
    for name, entry in peers.items():
        if managed["peers"].get(name) != digest(entry):
            run_hermes(cfg, ["config", "set", "--force", f"a2a_agents.{name}", json.dumps(entry)])
            managed["peers"][name] = digest(entry)
            save_managed(state, managed)
    for name in [n for n in managed["peers"] if n not in peers]:
        run_hermes(cfg, ["config", "unset", f"a2a_agents.{name}"], tolerate="not set")
        del managed["peers"][name]
        save_managed(state, managed)


def board_server(mesh: dict) -> dict | None:
    """The `mcp_servers` entry for the fleet board, or None when this agent
    has no board access. The token stays in .env; config.yaml holds a ref."""
    board = mesh.get("board")
    if not board:
        return None
    return {"url": board["url"], "headers": {"Authorization": "Bearer ${env:%s}" % BOARD_TOKEN_KEY}}


ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
CONFIG_OPEN_RE = re.compile(r"^\s*allow_all_users:\s*[\"']?(true|yes|1)[\"']?\s*(#.*)?$", re.I | re.M)


def open_chats(cfg: dict) -> list:
    """The settings that let anyone at all message this agent, by name: any
    `*_ALLOW_ALL_USERS` flag (Hermes's per-platform and gateway-wide
    switches, e.g. GATEWAY_ALLOW_ALL_USERS) in the profile's .env or in a
    Docker agent's container environment, where a compose file often sets
    it, and `allow_all_users` in config.yaml. Names only, never values.
    Beacon withholds the fleet board from such an agent (backend/mesh.py).
    Not seen: variables a systemd unit or launchd plist sets itself."""
    env_file = Path(cfg["env_file"])
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    hermes = cfg["hermes"]
    if len(hermes) > 5 and hermes[1] == "exec" and hermes[5] == "hermes":  # docker exec -u <user> <container>
        try:
            lines += subprocess.run(hermes[:5] + ["env"], capture_output=True, text=True,
                                    timeout=30).stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"could not read the container's environment: {exc}")
    found = set()
    for line in lines:
        match = ENV_LINE_RE.match(line)
        if (match and match.group(1).endswith("_ALLOW_ALL_USERS")
                and match.group(2).strip().strip("\"'").lower() in ("true", "1", "yes")):
            found.add(match.group(1))
    config = env_file.parent / "config.yaml"
    if config.exists() and CONFIG_OPEN_RE.search(config.read_text()):
        found.add("allow_all_users")
    return sorted(found)


def parts_text(obj) -> str:
    parts = obj.get("parts") if isinstance(obj, dict) else None
    return "\n".join(p["text"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("text"), str))


def deliver_locally(url: str, token: str, text: str, context_id: str | None, push_url: str | None = None) -> dict:
    """One A2A SendMessage to the agent on this host. Returns the report for
    Beacon: ok + reply, or the error and whether retrying later is safe. Only
    "never got there" is transient; a refusal is definite failure. A timeout
    means the agent has it: with `push_url` it is still working and will send
    Beacon the answer itself ("working"), without one the outcome is unknown
    ("uncertain"). Neither is ever sent again."""
    message = {"role": "ROLE_USER", "parts": [{"text": text}], "messageId": uuid.uuid4().hex}
    if context_id:
        message["contextId"] = context_id
    params = {"message": message}
    if push_url:
        params["configuration"] = {"taskPushNotificationConfig": {"url": push_url}}
    body = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "SendMessage",
                       "params": params}).encode()
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
    except (socket.timeout, OSError) as exc:  # waiting for the answer: it is working on it
        if push_url:
            return {"ok": False, "transient": False, "working": True,
                    "error": f"no answer within {LOCAL_REPLY_TIMEOUT} s; the agent sends it to Beacon when done"}
        return {"ok": False, "transient": False, "uncertain": True,
                "error": f"delivery uncertain: no answer: {exc}"}
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


SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_SKILL_BYTES = 64 * 1024


def install_skills(skills: list, skills_dir: Path) -> list:
    """Write Beacon's skills into the agent's skills folder; return the names
    changed. Skipped: a name that isn't a plain id (it becomes a path), a
    file over the size cap, and a checksum that doesn't match the content.
    Nothing is ever deleted, and a skill whose text already matches is not
    rewritten. A changed local copy is replaced: these are Beacon's skills,
    so keep local edits under another name."""
    changed = []
    for skill in skills:
        name, content = skill.get("name", ""), skill.get("content", "")
        if (not SKILL_NAME_RE.fullmatch(name) or not isinstance(content, str)
                or len(content.encode()) > MAX_SKILL_BYTES
                or hashlib.sha256(content.encode()).hexdigest() != skill.get("sha256")):
            log(f"skill {name!r} skipped: not a valid skill")
            continue
        target = skills_dir / name / "SKILL.md"
        if target.exists() and target.read_text() == content:
            continue
        existed = skills_dir.is_dir()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        # When run as root on a host, match the folder's owner: a Docker
        # agent's files belong to the container user, not root.
        if os.geteuid() == 0 and existed:
            owner = skills_dir.stat()
            for path in (target.parent, target):
                os.chown(path, owner.st_uid, owner.st_gid)
        changed.append(name)
    return changed


def read_env(path: Path, owned: set) -> tuple[list[str], dict[str, str]]:
    """(lines this script doesn't own, current values of the ones it does)."""
    kept, current = [], {}
    for line in path.read_text().splitlines() if path.exists() else []:
        key, sep, value = line.partition("=")
        if sep and key in owned:
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
    """Ask for a restart, and keep asking until one succeeds.

    Hermes 0.21 waits for running turns before restarting. 0.22 refuses
    while sessions are live (turns, cron jobs) and only offers --yes, which
    interrupts them. So a restart is recorded as pending and retried on
    every run until it goes through — on 0.22 that is the first idle moment,
    and no one's session is cut off.
    """
    # Settle an earlier attempt first, or its success would clear this request.
    settle_restart(state)
    (state / "restart.pending").touch()
    run_pending_restart(cfg, state)


def settle_restart(state: Path) -> None:
    """Read the exit code an earlier detached restart left behind."""
    result = state / "restart.result"
    if not result.exists():
        return
    code = result.read_text().strip()
    result.unlink()
    if code == "0":
        (state / "restart.pending").unlink(missing_ok=True)
        log("restart done")
    else:
        log(f"restart refused or failed (exit {code}); retrying on the next run")


def run_pending_restart(cfg: dict, state: Path) -> None:
    pending, result, started = state / "restart.pending", state / "restart.result", state / "restart.started"
    settle_restart(state)
    if not pending.exists():
        return
    if started.exists() and time.time() - started.stat().st_mtime < RESTART_STALE_SECONDS:
        return  # one is still running: a graceful drain can take a while
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
    started.touch()
    # Detached, since a graceful drain can take many minutes; the wrapper
    # leaves the exit code for the next run to read.
    # Clear "started" before publishing the result, so a reader that sees the
    # result never also sees the attempt as still running.
    script = (f"{shlex.join(command)}; code=$?; rm -f {shlex.quote(str(started))}; "
              f"echo $code > {shlex.quote(str(result))}")
    with open(state / "restart.log", "a") as out:
        subprocess.Popen(["sh", "-c", script], env=env, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    log(f"restart requested: {' '.join(command)}")


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
                                                  "X-Beacon-Sync-Version": SYNC_VERSION,
                                                  # "none", not empty: a proxy may drop an empty header.
                                                  "X-Beacon-Open-Chat": ",".join(open_chats(cfg)) or "none"})
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

        managed = load_managed(state)
        env_path = Path(cfg["env_file"])
        kept, current = read_env(env_path, owned_keys(mesh, managed))
        settings = env_settings(mesh, pull_token, managed, current)
        if settings != current:
            write_env(env_path, kept, settings)
            log(f"wrote {len(settings)} A2A settings to {env_path}")

        before = dict(managed["peers"])
        apply_peers(cfg, state, mesh, managed)
        if managed["peers"] != before:
            log(f"peer list: {', '.join(managed['peers']) or '(none)'}")
        managed["inbound"], managed["token_envs"] = list(mesh["inbound"]), new_token_envs(mesh)
        save_managed(state, managed)

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

        # Beacon's skills go beside the profile's .env, which sits in Hermes's home.
        for name in install_skills(mesh.get("skills") or [], Path(cfg["env_file"]).parent / "skills"):
            log(f"skill installed or updated: {name}")

        restarting = any(settings.get(k) != current.get(k) for k in INBOUND_KEYS) or (board_changed and (board or previous))
        if restarting:
            restart(cfg, state)
        else:
            run_pending_restart(cfg, state)  # an earlier one may have been refused

        for item in mesh.get("inbox") or []:
            if restarting:  # the gateway is draining; hand it back for the next run
                report = {"ok": False, "transient": True, "error": "agent restarting"}
            else:
                report = deliver_locally(mesh["endpoint"], item["token"], item["text"], item.get("context_id"),
                                         item.get("push_url"))
            post_report(cfg, pull_token, item["id"], report)
            log(f"held message {item['id']}: " + ("delivered" if report["ok"] else
                ("handed back" if report["transient"] else "working, answer by push" if report.get("working")
                 else "uncertain" if report.get("uncertain") else "failed") + f" ({report['error']})"))
    return 0


def adopt(state: Path) -> int:
    """Record what Beacon would write right now as Beacon's, changing nothing.
    For an install made before managed.json existed: its current A2A config is
    Beacon's, so the next sync may update and remove it."""
    cfg = json.loads((state / "sync.json").read_text())
    pull_token = (state / "token").read_text().strip()
    request = urllib.request.Request(f"{cfg['beacon']}/mesh/{cfg['agent']}",
                                     headers={"Authorization": f"Bearer {pull_token}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        mesh = json.load(response)
    # Digests stay empty on purpose: adopt cannot see what the agent has, so
    # the next sync writes each peer once more. Harmless, and a peer the agent
    # was still missing is not skipped.
    managed = {"peers": {n: "" for n in peer_list(mesh)},
               "inbound": list(mesh["inbound"]), "token_envs": new_token_envs(mesh)}
    save_managed(state, managed)
    log(f"adopted as Beacon's: {len(managed['peers'])} peers, {len(managed['inbound'])} callers")
    return 0


if __name__ == "__main__":
    state_dir = Path(sys.argv[1])
    sys.exit(adopt(state_dir) if "--adopt" in sys.argv[2:] else sync(state_dir))
