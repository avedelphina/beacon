from pydantic import BaseModel, Field, model_validator


class SSHConfig(BaseModel):
    user: str
    key: str | None = None
    port: int = 22
    # Path to an ssh_config to connect with instead of -i/-p (e.g. one a
    # Teleport tbot identity generates) — the file's own Host block owns
    # auth and host-key verification, Beacon doesn't need to know how.
    config_file: str | None = None

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
