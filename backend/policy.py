"""Who may call whom: Tailscale-style rules over agent ids and tags.

`fleet/policy.yaml` holds the rules; agents carry `tags` in their own record.

    tags: [family-a, family-b]            # optional: declared, so a typo is an error
    rules:
      - {from: [tag:family-a], to: [tag:family-a]}
      - {from: [coordinator],  to: [tag:family-*]}
    deny:
      - {from: [tag:family-b], to: [tag:family-a]}

A selector is an agent id, `tag:<name>` (`*` wildcards allowed in the name), or
`*` for every agent. A call from X to Y is allowed when no `deny` rule matches
and some `rules` entry does. Rules are one-way: `from` calls `to`. Beacon
itself (operators, MCP clients) is always allowed and is not part of any rule.
With no policy file nothing is allowed except Beacon, so "everyone may talk
to everyone" has to be written down: `{from: ["*"], to: ["*"]}`.

Replies are not governed here: an answer to a call that was allowed is always
allowed (backend/mailbox.py delivers late ones as Beacon).
"""

import fnmatch
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import store
from .schemas import ID_RE, Agent

POLICY_FILE = store.FLEET_DIR / "policy.yaml"
# Every saved-over version is kept here (gitignored), newest 50.
HISTORY_DIR = store.FLEET_DIR / "policy-history"
HISTORY_KEEP = 50
_SELECTOR_RE = re.compile(r"^(\*|tag:[a-z0-9*][a-z0-9*-]*|[a-z0-9][a-z0-9-]*)$")


def check_selector(value: str) -> str:
    if not isinstance(value, str) or not _SELECTOR_RE.fullmatch(value):
        raise ValueError(
            f"selector {value!r} must be an agent id, tag:<name> (wildcards allowed), or *"
        )
    return value


class Rule(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_: list[str] = Field(alias="from", min_length=1)
    to: list[str] = Field(min_length=1)

    @field_validator("from_", "to")
    @classmethod
    def _selectors(cls, v: list[str]) -> list[str]:
        return [check_selector(s) for s in v]


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    deny: list[Rule] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _tag_names(cls, v: list[str]) -> list[str]:
        for tag in v:
            if not ID_RE.fullmatch(tag):
                raise ValueError(f"tag {tag!r} must match {ID_RE.pattern}")
        return v


_cache: dict = {"key": None, "policy": None}


def load() -> Policy | None:
    """The policy, or None when there is no file (Beacon-only). Cached by the
    file's mtime and size, since every peer list and call consults it."""
    path = Path(POLICY_FILE)
    if not path.exists():
        return None
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _cache["key"] != key:
        _cache["policy"] = Policy.model_validate(yaml.safe_load(path.read_text()) or {})
        _cache["key"] = key
    return _cache["policy"]


def _matches(selector: str, agent: Agent) -> bool:
    if selector == "*":
        return True
    if selector.startswith("tag:"):
        pattern = selector[4:]
        return any(fnmatch.fnmatchcase(tag, pattern) for tag in agent.tags)
    return selector == agent.id


def _any(selectors: list[str], agent: Agent) -> bool:
    return any(_matches(s, agent) for s in selectors)


def allows(policy: Policy | None, caller: Agent, target: Agent) -> bool:
    """The rule itself: deny first, then allow, otherwise no."""
    if policy is None:
        return False
    if any(_any(r.from_, caller) and _any(r.to, target) for r in policy.deny):
        return False
    return any(_any(r.from_, caller) and _any(r.to, target) for r in policy.rules)


def problems(policy: Policy | None, fleet: list[Agent]) -> list[str]:
    """What is probably a mistake, for `mesh policy` and the API: undeclared
    tags, selectors that match nothing, and a missing file."""
    if policy is None:
        return ["no fleet/policy.yaml: only Beacon may call agents; "
                'write {from: ["*"], to: ["*"]} to let every agent talk to every other'] if fleet else []
    found = []
    declared = set(policy.tags)
    ids = {a.id for a in fleet}
    used = {t for a in fleet for t in a.tags}
    if declared:
        found += [f"agent {a.id!r} has undeclared tag {t!r}" for a in fleet for t in a.tags if t not in declared]
    for kind, rules in (("rule", policy.rules), ("deny", policy.deny)):
        for i, rule in enumerate(rules, 1):
            for selector in [*rule.from_, *rule.to]:
                if selector == "*":
                    continue
                if selector.startswith("tag:"):
                    name = selector[4:]
                    if "*" not in name and declared and name not in declared:
                        found.append(f"{kind} {i}: tag {name!r} is not declared in `tags`")
                    elif not any(fnmatch.fnmatchcase(t, name) for t in used):
                        found.append(f"{kind} {i}: no agent has a tag matching {selector!r}")
                elif selector not in ids:
                    found.append(f"{kind} {i}: no agent with id {selector!r}")
    return found


def matrix(policy: Policy | None, fleet: list[Agent]) -> dict[str, list[str]]:
    """caller id -> the agent ids it may call, among agents with a reach block."""
    reachable = [a for a in fleet if a.reach is not None]
    return {c.id: [t.id for t in reachable if t.id != c.id and allows(policy, c, t)] for c in reachable}


# ---------------------------------------------------------------------------
# Editing: check a new text, show what it would change, save it with history.
# ---------------------------------------------------------------------------


def read_text() -> str:
    path = Path(POLICY_FILE)
    return path.read_text() if path.exists() else ""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse(text: str) -> Policy:
    """A policy from editor text. ValueError carries a message a person can act on."""
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("the policy must be a mapping with `rules`, `deny`, and optionally `tags`")
    try:
        return Policy.model_validate(data)
    except ValueError as exc:  # pydantic's ValidationError: one line per problem
        lines = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]  # type: ignore[attr-defined]
        raise ValueError("; ".join(lines)) from exc


def inbound(grid: dict[str, list[str]]) -> dict[str, set[str]]:
    """target id -> the agents allowed to call it, from a matrix()."""
    result: dict[str, set[str]] = {caller: set() for caller in grid}
    for caller, targets in grid.items():
        for target in targets:
            result.setdefault(target, set()).add(caller)
    return result


def preview(text: str, fleet: list[Agent]) -> dict:
    """What saving `text` would do, without saving: validation errors, likely
    mistakes, the new who-may-call-whom table, which calls it adds and removes,
    and which agents restart (Hermes reads who may call in only at startup, so
    an agent whose set of allowed callers changes restarts on its next sync)."""
    current = load()
    old = matrix(current, fleet)
    result: dict = {"errors": [], "problems": [], "matrix": None, "added": [], "removed": [], "restarts": []}
    try:
        new_policy = parse(text)
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result
    new = matrix(new_policy, fleet)
    result["matrix"] = new
    result["problems"] = problems(new_policy, fleet)
    pairs = lambda grid: {(c, t) for c, ts in grid.items() for t in ts}  # noqa: E731
    result["added"] = sorted(pairs(new) - pairs(old))
    result["removed"] = sorted(pairs(old) - pairs(new))
    before, after = inbound(old), inbound(new)
    result["restarts"] = sorted(a for a in after if after[a] != before.get(a, set()))
    return result


def save(text: str, author: str) -> None:
    """Write `text` as the policy, keeping the version it replaces in
    fleet/policy-history/. The text is written exactly as given, so comments
    and layout survive. Atomic: a reader never sees half a file. Raises
    ValueError if it doesn't parse."""
    parse(text)
    path = Path(POLICY_FILE)
    if path.exists():
        history = Path(HISTORY_DIR)
        history.mkdir(parents=True, exist_ok=True)
        who = re.sub(r"[^a-z0-9]+", "-", author.lower()).strip("-") or "unknown"
        (history / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{who}.yaml").write_text(path.read_text())
        for old in sorted(history.glob("*.yaml"))[:-HISTORY_KEEP]:
            old.unlink()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".policy-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
