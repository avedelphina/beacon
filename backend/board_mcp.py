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

from . import auth, board, mesh, store

INSTRUCTIONS = """The Beacon fleet board: shared cards for work that spans agents, hosts, or days.
Use it to hand work to another agent, to record progress where others can see it, and to find
what is assigned to you. States: todo, doing, blocked, review, done. When you work on a card
over A2A, pass the thread's context_id to card_comment or card_update so the card links to it.
Assigning a card to an agent also sends that agent a notice."""


def author_for(authorization: str | None) -> str | None:
    if auth.is_mcp_authorization(authorization):
        return mesh.BEACON_PEER
    if not authorization and not auth.AUTH_ENABLED:
        return mesh.BEACON_PEER  # open mode, like every /api route
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    presented = authorization.removeprefix("Bearer ")
    try:
        # Only agents opted in with reach.board. Every agent holds its pull
        # token, so without this check the opt-in would only decide whether
        # the tools are configured, not whether the board answers.
        for agent in store.list_agents():
            if agent.reach and agent.reach.board and hmac.compare_digest(
                    presented, mesh.token(agent.id, mesh.BEACON_PEER)):
                return agent.id
    except mesh.MissingSecret:
        return None
    return None


def _author(ctx: Context) -> str:
    author = author_for((ctx.headers or {}).get("authorization"))
    if author is None:  # the HTTP gate already checked; this only guards misuse
        raise PermissionError("not authorized")
    return author


def build() -> MCPServer:
    server = MCPServer("beacon-board", instructions=INSTRUCTIONS)

    @server.tool()
    def board_list(project: str | None = None, assignee: str | None = None, state: str | None = None,
                   include_done: bool = False) -> list[dict]:
        """List cards, most recently changed first. Filter by project, assignee (an agent id),
        or state. Done cards are left out unless include_done is true or state is "done"."""
        return board.list_cards(project, assignee, state, include_done)

    @server.tool()
    def board_card(card_id: int) -> dict:
        """One card with its full history: who created, moved, assigned, or commented, and when."""
        return board.get_card(card_id)

    @server.tool()
    def card_create(ctx: Context, title: str, body: str = "", project: str | None = None,
                    assignee: str | None = None) -> dict:
        """Create a card in state "todo". Put what needs doing and what "done" means in body.
        assignee is an agent id (see board_list or the fleet); that agent gets a notice."""
        return board.create_card(_author(ctx), title, body, project, assignee)

    @server.tool()
    def card_update(ctx: Context, card_id: int, state: str | None = None, assignee: str | None = None,
                    title: str | None = None, body: str | None = None, project: str | None = None,
                    note: str | None = None, context_id: str | None = None) -> dict:
        """Change a card: move its state (todo, doing, blocked, review, done), reassign it
        (assignee="" unassigns), or edit it. note says why; it is kept in the history."""
        return board.update_card(_author(ctx), card_id, state=state, assignee=assignee, title=title,
                                 body=body, project=project, note=note, context_id=context_id)

    @server.tool()
    def card_comment(ctx: Context, card_id: int, text: str, context_id: str | None = None) -> dict:
        """Add progress or a question to a card's history. context_id links an A2A thread."""
        return board.comment(_author(ctx), card_id, text, context_id)

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
