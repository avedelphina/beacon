import json

import pytest

from backend.drivers import hermes
from backend.ssh import SSHResult
from tests.conftest import make_agent, make_host


def systemctl_stdout(active_state="active", sub_state="running", pid="1234", load_state="loaded", since="Wed 2026-08-26 23:10:01 CEST"):
    lines = [f"ActiveState={active_state}", f"SubState={sub_state}", f"MainPID={pid}", f"LoadState={load_state}"]
    if since:
        lines.append(f"ActiveEnterTimestamp={since}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pure string-building functions
# ---------------------------------------------------------------------------


def test_service_name_default():
    assert hermes.service_name(make_agent()) == "hermes-gateway"


def test_service_name_with_profile():
    assert hermes.service_name(make_agent(profile="holly")) == "hermes-gateway-holly"


def test_service_name_default_profile_string_treated_as_no_profile():
    assert hermes.service_name(make_agent(profile="default")) == "hermes-gateway"


def test_service_name_override():
    assert hermes.service_name(make_agent(desired={"service": "custom.service"})) == "custom.service"


def test_profile_home_default():
    assert hermes.profile_home(make_agent()) == "$HOME/.hermes"


def test_profile_home_with_profile():
    assert hermes.profile_home(make_agent(profile="holly")) == "$HOME/.hermes/profiles/holly"


def test_cmd_prefix_default():
    assert hermes._cmd_prefix(make_agent()) == "hermes"


def test_cmd_prefix_with_profile():
    assert hermes._cmd_prefix(make_agent(profile="holly")) == "hermes -p holly"


def test_target_user_none_by_default():
    assert hermes.target_user(make_agent()) is None


def test_target_user_from_desired():
    assert hermes.target_user(make_agent(desired={"os_user": "james"})) == "james"


# ---------------------------------------------------------------------------
# Validation / injection guards
# ---------------------------------------------------------------------------


def test_validate_agent_accepts_clean_profile():
    hermes._validate_agent(make_agent(profile="holly"))  # no raise


def test_validate_agent_rejects_injection_in_profile():
    with pytest.raises(ValueError):
        hermes._validate_agent(make_agent(profile="x; rm -rf ~ #"))


def test_validate_agent_rejects_injection_in_os_user():
    with pytest.raises(ValueError):
        hermes._validate_agent(make_agent(desired={"os_user": "evil; rm -rf ~ #"}))


def test_validate_deploy_rejects_unknown_install_mode():
    with pytest.raises(ValueError):
        hermes.validate_deploy(make_agent(desired={"install_mode": "yolo"}))


def test_validate_deploy_add_profile_requires_profile():
    with pytest.raises(ValueError):
        hermes.validate_deploy(make_agent(desired={"install_mode": "add-profile"}))
    hermes.validate_deploy(make_agent(profile="holly", desired={"install_mode": "add-profile"}))  # no raise


def test_validate_deploy_new_user_requires_os_user():
    with pytest.raises(ValueError):
        hermes.validate_deploy(make_agent(desired={"install_mode": "new-user"}))
    hermes.validate_deploy(make_agent(desired={"install_mode": "new-user", "os_user": "hermes-svc"}))  # no raise


def test_validate_deploy_simple_needs_nothing():
    hermes.validate_deploy(make_agent())  # no raise


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def test_status_active(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=systemctl_stdout(), stderr="", returncode=0)
    result = hermes.status(make_agent(), make_host())
    assert result == {
        "reachable": True, "state": "active", "active_state": "active",
        "sub_state": "running", "pid": 1234, "since": "Wed 2026-08-26 23:10:01 CEST",
    }
    assert "systemctl --user show" in fake_ssh.last_command
    assert "hermes-gateway" in fake_ssh.last_command


def test_status_failed(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=systemctl_stdout(active_state="failed", sub_state="failed", pid="0"), stderr="", returncode=0)
    assert hermes.status(make_agent(), make_host())["state"] == "failed"


def test_status_crashlooping(fake_ssh):
    # Real box: a unit whose profile dir was deleted retried every 5s.
    fake_ssh.result = SSHResult(ok=True, stdout=systemctl_stdout(active_state="activating", sub_state="auto-restart", pid="0"), stderr="", returncode=0)
    assert hermes.status(make_agent(), make_host())["state"] == "crashlooping"


def test_status_starting_vs_stopping(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=systemctl_stdout(active_state="activating", sub_state="start", pid="0"), stderr="", returncode=0)
    assert hermes.status(make_agent(), make_host())["state"] == "starting"

    fake_ssh.result = SSHResult(ok=True, stdout=systemctl_stdout(active_state="deactivating", sub_state="stop", pid="0"), stderr="", returncode=0)
    assert hermes.status(make_agent(), make_host())["state"] == "stopping"


def test_status_not_installed(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=systemctl_stdout(active_state="inactive", sub_state="dead", pid="0", load_state="not-found"), stderr="", returncode=0)
    result = hermes.status(make_agent(), make_host())
    assert result["state"] == "not-installed"


def test_status_unreachable_on_timeout(fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="ssh timed out", returncode=None)
    result = hermes.status(make_agent(), make_host())
    assert result == {"reachable": False, "state": "unreachable", "detail": "ssh timed out"}


def test_status_unreachable_on_ssh_transport_failure(fake_ssh):
    # exit 255 is ssh's own convention for a transport/auth failure, distinct
    # from the remote command's own exit code.
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="Permission denied", returncode=255)
    result = hermes.status(make_agent(), make_host())
    assert result["state"] == "unreachable"


def test_status_no_pid_when_zero(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=systemctl_stdout(active_state="inactive", sub_state="dead", pid="0"), stderr="", returncode=0)
    assert hermes.status(make_agent(), make_host())["pid"] is None


# ---------------------------------------------------------------------------
# logs()
# ---------------------------------------------------------------------------


def test_logs_uses_default_path_and_journalctl_fallback(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="log line 1\nlog line 2", stderr="", returncode=0)
    text = hermes.logs(make_agent(profile="holly"), make_host(), lines=50)
    assert text == "log line 1\nlog line 2"
    cmd = fake_ssh.last_command
    assert "$HOME/.hermes/profiles/holly/logs/gateway.log" in cmd
    assert "journalctl --user -u" in cmd
    assert "-n 50" in cmd


def test_logs_respects_log_path_override(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="", stderr="", returncode=0)
    hermes.logs(make_agent(desired={"log_path": "/custom/path.log"}), make_host())
    assert "/custom/path.log" in fake_ssh.last_command


def test_logs_unreachable(fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="", returncode=None)
    assert hermes.logs(make_agent(), make_host()).startswith("[unreachable]")


def test_logs_remote_error(fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="tail: no such file", returncode=1)
    assert hermes.logs(make_agent(), make_host()).startswith("[error]")


# ---------------------------------------------------------------------------
# reconcile()
# ---------------------------------------------------------------------------


def reconcile_stdout(hermes_present="yes", profile_dir="yes", load_state="loaded", active_state="active", sub_state="running", linger="yes"):
    return "\n".join([
        f"HERMES_PRESENT={hermes_present}",
        f"PROFILE_DIR={profile_dir}",
        f"LoadState={load_state}",
        f"ActiveState={active_state}",
        f"SubState={sub_state}",
        f"LINGER={linger}",
    ])


def test_reconcile_healthy(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings == [{"id": "healthy", "severity": "ok", "summary": "active and running", "fix": None}]


def test_reconcile_orphaned_unit(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(profile_dir="no", load_state="loaded"), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings[0]["id"] == "orphaned-unit"
    assert findings[0]["fix"] == "uninstall-orphan"


def test_reconcile_not_deployed(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(profile_dir="no", load_state="not-found"), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings[0]["id"] == "not-deployed"
    assert findings[0]["fix"] is None


def test_reconcile_service_missing(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(load_state="not-found"), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings[0]["id"] == "service-missing"
    assert findings[0]["fix"] == "install-and-start"


def test_reconcile_stuck_failed(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(active_state="failed", sub_state="failed"), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings[0]["id"] == "stuck-failed"
    assert findings[0]["fix"] == "restart-failed"


def test_reconcile_crashlooping_has_no_fix(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(active_state="activating", sub_state="auto-restart"), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings[0]["id"] == "crashlooping"
    assert findings[0]["fix"] is None


def test_reconcile_not_started(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(active_state="inactive", sub_state="dead"), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings[0]["id"] == "not-started"
    assert findings[0]["fix"] == "start"


def test_reconcile_linger_disabled_adds_second_finding(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(linger="no"), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings[0]["id"] == "healthy"
    assert findings[1]["id"] == "linger-disabled"
    assert findings[1]["fix"] == "enable-linger"


def test_reconcile_hermes_not_present(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=reconcile_stdout(hermes_present="no"), stderr="", returncode=0)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings == [{
        "id": "not-installed-on-host", "severity": "critical",
        "summary": "hermes isn't on PATH for deploy — nothing to reconcile, run Deploy first",
        "fix": None,
    }]


def test_reconcile_unreachable(fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="", returncode=None)
    findings = hermes.reconcile(make_agent(), make_host())
    assert findings[0]["id"] == "unreachable"


# ---------------------------------------------------------------------------
# apply_fix() / restart()
# ---------------------------------------------------------------------------


def test_apply_fix_unknown_raises_before_any_ssh(fake_ssh):
    with pytest.raises(ValueError):
        hermes.apply_fix(make_agent(), make_host(), "nonexistent-fix")
    assert fake_ssh.calls == []


@pytest.mark.parametrize("fix,expected_substring", [
    ("install-and-start", "gateway install"),
    ("restart-failed", "reset-failed"),
    ("start", "gateway start"),
    ("uninstall-orphan", "gateway uninstall"),
    ("enable-linger", "enable-linger"),
])
def test_apply_fix_commands(fake_ssh, fix, expected_substring):
    fake_ssh.result = SSHResult(ok=True, stdout="", stderr="", returncode=0)
    result = hermes.apply_fix(make_agent(), make_host(), fix)
    assert result["ok"] is True
    assert expected_substring in fake_ssh.last_command


def test_restart_command_and_output(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="restarted", stderr="", returncode=0)
    result = hermes.restart(make_agent(profile="holly"), make_host())
    assert result == {"ok": True, "output": "restarted"}
    assert "hermes -p holly gateway restart" in fake_ssh.last_command


def test_restart_unreachable(fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="ssh timed out", returncode=None)
    result = hermes.restart(make_agent(), make_host())
    assert result == {"ok": False, "output": "ssh timed out"}


# ---------------------------------------------------------------------------
# list_plugins() / update_plugin()
# ---------------------------------------------------------------------------


def test_list_plugins_parses_json(fake_ssh):
    payload = [{"name": "deltachat", "status": "enabled", "version": "1.6.3", "description": "", "source": "git"}]
    fake_ssh.result = SSHResult(ok=True, stdout=json.dumps(payload), stderr="", returncode=0)
    assert hermes.list_plugins(make_agent(), make_host()) == payload


def test_list_plugins_empty_non_json_output(fake_ssh):
    # `plugins list --json` prints a plain-text message instead of `[]`
    # when nothing's installed — found while testing, not documented.
    fake_ssh.result = SSHResult(ok=True, stdout="No plugins installed.", stderr="", returncode=0)
    assert hermes.list_plugins(make_agent(), make_host()) == []


def test_list_plugins_unparseable_output_raises(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="not json and not the empty message", stderr="", returncode=0)
    with pytest.raises(RuntimeError):
        hermes.list_plugins(make_agent(), make_host())


def test_list_plugins_ssh_failure_raises(fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="ssh timed out", returncode=None)
    with pytest.raises(RuntimeError):
        hermes.list_plugins(make_agent(), make_host())


def test_update_plugin_rejects_bad_name_before_any_ssh(fake_ssh):
    with pytest.raises(ValueError):
        hermes.update_plugin(make_agent(), make_host(), "x; rm -rf ~ #")
    assert fake_ssh.calls == []


def test_update_plugin_command(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="Plugin deltachat-platform updated.", stderr="", returncode=0)
    result = hermes.update_plugin(make_agent(), make_host(), "deltachat-platform")
    assert "plugins update deltachat-platform" in fake_ssh.last_command
    assert result["disabled_by_scan"] is False


def test_update_plugin_sets_disabled_by_scan_flag(fake_ssh):
    # Real box: an update tripped Hermes's own security scan on the
    # plugin's own test fixtures and it auto-disabled the plugin, silently
    # taking a live messaging channel offline. This flag is what lets the
    # GUI surface that instead of it being buried in scan-report text.
    fake_ssh.result = SSHResult(ok=True, stdout="Plugin 'deltachat-platform' has been disabled. Review the findings.", stderr="", returncode=0)
    result = hermes.update_plugin(make_agent(), make_host(), "deltachat-platform")
    assert result["disabled_by_scan"] is True


# ---------------------------------------------------------------------------
# config_diff() / push_config()
# ---------------------------------------------------------------------------


def _config_diff_stdout(config_yaml: str, env_keys: list[str]) -> str:
    # Mirrors the remote command's `sed "s/=$//"` — bare key names, no
    # trailing `=`, values never included.
    env_block = "\n".join(env_keys)
    return f"{config_yaml}__BEACON_ENV_KEYS__\n{env_block}"


def test_config_diff_match_drift_and_missing(fake_ssh):
    live_yaml = "agent:\n  max_turns: 90\n  guardrails: false\n"
    fake_ssh.result = SSHResult(ok=True, stdout=_config_diff_stdout(live_yaml, ["OPENROUTER_API_KEY"]), stderr="", returncode=0)

    agent = make_agent(desired={
        "config": {"agent": {"max_turns": 90, "guardrails": True}, "nonexistent": {"path": "x"}},
        "env_keys": ["OPENROUTER_API_KEY", "MISSING_KEY"],
    })
    result = hermes.config_diff(agent, make_host())

    by_path = {c["path"]: c for c in result["config"]}
    assert by_path["agent.max_turns"]["status"] == "match"
    assert by_path["agent.guardrails"]["status"] == "drift"
    assert by_path["nonexistent.path"]["status"] == "missing-live"

    by_key = {e["key"]: e for e in result["env"]}
    assert by_key["OPENROUTER_API_KEY"]["status"] == "present"
    assert by_key["MISSING_KEY"]["status"] == "missing"


def test_config_diff_empty_desired_config_produces_no_findings(fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout=_config_diff_stdout("agent:\n  max_turns: 90\n", []), stderr="", returncode=0)
    result = hermes.config_diff(make_agent(), make_host())
    assert result["config"] == []
    assert result["env"] == []


def test_config_diff_unreachable(fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="ssh timed out", returncode=None)
    result = hermes.config_diff(make_agent(desired={"config": {"a": 1}}), make_host())
    assert result["reachable"] is False


def test_push_config_rejects_empty_desired_config():
    with pytest.raises(ValueError):
        list(hermes.push_config(make_agent(), make_host()))


def test_push_config_script_content(fake_ssh):
    agent = make_agent(profile="holly", desired={"config": {"agent": {"max_turns": 42}, "toolsets": ["a", "b"], "skip_me": None}})
    list(hermes.push_config(agent, make_host()))
    script = fake_ssh.last_command
    assert "hermes -p holly config set agent.max_turns 42" in script
    assert 'hermes -p holly config set toolsets \'["a", "b"]\'' in script
    assert "skip_me" not in script  # None values are skipped, no clear "unset" semantics
    assert "gateway restart" in script


# ---------------------------------------------------------------------------
# deploy() / decommission() / update_agent() — script construction
# ---------------------------------------------------------------------------


def test_deploy_simple_mode_script(fake_ssh):
    list(hermes.deploy(make_agent(), make_host()))
    script = fake_ssh.last_command
    assert "curl -fsSL" in script
    assert "--skip-setup" in script
    assert "hermes gateway install" in script
    assert "hermes gateway start" in script


def test_deploy_add_profile_mode_creates_profile(fake_ssh):
    list(hermes.deploy(make_agent(profile="holly", desired={"install_mode": "add-profile"}), make_host()))
    script = fake_ssh.last_command
    assert "hermes profile create holly" in script
    assert "hermes -p holly gateway install" in script


def test_deploy_new_user_mode_wraps_in_sudo(fake_ssh):
    agent = make_agent(desired={"install_mode": "new-user", "os_user": "hermes-svc"})
    list(hermes.deploy(agent, make_host()))
    script = fake_ssh.last_command
    assert "useradd -m -s /bin/bash hermes-svc" in script
    assert "sudo -u hermes-svc -i bash -s" in script


def test_deploy_invalid_mode_raises_before_any_ssh(fake_ssh):
    with pytest.raises(ValueError):
        list(hermes.deploy(make_agent(desired={"install_mode": "yolo"}), make_host()))
    assert fake_ssh.calls == []


def test_decommission_baseline_script(fake_ssh):
    list(hermes.decommission(make_agent(), make_host()))
    script = fake_ssh.last_command
    assert "gateway uninstall" in script
    assert "rm -rf" not in script


def test_decommission_purge_requires_named_profile(fake_ssh):
    with pytest.raises(ValueError):
        list(hermes.decommission(make_agent(), make_host(), purge=True))
    assert fake_ssh.calls == []


def test_decommission_purge_named_profile_script(fake_ssh):
    list(hermes.decommission(make_agent(profile="holly"), make_host(), purge=True))
    script = fake_ssh.last_command
    assert 'rm -rf "$HOME/.hermes/profiles/holly"' in script


def test_decommission_remove_user_requires_os_user(fake_ssh):
    with pytest.raises(ValueError):
        list(hermes.decommission(make_agent(), make_host(), remove_user=True))
    assert fake_ssh.calls == []


def test_decommission_remove_user_script(fake_ssh):
    agent = make_agent(desired={"os_user": "hermes-svc"})
    list(hermes.decommission(agent, make_host(), remove_user=True))
    script = fake_ssh.last_command
    assert "userdel -r hermes-svc" in script


def test_update_agent_script(fake_ssh):
    list(hermes.update_agent(make_agent(), make_host()))
    assert "hermes update --yes" in fake_ssh.last_command
