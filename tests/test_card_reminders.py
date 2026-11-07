"""Board reminders: a card nobody moves is brought up again with whoever has to
move it, on that agent's own pull, at most REMINDERS times; then people hear by
email, once until the card changes."""

import pytest

from backend import auth, board, mesh, notify, opstore, policy, store
from backend.schemas import Agent, Host

DAY = 86400


def _agent(id_, tags=(), board_on=True, owner=None):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "tags": list(tags), "owner": owner,
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": board_on}})


@pytest.fixture
def crew(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("alpha", owner="owner@example.com"), _agent("bravo"), _agent("nob", board_on=False),
                  _agent("slow", ["patient"])):
        store.upsert_agent(agent)
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n'
                'health:\n  - {for: ["tag:patient"], remind_after: 3d}\n  - {for: ["bravo"], remind_after: never}\n',
                "test")
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"op@example.com"}))
    emails = []
    monkeypatch.setattr(notify, "_email", lambda to, subject, body: emails.append((sorted(to), subject, body)))
    return emails


def _card(title, assignee=None, state="todo", author="beacon", at=0.0):
    card = board.create_card(author, title, assignee=assignee, state=state)
    with opstore.connect() as db:  # as if made `at`, and nothing happened since
        db.execute("UPDATE cards SET created_at = ?, updated_at = ? WHERE id = ?", (at, at, card["id"]))
        db.execute("DELETE FROM mailbox")
    return card["id"]


def _reminders(agent):
    with opstore.connect() as db:
        return [r["body"] for r in db.execute("SELECT body FROM mailbox WHERE recipient = ? AND body LIKE"
                                               " 'Reminder from the Beacon board%' ORDER BY id", (agent,))]


def _kinds(card_id):
    return [e["kind"] for e in board.get_card(card_id)["events"] if e["kind"] in ("reminder", "escalation")]


def test_a_stale_card_is_brought_up_twice_on_its_agents_pull_then_people_hear(crew):
    card = _card("Rotate keys", "alpha")
    board.remind("alpha", now=0.5 * DAY)           # not stale yet
    board.remind("bravo", now=1.1 * DAY)           # someone else's pull: not alpha's turn
    assert _reminders("alpha") == []

    board.remind("alpha", now=1.1 * DAY)
    first, = _reminders("alpha")
    assert f'#{card} "Rotate keys" (todo, no change for 1 d): assigned to you and not started' in first
    board.remind("alpha", now=1.5 * DAY)           # once per remind_after
    board.remind("alpha", now=2.2 * DAY)
    board.remind("alpha", now=3.0 * DAY)
    assert len(_reminders("alpha")) == 2 and crew == []

    board.remind("bravo", now=3.3 * DAY)           # a day after the last one: people, on any pull
    board.remind("alpha", now=9 * DAY)             # and only once
    assert len(_reminders("alpha")) == 2
    assert sorted(to for to, _, _ in crew) == [["op@example.com"], ["owner@example.com"]]  # operator, alpha's owner
    _, subject, body = crew[0]
    assert subject == "Beacon: 1 board card not moving" and "alpha was reminded and nothing changed" in body
    assert _kinds(card) == ["reminder", "reminder", "escalation"]


def test_any_change_starts_over(crew):
    card = _card("Rotate keys", "alpha")
    board.remind("alpha", now=1.1 * DAY)
    with opstore.connect() as db:
        db.execute("UPDATE cards SET updated_at = ? WHERE id = ?", (1.2 * DAY, card))  # e.g. a comment
    board.remind("alpha", now=2.15 * DAY)          # under a day since the change
    assert len(_reminders("alpha")) == 1
    board.remind("alpha", now=2.3 * DAY)
    assert len(_reminders("alpha")) == 2 and "(1 of 2)" in board.get_card(card)["events"][-1]["body"]


def test_one_notice_carries_all_of_an_agents_cards_and_says_what_each_needs(crew):
    todo = _card("Write report", "alpha")
    blocked = _card("Fix backup", "alpha", state="blocked")
    review = _card("Check my work", "bravo", state="review", author="alpha")   # alpha made it: alpha checks
    loose = _card("Someone do this", author="alpha")
    board.remind("alpha", now=2 * DAY)
    notice, = _reminders("alpha")
    for card_id in (todo, blocked, review, loose):
        assert f"#{card_id} " in notice
    assert "bravo finished it and it waits for your check" in notice
    assert "nobody is assigned" in notice and "make sure whoever you wait for knows" in notice
    assert _reminders("bravo") == []


def test_cards_only_a_person_can_move_go_to_people_at_once(crew):
    own_review = _card("Done, please check", "alpha", state="review", author="alpha")
    no_tools = _card("Needs nob", "nob")
    for_person = _card("Sign the form", "dana@example.com")
    board.remind("bravo", now=1.1 * DAY)
    assert _reminders("alpha") == [] and _reminders("nob") == []
    ops = next(body for to, _, body in crew if to == ["op@example.com"])
    assert all(f"#{c} " in ops for c in (own_review, no_tools, for_person))
    assert "it waits for a person to check it" in ops and "nob has no board tools" in ops
    assert any(to == ["dana@example.com"] and f"#{for_person} " in body for to, _, body in crew)
    assert any(to == ["owner@example.com"] and f"#{own_review} " in body for to, _, body in crew)
    told = len(crew)
    board.remind("bravo", now=5 * DAY)
    assert len(crew) == told                       # once until the card changes


def test_remind_after_comes_from_the_agents_policy(crew):
    _card("Long job", "slow")
    _card("Never nag", "bravo")
    board.remind("slow", now=2 * DAY)
    board.remind("bravo", now=30 * DAY)
    assert _reminders("slow") == [] and _reminders("bravo") == [] and crew == []
    board.remind("slow", now=3.1 * DAY)
    assert len(_reminders("slow")) == 1
    with pytest.raises(ValueError):
        policy.parse('health:\n  - {for: ["*"], remind_after: whenever}\n')


def test_the_reminder_rides_the_agents_own_pull(client, crew):
    card = _card("Rotate keys", "alpha", at=-2 * DAY)
    response = client.get("/mesh/alpha", headers={"Authorization": f"Bearer {mesh.token('alpha', mesh.BEACON_PEER)}",
                                                  "X-Beacon-Sync-Version": "2"})
    item, = response.json()["inbox"]
    assert f'#{card} "Rotate keys"' in item["text"]


def test_a_reminder_failure_never_costs_the_agent_its_config(client, crew, monkeypatch):
    monkeypatch.setattr(board, "remind", lambda agent: 1 / 0)
    response = client.get("/mesh/alpha", headers={"Authorization": f"Bearer {mesh.token('alpha', mesh.BEACON_PEER)}"})
    assert response.status_code == 200
