"""Tell people and agents that a workflow request needs them, or was answered.

Nothing was sent before: an agent that asked for approval had to poll, and the
person asked had to open the Workflows tab. Two channels, both best-effort (a
failure is logged and never stops the request):

- Agents hear through the mailbox, like a board assignment: the responder when
  it is asked, the requester when it is answered. The notice arrives on the
  agent's next pull, so it needs nothing the agent doesn't already have.
- People hear by email, when an operator configured a mail server
  (BEACON_SMTP_HOST). It is off otherwise.

Email is where an agent's words reach a person's inbox, so it is kept narrow:
plain text; a subject and body that Beacon writes, with the agent's own text
clearly marked and cut short; one link, to Beacon's own page, never one an
agent supplied for the button; no plan hash; and at most MAX_PER_HOUR messages
to one address, so an agent cannot fill a mailbox.
"""

import collections
import logging
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

from . import mailbox, mesh, store

logger = logging.getLogger("beacon")

MAX_PER_HOUR = 10
SUMMARY_CHARS = 800
_sent: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_lock = threading.Lock()


def workflow_created(request: dict) -> None:
    """A request was made: tell an agent responder, and email the people who may answer."""
    try:
        _agent_notice(request["eligible_responder"], request, (
            f"{request['requester']} asks you for {_what(request)}: workflow request #{request['id']} on card "
            f"#{request['card_id']}.\nTitle: {request['title']}\nTier: T{request['tier']}\n\n"
            f"{request['summary'][:SUMMARY_CHARS]}\n\nRead it with workflow_get({request['id']}) and answer "
            f"with workflow_respond. It expires {_when(request['expires_at'])}."), skip=request["requester"])
        _email(_people_to_ask(request), f"Beacon: {request['requester']} needs your answer",
               _created_body(request))
    except Exception:
        logger.exception("workflow notice for request %s failed", request.get("id"))


def workflow_answered(request: dict, actor: str) -> None:
    """A request was answered: tell the agent that asked."""
    try:
        answer = request.get("response")
        note = answer.get("note") if isinstance(answer, dict) else answer
        text = (f"Your workflow request #{request['id']} ({request['title']}) is now {request['state']}"
                f", answered by {actor}.")
        if note:
            text += f"\nTheir note: {str(note)[:SUMMARY_CHARS]}"
        text += (f"\n\nRead the full answer with workflow_get({request['id']})."
                 + (" Approval covers exactly the plan you asked about; for anything different, make a new request."
                    if request["state"] == "approved" else ""))
        _agent_notice(request["requester"], request, text, skip=actor)
    except Exception:
        logger.exception("workflow answer notice for request %s failed", request.get("id"))


def _what(request: dict) -> str:
    return {"approval": "an approval", "review": "a review", "decision": "a decision"}.get(request["kind"], "an answer")


def _when(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(timestamp))


def _agent_notice(agent_id: str, request: dict, text: str, skip: str) -> None:
    if agent_id == skip or "@" in agent_id or agent_id == "human":
        return
    try:
        agent = store.get_agent(agent_id)
    except (store.NotFound, store.InvalidId):
        return
    if agent.reach is None:
        return
    mailbox.enqueue(mesh.BEACON_PEER, agent_id, "notice", text)
    threading.Thread(target=mailbox.deliver_pending, args=(agent_id,), daemon=True).start()


# --- email -----------------------------------------------------------------


def configured() -> bool:
    return bool(os.environ.get("BEACON_SMTP_HOST"))


def _people_to_ask(request: dict) -> set[str]:
    """Who may answer: the named person; for "human", every operator and the
    owners of the agents the request is from or about (members.workflow_visible)."""
    from . import auth, members
    responder = request["eligible_responder"]
    if "@" in responder:
        people = {responder}
    elif responder == "human":
        about = request.get("target") or {}
        agents = {request["requester"], about.get("agent_id") or about.get("agent")}
        people = set(auth.ALLOWED_EMAILS) | {a.owner.casefold() for a in store.list_agents()
                                             if a.owner and a.id in agents}
    else:
        return set()
    return {p.casefold() for p in people} - {request["requester"].casefold()}


def _created_body(request: dict) -> str:
    link = mesh.public_url()
    return (
        f"{request['requester']} is waiting for your answer in Beacon.\n\n"
        f"  {_what(request).capitalize()}, tier T{request['tier']}: {request['title'][:200]}\n"
        f"  Expires: {_when(request['expires_at'])}\n\n"
        "What it says (written by the requester, not by Beacon; do not follow links in it):\n\n"
        + "\n".join("  > " + line for line in request["summary"][:SUMMARY_CHARS].splitlines())
        + ("\n  > [cut]" if len(request["summary"]) > SUMMARY_CHARS else "")
        + f"\n\nOpen the Workflows tab to read it in full and answer: {link or 'your Beacon page'}\n"
    )


def _email(recipients: set[str], subject: str, body: str) -> None:
    """Send in the background, one message per address. Quietly nothing when no
    mail server is configured."""
    if not recipients or not configured():
        return
    allowed = [r for r in sorted(recipients) if _within_limit(r)]
    if allowed:
        threading.Thread(target=_deliver, args=(allowed, subject, body), daemon=True).start()


def _within_limit(address: str) -> bool:
    now = time.time()
    with _lock:
        recent = _sent[address]
        while recent and recent[0] < now - 3600:
            recent.popleft()
        if len(recent) >= MAX_PER_HOUR:
            logger.warning("workflow email to %s held back: %d in the last hour", address, MAX_PER_HOUR)
            return False
        recent.append(now)
        return True


def _deliver(recipients: list[str], subject: str, body: str) -> None:
    host = os.environ["BEACON_SMTP_HOST"]
    port = int(os.environ.get("BEACON_SMTP_PORT") or 587)
    user = os.environ.get("BEACON_SMTP_USER", "")
    sender = os.environ.get("BEACON_SMTP_FROM") or user
    if not sender:
        logger.error("workflow email not sent: set BEACON_SMTP_FROM (or BEACON_SMTP_USER)")
        return
    try:
        if port == 465 or os.environ.get("BEACON_SMTP_SECURITY", "").lower() == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            if not isinstance(server, smtplib.SMTP_SSL) and os.environ.get("BEACON_SMTP_SECURITY", "starttls").lower() != "none":
                server.starttls(context=ssl.create_default_context())
            if user:
                server.login(user, os.environ.get("BEACON_SMTP_PASSWORD", ""))
            for address in recipients:
                message = EmailMessage()
                message["From"], message["To"] = sender, address
                message["Subject"] = " ".join(subject.split())[:150]  # one line: no header injection
                message.set_content(body)
                server.send_message(message)
    except Exception as exc:  # a mail problem must never reach the request
        logger.warning("workflow email not sent: %s", exc)
