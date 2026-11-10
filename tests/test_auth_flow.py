"""AuthMiddleware dispatch, the CSRF check, and the OIDC login/callback flow.

The identity provider is replaced at its two seams (the token POST and the ID
token decode); everything Beacon decides itself runs for real, through the app.
"""

import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import auth, mesh, store
from backend.app import app
from backend.schemas import Agent
from tests.conftest import make_host

TOM, STRANGER = "tom@example.com", "stranger@example.com"
# The session cookie is Secure by default (BEACON_SESSION_HTTPS_ONLY), so a client
# that does not speak https never sends it back and no login could round-trip.
HTTPS = "https://testserver"


@pytest.fixture
def secured(fleet, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM}))
    monkeypatch.setattr(auth, "MCP_TOKEN", "mcp-secret")
    monkeypatch.setattr(auth, "ZITADEL_ISSUER", "https://id.example.com")
    monkeypatch.setattr(auth, "ZITADEL_CLIENT_ID", "client-1")


def session_for(email: str) -> TestClient:
    payload = base64.b64encode(json.dumps({"user": {"email": email}}).encode()).decode()
    client = TestClient(app, base_url=HTTPS, follow_redirects=False)
    client.cookies.set("session", TimestampSigner(auth.SESSION_SECRET).sign(payload.encode()).decode(),
                       domain="testserver.local", path="/")  # same jar slot the server's Set-Cookie replaces
    return client


def anonymous() -> TestClient:
    return TestClient(app, base_url=HTTPS, follow_redirects=False)


# --- AuthMiddleware ----------------------------------------------------------

def test_without_a_session_the_api_answers_401_and_the_page_redirects_to_login(secured):
    assert anonymous().get("/api/hosts").status_code == 401
    page = anonymous().get("/")
    assert page.status_code == 307 and page.headers["location"] == "/auth/login"


def test_with_auth_off_every_route_is_open(fleet, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    assert anonymous().get("/api/hosts").status_code == 200


def test_an_operator_session_reaches_the_api(secured):
    assert session_for(TOM).get("/api/hosts").status_code == 200


def test_a_session_for_someone_taken_off_the_list_is_refused_and_cleared(secured, monkeypatch):
    client = session_for(TOM)
    assert client.get("/api/hosts").status_code == 200
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"someone-else@example.com"}))  # login is re-checked every request
    assert client.get("/api/hosts").status_code == 401
    assert client.get("/auth/me").status_code == 401  # the cookie itself was cleared


def test_a_forged_session_cookie_is_not_a_login(secured):
    client = anonymous()
    client.cookies.set("session", "not-signed-by-beacon")
    assert client.get("/api/hosts").status_code == 401


def test_the_mcp_bearer_token_stands_in_for_a_session_but_only_when_exact(secured):
    assert anonymous().get("/api/hosts", headers={"Authorization": "Bearer mcp-secret"}).status_code == 200
    for header in ("Bearer mcp-secre", "Bearer mcp-secret-x", "mcp-secret", "Basic mcp-secret"):
        assert anonymous().get("/api/hosts", headers={"Authorization": header}).status_code == 401, header


def test_the_app_files_are_served_without_a_session_and_nothing_else_is(secured):
    assert anonymous().get("/manifest.json").status_code == 200
    assert anonymous().get("/sw.js").status_code == 200
    assert anonymous().get("/app.js").status_code == 307  # only PWA_FILES are open


def test_agent_routes_ignore_a_browser_session(secured, monkeypatch):
    """/mesh and /a2a authenticate each agent by its own token: a session unlocks neither."""
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    store.upsert_host(make_host("h1"))
    store.upsert_agent(Agent.model_validate({"id": "a1", "type": "hermes", "host": "h1",
                                             "reach": {"endpoint": "http://100.64.0.9:9900", "network": "tailnet"}}))
    client = session_for(TOM)
    assert client.get("/mesh/a1").status_code in (401, 403)
    assert client.get("/mesh/a1", headers={"Authorization": "Bearer wrong"}).status_code in (401, 403)
    assert client.post("/a2a/a1", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage"}).status_code in (401, 403)


def test_an_agent_token_reaches_workflows_only_with_board_access(secured, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    store.upsert_host(make_host("h1"))
    for id_, board in (("with-board", True), ("no-board", False)):
        store.upsert_agent(Agent.model_validate({
            "id": id_, "type": "hermes", "host": "h1",
            "reach": {"endpoint": "http://100.64.0.9:9900", "network": "tailnet", "board": board}}))
    ok = {"Authorization": f"Bearer {mesh.token('with-board', mesh.BEACON_PEER)}"}
    no = {"Authorization": f"Bearer {mesh.token('no-board', mesh.BEACON_PEER)}"}
    assert anonymous().get("/api/workflows", headers=ok).status_code == 200
    assert anonymous().get("/api/workflows", headers=no).status_code == 403
    # An agent token is no credential for anything else.
    assert anonymous().get("/api/hosts", headers=ok).status_code == 401


# --- CSRF --------------------------------------------------------------------

def test_a_browser_mutation_needs_a_same_origin_header(secured):
    client = session_for(TOM)
    body = {"id": "h9", "address": "10.0.0.9", "ssh": {"user": "u", "key": "/k"}}
    assert client.put("/api/hosts/h9", json=body).status_code == 403  # no Origin/Referer
    assert client.put("/api/hosts/h9", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.put("/api/hosts/h9", json=body, headers={"Origin": HTTPS}).status_code == 200


def test_the_mcp_token_needs_no_origin_header(secured):
    body = {"id": "h9", "address": "10.0.0.9", "ssh": {"user": "u", "key": "/k"}}
    assert anonymous().put("/api/hosts/h9", json=body, headers={"Authorization": "Bearer mcp-secret"}).status_code == 200


# --- login and callback ------------------------------------------------------

class _Token:
    def __init__(self, ok=True):
        self.is_success, self.text = ok, "idp internals: secret-detail"

    def json(self):
        return {"id_token": "jwt"}


def _provider(monkeypatch, claims, token_ok=True):
    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: _Token(token_ok))
    monkeypatch.setattr(auth, "_jwks", lambda: type("J", (), {
        "get_signing_key_from_jwt": staticmethod(lambda t: type("K", (), {"key": "k"})())})())
    monkeypatch.setattr(auth.jwt, "decode", lambda *a, **k: claims)


def _start_login(client):
    redirect = client.get("/auth/login")
    assert redirect.status_code == 307
    query = parse_qs(urlparse(redirect.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == ["client-1"]
    return query["state"][0]


def test_login_redirects_to_the_provider_with_pkce_and_a_state(secured):
    client = anonymous()
    state = _start_login(client)
    assert len(state) >= 24


def test_callback_signs_an_allowed_person_in(secured, monkeypatch):
    _provider(monkeypatch, {"sub": "s1", "email": TOM, "name": "Tom"})
    client = anonymous()
    state = _start_login(client)
    done = client.get("/auth/callback", params={"code": "c", "state": state})
    assert done.status_code == 307 and done.headers["location"] == "/"
    me = client.get("/auth/me").json()
    assert me["email"] == TOM and me["role"] == "operator"


def test_callback_refuses_a_wrong_missing_or_replayed_state(secured, monkeypatch):
    _provider(monkeypatch, {"sub": "s1", "email": TOM})
    client = anonymous()
    assert client.get("/auth/callback", params={"code": "c", "state": "x"}).status_code == 400  # none was started
    state = _start_login(client)
    assert client.get("/auth/callback", params={"code": "c", "state": "wrong"}).status_code == 400
    # The pending login was spent by the failed try: the right state no longer works either.
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400
    assert client.get("/auth/me").status_code == 401


def test_callback_passes_on_a_provider_error_without_signing_in(secured):
    assert anonymous().get("/auth/callback", params={"error": "access_denied"}).status_code == 400


def test_callback_hides_the_providers_token_error_from_the_browser(secured, monkeypatch):
    _provider(monkeypatch, {}, token_ok=False)
    client = anonymous()
    state = _start_login(client)
    refused = client.get("/auth/callback", params={"code": "c", "state": state})
    assert refused.status_code == 400
    assert "secret-detail" not in refused.text


@pytest.mark.parametrize("claims", [
    {"sub": "s2", "email": STRANGER, "email_verified": True},  # nobody named them
    {"sub": "s3"},                                              # no email claim at all
])
def test_callback_refuses_an_account_this_instance_does_not_admit(secured, monkeypatch, claims):
    _provider(monkeypatch, claims)
    client = anonymous()
    state = _start_login(client)
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 403
    assert client.get("/auth/me").status_code == 401


def test_logout_ends_the_session(secured):
    client = session_for(TOM)
    assert client.get("/auth/me").status_code == 200
    assert client.post("/auth/logout", headers={"Origin": HTTPS}).status_code == 303
    assert client.get("/auth/me").status_code == 401
