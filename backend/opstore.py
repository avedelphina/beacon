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
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A connection inside one transaction: committed on success, rolled back on error."""
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
