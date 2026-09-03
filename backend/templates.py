"""Config templates — a fleet/templates/*.yaml fragment applied to many
agents at once, so "every EU relay runs this model stack" is declared in one
place instead of copied into every agent file.

A template carries only `config` and `env_keys` (the same shape as those two
keys inside an agent's `desired`). Host-shaping fields — install_mode,
os_user, service, log_path — stay per-agent on purpose: a shared
install_mode is a good way to break several installs at once.

The merge is read-only. `Agent.templates` lists the fragments; `resolve()`
deep-merges them in order and then the agent's own `desired` on top (the
agent always wins). The expanded result is never written back to the agent
file — round-tripping that through GET/PUT would bake the template in
permanently.
"""

import re
from pathlib import Path

import yaml

from .schemas import Agent
from .store import NotFound

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "fleet" / "templates"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ALLOWED_KEYS = {"config", "env_keys"}


def _path(name: str) -> Path:
    if not NAME_RE.match(name):
        raise ValueError(f"template name {name!r} must match {NAME_RE.pattern}")
    return TEMPLATES_DIR / f"{name}.yaml"


def list_templates() -> list[str]:
    if not TEMPLATES_DIR.exists():
        return []
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml"))


def load_template(name: str) -> dict:
    path = _path(name)
    if not path.exists():
        raise NotFound(f"template {name!r}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"template {name!r} must be a mapping, got {type(data).__name__}")
    extra = set(data) - ALLOWED_KEYS
    if extra:
        raise ValueError(
            f"template {name!r} has unsupported keys {sorted(extra)} — only {sorted(ALLOWED_KEYS)} are allowed"
        )
    return data


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
