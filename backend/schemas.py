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
        if not self.key and not self.config_file:
            raise ValueError("ssh needs either a key or a config_file")
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
    desired: dict = Field(default_factory=dict)
    owner: str | None = None
    notes: str | None = None
