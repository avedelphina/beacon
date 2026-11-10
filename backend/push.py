"""Web Push: notices to people's browsers, next to email (backend/notify.py).

Why the browser standard and not an app service: the browser's own push
service (Apple's, Google's, Mozilla's) keeps the connection to the device, so
Beacon only sends a request when something happens. That fits a Beacon with no
timer, and it is the same for a phone with the page on its Home Screen, a Mac,
or a desktop browser.

A push says only the notice's subject ("alpha needs your answer"), never an
agent's own words: whoever holds a phone reads its lock screen, and the page
behind the tap shows the rest after login. The payload is encrypted to the
browser (RFC 8291), so the push service cannot read it either.

The VAPID key pair that signs Beacon's requests is derived from
BEACON_A2A_SECRET, like every other Beacon credential, so there is nothing new
to configure or store. Rotating that secret ends every subscription; the page
subscribes again the next time someone opens it.

ECDH, HKDF and AES-GCM come from `cryptography`, which pyjwt[crypto] already
installs: no new dependency.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import threading
import time
from urllib.parse import urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import mesh, opstore

logger = logging.getLogger("beacon")

# Beacon sends a request to whatever endpoint a browser gave it, so without
# this list a logged-in member could point Beacon at any address on its
# network. These are the push services that browsers use.
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")
TTL_SECONDS = 24 * 3600  # a notice nobody could receive for a day is old news
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _private_key() -> ec.EllipticCurvePrivateKey | None:
    secret = os.environ.get(mesh.SECRET_ENV)
    if not secret:
        return None
    # The message has no ">", so it can never be the HMAC of a mesh token ("caller>target").
    seed = hmac.new(secret.encode(), b"web-push:vapid", hashlib.sha256).digest()
    return ec.derive_private_key(int.from_bytes(seed, "big") % (_P256_ORDER - 1) + 1, ec.SECP256R1())


def _raw(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key() -> str | None:
    """The key a browser subscribes with, or None when Beacon cannot push."""
    key = _private_key()
    return _b64e(_raw(key.public_key())) if key else None


def allowed_endpoint(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (parsed.scheme == "https" and parsed.port in (None, 443)
            and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS))


def encrypt(plaintext: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291 (aes128gcm), one record. `salt` and `server_key` are only
    fixed by the test against the RFC's own example; normally both are new
    for every message."""
    ua_public = _b64d(p256dh)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw(server_key.public_key())
    shared = server_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = HKDF(hashes.SHA256(), 32, salt=_b64d(auth), info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    # 0x02: the last (and only) record. 4096 is the record size the RFC's example uses.
    return salt + struct.pack("!IB", 4096, len(as_public)) + as_public + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


def _vapid(endpoint: str, key: ec.EllipticCurvePrivateKey) -> str:
    from . import auth
    parsed = urlparse(endpoint)
    # Push services want a way to reach whoever runs the sender: Beacon's own
    # https URL, else an operator's address.
    contact = mesh.public_url()
    if not contact.startswith("https://"):
        contact = "mailto:" + min(auth.ALLOWED_EMAILS, default="beacon@example.com")
    claims = {"aud": f"{parsed.scheme}://{parsed.netloc}", "exp": int(time.time()) + 3600, "sub": contact}
    return f"vapid t={jwt.encode(claims, key, algorithm='ES256')}, k={_b64e(_raw(key.public_key()))}"


# --- subscriptions -----------------------------------------------------------


def save(email: str, endpoint: str, p256dh: str, auth: str) -> bool:
    """Keep one browser's subscription for a person; True when it is new.
    The same browser logged in as someone else becomes theirs."""
    with opstore.connect() as conn:
        known = conn.execute("SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, email, p256dh, auth, created_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (endpoint) DO UPDATE SET email = excluded.email, p256dh = excluded.p256dh, auth = excluded.auth",
            (endpoint, email.casefold(), p256dh, auth, time.time()))
    return known is None


def remove(endpoint: str, email: str | None = None) -> None:
    """A person removes their own browser; a push service's "gone" removes any."""
    with opstore.connect() as conn:
        if email is None:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        else:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND email = ?", (endpoint, email.casefold()))


def subscriptions(people: set[str]) -> list[dict]:
    people = {p.casefold() for p in people}
    with opstore.connect() as conn:
        rows = conn.execute("SELECT endpoint, email, p256dh, auth FROM push_subscriptions").fetchall()
    return [dict(r) for r in rows if r["email"] in people]


# --- sending -----------------------------------------------------------------


def deliver(subscription: dict, title: str) -> int | str:
    """Send one notice now. The push service's HTTP status, or the error text."""
    key = _private_key()
    if key is None:
        return f"{mesh.SECRET_ENV} is not set"
    endpoint = subscription["endpoint"]
    try:
        response = httpx.post(endpoint, timeout=10, content=encrypt(
            json.dumps({"title": title}).encode(), subscription["p256dh"], subscription["auth"]), headers={
                "Authorization": _vapid(endpoint, key), "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream", "TTL": str(TTL_SECONDS)})
    except (httpx.HTTPError, ValueError) as exc:  # ValueError: keys that are not a P-256 point
        logger.warning("push to %s not sent: %s", urlparse(endpoint).hostname, exc)
        return str(exc)
    if response.status_code in (404, 410):  # the browser unsubscribed, or the subscription expired
        remove(endpoint)
    elif not response.is_success:
        logger.warning("push to %s refused: %s %s", urlparse(endpoint).hostname, response.status_code, response.text[:200])
    return response.status_code


def send(people: set[str], title: str) -> None:
    """Every browser these people subscribed, in the background: a slow push
    service must never hold up the agent pull or request that caused the notice."""
    if not people or _private_key() is None:
        return
    browsers = subscriptions(people)
    if browsers:
        threading.Thread(target=lambda: [deliver(b, title) for b in browsers], daemon=True).start()
