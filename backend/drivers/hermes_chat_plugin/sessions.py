"""Listing, reading and copying the agent's other conversations (docs/chat-plan.md, 6).

The one part that relies on Hermes internals: the session store is not a plugin interface, and it
differs before and after Hermes 0.23. Everything of it is here; if a Hermes update breaks it, these
requests answer with an error and chat keeps working.

What is listed: command-line and desktop conversations, and direct messages from the platform user
ids the owner entered in Beacon ("platform:user id"). Never group chats, other people's direct
messages, or what agents said to each other: an agent may hold conversations that are not its
owner's. A conversation is continued as a **copy**; the original is never written to or switched to.
"""

import asyncio
import uuid

OWN_SOURCES = frozenset({"cli", "desktop", "local", "oneshot", "tui"})
SCAN = 300       # newest conversations looked at
LISTED = 50
TURNS = 60       # messages shown of one
TEXT = 4000


class Unavailable(Exception):
    pass


def allowed(row: dict, owner_ids: frozenset) -> bool:
    source = row.get("source")
    if source in OWN_SOURCES:
        return True
    return row.get("chat_type") == "dm" and bool(row.get("user_id")) and f"{source}:{row['user_id']}" in owner_ids


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # parts: keep the words, name the rest
        return "\n".join(p.get("text") or f"[{p.get('type', 'attachment')}]" for p in content if isinstance(p, dict))
    return ""


def turns(messages: list) -> list:
    """What was said, by the person and the agent: no tool calls, no system text."""
    said = [(m.get("role"), text_of(m.get("content")).strip()) for m in messages]
    return [{"role": role, "text": text[:TEXT]} for role, text in said if role in ("user", "assistant") and text][-TURNS:]


def _view(row: dict) -> dict:
    return {"id": str(row["id"]), "source": row.get("source"), "title": (row.get("title") or "")[:200],
            "preview": (row.get("preview") or "")[:200], "updated_at": row.get("last_active") or row.get("started_at")}


class Sqlite:
    """Hermes up to 0.22: the SessionDB behind the gateway's session store."""

    def __init__(self, adapter, key: str):
        self.store = adapter._session_store
        self.db = self.store._db_for_key(key)
        if self.db is None or not hasattr(self.db, "list_sessions_rich"):
            raise Unavailable("this Hermes has no session list the plugin knows")

    async def rows(self) -> list:
        return await asyncio.to_thread(self.db.list_sessions_rich, limit=SCAN, min_message_count=1,
                                       order_by_last_active=True)

    async def row(self, session_id: str) -> dict | None:
        return await asyncio.to_thread(self.db.get_session, session_id)

    async def messages(self, session_id: str) -> list:
        return await asyncio.to_thread(self.db.get_messages, session_id)

    async def copy(self, parent: dict, source) -> bool:
        def work():
            entry = self.store.get_or_create_session(source)
            child = "beacon_" + uuid.uuid4().hex[:16]
            # No system prompt: the parent's speaks of the platform it came from.
            self.db.create_session(child, "beacon", model=parent.get("model"), system_prompt=None,
                                   parent_session_id=str(parent["id"]),
                                   model_config={"_branched_from": str(parent["id"])})
            self.db.replace_messages(child, self.db.get_messages(str(parent["id"])))
            return self.store.switch_session(entry.session_key, child) is not None
        return await asyncio.to_thread(work)


class Persistence:
    """Hermes 0.23: the persistence layer of the gateway runner."""

    def __init__(self, adapter, key: str):
        from persistence.conversation.sessions import SessionQuery  # noqa: F401 (proves it is there)
        self.store = adapter._session_store
        self.db = adapter.gateway_runner.persistence.aio

    async def rows(self) -> list:
        from persistence.conversation.sessions import SessionOrder, SessionQuery, SessionWindow
        entries = await self.db.sessions.list_sessions(
            SessionQuery(min_message_count=1, order=SessionOrder.LAST_ACTIVE), SessionWindow(limit=SCAN))
        return [e.as_list_row() for e in entries]

    async def row(self, session_id: str) -> dict | None:
        record = await self.db.sessions.get(session_id)
        if record is None:
            return None
        return {"id": str(record.session_id), "source": record.source, "chat_type": record.chat_type,
                "user_id": record.user_id, "model": record.model}

    async def messages(self, session_id: str) -> list:
        return await asyncio.to_thread(self.store.load_transcript, session_id)

    async def copy(self, parent: dict, source) -> bool:
        from persistence.conversation.messages import NewMessage, ReplaceMode
        from persistence.conversation.sessions import NewSession, SessionLineageKind
        entry = await asyncio.to_thread(self.store.get_or_create_session, source)
        child = "beacon_" + uuid.uuid4().hex[:16]
        await self.db.sessions.create(NewSession(session_id=child, source="beacon", model=parent.get("model"),
                                                 parent_session_id=str(parent["id"]),
                                                 lineage_kind=SessionLineageKind.BRANCH))
        rows = await self.db.messages.read_transcript(str(parent["id"]))
        await self.db.messages.replace_transcript(
            child, [NewMessage.from_live(r.as_wire_row()) for r in rows], ReplaceMode.DESTRUCTIVE)
        return await asyncio.to_thread(self.store.switch_session, entry.session_key, child) is not None


def backend(adapter, key: str):
    try:
        try:
            return Persistence(adapter, key)
        except ImportError:
            return Sqlite(adapter, key)
    except Unavailable:
        raise
    except Exception as exc:
        raise Unavailable(f"this Hermes keeps conversations in a way the plugin does not know: {exc}")


async def listing(store, owner_ids: frozenset, home_chats: dict) -> dict:
    rows = await store.rows()
    listed = [_view(r) for r in rows if allowed(r, owner_ids)][:LISTED]
    # A hint only for the agent's own home chat on a platform: nothing about anyone else.
    hints = sorted({f"{r['source']}:{r['user_id']}" for r in rows
                    if r.get("chat_type") == "dm" and r.get("user_id") and not allowed(r, owner_ids)
                    and str(r.get("chat_id")) == home_chats.get(r.get("source"))})
    return {"sessions": listed, "hints": hints}


async def read(store, owner_ids: frozenset, session_id: str) -> dict:
    row = await store.row(session_id)
    if row is None or not allowed(row, owner_ids):
        raise Unavailable("no such conversation")
    return {"messages": turns(await store.messages(session_id))}


async def copy(store, owner_ids: frozenset, session_id: str, source) -> dict:
    row = await store.row(session_id)
    if row is None or not allowed(row, owner_ids):
        raise Unavailable("no such conversation")
    if not await store.copy(row, source):
        raise Unavailable("Hermes did not take the copy")
    return {"ok": True}
