"""Client agents' tokens, and making a client.

A client agent is an operator's own AI client in the fleet (schemas.AgentClient). Its token is not
derived from BEACON_A2A_SECRET like a fleet agent's pull token: that one cannot be shown in a web
page, cannot be revoked without changing every agent's token, and cannot say when it was last used.
A client's is random, shown once when it is minted, kept only as a hash, and revoked on its own. A
hash is enough because the token is 256 bits of randomness: there is nothing to guess.

Only a person in a browser session mints one (backend/app.py); a client cannot widen itself.
"""

import hashlib
import re
import secrets
import time

from . import opstore, store
from .schemas import ID_RE, Agent, AgentClient, Host

PREFIX = "bcl_"
KINDS = ("claude", "codex", "client")  # the first part of a client's id: whose client it is
CLIENT_HOST = "clients"  # an inventory-only host (no SSH): nothing runs there, but every agent names a host
TOUCH_AFTER = 60  # last_used_at is updated at most this often, so a busy client is not a write per call


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(agent_id: str, created_by: str, label: str = "") -> tuple[int, str]:
    """A new token for a client agent. The caller shows it once; Beacon keeps only its hash."""
    agent = store.get_agent(agent_id)
    if agent.client is None:
        raise ValueError(f"{agent_id!r} is not a client agent")
    token = PREFIX + secrets.token_urlsafe(32)
    with opstore.connect() as db:
        token_id = db.execute(
            "INSERT INTO client_tokens (agent_id, token_hash, label, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, _hash(token), (label or "").strip()[:80], created_by, time.time())).lastrowid
    return token_id, token


def tokens(agent_id: str) -> list[dict]:
    """What is known of each token, newest first: never the token itself."""
    with opstore.connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, label, created_by, created_at, last_used_at, revoked_at, revoked_by FROM client_tokens"
            " WHERE agent_id = ? ORDER BY id DESC", (agent_id,))]


def revoke(agent_id: str, token_id: int, by: str) -> bool:
    with opstore.connect() as db:
        return db.execute(
            "UPDATE client_tokens SET revoked_at = ?, revoked_by = ? WHERE id = ? AND agent_id = ? AND revoked_at IS NULL",
            (time.time(), by, token_id, agent_id)).rowcount == 1


def revoke_all(agent_id: str, by: str, keep: int | None = None) -> int:
    with opstore.connect() as db:
        return db.execute(
            "UPDATE client_tokens SET revoked_at = ?, revoked_by = ? WHERE agent_id = ? AND revoked_at IS NULL AND id != ?",
            (time.time(), by, agent_id, keep or 0)).rowcount


def resolve(presented: str) -> str | None:
    """The client agent a presented token belongs to, or None: unknown, revoked, or its client is off
    (disabled, or without board access)."""
    if not isinstance(presented, str) or not presented.startswith(PREFIX):
        return None
    with opstore.connect() as db:
        row = db.execute("SELECT id, agent_id, last_used_at FROM client_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                         (_hash(presented),)).fetchone()
        if row is None:
            return None
        try:
            agent = store.get_agent(row["agent_id"])
        except (store.NotFound, store.InvalidId):
            return None
        if agent.client is None or not agent.client.enabled or not agent.client.board:
            return None
        now = time.time()
        if row["last_used_at"] is None or now - row["last_used_at"] > TOUCH_AFTER:
            db.execute("UPDATE client_tokens SET last_used_at = ? WHERE id = ?", (now, row["id"]))
    return agent.id


def client_id(kind: str, email: str) -> str:
    """`claude-avedelphina` for a Claude client of avedelphina@...: the kind, then the part of the
    email before the @, in the characters an agent id allows."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    local = re.sub(r"[^a-z0-9]+", "-", email.casefold().split("@", 1)[0]).strip("-")[:40]
    ident = f"{kind}-{local}"
    if not local or not ID_RE.fullmatch(ident):
        raise ValueError(f"cannot make a client id from {email!r}")
    return ident


def create(kind: str, email: str, max_tier: int = 3) -> Agent:
    """The client agent for `email`: bound to that operator, board access on, on the clients host."""
    ident = client_id(kind, email)
    try:
        store.get_agent(ident)
    except store.NotFound:
        pass
    else:
        raise ValueError(f"{ident!r} already exists: add a token to it instead")
    try:
        store.get_host(CLIENT_HOST)
    except store.NotFound:
        store.upsert_host(Host.model_validate({"id": CLIENT_HOST, "address": "none.invalid"}))
    return store.upsert_agent(Agent.model_validate({
        "id": ident, "type": "client", "host": CLIENT_HOST, "owner": email.casefold(),
        "client": AgentClient(board=True, acts_for=email, max_tier=max_tier).model_dump()}))
