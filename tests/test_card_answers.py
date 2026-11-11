"""An agent's answer to a board notice is its acknowledgement: kept on the card,
shown on the board, and never mistaken for progress on the card itself."""

import pytest

from backend import board, mailbox, mesh, opstore, store
from backend.schemas import Agent, Host


@pytest.fixture
def crew(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, "http://beacon.test:8642")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent_id in ("alpha", "bravo"):
        store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": "h1",
            "reach": {"endpoint": f"http://{agent_id}.test:9900", "board": True}}))
    monkeypatch.setattr(mailbox, "deliver_pending", lambda agent: None)


def _answers(card_id):
    return [(e["author"], e["body"]) for e in board.get_card(card_id)["events"] if e["kind"] == "answer"]


def _delivery(card_id):
    return next(c for c in board.list_cards(include_done=True) if c["id"] == card_id)["delivery"]


def test_the_answer_on_the_agents_own_host_lands_on_the_card(crew):
    card = board.create_card("beacon", "Rotate keys", assignee="bravo")["id"]
    assert _delivery(card)["state"] == "queued" and _delivery(card)["to"] == "bravo"
    updated = board.get_card(card)["updated_at"]

    item, = mailbox.claim_for_pull("bravo")
    assert item["push_url"]                        # a late answer can still come back
    assert _delivery(card)["state"] == "delivering"
    mailbox.report("bravo", item["id"], ok=True, reply="On it; I will move it to doing.")

    assert _answers(card) == [("bravo", "On it; I will move it to doing.")]
    delivery = _delivery(card)
    assert delivery["state"] == "delivered" and delivery["answered_at"] is not None
    assert board.get_card(card)["updated_at"] == updated   # "will do" is not progress: reminders still count


def test_a_long_turn_shows_working_until_the_push(crew):
    card = board.create_card("beacon", "Rotate keys", assignee="bravo")["id"]
    item, = mailbox.claim_for_pull("bravo")
    mailbox.report("bravo", item["id"], ok=False, working=True, error="no answer in 60 s")
    assert _delivery(card)["state"] == "working" and _delivery(card)["answered_at"] is None
    mailbox.report("bravo", item["id"], ok=True, reply="Done.")      # the sync's late report, or the push
    mailbox.report("bravo", item["id"], ok=True, reply="Done again.")  # whichever comes second is ignored
    assert _answers(card) == [("bravo", "Done.")]


def test_no_text_is_no_answer_and_a_newer_notice_needs_its_own(crew):
    silent, card = (board.create_card("beacon", title, assignee="bravo")["id"] for title in ("Quiet", "Rotate keys"))
    first, second = mailbox.claim_for_pull("bravo")
    mailbox.report("bravo", first["id"], ok=True, reply="")
    mailbox.report("bravo", second["id"], ok=True, reply="Starting.")
    assert _answers(silent) == [] and _delivery(silent)["answered_at"] is None
    assert _delivery(card)["answered_at"] is not None
    board.comment("alpha", card, "Use the new vault.")  # a newer notice: the old answer does not cover it
    assert _delivery(card)["state"] == "queued" and _delivery(card)["answered_at"] is None


def test_one_reminder_answered_lands_on_every_card_it_named(crew):
    cards = [board.create_card("beacon", title, assignee="bravo")["id"] for title in ("One", "Two")]
    for item in mailbox.claim_for_pull("bravo"):
        mailbox.report("bravo", item["id"], ok=True, reply="ack")
    with opstore.connect() as db:
        db.execute("UPDATE cards SET updated_at = 0")
    board.remind("bravo", now=2 * 86400)
    item, = mailbox.claim_for_pull("bravo")
    mailbox.report("bravo", item["id"], ok=True, reply="Both are blocked on the vault; marked them.")
    for card in cards:
        assert _answers(card)[-1] == ("bravo", "Both are blocked on the vault; marked them.")


def test_a_notice_whose_answer_never_comes_still_counts_as_delivered(crew, monkeypatch):
    card = board.create_card("beacon", "Rotate keys", assignee="bravo")["id"]
    item, = mailbox.claim_for_pull("bravo")
    mailbox.report("bravo", item["id"], ok=False, working=True)
    with opstore.connect() as db:
        db.execute("UPDATE mailbox SET updated_at = 0 WHERE id = ?", (item["id"],))
    mailbox.claim_for_pull("alpha")                # any pull runs the expiry
    delivery = _delivery(card)
    assert delivery["state"] == "delivered" and "no answer came back" in delivery["error"]
    with opstore.connect() as db:                  # and nobody is sent an "expired" notice about it
        assert db.execute("SELECT count(*) FROM mailbox WHERE reply_to = ?", (item["id"],)).fetchone()[0] == 0


def test_an_answer_to_a_notice_about_no_card_is_simply_settled(crew):
    notice = mailbox.enqueue(mesh.BEACON_PEER, "bravo", "notice", "Your workflow request was approved.")
    item, = mailbox.claim_for_pull("bravo")
    assert item["id"] == notice
    mailbox.report("bravo", notice, ok=True, reply="Thanks.")
    assert mailbox.task_view(f"mbx-{notice}")["status"]["state"] == "TASK_STATE_COMPLETED"
