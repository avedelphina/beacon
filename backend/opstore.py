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
import time
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
    state       TEXT NOT NULL,     -- queued | delivering | relaying | working | delivered | failed | uncertain | expired
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
    updated_at  REAL NOT NULL,
    reminded_at  REAL,                       -- last reminder to whoever has to move it (board.remind)
    reminders    INTEGER NOT NULL DEFAULT 0, -- reminders since updated_at; counts only when reminded_at >= updated_at
    escalated_at REAL,                       -- people were told by email; again only after the card changes
    next_action TEXT,                        -- the one step that moves the card on
    done_when   TEXT,                        -- what has to be true to close it
    links       TEXT NOT NULL DEFAULT '[]',  -- JSON [{url, label}]: pointers out (PRs, issues), never state
    blocked_reason TEXT,                     -- while blocked: unclear | waiting | cannot (board.BLOCKED_REASONS); NULL on cards blocked before it existed
    waiting_on  TEXT,                        -- waiting: what the card waits on (an agent, workflow:<id>, something outside); cannot: what is missing
    parent_id   INTEGER REFERENCES cards (id)  -- the card this one is part of, if any (board.set_parent: no cycles)
);
CREATE TABLE IF NOT EXISTS card_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER NOT NULL REFERENCES cards (id),
    author      TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- created | comment | state | assign | edit | reference | relation | workflow | assign_notice | comment_notice | reminder | escalation | answer (an agent's answer to a notice)
    body        TEXT NOT NULL,
    context_id  TEXT,
    notice_id   INTEGER REFERENCES mailbox (id),  -- assign_notice, comment_notice: the mailbox row carrying the notice
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS card_events_by_card ON card_events (card_id);

-- Long-term work with an owner and a goal (backend/projects.py). A card belongs to one by name
-- (cards.project), which is how it was before projects were records.
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    goal        TEXT NOT NULL DEFAULT '',
    done_when   TEXT,
    owner       TEXT,                        -- agent id or email; NULL = the operators
    goal_by     TEXT,                        -- who set goal and done_when: only they change them (projects.may_set_goal)
    state       TEXT NOT NULL,               -- active | paused | done | abandoned
    paused_by   TEXT,
    status      TEXT,                        -- the owner's current summary
    status_at   REAL,
    links       TEXT NOT NULL DEFAULT '[]',  -- JSON [{url, label}]: tickets, repositories
    evidence    TEXT,                        -- when done: JSON {links, none_because}; a claim, never checked
    active_at   REAL NOT NULL DEFAULT 0,     -- created, reopened, resumed or planned: the stall clock starts no earlier
    stall_step  INTEGER NOT NULL DEFAULT 0,  -- 1 owner reminded, 2 person told, 3 paused (projects.check_stalled)
    stall_at    REAL,                        -- when; a step older than the last activity no longer counts
    created_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects (id),
    author      TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- created | comment | goal | milestone | owner | status | state | edit
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS project_events_by_project ON project_events (project_id);

-- A project's progress is which of these are met, in order of creation.
CREATE TABLE IF NOT EXISTS project_milestones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects (id),
    title       TEXT NOT NULL,
    done_when   TEXT NOT NULL,
    due         TEXT,              -- YYYY-MM-DD, for people to read; nothing acts on it
    set_by      TEXT NOT NULL,     -- who set title and done_when: only they change or remove it (as a goal)
    state       TEXT NOT NULL DEFAULT 'open',  -- open | done
    evidence    TEXT,              -- as projects.evidence
    closed_by   TEXT,
    closed_at   REAL,
    created_at  REAL NOT NULL
);

-- "card_id cannot finish until depends_on_id is done". A card's blocked_by is computed from these
-- edges and the other card's state, never stored (board.get_card). No cycles (board.add_dependency).
CREATE TABLE IF NOT EXISTS card_dependencies (
    card_id        INTEGER NOT NULL REFERENCES cards (id),
    depends_on_id  INTEGER NOT NULL REFERENCES cards (id),
    created_by     TEXT NOT NULL,
    created_at     REAL NOT NULL,
    PRIMARY KEY (card_id, depends_on_id),
    CHECK (card_id != depends_on_id)
);
CREATE INDEX IF NOT EXISTS card_dependencies_by_target ON card_dependencies (depends_on_id);

CREATE TABLE IF NOT EXISTS workflow_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id             INTEGER NOT NULL REFERENCES cards (id),
    kind                TEXT NOT NULL,     -- decision | review | approval | question | handoff
    state       TEXT NOT NULL,     -- pending | approved | claimed | rejected | changes_requested | expired | withdrawn | executed | verified | failed | execution_uncertain
    requester           TEXT NOT NULL,
    eligible_responder  TEXT NOT NULL,
    capability          TEXT NOT NULL,
    clearance                INTEGER NOT NULL,
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
    responded_at        REAL,
    consulted_json      TEXT NOT NULL DEFAULT '[]',  -- peers the requester asked first: [{agent, said, context_id}]
    consult_skipped     TEXT,                        -- why it did not (a request to a person, no peer consulted)
    consult_flag        TEXT,                        -- "missing": an agent asked a person with neither (workflow._consult_flag)
    require_human       INTEGER NOT NULL DEFAULT 0,  -- 1: a reviewer sent it to a person; only a person in the browser may answer (workflow.escalate)
    escalated_by        TEXT,
    escalated_reason    TEXT,
    answered_by         TEXT,                        -- a client agent that answered, and for whom (workflow._delegate)
    on_behalf_of        TEXT,
    operators_told_at   REAL,                        -- CL4 left waiting on a non-operator owner: when the operators were told (workflow.nudge_operators)
    urgent_reason       TEXT                         -- why it cannot wait for working hours; NULL = it can
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

-- Pointers from a card to work elsewhere (a PR, an issue, a ticket). Free-text provider and
-- kind: a new provider needs no schema change. last_seen_* is what whoever added it last saw,
-- typed by hand; Beacon never fetches or verifies it (backend/board.py, "references").
-- The tokens a client agent (an operator's own AI client) presents to the board: random, shown once when
-- minted, kept only as a hash, revocable one by one (backend/clients.py).
CREATE TABLE IF NOT EXISTS client_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    revoked_at    REAL,
    revoked_by    TEXT
);
CREATE INDEX IF NOT EXISTS client_tokens_by_agent ON client_tokens (agent_id);

-- Deliveries accepted from outside (backend/intake.py): a second one with the same id is a replay.
CREATE TABLE IF NOT EXISTS intake_deliveries (
    source       TEXT NOT NULL,
    delivery_id  TEXT NOT NULL,
    received_at  REAL NOT NULL,
    card_id      INTEGER REFERENCES cards (id),
    claim_token  TEXT,
    PRIMARY KEY (source, delivery_id)
);

CREATE TABLE IF NOT EXISTS card_references (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id          INTEGER NOT NULL REFERENCES cards (id),
    provider         TEXT NOT NULL,
    kind             TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    url              TEXT NOT NULL,
    label            TEXT NOT NULL,
    workflow_id      INTEGER REFERENCES workflow_requests (id),  -- a workflow on the same card
    last_seen_state  TEXT,
    last_seen_at     REAL,
    created_by       TEXT NOT NULL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    UNIQUE (card_id, provider, kind, external_id)
);
CREATE INDEX IF NOT EXISTS card_references_by_card ON card_references (card_id, id);

-- A source-specific adapter can claim one external object while creating its card and
-- reference in the same transaction. This is the cross-process uniqueness boundary;
-- the ordinary card-reference uniqueness remains deliberately card-local for callers
-- that use references as free-form pointers.
CREATE TABLE IF NOT EXISTS card_reference_claims (
    provider       TEXT NOT NULL,
    kind           TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    card_id        INTEGER NOT NULL REFERENCES cards (id),
    reference_id   INTEGER NOT NULL REFERENCES card_references (id),
    created_at     REAL NOT NULL,
    last_event_at  REAL,
    PRIMARY KEY (provider, kind, external_id),
    UNIQUE (card_id, reference_id)
);
CREATE INDEX IF NOT EXISTS card_reference_claims_by_card ON card_reference_claims (card_id);

CREATE TABLE IF NOT EXISTS chat_exposure (
    agent       TEXT PRIMARY KEY,
    open_json   TEXT NOT NULL,     -- names of the settings that open its chat to anyone; [] for none
    reported_at REAL NOT NULL
);

-- The fleet itself (backend/store.py): one row per record.
CREATE TABLE IF NOT EXISTS fleet_records (
    kind        TEXT NOT NULL,     -- host | agent | decommissioned | cron | template | policy
    id          TEXT NOT NULL,
    body        TEXT NOT NULL,     -- JSON of the record; the policy: its YAML text as written
    revision    INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (kind, id)
);
-- What Beacon did on hosts and who asked (backend/audit.py).
CREATE TABLE IF NOT EXISTS host_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    actor       TEXT NOT NULL,     -- the person's email, or "beacon" for the MCP token / open mode / an agent
    via         TEXT NOT NULL,     -- session | mcp | agent | open
    capability  TEXT NOT NULL,     -- a key of clearances.CAPABILITY_CLEARANCES
    clearance        TEXT NOT NULL,     -- CL2 ... CL5
    target      TEXT,              -- the agent (or template) acted on
    detail      TEXT NOT NULL,     -- what the gate said it would do
    outcome     TEXT NOT NULL      -- requested | succeeded | failed | did not finish | interrupted
);
CREATE INDEX IF NOT EXISTS host_actions_by_target ON host_actions (target, id);
CREATE TABLE IF NOT EXISTS fleet_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    rid         TEXT NOT NULL,
    action      TEXT NOT NULL,     -- set | delete | archive | import
    body        TEXT,              -- what the record became; null: deleted
    actor       TEXT NOT NULL,
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fleet_history_by_record ON fleet_history (kind, rid, id);
CREATE TABLE IF NOT EXISTS fleet_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- Operator settings that are not fleet records: who may see intake cards, and who changed it.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_seen (
    agent       TEXT PRIMARY KEY,
    last_pull   REAL NOT NULL,     -- the agent's host sync last pulled /mesh/{agent}
    pulls       INTEGER NOT NULL DEFAULT 1,
    restart_pending INTEGER,       -- 1 while the host sync still owes the agent a gateway restart; NULL = not reported
    health      TEXT,              -- the host sync's last probe: ok | failing | healing | gave-up | network
    health_detail TEXT,
    quiet_notified INTEGER NOT NULL DEFAULT 0,  -- 1 once people were told it went quiet; cleared on its next pull
    sync_hash TEXT  -- sha256 of the sync script the host runs, as it reports it
);

-- Every change of an agent's health as its host sync reports it, kept as the
-- record of what self-healing did and why (backend/availability.py).
CREATE TABLE IF NOT EXISTS health_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    state       TEXT NOT NULL,
    detail      TEXT,
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS health_events_by_agent ON health_events (agent, id);

-- Single-use codes that hand an agent's install script to its host (backend/onboarding.py).
CREATE TABLE IF NOT EXISTS enrollments (
    code_hash       TEXT PRIMARY KEY,   -- sha256 of the code: the table never holds a usable one
    agent           TEXT NOT NULL,
    docker          TEXT,
    docker_user     TEXT,
    created_by      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    fetched_at      REAL,
    pulls_at_fetch  INTEGER
);
CREATE INDEX IF NOT EXISTS enrollments_by_agent ON enrollments (agent, created_at);

-- Browsers that asked for Web Push, one row per browser (backend/push.py).
-- Without Beacon's VAPID key these values cannot send anything to the browser.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint    TEXT PRIMARY KEY,
    email       TEXT NOT NULL,      -- whose browser it is, casefolded
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Phones and tablets logged in through the native app, one row per device (backend/app_devices.py).
-- The token is kept only as a hash; the public key is the device's Secure Enclave key, which signs
-- what needs a person (CL4/CL5). Logging out or revoking deletes the row.
CREATE TABLE IF NOT EXISTS app_devices (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL,      -- whose device it is, casefolded
    name            TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    public_key      TEXT NOT NULL,      -- base64 X9.63 P-256 point
    last_signed_at  REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    apns_token      TEXT,               -- where Apple delivers this device's notifications (backend/apns.py)
    apns_env        TEXT                -- "sandbox" (a build from Xcode) or "production" (TestFlight)
);
CREATE INDEX IF NOT EXISTS app_devices_by_email ON app_devices (email);
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
        _migrate(conn)
        with conn:
            yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent column backfills for tables that predate them.
    `CREATE TABLE IF NOT EXISTS` above never touches an existing table, so a
    new nullable column needs its own guarded `ALTER TABLE` here."""
    seen = {row[1] for row in conn.execute("PRAGMA table_info(agent_seen)")}
    if "pulls" not in seen:
        conn.execute("ALTER TABLE agent_seen ADD COLUMN pulls INTEGER NOT NULL DEFAULT 1")
    if "restart_pending" not in seen:
        conn.execute("ALTER TABLE agent_seen ADD COLUMN restart_pending INTEGER")
    for column, decl in (("health", "TEXT"), ("health_detail", "TEXT"),
                         ("quiet_notified", "INTEGER NOT NULL DEFAULT 0"), ("sync_hash", "TEXT")):
        if column not in seen:
            conn.execute(f"ALTER TABLE agent_seen ADD COLUMN {column} {decl}")
    card_columns = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
    for column, decl in (("reminded_at", "REAL"), ("reminders", "INTEGER NOT NULL DEFAULT 0"),
                         ("escalated_at", "REAL"), ("next_action", "TEXT"), ("done_when", "TEXT"),
                         ("links", "TEXT NOT NULL DEFAULT '[]'"), ("blocked_reason", "TEXT"), ("waiting_on", "TEXT"),
                         ("parent_id", "INTEGER REFERENCES cards (id)"),
                         ("milestone_id", "INTEGER"), ("help_by", "TEXT"), ("help_asked_at", "REAL")):
        if column not in card_columns:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {column} {decl}")
    # "tier" became "clearance" (CL0 to CL5, the DeepOcean operating model's word). Existing databases keep their
    # rows: the column is renamed, and the names stored in host_actions ("T2") become "CL2".
    for table in ("workflow_requests", "host_actions"):
        names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "tier" in names and "clearance" not in names:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN tier TO clearance")
    # Only when there is something to change: every connection runs this, and a write each time would collide.
    if conn.execute("SELECT 1 FROM host_actions WHERE clearance GLOB 'T[0-5]' LIMIT 1").fetchone():
        conn.execute("UPDATE host_actions SET clearance = 'CL' || substr(clearance, 2) WHERE clearance GLOB 'T[0-5]'")
    request_columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_requests)")}
    for column, decl in (("consulted_json", "TEXT NOT NULL DEFAULT '[]'"), ("consult_skipped", "TEXT"),
                         ("consult_flag", "TEXT"), ("require_human", "INTEGER NOT NULL DEFAULT 0"),
                         ("escalated_by", "TEXT"), ("escalated_reason", "TEXT"),
                         ("answered_by", "TEXT"), ("on_behalf_of", "TEXT"), ("operators_told_at", "REAL"),
                         ("urgent_reason", "TEXT")):
        if column not in request_columns:
            conn.execute(f"ALTER TABLE workflow_requests ADD COLUMN {column} {decl}")
    _migrate_projects(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(card_events)")}
    if "notice_id" not in columns:
        conn.execute("ALTER TABLE card_events ADD COLUMN notice_id INTEGER REFERENCES mailbox (id)")
    delivery_columns = {row[1] for row in conn.execute("PRAGMA table_info(intake_deliveries)")}
    if delivery_columns and "claim_token" not in delivery_columns:
        conn.execute("ALTER TABLE intake_deliveries ADD COLUMN claim_token TEXT")
    device_columns = {row[1] for row in conn.execute("PRAGMA table_info(app_devices)")}
    for column in ("apns_token", "apns_env"):
        if device_columns and column not in device_columns:
            conn.execute(f"ALTER TABLE app_devices ADD COLUMN {column} TEXT")
    claim_columns = {row[1] for row in conn.execute("PRAGMA table_info(card_reference_claims)")}
    if claim_columns and "last_event_at" not in claim_columns:
        conn.execute("ALTER TABLE card_reference_claims ADD COLUMN last_event_at REAL")


def _migrate_projects(conn: sqlite3.Connection) -> None:
    """Once: every free-text project name on a card becomes a project with no goal and no owner
    (the operators'). Names that differ only in case or outer spaces become one."""
    if conn.execute("SELECT 1 FROM settings WHERE key = 'projects_migrated'").fetchone():
        return
    now = time.time()
    with conn:
        conn.execute("UPDATE cards SET project = NULLIF(trim(project), '') WHERE project IS NOT NULL")
        conn.execute("INSERT OR IGNORE INTO projects (name, state, created_by, created_at, updated_at)"
                     " SELECT project, 'active', 'beacon', ?, ? FROM cards WHERE project IS NOT NULL ORDER BY id",
                     (now, now))
        conn.execute("UPDATE cards SET project = (SELECT name FROM projects WHERE projects.name = cards.project)"
                     " WHERE project IS NOT NULL")
        conn.execute("INSERT OR IGNORE INTO settings (key, value, updated_by, updated_at)"
                     " VALUES ('projects_migrated', '1', 'beacon', ?)", (now,))
