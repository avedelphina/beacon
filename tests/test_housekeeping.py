"""What Beacon does by the clock: the scheduler's tick reminds every agent and escalates
stale cards without waiting for a pull, and expires workflow requests, including those
waiting for their requester to send a changed plan."""

import time

import pytest

from backend import auth, board, mesh, notify, opstore, policy, scheduler, store, workflow
from backend.schemas import Agent, Host

DAY = 86400


def _agent(id_):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1",
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "board": True}})


@pytest.fixture
def crew(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("alpha"), _agent("bravo")):
        store.upsert_agent(agent)
    policy.save('rules:\n  - {from: ["*"], to: ["*"]}\n', "test")
    monkeypatch.setattr(board.mailbox, "deliver_pending", lambda agent: None)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"op@example.com"}))
    monkeypatch.setattr(workflow.notify, "workflow_created", lambda *a, **k: None)
    monkeypatch.setattr(workflow.notify, "workflow_answered", lambda *a, **k: None)
    emails = []
    monkeypatch.setattr(notify, "_tell", lambda to, subject, body: emails.append((sorted(to), subject, body)))
    return emails


def _stale(title, assignee, at=0.0):
    card = board.create_card("beacon", title, assignee=assignee)
    with opstore.connect() as db:
        db.execute("UPDATE cards SET created_at = ?, updated_at = ? WHERE id = ?", (at, at, card["id"]))
        db.execute("DELETE FROM mailbox")
    return card["id"]


def _reminders(agent):
    with opstore.connect() as db:
        return [r["body"] for r in db.execute("SELECT body FROM mailbox WHERE recipient = ? AND body LIKE"
                                               " 'Reminder from the Beacon board%' ORDER BY id", (agent,))]


def test_the_scheduler_reminds_every_agent_with_nobody_pulling(crew):
    one, two = _stale("Rotate keys", "alpha"), _stale("Check backups", "bravo")

    board.remind(None, now=1.1 * DAY)

    assert f"#{one} " in _reminders("alpha")[0] and _reminders("bravo")[0].count(f"#{two} ") == 1
    assert len(_reminders("alpha")) == 1 and len(_reminders("bravo")) == 1
    board.remind(None, now=1.2 * DAY)              # once per remind_after, however often the clock runs
    assert len(_reminders("alpha")) == 1 and crew == []


def test_the_scheduler_escalates_without_any_pull_and_only_once(crew):
    card = _stale("Rotate keys", "alpha")
    for day in (1.1, 2.2, 3.3, 4.4, 9):
        board.remind(None, now=day * DAY)
    assert len(_reminders("alpha")) == 2           # two reminders, as on a pull
    assert len(crew) == 1 and "alpha was reminded and nothing changed" in crew[0][2]
    assert [e["kind"] for e in board.get_card(card)["events"] if e["kind"] in ("reminder", "escalation")] == \
        ["reminder", "reminder", "escalation"]


def test_a_pull_still_reminds_only_the_puller(crew):
    _stale("Rotate keys", "alpha")
    _stale("Check backups", "bravo")
    board.remind("alpha", now=1.1 * DAY)
    assert len(_reminders("alpha")) == 1 and _reminders("bravo") == []


def test_the_tick_runs_housekeeping_unless_it_is_a_dry_run(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(scheduler, "housekeeping", lambda: calls.append("ran"))
    monkeypatch.setattr(scheduler.cron_store, "list_cron_jobs", lambda: [])
    assert scheduler.main(["tick", "--utc"]) == 0
    assert scheduler.main(["tick", "--utc", "--dry-run"]) == 0
    assert calls == ["ran"]
    assert capsys.readouterr().out.count("[]") == 2  # the jobs report is unchanged


def test_housekeeping_does_both_and_one_failing_does_not_stop_the_other(monkeypatch, capsys):
    ran = []
    def expiry_fails():
        ran.append("expiry")
        raise RuntimeError("db busy")
    monkeypatch.setattr(workflow, "expire_due", expiry_fails)
    monkeypatch.setattr(board, "remind", lambda: ran.append("reminders"))
    scheduler.housekeeping()
    assert ran == ["expiry", "reminders"]
    assert "workflow expiry failed: db busy" in capsys.readouterr().err


def _review(card_id, **extra):
    binding = workflow.plan_binding({"card": card_id})
    request = workflow.create(card_id=card_id, kind="review", requester="beacon", eligible_responder="human",
                              capability="push_config", tier=3, title="Review it", summary="Exact plan",
                              options=[], target={}, **extra, **binding)
    return request, binding


def _set_expiry(request_id, at):
    with opstore.connect() as db:
        db.execute("UPDATE workflow_requests SET expires_at = ? WHERE id = ?", (at, request_id))


def test_the_scheduler_expires_a_request_nobody_read(crew):
    card = board.create_card("beacon", "Push config")["id"]
    request, _ = _review(card)
    _set_expiry(request["id"], time.time() - 1)

    assert workflow.expire_due() == 1
    assert workflow.expire_due() == 0
    assert workflow.get(request["id"])["state"] == "expired"
    assert board.get_card(card)["events"][-1]["body"].endswith(": expired without an answer")


def test_asking_for_changes_gives_the_requester_a_fresh_window(crew):
    card = board.create_card("beacon", "Push config")["id"]
    request, binding = _review(card)
    _set_expiry(request["id"], time.time() + 60)   # the responder took nearly all of the time

    workflow.respond(request["id"], actor="human", human=True, action="changes_requested",
                     response={"note": "add a canary"}, **binding)

    left = workflow.get(request["id"])["expires_at"] - time.time()
    assert workflow.CHANGES_WINDOW_SECONDS - 10 < left <= workflow.CHANGES_WINDOW_SECONDS


def test_a_request_waiting_for_a_revision_expires_too(crew):
    card = board.create_card("beacon", "Push config")["id"]
    request, binding = _review(card)
    workflow.respond(request["id"], actor="human", human=True, action="changes_requested",
                     response={"note": "add a canary"}, **binding)
    assert workflow.expire_due() == 0               # inside its window

    _set_expiry(request["id"], time.time() - 1)
    assert workflow.expire_due() == 1
    assert workflow.get(request["id"])["state"] == "expired"
    assert board.get_card(card)["events"][-1]["body"].endswith(": expired, no changed plan was sent")
    with pytest.raises(workflow.WorkflowError, match="expired"):
        workflow.revise(request["id"], actor="beacon", **workflow.plan_binding({"revised": 1}))


def test_a_revision_in_time_still_works(crew):
    card = board.create_card("beacon", "Push config")["id"]
    request, binding = _review(card)
    workflow.respond(request["id"], actor="human", human=True, action="changes_requested",
                     response={"note": "add a canary"}, **binding)
    revised = workflow.revise(request["id"], actor="beacon", summary="Now with a canary",
                              **workflow.plan_binding({"revised": 1}))
    assert revised["state"] == "pending"
