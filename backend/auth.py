"""OIDC login (authorization code + PKCE) against Zitadel, gating the GUI
and API behind a session cookie. Auth is only enforced when ZITADEL_ISSUER
and ZITADEL_CLIENT_ID are both set — unset, Beacon runs open, matching the
pre-auth local-dev workflow.
"""

import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

ZITADEL_ISSUER = os.environ.get("ZITADEL_ISSUER", "").rstrip("/")
ZITADEL_CLIENT_ID = os.environ.get("ZITADEL_CLIENT_ID", "")
AUTH_ENABLED = bool(ZITADEL_ISSUER and ZITADEL_CLIENT_ID)

# The same static token the MCP server requires of its own callers doubles
# as its credential for calling *this* API — a service has no browser to
# complete a PKCE redirect with, so it can't hold a session cookie the
# normal way. Bearer <this token> on any /api/* request stands in for one.
MCP_TOKEN = os.environ.get("BEACON_MCP_TOKEN")

SESSION_SECRET = os.environ.get("BEACON_SESSION_SECRET")
if not SESSION_SECRET:
    if AUTH_ENABLED:
        raise RuntimeError("BEACON_SESSION_SECRET must be set when ZITADEL_ISSUER/ZITADEL_CLIENT_ID are configured")
    SESSION_SECRET = secrets.token_urlsafe(32)  # dev-only, ephemeral — fine since nothing enforces it

router = APIRouter()

_jwks_client: "jwt.PyJWKClient | None" = None


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(f"{ZITADEL_ISSUER}/oauth/v2/keys")
    return _jwks_client


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@router.get("/auth/login")
def login(request: Request):
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    # Held in the session cookie between the redirect out and the callback
    # landing — there's no user identity yet at this point to key anything
    # server-side on, so the cookie itself is the only place to put it.
    request.session["pending"] = {"verifier": verifier, "state": state}

    params = {
        "client_id": ZITADEL_CLIENT_ID,
        "redirect_uri": str(request.url_for("auth_callback")),
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(f"{ZITADEL_ISSUER}/oauth/v2/authorize?{urlencode(params)}")


@router.get("/auth/callback", name="auth_callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(400, f"Zitadel returned an error: {error}")

    pending = request.session.pop("pending", None)
    if not pending or not secrets.compare_digest(pending["state"], state):
        raise HTTPException(400, "invalid or expired login state — try logging in again")

    token_resp = httpx.post(
        f"{ZITADEL_ISSUER}/oauth/v2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": str(request.url_for("auth_callback")),
            "client_id": ZITADEL_CLIENT_ID,
            "code_verifier": pending["verifier"],
        },
        timeout=15,
    )
    if not token_resp.is_success:
        raise HTTPException(400, f"token exchange failed: {token_resp.text}")
    tokens = token_resp.json()

    signing_key = _jwks().get_signing_key_from_jwt(tokens["id_token"])
    claims = jwt.decode(
        tokens["id_token"], signing_key.key, algorithms=["RS256"],
        audience=ZITADEL_CLIENT_ID, issuer=ZITADEL_ISSUER,
    )

    request.session["user"] = {
        "sub": claims["sub"],
        "email": claims.get("email"),
        "name": claims.get("name") or claims.get("preferred_username") or claims.get("email"),
    }
    return RedirectResponse("/")


@router.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@router.get("/auth/me")
def me(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "not logged in")
    return user


class AuthMiddleware(BaseHTTPMiddleware):
    """Gates every request except /auth/* behind a logged-in session.
    JSON API paths get a 401; anything else (the GUI) gets redirected to
    /auth/login — a plain GET to `/` shouldn't have to know it needs auth,
    it should just land on the login page.
    """

    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED or request.url.path.startswith("/auth/"):
            return await call_next(request)
        if request.session.get("user"):
            return await call_next(request)
        if MCP_TOKEN and request.headers.get("authorization") == f"Bearer {MCP_TOKEN}":
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return RedirectResponse("/auth/login")
