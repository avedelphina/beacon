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
