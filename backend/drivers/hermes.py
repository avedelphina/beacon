import json
import re
import shlex
from collections.abc import Iterator

import yaml

from .. import ssh
from ..schemas import Agent, Host

INSTALL_URL = "https://hermes-agent.nousresearch.com/install.sh"

PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
OS_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")

# Hermes puts the `hermes` binary and per-profile command aliases in
# ~/.local/bin (installer writes this to .bashrc/.zshrc, but that file is
# never sourced by the non-interactive `bash -s` shell we pipe scripts into).
PATH_PREFIX = 'export PATH="$HOME/.local/bin:$HOME/.hermes/node/bin:$PATH"'

# A real ssh login gets XDG_RUNTIME_DIR from pam_systemd automatically, but
# `sudo -u <user>` does not register a logind session — `systemctl --user`
# then fails with "Failed to connect to bus" even though the unit and its
# manager are both there. Setting it explicitly is a correct no-op under a
# real login and the actual fix under sudo -u, so it's always included.
RUNTIME_ENV = 'export XDG_RUNTIME_DIR="/run/user/$(id -u)"'


def service_name(agent: Agent) -> str:
    configured = agent.desired.get("service")
    if configured:
        return configured
    return f"hermes-gateway-{agent.profile}" if agent.profile and agent.profile != "default" else "hermes-gateway"


def target_user(agent: Agent) -> str | None:
    return agent.desired.get("os_user") or None


def profile_home(agent: Agent) -> str:
    # $HOME, not ~ — tilde only expands unquoted, and reconcile() uses this
    # inside a quoted `[ -d "..." ]` test where a literal ~ silently never matches.
    return f"$HOME/.hermes/profiles/{agent.profile}" if agent.profile and agent.profile != "default" else "$HOME/.hermes"


def _cmd_prefix(agent: Agent) -> str:
    profile = agent.profile if agent.profile and agent.profile != "default" else None
    return f"hermes -p {shlex.quote(profile)}" if profile else "hermes"


def _validate_agent(agent: Agent) -> None:
    """Profile and os_user get interpolated into remote shell scripts, so
    every entry point (not just deploy) must reject anything outside a safe
    identifier charset before it reaches ssh.run/stream_script.
    """
    if agent.profile and agent.profile != "default" and not PROFILE_RE.match(agent.profile):
        raise ValueError(f"profile {agent.profile!r} must match {PROFILE_RE.pattern}")
    os_user = agent.desired.get("os_user")
    if os_user and not OS_USER_RE.match(os_user):
        raise ValueError(f"desired.os_user {os_user!r} must match {OS_USER_RE.pattern}")


def _wrap_for_user(agent: Agent, host: Host, command: str) -> str:
    """Run `command` as agent.desired.os_user when it differs from the SSH
    login user (the "new user" deploy mode installs Hermes under its own
    account). `-i` gives a login shell so $HOME/PATH resolve for that user.
    """
    _validate_agent(agent)
    user = target_user(agent)
    if user and user != host.ssh.user:
        return f"sudo -u {shlex.quote(user)} -i bash -c {shlex.quote(command)}"
    return command


def status(agent: Agent, host: Host) -> dict:
    unit = shlex.quote(service_name(agent))
    inner = f"{RUNTIME_ENV}; {PATH_PREFIX}; systemctl --user show {unit} --no-page -p LoadState,ActiveState,SubState,MainPID,ActiveEnterTimestamp"
    result = ssh.run(host, _wrap_for_user(agent, host, inner))

    # ssh itself (not the remote command) exits 255 on transport/auth failure; None means it never returned at all.
    if result.returncode is None or result.returncode == 255:
        return {"reachable": False, "state": "unreachable", "detail": result.stderr.strip() or "ssh connection failed"}

    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    if fields.get("LoadState") == "not-found":
        return {"reachable": True, "state": "not-installed", "detail": f"no unit {service_name(agent)!r} on host"}

    active_state = fields.get("ActiveState", "unknown")
    sub_state = fields.get("SubState")
    if active_state in ("active", "reloading"):
        state = "active"
    elif active_state == "failed":
        state = "failed"
    elif active_state == "activating" and sub_state == "auto-restart":
        # Found on a real box: a unit whose profile dir had been deleted kept
        # retrying every 5s (restart counter in the thousands). Plain
        # "activating" would read as a normal first start, not a crash loop.
        state = "crashlooping"
    elif active_state in ("activating", "deactivating"):
        state = "starting" if active_state == "activating" else "stopping"
    else:
        state = "inactive"

    return {
        "reachable": True,
        "state": state,
        "active_state": active_state,
        "sub_state": fields.get("SubState"),
        "pid": int(fields["MainPID"]) if fields.get("MainPID", "0") != "0" else None,
        "since": fields.get("ActiveEnterTimestamp") or None,
    }


def logs(agent: Agent, host: Host, lines: int = 200) -> str:
    log_path = agent.desired.get("log_path") or f"{profile_home(agent)}/logs/gateway.log"
    unit = shlex.quote(service_name(agent))
    # Fall back to the journal when the app-level log file doesn't exist yet —
    # a unit that has never started successfully (bad profile, missing venv)
    # writes nothing to gateway.log but still has a systemd exit trail.
    inner = (
        f"{RUNTIME_ENV}; "
        f"if [ -f {log_path} ]; then tail -n {int(lines)} {log_path}; "
        f"else journalctl --user -u {unit} -n {int(lines)} --no-pager --output=short-iso; fi"
    )
    result = ssh.run(host, _wrap_for_user(agent, host, inner))

    if result.returncode is None or result.returncode == 255:
        return f"[unreachable] {result.stderr.strip() or 'ssh connection failed'}"
    return result.stdout if result.ok else f"[error] {result.stderr.strip()}"


def validate_deploy(agent: Agent) -> None:
    mode = agent.desired.get("install_mode", "simple")
    if mode not in ("simple", "add-profile", "new-user"):
        raise ValueError(f"desired.install_mode must be one of simple/add-profile/new-user, got {mode!r}")
    if mode == "add-profile" and not (agent.profile and agent.profile != "default"):
        raise ValueError("install_mode 'add-profile' requires a non-default agent.profile")
    if mode == "new-user" and not agent.desired.get("os_user"):
        raise ValueError("install_mode 'new-user' requires desired.os_user")
    _validate_agent(agent)


def _bringup_script(agent: Agent) -> str:
    """Install Hermes if missing, create the profile if named, then install
    and start its gateway service. Safe to re-run: each step checks before
    acting, so this covers both a bare host (nothing installed yet) and an
    existing install that just needs another profile.
    """
    profile = agent.profile if agent.profile and agent.profile != "default" else None
    profile_step = ""
    if profile:
        p = shlex.quote(profile)
        profile_step = f"""
if [ -d "$HOME/.hermes/profiles/{profile}" ]; then
  echo "[beacon] profile {profile} already exists"
else
  echo "[beacon] creating profile {profile}"
  hermes profile create {p}
fi
"""
    cmd_prefix = _cmd_prefix(agent)

    return f"""\
set -e
{RUNTIME_ENV}
{PATH_PREFIX}

if command -v hermes >/dev/null 2>&1; then
  echo "[beacon] hermes already installed ($(hermes --version 2>&1 | head -1))"
else
  echo "[beacon] installing hermes"
  curl -fsSL {INSTALL_URL} | bash -s -- --skip-setup
  {PATH_PREFIX}
fi
{profile_step}
loginctl enable-linger "$(whoami)" 2>/dev/null || sudo -n loginctl enable-linger "$(whoami)" 2>/dev/null || echo "[beacon] warning: could not enable linger — service may stop when this SSH session ends"

echo "[beacon] installing gateway service"
{cmd_prefix} gateway install || echo "[beacon] gateway install returned non-zero (may already be installed)"

echo "[beacon] starting gateway"
{cmd_prefix} gateway start

echo "[beacon] done"
"""


def _new_user_script(agent: Agent) -> str:
    user = shlex.quote(agent.desired["os_user"])
    inner = _bringup_script(agent)
    return f"""\
set -e
if id -u {user} >/dev/null 2>&1; then
  echo "[beacon] user {agent.desired['os_user']} already exists"
else
  echo "[beacon] creating user {agent.desired['os_user']}"
  sudo -n useradd -m -s /bin/bash {user}
fi
sudo -n loginctl enable-linger {user} || echo "[beacon] warning: could not enable linger for {agent.desired['os_user']}"

sudo -u {user} -i bash -s <<'BEACON_INNER'
{inner}
BEACON_INNER
"""


def deploy(agent: Agent, host: Host) -> Iterator[str]:
    validate_deploy(agent)
    mode = agent.desired.get("install_mode", "simple")
    script = _new_user_script(agent) if mode == "new-user" else _bringup_script(agent)
    yield from ssh.stream_script(host, script, timeout=900)


def _decommission_script(agent: Agent, purge: bool) -> str:
    cmd_prefix = _cmd_prefix(agent)
    home = profile_home(agent)
    purge_step = ""
    if purge:
        purge_step = f"""
echo "[beacon] purging profile data at {home}"
rm -rf "{home}"
"""
    return f"""\
{RUNTIME_ENV}
{PATH_PREFIX}

echo "[beacon] uninstalling gateway service"
{cmd_prefix} gateway uninstall 2>&1 || echo "[beacon] gateway uninstall returned non-zero (may not have been installed)"
{purge_step}
echo "[beacon] done"
"""


def decommission(agent: Agent, host: Host, purge: bool = False, remove_user: bool = False) -> Iterator[str]:
    """Stop and uninstall the gateway service. `purge` also deletes the
    profile's data (memory, sessions, skills) — refused for the default
    profile, since that directory is shared with every other profile on the
    same install, not this agent's alone. `remove_user` additionally deletes
    the OS account (only meaningful for a new-user-mode agent).
    """
    _validate_agent(agent)
    if remove_user and not target_user(agent):
        raise ValueError("remove_user requires desired.os_user")
    if purge and not (agent.profile and agent.profile != "default"):
        raise ValueError("purge is only allowed for a named (non-default) profile, to avoid wiping a shared ~/.hermes")

    inner = _decommission_script(agent, purge)

    if remove_user:
        user = shlex.quote(agent.desired["os_user"])
        script = f"""\
sudo -u {user} -i bash -s <<'BEACON_INNER'
{inner}
BEACON_INNER

echo "[beacon] removing user {agent.desired['os_user']}"
sudo -n loginctl terminate-user {user} 2>/dev/null || true
sleep 1
sudo -n userdel -r {user} 2>&1 || echo "[beacon] userdel failed — user may still have running processes, check manually"
"""
    else:
        script = _wrap_for_user(agent, host, inner)

    yield from ssh.stream_script(host, script, timeout=120)


_MISSING = object()

# desired.config only declares the keys Beacon manages — a real config.yaml
# also carries fields the CLI/agent itself writes (_config_version,
# onboarding.seen, command_allowlist, ...). Diffing the whole live file
# against desired would report those as permanent "drift" for no reason, so
# comparison is scoped to exactly the paths present in desired.config.


def _flatten(prefix: str, value) -> Iterator[tuple[str, object]]:
    if isinstance(value, dict) and value:
        for k, v in value.items():
            yield from _flatten(f"{prefix}.{k}" if prefix else k, v)
    else:
        yield prefix, value


def _get_path(tree: dict, path: str):
    node = tree
    for seg in path.split("."):
        if not isinstance(node, dict) or seg not in node:
            return _MISSING
        node = node[seg]
    return node


def _read_live_config(agent: Agent, host: Host) -> tuple[dict, list[str]]:
    home = profile_home(agent)
    script = (
        f'cat "{home}/config.yaml" 2>/dev/null\n'
        f'echo "__BEACON_ENV_KEYS__"\n'
        # key NAMES only — values are secrets and must never leave the host.
        f'grep -oE "^[A-Za-z_][A-Za-z0-9_]*=" "{home}/.env" 2>/dev/null | sed "s/=$//"\n'
    )
    result = ssh.run(host, _wrap_for_user(agent, host, script), timeout=15)
    if result.returncode is None or result.returncode == 255:
        raise RuntimeError(result.stderr.strip() or "ssh connection failed")
    config_part, _, env_part = result.stdout.partition("__BEACON_ENV_KEYS__\n")
    live_config = yaml.safe_load(config_part) or {}
    if not isinstance(live_config, dict):
        live_config = {}
    env_keys = [line.strip() for line in env_part.splitlines() if line.strip()]
    return live_config, env_keys


def config_diff(agent: Agent, host: Host) -> dict:
    """Compare desired.config (a declared subset of config.yaml) and
    desired.env_keys (expected .env key NAMES, never values) against what's
    actually on the host. Read-only.
    """
    _validate_agent(agent)
    desired_config = agent.desired.get("config") or {}
    desired_env_keys = agent.desired.get("env_keys") or []

    try:
        live_config, live_env_keys = _read_live_config(agent, host)
    except RuntimeError as e:
        return {"reachable": False, "detail": str(e), "config": [], "env": []}

    config_findings = []
    for path, desired_value in (_flatten("", desired_config) if desired_config else []):
        live_value = _get_path(live_config, path)
        if live_value is _MISSING:
            status = "missing-live"
        elif live_value == desired_value:
            status = "match"
        else:
            status = "drift"
        config_findings.append({
            "path": path, "status": status,
            "desired": desired_value, "live": None if live_value is _MISSING else live_value,
        })

    env_findings = [
        {"key": k, "status": "present" if k in live_env_keys else "missing"}
        for k in desired_env_keys
    ]

    return {"reachable": True, "config": config_findings, "env": env_findings}


def _cli_value(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value)  # JSON is valid YAML flow syntax — `config set` structured-value-detects it
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def push_config(agent: Agent, host: Host) -> Iterator[str]:
    """Push every key declared in desired.config via `hermes config set`,
    then restart the gateway if it's currently active so the change actually
    takes effect. None values are skipped — there's no clear unset semantics
    here, so "no opinion" just means don't touch that key.
    """
    _validate_agent(agent)
    desired_config = agent.desired.get("config") or {}
    if not desired_config:
        raise ValueError("desired.config is empty — nothing to push")

    cmd_prefix = _cmd_prefix(agent)
    unit = shlex.quote(service_name(agent))
    lines = [RUNTIME_ENV, PATH_PREFIX, ""]
    for path, value in _flatten("", desired_config):
        if value is None:
            continue
        lines.append(f'echo "[beacon] set {path}"')
        lines.append(f"{cmd_prefix} config set {shlex.quote(path)} {shlex.quote(_cli_value(value))}")
    lines.append(
        f'if [ "$(systemctl --user is-active {unit} 2>/dev/null)" = "active" ]; then '
        f'echo "[beacon] restarting gateway to apply config"; {cmd_prefix} gateway restart; '
        f'else echo "[beacon] gateway not active — config saved but nothing running to restart"; fi'
    )
    lines.append('echo "[beacon] done"')

    script = _wrap_for_user(agent, host, "\n".join(lines))
    yield from ssh.stream_script(host, script, timeout=120)


# Fix ids reconcile() can propose and apply() can run. Each maps to a short,
# fast, targeted command — never the full installer (that's deploy()'s job).
FIXES = {
    "install-and-start": "install the gateway service and start it",
    "restart-failed": "clear the failed state and start the service",
    "start": "start the (installed but stopped) service",
    "uninstall-orphan": "remove the systemd unit for a profile that no longer exists",
    "enable-linger": "enable linger so the service survives SSH logout",
}


def reconcile(agent: Agent, host: Host) -> list[dict]:
    """Read-only diagnosis of the drift/breakage patterns found in the wild:
    an orphaned unit (profile dir deleted, unit left behind), a unit stuck in
    `failed` after a one-off crash, a unit installed but never started, and
    linger left off (service dies when the SSH session that deployed it ends).
    Each finding names a `fix` id for apply_fix() — nothing here mutates state.
    """
    _validate_agent(agent)
    unit = shlex.quote(service_name(agent))
    home = profile_home(agent)
    target = target_user(agent) or host.ssh.user
    script = (
        f"{RUNTIME_ENV}; {PATH_PREFIX}\n"
        f'echo "HERMES_PRESENT=$(command -v hermes >/dev/null 2>&1 && echo yes || echo no)"\n'
        f'echo "PROFILE_DIR=$([ -d "{home}" ] && echo yes || echo no)"\n'
        f"systemctl --user show {unit} --no-page -p LoadState,ActiveState,SubState,MainPID\n"
        f'echo "LINGER=$(loginctl show-user "$(whoami)" -p Linger --value 2>/dev/null || echo unknown)"\n'
    )
    result = ssh.run(host, _wrap_for_user(agent, host, script), timeout=20)

    if result.returncode is None or result.returncode == 255:
        return [{"id": "unreachable", "severity": "critical",
                  "summary": result.stderr.strip() or "ssh connection failed", "fix": None}]

    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    findings = []

    if fields.get("HERMES_PRESENT") == "no":
        return [{"id": "not-installed-on-host", "severity": "critical",
                  "summary": f"hermes isn't on PATH for {target} — nothing to reconcile, run Deploy first",
                  "fix": None}]

    profile_exists = fields.get("PROFILE_DIR") == "yes"
    unit_loaded = fields.get("LoadState") == "loaded"
    active_state = fields.get("ActiveState", "inactive")
    sub_state = fields.get("SubState", "dead")

    if not profile_exists:
        if unit_loaded:
            findings.append({"id": "orphaned-unit", "severity": "critical",
                              "summary": f"{service_name(agent)!r} is installed but its profile dir ({home}) is gone",
                              "fix": "uninstall-orphan"})
        else:
            findings.append({"id": "not-deployed", "severity": "info",
                              "summary": "not deployed on this host yet — use Deploy, not Reconcile", "fix": None})
    elif not unit_loaded:
        findings.append({"id": "service-missing", "severity": "warn",
                          "summary": "profile exists but the gateway service isn't installed", "fix": "install-and-start"})
    elif active_state in ("active", "reloading"):
        findings.append({"id": "healthy", "severity": "ok", "summary": "active and running", "fix": None})
    elif active_state == "failed":
        findings.append({"id": "stuck-failed", "severity": "warn",
                          "summary": "failed and systemd gave up retrying — safe to reset and restart", "fix": "restart-failed"})
    elif active_state == "activating" and sub_state == "auto-restart":
        findings.append({"id": "crashlooping", "severity": "critical",
                          "summary": "crash-looping — check logs before restarting, this isn't a simple stopped/failed state",
                          "fix": None})
    elif active_state == "inactive":
        findings.append({"id": "not-started", "severity": "warn",
                          "summary": "installed but not running", "fix": "start"})
    else:
        findings.append({"id": "unexpected-state", "severity": "warn",
                          "summary": f"unexpected state active={active_state} sub={sub_state}", "fix": None})

    if fields.get("LINGER") != "yes":
        findings.append({"id": "linger-disabled", "severity": "warn",
                          "summary": f"linger not enabled for {target} — service stops when the SSH session ends",
                          "fix": "enable-linger"})

    return findings


def _fix_command(agent: Agent, fix: str) -> str:
    cmd_prefix = _cmd_prefix(agent)
    unit = shlex.quote(service_name(agent))
    if fix == "install-and-start":
        return f"{cmd_prefix} gateway install && {cmd_prefix} gateway start"
    if fix == "restart-failed":
        return f"systemctl --user reset-failed {unit}; {cmd_prefix} gateway start"
    if fix == "start":
        return f"{cmd_prefix} gateway start"
    if fix == "uninstall-orphan":
        return f"{cmd_prefix} gateway uninstall"
    if fix == "enable-linger":
        return 'loginctl enable-linger "$(whoami)" 2>/dev/null || sudo -n loginctl enable-linger "$(whoami)"'
    raise ValueError(f"unknown fix {fix!r}, must be one of {sorted(FIXES)}")


def apply_fix(agent: Agent, host: Host, fix: str) -> dict:
    _validate_agent(agent)
    inner = f"{RUNTIME_ENV}; {PATH_PREFIX}; {_fix_command(agent, fix)}"
    result = ssh.run(host, _wrap_for_user(agent, host, inner), timeout=60)

    if result.returncode is None or result.returncode == 255:
        return {"ok": False, "output": result.stderr.strip() or "ssh connection failed"}
    return {"ok": result.ok, "output": (result.stdout + result.stderr).strip()}
