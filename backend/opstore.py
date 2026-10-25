"""Beacon's operational store: SQLite for runtime state that must not live in
the git-tracked fleet YAML — the mailbox now, the board and observations next
(see ROADMAP.md). Fleet YAML answers "what do we want"; this answers "what is
happening".

One file under BEACON_STATE_DIR (default: state/ next to fleet/), stdlib
sqlite3, and a short-lived connection per use: Beacon's traffic is small, and
FastAPI runs sync routes on a threadpool, where a shared connection would need
locking for no gain. WAL lets readers and the one writer overlap.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

STATE_DIR = Path(os.environ.get("BEACON_STATE_DIR", Path(__file__).resolve().parent.parent / "state"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender      TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- message | reply | notice
    body        TEXT NOT NULL,
    context_id  TEXT,
    reply_to    INTEGER,
    state       TEXT NOT NULL,     -- queued | delivering | delivered | failed | expired
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS mailbox_by_recipient ON mailbox (recipient, state);

CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    project     TEXT,
    assignee    TEXT,
    state       TEXT NOT NULL,     -- todo | doing | blocked | review | done
    created_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS card_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER NOT NULL REFERENCES cards (id),
    author      TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- created | comment | state | assign | edit
    body        TEXT NOT NULL,
    context_id  TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS card_events_by_card ON card_events (card_id);

CREATE TABLE IF NOT EXISTS workflow_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id             INTEGER NOT NULL REFERENCES cards (id),
    kind                TEXT NOT NULL,     -- decision | review | approval
    state       TEXT NOT NULL,     -- pending | approved | claimed | rejected | changes_requested | expired | withdrawn | executed | verified | failed | execution_uncertain
    requester           TEXT NOT NULL,
    eligible_responder  TEXT NOT NULL,
    capability          TEXT NOT NULL,
    tier                INTEGER NOT NULL,
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL,
    options_json        TEXT NOT NULL DEFAULT '[]',
    response_json       TEXT,
    target_json         TEXT NOT NULL DEFAULT '{}',
    plan_revision       TEXT NOT NULL,
    plan_hash           TEXT NOT NULL,
    expires_at          REAL NOT NULL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    responded_at        REAL
);
CREATE INDEX IF NOT EXISTS workflow_requests_by_card ON workflow_requests (card_id, created_at DESC);
CREATE INDEX IF NOT EXISTS workflow_requests_by_state ON workflow_requests (state, expires_at);

CREATE TABLE IF NOT EXISTS workflow_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL REFERENCES workflow_requests (id),
    actor       TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- created | responded | state_changed | expired
    from_state  TEXT,
    to_state    TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_events_by_request ON workflow_events (request_id, id);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A connection inside one transaction: committed on success, rolled back on error.

    Workflow requests are durable operational records alongside the mailbox and
    board, not a replacement for fleet YAML.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DIR / "beacon.db", timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()
