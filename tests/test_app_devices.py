"""The native app's login and device tokens (backend/app_devices.py).

The identity provider is real JWT verification against a test RSA key; the device key is a real P-256
key, as the Secure Enclave's would be. Everything Beacon decides runs through the app."""

import base64
import hashlib
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from backend import app_devices, audit, auth, board, mesh, policy, store, workflow
from backend.app import app
from backend.schemas import Agent
from tests.conftest import make_host

TOM, ANN, BOB = "tom@example.com", "ann@example.com", "bob@example.com"
ISSUER, APP_CLIENT = "https://id.example.com", "app-client-1"
HTTPS = "https://testserver"
_IDP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
VECTOR = Path(__file__).resolve().parent.parent / "apps" / "contract" / "signature-v1.json"


class Device:
    """What the app holds: a device key and, after login, a token."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = base64.b64encode(self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)).decode()
        self.token = self.id = None

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self.key.sign(message, ec.ECDSA(hashes.SHA256()))).decode()

    def id_token(self, email=TOM, verified=True, aud=APP_CLIENT, iat=None, nonce=None, **extra) -> str:
        now = time.time() if iat is None else iat
        claims = {"iss": ISSUER, "aud": aud, "sub": "s-" + email, "email": email, "email_verified": verified,
                  "iat": int(now), "exp": int(now) + 600, "nonce": nonce or app_devices.nonce_for(self.public)} | extra
        return jwt.encode(claims, _IDP_KEY, algorithm="RS256")

    def login(self, client, id_token=None, signature=None):
        id_token = id_token or self.id_token()
        response = client.post("/auth/app/login", json={
            "id_token": id_token, "public_key": self.public, "name": "Test iPhone",
            "signature": signature or self.sign(app_devices.login_message(id_token))})
        if response.status_code == 200:
            self.token, self.id = response.json()["token"], response.json()["device_id"]
        return response

    def headers(self, method=None, path=None, body=b"", signed_at=None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        if method:
            signed_at = signed_at or repr(time.time())
            headers |= {"X-Beacon-Signed-At": signed_at,
                        "X-Beacon-Signature": self.sign(app_devices.request_message(method, path, signed_at, body))}
        return headers


@pytest.fixture
def secured(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "app-test-secret")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM}))
    monkeypatch.setattr(auth, "MCP_TOKEN", "mcp-secret")
    monkeypatch.setattr(auth, "ZITADEL_ISSUER", ISSUER)
    monkeypatch.setattr(auth, "ZITADEL_CLIENT_ID", "web-client-1")
    monkeypatch.setattr(app_devices, "CLIENT_ID", APP_CLIENT)
    monkeypatch.setattr(auth, "_jwks", lambda: type("J", (), {
        "get_signing_key_from_jwt": staticmethod(lambda t: type("K", (), {"key": _IDP_KEY.public_key()})())})())
    store.upsert_host(make_host("h1"))
    for id_, owner in (("worker", BOB), ("annsbot", ANN)):
        store.upsert_agent(Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "owner": owner,
                                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": True}}))
    return TestClient(app, base_url=HTTPS, follow_redirects=False)


def logged_in(client, email=TOM) -> Device:
    device = Device()
    assert device.login(client, device.id_token(email=email)).status_code == 200
    return device


# --- login -------------------------------------------------------------------

def test_the_app_learns_how_to_log_in_from_beacon(secured):
    assert secured.get("/auth/app/config").json() == {"issuer": ISSUER, "client_id": APP_CLIENT, "api": app_devices.API}
    assert secured.get("/api/version", headers={"Authorization": "Bearer mcp-secret"}).json()["api"] == app_devices.API


def test_without_an_app_client_there_is_no_app_login(secured, monkeypatch):
    monkeypatch.setattr(app_devices, "CLIENT_ID", "")
    assert secured.get("/auth/app/config").status_code == 404
    assert Device().login(secured).status_code == 404


def test_an_operator_logs_in_and_the_token_works(secured):
    device = Device()
    response = device.login(secured)
    assert response.status_code == 200
    assert response.json() | {"token": None, "device_id": None} == {
        "token": None, "device_id": None, "email": TOM, "name": TOM, "role": "operator", "idle_days": 30}
    assert device.token.startswith(app_devices.PREFIX)
    assert secured.get("/api/hosts", headers=device.headers()).status_code == 200
    assert secured.get("/auth/me", headers=device.headers()).json()["role"] == "operator"


@pytest.mark.parametrize("change, status", [
    ({"aud": "web-client-1"}, 401),             # an ID token for the web page, not the app
    ({"iat": time.time() - 3600}, 401),         # not a login happening now
    ({"nonce": "someone-elses-key"}, 401),      # made for another device key
    ({"email": "stranger@example.com"}, 403),   # nobody named them
    ({"email": ANN, "verified": False}, 403),   # a member must have a verified email
])
def test_a_login_is_refused_when_the_id_token_does_not_hold(secured, change, status):
    device = Device()
    assert device.login(secured, device.id_token(**change)).status_code == status


def test_a_login_must_be_signed_by_the_key_it_names(secured):
    device, thief = Device(), Device()
    id_token = device.id_token()
    assert device.login(secured, id_token, signature=thief.sign(app_devices.login_message(id_token))).status_code == 401
    # A leaked ID token is no use with another key: the nonce names this one.
    thief.public = device.public
    assert thief.login(secured, id_token).status_code == 401


def test_a_bad_public_key_is_a_bad_request(secured):
    device = Device()
    device.public = base64.b64encode(b"not a point").decode()
    assert device.login(secured, Device().id_token()).status_code == 400


def test_a_person_keeps_at_most_ten_devices(secured):
    devices = [logged_in(secured) for _ in range(app_devices.MAX_PER_PERSON + 1)]
    assert secured.get("/api/hosts", headers=devices[0].headers()).status_code == 401
    assert secured.get("/api/hosts", headers=devices[-1].headers()).status_code == 200
    assert len(app_devices.devices(TOM)) == app_devices.MAX_PER_PERSON


# --- the token ---------------------------------------------------------------

def test_an_unknown_token_is_401_not_a_redirect(secured):
    response = secured.get("/", headers={"Authorization": f"Bearer {app_devices.PREFIX}nope"})
    assert response.status_code == 401 and "log in again" in response.json()["detail"]


def test_the_role_is_read_on_every_request(secured, monkeypatch):
    device = logged_in(secured)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"someone-else@example.com"}))
    assert secured.get("/api/hosts", headers=device.headers()).status_code == 401
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM}))  # named again: the device works again
    assert secured.get("/api/hosts", headers=device.headers()).status_code == 200


def test_a_device_idle_for_thirty_days_is_logged_out(secured):
    device = logged_in(secured)
    from backend import opstore
    with opstore.connect() as db:
        db.execute("UPDATE app_devices SET last_seen_at = ?", (time.time() - app_devices.IDLE_SECONDS - 1,))
    assert secured.get("/api/hosts", headers=device.headers()).status_code == 401
    assert app_devices.devices(TOM) == []


def test_a_member_device_gets_only_the_member_routes(secured):
    device = logged_in(secured, ANN)
    assert [a["id"] for a in secured.get("/api/agents", headers=device.headers()).json()] == ["annsbot"]
    assert secured.get("/api/hosts", headers=device.headers()).status_code == 403


def test_app_requests_need_no_origin_header(secured):
    device = logged_in(secured)
    body = {"id": "h9", "address": "10.0.0.9", "ssh": {"user": "u", "key": "/k"}}
    assert secured.put("/api/hosts/h9", json=body, headers=device.headers()).status_code == 200


def test_what_the_app_does_is_recorded_under_the_person(secured, fake_ssh):
    device = logged_in(secured)
    body = {"id": "h9", "address": "10.0.0.9", "ssh": {"user": "u", "key": "/k"}}
    secured.put("/api/hosts/h9", json=body, headers=device.headers())
    assert store.history("host", "h9")[0]["actor"] == TOM
    secured.post("/api/agents/worker/restart?confirm=true", headers=device.headers())
    row = audit.recent(1)[0]
    assert (row["actor"], row["via"], row["capability"]) == (TOM, "app", "restart")


# --- devices -----------------------------------------------------------------

def test_a_member_sees_and_logs_out_only_their_own_devices(secured):
    tom, ann, ann2 = logged_in(secured), logged_in(secured, ANN), logged_in(secured, ANN)
    seen = secured.get("/api/app/devices", headers=ann.headers()).json()
    assert sorted(d["id"] for d in seen) == sorted([ann.id, ann2.id])
    assert [d["current"] for d in seen if d["id"] == ann.id] == [True]
    assert secured.delete(f"/api/app/devices/{tom.id}", headers=ann.headers()).status_code == 404
    assert secured.delete(f"/api/app/devices/{ann.id}", headers=ann.headers()).status_code == 200  # log out
    assert secured.get("/api/app/devices", headers=ann.headers()).status_code == 401


def test_an_operator_sees_and_revokes_any_device(secured):
    tom, ann = logged_in(secured), logged_in(secured, ANN)
    assert {d["email"] for d in secured.get("/api/app/devices", headers=tom.headers()).json()} == {TOM, ANN}
    assert secured.delete(f"/api/app/devices/{ann.id}", headers=tom.headers()).status_code == 200
    assert secured.get("/api/agents", headers=ann.headers()).status_code == 401


# --- a person, and Face ID at CL4/CL5 ------------------------------------------

def _request(clearance, responder=TOM):
    card = board.create_card("beacon", "Change it")["id"]
    binding = workflow.plan_binding({"c": card, "n": clearance})
    kind = "approval" if clearance >= 4 else "review"
    request = workflow.create(card_id=card, kind=kind, requester="worker", eligible_responder=responder,
                              capability="change", clearance=clearance, title="Change", summary="Exact plan",
                              options=[], target={}, **binding)
    path = f"/api/workflows/{request['id']}/respond"
    body = json.dumps({"action": "approved", "response": {}, **binding}).encode()
    return path, body


def _post(client, path, body, headers):
    return client.post(path, content=body, headers=headers | {"Content-Type": "application/json"})


def test_the_app_answers_cl3_with_its_token_alone(secured, monkeypatch):
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    device = logged_in(secured)
    path, body = _request(3)
    answered = _post(secured, path, body, device.headers())
    assert answered.status_code == 200 and answered.json()["state"] == "approved"
    assert [e["actor"] for e in workflow.get(answered.json()["id"])["events"]][-1] == TOM


def test_cl4_from_the_app_needs_the_face_id_signature(secured, monkeypatch):
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    device = logged_in(secured)
    path, body = _request(4)
    refused = _post(secured, path, body, device.headers())
    assert refused.status_code == 403 and "Face ID" in refused.json()["detail"]
    done = _post(secured, path, body, device.headers("POST", path, body))
    assert done.status_code == 200 and done.json()["state"] == "approved"


def test_a_signature_is_good_once_for_this_request_and_now(secured):
    device = logged_in(secured)
    path, body = _request(4)
    old = _post(secured, path, body, device.headers("POST", path, body, signed_at=repr(time.time() - 600)))
    assert old.status_code == 403 and "clock" in old.json()["detail"]
    other_body = _post(secured, path, body, device.headers("POST", path, body + b" "))
    assert other_body.status_code == 403 and "does not match" in other_body.json()["detail"]
    headers = device.headers("POST", path, b"{}")  # a request that fails after the check spends its signature
    _post(secured, path, b"{}", headers)
    replay = _post(secured, path, b"{}", headers)
    assert replay.status_code == 403 and "already used" in replay.json()["detail"]


def test_another_devices_signature_is_not_this_devices(secured):
    device, other = logged_in(secured), logged_in(secured)
    path, body = _request(4)
    forged = device.headers() | {k: v for k, v in other.headers("POST", path, body).items() if k != "Authorization"}
    assert _post(secured, path, body, forged).status_code == 403


def test_a_cl4_action_from_the_app_needs_the_signature_too(secured):
    device = logged_in(secured)
    current = secured.get("/api/policy", headers=device.headers()).json()
    body = json.dumps({"text": "rules:\n  - {from: ['*'], to: ['*']}\n", "base_hash": current["hash"], "confirm": True}).encode()
    put = lambda headers: secured.put("/api/policy", content=body, headers=headers | {"Content-Type": "application/json"})
    refused = put(device.headers())
    assert refused.status_code == 403 and "Face ID" in refused.json()["detail"]
    assert put(device.headers("PUT", "/api/policy", body)).status_code == 200
    assert policy.load() is not None


def test_the_mcp_token_and_agent_tokens_are_still_no_person(secured):
    path, body = _request(4)
    assert _post(secured, path, body, {"Authorization": "Bearer mcp-secret"}).status_code == 403
    agent = {"Authorization": f"Bearer {mesh.token('worker', mesh.BEACON_PEER)}"}
    assert _post(secured, path, body, agent).status_code == 403
    # A signature header on a non-app request is not a way in either.
    assert _post(secured, path, body, {"Authorization": "Bearer mcp-secret", "X-Beacon-Signature": "x"}).status_code == 403


# --- the format the app implements ---------------------------------------------

def test_the_shared_signature_vector_holds():
    """apps/contract/signature-v1.json is what the app's tests check their own signing against."""
    vector = json.loads(VECTOR.read_text())
    login = vector["login"]
    assert app_devices.login_message(login["id_token"]).decode() == login["message"]
    assert app_devices.nonce_for(vector["public_key"]) == login["nonce"]
    assert app_devices.verify(vector["public_key"], login["message"].encode(), login["signature"])
    req = vector["request"]
    message = app_devices.request_message(req["method"], req["path"], req["signed_at"], req["body"].encode())
    assert message.decode() == req["message"]
    assert app_devices.verify(vector["public_key"], message, req["signature"])
    assert not app_devices.verify(vector["public_key"], message + b"x", req["signature"])
