"""Cards relate: one optional parent, and `depends_on` edges from which `blocked_by` is computed.
No cycles, no self-links, and a person or agent sees only the related cards they may see."""

import sqlite3
from types import SimpleNamespace

import pytest

from backend import board, board_mcp, intake, members, opstore, store
from backend.schemas import Agent, Host


def _card(title="Card", author="beacon"):
    return board.create_card(author, title)["id"]


def _history(card_id):
    return [(e["author"], e["body"], e["context_id"]) for e in board.get_card(card_id)["events"] if e["kind"] == "relation"]


def test_existing_cards_have_stable_empty_relationships(fleet):
    card = board.get_card(_card())
    assert (card["parent_id"], card["parent"], card["children"], card["depends_on"], card["blocked_by"], card["blocks"]) \
        == (None, None, [], [], [], [])
    assert board.list_cards()[0]["blocked_by_count"] == 0 and board.list_cards()[0]["parent_id"] is None


def test_a_parent_and_its_children(fleet):
    parent, one, two = _card("Review"), _card("Check A"), _card("Check B")
    board.set_parent("alpha", one, parent, "ctx-1")
    got = board.set_parent("alpha", two, parent)

    assert got["parent_id"] == parent and got["parent"]["title"] == "Review"
    detail = board.get_card(parent)
    assert [c["id"] for c in detail["children"]] == [one, two]
    assert _history(one) == [("alpha", "part of #%d: Review" % parent, "ctx-1")]

    freed = board.set_parent("alpha", one, None)
    assert freed["parent_id"] is None and [c["id"] for c in board.get_card(parent)["children"]] == [two]
    assert _history(one)[-1][1] == f"no longer part of #{parent}"
    board.set_parent("alpha", one, None)  # already free: nothing happens, nothing is recorded
    assert len(_history(one)) == 2


def test_parent_self_links_missing_cards_and_loops_are_refused(fleet):
    a, b, c = _card("A"), _card("B"), _card("C")
    with pytest.raises(ValueError, match="own parent"):
        board.set_parent("beacon", a, a)
    with pytest.raises(store.NotFound):
        board.set_parent("beacon", a, 9999)
    with pytest.raises(store.NotFound):
        board.set_parent("beacon", 9999, a)
    for bad in ("1", 1.5, True):
        with pytest.raises(ValueError, match="card number"):
            board.set_parent("beacon", a, bad)

    board.set_parent("beacon", b, a)   # b is part of a
    board.set_parent("beacon", c, b)   # c is part of b
    with pytest.raises(ValueError, match="loop"):
        board.set_parent("beacon", a, c)
    with pytest.raises(ValueError, match="loop"):
        board.set_parent("beacon", a, b)
    assert board.get_card(a)["parent_id"] is None and len(_history(a)) == 0


def test_blocked_by_is_computed_from_unfinished_dependencies(fleet):
    ship, build, review = _card("Ship"), _card("Build"), _card("Review")
    board.add_dependency("alpha", ship, build, "ctx-1")
    board.add_dependency("alpha", ship, review)

    detail = board.get_card(ship)
    assert [c["id"] for c in detail["depends_on"]] == [build, review]
    assert [c["id"] for c in detail["blocked_by"]] == [build, review]
    assert [c["id"] for c in board.get_card(build)["blocks"]] == [ship]
    assert next(c for c in board.list_cards() if c["id"] == ship)["blocked_by_count"] == 2
    assert _history(ship)[0] == ("alpha", f"depends on #{build}: Build", "ctx-1")

    board.update_card("beacon", build, state="done")
    detail = board.get_card(ship)
    assert [c["id"] for c in detail["blocked_by"]] == [review]          # only the unfinished one
    assert [c["id"] for c in detail["depends_on"]] == [build, review]   # the edge stays
    board.update_card("beacon", review, state="done")
    assert board.get_card(ship)["blocked_by"] == []                      # gone when the last one is done
    board.update_card("beacon", review, state="doing")
    assert [c["id"] for c in board.get_card(ship)["blocked_by"]] == [review]  # and back if it is reopened


def test_dependency_self_links_duplicates_missing_and_cycles_are_refused(fleet):
    a, b, c = _card("A"), _card("B"), _card("C")
    with pytest.raises(ValueError, match="itself"):
        board.add_dependency("beacon", a, a)
    with pytest.raises(store.NotFound):
        board.add_dependency("beacon", a, 9999)
    with pytest.raises(store.NotFound):
        board.add_dependency("beacon", 9999, a)
    board.add_dependency("beacon", a, b)
    with pytest.raises(ValueError, match="already depends"):
        board.add_dependency("beacon", a, b)
    board.add_dependency("beacon", b, c)
    with pytest.raises(ValueError, match="loop"):
        board.add_dependency("beacon", b, a)     # directly back
    with pytest.raises(ValueError, match="loop"):
        board.add_dependency("beacon", c, a)     # round a longer way
    assert board.get_card(c)["depends_on"] == []


def test_removing_a_dependency(fleet):
    a, b = _card("A"), _card("B")
    board.add_dependency("beacon", a, b)
    board.remove_dependency("alpha", a, b, "ctx-2")
    assert board.get_card(a)["depends_on"] == [] and _history(a)[-1] == ("alpha", f"no longer depends on #{b}", "ctx-2")
    with pytest.raises(store.NotFound):
        board.remove_dependency("beacon", a, b)
    board.add_dependency("beacon", b, a)         # and the reverse direction is free again


def test_a_relationship_change_counts_as_the_card_changing(fleet):
    a, b = _card("A"), _card("B")
    before = board.get_card(a)["updated_at"]
    board.add_dependency("beacon", a, b)
    assert board.get_card(a)["updated_at"] >= before


def test_cards_from_before_relationships_get_them(fleet, monkeypatch, tmp_path):
    monkeypatch.setattr(opstore, "STATE_DIR", tmp_path / "old")
    (tmp_path / "old").mkdir()
    with sqlite3.connect(tmp_path / "old" / "beacon.db") as db:
        db.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                   " body TEXT NOT NULL DEFAULT '', project TEXT, assignee TEXT, state TEXT NOT NULL,"
                   " created_by TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        db.execute("INSERT INTO cards (title, state, created_by, created_at, updated_at) VALUES ('old', 'todo', 'beacon', 1, 1)")
        db.execute("INSERT INTO cards (title, state, created_by, created_at, updated_at) VALUES ('older', 'done', 'beacon', 1, 1)")
    card = board.get_card(1)
    assert (card["parent_id"], card["children"], card["blocked_by"]) == (None, [], [])
    board.set_parent("beacon", 1, 2)
    board.add_dependency("beacon", 2, 1)
    assert board.get_card(1)["parent"]["title"] == "older"


def test_rest_does_the_same(client):
    a, b, c = _card("A"), _card("B"), _card("C")
    base = f"/api/board/cards/{a}"

    assert client.put(f"{base}/parent", json={"parent_id": b, "context_id": "ctx-1"}).json()["parent"]["id"] == b
    assert client.put(f"{base}/parent", json={"parent_id": a}).status_code == 400
    assert client.put(f"/api/board/cards/{b}/parent", json={"parent_id": a}).status_code == 400   # loop
    assert client.put(f"{base}/parent", json={"parent_id": 9999}).status_code == 404
    assert client.put(f"{base}/parent", json={"parent_id": None}).json()["parent"] is None

    added = client.post(f"{base}/dependencies", json={"depends_on": c, "context_id": "ctx-2"}).json()
    assert [x["id"] for x in added["blocked_by"]] == [c]
    assert client.post(f"{base}/dependencies", json={"depends_on": c}).status_code == 400
    assert client.post(f"/api/board/cards/{c}/dependencies", json={"depends_on": a}).status_code == 400
    assert client.get(base).json()["blocked_by"][0]["id"] == c
    removed = client.delete(f"{base}/dependencies/{c}?context_id=ctx-3").json()
    assert removed["blocked_by"] == []
    assert client.delete(f"{base}/dependencies/{c}").status_code == 404
    assert [e[2] for e in _history(a)] == ["ctx-1", None, "ctx-2", "ctx-3"]


@pytest.fixture
def reader(fleet, monkeypatch):
    store.upsert_host(Host.model_validate({"id": "h-rel", "address": "10.0.0.9", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(Agent.model_validate({"id": "reader", "type": "hermes", "host": "h-rel",
                                             "reach": {"endpoint": "http://reader.test:9900"}}))
    monkeypatch.setattr(board_mcp, "author_for", lambda token: "reader")
    return SimpleNamespace(headers={"authorization": "Bearer agent-token"})


def _tool(name):
    return board_mcp.build()._tool_manager.get_tool(name).fn


def test_the_board_mcp_tools_do_the_same(reader):
    a, b, c = _card("A"), _card("B"), _card("C")
    assert _tool("card_set_parent")(ctx=reader, card_id=a, parent_id=b, context_id="ctx-1")["parent"]["id"] == b
    assert _tool("card_set_parent")(ctx=reader, card_id=a)["parent"] is None
    got = _tool("card_add_dependency")(ctx=reader, card_id=a, depends_on=c)
    assert [x["id"] for x in got["blocked_by"]] == [c]
    assert [x["id"] for x in _tool("board_card")(ctx=reader, card_id=a)["blocked_by"]] == [c]
    with pytest.raises(ValueError, match="loop"):
        _tool("card_add_dependency")(ctx=reader, card_id=c, depends_on=a)
    assert _tool("card_remove_dependency")(ctx=reader, card_id=a, depends_on=c)["blocked_by"] == []
    assert {e[0] for e in _history(a)} == {"reader"}


def test_an_agent_does_not_see_an_intake_card_among_related_cards(reader):
    plain, event = _card("Plain"), intake.record("forge", "push", b"{}")["id"]
    board.add_dependency("beacon", plain, event)
    board.set_parent("beacon", event, plain)

    seen = _tool("board_card")(ctx=reader, card_id=plain)
    assert seen["depends_on"] == [] and seen["blocked_by"] == [] and seen["children"] == []
    assert [c["id"] for c in board.get_card(plain)["blocked_by"]] == [event]  # the record itself is whole
    with pytest.raises(store.NotFound):
        _tool("card_add_dependency")(ctx=reader, card_id=plain, depends_on=event)
    intake.set_visibility("op@example.com", members=False, agents=True)
    assert [c["id"] for c in _tool("board_card")(ctx=reader, card_id=plain)["blocked_by"]] == [event]


def test_a_member_sees_and_relates_only_cards_they_may_see(fleet, monkeypatch):
    from tests.test_members import ANN, BOB, TOM, as_person  # noqa: F401
    from backend import auth, mesh
    from tests.conftest import make_host
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({TOM}))
    store.upsert_host(make_host("h1"))
    store.upsert_agent(Agent.model_validate({"id": "a1", "type": "hermes", "host": "h1", "owner": ANN,
                                             "reach": {"endpoint": "http://100.64.0.9:9900", "board": True}}))
    mine, mine2 = _card("Mine", ANN), _card("Mine too", ANN)
    secret = _card("Not for Ann", BOB)
    board.add_dependency("beacon", mine, secret)
    board.set_parent("beacon", secret, mine)
    ann = as_person(ANN)

    seen = ann.get(f"/api/board/cards/{mine}").json()
    assert seen["depends_on"] == [] and seen["blocked_by"] == [] and seen["children"] == []
    assert board.get_card(mine)["blocked_by"][0]["id"] == secret  # the record itself is whole
    assert ann.post(f"/api/board/cards/{mine}/dependencies", json={"depends_on": secret}).status_code == 404
    assert ann.put(f"/api/board/cards/{mine}/parent", json={"parent_id": secret}).status_code == 404
    assert ann.put(f"/api/board/cards/{secret}/parent", json={"parent_id": mine}).status_code == 404   # not their card
    ok = ann.post(f"/api/board/cards/{mine}/dependencies", json={"depends_on": mine2})
    assert ok.status_code == 200 and [c["id"] for c in ok.json()["blocked_by"]] == [mine2]
    assert ann.delete(f"/api/board/cards/{mine}/dependencies/{mine2}").status_code == 200
    assert members.check("PUT", f"/api/board/cards/{mine}/parent", ANN) is None
