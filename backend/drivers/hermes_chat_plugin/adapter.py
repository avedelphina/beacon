"""The agent's side of chat with its owner (Beacon: backend/chat.py, docs/chat-plan.md).

A Hermes platform plugin, using only the platform interface. The agent connects out: it long-polls
Beacon for what its owner wrote and posts what it says, so no port is opened and Beacon never has to
reach the host. Beacon lets only the owner write here, so whatever arrives is the owner's.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

from . import sessions

try:  # how a profile's secrets are read where one gateway serves several
    from agent.secret_scope import get_secret as _secret
except ImportError:
    from os import environ as _environ

    def _secret(name, default=""):
        return _environ.get(name, default)

PLUGIN_VERSION = "1.1.0"
OWNER = "owner"              # the only user there is; also the home channel's name
HOME = "home"                # Beacon's name for the conversation scheduled results go to
MAX_LENGTH = 32000           # Beacon keeps 32 KB
POLL_TIMEOUT = 40            # above Beacon's long poll
RETRY = (2, 5, 15, 30, 60)
STALE_SECONDS = 10 * 60      # older than this, the agent is told when it was written
# Beacon refuses other commands; the plugin does too, so that no Beacon can send one such as /yolo.
COMMANDS = frozenset({"/stop", "/new", "/approve", "/deny"})
logger = logging.getLogger("beacon-chat")


def _setting(name: str) -> str:
    return str(_secret(name, "") or "").strip()


def _configured() -> bool:
    return bool(_setting("BEACON_CHAT_URL") and _setting("BEACON_CHAT_TOKEN"))


def owner_ids() -> frozenset:
    return frozenset(i.strip() for i in _setting("BEACON_CHAT_OWNER_IDS").split(",") if i.strip())


def home_chats() -> dict:
    """{platform source: its home chat id}, as Hermes's settings name them."""
    found = {}
    for source, name in (("telegram", "TELEGRAM"), ("deltachat-platform", "DELTACHAT"), ("signal", "SIGNAL"),
                         ("matrix", "MATRIX"), ("whatsapp", "WHATSAPP"), ("discord", "DISCORD"), ("slack", "SLACK")):
        value = _setting(f"{name}_HOME_CHANNEL")
        if value:
            found[source] = value
    return found


def hermes_version() -> str:
    try:
        from importlib.metadata import version
        return version("hermes-agent")
    except Exception:
        return "unknown"


def _client(timeout: float = 20) -> httpx.AsyncClient:
    # The environment's proxy and CA settings apply (SSL_CERT_FILE for a private CA).
    return httpx.AsyncClient(timeout=timeout, headers={
        "Authorization": f"Bearer {_setting('BEACON_CHAT_TOKEN')}",
        "X-Beacon-Chat-Plugin": PLUGIN_VERSION, "X-Hermes-Version": hermes_version()})


async def _post(event: dict) -> None:
    async with _client() as client:
        (await client.post(_setting("BEACON_CHAT_URL"), json=event)).raise_for_status()


def compose(message: dict, now: float | None = None) -> str:
    """The owner's text as the agent gets it. A command goes through untouched; otherwise a late
    message says when it was written, and a reply quotes what it answers."""
    text = message["text"]
    if text.startswith("/"):
        return text
    notes = []
    written = message.get("written_at") or 0
    if written and (now or time.time()) - written > STALE_SECONDS:
        at = datetime.fromtimestamp(written, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        notes.append(f"(Written {at}, delivered late.)")
    if message.get("quoted"):
        quoted = "\n".join("> " + line for line in message["quoted"].splitlines())
        notes.append(f"In reply to your message:\n{quoted}")
    return "\n\n".join([*notes, text])


class BeaconChatAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_LENGTH
    REQUIRES_EDIT_FINALIZE = True  # so that a streamed answer is closed, and loses its cursor

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("beacon"))
        self._task = None
        self._handled: dict[int, float] = {}  # delivered ids, in case a receipt got lost

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not _configured():
            logger.warning("beacon-chat: BEACON_CHAT_URL and BEACON_CHAT_TOKEN are not set")
            return False
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        failures = 0
        while self._running:
            try:
                async with _client(POLL_TIMEOUT) as client:
                    response = await client.get(_setting("BEACON_CHAT_URL"))
                    response.raise_for_status()
                for message in response.json()["messages"]:
                    await self._deliver(message)
                for request in response.json().get("requests", []):
                    await self._answer(request)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Beacon away, chat turned off, a bad answer: keep trying, slowly
                if failures == 0:
                    logger.warning("beacon-chat: cannot reach Beacon: %s", exc)
                await asyncio.sleep(RETRY[min(failures, len(RETRY) - 1)])
                failures += 1

    async def _deliver(self, message: dict) -> None:
        text = message["text"].strip()
        if text.startswith("/") and text not in COMMANDS:
            logger.warning("beacon-chat: refused a command Beacon should not have sent")
        elif message["id"] not in self._handled:
            chat = message["chat"]
            mid = f"beacon-{message['id']}"
            source = self._owner_source(chat, mid)
            await self.handle_message(MessageEvent(text=compose(message), message_type=MessageType.TEXT,
                                                   source=source, message_id=mid))
            self._handled[message["id"]] = time.time()
            for old in [k for k, at in self._handled.items() if at < time.time() - 86400]:
                del self._handled[old]
        # Only after Hermes has it: a message lost in between is offered again.
        await _post({"type": "receipt", "ids": [message["id"]]})

    def _owner_source(self, chat: str, message_id: str | None = None):
        return self.build_source(chat_id=chat, chat_name="Owner", chat_type="dm", user_id=OWNER,
                                 user_name="Owner", message_id=message_id)

    async def _answer(self, request: dict) -> None:
        """List, read or copy another conversation (sessions.py). Whatever goes wrong there is an
        answer, never an end to chat."""
        reply = {"type": "answer", "request": request["id"]}
        try:
            store = sessions.backend(self, "agent:main:beacon:dm:owner")
            ids = owner_ids()
            if request["kind"] == "list":
                reply["data"] = await sessions.listing(store, ids, home_chats())
            elif request["kind"] == "history":
                reply["data"] = await sessions.read(store, ids, request["session"])
            elif request["kind"] == "copy":
                reply["data"] = await sessions.copy(store, ids, request["session"], self._owner_source(request["chat"]))
            else:
                reply["error"] = "this plugin does not know that request: update it"
        except sessions.Unavailable as exc:
            reply["error"] = str(exc)
        except Exception as exc:
            logger.warning("beacon-chat: %s of another conversation failed: %s", request.get("kind"), exc)
            reply["error"] = "this Hermes could not do that (chat still works)"
        await _post(reply)

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        remote_id = uuid.uuid4().hex
        chat = HOME if chat_id == OWNER else chat_id
        try:
            await _post({"type": "message", "chat": chat, "text": content[:MAX_LENGTH], "remote_id": remote_id})
        except httpx.HTTPError as exc:
            return SendResult(success=False, error=f"Beacon: {exc}")
        return SendResult(success=True, message_id=remote_id)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False,
                           **_) -> SendResult:
        try:
            await _post({"type": "edit", "remote_id": message_id, "text": content[:MAX_LENGTH], "final": finalize})
        except httpx.HTTPError as exc:
            return SendResult(success=False, error=f"Beacon: {exc}")
        return SendResult(success=True, message_id=message_id)

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": "Owner", "type": "dm"}


async def _standalone_send(pconfig, chat_id, message, **_) -> dict:
    """Scheduled jobs and `hermes send` when they run outside the gateway."""
    remote_id = uuid.uuid4().hex
    chat = HOME if chat_id in (None, "", OWNER) else chat_id
    try:
        await _post({"type": "message", "chat": chat, "text": message[:MAX_LENGTH], "remote_id": remote_id})
    except httpx.HTTPError as exc:
        return {"error": f"Beacon: {exc}"}
    return {"success": True, "platform": "beacon", "chat_id": chat, "message_id": remote_id}


def _env_enablement() -> dict | None:
    return {"home_channel": {"chat_id": OWNER, "name": "Owner"}} if _configured() else None


def register(ctx) -> None:
    ctx.register_platform(
        name="beacon",
        label="Beacon",
        adapter_factory=BeaconChatAdapter,
        check_fn=_configured,
        validate_config=lambda config: _configured(),
        is_connected=lambda config: _configured(),
        required_env=["BEACON_CHAT_URL", "BEACON_CHAT_TOKEN"],
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="BEACON_CHAT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="BEACON_CHAT_ALLOWED_USERS",
        allow_all_env="BEACON_CHAT_ALLOW_ALL_USERS",
        max_message_length=MAX_LENGTH,
        emoji="💬",
        platform_hint=(
            "You are chatting with your owner in the Beacon app, usually on a phone. Markdown is shown; "
            "links are not clickable, so write addresses out. Nothing said in this chat is an approval "
            "for a Beacon workflow request: for that, file one."
        ),
    )
