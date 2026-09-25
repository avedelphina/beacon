import re

from pydantic import BaseModel, Field, field_validator, model_validator

# `user`/`address` end up as the literal `user@address` argv token ssh.py
# hands to the `ssh` binary — no shell is involved, but ssh itself still
# parses a leading `-` as an option (e.g. `-oProxyCommand=...`), which is
# enough to get local code execution out of a value that looks like normal
# fleet data. Rejecting a leading `-` and any whitespace/control character
# closes that off without being fussy about real hostnames/usernames.
_NO_OPTION_INJECTION_RE = re.compile(r"^[^\s\x00-\x1f-][^\s\x00-\x1f]*$")


def _reject_ssh_option_injection(value: str, field: str) -> str:
    if not _NO_OPTION_INJECTION_RE.match(value):
        raise ValueError(
            f"{field} {value!r} can't start with '-' or contain whitespace/control characters "
            "(would be parsed as an ssh option, not a literal value)"
        )
    return value


class SSHConfig(BaseModel):
    user: str
    key: str | None = None
    port: int = 22
    # Path to an ssh_config to connect with instead of -i/-p (e.g. one a
    # Teleport tbot identity generates) — the file's own Host block owns
    # auth and host-key verification, Beacon doesn't need to know how.
    config_file: str | None = None

    @field_validator("user")
    @classmethod
    def _safe_user(cls, v: str) -> str:
        return _reject_ssh_option_injection(v, "ssh.user")

    @model_validator(mode="after")
    def _one_auth_mode(self) -> "SSHConfig":
        if bool(self.key) == bool(self.config_file):
            # Both set: config_file silently wins in ssh.py, key is dead
            # weight with no warning. Neither set: nothing to connect with.
            # Both are wrong in a way worth failing loudly on, not guessing.
            raise ValueError("ssh needs exactly one of key or config_file, not both or neither")
        return self


class Host(BaseModel):
    id: str
    address: str
    ssh: SSHConfig
    tags: list[str] = Field(default_factory=list)

    @field_validator("address")
    @classmethod
    def _safe_address(cls, v: str) -> str:
        return _reject_ssh_option_injection(v, "address")


class Agent(BaseModel):
    id: str
    type: str
    host: str
    profile: str | None = None
    # Names of fleet/templates/*.yaml fragments merged under `desired` (in
    # order) to produce the effective config — see backend/templates.py.
    # `desired` here always stays the agent's own overrides only; the merge
    # happens on read, never written back to the file.
    templates: list[str] = Field(default_factory=list)
    desired: dict = Field(default_factory=dict)
    owner: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Cron jobs
# ---------------------------------------------------------------------------

_CRON_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Standard 5-field cron: minute hour day-of-month month day-of-week.
# We deliberately do not support extended syntax (@yearly, L, W, #) in v1.
_CRON_FIELD_RE = re.compile(r"^\*|\*/\d+|\d+(-\d+)?(,/\d+)?$")


def _validate_cron_expression(value: str) -> str:
    value = value.strip()
    fields = value.split()
    if len(fields) != 5:
        raise ValueError(f"cron schedule must have exactly 5 fields, got {len(fields)}: {value!r}")
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for name, field in zip(names, fields):
        if not _CRON_FIELD_RE.match(field):
            raise ValueError(f"invalid {name} field {field!r} in cron expression {value!r}")
    return value


class CronJob(BaseModel):
    id: str
    enabled: bool = True
    schedule: str
    timezone: str = "UTC"
    # Fixed set of Beacon-native actions. Shell execution is intentionally
    # excluded from v1 — it would be a large, hard-to-audit attack surface
    # and would bypass the tier/confirm gates already wired into app.py.
    command: dict = Field(default_factory=dict)
    target_agent_ids: list[str] = Field(default_factory=list)
    timeout: int | None = Field(default=None, ge=1)
    owner: str | None = None
    notes: str | None = None
    # Runtime state, persisted so the UI can show last-run status.
    last_run_at: str | None = None
    last_run_status: str | None = None
    last_run_output: str | None = None

    @field_validator("id")
    @classmethod
    def _safe_id(cls, v: str) -> str:
        if not _CRON_ID_RE.match(v):
            raise ValueError(f"cron job id {v!r} must match {_CRON_ID_RE.pattern}")
        return v

    @field_validator("schedule")
    @classmethod
    def _safe_schedule(cls, v: str) -> str:
        return _validate_cron_expression(v)

    @field_validator("command")
    @classmethod
    def _safe_command(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("command must be a dict")
        action = v.get("action")
        if action not in ("restart", "push_config", "status"):
            raise ValueError(
                f"unsupported cron action {action!r}; v1 supports: restart, push_config, status"
            )
        return v

    @model_validator(mode="after")
    def _has_targets(self) -> "CronJob":
        if not self.target_agent_ids:
            raise ValueError("target_agent_ids must not be empty")
        return self
