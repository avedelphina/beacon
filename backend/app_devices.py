"""Devices logged in through the native app (iOS and iPadOS first; docs/ios-app-plan.md).

A phone has no browser session to keep, so the app logs in once and Beacon gives it a token of its
own. Why not accept Zitadel's access token on every request instead: Zitadel's access tokens are
opaque by default and carry no email, so each request would need a call to Zitadel, and revoking one
phone would mean revoking the person everywhere. A Beacon token is random, kept only as a hash
(like a client's, backend/clients.py), lasts IDLE_SECONDS after its last use, and is revoked per
device. The person's role is still read from their email on every request (auth.role_for_email), so
taking someone off the lists or changing an owner ends a device's access at once, as for a session.

The login uses a native Zitadel client (PKCE, no secret; ZITADEL_APP_CLIENT_ID). The app sends Beacon
the ID token it got, and the public half of a key that the device made in its Secure Enclave, usable
only after Face ID. The ID token's nonce is the hash of that key, and the login is signed by it, so a
leaked ID token cannot register some other device.

The token alone counts as a person for what a person may do up to CL3: that is what lets a lock-screen
Approve work. CL4 and CL5 need more: each such request carries a signature by the device key
(verify_request), which the device makes only after Face ID. A stolen token is not a CL4/CL5 approver.
"""

import base64
import hashlib
import math
import os
import secrets
import time

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import auth, opstore

CLIENT_ID = os.environ.get("ZITADEL_APP_CLIENT_ID", "")
PREFIX = "bk_app_"
API = 1  # also in GET /api/version: raised only when a change breaks the app (docs/ios-app-plan.md, 3.4)
IDLE_SECONDS = 30 * 24 * 3600  # a device not used for 30 days is logged out (decided 2026-11-26)
TOUCH_AFTER = 60  # last_seen_at is written at most this often
MAX_PER_PERSON = 10  # an 11th login replaces the device used least recently
LOGIN_FRESH_SECONDS = 300  # an ID token older than this is not a login happening now
SIGNED_WINDOW_SECONDS = 120  # how far a signed request's time may be from Beacon's clock
VERSION_TAG = b"beacon-app-v1\n"  # first line of everything a device signs; a new format gets a new tag


def enabled() -> bool:
    return auth.AUTH_ENABLED and bool(CLIENT_ID)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _raw_key(public_key: str) -> bytes:
    raw = base64.b64decode(public_key, validate=True)
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)  # ValueError for anything but a P-256 point
    return raw


def nonce_for(public_key: str) -> str:
    """The nonce the app puts in its authorization request: binds the ID token to this device key."""
    return _b64url(hashlib.sha256(_raw_key(public_key)).digest())


def login_message(id_token: str) -> bytes:
    return VERSION_TAG + b"login\n" + hashlib.sha256(id_token.encode()).hexdigest().encode()


def request_message(method: str, path: str, signed_at: str, body: bytes) -> bytes:
    """What a device signs for a CL4/CL5 request. `path` includes the query string, if any;
    `signed_at` is the header's own text, so no float formatting can differ between the two sides."""
    return VERSION_TAG + f"{method.upper()}\n{path}\n{signed_at}\n{hashlib.sha256(body).hexdigest()}".encode()


def verify(public_key: str, message: bytes, signature: str) -> bool:
    """An ECDSA P-256/SHA-256 signature in DER, base64: what CryptoKit's derRepresentation gives."""
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _raw_key(public_key))
        key.verify(base64.b64decode(signature, validate=True), message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


class LoginRefused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _claims(id_token: str, public_key: str) -> dict:
    try:
        signing_key = auth._jwks().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(id_token, signing_key.key, algorithms=["RS256"], audience=CLIENT_ID,
                            issuer=auth.ZITADEL_ISSUER, leeway=30,
                            options={"require": ["exp", "iat", "sub", "nonce"]})
    except jwt.PyJWTError as exc:
        raise LoginRefused(401, f"the ID token is not valid: {exc}")
    if claims["iat"] < time.time() - LOGIN_FRESH_SECONDS:
        raise LoginRefused(401, "the ID token is too old: log in again")
    if not secrets.compare_digest(str(claims["nonce"]), nonce_for(public_key)):
        raise LoginRefused(401, "the ID token was not made for this device's key")
    return claims


def login(id_token: str, public_key: str, name: str, signature: str) -> dict:
    """Register a device for the person in the ID token; the token is shown here once."""
    try:
        nonce_for(public_key)
    except ValueError:
        raise LoginRefused(400, "public_key is not a base64 P-256 point (X9.63)")
    if not verify(public_key, login_message(id_token), signature):
        raise LoginRefused(401, "the login is not signed by the device key")
    claims = _claims(id_token, public_key)
    role = auth.admit(claims)
    if role is None:
        raise LoginRefused(403, "this Beacon instance is not configured to admit that account")
    email = claims["email"].casefold()
    token, device_id, now = PREFIX + secrets.token_urlsafe(32), "dev_" + secrets.token_urlsafe(9), time.time()
    with opstore.connect() as db:
        db.execute("INSERT INTO app_devices (id, email, name, token_hash, public_key, created_at, last_seen_at)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?)", (device_id, email, name, _hash(token), public_key, now, now))
        db.execute("DELETE FROM app_devices WHERE email = ? AND id NOT IN"
                   " (SELECT id FROM app_devices WHERE email = ? ORDER BY last_seen_at DESC LIMIT ?)",
                   (email, email, MAX_PER_PERSON))
    return {"token": token, "device_id": device_id, "email": email,
            "name": claims.get("name") or claims.get("preferred_username") or email, "role": role,
            "idle_days": IDLE_SECONDS // 86400}


def resolve(presented: str) -> dict | None:
    """The device a presented token belongs to, or None: unknown, logged out, or idle too long."""
    if not isinstance(presented, str) or not presented.startswith(PREFIX):
        return None
    now = time.time()
    with opstore.connect() as db:
        row = db.execute("SELECT id, email, name, public_key, last_seen_at FROM app_devices WHERE token_hash = ?",
                         (_hash(presented),)).fetchone()
        if row is None:
            return None
        if now - row["last_seen_at"] > IDLE_SECONDS:
            db.execute("DELETE FROM app_devices WHERE id = ?", (row["id"],))
            return None
        if now - row["last_seen_at"] > TOUCH_AFTER:
            db.execute("UPDATE app_devices SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
    return {k: row[k] for k in ("id", "email", "name", "public_key")}


def verify_request(device: dict, method: str, path: str, signed_at: str, body: bytes, signature: str) -> str | None:
    """None when the device signed this very request just now; else why not. Each signature works once:
    its time must be later than the last one this device used, which needs no table of used nonces."""
    try:
        when = float(signed_at)
    except ValueError:
        return "X-Beacon-Signed-At must be the signing time in Unix seconds"
    if not math.isfinite(when) or abs(time.time() - when) > SIGNED_WINDOW_SECONDS:
        return "the signature's time is too far from Beacon's clock: check the device's date and time"
    if not verify(device["public_key"], request_message(method, path, signed_at, body), signature):
        return "the signature does not match this request and device"
    with opstore.connect() as db:
        moved = db.execute("UPDATE app_devices SET last_signed_at = ? WHERE id = ? AND last_signed_at < ?",
                           (when, device["id"], when)).rowcount
    return None if moved else "this signature was already used"


def devices(email: str | None) -> list[dict]:
    """One person's devices, or everyone's (email None: an operator, or open mode)."""
    query = "SELECT id, email, name, created_at, last_seen_at FROM app_devices"
    with opstore.connect() as db:
        rows = (db.execute(query + " WHERE email = ? ORDER BY last_seen_at DESC", (email.casefold(),)) if email
                else db.execute(query + " ORDER BY email, last_seen_at DESC")).fetchall()
    return [dict(r) | {"expires_at": r["last_seen_at"] + IDLE_SECONDS} for r in rows]


def revoke(device_id: str, email: str | None) -> bool:
    """Log a device out; `email` limits it to that person's own devices."""
    with opstore.connect() as db:
        if email is None:
            return db.execute("DELETE FROM app_devices WHERE id = ?", (device_id,)).rowcount > 0
        return db.execute("DELETE FROM app_devices WHERE id = ? AND email = ?",
                          (device_id, email.casefold())).rowcount > 0


# --- routes ------------------------------------------------------------------

router = APIRouter()


class AppLogin(BaseModel):
    id_token: str = Field(max_length=16384)
    public_key: str = Field(max_length=200)
    name: str = Field(min_length=1, max_length=100)
    signature: str = Field(max_length=200)


@router.get("/auth/app/config")
def app_config() -> dict:
    """What the app needs before it can log in, so that a person types only Beacon's URL."""
    if not enabled():
        raise HTTPException(404, "this Beacon has no app login: set ZITADEL_APP_CLIENT_ID (and OIDC auth)")
    return {"issuer": auth.ZITADEL_ISSUER, "client_id": CLIENT_ID, "api": API}


@router.post("/auth/app/login")
def app_login(body: AppLogin) -> dict:
    if not enabled():
        raise HTTPException(404, "this Beacon has no app login")
    try:
        return login(body.id_token, body.public_key, body.name.strip(), body.signature)
    except LoginRefused as exc:
        raise HTTPException(exc.status, exc.detail)


def _whose(request: Request) -> str | None:
    """Whose devices this request may see and log out: an operator's (or open mode's) are everyone's."""
    user = auth.current_user(request)
    email = (user or {}).get("email")
    if not email or auth.role_for_email(email) == "operator":
        return None
    return email


@router.get("/api/app/devices")
def list_devices(request: Request) -> list[dict]:
    current = getattr(request.state, "app_device", None)
    return [d | {"current": bool(current) and d["id"] == current["id"]} for d in devices(_whose(request))]


@router.delete("/api/app/devices/{device_id}")
def delete_device(device_id: str, request: Request) -> dict:
    """Log out (the app deletes its own device) or revoke one from the page."""
    if not revoke(device_id, _whose(request)):
        raise HTTPException(404, "no such device")
    return {"ok": True}
