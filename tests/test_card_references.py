"""Card references: structured pointers to work elsewhere (a PR, an issue, a ticket).

They are links plus an optional hand-typed state hint. Nothing here fetches, polls
or verifies anything outside Beacon."""

from types import SimpleNamespace

import pytest

from backend import board, board_mcp, members, store, workflow
from backend.schemas import Agent, Host

GITEA = dict(provider="gitea", kind="pull_request", external_id="org/repo#15",
             url="https://git.example.test/org/repo/pulls/15", label="Intake proposal")
GITHUB = dict(provider="github", kind="issue", external_id="acme/widget#7",
              url="https://github.example.test/acme/widget/issues/7", label="Widget crash")
MANTIS = dict(provider="mantis", kind="ticket", external_id="337",
              url="https://issues.example.test/view.php?id=337", label="Board tools fail")


def _card(title="Card"):
    return board.create_card("beacon", title)


def _events(card_id):
    return [e for e in board.get_card(card_id)["events"] if e["kind"] == "reference"]


def _workflow(card_id):
    binding = workflow.plan_binding({"card": card_id})
    return workflow.create(
        card_id=card_id, kind="decision", requester="beacon", eligible_responder="beacon",
        capability="ask", tier=1, title="Pick", summary="Pick one", options=["a"], target={}, **binding)


def test_a_gitea_pr_a_github_issue_and_a_mantis_ticket(fleet):
    card = _card()
    for fields in (GITEA, GITHUB, MANTIS):
        board.add_reference("beacon", card["id"], **fields)

    refs = board.get_card(card["id"])["references"]
    assert [(r["provider"], r["kind"], r["external_id"]) for r in refs] == [
        ("gitea", "pull_request", "org/repo#15"), ("github", "issue", "acme/widget#7"), ("mantis", "ticket", "337")]
    assert refs[0]["url"] == GITEA["url"] and refs[0]["label"] == "Intake proposal"
    assert refs[0]["last_seen_state"] is None and refs[0]["workflow_id"] is None


def test_an_unknown_provider_and_kind_need_no_schema_change_and_are_lowercased(fleet):
    card = _card()
    ref = board.add_reference("beacon", card["id"], **{**GITEA, "provider": "Forgejo-Next", "kind": "Merge.Request"})
    assert (ref["provider"], ref["kind"]) == ("forgejo-next", "merge.request")
    for bad in ("", " ", "has space", "x" * 41, "-leading"):
        with pytest.raises(ValueError):
            board.add_reference("beacon", card["id"], **{**GITEA, "provider": bad})


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,x", "ftp://git.example.test/x", "https://", "git.example.test/x",
    "https://user:secret@git.example.test/x", "https://user@git.example.test/x", "https://h.test/" + "x" * 2000, ""])
def test_unsafe_urls_are_refused(fleet, url):
    card = _card()
    with pytest.raises(ValueError):
        board.add_reference("beacon", card["id"], **{**GITEA, "url": url})
    assert board.get_card(card["id"])["references"] == []


def test_a_reference_may_name_only_a_workflow_on_the_same_card(fleet):
    card, other = _card("One"), _card("Two")
    mine, theirs = _workflow(card["id"]), _workflow(other["id"])

    ref = board.add_reference("beacon", card["id"], **GITEA, workflow_id=mine["id"])
    assert ref["workflow_id"] == mine["id"]
    with pytest.raises(ValueError, match="another card"):
        board.add_reference("beacon", card["id"], **GITHUB, workflow_id=theirs["id"])
    with pytest.raises(ValueError, match="does not exist"):
        board.add_reference("beacon", card["id"], **GITHUB, workflow_id=9999)
    with pytest.raises(ValueError, match="another card"):
        board.update_reference("beacon", card["id"], ref["id"], workflow_id=theirs["id"])

    cleared = board.update_reference("beacon", card["id"], ref["id"], workflow_id=None)
    assert cleared["workflow_id"] is None


def test_the_state_hint_is_dated_and_can_be_cleared(fleet):
    card = _card()
    ref = board.add_reference("beacon", card["id"], **GITEA, last_seen_state="open, 0 approvals")
    assert ref["last_seen_state"] == "open, 0 approvals" and ref["last_seen_at"] > 0

    stamped = board.add_reference("beacon", card["id"], **GITHUB, last_seen_state="closed", last_seen_at=1700000000)
    assert stamped["last_seen_at"] == 1700000000.0

    newer = board.update_reference("beacon", card["id"], ref["id"], last_seen_state="merged")
    assert newer["last_seen_state"] == "merged" and newer["last_seen_at"] >= ref["last_seen_at"]
    cleared = board.update_reference("beacon", card["id"], ref["id"], last_seen_state=None)
    assert cleared["last_seen_state"] is None and cleared["last_seen_at"] is None

    for bad in (-1, "yesterday", True):
        with pytest.raises(ValueError):
            board.add_reference("beacon", card["id"], **MANTIS, last_seen_at=bad)


def test_every_change_is_a_card_history_event_with_author_and_context(fleet):
    card = _card()
    ref = board.add_reference("alpha", card["id"], **GITEA, context_id="ctx-1")
    board.update_reference("bravo", card["id"], ref["id"], label="Renamed", context_id="ctx-2")
    board.update_reference("bravo", card["id"], ref["id"], label="Renamed")  # unchanged: no event
    board.remove_reference("alpha", card["id"], ref["id"], "ctx-3")

    events = _events(card["id"])
    assert [(e["author"], e["context_id"]) for e in events] == [("alpha", "ctx-1"), ("bravo", "ctx-2"), ("alpha", "ctx-3")]
    assert events[0]["body"].startswith("added gitea pull_request org/repo#15")
    assert events[1]["body"].startswith("changed") and "label" in events[1]["body"]
    assert events[2]["body"].startswith("removed")
    assert board.get_card(card["id"])["references"] == []


def test_a_change_counts_as_the_card_changing(fleet):
    card = _card()
    board.add_reference("beacon", card["id"], **GITEA)
    assert board.get_card(card["id"])["updated_at"] >= card["updated_at"]


def test_duplicates_and_missing_things_are_refused(fleet):
    card = _card()
    ref = board.add_reference("beacon", card["id"], **GITEA)
    with pytest.raises(ValueError, match="already has"):
        board.add_reference("beacon", card["id"], **GITEA)
    other = board.add_reference("beacon", card["id"], **GITHUB)
    with pytest.raises(ValueError, match="already has"):
        board.update_reference("beacon", card["id"], other["id"], **{k: GITEA[k] for k in ("provider", "kind", "external_id")})
    with pytest.raises(store.NotFound):
        board.add_reference("beacon", 9999, **GITEA)
    with pytest.raises(store.NotFound):
        board.update_reference("beacon", card["id"], 9999, label="x")
    with pytest.raises(store.NotFound):
        board.remove_reference("beacon", _card("Other")["id"], ref["id"])  # a reference of another card
    with pytest.raises(ValueError, match="unknown"):
        board.update_reference("beacon", card["id"], ref["id"], colour="red")


def test_a_card_holds_a_bounded_number_of_references(fleet):
    card = _card()
    for i in range(board.MAX_REFERENCES):
        board.add_reference("beacon", card["id"], **{**GITEA, "external_id": f"#{i}"})
    with pytest.raises(ValueError, match="at most"):
        board.add_reference("beacon", card["id"], **{**GITEA, "external_id": "#over"})


def test_links_still_work_and_are_not_touched_by_references(fleet):
    card = board.create_card("beacon", "With links", links=[{"url": "https://x.example.test/a", "label": "a"}])
    board.add_reference("beacon", card["id"], **GITEA)
    got = board.get_card(card["id"])
    assert got["links"] == [{"url": "https://x.example.test/a", "label": "a"}]
    assert len(got["references"]) == 1
    board.update_card("beacon", card["id"], state="doing")
    assert len(board.get_card(card["id"])["references"]) == 1


def test_rest_crud(client):
    card = _card()
    base = f"/api/board/cards/{card['id']}/references"

    made = client.post(base, json={**GITEA, "last_seen_state": "open", "context_id": "ctx-9"})
    assert made.status_code == 200
    ref = made.json()
    assert client.get(base).json() == [ref]
    assert client.get(f"/api/board/cards/{card['id']}").json()["references"] == [ref]

    patched = client.patch(f"{base}/{ref['id']}", json={"label": "New label"}).json()
    assert patched["label"] == "New label" and patched["last_seen_state"] == "open"  # the rest is left alone
    cleared = client.patch(f"{base}/{ref['id']}", json={"last_seen_state": None}).json()
    assert cleared["last_seen_state"] is None

    assert client.post(base, json={**GITHUB, "url": "javascript:alert(1)"}).status_code == 400
    assert client.post(base, json=GITEA).status_code == 400  # duplicate
    assert client.post("/api/board/cards/9999/references", json=GITEA).status_code == 404
    assert client.patch(f"{base}/9999", json={"label": "x"}).status_code == 404

    removed = client.delete(f"{base}/{ref['id']}?context_id=ctx-10")
    assert removed.status_code == 200 and removed.json()["id"] == ref["id"]
    assert client.get(base).json() == []
    assert [e["context_id"] for e in _events(card["id"])] == ["ctx-9", None, None, "ctx-10"]


@pytest.fixture
def reader(fleet, monkeypatch):
    """An agent that calls the board tools; its author is its own id."""
    store.upsert_host(Host.model_validate({"id": "h-refs", "address": "10.0.0.9", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(Agent.model_validate({"id": "reader", "type": "hermes", "host": "h-refs",
                                             "reach": {"endpoint": "http://reader.test:9900"}}))
    monkeypatch.setattr(board_mcp, "author_for", lambda token: "reader")
    return SimpleNamespace(headers={"authorization": "Bearer agent-token"})


def _tool(name):
    return board_mcp.build()._tool_manager.get_tool(name).fn


def test_the_board_mcp_tools_do_the_same(reader):
    card = _card()
    entry = _workflow(card["id"])

    ref = _tool("card_reference_add")(ctx=reader, card_id=card["id"], **GITEA, workflow_id=entry["id"],
                                      last_seen_state="open", context_id="ctx-1")
    assert _tool("card_references")(ctx=reader, card_id=card["id"]) == [ref]
    assert _tool("board_card")(ctx=reader, card_id=card["id"])["references"] == [ref]

    changed = _tool("card_reference_update")(ctx=reader, card_id=card["id"], reference_id=ref["id"],
                                             label="Renamed", workflow_id=0, last_seen_state="")
    assert changed["label"] == "Renamed" and changed["url"] == GITEA["url"]  # what was left out stays
    assert changed["workflow_id"] is None and changed["last_seen_state"] is None and changed["last_seen_at"] is None

    gone = _tool("card_reference_remove")(ctx=reader, card_id=card["id"], reference_id=ref["id"], context_id="ctx-2")
    assert gone["id"] == ref["id"]
    assert _tool("card_references")(ctx=reader, card_id=card["id"]) == []
    assert [(e["author"], e["context_id"]) for e in _events(card["id"])] == [
        ("reader", "ctx-1"), ("reader", None), ("reader", "ctx-2")]

    with pytest.raises(ValueError):
        _tool("card_reference_add")(ctx=reader, card_id=card["id"], **{**GITEA, "url": "data:text/html,x"})


def test_a_member_reaches_references_only_on_cards_they_may_see(fleet):
    mine = board.create_card("member@example.com", "Theirs")
    others = board.create_card("beacon", "Not theirs")
    for method, path in (("GET", "/references"), ("POST", "/references"), ("PATCH", "/references/1"),
                         ("DELETE", "/references/1")):
        assert members.check(method, f"/api/board/cards/{mine['id']}{path}", "member@example.com") is None
        denied = members.check(method, f"/api/board/cards/{others['id']}{path}", "member@example.com")
        assert denied is not None and denied[0] == 404


def test_the_board_list_carries_each_cards_references(client):
    card, other = _card()["id"], _card("Other")["id"]
    board.add_reference("beacon", card, **GITEA)
    board.add_reference("beacon", card, **MANTIS)
    listed = {c["id"]: c for c in client.get("/api/board").json()}
    assert [(r["provider"], r["external_id"], r["url"]) for r in listed[card]["references"]] == [
        ("gitea", "org/repo#15", GITEA["url"]), ("mantis", "337", MANTIS["url"])]
    assert listed[other]["references"] == []
    assert all("references" in c for c in board.list_cards())                 # the direct call carries them too
