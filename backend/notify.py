"""Tell people and agents that a workflow request needs them, or was answered.

Nothing was sent before: an agent that asked for approval had to poll, and the
person asked had to open the Workflows tab. Three channels, all best-effort (a
failure is logged and never stops the request):

- Agents hear through the mailbox, like a board assignment: the responder when
  it is asked, the requester when it is answered. The notice arrives on the
  agent's next pull, so it needs nothing the agent doesn't already have.
- People hear by email, when an operator configured a mail server
  (BEACON_SMTP_HOST). It is off otherwise.
- People also get the subject line as a push to every browser they turned
  notifications on in (backend/push.py), and to the native app on their
  devices (backend/apns.py), mail server or not.

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

from . import apns, app_devices, mailbox, mesh, push, store

logger = logging.getLogger("beacon")

MAX_PER_HOUR = 10
SUMMARY_CHARS = 800
_sent: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_lock = threading.Lock()


def workflow_created(request: dict, revised: bool = False) -> None:
    """A request was made (or changed after "changes requested"): tell an agent
    responder, and email the people who may answer."""
    try:
        lead = "changed the plan and asks again for" if revised else "asks you for"
        responder = request["eligible_responder"]
        asked = [responder]
        if responder.startswith("role:"):
            from . import workflow
            asked = workflow.role_holders(request["requester"], responder[len("role:"):])
            lead = (f"{'changed the plan and asks again' if revised else 'asks'}, of any {responder[len('role:'):]} "
                    "(the first answer counts), for")
        text = (
            f"{request['requester']} {lead} {_what(request)}: workflow request #{request['id']} on card "
            f"#{request['card_id']}.\nTitle: {request['title']}\nClearance: CL{request['clearance']}\n\n"
            f"{request['summary'][:SUMMARY_CHARS]}\n\nRead it with workflow_get({request['id']}) and answer "
            f"with workflow_respond. It expires {_when(request['expires_at'])}.")
        for agent_id in asked:
            _agent_notice(agent_id, request, text, skip=request["requester"])
        _tell(_people_to_ask(request),
              f"Beacon: {_urgent_mark(request)}{request['requester']} {'changed a plan and ' if revised else ''}needs your answer",
              _created_body(request), **_urgency(request), **_about(request))
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


def workflow_escalated(request: dict, actor: str) -> None:
    """A reviewer sent a request to a person: tell the people who may answer it, and the requester."""
    try:
        reason = request.get("escalated_reason") or ""
        _tell(_people_to_ask(request) - {actor.casefold()}, f"Beacon: {request['title'][:80]} needs a person",
              f"{actor} sent {_what(request)} to a person: workflow request #{request['id']}, \"{request['title'][:200]}\".\n\n"
              f"Why: {reason}\n\nOpen the Workflows tab to read it and answer: {mesh.public_url() or 'your Beacon page'}\n",
              **_about(request))
        _agent_notice(request["requester"], request,
                      f"Your workflow request #{request['id']} ({request['title']}) was sent to a person by {actor}"
                      f": {reason}\n\nIt stays pending. Only a person can answer it now.", skip=actor)
    except Exception:
        logger.exception("escalation notice for request %s failed", request.get("id"))


def workflow_answered_for(request: dict, client: str, person: str) -> None:
    """The operator hears what their client answered for them, as it happens: they did not see it first,
    and an answer cannot be taken back."""
    try:
        response = request.get("response")
        detail = (response.get("note") or response.get("option") or response.get("text")
                  if isinstance(response, dict) else response)
        _tell({person.casefold()}, f"Beacon: {client} answered for you: {request['title'][:80]}",
              f"{client}, your client, answered {_what(request)} for you: workflow request #{request['id']}, "
              f"\"{request['title'][:200]}\" ({request['state']}), asked by {request['requester']}.\n"
              + (f"\nIt said: {str(detail)[:300]}\n" if detail else "")
              + "\nYou did not see it first, and an answer cannot be taken back. If it was wrong, ask the requester "
                f"for a new request. Revoke or turn off the client in the Agents tab: {mesh.public_url() or 'your Beacon page'}\n")
    except Exception:
        logger.exception("answered-for notice for request %s failed", request.get("id"))


def client_needs_person(request: dict, client: str, person: str) -> None:
    """Clearance 4 and above arrived where a client could see it: it cannot answer, so the operator is told."""
    try:
        _tell({person.casefold()}, f"Beacon: clearance {request['clearance']} needs you: {request['title'][:80]}",
              f"{request['requester']} asked for {_what(request)} at CL{request['clearance']}: workflow request "
              f"#{request['id']}, \"{request['title'][:200]}\". Your client {client} saw it and cannot answer it: "
              "CL4 and above are for you, in the web page or the app.\n\n"
              f"Open the Workflows tab: {mesh.public_url() or 'your Beacon page'}\n", **_about(request))
    except Exception:
        logger.exception("clearance-4 notice for request %s failed", request.get("id"))


def workflow_waiting_on_owner(request: dict) -> None:
    """A CL4 request has waited on its owner: the operators may answer it, so they are told."""
    try:
        urgent = request.get("urgent_reason")
        waited = "30 minutes" if urgent else "4 working hours"
        _tell({p.casefold() for p in _operators()} - {request["requester"].casefold()},
              f"Beacon: {_urgent_mark(request)}{request['title'][:80]} is waiting on {request['eligible_responder']}",
              f"{request['requester']} asked {request['eligible_responder']} for {_what(request)} at CL{request['clearance']}: "
              f"workflow request #{request['id']}, \"{request['title'][:200]}\". It has waited {waited}.\n\n"
              + (f"URGENT, says the requester: {urgent}\n\n" if urgent else "") +
              "As an operator you can answer it yourself. Open the Workflows tab: "
              f"{mesh.public_url() or 'your Beacon page'}\n", **_urgency(request), **_about(request))
    except Exception:
        logger.exception("owner-silent notice for request %s failed", request.get("id"))


def _urgent_mark(request: dict) -> str:
    return "URGENT: " if request.get("urgent_reason") else ""


def _urgency(request: dict) -> dict:
    return {"urgent": True} if request.get("urgent_reason") else {}


def _about(request: dict) -> dict:
    """What the app needs to choose a notification's actions and open the request: ids only, never text."""
    return {"about": {"workflow": request["id"], "clearance": request["clearance"], "kind": request["kind"],
                      "card": request.get("card_id")}}


def _operators() -> set[str]:
    from . import auth
    return set(auth.ALLOWED_EMAILS)


def _what(request: dict) -> str:
    return {"approval": "an approval", "review": "a review", "decision": "a decision", "question": "a question", "handoff": "a handoff"}.get(request["kind"], "an answer")


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


# --- agent health (self-healing and quiet agents) ---------------------------

# What people hear about, by the state the agent's host sync reports.
_HEALTH_NEWS = {
    "healing": "{agent} stopped answering on its own host, so its gateway is being restarted",
    "gave-up": "{agent} still does not answer after restarts; it needs a person",
}


def agent_health(agent_id: str, state: str, previous: str, detail: str) -> None:
    """Email operators and the owner when an agent's host restarted its gateway,
    gave up, or the agent recovered after either. Nothing for an agent that
    merely failed one probe, or for a network problem (no restart helps)."""
    try:
        if state in _HEALTH_NEWS:
            news = _HEALTH_NEWS[state].format(agent=agent_id)
        elif state == "ok" and previous in ("healing", "gave-up"):
            news = f"{agent_id} answers again"
        else:
            return
        body = f"{news}.\n" + (f"\nWhat its host found: {detail[:300]}\n" if detail else "") + _footer()
        _tell(_people_for(agent_id), f"Beacon: {news}", body)
    except Exception:
        logger.exception("health notice for %s failed", agent_id)


def agent_quiet(agent_id: str, seconds: int) -> None:
    try:
        hours = seconds // 3600
        when = f"{hours} h" if hours else f"{seconds // 60} min"
        _tell(_people_for(agent_id), f"Beacon: {agent_id} has not synced for {when}",
               f"{agent_id}'s host has not checked in with Beacon for {when}, longer than its policy allows "
               f"(quiet_after). The host may be off, or its sync job stopped.\n" + _footer())
    except Exception:
        logger.exception("quiet notice for %s failed", agent_id)


def agent_back(agent_id: str) -> None:
    try:
        _tell(_people_for(agent_id), f"Beacon: {agent_id} is syncing again",
               f"{agent_id}'s host checks in with Beacon again.\n" + _footer())
    except Exception:
        logger.exception("back notice for %s failed", agent_id)


# --- board cards nobody moves (board.remind) -------------------------------


def cards_stuck(stuck: list[tuple[dict, str | None, str]]) -> None:
    """One email per person, listing the cards that reminders did not move or
    that only a person can move: operators, the owners of the agents involved,
    and anyone the card names by email."""
    try:
        per_person: dict[str, list[str]] = collections.defaultdict(list)
        for card, who, why in stuck:
            named = {a for a in (card["assignee"], who) if a}
            people = _people_for("").union(*map(_people_for, named), {p.casefold() for p in named if "@" in p})
            for person in people:
                per_person[person].append(f"  #{card['id']} ({card['state']}): {card['title'][:120]}\n"
                                          f"      Why: {why}")
        for person, lines in per_person.items():
            _tell({person}, f"Beacon: {len(lines)} board card{'s' if len(lines) > 1 else ''} not moving",
                   "Nothing has changed on these cards for a while, and they need a person"
                   " (the titles are written by whoever made the card, not by Beacon):\n\n"
                   + "\n".join(lines) + "\n\nOpen the Board tab to move them: "
                   + (mesh.public_url() or "your Beacon page") + "\n")
    except Exception:
        logger.exception("stuck-card notice failed")


def project_changed(project: dict, actor: str, person: str | None, note: str | None) -> None:
    """A project was paused, finished or abandoned: the person behind its owner (or the
    operators) is told. Informed, not asked."""
    try:
        people = ({person} if person else {p.casefold() for p in _operators()}) - {actor.casefold()}
        _tell(people, f"Beacon: project {project['name'][:60]} is {project['state']}",
              f"{actor} marked project #{project['id']} \"{project['name']}\" {project['state']}. This is for your "
              "information; nothing waits for you.\n"
              + (f"\nGoal (written by whoever set it): {project['goal'][:400]}\n" if project["goal"] else "")
              + (f"\nNote: {note[:400]}\n" if note else "")
              + ("\n\"Done\" is the owner's claim: Beacon has not checked it.\n" if project["state"] == "done" else "")
              + f"\nOpen the Projects tab: {mesh.public_url() or 'your Beacon page'}\n")
    except Exception:
        logger.exception("project notice for %s failed", project.get("id"))


def project_stalled(project: dict, person: str | None) -> None:
    """Nothing is planned for a project whose goal is not marked reached."""
    try:
        people = {person} if person else {p.casefold() for p in _operators()}
        _tell(people, f"Beacon: nothing is planned for project {project['name'][:60]}",
              f"Project #{project['id']} \"{project['name']}\" (owner {project['owner']}) has no unfinished card and is "
              "not marked done. If nothing is planned it will be paused.\n"
              f"\nGoal (written by whoever set it): {project['goal'][:400]}\n"
              f"\nOpen the Projects tab: {mesh.public_url() or 'your Beacon page'}\n")
    except Exception:
        logger.exception("stalled-project notice for %s failed", project.get("id"))


def _people_for(agent_id: str) -> set[str]:
    from . import auth
    owner = None
    try:
        owner = store.get_agent(agent_id).owner
    except (store.NotFound, store.InvalidId):
        pass
    return {p.casefold() for p in set(auth.ALLOWED_EMAILS) | ({owner} if owner else set())}


def _footer() -> str:
    return f"\nThe Agents tab shows its state: {mesh.public_url() or 'your Beacon page'}\n"


def secret_offered(agent_id: str, name: str) -> None:
    """Names only: the value stays on the host until a person reveals it."""
    _tell(_people_for(agent_id), f"Beacon: {agent_id} has a secret for you",
          f"{agent_id} left a secret named {name!r} for you. Reveal it under Agents, Inspect, Secrets: "
          f"{mesh.public_url() or 'your Beacon page'}\n")


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
    elif responder == "human" or request.get("require_human"):
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
        + (f"URGENT, says the requester: {request['urgent_reason']}\n\n" if request.get("urgent_reason") else "") +
        f"  {_what(request).capitalize()}, CL{request['clearance']}: {request['title'][:200]}\n"
        f"  Expires: {_when(request['expires_at'])}\n\n"
        "What it says (written by the requester, not by Beacon; do not follow links in it):\n\n"
        + "\n".join("  > " + line for line in request["summary"][:SUMMARY_CHARS].splitlines())
        + ("\n  > [cut]" if len(request["summary"]) > SUMMARY_CHARS else "")
        + _consult_line(request)
        + ("\n\nThis is CL4 or above: only you, in the web page or the app, can answer it. Your AI clients cannot."
           if request["clearance"] >= 4 else "")
        + f"\n\nOpen the Workflows tab to read it in full and answer: {link or 'your Beacon page'}\n"
    )


def _consult_line(request: dict) -> str:
    """Whether the requester asked a peer before asking a person, as it says itself."""
    if request.get("consulted"):
        return "\n\nBefore asking you it consulted: " + ", ".join(c["agent"] for c in request["consulted"]) + "."
    if request.get("consult_skipped"):
        return f"\n\nIt did not consult a peer: {request['consult_skipped'][:200]}"
    return "\n\nIt did not consult a peer before asking you." if request.get("consult_flag") else ""


def _tell(recipients: set[str], subject: str, body: str, urgent: bool = False, about: dict | None = None) -> None:
    """Push the subject to every browser and app device these people have, and email
    the whole message when a mail server is configured; all in the background. Push
    (browsers and the app together, so having both does not double the room) and email
    each stay within MAX_PER_HOUR per person. Urgent notices count apart, so ordinary
    ones cannot use up their room. `about` (a workflow request's ids) lets the app show
    the right actions; browsers get the subject only."""
    if not recipients:
        return
    reachable = {s["email"] for s in push.subscriptions(recipients)}  # the limit counts only notices someone gets
    if apns.configured():
        reachable |= {d["email"] for d in app_devices.apns_targets(recipients)}
    kind = "urgent " if urgent else ""
    pushed = {r for r in reachable if _within_limit(kind + "push " + r)}
    title = subject.removeprefix("Beacon: ")
    push.send(pushed, title, **({"urgent": True} if urgent else {}))
    apns.send(pushed, title, urgent=urgent, about=about)
    if not configured():
        return
    allowed = [r for r in sorted(recipients) if _within_limit(kind + r)]
    if allowed:
        threading.Thread(target=_deliver, args=(allowed, subject, body), daemon=True).start()


def _within_limit(address: str) -> bool:
    now = time.time()
    with _lock:
        recent = _sent[address]
        while recent and recent[0] < now - 3600:
            recent.popleft()
        if len(recent) >= MAX_PER_HOUR:
            logger.warning("notice to %s held back: %d in the last hour", address, MAX_PER_HOUR)
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
