"""An owner talking with their agent (backend/chat.py, docs/chat-plan.md)."""

import base64
import json
import time

import pytest
from itsdangerous import TimestampSigner

from backend import apns, auth, chat, mesh, notify, opstore, store
from backend.schemas import Agent
from tests.test_app_devices import ANN, BOB, TOM, logged_in, secured  # noqa: F401 (fixture)

MCP = {"Authorization": "Bearer mcp-secret"}


@pytest.fixture
def chatting(secured, monkeypatch):
    """tom (an operator) owns tombot, ann (a member) annsbot, both with chat on; worker is bob's, chat off."""
    monkeypatch.setattr(chat, "POLL_SECONDS", 0)
    told = []
    monkeypatch.setattr(notify, "_tell", lambda *a, **k: told.append((a, k)))
    for id_, owner in (("tombot", TOM), ("annsbot", ANN)):
        store.upsert_agent(Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "owner": owner,
                                                 "reach": {"endpoint": f"http://{id_}.test:9900", "chat": True}}))
    secured.told = told
    return secured


def browser(client, email):
    payload = base64.b64encode(json.dumps({"user": {"email": email}}).encode()).decode()
    cookie = TimestampSigner(auth.SESSION_SECRET).sign(payload.encode()).decode()
    return {"Cookie": f"session={cookie}", "Origin": "https://testserver"}


def agent_headers(agent_id):
    return {"Authorization": f"Bearer {mesh.token(agent_id, mesh.BEACON_PEER)}", "X-Beacon-Chat-Plugin": "1",
            "X-Hermes-Version": "0.21.1"}


def pull(client, agent_id="tombot"):
    return client.get(f"/mesh/{agent_id}/chat", headers=agent_headers(agent_id)).json()["messages"]


def post(client, agent_id="tombot", **event):
    return client.post(f"/mesh/{agent_id}/chat", json=event, headers=agent_headers(agent_id))


def start(client, text="hello", agent="tombot", email=TOM):
    return client.post("/api/chat/threads", json={"agent": agent, "text": text}, headers=browser(client, email))


def messages(client, thread_id, email=TOM, since=None):
    path = f"/api/chat/threads/{thread_id}" + (f"?since={since}" if since is not None else "")
    return client.get(path, headers=browser(client, email)).json()


def test_a_message_goes_to_the_agent_and_the_answer_comes_back(chatting):
    thread = start(chatting).json()
    queued = pull(chatting)
    assert [(m["chat"], m["text"], m["quoted"]) for m in queued] == [(f"t{thread['id']}", "hello", None)]
    assert messages(chatting, thread["id"])["messages"][0]["state"] == "queued"
    post(chatting, type="receipt", ids=[queued[0]["id"]])
    assert pull(chatting) == []
    post(chatting, type="message", chat=f"t{thread['id']}", text="Hel", remote_id="r1", final=False)
    first = messages(chatting, thread["id"])
    assert [(m["sender"], m["state"]) for m in first["messages"]] == [("owner", "delivered"), ("agent", "delivered")]
    post(chatting, type="edit", remote_id="r1", text="Hello back", final=True)
    changed = messages(chatting, thread["id"], since=first["rev"])["messages"]
    assert [(m["text"], m["final"]) for m in changed] == [("Hello back", True)]
    assert chatting.get("/api/chat/agents", headers=browser(chatting, TOM)).json()[0]["hermes"] == "0.21.1"


def test_a_message_sent_twice_by_the_plugin_is_stored_once(chatting):
    for _ in range(2):
        post(chatting, type="message", text="done", remote_id="r1")
    [home] = chatting.get("/api/chat/threads", headers=browser(chatting, TOM)).json()
    assert home["origin"] == "home" and home["unread"] == 1
    assert len(messages(chatting, home["id"])["messages"]) == 1


def test_only_the_owner_operators_included_in_the_refusal(chatting):
    thread = start(chatting, agent="annsbot", email=ANN).json()
    # tom is an operator, and still gets nothing of ann's.
    assert start(chatting, agent="annsbot", email=TOM).status_code == 404
    assert chatting.get(f"/api/chat/threads/{thread['id']}", headers=browser(chatting, TOM)).status_code == 404
    assert chatting.get("/api/chat/threads", headers=browser(chatting, TOM)).json() == []
    assert start(chatting, agent="tombot", email=ANN).status_code == 404
    assert [a["id"] for a in chatting.get("/api/chat/agents", headers=browser(chatting, ANN)).json()] == ["annsbot"]


def test_no_ai_client_and_no_other_agent_gets_in(chatting):
    assert chatting.get("/api/chat/threads", headers=MCP).status_code == 403
    assert chatting.post("/api/chat/threads", json={"agent": "tombot", "text": "x"}, headers=MCP).status_code == 403
    assert chatting.get("/api/chat/threads", headers=agent_headers("tombot")).status_code == 401
    # Another agent's token opens nothing of this one's.
    assert chatting.get("/mesh/tombot/chat", headers=agent_headers("annsbot")).status_code == 401


def test_chat_off_means_off(chatting):
    store.upsert_agent(store.get_agent("tombot").model_copy(update={"owner": BOB}))
    assert chatting.get("/mesh/worker/chat", headers=agent_headers("worker")).status_code == 409
    assert chatting.post("/api/chat/threads", json={"agent": "worker", "text": "x"},
                         headers=browser(chatting, TOM)).status_code == 404


def test_without_login_there_is_no_chat(chatting, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    assert chatting.get("/api/chat/threads").status_code == 403


def test_the_app_needs_face_id_before_chat(chatting):
    device = logged_in(chatting)
    assert chatting.get("/api/chat/threads", headers=device.headers()).status_code == 423
    assert chatting.post("/api/chat/unlock", headers=device.headers()).status_code == 403
    signed = device.headers("POST", "/api/chat/unlock")
    assert chatting.post("/api/chat/unlock", headers=signed).status_code == 200
    sent = chatting.post("/api/chat/threads", json={"agent": "tombot", "text": "hi"}, headers=device.headers())
    assert sent.status_code == 200
    with opstore.connect() as db:
        db.execute("UPDATE chat_grants SET expires_at = ?", (time.time() - 1,))
    assert chatting.get("/api/chat/threads", headers=device.headers()).status_code == 423


@pytest.mark.parametrize("text, status", [
    ("/stop", 200), ("/new", 200), ("/approve", 200), ("/deny", 200), ("  /deny  ", 200),
    ("/approve always", 400), ("/yolo", 400), ("/resume x", 400), ("", 400), ("x" * (chat.MAX_BODY + 1), 413),
])
def test_only_a_few_commands_pass(chatting, text, status):
    assert start(chatting, text).status_code == status


def test_a_queued_message_can_be_taken_back_and_expires(chatting):
    thread = start(chatting).json()
    [first] = messages(chatting, thread["id"])["messages"]
    assert chatting.delete(f"/api/chat/messages/{first['id']}", headers=browser(chatting, TOM)).status_code == 200
    assert pull(chatting) == []
    second = chatting.post(f"/api/chat/threads/{thread['id']}/messages", json={"text": "again"},
                           headers=browser(chatting, TOM)).json()
    with opstore.connect() as db:
        db.execute("UPDATE chat_messages SET expires_at = ? WHERE id = ?", (time.time() - 1, second["id"]))
    chat.purge()
    assert pull(chatting) == []
    assert [m["state"] for m in messages(chatting, thread["id"])["messages"]] == ["cancelled", "expired"]


def test_a_delivered_message_cannot_be_taken_back(chatting):
    thread = start(chatting).json()
    [queued] = pull(chatting)
    post(chatting, type="receipt", ids=[queued["id"]])
    assert chatting.delete(f"/api/chat/messages/{queued['id']}", headers=browser(chatting, TOM)).status_code == 409
    assert thread["id"]


def test_a_reply_carries_what_it_answers(chatting):
    post(chatting, type="message", text="Shall I rotate the key?", remote_id="r1")
    [home] = chatting.get("/api/chat/threads", headers=browser(chatting, TOM)).json()
    [asked] = messages(chatting, home["id"])["messages"]
    chatting.post(f"/api/chat/threads/{home['id']}/messages", json={"text": "yes", "reply_to": asked["id"]},
                  headers=browser(chatting, TOM))
    [queued] = pull(chatting)
    assert (queued["chat"], queued["quoted"]) == ("home", "Shall I rotate the key?")


def test_history_is_gone_after_thirty_days(chatting):
    thread = start(chatting).json()
    old = time.time() - chat.KEEP_SECONDS - 1
    with opstore.connect() as db:
        db.execute("UPDATE chat_messages SET created_at = ?", (old,))
        db.execute("UPDATE chat_threads SET updated_at = ?", (old,))
    chat.purge()
    assert chatting.get(f"/api/chat/threads/{thread['id']}", headers=browser(chatting, TOM)).status_code == 404
    with opstore.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0


def test_a_new_owner_does_not_see_the_old_owners_chat_nor_send_it_on(chatting):
    thread = start(chatting, "private").json()
    store.upsert_agent(store.get_agent("tombot").model_copy(update={"owner": ANN}))
    assert pull(chatting) == []  # what tom queued does not reach the agent any more
    assert chatting.get(f"/api/chat/threads/{thread['id']}", headers=browser(chatting, ANN)).status_code == 404
    assert chatting.get("/api/chat/threads", headers=browser(chatting, ANN)).json() == []
    assert chatting.get(f"/api/chat/threads/{thread['id']}", headers=browser(chatting, TOM)).status_code == 404


def test_one_notice_until_read_and_never_the_words(chatting):
    for n in range(3):
        post(chatting, type="message", text=f"secret plan {n}", remote_id=f"r{n}")
    assert len(chatting.told) == 1
    args, kwargs = chatting.told[0]
    assert args[0] == {TOM} and args[1] == "tombot sent you a message" and "secret" not in str(chatting.told)
    assert kwargs["email"] is False and kwargs["about"]["agent"] == "tombot"
    assert apns.payload(args[1], about=kwargs["about"])["beacon"]["thread"] == kwargs["about"]["thread"]
    assert "category" not in apns.payload(args[1], about=kwargs["about"])["aps"]


def test_turning_chat_on_takes_a_person(chatting):
    record = store.get_agent("worker").model_dump()
    record["reach"]["chat"] = True
    assert chatting.put("/api/agents/worker", json=record, headers=MCP).status_code == 403
    assert chatting.put("/api/agents/worker", json=record, headers=browser(chatting, TOM)).status_code == 200


def test_deleting_a_conversation(chatting):
    thread = start(chatting).json()
    assert chatting.delete(f"/api/chat/threads/{thread['id']}", headers=browser(chatting, TOM)).status_code == 200
    assert chatting.get("/api/chat/threads", headers=browser(chatting, TOM)).json() == []
    assert pull(chatting) == []
