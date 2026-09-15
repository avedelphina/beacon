"""Authentication helpers for the MCP HTTP service."""

import secrets


def is_valid_bearer(authorization: object, token: str | None) -> bool:
    return (
        isinstance(authorization, str)
        and token is not None
        and secrets.compare_digest(authorization, f"Bearer {token}")
    )
