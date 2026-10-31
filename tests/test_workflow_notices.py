"""Workflow notices: agents hear through the mailbox, people by email (when a
mail server is configured). Best-effort: nothing here may stop a request."""

import smtplib

import pytest

from backend import auth, board, mailbox, mesh, notify, opstore, store, workflow
from backend.schemas import Agent, Host


class FakeSMTP:
    """Stands in for smtplib.SMTP; every instance records what it was asked."""
    sent: list = []
    logins: list = []
    starttls_calls = 0
    fail = False

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.fail:
            raise OSError("connection refused")
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSMTP.starttls_calls += 1

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.fixture
def world(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, "https://beacon.test")
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", frozenset({"op1@example.com", "op2@example.com"}))
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent_id, owner in (("asker", None), ("helper", None), ("kid-bot", "parent@example.com"),
                            ("other-bot", "stranger@example.com")):
        store.upsert_agent(Agent.model_validate({"id": agent_id, "type": "hermes", "host": "h1", "owner": owner,
            "reach": {"endpoint": f"http://{agent_id}.test:9900", "board": True}}))
    monkeypatch.setattr(mailbox, "deliver_pending", lambda *_: None)  # hold notices; read them from the table
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.starttls_calls, FakeSMTP.fail = [], [], 0, False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", type("FakeSSL", (FakeSMTP, smtplib.SMTP_SSL), {}))
    notify._sent.clear()
    return None


@pytest.fixture
def mail(world, monkeypatch):
    for key, value in {"BEACON_SMTP_HOST": "smtp.test", "BEACON_SMTP_USER": "beacon@example.com",
                       "BEACON_SMTP_PASSWORD": "secret-pw"}.items():
        monkeypatch.setenv(key, value)


def _ask(requester="asker", responder="human", summary="Expose the dashboard on the tailnet.", **extra):
    card = board.create_card("beacon", "Card")
    binding = workflow.plan_binding({"capability": "ask"})
    return workflow.create(card_id=card["id"], kind="approval", requester=requester, eligible_responder=responder,
                           capability="ask", tier=2, title=extra.pop("title", "Expose it"), summary=summary,
                           options=[], target=extra.pop("target", {}), **binding, **extra), binding


def _notices(to):
    with opstore.connect() as db:
        return [r["body"] for r in db.execute(
            "SELECT body FROM mailbox WHERE recipient = ? AND kind = 'notice' ORDER BY id", (to,))]


def test_an_agent_that_is_asked_gets_a_notice_and_a_person_is_not_a_mailbox_recipient(world):
    request, _ = _ask(responder="helper", summary="please review")

    body, = _notices("helper")
    assert f"workflow request #{request['id']}" in body and "asker asks you for an approval" in body
    assert "please review" in body and "workflow_respond" in body
    assert _notices("asker") == []  # the requester is not told about its own request


def _mailbox_rows():
    with opstore.connect() as db:
        return db.execute("SELECT count(*) FROM mailbox").fetchone()[0]


def test_no_notice_for_a_person_or_human_or_for_asking_yourself(world):
    _ask(responder="human")
    _ask(responder="op1@example.com")
    _ask(requester="asker", responder="asker")
    assert _mailbox_rows() == 0


def test_the_requester_hears_the_answer(world):
    request, binding = _ask(responder="human")

    workflow.respond(request["id"], actor="op1@example.com", human=True, action="approved",
                     response={"note": "fine, tailnet only"}, **binding)

    body, = _notices("asker")
    assert f"#{request['id']}" in body and "now approved" in body and "op1@example.com" in body
    assert "tailnet only" in body and "exactly the plan" in body


def test_a_rejection_is_reported_without_the_approval_hint(world):
    request, binding = _ask(responder="human")
    workflow.respond(request["id"], actor="op1@example.com", human=True, action="rejected", response=None, **binding)
    body, = _notices("asker")
    assert "now rejected" in body and "exactly the plan" not in body


def test_answering_your_own_request_tells_nobody(world):
    request, binding = _ask(requester="asker", responder="asker")
    workflow.respond(request["id"], actor="asker", human=False, action="approved", response=None, **binding)
    assert _mailbox_rows() == 0


def test_email_goes_to_every_operator_and_the_owners_involved_for_human(mail):
    _ask(requester="kid-bot", responder="human", summary="Line one.\nLine two.")

    to = sorted(m["To"] for m in FakeSMTP.sent)
    assert to == ["op1@example.com", "op2@example.com", "parent@example.com"]  # not the other bot's owner
    message = FakeSMTP.sent[0]
    text = message.get_content()
    assert message["From"] == "beacon@example.com" and "kid-bot" in message["Subject"]
    assert "> Line one." in text and "> Line two." in text
    assert "https://beacon.test" in text and "written by the requester" in text
    assert FakeSMTP.logins == [("beacon@example.com", "secret-pw")] and FakeSMTP.starttls_calls == 1


def test_a_named_person_is_the_only_one_emailed(mail):
    _ask(requester="kid-bot", responder="PARENT@example.com")
    assert [m["To"] for m in FakeSMTP.sent] == ["parent@example.com"]


def test_a_target_agents_owner_is_asked_too(mail):
    _ask(requester="asker", responder="human", target={"agent_id": "kid-bot"})
    assert "parent@example.com" in [m["To"] for m in FakeSMTP.sent]


def test_no_email_without_a_mail_server_or_for_an_agent_responder(world, mail, monkeypatch):
    _ask(responder="helper")
    assert FakeSMTP.sent == []
    monkeypatch.delenv("BEACON_SMTP_HOST")
    _ask(responder="human")
    assert FakeSMTP.sent == []


def test_an_agent_cannot_fill_an_inbox(mail):
    for i in range(notify.MAX_PER_HOUR + 5):
        _ask(responder="op1@example.com", title=f"Ask {i}")
    assert len(FakeSMTP.sent) == notify.MAX_PER_HOUR


def test_hostile_text_cannot_inject_headers_or_leak_the_plan(mail):
    request, _ = _ask(responder="human", summary="x" * 5000 + "\nBcc: evil@example.com")
    message = FakeSMTP.sent[0]
    assert "\n" not in message["Subject"] and message["Bcc"] is None
    text = message.get_content()
    assert request["plan_hash"] not in text and len(text) < 2500
    assert "[cut]" in text


def test_a_mail_failure_never_stops_the_request(mail, caplog):
    FakeSMTP.fail = True
    request, _ = _ask(responder="human")
    assert workflow.get(request["id"])["state"] == "pending"


def test_port_465_uses_ssl_and_none_skips_starttls(mail, monkeypatch):
    monkeypatch.setenv("BEACON_SMTP_PORT", "465")
    _ask(responder="op1@example.com")
    assert FakeSMTP.sent and FakeSMTP.starttls_calls == 0
    notify._sent.clear()
    FakeSMTP.sent.clear()
    monkeypatch.setenv("BEACON_SMTP_PORT", "25")
    monkeypatch.setenv("BEACON_SMTP_SECURITY", "none")
    _ask(responder="op1@example.com", title="Another")
    assert FakeSMTP.sent and FakeSMTP.starttls_calls == 0
