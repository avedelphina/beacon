"""Notifications to the native app through Apple's push service (APNs), next to Web Push (backend/push.py).

Why APNs and not only Web Push: a notification sent by APNs can carry actions (Approve, Reject) that
work from the lock screen, and the app decides what a tap opens. Safari's Web Push can do neither.

The same rule as Web Push: a notification holds only the notice's subject ("alpha needs your answer"),
never an agent's words, because whoever holds the phone reads its lock screen. What a request is about
travels as ids (`beacon` in the payload); the app fetches the rest after the person unlocks it.

Apple issues the signing key (a .p8 file), so unlike the VAPID key it cannot be derived from
BEACON_A2A_SECRET: four settings name it, and without all four Beacon sends no APNs and the app works
without notifications. Token auth (an ES256 JWT, pyjwt already installed) over HTTP/2, which APNs
requires: `httpx[http2]` brings `h2`.
"""

import logging
import os
import threading
import time
from functools import lru_cache
from pathlib import Path

import httpx
import jwt

from . import app_devices

logger = logging.getLogger("beacon")

KEY_FILE = os.environ.get("BEACON_APNS_KEY_FILE", "")
KEY_ID = os.environ.get("BEACON_APNS_KEY_ID", "")
TEAM_ID = os.environ.get("BEACON_APNS_TEAM_ID", "")
TOPIC = os.environ.get("BEACON_APNS_TOPIC", "")  # the app's bundle id
HOSTS = {"production": "api.push.apple.com", "sandbox": "api.sandbox.push.apple.com"}
TTL_SECONDS = 24 * 3600  # as for Web Push: a notice nobody could receive for a day is old news
# Apple refuses a provider token older than an hour and one renewed more often than every 20 minutes.
TOKEN_RENEW_SECONDS = 40 * 60
GONE = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}  # the token will never work again

_token: tuple[str, float] | None = None
_token_lock = threading.Lock()


def configured() -> bool:
    return bool(KEY_FILE and KEY_ID and TEAM_ID and TOPIC)


def _provider_token() -> str:
    global _token
    with _token_lock:
        if _token is None or time.time() - _token[1] > TOKEN_RENEW_SECONDS:
            now = time.time()
            key = Path(KEY_FILE).read_text()
            _token = (jwt.encode({"iss": TEAM_ID, "iat": int(now)}, key, algorithm="ES256",
                                 headers={"kid": KEY_ID}), now)
        return _token[0]


@lru_cache(maxsize=1)
def _client() -> httpx.Client:
    # One connection reused for every notice: APNs wants long-lived HTTP/2 connections, not one per push.
    return httpx.Client(http2=True, timeout=10)


def category(about: dict | None) -> str | None:
    """Which set of actions the notification shows (the app registers the same names).
    CL4/CL5 open the app, where Face ID signs the answer: a lock-screen action cannot ask for Face ID.
    Approvals and reviews up to CL3 can be answered from the lock screen. Other requests need words
    or a choice, so they open the app."""
    if not about or about.get("workflow") is None:
        return None
    if about.get("clearance", 0) >= 4:
        return "WF_HUMAN"
    return "WF_APPROVE" if about.get("kind") in ("approval", "review") else "WF_OPEN"


def payload(subject: str, urgent: bool = False, about: dict | None = None) -> dict:
    cat = category(about)
    aps: dict = {"alert": {"title": subject}, "sound": "default"}
    if cat:
        aps["category"] = cat
        # Time Sensitive breaks through Focus: for what needs a person now, not for every notice.
        aps["interruption-level"] = "time-sensitive" if urgent or cat == "WF_HUMAN" else "active"
    if about and about.get("card") is not None:
        aps["thread-id"] = f"card-{about['card']}"
    body = {"aps": aps}
    if about:
        body["beacon"] = {k: about[k] for k in ("workflow", "clearance", "kind", "card", "agent", "project")
                          if about.get(k) is not None}
    return body


def deliver(device: dict, body: dict, priority: int) -> int | str:
    """Send one notice now. APNs's HTTP status, or the error text."""
    global _token
    host = HOSTS.get(device["apns_env"], HOSTS["production"])
    try:
        response = _client().post(f"https://{host}/3/device/{device['apns_token']}", json=body, headers={
            "authorization": f"bearer {_provider_token()}", "apns-topic": TOPIC, "apns-push-type": "alert",
            "apns-priority": str(priority), "apns-expiration": str(int(time.time()) + TTL_SECONDS)})
    except (httpx.HTTPError, OSError, ValueError) as exc:  # OSError: the key file; ValueError: a key that is not one
        logger.warning("APNs notice to device %s not sent: %s", device["id"], exc)
        return str(exc)
    if response.is_success:
        return response.status_code
    try:
        reason = response.json().get("reason", "")
    except ValueError:
        reason = ""
    if response.status_code == 410 or reason in GONE:
        app_devices.drop_apns(device["apns_token"])
    elif reason == "ExpiredProviderToken":
        with _token_lock:
            _token = None  # renewed on the next notice
    logger.warning("APNs refused a notice to device %s: %s %s", device["id"], response.status_code, reason)
    return response.status_code


def send(people: set[str], subject: str, urgent: bool = False, about: dict | None = None) -> None:
    """Every app device these people have, in the background, as push.send does for browsers."""
    if not people or not configured():
        return
    devices = app_devices.apns_targets(people)
    if not devices:
        return
    body = payload(subject, urgent, about)
    # 10 = now; 5 = when it suits the phone's power. A request waiting on someone should not wait more.
    priority = 10 if urgent or category(about) else 5
    threading.Thread(target=lambda: [deliver(d, body, priority) for d in devices], daemon=True).start()
