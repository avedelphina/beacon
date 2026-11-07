"""Web Push (backend/push.py): the encryption matches the RFC, Beacon sends only
to real push services, and a person's notices reach only their own browsers."""

import base64
import collections
import json
import types

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import auth, mesh, notify, push, store
from backend.app import app
from backend.schemas import Agent
from tests.conftest import make_host

ANN, TOM = "ann@example.com", "tom@example.com"
APPLE = "https://web.push.apple.com/QGuQyavXutnMH-example"


def test_encryption_matches_the_rfc_8291_example():
    # RFC 8291, appendix A's keys and salt; the result is section 5's message body.
    server_key = ec.derive_private_key(int.from_bytes(push._b64d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    body = push.encrypt(b"When I grow up, I want to be a watermelon",
                        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                        "BTBZMqHH6r4Tts7J_aSIgg", salt=push._b64d("DGv6ra1nlYgDCS1FRnbzlw"), server_key=server_key)
    assert push._b64e(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
        "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


@pytest.mark.parametrize("url, ok", [
    (APPLE, True),
    ("https://fcm.googleapis.com/fcm/send/abc", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
    ("http://web.push.apple.com/x", False),           # not https
    ("https://web.push.apple.com:8443/x", False),     # another port
    ("https://web.push.apple.com.evil.example/x", False),
    ("https://10.0.0.1/x", False),
    ("https://beacon.example/a2a/x", False),
])
def test_only_browser_push_services_are_endpoints(url, ok):
    assert push.allowed_endpoint(url) is ok


def subscription(endpoint=APPLE):
    browser = ec.generate_private_key(ec.SECP256R1())
    raw = browser.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {"p256dh": push._b64e(raw), "auth": push._b64e(b"0123456789abcdef")}}


def as_person(email):
    payload = base64.b64encode(json.dumps({"user": {"email": email}}).encode()).decode()
    client = TestClient(app)
    client.cookies.set("session", TimestampSigner(auth.SESSION_SECRET).sign(payload.encode()).decode())
    client.headers["Origin"] = "http://testserver"
    return client


@pytest.fixture
def sent(fleet, monkeypatch):
    """Auth on, Tom an operator, Ann the owner of a1; every push recorded, none sent."""
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM}))
    store.upsert_host(make_host("h1"))
    store.upsert_agent(Agent.model_validate({"id": "a1", "type": "hermes", "host": "h1", "owner": ANN}))
    monkeypatch.setattr(notify, "_sent", collections.defaultdict(collections.deque))  # a fresh hourly limit
    calls = Sent()

    def post(url, content, headers, timeout):
        calls.append({"url": url, "body": content, "headers": headers})
        return types.SimpleNamespace(status_code=calls.status, is_success=calls.status < 300, text="")

    monkeypatch.setattr(push.httpx, "post", post)
    return calls


class Sent(list):
    status = 201  # what the push service answers


def test_a_new_browser_gets_a_signed_test_notice_once(sent):
    tom = as_person(TOM)
    sub = subscription()
    first = tom.post("/api/push", json=sub).json()
    assert first == {"new": True, "sent": 201}
    assert tom.post("/api/push", json=sub).json() == {"new": False, "sent": None}  # every page open re-sends it
    assert len(sent) == 1
    headers = sent[0]["headers"]
    assert headers["Content-Encoding"] == "aes128gcm" and int(headers["TTL"]) > 0
    token = headers["Authorization"].removeprefix("vapid t=").split(", k=")[0]
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push._b64d(tom.get("/api/push").json()["key"]))
    claims = jwt.decode(token, public, algorithms=["ES256"], audience="https://web.push.apple.com")
    assert claims["sub"] == "mailto:" + TOM


def test_beacon_refuses_an_endpoint_that_is_not_a_push_service_and_keys_that_cannot_encrypt(sent):
    tom = as_person(TOM)
    assert tom.post("/api/push", json=subscription("https://10.0.0.1/steal")).status_code == 400
    bad = subscription()
    bad["keys"]["p256dh"] = push._b64e(b"\x04" + b"\x01" * 64)  # not a point on the curve
    assert tom.post("/api/push", json=bad).status_code == 400
    assert sent == []


def test_notifications_need_a_person_and_the_secret(sent, monkeypatch):
    monkeypatch.setattr(auth, "MCP_TOKEN", "mcp-token")
    service = TestClient(app, headers={"Authorization": "Bearer mcp-token"})
    assert service.post("/api/push", json=subscription()).status_code == 409
    monkeypatch.delenv(mesh.SECRET_ENV)
    assert as_person(TOM).get("/api/push").json() == {"key": None}
    assert as_person(TOM).post("/api/push", json=subscription()).status_code == 409


def test_a_notice_reaches_the_people_it_concerns_and_only_their_browsers(sent):
    ann, tom = as_person(ANN), as_person(TOM)
    ann.post("/api/push", json=subscription(APPLE + "-ann"))
    tom.post("/api/push", json=subscription(APPLE + "-tom"))
    store.upsert_agent(Agent.model_validate({"id": "b1", "type": "hermes", "host": "h1", "owner": "bob@example.com"}))
    sent.clear()
    notify.agent_quiet("a1", 7200)  # Ann owns a1, Tom runs the fleet: both hear
    assert sorted(c["url"] for c in sent) == [APPLE + "-ann", APPLE + "-tom"]
    sent.clear()
    notify.agent_quiet("b1", 7200)  # Bob's agent: Ann hears nothing
    assert [c["url"] for c in sent] == [APPLE + "-tom"]


def test_a_gone_subscription_is_dropped_and_a_person_removes_only_their_own(sent):
    ann, tom = as_person(ANN), as_person(TOM)
    ann.post("/api/push", json=subscription(APPLE + "-ann"))
    tom.post("/api/push", json=subscription(APPLE + "-tom"))
    assert ann.request("DELETE", "/api/push", json={"endpoint": APPLE + "-tom"}).status_code == 200
    assert [s["endpoint"] for s in push.subscriptions({TOM})] == [APPLE + "-tom"]  # not Ann's to remove
    sent.status = 410
    sent.clear()
    notify.agent_quiet("a1", 7200)
    assert push.subscriptions({ANN, TOM}) == []  # the push service said both are gone


def test_the_app_files_load_without_a_session_and_the_page_does_not(sent):
    anonymous = TestClient(app, follow_redirects=False)
    for path in sorted(auth.PWA_FILES):
        assert anonymous.get(path).status_code == 200, path
    assert anonymous.get("/app.js").status_code == 307
    assert anonymous.get("/api/push").status_code == 401
