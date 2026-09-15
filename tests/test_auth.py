import base64
import hashlib
import importlib.util
from pathlib import Path

from backend.auth import _pkce_pair, is_allowed_email, is_mcp_authorization


_auth_spec = importlib.util.spec_from_file_location("beacon_mcp_auth", Path(__file__).parents[1] / "mcp" / "auth.py")
_auth_module = importlib.util.module_from_spec(_auth_spec)
_auth_spec.loader.exec_module(_auth_module)
is_valid_bearer = _auth_module.is_valid_bearer


def test_pkce_challenge_is_s256_of_verifier():
    # Get this wrong (e.g. swap sha256 for md5, or forget the base64url
    # strip) and login breaks with an opaque error from the OIDC provider
    # — worth pinning down directly rather than only via a live login.
    verifier, challenge = _pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_pkce_pair_is_random_each_call():
    v1, c1 = _pkce_pair()
    v2, c2 = _pkce_pair()
    assert v1 != v2
    assert c1 != c2


def test_pkce_verifier_meets_rfc7636_minimum_length():
    # RFC 7636 requires the verifier to be 43-128 characters.
    verifier, _ = _pkce_pair()
    assert 43 <= len(verifier) <= 128



def test_allowed_email_is_case_insensitive_and_normalized(monkeypatch):
    import backend.auth as auth

    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"tom@example.com", "admin@example.com"}))

    assert is_allowed_email(" TOM@EXAMPLE.COM ".strip())
    assert is_allowed_email("admin@example.com")
    assert not is_allowed_email("other@example.com")


def test_allowed_email_rejects_missing_or_non_string_claims(monkeypatch):
    import backend.auth as auth

    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"tom@example.com"}))

    assert not is_allowed_email(None)
    assert not is_allowed_email(123)


def test_mcp_authorization_requires_exact_bearer_token(monkeypatch):
    import backend.auth as auth

    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")

    assert is_mcp_authorization("Bearer test-mcp-token")
    assert not is_mcp_authorization("Bearer test-mcp-toke")
    assert not is_mcp_authorization("Bearer test-mcp-token-extra")
    assert not is_mcp_authorization(None)
    assert not is_mcp_authorization(123)
    assert is_valid_bearer("Bearer test-mcp-token", "test-mcp-token")
    assert not is_valid_bearer("Bearer test-mcp-toke", "test-mcp-token")
    assert not is_valid_bearer(None, "test-mcp-token")
