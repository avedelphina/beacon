"""Who wrote what: the mailbox's own records name the real author of every row, and the first
line of a delivered notice says who it is from, though Beacon's token carries it."""

import pytest

from backend import mailbox, mesh, store
from backend.schemas import Agent, Host


@pytest.fixture
def crew(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    monkeypatch.setattr(mailbox, "deliver_pending", lambda agent: None)  # no delivery attempts to made-up agents
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for id_ in ("alpha", "bravo"):
        store.upsert_agent(Agent.model_validate({"id": id_, "type": "hermes", "host": "h1",
                                                 "reach": {"endpoint": f"http://{id_}.test:9900"}}))


def test_a_notice_an_agent_caused_names_that_agent_and_beacons_own_does_not():
    from_agent = mailbox._framed({"kind": "notice", "sender": "alpha", "body": "You were assigned card #3."})
    from_beacon = mailbox._framed({"kind": "notice", "sender": mesh.BEACON_PEER, "body": "Reminder."})
    assert from_agent == "[Notice from alpha via the Beacon mailbox]\n\nYou were assigned card #3."
    assert from_beacon == "[Notice from the Beacon mailbox]\n\nReminder."


def test_a_notice_to_an_agent_caused_by_another_agent_is_stored_and_framed_with_that_agent(crew):
    from backend import board
    board.create_card("alpha", "Check backups", assignee="bravo")
    notice, = [r for r in mailbox.recent() if r["kind"] == "notice"]
    assert notice["sender"] == "alpha" and notice["recipient"] == "bravo"
    assert mailbox._framed(notice).startswith("[Notice from alpha via the Beacon mailbox]")


def test_recent_filters_by_the_real_sender_recipient_and_kind(crew):
    mailbox.enqueue("alpha", "bravo", "message", "are you there?")
    mailbox.enqueue("bravo", "alpha", "reply", "yes", reply_to=1)      # delivered under Beacon's token, written by bravo
    mailbox.enqueue(mesh.BEACON_PEER, "bravo", "notice", "reminder")

    assert [r["sender"] for r in mailbox.recent(sender="bravo")] == ["bravo"]
    assert [r["kind"] for r in mailbox.recent(recipient="bravo")] == ["notice", "message"]
    assert [r["body"] for r in mailbox.recent(kind="reply")] == ["yes"]
    assert [r["body"] for r in mailbox.recent(sender="bravo", kind="notice")] == []
    assert len(mailbox.recent()) == 3 and len(mailbox.recent(limit=1)) == 1


def test_the_api_filters_too(client, crew):
    mailbox.enqueue("alpha", "bravo", "message", "are you there?")
    mailbox.enqueue("bravo", "alpha", "reply", "yes", reply_to=1)
    assert [r["body"] for r in client.get("/api/mailbox?sender=bravo").json()] == ["yes"]
    assert [r["sender"] for r in client.get("/api/mailbox?recipient=alpha&kind=reply").json()] == ["bravo"]
    assert len(client.get("/api/mailbox").json()) == 2
