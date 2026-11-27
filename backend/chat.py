"""An owner talking with their own agent (docs/chat-plan.md).

Two sides. The agent's plugin connects out (`/mesh/{agent}/chat`, with the agent's pull token), takes
what the owner wrote and posts what the agent says. The owner reads and writes through `/api/chat`.
Beacon only carries: it queues for whoever is away and keeps KEEP_SECONDS of history.

Why only the owner, operators included in the refusal: a conversation is private, and it drives an
agent with all its tools. Why chat is never an approval: nothing said here answers a workflow request
or satisfies a clearance. Why the app needs a Face ID grant: a device token alone counts as a person
only up to CL3, and must not be enough to command an agent that has a terminal.
"""

import asyncio
import time

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import auth, members, mesh, notify, opstore, store

KEEP_SECONDS = 30 * 24 * 3600
GRANT_SECONDS = 15 * 60      # from the last use
MAX_BODY = 32 * 1024
POLL_SECONDS = 25            # how long a long poll waits; below usual proxy timeouts
POLL_STEP = 1.0
MAX_AGENT_MESSAGES_PER_HOUR = 300
READING_SECONDS = 60         # an owner who polled a thread this recently is reading it
# The only slash commands the owner can send. Hermes has more, some of which switch safety off
# (/yolo) or change what a chat is attached to; /approve takes no argument, so never "always".
COMMANDS = frozenset({"/stop", "/new", "/approve", "/deny"})
HOME = "home"                # the plugin's name for the thread scheduled results go to


class ChatError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def enabled(agent) -> bool:
    return bool(agent.owner and agent.reach and agent.reach.chat)


def _agent_for(email: str, agent_id: str):
    """The agent, if this person may chat with it. One answer for "no such agent", "not yours" and
    "chat is off for others", so that nobody can probe the fleet through it."""
    try:
        agent = store.get_agent(agent_id)
    except (store.NotFound, store.InvalidId):
        raise ChatError(404, "no such agent")
    if not members.owns(email, agent):
        raise ChatError(404, "no such agent")
    if not enabled(agent):
        raise ChatError(409, "chat is off for this agent")
    return agent


def purge(now: float | None = None) -> None:
    """Expire what waited too long and forget what is old. Beacon has no timer: every chat request
    and every agent's pull runs this."""
    now = now or time.time()
    with opstore.connect() as db:
        db.execute("UPDATE chat_messages SET state = 'expired', rev = ? WHERE state = 'queued' AND expires_at < ?",
                   (_rev(db), now))
        db.execute("DELETE FROM chat_messages WHERE created_at < ?", (now - KEEP_SECONDS,))
        db.execute("DELETE FROM chat_threads WHERE updated_at < ? AND NOT EXISTS"
                   " (SELECT 1 FROM chat_messages m WHERE m.thread_id = chat_threads.id)", (now - KEEP_SECONDS,))
        db.execute("DELETE FROM chat_grants WHERE expires_at < ?", (now,))


def _rev(db) -> int:
    """A number that only grows, for "what changed since": timestamps can tie."""
    rev = db.execute("INSERT INTO chat_rev DEFAULT VALUES").lastrowid
    db.execute("DELETE FROM chat_rev WHERE id < ?", (rev,))  # AUTOINCREMENT never hands a number out twice
    return rev


def _thread(db, agent: str, owner: str, title: str, origin: str) -> int:
    now = time.time()
    return db.execute("INSERT INTO chat_threads (agent, owner, title, origin, created_at, updated_at, read_at)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?)", (agent, owner.casefold(), title, origin, now, now, now)).lastrowid


def _message(db, thread_id: int, sender: str, body: str, state: str, remote_id: str | None = None,
             reply_to: int | None = None, expires_at: float | None = None, final: bool = True) -> int:
    now = time.time()
    db.execute("UPDATE chat_threads SET updated_at = ? WHERE id = ?", (now, thread_id))
    return db.execute(
        "INSERT INTO chat_messages (thread_id, sender, body, state, remote_id, reply_to, created_at, expires_at,"
        " final, rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (thread_id, sender, body, state, remote_id, reply_to, now, expires_at, int(final), _rev(db))).lastrowid


def _view(row) -> dict:
    return {"id": row["id"], "sender": row["sender"], "text": row["body"], "state": row["state"],
            "reply_to": row["reply_to"], "created_at": row["created_at"], "final": bool(row["final"]), "rev": row["rev"]}


def _thread_view(db, row) -> dict:
    unread = db.execute("SELECT COUNT(*) FROM chat_messages WHERE thread_id = ? AND sender = 'agent'"
                        " AND created_at > ?", (row["id"], row["read_at"])).fetchone()[0]
    last = db.execute("SELECT body FROM chat_messages WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    return {"id": row["id"], "agent": row["agent"], "title": row["title"], "origin": row["origin"],
            "updated_at": row["updated_at"], "unread": unread, "last": last["body"][:200] if last else ""}


def _own_thread(db, email: str, thread_id: int):
    """A thread is the person's who it was made for: a new owner of the agent does not see it."""
    row = db.execute("SELECT * FROM chat_threads WHERE id = ? AND owner = ?", (thread_id, email.casefold())).fetchone()
    if row is None:
        raise ChatError(404, "no such conversation")
    _agent_for(email, row["agent"])
    return row


# --- the owner's side ----------------------------------------------------------

def check_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ChatError(400, "nothing to send")
    if len(text.encode()) > MAX_BODY:
        raise ChatError(413, "the message is too long")
    if text.startswith("/") and text not in COMMANDS:
        raise ChatError(400, f"only these commands work here: {', '.join(sorted(COMMANDS))}")
    return text


def _queue(db, agent, thread_id: int, text: str, reply_to: int | None) -> int:
    if reply_to is not None and db.execute("SELECT 1 FROM chat_messages WHERE id = ? AND thread_id = ?",
                                           (reply_to, thread_id)).fetchone() is None:
        raise ChatError(400, "the message replied to is not in this conversation")
    return _message(db, thread_id, "owner", text, "queued", reply_to=reply_to,
                    expires_at=time.time() + _wait_seconds(agent))


def _wait_seconds(agent) -> float:
    """A message waits for its agent as long as agent mail does (policy `mailbox_ttl`)."""
    from . import mailbox
    return mailbox._ttl(agent.id)


def start(email: str, agent_id: str, text: str) -> dict:
    agent, text = _agent_for(email, agent_id), check_text(text)
    with opstore.connect() as db:
        thread_id = _thread(db, agent.id, email, " ".join(text.split())[:60], "app")
        _queue(db, agent, thread_id, text, None)
        return _thread_view(db, db.execute("SELECT * FROM chat_threads WHERE id = ?", (thread_id,)).fetchone())


def say(email: str, thread_id: int, text: str, reply_to: int | None = None) -> dict:
    text = check_text(text)
    with opstore.connect() as db:
        row = _own_thread(db, email, thread_id)
        message_id = _queue(db, _agent_for(email, row["agent"]), thread_id, text, reply_to)
        return _view(db.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone())


def threads(email: str, agent_id: str | None = None) -> list[dict]:
    mine = {a.id for a in store.list_agents() if members.owns(email, a) and enabled(a)}
    with opstore.connect() as db:
        rows = db.execute("SELECT * FROM chat_threads WHERE owner = ? ORDER BY updated_at DESC", (email.casefold(),))
        return [_thread_view(db, r) for r in rows.fetchall()
                if r["agent"] in mine and (agent_id is None or r["agent"] == agent_id)]


def agents(email: str) -> list[dict]:
    """The agents this person can talk with, and when each one's plugin was last there."""
    with opstore.connect() as db:
        seen = {r["agent"]: r for r in db.execute("SELECT * FROM chat_seen")}
    out = []
    for agent in store.list_agents():
        if members.owns(email, agent) and enabled(agent):
            row = seen.get(agent.id)
            out.append({"id": agent.id, "connected_at": row["at"] if row else None,
                        "plugin": row["plugin"] if row else None, "hermes": row["hermes"] if row else None})
    return out


def read(email: str, thread_id: int, since: int = 0) -> dict:
    """The thread's messages that changed after revision `since`; reading marks them read."""
    with opstore.connect() as db:
        row = _own_thread(db, email, thread_id)
        messages = db.execute("SELECT * FROM chat_messages WHERE thread_id = ? AND rev > ? ORDER BY id",
                              (thread_id, since)).fetchall()
        db.execute("UPDATE chat_threads SET read_at = ?, notified = 0 WHERE id = ?", (time.time(), thread_id))
        return {"thread": _thread_view(db, db.execute("SELECT * FROM chat_threads WHERE id = ?", (thread_id,)).fetchone()),
                "messages": [_view(m) for m in messages], "rev": max([since, *(m["rev"] for m in messages)])}


def cancel(email: str, message_id: int) -> None:
    with opstore.connect() as db:
        row = db.execute("SELECT thread_id FROM chat_messages WHERE id = ? AND sender = 'owner'", (message_id,)).fetchone()
        if row is None:
            raise ChatError(404, "no such message")
        _own_thread(db, email, row["thread_id"])
        changed = db.execute("UPDATE chat_messages SET state = 'cancelled', rev = ? WHERE id = ? AND state = 'queued'",
                             (_rev(db), message_id)).rowcount
    if not changed:
        raise ChatError(409, "the agent already has this message")


def delete_thread(email: str, thread_id: int) -> None:
    with opstore.connect() as db:
        _own_thread(db, email, thread_id)
        db.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
        db.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))


# --- the agent's side -----------------------------------------------------------

def _remote(row) -> str:
    """The plugin's chat id for a thread: one conversation, one Hermes session."""
    return HOME if row["origin"] == HOME else f"t{row['id']}"


def waiting(agent) -> list[dict]:
    """What the owner wrote and the agent does not have yet. Only the current owner's: after a change
    of owner, nothing the earlier one queued reaches the agent."""
    if not enabled(agent):
        return []
    with opstore.connect() as db:
        rows = db.execute(
            "SELECT m.id, m.body, m.created_at, m.reply_to, t.id AS thread_id, t.origin, r.body AS quoted"
            " FROM chat_messages m JOIN chat_threads t ON t.id = m.thread_id"
            " LEFT JOIN chat_messages r ON r.id = m.reply_to"
            " WHERE t.agent = ? AND t.owner = ? AND m.sender = 'owner' AND m.state = 'queued' ORDER BY m.id",
            (agent.id, agent.owner.casefold())).fetchall()
    return [{"id": r["id"], "chat": HOME if r["origin"] == HOME else f"t{r['thread_id']}", "text": r["body"],
             "written_at": r["created_at"], "quoted": r["quoted"][:1000] if r["quoted"] else None} for r in rows]


def seen(agent_id: str, plugin: str | None, hermes: str | None) -> None:
    with opstore.connect() as db:
        db.execute("INSERT INTO chat_seen (agent, at, plugin, hermes) VALUES (?, ?, ?, ?) ON CONFLICT (agent)"
                   " DO UPDATE SET at = excluded.at, plugin = excluded.plugin, hermes = excluded.hermes",
                   (agent_id, time.time(), plugin, hermes))


def receipt(agent, ids: list[int]) -> None:
    with opstore.connect() as db:
        for message_id in ids:
            db.execute("UPDATE chat_messages SET state = 'delivered', rev = ? WHERE id = ? AND state = 'queued'"
                       " AND thread_id IN (SELECT id FROM chat_threads WHERE agent = ?)", (_rev(db), message_id, agent.id))


def _agent_thread(db, agent, chat: str):
    """The owner's thread the plugin means; anything it does not know goes to the home thread."""
    owner = agent.owner.casefold()
    if chat.startswith("t") and chat[1:].isdigit():
        row = db.execute("SELECT * FROM chat_threads WHERE id = ? AND agent = ? AND owner = ?",
                         (int(chat[1:]), agent.id, owner)).fetchone()
        if row is not None:
            return row
    row = db.execute("SELECT * FROM chat_threads WHERE agent = ? AND owner = ? AND origin = ?",
                     (agent.id, owner, HOME)).fetchone()
    if row is None:
        row_id = _thread(db, agent.id, owner, "Scheduled and other messages", HOME)
        db.execute("UPDATE chat_threads SET read_at = 0 WHERE id = ?", (row_id,))
        row = db.execute("SELECT * FROM chat_threads WHERE id = ?", (row_id,)).fetchone()
    return row


def agent_says(agent, chat: str, text: str, remote_id: str, final: bool) -> tuple[int, str] | None:
    """Store a new message from the agent; (thread, owner) when the owner should be told."""
    text = text[:MAX_BODY]
    with opstore.connect() as db:
        count = db.execute("SELECT COUNT(*) FROM chat_messages m JOIN chat_threads t ON t.id = m.thread_id"
                           " WHERE t.agent = ? AND m.sender = 'agent' AND m.created_at > ?",
                           (agent.id, time.time() - 3600)).fetchone()[0]
        if count >= MAX_AGENT_MESSAGES_PER_HOUR:
            raise ChatError(429, "too many messages in an hour")
        thread = _agent_thread(db, agent, chat)
        if db.execute("SELECT 1 FROM chat_messages WHERE thread_id = ? AND remote_id = ?",
                      (thread["id"], remote_id)).fetchone():
            return None  # sent again after a lost answer
        _message(db, thread["id"], "agent", text, "delivered", remote_id=remote_id, final=final)
        # Once per thread until the owner reads it, and not while they are looking at it.
        if thread["notified"] or time.time() - thread["read_at"] < READING_SECONDS:
            return None
        db.execute("UPDATE chat_threads SET notified = 1 WHERE id = ?", (thread["id"],))
        return thread["id"], agent.owner.casefold()


def agent_edits(agent, remote_id: str, text: str, final: bool) -> None:
    """A streamed answer: the same message, longer."""
    with opstore.connect() as db:
        db.execute("UPDATE chat_messages SET body = ?, final = ?, rev = ? WHERE sender = 'agent' AND remote_id = ?"
                   " AND thread_id IN (SELECT id FROM chat_threads WHERE agent = ? AND owner = ?)",
                   (text[:MAX_BODY], int(final), _rev(db), remote_id, agent.id, agent.owner.casefold()))


# --- routes -------------------------------------------------------------------

router = APIRouter()


class NewThread(BaseModel):
    agent: str = Field(max_length=100)
    text: str


class NewMessage(BaseModel):
    text: str
    reply_to: int | None = None


def _person(request: Request, unlock: bool = False) -> str:
    """The owner's email. Never the MCP bearer or an agent token; an app device needs a live grant."""
    if getattr(request.state, "auth_via_mcp", False):
        raise ChatError(403, "chat is for a person: no AI client may read or write it")
    user = auth.current_user(request)
    email = (user or {}).get("email")
    if not email:
        raise ChatError(403, "chat needs login (OIDC): it belongs to a person")
    device = getattr(request.state, "app_device", None)
    if device and not unlock:
        with opstore.connect() as db:
            held = db.execute("UPDATE chat_grants SET expires_at = ? WHERE device_id = ? AND expires_at > ?",
                              (time.time() + GRANT_SECONDS, device["id"], time.time())).rowcount
        if not held:
            raise ChatError(423, "chat is locked: confirm with Face ID or Touch ID")
    purge()
    return email.casefold()


def _answer(work):
    try:
        return work()
    except ChatError as exc:
        raise HTTPException(exc.status, exc.detail)


@router.post("/api/chat/unlock")
def unlock(request: Request) -> dict:
    """The app opens chat with a request signed after Face ID; a browser session needs none."""
    def work():
        _person(request, unlock=True)
        device = getattr(request.state, "app_device", None)
        if device is None:
            return {"ok": True}
        if not getattr(request.state, "app_verified", False):
            raise ChatError(403, "confirm with Face ID or Touch ID in the app")
        with opstore.connect() as db:
            db.execute("INSERT INTO chat_grants (device_id, expires_at) VALUES (?, ?) ON CONFLICT (device_id)"
                       " DO UPDATE SET expires_at = excluded.expires_at", (device["id"], time.time() + GRANT_SECONDS))
        return {"ok": True, "seconds": GRANT_SECONDS}
    return _answer(work)


@router.get("/api/chat/agents")
def chat_agents(request: Request) -> list[dict]:
    return _answer(lambda: agents(_person(request)))


@router.get("/api/chat/threads")
def chat_threads(request: Request, agent: str | None = None) -> list[dict]:
    return _answer(lambda: threads(_person(request), agent))


@router.post("/api/chat/threads")
def new_thread(body: NewThread, request: Request) -> dict:
    return _answer(lambda: start(_person(request), body.agent, body.text))


@router.get("/api/chat/threads/{thread_id}")
async def thread(thread_id: int, request: Request, since: int | None = None) -> dict:
    """With `since`, waits up to POLL_SECONDS for something newer (a long poll)."""
    try:
        email = await asyncio.to_thread(_person, request)
        deadline = time.monotonic() + (POLL_SECONDS if since is not None else 0)
        while True:
            answer = await asyncio.to_thread(read, email, thread_id, since or 0)
            if answer["messages"] or time.monotonic() >= deadline or await request.is_disconnected():
                return answer
            await asyncio.sleep(POLL_STEP)
    except ChatError as exc:
        raise HTTPException(exc.status, exc.detail)


@router.post("/api/chat/threads/{thread_id}/messages")
def new_message(thread_id: int, body: NewMessage, request: Request) -> dict:
    return _answer(lambda: say(_person(request), thread_id, body.text, body.reply_to))


@router.delete("/api/chat/messages/{message_id}")
def cancel_message(message_id: int, request: Request) -> dict:
    _answer(lambda: cancel(_person(request), message_id))
    return {"ok": True}


@router.delete("/api/chat/threads/{thread_id}")
def remove_thread(thread_id: int, request: Request) -> dict:
    _answer(lambda: delete_thread(_person(request), thread_id))
    return {"ok": True}


class AgentEvent(BaseModel):
    type: str  # message | edit | receipt
    chat: str = Field(HOME, max_length=40)
    text: str = Field("", max_length=4 * MAX_BODY)
    remote_id: str = Field("", max_length=100)
    final: bool = True
    ids: list[int] = Field(default_factory=list, max_length=200)


def _plugin_agent(agent_id: str, authorization: str | None):
    # The token is checked before the agent is looked up, as for /mesh/{agent}.
    if not mesh.may_pull(agent_id, authorization):
        raise HTTPException(401, "not authorized for this agent's chat")
    agent = store.get_agent(agent_id)
    if not enabled(agent):
        raise HTTPException(409, "chat is off for this agent")
    return agent


@router.get("/mesh/{agent_id}/chat")
async def agent_poll(agent_id: str, authorization: str | None = Header(None),
                     x_beacon_chat_plugin: str | None = Header(None, max_length=40),
                     x_hermes_version: str | None = Header(None, max_length=40)) -> dict:
    """The plugin's long poll: what its owner wrote. Async, so that a waiting agent holds no thread."""
    agent = await asyncio.to_thread(_plugin_agent, agent_id, authorization)
    await asyncio.to_thread(seen, agent.id, x_beacon_chat_plugin, x_hermes_version)
    await asyncio.to_thread(purge)
    deadline = time.monotonic() + POLL_SECONDS
    while True:
        messages = await asyncio.to_thread(waiting, agent)
        if messages or time.monotonic() >= deadline:
            return {"messages": messages}
        await asyncio.sleep(POLL_STEP)


@router.post("/mesh/{agent_id}/chat")
def agent_event(agent_id: str, event: AgentEvent, background: BackgroundTasks,
                authorization: str | None = Header(None)) -> dict:
    agent = _plugin_agent(agent_id, authorization)
    if event.type == "receipt":
        receipt(agent, event.ids)
    elif event.type in ("message", "edit"):
        if not event.remote_id:
            raise HTTPException(400, "remote_id is required")
        if event.type == "edit":
            agent_edits(agent, event.remote_id, event.text, event.final)
        else:
            tell = _answer(lambda: agent_says(agent, event.chat, event.text, event.remote_id, event.final))
            if tell:
                background.add_task(notify.chat_message, agent.id, tell[1], tell[0])
    else:
        raise HTTPException(400, f"unknown event {event.type!r}")
    return {"ok": True}
