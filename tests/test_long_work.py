"""Work that takes longer than a short exchange.

A Hermes caller gives up after 120 s and a Hermes agent used to drop answers
after 300 s, so long work never came back. The mailbox hands slow answers off:
the caller gets a durable mbx-N task, the agent POSTs the finished task to
Beacon (A2A push notification), and the answer reaches the caller as a new
message. These tests fake Hermes on both ends, including its habit of pushing
*before* it writes the HTTP response.
"""

import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend import board, bus, mailbox, mesh, opstore, store
from backend.schemas import Agent, Host

PUBLIC = "http://beacon.test:8642"


class FakeNet:
    """bravo is the worker. `slow` makes it time out (still working); `answer`
    is what it says; `push_first` pushes the answer before responding, as
    Hermes does. Every SendMessage is recorded with its push URL."""

    def __init__(self):
        self.slow = False
        self.push_first = None  # a TestClient to push through, or None
        self.answer = "bravo: done"
        self.sent = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        agent = request.url.host.split(".")[0]
        body = json.loads(request.content)
        if body["method"] == "GetTask":
            return self._task(body["id"], body["params"]["id"], self.answer)
        params = body["params"]
        push = ((params.get("configuration") or {}).get("taskPushNotificationConfig") or {}).get("url")
        self.sent.append({"to": agent, "text": params["message"]["parts"][0]["text"], "push": push,
                          "auth": request.headers["authorization"]})
        if agent == "bravo" and self.push_first is not None:
            push_answer(self.push_first, push, self.answer)
        if agent == "bravo" and self.slow:
            raise httpx.ReadTimeout("no answer yet")
        return self._task(body["id"], "task-1", self.answer if agent == "bravo" else f"{agent} read it")

    @staticmethod
    def _task(req_id, task_id, text):
        task = {"id": task_id, "contextId": "ctx-x", "status": {"state": "TASK_STATE_COMPLETED",
                "message": {"role": "ROLE_AGENT", "parts": [{"text": text}]}}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {"task": task}})


def push_answer(client, url, text, state="TASK_STATE_COMPLETED"):
    """What Hermes POSTs to a push URL: a v1.0 statusUpdate, text cut at 2000."""
    parts = urlsplit(url)
    payload = {"statusUpdate": {"taskId": "task-1", "contextId": "ctx-x", "status": {
        "state": state, "message": {"role": "ROLE_AGENT", "parts": [{"text": text[:2000]}]}}}}
    return client.post(f"{parts.path}?{parts.query}", json=payload)


@pytest.fixture
def net(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, PUBLIC)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent_id in ("alpha", "bravo"):
        store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": "h1",
            "reach": {"endpoint": f"http://{agent_id}.test:9900", "network": "tailnet"}}))
    fake = FakeNet()
    real_client = httpx.Client
    monkeypatch.setattr(bus.httpx, "Client",
                        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(fake.handler)}))
    return fake


def _ask(client, text="review the whole repo"):
    body = {"jsonrpc": "2.0", "id": "r1", "method": "SendMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": text}], "messageId": "m1"}}}
    return client.post("/a2a/bravo", json=body,
                       headers={"Authorization": f"Bearer {mesh.token('alpha', 'bravo')}"}).json()["result"]["task"]


def _rows():
    with opstore.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM mailbox ORDER BY id")]


def _to_alpha(net):
    return [s for s in net.sent if s["to"] == "alpha"]


def test_a_slow_answer_is_handed_off_and_comes_back_as_a_new_message(client, net):
    net.slow = True

    task = _ask(client)

    assert task["status"]["state"] == "TASK_STATE_WORKING" and task["id"].startswith("mbx-")
    assert "still working" in task["status"]["message"]["parts"][0]["text"]
    push = net.sent[0]["push"]
    assert push.startswith(f"{PUBLIC}/a2a/bravo/push/")  # bravo was told where to send the answer
    assert mailbox.task_view(task["id"])["status"]["state"] == "TASK_STATE_WORKING"

    assert push_answer(client, push, "bravo: the review is done").status_code == 200

    late, = _to_alpha(net)
    assert "Reply from bravo via the Beacon mailbox" in late["text"] and late["text"].endswith("the review is done")
    assert late["auth"] == f"Bearer {mesh.token('beacon', 'alpha')}"
    view = mailbox.task_view(task["id"])
    assert view["status"]["state"] == "TASK_STATE_COMPLETED"
    assert view["status"]["message"]["parts"][0]["text"] == "bravo: the review is done"


def test_a_quick_answer_still_comes_back_at_once_and_keeps_nothing(client, net):
    net.push_first = client  # Hermes pushes even when it answers in time

    task = _ask(client, "ping")

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["message"]["parts"][0]["text"] == "bravo: done"
    assert _rows() == [] and _to_alpha(net) == []  # no copy kept, no second answer later


def test_a_push_that_beats_the_timeout_is_answered_in_the_same_call(client, net):
    net.push_first, net.slow = client, True  # the answer was pushed, then the response was lost

    task = _ask(client)

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["message"]["parts"][0]["text"] == "bravo: done"
    assert _rows() == [] and _to_alpha(net) == []


def test_a_long_answer_cut_by_the_push_is_fetched_in_full(client, net):
    net.slow = True
    task = _ask(client)
    net.answer = "x" * 5000  # what bravo's GetTask has

    push_answer(client, net.sent[0]["push"], net.answer)

    assert mailbox.task_view(task["id"])["status"]["message"]["parts"][0]["text"] == "x" * 5000


def test_only_the_issued_push_url_is_accepted(client, net):
    net.slow = True
    task = _ask(client)
    url = urlsplit(net.sent[0]["push"])
    message_id = task["id"].removeprefix("mbx-")

    assert client.post(f"{url.path}?k=forged", json={}).status_code == 401
    # The key is bound to the agent and the message: alpha can't settle bravo's.
    other = f"/a2a/alpha/push/{message_id}?{url.query}"
    assert client.post(other, json={}).status_code == 401
    assert mailbox.task_view(task["id"])["status"]["state"] == "TASK_STATE_WORKING"
    key = parse_qs(url.query)["k"][0]
    assert key not in {mesh.token(a, b) for a in ("alpha", "bravo", "beacon") for b in ("alpha", "bravo")}


def test_a_held_message_delivered_by_the_sync_is_handed_off_too(client, net):
    message_id = mailbox.enqueue("alpha", "bravo", "message", "migrate the database")
    pulled = client.get("/mesh/bravo", headers={"Authorization": f"Bearer {mesh.token('bravo', 'beacon')}",
                                                "X-Beacon-Sync-Version": "2"}).json()
    item, = pulled["inbox"]
    assert item["push_url"].startswith(f"{PUBLIC}/a2a/bravo/push/{message_id}?k=")

    auth = {"Authorization": f"Bearer {mesh.token('bravo', 'beacon')}"}
    client.post(f"/mesh/bravo/inbox/{message_id}", headers=auth, json={"ok": False, "working": True, "error": "slow"})
    assert mailbox.task_view(f"mbx-{message_id}")["mailbox"]["state"] == "working"

    push_answer(client, item["push_url"], "bravo: migrated")

    assert _to_alpha(net)[0]["text"].endswith("bravo: migrated")
    # A sync report that turns up after the push is accepted, not a 404.
    assert client.post(f"/mesh/bravo/inbox/{message_id}", headers=auth,
                       json={"ok": True, "reply": "bravo: migrated"}).status_code == 200
    assert len(_to_alpha(net)) == 1


def test_a_handoff_with_no_answer_expires_and_tells_the_sender(client, net):
    net.slow = True
    task = _ask(client)
    with opstore.connect() as db:
        db.execute("UPDATE mailbox SET updated_at = ?", (time.time() - mailbox.WORK_DEADLINE_SECONDS - 1,))

    mailbox.deliver_pending("alpha")  # any pull runs the sweep

    view = mailbox.task_view(task["id"])
    assert view["status"]["state"] == "TASK_STATE_FAILED"
    assert "did not answer" in view["status"]["message"]["parts"][0]["text"]
    assert "did not answer" in _to_alpha(net)[0]["text"]
    push_answer(client, net.sent[0]["push"], "too late")  # a push after expiry changes nothing
    assert mailbox.task_view(task["id"])["status"]["state"] == "TASK_STATE_FAILED"


def test_without_a_public_url_a_timeout_is_final_not_working(client, net, monkeypatch):
    monkeypatch.delenv(mesh.PUBLIC_URL_ENV)
    net.slow = True
    message_id = mailbox.enqueue("alpha", "bravo", "message", "hello")

    mailbox.deliver_pending("bravo")

    assert net.sent[0]["push"] is None
    view = mailbox.task_view(f"mbx-{message_id}")
    assert view["mailbox"]["state"] == "uncertain" and view["status"]["state"] == "TASK_STATE_FAILED"
    mailbox.deliver_pending("bravo")
    assert len(net.sent) == 1  # never sent twice


def test_an_assignment_notice_task_id_can_be_looked_up(client, net):
    card = board.create_card("beacon", "ship it", assignee="bravo")
    notice = next(e for e in card["events"] if e["kind"] == "assign_notice")["notice"]

    view = client.get(f"/api/mailbox/tasks/{notice['task_id']}").json()

    assert view["status"]["state"] == "TASK_STATE_COMPLETED"
    assert view["status"]["message"]["parts"][0]["text"] == "Delivered to bravo."


def test_agents_get_the_long_reply_window(net):
    assert mesh.config("bravo")["listen"]["reply_timeout"] == mesh.REPLY_WINDOW_SECONDS >= 3600
    assert mailbox.HANDOFF_SECONDS < 120  # Hermes's a2a_call gives up at 120 s
