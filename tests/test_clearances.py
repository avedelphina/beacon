import pytest

from backend.clearances import CAPABILITY_CLEARANCES, Clearance, requires_confirm, clearance_for


@pytest.mark.parametrize("capability", list(CAPABILITY_CLEARANCES))
def test_every_registered_capability_resolves(capability):
    clearance, rationale = clearance_for(capability)
    assert isinstance(clearance, Clearance)
    assert rationale  # every assignment carries its own reasoning


def test_unknown_capability_raises():
    with pytest.raises(KeyError):
        clearance_for("delete_everything")


def test_read_only_capabilities_never_need_confirm():
    for capability in ("status", "logs", "list_plugins", "reconcile_check", "config_diff"):
        clearance, _ = clearance_for(capability)
        assert clearance < Clearance.CL2
        assert not requires_confirm(clearance)


def test_t2_and_above_require_confirm():
    for capability in ("restart", "restart_agent", "apply_fix", "push_config", "update_plugin", "deploy", "update_agent", "decommission"):
        clearance, _ = clearance_for(capability)
        assert clearance >= Clearance.CL2
        assert requires_confirm(clearance)


def test_decommission_escalates_only_on_purge_or_remove_user():
    plain, _ = clearance_for("decommission")
    purge, _ = clearance_for("decommission", purge=True)
    remove_user, _ = clearance_for("decommission", remove_user=True)
    both, _ = clearance_for("decommission", purge=True, remove_user=True)

    assert plain == Clearance.CL4
    assert purge == Clearance.CL5
    assert remove_user == Clearance.CL5
    assert both == Clearance.CL5


def test_decommission_false_flags_do_not_escalate():
    clearance, _ = clearance_for("decommission", purge=False, remove_user=False)
    assert clearance == Clearance.CL4


# --- the rename: tier became clearance (CL0 to CL5) ---------------------------------------------------


def test_the_names_are_the_operating_models(client):
    from backend import clearances
    assert [c.name for c in clearances.Clearance] == ["CL0", "CL1", "CL2", "CL3", "CL4", "CL5"]
    assert clearances.CLEARANCE_LABELS[clearances.Clearance.CL5] == "Human-only"
    listed = client.get("/api/clearances").json()
    assert listed and all({"clearance", "clearance_label", "rationale"} <= set(v) for v in listed.values())
    assert all(v["clearance"].startswith("CL") for v in listed.values())
    assert client.get("/api/tiers").status_code in (404, 405)                # the old route is gone


def test_a_database_from_before_the_rename_keeps_its_rows_under_the_new_names(fleet, monkeypatch, tmp_path):
    import sqlite3
    from backend import opstore, workflow
    monkeypatch.setattr(opstore, "STATE_DIR", tmp_path / "old")
    (tmp_path / "old").mkdir()
    with sqlite3.connect(tmp_path / "old" / "beacon.db") as db:
        db.execute("CREATE TABLE workflow_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, card_id INTEGER NOT NULL,"
                   " kind TEXT NOT NULL, state TEXT NOT NULL, requester TEXT NOT NULL, eligible_responder TEXT NOT NULL,"
                   " capability TEXT NOT NULL, tier INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,"
                   " options_json TEXT NOT NULL DEFAULT '[]', response_json TEXT, target_json TEXT NOT NULL DEFAULT '{}',"
                   " plan_revision TEXT NOT NULL, plan_hash TEXT NOT NULL, expires_at REAL NOT NULL,"
                   " created_at REAL NOT NULL, updated_at REAL NOT NULL, responded_at REAL)")
        db.execute("INSERT INTO workflow_requests (card_id, kind, state, requester, eligible_responder, capability, tier,"
                   " title, summary, plan_revision, plan_hash, expires_at, created_at, updated_at)"
                   " VALUES (1, 'review', 'pending', 'a', 'human', 'push_config', 3, 't', 's', 'r', 'h', 9e9, 1, 1)")
        db.execute("CREATE TABLE host_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL,"
                   " via TEXT NOT NULL, capability TEXT NOT NULL, tier TEXT NOT NULL, target TEXT, detail TEXT NOT NULL,"
                   " outcome TEXT NOT NULL)")
        db.execute("INSERT INTO host_actions (at, actor, via, capability, tier, target, detail, outcome)"
                   " VALUES (1, 'tom', 'session', 'restart', 'T2', 'a1', 'd', 'requested')")
    assert workflow.get(1)["clearance"] == 3 and "tier" not in workflow.get(1)
    with opstore.connect() as db:
        assert db.execute("SELECT clearance FROM host_actions").fetchone()[0] == "CL2"
        assert "tier" not in {r[1] for r in db.execute("PRAGMA table_info(host_actions)")}


def test_the_old_names_are_still_accepted_on_input_for_now(client, fleet):
    from backend import board, workflow
    from backend.schemas import AgentClient
    card = board.create_card("beacon", "Card")["id"]
    body = {"kind": "question", "eligible_responder": "human", "capability": "ask", "title": "t", "summary": "s",
            "plan_revision": "r", "plan_hash": "h"}
    old = client.post(f"/api/board/cards/{card}/workflows", json={**body, "tier": 1})
    new = client.post(f"/api/board/cards/{card}/workflows", json={**body, "clearance": 1})
    assert old.status_code == new.status_code == 200 and old.json()["clearance"] == new.json()["clearance"] == 1
    assert "tier" not in old.json()                                           # answers use the new name only
    assert AgentClient.model_validate({"max_tier": 2}).max_clearance == 2      # a record saved before the rename
    assert AgentClient.model_validate({"max_clearance": 2}).model_dump()["max_clearance"] == 2
    assert "max_tier" not in AgentClient().model_dump()


def test_the_board_tool_still_takes_the_old_name_and_asks_for_one_of_them(fleet, monkeypatch):
    from types import SimpleNamespace
    from backend import board, board_mcp, workflow
    monkeypatch.setattr(board_mcp, "author_for", lambda token: "beacon")
    card = board.create_card("beacon", "Card")["id"]
    create = board_mcp.build()._tool_manager.get_tool("workflow_create").fn
    ctx = SimpleNamespace(headers={"authorization": "Bearer t"})
    ask = dict(ctx=ctx, card_id=card, kind="question", eligible_responder="human", capability="ask", title="t", summary="s")
    assert create(**ask, tier=1)["clearance"] == 1 and create(**ask, clearance=1)["clearance"] == 1
    with pytest.raises(workflow.WorkflowError, match="clearance is required"):
        create(**ask)
