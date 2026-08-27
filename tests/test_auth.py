import base64
import hashlib

from backend.auth import _pkce_pair


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
