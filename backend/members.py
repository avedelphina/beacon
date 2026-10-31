"""Members: people who see, and do a little with, the agents they own.

An operator (BEACON_ALLOWED_EMAILS) runs the fleet. A member is anyone whose
email an operator wrote as the `owner` of an agent: that field is the whole
list of members, so there is nothing to configure beside the agents, and
changing an owner changes who may log in. A member gets a short list of things
to do with their agents. The list is the whole design: auth.py
lets a member's session reach only the routes below, so a route added later is
closed to members until someone adds it here. Everything per-agent is also an
ownership check, answered 404 for another person's agent so a member cannot tell
which agents exist.

What a member may do, and why those:
- see their agents (a trimmed record: no endpoint, config or host details) and
  whether each answers over A2A;
- restart the gateway of their agent: reversible, the usual fix, T2 with the
  same confirm gate as for an operator;
- read an agent's logs only where an operator turned `member_logs` on for it,
  since logs of a shared household agent hold other people's chats;
- work the board for their agents: cards assigned to them or made by them,
  comments, state changes. An agent's owner hears from it, and it from them;
- answer approvals and decisions that concern their agents: what the agent asks
  of "a human" is asked of its owner.
Nothing else: no hosts, templates, policy, cron, config, deploy, enrollment,
or any other person's agent.
"""

import re

from . import store, workflow

# (method, path, what the path names: an agent, a card, a workflow request, or nothing)
_ROUTES = [(method, re.compile(pattern), subject) for method, pattern, subject in (
    ("GET", r"/api/version", None),
    ("GET", r"/api/tiers", None),
    ("GET", r"/api/agents", None),
    ("GET", r"/api/availability", None),
    ("GET", r"/api/agents/(?P<id>[^/]+)", "agent"),
    ("GET", r"/api/agents/(?P<id>[^/]+)/status", "agent"),
    ("POST", r"/api/agents/(?P<id>[^/]+)/restart", "agent"),
    ("GET", r"/api/agents/(?P<id>[^/]+)/logs", "logs"),
    ("GET", r"/api/board", None),
    ("POST", r"/api/board/cards", None),
    ("GET", r"/api/board/cards/(?P<id>\d+)", "card"),
    ("PATCH", r"/api/board/cards/(?P<id>\d+)", "card"),
    ("POST", r"/api/board/cards/(?P<id>\d+)/comments", "card"),
    ("GET", r"/api/workflows", None),
    ("GET", r"/api/workflows/(?P<id>\d+)", "workflow"),
    ("POST", r"/api/workflows/(?P<id>\d+)/respond", "workflow"),
)]


def owns(email: str, agent) -> bool:
    return bool(agent.owner) and agent.owner.casefold() == email.casefold()


def owns_any(email: str) -> bool:
    return any(owns(email, a) for a in store.list_agents())


def owned_ids(email: str) -> set[str]:
    return {a.id for a in store.list_agents() if owns(email, a)}


def _owned_agent(email: str, agent_id: str):
    try:
        agent = store.get_agent(agent_id)
    except (store.NotFound, store.InvalidId):
        return None
    return agent if owns(email, agent) else None


def card_visible(email: str, card: dict) -> bool:
    return card["created_by"].casefold() == email.casefold() or (
        card["assignee"] is not None and card["assignee"] in owned_ids(email))


def _target_agent(request: dict) -> str | None:
    """The agent an approval is about: a push names it `agent_id`, an enrollment `agent`."""
    target = request.get("target") or {}
    named = target.get("agent_id") or target.get("agent")
    return named if isinstance(named, str) else None


def workflow_visible(email: str, request: dict) -> bool:
    """A request that asks a person, about one of the member's agents or from one."""
    if request["eligible_responder"] != "human":
        return email.casefold() in {request["requester"].casefold(), request["eligible_responder"].casefold()}
    return request["requester"].casefold() == email.casefold() or bool({request["requester"], _target_agent(request)} & owned_ids(email))


def check(method: str, path: str, email: str) -> tuple[int, str] | None:
    """None to let a member's request through, else (status, reason)."""
    if not path.startswith("/api/"):
        # The page itself (static files) and FastAPI's schema; everything the
        # page does goes through /api/ and is checked below.
        return None if method == "GET" else (403, "not available to members")
    for allowed_method, pattern, subject in _ROUTES:
        match = pattern.fullmatch(path)
        if allowed_method != method or match is None:
            continue
        if subject in ("agent", "logs"):
            agent = _owned_agent(email, match["id"])
            if agent is None:
                return 404, "no such agent"
            if subject == "logs" and not agent.member_logs:
                return 403, "an operator has not opened this agent's logs to its owner"
        elif subject == "card":
            try:
                from . import board
                visible = card_visible(email, board.get_card(int(match["id"])))
            except store.NotFound:
                visible = False
            if not visible:
                return 404, "no such card"
        elif subject == "workflow":
            try:
                visible = workflow_visible(email, workflow.get(int(match["id"])))
            except workflow.WorkflowNotFound:
                visible = False
            if not visible:
                return 404, "no such workflow request"
        return None
    return 403, "not available to members"


def agent_view(agent, chat_open) -> dict:
    """What an owner sees of their agent. Not the endpoint, the desired config,
    templates or caller lists: those are the operator's."""
    return {"id": agent.id, "type": agent.type, "host": agent.host, "profile": agent.profile, "owner": agent.owner,
            "tags": agent.tags, "notes": agent.notes, "chat_open": chat_open, "member_logs": agent.member_logs,
            "reach": {"network": agent.reach.network, "board": agent.reach.board} if agent.reach else None}


def status_view(status: dict) -> dict:
    """The service's state and when it changed; not the host's error text."""
    return {k: v for k, v in status.items() if k in ("state", "active_state", "sub_state", "since")}


def availability_view(result: dict) -> dict:
    """State only: the detail names the endpoint and the connection error."""
    return {"state": result["state"], "last_pull_seconds_ago": result["last_pull_seconds_ago"], "detail": ""}


def workflow_view(request: dict) -> dict:
    """The request without its target, which for a config push holds the host's
    address and SSH settings. The plan hashes stay: a response has to name them."""
    shown = {k: v for k, v in request.items() if k != "target"}
    shown["target"] = {"agent": _target_agent(request)}
    return shown
