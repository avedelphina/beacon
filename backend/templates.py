"""Config templates — a fragment applied to many
agents at once, so "every EU relay runs this model stack" is declared in one
place instead of copied into every agent file.

A template carries only `config` and `env_keys` (the same shape as those two
keys inside an agent's `desired`). Host-shaping fields — install_mode,
os_user, service, log_path — stay per-agent on purpose: a shared
install_mode is a good way to break several installs at once.

The merge is read-only. `Agent.templates` lists the fragments; `resolve()`
deep-merges them in order and then the agent's own `desired` on top (the
agent always wins). The expanded result is never written back to the agent
record — round-tripping that through GET/PUT would bake the template in
permanently.
"""

import json

from . import store
from .schemas import ID_RE as NAME_RE, Agent
from .store import NotFound

ALLOWED_KEYS = {"config", "env_keys"}


def check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"template name {name!r} must match {NAME_RE.pattern}")


def validate(name: str, data: object) -> dict:
    """The template's content, or ValueError saying what is wrong with it."""
    check_name(name)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"template {name!r} must be a mapping, got {type(data).__name__}")
    extra = set(data) - ALLOWED_KEYS
    if extra:
        raise ValueError(
            f"template {name!r} has unsupported keys {sorted(extra)} — only {sorted(ALLOWED_KEYS)} are allowed"
        )
    return data


def list_templates() -> list[str]:
    return sorted(store.records("template"))


def load_template(name: str) -> dict:
    check_name(name)
    body = store.find_record("template", name)
    if body is None:
        raise NotFound(f"template {name!r}")
    return validate(name, json.loads(body))


def save_template(name: str, content: object) -> dict:
    data = validate(name, content)
    store.save_record("template", name, json.dumps(data))
    return data


def delete_template(name: str) -> None:
    check_name(name)
    try:
        store.delete_record("template", name)
    except NotFound:
        raise NotFound(f"template {name!r}") from None


def _deep_merge(base: dict, over: dict) -> dict:
    """dict + dict recurses; every other case (scalar, list, or a type
    mismatch between the two sides) is a wholesale replace. Lists are
    replaced, never merged element-wise — there's no unambiguous way to do
    that, so `fallbacks: [a, b]` in an agent fully replaces the template's.
    """
    out = dict(base)
    for key, value in over.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve(agent: Agent) -> dict:
    """The effective `desired` for an agent: each named template merged in
    listed order, then the agent's own `desired` on top.
    """
    merged: dict = {}
    for name in agent.templates:
        merged = _deep_merge(merged, load_template(name))
    return _deep_merge(merged, agent.desired)
