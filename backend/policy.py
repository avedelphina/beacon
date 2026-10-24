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
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import store
from .schemas import ID_RE, Agent

POLICY_FILE = store.FLEET_DIR / "policy.yaml"
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
