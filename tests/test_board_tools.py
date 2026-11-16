"""The agent's board read tools run, and respect the intake setting.

These call the registered tool functions themselves, so a signature mistake
(a tool body using `ctx` that its signature does not take) fails here, not
in an agent's conversation."""

from types import SimpleNamespace

import pytest

from backend import board, board_mcp, intake, store
from backend.schemas import Agent, Host


@pytest.fixture
def reader(fleet, monkeypatch):
    """An agent that calls the board tools; its author is its own id."""
    store.upsert_host(Host.model_validate({"id": "h-tools", "address": "10.0.0.8", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(Agent.model_validate({"id": "reader", "type": "hermes", "host": "h-tools",
                                             "reach": {"endpoint": "http://reader.test:9900"}}))
    monkeypatch.setattr(board_mcp, "author_for", lambda token: "reader")
    return SimpleNamespace(headers={"authorization": "Bearer agent-token"})


def _tool(name):
    return board_mcp.build()._tool_manager.get_tool(name).fn


def test_board_list_and_board_card_run_for_an_agent(reader):
    card = board.create_card("beacon", "Plain card")

    listed = _tool("board_list")(ctx=reader)
    assert [c["id"] for c in listed] == [card["id"]]
    assert _tool("board_card")(ctx=reader, card_id=card["id"])["id"] == card["id"]


def test_intake_cards_are_hidden_from_agents_until_the_setting_allows_them(reader):
    intake_card = intake.record("forge", "push", b"{}")
    board.create_card("beacon", "Plain card")

    assert intake_card["id"] not in [c["id"] for c in _tool("board_list")(ctx=reader)]
    with pytest.raises(store.NotFound):
        _tool("board_card")(ctx=reader, card_id=intake_card["id"])

    intake.set_visibility("operator@example.com", members=False, agents=True)
    assert intake_card["id"] in [c["id"] for c in _tool("board_list")(ctx=reader)]
    assert _tool("board_card")(ctx=reader, card_id=intake_card["id"])["id"] == intake_card["id"]
