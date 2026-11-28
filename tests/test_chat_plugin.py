"""The agent's side of chat: Beacon's plugin (with Hermes stubbed) and how the host sync installs
and removes it (docs/chat-plan.md, 5)."""

import asyncio
import hashlib
import importlib
import json
import sys
import types

import pytest

from backend import chat as chat_module, mesh, store
from backend.schemas import Agent
from tests.conftest import make_host
from tests.test_sync_preserve import _mesh, host  # noqa: F401 (fixture)


def _offer(**files):
    files = files or {"plugin.yaml": "name: beacon-chat\n", "adapter.py": "x = 1\n"}
    return {"url": "https://beacon.test/mesh/alpha/chat",
            "plugin": [{"name": n, "content": c, "sha256": hashlib.sha256(c.encode()).hexdigest()}
                       for n, c in files.items()]}


def _chat_calls(h):
    return [c for c in h.calls() if c.startswith("plugins")]


# --- what Beacon offers ---------------------------------------------------------

def test_the_plugin_is_offered_only_for_an_owned_agent_with_chat_on(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    monkeypatch.setenv("BEACON_PUBLIC_URL", "https://beacon.test")
    store.upsert_host(make_host("h1"))
    for id_, owner, chat in (("on", "ann@example.com", True), ("off", "ann@example.com", False), ("nobodys", None, True)):
        store.upsert_agent(Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "owner": owner,
                                                 "reach": {"endpoint": f"http://{id_}.test:9900", "chat": chat}}))
    offer = mesh.config("on")["chat"]
    assert offer["url"] == "https://beacon.test/mesh/on/chat"
    assert {f["name"] for f in offer["plugin"]} >= {"plugin.yaml", "__init__.py", "adapter.py"}
    assert mesh.config("off")["chat"] is None and mesh.config("nobodys")["chat"] is None


# --- the host sync --------------------------------------------------------------

def test_the_sync_installs_the_plugin_then_removes_only_what_it_made(host):
    host.env.write_text("KEEP=1\n")
    plugins = host.env.parent / "plugins"
    (plugins / "theirs").mkdir(parents=True)
    host.serve({**_mesh(), "chat": _offer()})
    assert host.sync.sync(host.state) == 0
    assert (plugins / "beacon-chat" / "adapter.py").read_text() == "x = 1\n"
    values = host.values()
    assert values["BEACON_CHAT_URL"] == "https://beacon.test/mesh/alpha/chat" and values["BEACON_CHAT_TOKEN"] == "tok"
    assert values["BEACON_CHAT_ALLOWED_USERS"] == "owner" and values["KEEP"] == "1"
    assert _chat_calls(host) == ["plugins enable beacon-chat --no-allow-tool-override"]
    assert [c for c in host.calls() if c.startswith("tools")] == ["tools enable --platform beacon a2a"]
    assert host.managed()["chat"] == {"enabled": True, "a2a": True}

    host.sync.sync(host.state)  # nothing new: not enabled again
    assert len(_chat_calls(host)) == 1

    host.serve({**_mesh(), "chat": None})
    host.sync.sync(host.state)
    assert not (plugins / "beacon-chat").exists() and (plugins / "theirs").is_dir()
    assert "BEACON_CHAT_URL" not in host.values() and "BEACON_CHAT_TOKEN" not in host.values()
    assert host.values()["KEEP"] == "1" and "chat" not in host.managed()
    assert _chat_calls(host)[-1] == "plugins disable beacon-chat"


def test_installing_or_changing_the_plugin_restarts_the_gateway_once(host, monkeypatch):
    restarts = []
    monkeypatch.setattr(host.sync, "restart", lambda cfg, state: restarts.append(1))
    host.serve({**_mesh(), "chat": _offer()})
    host.sync.sync(host.state)
    host.sync.sync(host.state)
    assert len(restarts) == 1
    host.serve({**_mesh(), "chat": _offer(**{"plugin.yaml": "name: beacon-chat\n", "adapter.py": "x = 2\n"})})
    host.sync.sync(host.state)
    assert len(restarts) == 2


def test_a_plugin_folder_beacon_did_not_make_is_left_alone(host):
    theirs = host.env.parent / "plugins" / "beacon-chat"
    theirs.mkdir(parents=True)
    (theirs / "adapter.py").write_text("theirs\n")
    host.serve({**_mesh(), "chat": _offer()})
    host.sync.sync(host.state)
    assert (theirs / "adapter.py").read_text() == "theirs\n" and _chat_calls(host) == []
    host.serve(_mesh())
    host.sync.sync(host.state)
    assert theirs.is_dir()


@pytest.mark.parametrize("bad", [
    {"name": "../escape.py", "content": "x", "sha256": hashlib.sha256(b"x").hexdigest()},
    {"name": "adapter.py", "content": "x", "sha256": "0" * 64},
])
def test_a_file_that_does_not_check_out_installs_nothing(host, bad):
    offer = _offer()
    offer["plugin"].append(bad)
    host.serve({**_mesh(), "chat": offer})
    host.sync.sync(host.state)
    assert not (host.env.parent / "plugins" / "beacon-chat").exists() and _chat_calls(host) == []


# --- the plugin -----------------------------------------------------------------

@pytest.fixture
def plugin(monkeypatch):
    """The plugin against a stand-in for Hermes's platform interface."""
    class Result:
        def __init__(self, success, message_id=None, error=None):
            self.success, self.message_id, self.error = success, message_id, error

    class Event:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Base:
        def __init__(self, config, platform):
            self.config, self.platform, self.got = config, platform, []

        def build_source(self, **kw):
            return kw

        async def handle_message(self, event):
            self.got.append(event)

    base = types.ModuleType("gateway.platforms.base")
    base.BasePlatformAdapter, base.MessageEvent, base.SendResult = Base, Event, Result
    base.MessageType = types.SimpleNamespace(TEXT="text")
    config = types.ModuleType("gateway.config")
    config.Platform, config.PlatformConfig = (lambda name: name), object
    for name, module in (("gateway", types.ModuleType("gateway")), ("gateway.platforms", types.ModuleType("gateway.platforms")),
                         ("gateway.config", config), ("gateway.platforms.base", base)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("BEACON_CHAT_URL", "https://beacon.test/mesh/alpha/chat")
    monkeypatch.setenv("BEACON_CHAT_TOKEN", "tok")
    monkeypatch.syspath_prepend(str(mesh.CHAT_PLUGIN_DIR.parent))
    sys.modules.pop("hermes_chat_plugin", None)
    sys.modules.pop("hermes_chat_plugin.adapter", None)
    module = importlib.import_module("hermes_chat_plugin.adapter")
    module.posted = []

    async def post(event):
        module.posted.append(event)

    monkeypatch.setattr(module, "_post", post)
    return module


def test_the_owners_message_reaches_hermes_and_is_receipted(plugin):
    adapter = plugin.BeaconChatAdapter(None)
    message = {"id": 7, "chat": "t3", "text": "yes", "written_at": 0, "quoted": "Shall I?\nReally?"}
    asyncio.run(adapter._deliver(message))
    asyncio.run(adapter._deliver(message))  # offered again after a lost receipt
    [event] = adapter.got
    assert event.source["chat_id"] == "t3" and event.source["user_id"] == "owner"
    assert event.text == "In reply to your message:\n> Shall I?\n> Really?\n\nyes"
    assert plugin.posted == [{"type": "receipt", "ids": [7]}] * 2


def test_a_late_message_says_so_and_a_command_is_left_alone(plugin):
    late = plugin.compose({"text": "restart it", "written_at": 1000.0}, now=1000.0 + 3600)
    assert late.startswith("(Written 1970-01-01 00:16 UTC, delivered late.)") and late.endswith("restart it")
    assert plugin.compose({"text": "/approve", "written_at": 1000.0, "quoted": "q"}, now=99999.0) == "/approve"


def test_what_the_agent_says_and_edits_goes_to_beacon(plugin):
    adapter = plugin.BeaconChatAdapter(None)
    sent = asyncio.run(adapter.send("t3", "Hel"))
    asyncio.run(adapter.edit_message("t3", sent.message_id, "Hello", finalize=True))
    asyncio.run(adapter.send("owner", "from a job"))
    first, edit, home = plugin.posted
    assert first["chat"] == "t3" and edit == {"type": "edit", "remote_id": sent.message_id, "text": "Hello", "final": True}
    assert home["chat"] == "home"
    assert asyncio.run(plugin._standalone_send(None, "owner", "cron"))["success"]


# --- other conversations --------------------------------------------------------

@pytest.fixture
def sessions(plugin):
    return plugin.sessions


ROWS = [
    {"id": "c1", "source": "cli", "preview": "mine"},
    {"id": "t1", "source": "telegram", "chat_type": "dm", "user_id": "11", "chat_id": "11"},
    {"id": "t2", "source": "telegram", "chat_type": "dm", "user_id": "22", "chat_id": "22", "preview": "a stranger"},
    {"id": "g1", "source": "telegram", "chat_type": "group", "user_id": "11", "chat_id": "-5"},
    {"id": "a1", "source": "a2a", "chat_type": "dm", "user_id": "alpha"},
    {"id": "j1", "source": "cron"},
    {"id": "b1", "source": "beacon", "chat_type": "dm", "user_id": "owner"},
]


class FakeStore:
    def __init__(self, copied=True):
        self.copies, self.copied = [], copied

    async def rows(self):
        return ROWS

    async def row(self, session_id):
        return next((r for r in ROWS if r["id"] == session_id), None)

    async def messages(self, session_id):
        return [{"role": "system", "content": "secret prompt"}, {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}, {"type": "image"}]},
                {"role": "tool", "content": "output"}, {"role": "assistant", "content": ""}]

    async def copy(self, parent, source):
        self.copies.append((parent["id"], source))
        return self.copied


def test_only_what_is_the_owners_is_listed(sessions):
    def ids(owner_ids):
        return [s["id"] for s in asyncio.run(sessions.listing(FakeStore(), frozenset(owner_ids), {}))["sessions"]]

    assert ids([]) == ["c1"]
    assert ids(["telegram:11"]) == ["c1", "t1"]  # not the group, not the stranger, not agents or jobs


def test_the_only_hint_is_the_agents_own_home_chat(sessions):
    found = asyncio.run(sessions.listing(FakeStore(), frozenset(), {"telegram": "11"}))
    assert found["hints"] == ["telegram:11"] and "stranger" not in str(found)


def test_what_is_not_theirs_can_be_neither_read_nor_copied(sessions):
    store = FakeStore()
    for forbidden in ("t2", "g1", "a1", "nope"):
        with pytest.raises(sessions.Unavailable):
            asyncio.run(sessions.read(store, frozenset({"telegram:11"}), forbidden))
        with pytest.raises(sessions.Unavailable):
            asyncio.run(sessions.copy(store, frozenset({"telegram:11"}), forbidden, "src"))
    assert store.copies == []
    said = asyncio.run(sessions.read(store, frozenset(), "c1"))["messages"]
    assert said == [{"role": "user", "text": "hello"}, {"role": "assistant", "text": "hi\n[image]"}]


def test_a_request_is_answered_even_when_hermes_breaks(plugin, monkeypatch):
    adapter = plugin.BeaconChatAdapter(None)
    monkeypatch.setattr(plugin.sessions, "backend", lambda adapter, key: (_ for _ in ()).throw(AttributeError("gone")))
    asyncio.run(adapter._answer({"id": "q1", "kind": "list", "session": "", "chat": ""}))
    assert plugin.posted == [{"type": "answer", "request": "q1", "error": "this Hermes could not do that (chat still works)"}]

    monkeypatch.setattr(plugin.sessions, "backend", lambda adapter, key: FakeStore())
    monkeypatch.setenv("BEACON_CHAT_OWNER_IDS", "telegram:11, signal:x")
    asyncio.run(adapter._answer({"id": "q2", "kind": "copy", "session": "t1", "chat": "t9"}))
    assert plugin.posted[-1] == {"type": "answer", "request": "q2", "data": {"ok": True}}


def test_the_plugin_refuses_other_commands_whatever_beacon_says(plugin):
    adapter = plugin.BeaconChatAdapter(None)
    for number, text in enumerate(["/yolo", " /approve always", "/resume x"]):
        asyncio.run(adapter._deliver({"id": number, "chat": "t1", "text": text}))
    assert adapter.got == [] and len(plugin.posted) == 3  # receipted, so not offered again
    assert chat_module.COMMANDS == plugin.COMMANDS
