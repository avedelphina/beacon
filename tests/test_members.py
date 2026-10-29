"""Members: people who own agents see and do a little with them, and nothing
else. The server is what refuses (backend/members.py); the page only hides."""

import base64
import json

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import auth, board, mesh, store, workflow
from backend.app import app
from backend.schemas import Agent, Host
from backend.ssh import SSHResult
from tests.conftest import make_host

ANN, BOB, TOM = "ann@example.com", "bob@example.com", "tom@example.com"


def as_person(email):
    payload = base64.b64encode(json.dumps({"user": {"email": email}}).encode()).decode()
    client = TestClient(app)
    client.cookies.set("session", TimestampSigner(auth.SESSION_SECRET).sign(payload.encode()).decode())
    client.headers["Origin"] = "http://testserver"
    return client


@pytest.fixture
def people(fleet, fake_ssh, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM}))
    monkeypatch.setattr(auth, "MEMBER_EMAILS", frozenset({ANN, BOB}))
    store.upsert_host(make_host("h1"))
    reach = {"endpoint": "http://100.64.0.9:9900", "network": "tailnet", "board": True}
    for id_, owner, logs in (("a1", ANN, False), ("a2", "Ann@Example.com", True), ("b1", BOB, False), ("n1", None, False)):
        store.upsert_agent(Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "owner": owner, "member_logs": logs,
                                                 "desired": {"config": {"secret-ish": 1}}, "reach": reach}))
    return {"ann": as_person(ANN), "bob": as_person(BOB), "tom": as_person(TOM)}


def test_a_member_sees_their_agents_and_no_more(people):
    listed = people["ann"].get("/api/agents").json()
    assert [a["id"] for a in listed] == ["a1", "a2"]  # an email matches whatever its case
    assert "desired" not in listed[0] and "endpoint" not in json.dumps(listed[0]) and listed[0]["reach"] == {"network": "tailnet", "board": True}
    assert [a["id"] for a in people["tom"].get("/api/agents").json()] == ["a1", "a2", "b1", "n1"]  # an operator sees all, in full
    assert "desired" in people["tom"].get("/api/agents/a1").json()
    assert "desired" not in people["ann"].get("/api/agents/a1").json()


def test_another_persons_agent_does_not_exist_for_a_member(people):
    for path, method in (("", "get"), ("/status", "get"), ("/restart?confirm=true", "post"), ("/logs", "get")):
        assert getattr(people["ann"], method)(f"/api/agents/b1{path}").status_code == 404
        assert getattr(people["ann"], method)(f"/api/agents/nothing{path}").status_code == 404  # same answer as a stranger's
    assert getattr(people["ann"], "get")("/api/agents/n1").status_code == 404  # no owner at all


def test_a_member_sees_the_state_of_the_service_not_the_hosts_error_text(people, fake_ssh):
    fake_ssh.result = SSHResult(ok=False, stdout="", stderr="ssh: connect to host 10.0.0.1 port 22: refused; key /home/x/id", returncode=255)
    seen = people["ann"].get("/api/agents/a1/status").json()
    assert "state" in seen and "10.0.0.1" not in json.dumps(seen) and "detail" not in seen
    assert "10.0.0.1" in json.dumps(people["tom"].get("/api/agents/a1/status").json())


def test_a_member_can_restart_their_agent_after_the_usual_confirm(people, fake_ssh):
    asked = people["ann"].post("/api/agents/a1/restart").json()
    assert asked["would_run"] is True and fake_ssh.calls == []
    fake_ssh.result = SSHResult(ok=True, stdout="restarted", stderr="", returncode=0)
    assert people["ann"].post("/api/agents/a1/restart?confirm=true").status_code == 200
    assert "gateway restart" in fake_ssh.last_command


def test_logs_are_open_to_the_owner_only_where_an_operator_said_so(people, fake_ssh):
    fake_ssh.result = SSHResult(ok=True, stdout="log line", stderr="", returncode=0)
    assert people["ann"].get("/api/agents/a1/logs").status_code == 403
    assert people["ann"].get("/api/agents/a2/logs").json() == {"text": "log line"}


def test_everything_else_is_closed_to_a_member_even_routes_added_later(people):
    """Walk every route Beacon has; each /api route outside the member list must answer a member
    with a refusal, not a result."""
    from backend import members

    seen = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        concrete = path.replace("{id_}", "a1").replace("{card_id}", "1").replace("{request_id}", "1").replace("{plugin}", "p") \
                       .replace("{name}", "t").replace("{code}", "c")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if any(m == method and pattern.fullmatch(concrete) for m, pattern, _ in members._ROUTES):
                continue
            seen += 1
            response = people["ann"].request(method, concrete, json={})
            assert response.status_code == 403, (method, path, response.status_code)
    assert seen > 30  # the sweep really covered the API


def test_the_member_list_holds_only_what_the_design_names():
    from backend import members

    assert sorted((m, p.pattern) for m, p, _ in members._ROUTES if "agents" in p.pattern) == [
        ("GET", "/api/agents"), ("GET", "/api/agents/(?P<id>[^/]+)"), ("GET", "/api/agents/(?P<id>[^/]+)/logs"),
        ("GET", "/api/agents/(?P<id>[^/]+)/status"), ("POST", "/api/agents/(?P<id>[^/]+)/restart"),
    ]


def test_availability_shows_a_member_their_agents_without_the_endpoint(people):
    seen = people["ann"].get("/api/availability").json()
    assert sorted(seen) == ["a1", "a2"] and all(v["detail"] == "" for v in seen.values())
    assert "100.64.0.9" not in json.dumps(seen)


def test_a_member_works_the_board_for_their_agents(people):
    ann, bob = people["ann"], people["bob"]
    card = ann.post("/api/board/cards", json={"title": "Check backups", "assignee": "a1"}).json()
    assert card["created_by"] == ANN
    assert ann.post(f"/api/board/cards/{card['id']}/comments", json={"text": "Skip the NAS."}).status_code == 200
    assert ann.patch(f"/api/board/cards/{card['id']}", json={"state": "doing"}).status_code == 200

    assert [c["id"] for c in ann.get("/api/board").json()] == [card["id"]]
    assert bob.get("/api/board").json() == []                                        # not Bob's
    assert bob.get(f"/api/board/cards/{card['id']}").status_code == 404
    assert bob.post(f"/api/board/cards/{card['id']}/comments", json={"text": "hi"}).status_code == 404

    other = board.create_card("beacon", "Not Ann's", assignee="b1")
    assert ann.get(f"/api/board/cards/{other['id']}").status_code == 404
    assert ann.post("/api/board/cards", json={"title": "x", "assignee": "b1"}).status_code == 403   # not their agent
    assert ann.patch(f"/api/board/cards/{card['id']}", json={"assignee": "b1"}).status_code == 403
    assert ann.post("/api/board/cards", json={"title": "mine only"}).status_code == 200


def _ask(requester, eligible="human", **target):
    card = board.create_card("beacon", "Ask")["id"]
    binding = workflow.plan_binding({"ask": requester, "t": target})
    return workflow.create(card_id=card, kind="decision", requester=requester, eligible_responder=eligible,
                           capability="ask", tier=1, title="Which?", summary="This or that?", options=["a", "b"],
                           target=target, **binding)


def test_an_owner_answers_what_their_agent_asks_a_person(people):
    request = _ask("a1")
    answer = {"action": "choose_option", "response": {"option": "a"},
              "plan_revision": request["plan_revision"], "plan_hash": request["plan_hash"]}
    assert [r["id"] for r in people["ann"].get("/api/workflows").json()] == [request["id"]]
    assert people["bob"].get("/api/workflows").json() == []
    assert people["bob"].get(f"/api/workflows/{request['id']}").status_code == 404
    assert people["bob"].post(f"/api/workflows/{request['id']}/respond", json=answer).status_code == 404
    done = people["ann"].post(f"/api/workflows/{request['id']}/respond", json=answer)
    assert done.status_code == 200 and done.json()["state"] == "approved"


def test_a_member_sees_what_an_approval_is_about_but_not_the_hosts_settings(people):
    asked = _ask("beacon", agent_id="a1", host={"address": "10.0.0.1", "ssh": {"key": "/home/x/.ssh/id"}})
    shown = people["ann"].get(f"/api/workflows/{asked['id']}").json()
    assert shown["target"] == {"agent": "a1"} and "10.0.0.1" not in json.dumps(shown)
    assert people["ann"].get("/api/workflows").json()[0]["target"] == {"agent": "a1"}
    assert people["tom"].get(f"/api/workflows/{asked['id']}").json()["target"]["host"]["address"] == "10.0.0.1"  # the operator still does
    assert _ask("a1", eligible="b1")["id"] not in [r["id"] for r in people["ann"].get("/api/workflows").json()]  # asked of an agent, not a person


def test_taking_someone_off_the_list_ends_their_session(people, monkeypatch):
    assert people["ann"].get("/api/agents").status_code == 200
    monkeypatch.setattr(auth, "MEMBER_EMAILS", frozenset({BOB}))
    assert people["ann"].get("/api/agents").status_code == 401
    assert as_person("stranger@example.com").get("/api/agents").status_code == 401


def test_the_page_learns_the_role_and_the_operator_stays_one(people, monkeypatch):
    assert people["ann"].get("/auth/me").json()["role"] == "member"
    assert people["tom"].get("/auth/me").json()["role"] == "operator"
    monkeypatch.setattr(auth, "MEMBER_EMAILS", frozenset({ANN, TOM}))  # in both lists: an operator
    assert people["tom"].get("/auth/me").json()["role"] == "operator"
    assert people["ann"].get("/").status_code == 200 and people["ann"].get("/app.js").status_code == 200  # the page itself


def test_making_someone_an_owner_is_an_access_change_for_a_person_only(people, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(auth, "MCP_TOKEN", "test-mcp-token")
    mcp = TestClient(app, headers={"Authorization": "Bearer test-mcp-token"})
    record = store.get_agent("n1").model_dump()
    assert mcp.put("/api/agents/n1", json=record | {"owner": ANN}).status_code == 403
    assert mcp.put("/api/agents/n1", json=record | {"member_logs": True}).status_code == 403
    assert mcp.put("/api/agents/n1", json=record | {"notes": "fine"}).status_code == 200
