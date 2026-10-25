"""Beacon mailbox: relay when the target answers, voicemail when it doesn't.

The A2A network is faked with httpx.MockTransport: each agent is either up
(answers with a completed task echoing what it got) or down (connection
refused). Every delivery is recorded with the token it carried, so the tests
can check identity is preserved end to end. Delivery is triggered the real
way — the recipient's host pulling GET /mesh/{agent} — and TestClient runs
the background task before returning.
"""

import json
import time

import httpx
import pytest

from backend import bus, mailbox, mesh, opstore, store
from backend.schemas import Agent, Host

PUBLIC = "http://beacon.test:8642"


class FakeNet:
    def __init__(self):
        self.down: set[str] = set()
        self.refuse: set[str] = set()
        self.seen: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        agent = request.url.host.split(".")[0]
        if agent in self.down:
            raise httpx.ConnectError("connection refused")
        if agent in self.refuse:
            return httpx.Response(403, json={"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32052, "message": "peer not trusted"}})
        body = json.loads(request.content)
        text = body["params"]["message"]["parts"][0]["text"]
        self.seen.append({"to": agent, "auth": request.headers["authorization"], "text": text,
                          "context": body["params"]["message"].get("contextId")})
        task = {"id": f"t-{len(self.seen)}", "contextId": "ctx-x",
                "status": {"state": "TASK_STATE_COMPLETED",
                           "message": {"role": "ROLE_AGENT", "parts": [{"text": f"{agent} got it"}]}}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"task": task}})


@pytest.fixture
def net(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, PUBLIC)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent_id in ("alpha", "bravo", "charlie"):
        store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": "h1",
            "reach": {"endpoint": f"http://{agent_id}.test:9900", "network": "tailnet"}}))
    fake = FakeNet()
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(bus.httpx, "Client", client)
    return fake


def _send(client, caller, target, text, token=None):
    body = {"jsonrpc": "2.0", "id": "r1", "method": "SendMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": text}], "messageId": "m1",
                                   "contextId": "ctx-1"}}}
    return client.post(f"/a2a/{target}", json=body,
                       headers={"Authorization": f"Bearer {token or mesh.token(caller, target)}"})


def _pull(client, agent_id):
    return client.get(f"/mesh/{agent_id}", headers={"Authorization": f"Bearer {mesh.token(agent_id, 'beacon')}"})


def _rows():
    with opstore.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM mailbox ORDER BY id")]


def test_online_target_is_relayed_with_the_callers_own_token(client, net):
    response = _send(client, "alpha", "bravo", "ping")

    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["message"]["parts"][0]["text"] == "bravo got it"
    assert net.seen == [{"to": "bravo", "auth": f"Bearer {mesh.token('alpha', 'bravo')}", "text": "ping",
                         "context": "ctx-1"}]
    assert _rows() == []  # relayed, nothing kept


def test_offline_target_gets_voicemail_and_the_reply_comes_back(client, net):
    net.down.add("bravo")

    task = _send(client, "alpha", "bravo", "are you there?").json()["result"]["task"]

    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert "holding the message" in task["status"]["message"]["parts"][0]["text"]
    assert [r["state"] for r in _rows()] == ["queued"]

    net.down.discard("bravo")
    assert _pull(client, "bravo").status_code == 200  # bravo's host checks in

    delivered, reply = net.seen
    assert delivered["to"] == "bravo" and delivered["auth"] == f"Bearer {mesh.token('alpha', 'bravo')}"
    assert "Delivered by the Beacon mailbox: alpha sent this" in delivered["text"]
    assert delivered["text"].endswith("are you there?") and delivered["context"] == "ctx-1"
    # The reply reaches alpha as a new message about bravo, delivered by Beacon:
    # the policy is one-way, so alpha need not trust bravo to hear its answer.
    assert reply["to"] == "alpha" and reply["auth"] == f"Bearer {mesh.token('beacon', 'alpha')}"
    assert "Reply from bravo via the Beacon mailbox" in reply["text"] and reply["text"].endswith("bravo got it")
    assert [(r["kind"], r["state"]) for r in _rows()] == [("message", "delivered"), ("reply", "delivered")]


def test_reply_waits_if_the_original_sender_went_offline(client, net):
    net.down.update({"bravo"})
    _send(client, "alpha", "bravo", "hello")
    net.down = {"alpha"}

    _pull(client, "bravo")
    assert [(r["kind"], r["state"]) for r in _rows()] == [("message", "delivered"), ("reply", "queued")]

    net.down = set()
    _pull(client, "alpha")
    assert [(r["kind"], r["state"]) for r in _rows()] == [("message", "delivered"), ("reply", "delivered")]


def test_a_refusal_is_not_queued(client, net):
    net.refuse.add("bravo")

    response = _send(client, "alpha", "bravo", "hello")

    assert response.status_code == 502
    assert "not trusted" in response.json()["error"]["message"]
    assert _rows() == []


def test_unknown_or_impersonating_callers_are_refused(client, net):
    assert _send(client, "alpha", "bravo", "x", token="wrong").status_code == 401
    # Beacon's own token for bravo must not let an agent pose as Beacon.
    assert _send(client, "alpha", "bravo", "x", token=mesh.token("beacon", "bravo")).status_code == 401
    # A token for another target doesn't work here either.
    assert _send(client, "alpha", "bravo", "x", token=mesh.token("alpha", "charlie")).status_code == 401


def test_expired_voicemail_tells_the_sender(client, net, monkeypatch):
    net.down.add("bravo")
    _send(client, "alpha", "bravo", "too late")
    with opstore.connect() as db:
        db.execute("UPDATE mailbox SET expires_at = ?", (time.time() - 1,))

    _pull(client, "alpha")

    kinds = [(r["kind"], r["state"], r["recipient"]) for r in _rows()]
    assert kinds == [("message", "expired", "bravo"), ("notice", "delivered", "alpha")]
    assert "expired undelivered" in net.seen[-1]["text"] and net.seen[-1]["to"] == "alpha"


def test_mcp_caller_leaves_voicemail_and_reads_the_reply(client, net):
    net.down.add("bravo")

    queued = client.post("/api/agents/bravo/call", json={"message": "status?", "queue_if_offline": True}).json()
    assert queued["state"] == "TASK_STATE_SUBMITTED" and queued["queued"]
    assert client.post("/api/agents/bravo/call", json={"message": "status?"}).status_code == 502  # opt-in only

    net.down.discard("bravo")
    _pull(client, "bravo")

    inbox = [r for r in client.get("/api/mailbox").json() if r["recipient"] == "beacon"]
    assert inbox[0]["kind"] == "reply" and inbox[0]["body"] == "bravo got it" and inbox[0]["state"] == "delivered"
    assert net.seen[0]["auth"] == f"Bearer {mesh.token('beacon', 'bravo')}"


def test_mailbox_task_can_be_polled_and_subscribed(client, net):
    net.down.add("bravo")
    response = _send(client, "alpha", "bravo", "status?")
    task_id = response.json()["result"]["task"]["id"]

    get_body = {"jsonrpc": "2.0", "id": "get-1", "method": "GetTask",
                "params": {"taskId": task_id}}
    get_response = client.post("/a2a/bravo", json=get_body,
                               headers={"Authorization": f"Bearer {mesh.token('alpha', 'bravo')}"})
    assert get_response.status_code == 200
    assert get_response.json()["result"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    rest_task = client.get(f"/api/mailbox/tasks/{task_id}")
    assert rest_task.status_code == 200 and rest_task.json()["id"] == task_id
    assert rest_task.json()["mailbox"]["state"] == "queued"

    subscribe_body = {"jsonrpc": "2.0", "id": "sub-1", "method": "tasks/subscribe",
                      "params": {"taskId": task_id}}
    subscribe = client.post("/a2a/bravo", json=subscribe_body,
                            headers={"Authorization": f"Bearer {mesh.token('alpha', 'bravo')}"})
    assert subscribe.status_code == 200 and "text/event-stream" in subscribe.headers["content-type"]
    assert "TASK_STATE_SUBMITTED" in subscribe.text and ": done" in subscribe.text

    # The original sender can poll; a different target route cannot use the id.
    wrong_target = client.post("/a2a/charlie", json=get_body,
                               headers={"Authorization": f"Bearer {mesh.token('alpha', 'charlie')}"})
    assert wrong_target.json()["error"]["code"] == -32001

    net.down.discard("bravo")
    _pull(client, "bravo")
    completed = client.post("/a2a/bravo", json=get_body,
                            headers={"Authorization": f"Bearer {mesh.token('alpha', 'bravo')}"})
    assert completed.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert completed.json()["result"]["status"]["message"]["parts"][0]["text"] == "bravo got it"


def test_a_claimed_message_is_delivered_once(client, net):
    net.down.add("bravo")
    _send(client, "alpha", "bravo", "once")
    net.down.discard("bravo")

    _pull(client, "bravo")
    _pull(client, "bravo")

    assert [s["to"] for s in net.seen].count("bravo") == 1


def test_mailbox_card_points_back_at_the_mailbox(client, net):
    card = client.get("/a2a/bravo/.well-known/agent-card.json").json()
    assert card["supportedInterfaces"][0]["url"] == f"{PUBLIC}/a2a/bravo"
    assert card["url"] == f"{PUBLIC}/a2a/bravo"


def test_mesh_config_offers_a_mailbox_entry_per_peer(net):
    cfg = mesh.config("alpha")
    names = [p["name"] for p in cfg["outbound"]]
    assert names == ["bravo", "bravo-mailbox", "charlie", "charlie-mailbox"]
    mailbox_entry = cfg["outbound"][1]
    assert mailbox_entry["url"] == f"{PUBLIC}/a2a/bravo"
    assert mailbox_entry["token"] == mesh.token("alpha", "bravo")
    assert cfg["beacon"] == PUBLIC


def test_transient_classification():
    agent = Agent.model_validate({"id": "x", "type": "hermes", "host": "h1",
                                  "reach": {"endpoint": "http://x.test:9900"}})

    def call_with(handler):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            try:
                bus.call(agent, "hi", token="t", client=c)
            except bus.Unreachable as exc:
                return exc.transient

    def refused(request):
        raise httpx.ConnectError("refused")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    assert call_with(refused) is True
    assert call_with(lambda r: httpx.Response(503)) is True
    assert call_with(slow) is False  # it may already be working on it
    assert call_with(lambda r: httpx.Response(403, json={"error": {"code": 1, "message": "no"}})) is False
