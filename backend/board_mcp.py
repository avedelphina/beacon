"""Board tools over MCP, at /board/mcp.

Served by Beacon itself, at the address agents already reach for their
mesh config, rather than by the fleet MCP service: that one carries fleet
operations an agent must never get. Only board tools live here.

The caller comes from the request: an agent's own derived token (the one
it pulls its mesh config with) makes it that agent; the MCP bearer makes
it "beacon". Anything else is refused before MCP sees the request.
Stateless JSON mode — each tool call is one request — because there is
nothing to keep between calls.

The SDK's session manager can be started only once, so a fresh server is
built for every app lifespan (uvicorn starts one; each test client starts
another) and reached through a small delegating ASGI app.
"""

import hmac
from contextlib import asynccontextmanager

from mcp.server.mcpserver import Context, MCPServer
from starlette.responses import JSONResponse

from . import auth, board, intake, mesh, onboarding, store, workflow

INSTRUCTIONS = """The Beacon fleet board: shared cards for work that spans agents, hosts, or days.
Use it to hand work to another agent, to record progress where others can see it, and to find
what is assigned to you. States: todo, doing, blocked, review, done. When you work on a card
over A2A, pass the thread's context_id to card_comment or card_update so the card links to it.
Assigning a card to an agent also sends that agent a notice, and so does a comment on a card
assigned to it."""


def author_for(authorization: str | None) -> str | None:
    if auth.is_mcp_authorization(authorization):
        return mesh.BEACON_PEER
    if not authorization and not auth.AUTH_ENABLED:
        return mesh.BEACON_PEER  # open mode, like every /api route
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    presented = authorization.removeprefix("Bearer ")
    try:
        # Only agents opted in with reach.board, and not while their chat is
        # open to anyone. Every agent holds its pull token, so without this
        # check the opt-in would only decide whether the tools are
        # configured, not whether the board answers.
        exposure = mesh.open_chats()
        for agent in store.list_agents():
            if mesh.board_allowed(agent, exposure) and hmac.compare_digest(
                    presented, mesh.token(agent.id, mesh.BEACON_PEER)):
                return agent.id
    except mesh.MissingSecret:
        return None
    return None


def _visible_card(ctx: Context, card_id: int) -> dict:
    """The card, or not found when the operators keep intake cards from agents. Related cards
    an agent may not see are left out of it."""
    card = board.get_card(card_id)
    author = _author(ctx)
    if not intake.agent_may_see(author, card):
        raise store.NotFound(f"card {card_id} not found")
    return board.restrict(card, lambda c: intake.agent_may_see(author, c))


def _visible_after(ctx: Context, card: dict) -> dict:
    """A card an agent just changed, as that agent may see it."""
    author = _author(ctx)
    return board.restrict(card, lambda c: intake.agent_may_see(author, c))


def _author(ctx: Context) -> str:
    author = author_for((ctx.headers or {}).get("authorization"))
    if author is None:  # the HTTP gate already checked; this only guards misuse
        raise PermissionError("not authorized")
    return author


def owner_lookup(caller: str, agent_id: str | None = None) -> dict:
    """The owner of `agent_id` (the caller's own by default), for a caller allowed
    to know: its own owner, or the owner of an agent it may call. Anyone else's
    owner is not for an agent to learn. Beacon (an operator's MCP) may ask any."""
    target = agent_id or caller
    if caller != mesh.BEACON_PEER and target != caller:
        try:
            allowed = mesh.may_call(caller, store.get_agent(target))
        except (store.NotFound, store.InvalidId):
            allowed = False
        if not allowed:
            raise PermissionError(f"you may not look up {target!r}")
    owner = workflow.owner_of(target)
    return {"agent": target, "owner": owner, "ask_with": owner or "human"}


def build() -> MCPServer:
    server = MCPServer("beacon-board", instructions=INSTRUCTIONS)

    @server.tool()
    def board_list(ctx: Context, project: str | None = None, assignee: str | None = None,
                   state: str | None = None, include_done: bool = False) -> list[dict]:
        """List cards, most recently changed first. Filter by project, assignee (an agent id),
        or state. Done cards are left out unless include_done is true or state is "done"."""
        author = _author(ctx)
        return [c for c in board.list_cards(project, assignee, state, include_done)
                if intake.agent_may_see(author, c)]

    @server.tool()
    def board_card(ctx: Context, card_id: int) -> dict:
        """One card with its full history: who created, moved, assigned, or commented, and when."""
        return _visible_card(ctx, card_id)

    @server.tool()
    def card_create(ctx: Context, title: str, body: str = "", project: str | None = None,
                    assignee: str | None = None, next_action: str | None = None,
                    done_when: str | None = None, links: list[dict] | None = None) -> dict:
        """Create a card in state "todo". Put what needs doing in body, the first step in
        next_action, and what must be true to close it in done_when. links is a list of
        {url, label} pointing to PRs or issues elsewhere. assignee is an agent id (see
        board_list or the fleet); that agent gets a notice."""
        return board.create_card(_author(ctx), title, body, project, assignee,
                                 next_action=next_action, done_when=done_when, links=links)

    @server.tool()
    def card_update(ctx: Context, card_id: int, state: str | None = None, assignee: str | None = None,
                    title: str | None = None, body: str | None = None, project: str | None = None,
                    next_action: str | None = None, done_when: str | None = None,
                    links: list[dict] | None = None, note: str | None = None,
                    context_id: str | None = None, blocked_reason: str | None = None,
                    waiting_on: str | None = None) -> dict:
        """Change a card: move its state (todo, doing, blocked, review, done), reassign it
        (assignee="" unassigns), or edit it. Keep next_action current as the work moves;
        "" clears it or done_when. links replaces the whole list of {url, label}, so send
        the links you keep too. note says why; it is kept in the history.

        Moving a card to blocked needs blocked_reason: "unclear" (you do not know how to go on:
        consult a peer and try before asking a person), "waiting" (the next step depends on
        something named: give waiting_on, an agent id, "workflow:<id>" for a request on this
        card, or something outside Beacon; waiting on a person means making a workflow request
        first) or "cannot" (you lack a capability or access: make a workflow request to a person for
        exactly what is missing, then set waiting_on to "workflow:<id>"; you are told when it ends).
        Do not use blocked for work you could still do."""
        _visible_card(ctx, card_id)
        return board.update_card(_author(ctx), card_id, state=state, assignee=assignee, title=title,
                                 body=body, project=project, next_action=next_action,
                                 done_when=done_when, links=links, note=note, context_id=context_id,
                                 blocked_reason=blocked_reason, waiting_on=waiting_on)

    @server.tool()
    def card_comment(ctx: Context, card_id: int, text: str, context_id: str | None = None) -> dict:
        """Add progress or a question to a card's history. context_id links an A2A thread."""
        _visible_card(ctx, card_id)
        return board.comment(_author(ctx), card_id, text, context_id)

    @server.tool()
    def card_set_parent(ctx: Context, card_id: int, parent_id: int | None = None,
                        context_id: str | None = None) -> dict:
        """Make a card part of another (parent_id), or leave parent_id out to free it. A card has
        one parent and cannot sit under itself or anything below it. The card's `children` list
        shows what is part of it. Recorded in the card's history."""
        _visible_card(ctx, card_id)
        if parent_id is not None:
            _visible_card(ctx, parent_id)
        return _visible_after(ctx, board.set_parent(_author(ctx), card_id, parent_id, context_id))

    @server.tool()
    def card_add_dependency(ctx: Context, card_id: int, depends_on: int, context_id: str | None = None) -> dict:
        """The card cannot finish until the card `depends_on` is done. Beacon then lists the unfinished
        ones in the card's `blocked_by` and drops each when it reaches done; it does not move the card's
        own state. A card cannot depend on itself or on anything that depends on it."""
        _visible_card(ctx, card_id)
        _visible_card(ctx, depends_on)
        return _visible_after(ctx, board.add_dependency(_author(ctx), card_id, depends_on, context_id))

    @server.tool()
    def card_remove_dependency(ctx: Context, card_id: int, depends_on: int, context_id: str | None = None) -> dict:
        """Remove a dependency the card has on the card `depends_on`."""
        _visible_card(ctx, card_id)
        return _visible_after(ctx, board.remove_dependency(_author(ctx), card_id, depends_on, context_id))

    @server.tool()
    def card_references(ctx: Context, card_id: int) -> list[dict]:
        """A card's references: pointers to a PR, issue or ticket elsewhere, each with provider, kind,
        external_id, url and label, and sometimes a workflow_id (a workflow on this card) and a
        last_seen_state with last_seen_at. The state is a hint that whoever added the reference typed;
        Beacon does not check it, so read the thing itself before relying on it."""
        return _visible_card(ctx, card_id)["references"]

    @server.tool()
    def card_reference_add(ctx: Context, card_id: int, provider: str, kind: str, external_id: str,
                           url: str, label: str, workflow_id: int | None = None,
                           last_seen_state: str | None = None, last_seen_at: float | None = None,
                           context_id: str | None = None) -> dict:
        """Point a card at work elsewhere. provider and kind are free words (gitea, github, mantis;
        pull_request, issue, ticket). url is http(s) without credentials. workflow_id must be a
        workflow on this same card. last_seen_state is what you saw there, and is dated now unless
        you give last_seen_at (Unix time). The change is recorded in the card's history."""
        _visible_card(ctx, card_id)
        return board.add_reference(_author(ctx), card_id, provider=provider, kind=kind, external_id=external_id,
                                   url=url, label=label, workflow_id=workflow_id, last_seen_state=last_seen_state,
                                   last_seen_at=last_seen_at, context_id=context_id)

    @server.tool()
    def card_reference_update(ctx: Context, card_id: int, reference_id: int, provider: str | None = None,
                              kind: str | None = None, external_id: str | None = None, url: str | None = None,
                              label: str | None = None, workflow_id: int | None = None,
                              last_seen_state: str | None = None, last_seen_at: float | None = None,
                              context_id: str | None = None) -> dict:
        """Change a reference; what you leave out stays. workflow_id=0 removes the workflow link and
        last_seen_state="" clears the state hint with its time."""
        _visible_card(ctx, card_id)
        given = {"provider": provider, "kind": kind, "external_id": external_id, "url": url, "label": label,
                 "workflow_id": workflow_id, "last_seen_state": last_seen_state, "last_seen_at": last_seen_at}
        changes = {name: value for name, value in given.items() if value is not None}
        if changes.get("workflow_id") == 0:
            changes["workflow_id"] = None
        if changes.get("last_seen_state") == "":
            changes["last_seen_state"] = None
        return board.update_reference(_author(ctx), card_id, reference_id, context_id=context_id, **changes)

    @server.tool()
    def card_reference_remove(ctx: Context, card_id: int, reference_id: int,
                              context_id: str | None = None) -> dict:
        """Remove a reference from a card. The history keeps what it was."""
        _visible_card(ctx, card_id)
        return board.remove_reference(_author(ctx), card_id, reference_id, context_id)

    @server.tool()
    def agent_owner(ctx: Context, agent_id: str | None = None) -> dict:
        """Who owns an agent: its owner's email, or null. Leave agent_id out for your own. Use
        the email as workflow_create's eligible_responder to ask that person; "human" asks any
        operator instead. You may look up your own owner and the owners of agents you may call."""
        return owner_lookup(_author(ctx), agent_id)

    @server.tool()
    def workflow_list(ctx: Context, card_id: int | None = None, state: str | None = None) -> list[dict]:
        """List typed decision, question, review, and approval requests visible to this caller."""
        return workflow.list_requests(card_id=card_id, state=state, actor=_author(ctx))

    @server.tool()
    def workflow_get(ctx: Context, request_id: int) -> dict:
        """Get one visible workflow request with its response and event history."""
        return workflow.get(request_id, actor=_author(ctx))

    @server.tool()
    def workflow_respond(ctx: Context, request_id: int, action: str, plan_revision: str,
                         plan_hash: str, response: object = None, context_id: str | None = None) -> dict:
        """Respond to an eligible workflow request. action is approved, rejected, changes_requested,
        choose_option (a decision: response {"option": ...}), or answered (a question: response
        {"text": ...}). For a handoff, approved accepts it (the card becomes yours) and rejected
        declines it. T4/T5 responses always need a human browser session. context_id links an
        A2A thread; the answer is also written to the card's history."""
        return workflow.respond(
            request_id, actor=_author(ctx), human=False, action=action, response=response,
            plan_revision=plan_revision, plan_hash=plan_hash, context_id=context_id,
        )

    @server.tool()
    def workflow_revise(ctx: Context, request_id: int, title: str | None = None, summary: str | None = None,
                        options: list[str] | None = None, target: dict | None = None,
                        expires_at: float | None = None, plan_revision: str | None = None,
                        plan_hash: str | None = None, context_id: str | None = None) -> dict:
        """After a responder asked for changes (state "changes_requested"), send the changed plan: the
        request goes back to pending for the same responder. Only the requester may; give only what
        changed. Leave out plan_revision and plan_hash and Beacon binds the new text itself."""
        if (plan_revision is None) != (plan_hash is None):
            raise workflow.WorkflowError("give both plan_revision and plan_hash, or neither")
        if plan_revision is None:
            old = workflow.get(request_id, actor=_author(ctx))
            plan_revision, plan_hash = workflow.plan_binding(
                {"capability": old["capability"], "tier": old["tier"], "title": title or old["title"],
                 "summary": summary or old["summary"], "options": old["options"] if options is None else options,
                 "target": old["target"] if target is None else target}).values()
        return workflow.revise(
            request_id, actor=_author(ctx), title=title, summary=summary, options=options, target=target,
            expires_at=expires_at, plan_revision=plan_revision, plan_hash=plan_hash, context_id=context_id)

    @server.tool()
    def workflow_create(ctx: Context, card_id: int, kind: str, eligible_responder: str,
                        capability: str, tier: int, title: str, summary: str,
                        options: list[str] | None = None, target: dict | None = None,
                        expires_at: float | None = None, plan_revision: str | None = None,
                        plan_hash: str | None = None, context_id: str | None = None,
                        consulted: list[dict] | None = None, consult_skipped: str | None = None) -> dict:
        """Create a pending typed workflow request attached to a board card. When you ask a person
        (eligible_responder "human" or an email), first consult at least one peer you may call about
        the problem and your plan, and pass what you learned as consulted: [{"agent": "<peer>",
        "said": "<what they answered>", "context_id": "<thread, optional>"}]. If a peer cannot help
        (a plain yes/no, nobody to ask), say why in consult_skipped. A request to a person with neither
        is flagged for them as "no peer consulted"; it is not refused. summary is Markdown:
        its first line says what you need, then the short plan. kind: decision (the
        responder picks one of options), question (the responder writes a free-text answer;
        no options), handoff (ask an agent to take the card over: no options; when it approves,
        the card is assigned to it), review, or approval. Leave out plan_revision and plan_hash
        and Beacon binds the request to its own text: a response then has to name that exact
        plan, and an edited plan cannot be approved. context_id links an A2A thread; every change
        to the request is also written to the card's history."""
        _visible_card(ctx, card_id)
        workflow.validate_kind_for_tier(kind, tier)
        workflow.validate_participants(_author(ctx), eligible_responder)
        if (plan_revision is None) != (plan_hash is None):
            raise workflow.WorkflowError("give both plan_revision and plan_hash, or neither")
        if plan_revision is None:
            plan_revision, plan_hash = workflow.plan_binding(
                {"capability": capability, "tier": tier, "title": title, "summary": summary,
                 "options": options or [], "target": target or {}}).values()
        return workflow.create(
            card_id=card_id, kind=kind, requester=_author(ctx), eligible_responder=eligible_responder,
            capability=capability, tier=tier, title=title, summary=summary, options=options,
            target=target, plan_revision=plan_revision, plan_hash=plan_hash, expires_at=expires_at,
            context_id=context_id, consulted=consulted, consult_skipped=consult_skipped,
        )

    @server.tool()
    def enroll_request(ctx: Context, card_id: int, agent_id: str, docker: str | None = None,
                       docker_user: str = "hermes", schedule: bool = True) -> dict:
        """Ask a person to approve a one-time enrollment command for an agent (yourself, unless you
        are an MCP client). Creates an approval request on the card; wait for state "approved",
        then call enroll_claim. docker is the container name for an agent that runs in Docker."""
        return onboarding.request_enrollment(_author(ctx), card_id, agent_id, docker, docker_user, schedule)

    @server.tool()
    def enroll_claim(ctx: Context, request_id: int) -> dict:
        """Get the command for an approved enrollment request, once. Run it on the agent's host
        (as root for a Docker agent). It works for 15 minutes and only once; do not store or repeat it."""
        return onboarding.claim(request_id, _author(ctx))

    return server


class BoardApp:
    """ASGI app mounted at /board: checks the caller, then hands over to the
    current lifespan's MCP app."""

    def __init__(self):
        self.mcp_app = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if author_for(headers.get("authorization")) is None:
                await JSONResponse({"detail": "not authorized for the board"}, status_code=401)(scope, receive, send)
                return
        if self.mcp_app is None:
            await JSONResponse({"detail": "board not started"}, status_code=503)(scope, receive, send)
            return
        await self.mcp_app(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self):
        server = build()
        # host 0.0.0.0: Beacon is reached by tailnet IP or a public name, so the
        # SDK's localhost-only Host check would refuse every agent.
        self.mcp_app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                                  json_response=True, host="0.0.0.0")
        async with server.session_manager.run():
            yield
        self.mcp_app = None


board_app = BoardApp()
