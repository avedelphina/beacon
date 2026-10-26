"""Board assignment durability and mailbox delivery receipts.

Assigning a card is a card mutation (SQLite, committed before return);
notifying the assignee is a separate mailbox delivery that may be slow,
offline, or fail. These tests go through the board/mailbox seam (board.py
calling into mailbox.py, with the A2A network faked the way
tests/test_mailbox.py does) to check the two are tracked independently and
that retries never duplicate a notice.
"""

import httpx
import pytest

from backend import bus, board, mailbox, mesh, opstore, store
from backend.schemas import Agent, Host

PUBLIC = "http://beacon.test:8642"


class FakeNet:
    def __init__(self):
        self.down: set[str] = set()
        self.seen: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        agent = request.url.host.split(".")[0]
        if agent in self.down:
            raise httpx.ConnectError("connection refused")
        self.seen.append(agent)
        body = request.content
        import json
        req_id = json.loads(body)["id"]
        task = {"id": "t-1", "contextId": "ctx-x",
                "status": {"state": "TASK_STATE_COMPLETED",
                           "message": {"role": "ROLE_AGENT", "parts": [{"text": f"{agent} got it"}]}}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {"task": task}})


@pytest.fixture
def net(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, PUBLIC)
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(Agent.model_validate({"id": "bravo", "type": "hermes", "host": "h1",
        "reach": {"endpoint": "http://bravo.test:9900", "network": "tailnet"}}))
    fake = FakeNet()
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(bus.httpx, "Client", client)
    return fake


def _notice_event(card):
    events = [e for e in card["events"] if e["kind"] == "assign_notice"]
    assert len(events) == 1
    return events[0]


def test_assignment_commits_before_delivery_is_attempted(net, monkeypatch):
    """The card row and its assign event are durable (already committed in
    their own opstore transaction) before mailbox delivery is even enqueued,
    so a slow or hanging delivery can never block or lose the assignment."""
    order = []
    real_enqueue = mailbox.enqueue

    def tracking_enqueue(*a, **kw):
        order.append("enqueue")
        return real_enqueue(*a, **kw)

    monkeypatch.setattr(mailbox, "enqueue", tracking_enqueue)
    monkeypatch.setattr(mailbox, "deliver_pending", lambda recipient: order.append("deliver"))

    card = board.create_card("beacon", "ship it", assignee="bravo")

    # The returned card already reflects the assignment (durable mutation);
    # delivery is enqueued after, and actual delivery after that.
    assert card["assignee"] == "bravo"
    assert order == ["enqueue", "deliver"]
    notice = _notice_event(card)
    assert notice["notice"]["state"] == "queued"


def test_delivered_notice_is_reflected_on_the_card(net):
    # The autouse fixture in conftest runs the delivery thread inline, so by
    # the time create_card returns, delivery has already been attempted.
    card = board.create_card("beacon", "ship it", assignee="bravo")

    assert net.seen == ["bravo"]
    notice = _notice_event(card)["notice"]
    assert notice["state"] == "delivered"
    assert notice["task_id"].startswith("mbx-")

    refreshed = board.get_card(card["id"])
    assert _notice_event(refreshed)["notice"]["state"] == "delivered"


def test_offline_assignee_keeps_the_notice_queued_until_the_next_pull(net):
    net.down.add("bravo")

    card = board.create_card("beacon", "ship it", assignee="bravo")

    notice = _notice_event(board.get_card(card["id"]))["notice"]
    assert notice["state"] == "queued"
    assert net.seen == []  # never reached bravo

    net.down.discard("bravo")
    mailbox.deliver_pending("bravo")

    notice = _notice_event(board.get_card(card["id"]))["notice"]
    assert notice["state"] == "delivered"
    assert net.seen == ["bravo"]


def test_failed_delivery_is_distinguishable_from_assignment_success(net, monkeypatch):
    """A refusal (bus.Unreachable, transient=False) marks the notice failed
    without touching the card or its assign event — the API must be able to
    tell "card assignment succeeded" apart from "notice delivery failed"."""
    def refuse(agent, message, context_id=None, **kwargs):
        raise bus.Unreachable("agent refused: untrusted peer", transient=False)

    monkeypatch.setattr(bus, "call", refuse)

    card = board.create_card("beacon", "ship it", assignee="bravo")

    assert card["assignee"] == "bravo"  # the assignment itself is untouched
    notice = _notice_event(board.get_card(card["id"]))["notice"]
    assert notice["state"] == "failed"
    assert "refused" in notice["last_error"]


def test_a_notice_without_an_answer_in_time_counts_as_delivered_and_is_not_resent(net, monkeypatch):
    """A read timeout means the agent has the notice and is busy with it. A
    notice needs no answer, so it is delivered; sending it again would hand
    the agent the same assignment twice."""
    def timeout(agent, message, context_id=None, **kwargs):
        raise bus.Unreachable("no answer: read timeout", uncertain=True)

    monkeypatch.setattr(bus, "call", timeout)
    card = board.create_card("beacon", "ship it", assignee="bravo")
    mailbox.deliver_pending("bravo")

    notice = _notice_event(board.get_card(card["id"]))["notice"]
    assert notice["state"] == "delivered" and notice["attempts"] == 1
    assert "read timeout" in notice["last_error"]
    assert mailbox.task_view(notice["task_id"])["status"]["state"] == "TASK_STATE_COMPLETED"


def test_transient_failure_leaves_the_notice_queued_for_retry(net):
    """A transient failure (for example, a connect timeout) must not be marked
    failed: retrying later could still deliver it, so it goes back to queued."""
    net.down.add("bravo")
    card = board.create_card("beacon", "ship it", assignee="bravo")
    notice_id = _notice_event(board.get_card(card["id"]))["notice_id"]

    with opstore.connect() as db:
        assert db.execute("SELECT attempts FROM mailbox WHERE id = ?", (notice_id,)).fetchone()["attempts"] == 1

    mailbox.deliver_pending("bravo")  # still down: another transient attempt
    notice = _notice_event(board.get_card(card["id"]))["notice"]
    assert notice["state"] == "queued"
    assert notice["attempts"] == 2


def test_reassigning_without_changing_assignee_does_not_duplicate_the_notice(net):
    """A retried update with the same assignee and no other change makes no
    new card change, so it must not enqueue a second notice."""
    card = board.create_card("beacon", "ship it", assignee="bravo")
    first_notice_id = _notice_event(card)["notice_id"]

    again = board.update_card("beacon", card["id"], assignee="bravo")
    notices = [e for e in again["events"] if e["kind"] == "assign_notice"]
    assert len(notices) == 1 and notices[0]["notice_id"] == first_notice_id
    assert net.seen.count("bravo") == 1


def test_reassigning_to_a_new_agent_sends_a_second_distinct_notice(net):
    store.upsert_agent(Agent.model_validate({"id": "charlie", "type": "hermes", "host": "h1",
        "reach": {"endpoint": "http://charlie.test:9900", "network": "tailnet"}}))

    card = board.create_card("beacon", "ship it", assignee="bravo")
    first_notice_id = _notice_event(card)["notice_id"]

    reassigned = board.update_card("beacon", card["id"], assignee="charlie")
    notices = [e for e in reassigned["events"] if e["kind"] == "assign_notice"]
    assert len(notices) == 2
    assert notices[0]["notice_id"] == first_notice_id
    assert notices[1]["notice_id"] != first_notice_id
    assert notices[1]["notice"]["state"] == "delivered"
    assert net.seen == ["bravo", "charlie"]
