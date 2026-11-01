-- Core durable state: conversations, run telemetry, the audit trail, and the
-- approval machinery. The approvals table is created now but only used from
-- Phase 3 (deferred approvals for headless runs).

CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL DEFAULT 'cli',
    title       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    idx          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (session_id, idx)
);

CREATE INDEX idx_messages_session ON messages(session_id, idx);

CREATE TABLE runs (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT REFERENCES sessions(id),
    trigger            TEXT NOT NULL DEFAULT 'user',  -- 'user' | 'cron:<id>' | 'watch:<id>'
    channel            TEXT,
    playbook           TEXT,
    status             TEXT NOT NULL,                 -- running | ok | error | killed
    model              TEXT,
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0
);

CREATE TABLE run_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(id),
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,   -- model_response | tool_call | tool_result | policy_decision | error
    data_json TEXT NOT NULL
);

CREATE INDEX idx_run_events_run ON run_events(run_id);

-- Append-only: what did the agent do to the world, and who approved it.
CREATE TABLE audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT,
    ts             TEXT NOT NULL,
    tool           TEXT NOT NULL,
    args_json      TEXT NOT NULL,
    classification TEXT NOT NULL,  -- READ_ONLY | MUTATING_LOCAL | OUTWARD | DESTRUCTIVE
    decision       TEXT NOT NULL,  -- auto | approved | denied | deferred
    result_digest  TEXT,
    is_error       INTEGER NOT NULL DEFAULT 0
);

-- Deferred approvals for headless runs (used from Phase 3).
CREATE TABLE approvals (
    id          TEXT PRIMARY KEY,
    run_id      TEXT,
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    status      TEXT NOT NULL,  -- pending | approved | denied | expired
    channel_ref TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

-- Persisted "always" answers from interactive approval prompts.
CREATE TABLE allowlist (
    tool       TEXT NOT NULL,
    pattern    TEXT NOT NULL DEFAULT '*',
    created_at TEXT NOT NULL,
    PRIMARY KEY (tool, pattern)
);
