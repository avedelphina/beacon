"""Notification tones in the app: each device chooses, per kind of notice (backend/apns.py)."""

import pytest

from backend import apns, app_devices, opstore
from tests.test_app_devices import ANN, TOM, logged_in, secured  # noqa: F401 (fixture)

REQUEST = {"workflow": 1, "clearance": 3, "kind": "approval", "card": 2}
CHAT = {"agent": "alpha", "thread": 5}


@pytest.mark.parametrize("urgent, about, kind", [
    (False, REQUEST, "requests"), (True, REQUEST, "urgent"), (False, CHAT, "chat"),
    (False, None, None), (False, {"agent": "alpha"}, None), (False, {"project": 1}, None), (True, None, "urgent"),
])
def test_which_tone_a_notice_takes(urgent, about, kind):
    assert apns.sound_kind(urgent, about) == kind


def test_the_chosen_tone_is_named_and_none_means_silent():
    chosen = {"requests": "bell", "chat": "none", "urgent": "default"}
    assert apns.payload("x", about=REQUEST, sounds=chosen)["aps"]["sound"] == "bell.wav"
    assert "sound" not in apns.payload("x", about=CHAT, sounds=chosen)["aps"]
    assert apns.payload("x", urgent=True, about=REQUEST, sounds=chosen)["aps"]["sound"] == "default"
    assert apns.payload("x", sounds=chosen)["aps"]["sound"] == "default"
    assert apns.payload("x", about=REQUEST)["aps"]["sound"] == "default"


def test_each_device_hears_its_own_choice(secured, monkeypatch):
    first, second = logged_in(secured), logged_in(secured)
    for device, token in ((first, "a" * 64), (second, "b" * 64)):
        secured.put("/api/app/devices/self/apns", json={"token": token, "env": "sandbox"}, headers=device.headers())
    assert secured.put("/api/app/devices/self/sounds", json={"chat": "ping"}, headers=first.headers()).status_code == 200
    sent = {}
    monkeypatch.setattr(apns, "configured", lambda: True)
    monkeypatch.setattr(apns, "deliver", lambda device, body, priority: sent.update({device["apns_token"]: body}))
    monkeypatch.setattr(apns, "threading", type("T", (), {
        "Thread": staticmethod(lambda target, daemon: type("R", (), {"start": staticmethod(target)})())}))
    apns.send({TOM}, "alpha sent you a message", about=CHAT)
    assert sent["a" * 64]["aps"]["sound"] == "ping.wav" and sent["b" * 64]["aps"]["sound"] == "default"


def test_sounds_are_a_devices_own_and_only_known_tones(secured):
    member = logged_in(secured, email=ANN)
    assert secured.put("/api/app/devices/self/sounds", json={"requests": "rise"}, headers=member.headers()).status_code == 200
    assert secured.put("/api/app/devices/self/sounds", json={"requests": "../x"}, headers=member.headers()).status_code == 422
    assert secured.put("/api/app/devices/self/sounds", json={}, headers={"Authorization": "Bearer mcp-secret"}).status_code == 409
    with opstore.connect() as db:
        assert db.execute("SELECT sounds FROM app_devices").fetchone()[0] == '{"chat": "default", "requests": "rise", "urgent": "default"}'


# --- the badge and stuck cards ---------------------------------------------------

def test_the_badge_counts_what_waits_on_that_person(secured, monkeypatch):
    from backend import board, chat, notify, store, workflow
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    store.upsert_agent(store.get_agent("annsbot").model_copy(update={
        "reach": store.get_agent("annsbot").reach.model_copy(update={"chat": True})}))
    ann, tom = logged_in(secured, email=ANN), logged_in(secured, email=TOM)
    assert secured.get("/api/app/badge", headers=ann.headers()).json() == {"requests": 0, "chats": 0}
    card = board.create_card("beacon", "Rotate the key", assignee="annsbot")["id"]
    workflow.create(card_id=card, kind="review", requester="annsbot", eligible_responder="human", capability="change",
                    clearance=3, title="Rotate", summary="s", options=[], target={}, consult_skipped="x",
                    **workflow.plan_binding({"n": 1}))
    chat.agent_says(store.get_agent("annsbot"), "home", "Done.", "r1", True)
    assert secured.get("/api/app/badge", headers=ann.headers()).json() == {"requests": 1, "chats": 1}
    assert secured.get("/api/app/badge", headers=tom.headers()).json() == {"requests": 1, "chats": 0}  # an operator
    assert secured.get("/api/app/badge", headers={"Authorization": "Bearer mcp-secret"}).json() == {"requests": 0, "chats": 0}
    assert sum(notify.waiting_for(ANN).values()) == 2


def test_each_person_gets_their_own_number(secured, monkeypatch):
    devices = {ANN: logged_in(secured, email=ANN), TOM: logged_in(secured, email=TOM)}
    for n, device in enumerate(devices.values()):
        secured.put("/api/app/devices/self/apns", json={"token": str(n) * 64, "env": "sandbox"}, headers=device.headers())
    sent = []
    monkeypatch.setattr(apns, "configured", lambda: True)
    monkeypatch.setattr(apns, "deliver", lambda device, body, priority: sent.append((device["email"], body["aps"].get("badge"))))
    monkeypatch.setattr(apns, "threading", type("T", (), {
        "Thread": staticmethod(lambda target, daemon: type("R", (), {"start": staticmethod(target)})())}))
    apns.send({ANN, TOM}, "x", badge=lambda email: 2 if email == ANN else 7)
    assert sorted(sent) == [(ANN, 2), (TOM, 7)]
    sent.clear()
    apns.send({ANN}, "x", badge=lambda email: 1 / 0)  # the notice still goes
    assert sent == [(ANN, None)]


def test_a_stuck_card_notice_names_the_cards(secured, monkeypatch):
    from backend import notify
    told = []
    monkeypatch.setattr(notify, "_tell", lambda people, subject, body, **kw: told.append((people, kw)))
    card = {"id": 4, "state": "doing", "title": "t", "assignee": "annsbot"}
    notify.cards_stuck([(card, None, "no change"), (card | {"id": 9}, None, "no change")])
    about = {next(iter(people)): kw["about"] for people, kw in told}
    assert about[ANN] == {"cards": [4, 9]} and about[TOM] == {"cards": [4, 9]}
    assert apns.payload("x", about=about[ANN])["beacon"] == {"cards": [4, 9]}


def test_a_card_is_stuck_once_people_were_told_and_until_it_changes(secured, monkeypatch):
    from backend import board
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    card = board.create_card("beacon", "Tidy the logs")["id"]
    flag = lambda: next(c["stuck"] for c in board.list_cards() if c["id"] == card)
    assert flag() is False
    with opstore.connect() as db:  # as board.remind records it, a while after the last change
        db.execute("UPDATE cards SET updated_at = updated_at - 100, escalated_at = updated_at - 50 WHERE id = ?", (card,))
    assert flag() is True and board.get_card(card)["stuck"] is True and board.get_card(card)["state"] == "todo"
    board.update_card(TOM, card, state="doing")  # any change ends it
    assert flag() is False
