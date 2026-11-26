"""Notifications to the native app through APNs (backend/apns.py). Apple is replaced by an httpx
MockTransport; the provider token is a real ES256 JWT signed with a test key."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from backend import apns, app_devices, notify, opstore
from tests.test_app_devices import Device, logged_in, secured  # noqa: F401 (fixture)

TOKEN_A, TOKEN_B = "a" * 64, "b" * 64


class Apple:
    """What APNs received, and what it answers."""

    def __init__(self):
        self.requests, self.answer = [], (200, {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answer
        return httpx.Response(status, json=body)


@pytest.fixture
def apple(fleet, monkeypatch, tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "AuthKey.p8").write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    monkeypatch.setattr(apns, "KEY_FILE", str(tmp_path / "AuthKey.p8"))
    monkeypatch.setattr(apns, "KEY_ID", "KEY123")
    monkeypatch.setattr(apns, "TEAM_ID", "TEAM456")
    monkeypatch.setattr(apns, "TOPIC", "org.example.beacon")
    monkeypatch.setattr(apns, "_token", None)
    fake = Apple()
    monkeypatch.setattr(apns, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))

    class Now:  # run the background sends inline, so the test sees them
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(apns.threading, "Thread", Now)
    monkeypatch.setattr(notify.push.threading, "Thread", Now)
    notify._sent.clear()
    fake.public_key = key.public_key()
    return fake


def _device(email="tom@example.com", token=TOKEN_A, env="production", seen=None) -> str:
    device_id = "dev_" + token[:6] + email[:3]
    now = time.time()
    with opstore.connect() as db:
        db.execute("INSERT INTO app_devices (id, email, name, token_hash, public_key, created_at, last_seen_at)"
                   " VALUES (?, ?, 'phone', ?, 'k', ?, ?)", (device_id, email, device_id, now, seen or now))
    app_devices.set_apns(device_id, token, env)
    return device_id


# --- what a notification says -----------------------------------------------------

@pytest.mark.parametrize("about, urgent, category, level", [
    ({"workflow": 1, "clearance": 3, "kind": "approval", "card": 7}, False, "WF_APPROVE", "active"),
    ({"workflow": 1, "clearance": 2, "kind": "review", "card": 7}, True, "WF_APPROVE", "time-sensitive"),
    ({"workflow": 1, "clearance": 4, "kind": "approval", "card": 7}, False, "WF_HUMAN", "time-sensitive"),
    ({"workflow": 1, "clearance": 5, "kind": "decision", "card": 7}, False, "WF_HUMAN", "time-sensitive"),
    ({"workflow": 1, "clearance": 2, "kind": "decision", "card": 7}, False, "WF_OPEN", "active"),
])
def test_a_request_gets_the_actions_its_clearance_allows(about, urgent, category, level):
    body = apns.payload("alpha needs your answer", urgent, about)
    assert body["aps"]["category"] == category and body["aps"]["interruption-level"] == level
    assert body["aps"]["alert"] == {"title": "alpha needs your answer"} and body["aps"]["thread-id"] == "card-7"
    assert body["beacon"] == about


def test_any_other_notice_is_the_subject_and_nothing_else():
    assert apns.payload("alpha has not synced for 2 hours") == {
        "aps": {"alert": {"title": "alpha has not synced for 2 hours"}, "sound": "default"}}


# --- sending ------------------------------------------------------------------------

def test_a_notice_goes_to_the_devices_apns_host_signed_with_the_provider_token(apple):
    _device(token=TOKEN_A, env="production")
    _device("tom@example.com", TOKEN_B, "sandbox")
    apns.send({"tom@example.com"}, "alpha needs your answer", about={"workflow": 3, "clearance": 3, "kind": "review"})
    assert sorted(str(r.url) for r in apple.requests) == [
        f"https://api.push.apple.com/3/device/{TOKEN_A}", f"https://api.sandbox.push.apple.com/3/device/{TOKEN_B}"]
    sent = apple.requests[0]
    assert (sent.headers["apns-topic"], sent.headers["apns-push-type"], sent.headers["apns-priority"]) == \
        ("org.example.beacon", "alert", "10")
    provider = sent.headers["authorization"].removeprefix("bearer ")
    assert jwt.get_unverified_header(provider)["kid"] == "KEY123"
    assert jwt.decode(provider, apple.public_key, algorithms=["ES256"])["iss"] == "TEAM456"
    assert json.loads(sent.content)["aps"]["category"] == "WF_APPROVE"


def test_an_ordinary_notice_is_sent_when_it_suits_the_phone(apple):
    _device()
    apns.send({"tom@example.com"}, "alpha is syncing again")
    assert apple.requests[0].headers["apns-priority"] == "5"


def test_the_provider_token_is_reused_then_renewed(apple):
    _device()
    for _ in range(2):
        apns.send({"tom@example.com"}, "one")
    first, second = (r.headers["authorization"] for r in apple.requests)
    assert first == second  # Apple refuses renewals more often than every 20 minutes
    aged = time.time() - apns.TOKEN_RENEW_SECONDS - 1
    apns._token = (apns._token[0], aged)
    apns.send({"tom@example.com"}, "two")
    assert apns._token[1] > aged + 60  # made anew


@pytest.mark.parametrize("status, reason", [(410, "Unregistered"), (400, "BadDeviceToken"), (400, "DeviceTokenNotForTopic")])
def test_a_token_apple_calls_gone_is_dropped(apple, status, reason):
    _device()
    apple.answer = (status, {"reason": reason})
    apns.send({"tom@example.com"}, "one")
    assert app_devices.apns_targets({"tom@example.com"}) == []


def test_an_expired_provider_token_is_renewed_on_the_next_notice(apple):
    _device()
    apple.answer = (403, {"reason": "ExpiredProviderToken"})
    apns.send({"tom@example.com"}, "one")
    assert apns._token is None and app_devices.apns_targets({"tom@example.com"})  # the device keeps its token


def test_nothing_is_sent_without_all_four_settings_or_to_other_people_or_idle_devices(apple, monkeypatch):
    _device()
    _device("ann@example.com", TOKEN_B, seen=time.time() - app_devices.IDLE_SECONDS - 1)
    apns.send({"bob@example.com", "ann@example.com"}, "one")
    assert apple.requests == []
    monkeypatch.setattr(apns, "TEAM_ID", "")
    apns.send({"tom@example.com"}, "one")
    assert apple.requests == []


# --- through notify -----------------------------------------------------------------

def test_notify_reaches_the_app_with_the_request_ids_and_counts_one_limit_for_both_pushes(apple, monkeypatch):
    _device()
    browsers = []
    monkeypatch.setattr(notify.push, "subscriptions", lambda people: [{"email": "tom@example.com"}])
    monkeypatch.setattr(notify.push, "send", lambda people, title, **kw: browsers.append(sorted(people)))
    request = {"id": 9, "clearance": 4, "kind": "approval", "card_id": 2, "requester": "alpha", "title": "Rotate",
               "summary": "s", "eligible_responder": "tom@example.com", "expires_at": time.time() + 60}
    notify._tell({"tom@example.com"}, "Beacon: alpha needs your answer", "body", **notify._about(request))
    body = json.loads(apple.requests[0].content)
    assert body["beacon"] == {"workflow": 9, "clearance": 4, "kind": "approval", "card": 2}
    assert body["aps"]["alert"]["title"] == "alpha needs your answer" and browsers == [["tom@example.com"]]
    for _ in range(notify.MAX_PER_HOUR):
        notify._tell({"tom@example.com"}, "Beacon: more", "body")
    assert len(apple.requests) == notify.MAX_PER_HOUR  # both channels share the person's room


def test_someone_with_only_the_app_is_notified(apple, monkeypatch):
    _device()
    monkeypatch.setattr(notify.push, "subscriptions", lambda people: [])
    notify._tell({"tom@example.com"}, "Beacon: hello", "body")
    assert len(apple.requests) == 1


# --- registering --------------------------------------------------------------------

def test_the_app_registers_its_apns_token_and_a_new_login_takes_it_over(secured):
    old, new = logged_in(secured), logged_in(secured)
    put = lambda device, body: secured.put("/api/app/devices/self/apns", json=body, headers=device.headers())
    assert put(old, {"token": TOKEN_A.upper(), "env": "sandbox"}).status_code == 200
    assert [(d["id"], d["apns_token"], d["apns_env"]) for d in app_devices.apns_targets({"tom@example.com"})] == \
        [(old.id, TOKEN_A, "sandbox")]
    assert put(new, {"token": TOKEN_A, "env": "production"}).status_code == 200  # same phone, logged in again
    assert [d["id"] for d in app_devices.apns_targets({"tom@example.com"})] == [new.id]
    assert put(new, {"token": "not-hex", "env": "production"}).status_code == 422
    assert put(new, {"token": TOKEN_B, "env": "staging"}).status_code == 422


def test_only_an_app_device_has_an_apns_token(secured):
    response = secured.put("/api/app/devices/self/apns", json={"token": TOKEN_A, "env": "production"},
                           headers={"Authorization": "Bearer mcp-secret"})
    assert response.status_code == 409


def test_a_member_device_may_register(secured):
    ann = logged_in(secured, "ann@example.com")
    assert secured.put("/api/app/devices/self/apns", json={"token": TOKEN_A, "env": "production"},
                       headers=ann.headers()).status_code == 200
