"""Intake: a replayed delivery makes no second card, and each source signs with its own secret."""

import hashlib
import hmac
import json

import pytest

from backend import intake, opstore


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client, source, secret, body=None, **headers):
    body = json.dumps({"action": "opened"}).encode() if body is None else body
    return client.post(f"/intake/{source}", content=body, headers={"X-Hub-Signature-256": _sign(secret, body), **headers})


def _cards():
    with opstore.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM cards ORDER BY id")]


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setenv(intake.SECRET_ENV, "shared-secret")
    return "shared-secret"


# --- replays ---------------------------------------------------------------------------------


def test_a_replayed_request_makes_no_second_card_and_says_so(client, shared):
    body = json.dumps({"action": "opened", "n": 1}).encode()
    first = _post(client, "forge", shared, body)
    replay = _post(client, "forge", shared, body)       # the same bytes, the same valid signature

    assert first.status_code == 202 and replay.status_code == 200
    assert replay.json() == {"card_id": first.json()["card_id"], "duplicate": True}
    assert len(_cards()) == 1


def test_the_forges_delivery_id_decides_when_there_is_one(client, shared):
    one = _post(client, "forge", shared, b'{"n": 1}', **{"X-Gitea-Delivery": "d-1"})
    again = _post(client, "forge", shared, b'{"n": 2}', **{"X-Gitea-Delivery": "d-1"})    # same delivery, re-sent
    other = _post(client, "forge", shared, b'{"n": 1}', **{"X-Gitea-Delivery": "d-2"})    # same text, a new delivery
    assert (one.status_code, again.status_code, other.status_code) == (202, 200, 202)
    assert again.json()["card_id"] == one.json()["card_id"] and len(_cards()) == 2


def test_different_events_and_different_sources_are_not_replays(client, shared):
    assert _post(client, "forge", shared, b'{"n": 1}').status_code == 202
    assert _post(client, "forge", shared, b'{"n": 2}').status_code == 202
    assert _post(client, "monitor", shared, b'{"n": 1}').status_code == 202      # the same bytes from another source
    assert len(_cards()) == 3


def test_a_delivery_is_forgotten_after_the_retention_period(client, shared):
    body = b'{"n": 1}'
    _post(client, "forge", shared, body)
    with opstore.connect() as db:
        db.execute("UPDATE intake_deliveries SET received_at = ?", (1.0,))
    assert _post(client, "forge", shared, body).status_code == 202
    assert len(_cards()) == 2


def test_a_delivery_that_made_no_card_does_not_block_a_retry(client, shared):
    body = b'{"n": 1}'
    bad = _post(client, "Forge", shared, body)          # a malformed source: refused after the signature
    assert bad.status_code == 400
    with opstore.connect() as db:
        assert db.execute("SELECT count(*) FROM intake_deliveries").fetchone()[0] == 0
    assert _post(client, "forge", shared, body).status_code == 202


def test_a_refused_signature_leaves_no_trace(client, shared):
    body = b'{"n": 1}'
    assert _post(client, "forge", "wrong", body).status_code == 401
    with opstore.connect() as db:
        assert db.execute("SELECT count(*) FROM intake_deliveries").fetchone()[0] == 0
    assert _post(client, "forge", shared, body).status_code == 202


def test_the_claim_on_a_delivery_is_a_primary_key_so_the_second_of_two_loses(fleet):
    first = intake.receive("forge", "push", b"{}", "d-1")
    with pytest.raises(intake.Duplicate) as caught:
        intake.receive("forge", "push", b"{}", "d-1")
    assert caught.value.card_id == first["id"] and len(_cards()) == 1


# --- one secret per source -------------------------------------------------------------------


def test_a_source_with_its_own_secret_accepts_only_that_one(client, shared, monkeypatch):
    monkeypatch.setenv("BEACON_INTAKE_SECRET_FORGE", "forge-secret")
    monkeypatch.setenv("BEACON_INTAKE_SECRET_MAIL_IN", "mail-secret")

    assert _post(client, "forge", "forge-secret", b'{"a": 1}').status_code == 202
    assert _post(client, "forge", shared, b'{"a": 2}').status_code == 401        # the shared one no longer stands for forge
    assert _post(client, "forge", "mail-secret", b'{"a": 3}').status_code == 401  # nor another source's
    assert _post(client, "mail-in", "mail-secret", b'{"a": 4}').status_code == 202  # a dash is an underscore in the name
    assert _post(client, "mail-in", "forge-secret", b'{"a": 5}').status_code == 401
    assert _post(client, "monitor", shared, b'{"a": 6}').status_code == 202       # a source with none uses the shared one
    assert len(_cards()) == 3


def test_with_only_per_source_secrets_other_sources_do_not_exist(client, monkeypatch):
    monkeypatch.delenv(intake.SECRET_ENV, raising=False)
    monkeypatch.setenv("BEACON_INTAKE_SECRET_FORGE", "forge-secret")
    assert _post(client, "forge", "forge-secret").status_code == 202
    assert client.post("/intake/monitor", content=b"{}").status_code == 404      # nobody can sign for it: no such route
    assert _post(client, "monitor", "forge-secret").status_code == 404
