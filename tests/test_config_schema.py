"""Schema guardrail tests — validate_config_key / validate_config against the
checked-in snapshot (backend/drivers/hermes_config_schema.yaml), and its
enforcement in push_config / config_diff.

The snapshot is generated from hermes-agent's DEFAULT_CONFIG plus the
hand-maintained OVERLAYS in scripts/dump_hermes_config_schema.py; these
tests pin the RULES, not every key, so a regenerated snapshot with new
Hermes keys shouldn't break them.
"""

import json
from pathlib import Path

import pytest
import yaml

from backend.drivers import hermes
from backend.ssh import SSHResult
from tests.conftest import make_agent, make_host

REPO_ROOT = Path(__file__).resolve().parent.parent


# validate_config_key() rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "model.default",                # overlaid section, known leaf
    "model.provider",
    "model.aliases.myrole",         # free-form mapping: user-chosen role names
    "model.context_length",
    "fallback_providers",           # list-shaped leaf — pushed as one value
    "delegation.orchestrator_enabled",
    "delegation.fallback_providers",
    "auxiliary.compression.model",
    "agent.max_turns",
    "toolsets",                     # list leaf
    "mcp_servers.my-server.url",    # open root: validated first segment only
    "plugins.my-plugin.enabled",    # schema-defined dict root: first segment only
    "gateway.platforms.discord.foo",  # platform container at any depth
    "platforms.discord.require_mention",
    "_internal.thing",              # leading underscore = intentionally non-schema
])
def test_known_and_freeform_paths_are_ok(path):
    assert hermes.validate_config_key(path) == ("ok", None)


def test_unknown_subkey_of_populated_section_is_error():
    # The headline case: `hermes config set` WRITES these with a notice and
    # exit 0 — Beacon must refuse, or a fail-closed push reports success
    # while the agent is silently misconfigured.
    for path in ("model.primary", "model.fallbacks", "delegation.max_childs", "tools.model"):
        level, detail = hermes.validate_config_key(path)
        assert level == "error", path
        assert detail


def test_unknown_subkey_error_suggests_close_sibling():
    level, detail = hermes.validate_config_key("delegation.max_concurrent_child")
    assert level == "error"
    assert "max_concurrent_children" in detail  # did-you-mean from the snapshot


def test_unknown_top_level_is_warn_not_error():
    level, detail = hermes.validate_config_key("summarization.model")
    assert level == "warn"
    assert "summarization" in detail


@pytest.mark.parametrize("path", ["", "agent.", ".agent", "agent..max_turns", "agent. "])
def test_empty_path_segments_are_errors(path):
    level, _ = hermes.validate_config_key(path)
    assert level == "error"


# validate_config()
# ---------------------------------------------------------------------------


def test_validate_config_returns_only_non_ok_findings():
    findings = hermes.validate_config({
        "model": {"default": "x", "primary": "y"},   # one ok, one error
        "custom_thing": True,                         # warn
        "delegation": {"max_spawn_depth": 1},         # ok
    })
    by_path = {f["path"]: f for f in findings}
    assert set(by_path) == {"model.primary", "custom_thing"}
    assert by_path["model.primary"]["level"] == "error"
    assert by_path["custom_thing"]["level"] == "warn"


# Shipped template examples must validate clean against the snapshot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("example", sorted((REPO_ROOT / "fleet" / "templates").glob("*.yaml.example")), ids=lambda p: p.name)
def test_shipped_template_examples_validate_clean(example):
    data = yaml.safe_load(example.read_text())
    findings = hermes.validate_config(data.get("config") or {})
    assert findings == [], f"{example.name}: {findings}"


# push_config() enforcement
# ---------------------------------------------------------------------------


def test_push_config_refuses_unknown_subkey_without_touching_ssh(fake_ssh):
    agent = make_agent(desired={"config": {"model": {"primary": "anthropic/claude-sonnet-4"}}})
    with pytest.raises(ValueError, match="unknown config paths"):
        list(hermes.push_config(agent, make_host()))
    assert fake_ssh.calls == []  # refused before anything reached the host


def test_push_config_refusal_lists_every_bad_path(fake_ssh):
    agent = make_agent(desired={"config": {"model": {"primary": "x"}, "delegation": {"max_childs": 3}}})
    with pytest.raises(ValueError) as exc_info:
        list(hermes.push_config(agent, make_host()))
    assert "model.primary" in str(exc_info.value)
    assert "delegation.max_childs" in str(exc_info.value)


def test_push_config_warns_but_proceeds_on_unknown_top_level(fake_ssh):
    agent = make_agent(desired={"config": {"agent": {"max_turns": 42}, "my_custom_top": True}})
    list(hermes.push_config(agent, make_host()))
    script = fake_ssh.last_command
    assert "[beacon] warning:" in script and "my_custom_top" in script
    assert "config set agent.max_turns 42" in script
    assert "config set my_custom_top true" in script


def test_push_config_does_not_validate_none_valued_paths(fake_ssh):
    # None = "no opinion, don't touch" — never pushed, so never validated.
    agent = make_agent(desired={"config": {"agent": {"max_turns": 42}, "bogus_section": {"skip_me": None}}})
    list(hermes.push_config(agent, make_host()))
    script = fake_ssh.last_command
    assert "skip_me" not in script
    assert "bogus_section" not in script


def test_push_config_sends_structured_list_as_one_json_value(fake_ssh):
    fallbacks = [
        {"provider": "openrouter", "model": "a/b", "base_url": "https://openrouter.ai/api/v1",
         "key_env": "OPENROUTER_API_KEY", "api_mode": "chat_completions"},
        {"provider": "custom", "model": "qwen2.5-coder:14b", "base_url": "http://localhost:11434/v1",
         "api_mode": "chat_completions"},
    ]
    agent = make_agent(desired={"config": {"fallback_providers": fallbacks}})
    list(hermes.push_config(agent, make_host()))
    script = fake_ssh.last_command
    # One `config set` for the whole chain — not one per element — and the
    # value round-trips as JSON (valid YAML flow syntax for the CLI's
    # structured-value detection).
    assert script.count("config set fallback_providers") == 1
    line = next(l for l in script.splitlines() if "config set fallback_providers" in l)
    assert json.loads(line.split("fallback_providers ", 1)[1].strip("'")) == fallbacks


# config_diff() annotation
# ---------------------------------------------------------------------------


def test_config_diff_annotates_schema_status(fake_ssh):
    stdout = "agent:\n  max_turns: 90\n__BEACON_ENV_KEYS__\n"
    fake_ssh.result = SSHResult(ok=True, stdout=stdout, stderr="", returncode=0)
    agent = make_agent(desired={"config": {
        "agent": {"max_turns": 90},
        "model": {"primary": "x"},
        "my_custom_top": True,
    }})
    result = hermes.config_diff(agent, make_host())
    by_path = {c["path"]: c for c in result["config"]}
    assert by_path["agent.max_turns"]["schema"] == "ok"
    assert by_path["model.primary"]["schema"] == "error"
    assert "schema_detail" in by_path["model.primary"]
    assert by_path["my_custom_top"]["schema"] == "warn"
