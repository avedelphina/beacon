"""Waits for a person count working hours; an urgent request counts the clock."""

import datetime
import zoneinfo

import pytest

from backend import notify, push, workflow, workhours
from tests.test_clearance_authority import OWEN, TOM, _ask, world  # noqa: F401 (fixture)

HOUR = 3600
BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")


def at(day, hour, month=11):
    """2026: 27 November is a Friday."""
    return datetime.datetime(2026, month, day, hour, tzinfo=BERLIN).timestamp()


@pytest.fixture
def office(monkeypatch):
    monkeypatch.setenv(workhours.ENV, "Mon-Fri 08:00-17:00 Europe/Berlin")


@pytest.fixture
def told(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_tell", lambda to, subject, body, urgent=False, **_: sent.append((sorted(to), subject, body, urgent)))
    return sent


def test_working_time_skips_nights_and_weekends(office):
    assert workhours.after(at(27, 16), 4 * HOUR) == at(30, 11)          # Friday 16:00 -> Monday 11:00
    assert workhours.after(at(28, 12), HOUR) == at(30, 9)               # a Saturday starts on Monday
    assert workhours.after(at(30, 5), HOUR) == at(30, 9)                # before opening
    assert workhours.after(at(30, 20), HOUR) == at(1, 9, month=12)      # after closing
    assert workhours.after(at(27, 16), 2 * workhours.day_seconds()) == at(1, 16, month=12)
    assert workhours.after(at(23, 16, month=10), 2 * HOUR) == at(26, 9, month=10)  # across the clock change


def test_the_window_setting(monkeypatch):
    monkeypatch.delenv(workhours.ENV, raising=False)
    assert workhours.day_seconds() == 9 * HOUR                          # 08:00-17:00 by default
    monkeypatch.setenv(workhours.ENV, "Sun-Thu 09:00-17:30 UTC")
    assert workhours.window()[0] == {6, 0, 1, 2, 3} and workhours.day_seconds() == 8.5 * HOUR
    monkeypatch.setenv(workhours.ENV, "always")
    assert workhours.after(100.0, HOUR) == 100.0 + HOUR
    for bad in ("08:00-17:00", "Mon-Fri 17:00-08:00 UTC", "Mon-Fri 08:00-17:00 Mars/Base", "Mon-Xyz 08:00-17:00 UTC"):
        monkeypatch.setenv(workhours.ENV, bad)
        with pytest.raises(ValueError, match=workhours.ENV):
            workhours.window()


def _create(card, now, **extra):
    return workflow.create(card_id=card, kind="approval", requester="worker", eligible_responder="human",
                           capability="change", clearance=4, title=f"Change {now} {len(extra)}", summary="Plan",
                           options=[], target={}, now=now, plan_revision="1", plan_hash="h", **extra)


def test_a_friday_request_waits_for_monday_and_an_urgent_one_does_not(world, office, told, monkeypatch):  # noqa: F811
    friday = at(27, 16)
    monkeypatch.setattr(workflow.time, "time", lambda: friday)
    plain = _create(world, friday)
    urgent = _create(world, friday, urgent_reason="The shop is down")
    assert plain["expires_at"] == at(1, 16, month=12) and urgent["expires_at"] == friday + 24 * HOUR
    assert told[-1][1].startswith("Beacon: URGENT: worker") and "The shop is down" in told[-1][2] and told[-1][3]
    assert "URGENT" not in told[-2][1] and not told[-2][3]

    told.clear()
    assert workflow.nudge_operators(now=friday + HOUR / 4) == 0
    assert workflow.nudge_operators(now=friday + HOUR) == 1             # only the urgent one
    assert "URGENT" in told[0][1] and "30 minutes" in told[0][2] and TOM in told[0][0] and told[0][3]
    assert workflow.nudge_operators(now=at(30, 10)) == 0
    assert workflow.nudge_operators(now=at(30, 11)) == 1
    assert "4 working hours" in told[-1][2] and told[-1][0] != [OWEN]


def test_urgent_needs_a_reason_and_a_person(world):  # noqa: F811
    with pytest.raises(workflow.WorkflowError, match="what breaks"):
        _create(world, workflow.time.time(), urgent_reason="  ")
    with pytest.raises(workflow.WorkflowError, match="agents answer at any hour"):
        workflow.create(card_id=world, kind="review", requester="worker", eligible_responder="other", capability="c",
                        clearance=3, title="t", summary="s", options=[], target={}, plan_revision="1", plan_hash="h",
                        urgent_reason="now")


def test_an_urgent_push_asks_the_service_to_wake_the_device(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "_private_key", lambda: object())
    monkeypatch.setattr(push, "subscriptions", lambda people: [{"endpoint": "e", "email": TOM}])
    monkeypatch.setattr(push, "deliver", lambda browser, title, urgent=False: sent.append(urgent))
    monkeypatch.setattr(notify, "configured", lambda: False)
    notify._tell({TOM}, "Beacon: x", "body", urgent=True)
    assert sent == [True]
