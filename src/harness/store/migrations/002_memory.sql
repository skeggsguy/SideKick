-- Memory: durable facts about the user + episode summaries of past runs.
--
-- The vector indexes (vec_facts, vec_episodes) are vec0 VIRTUAL tables and
-- are NOT created here: virtual tables need the sqlite-vec extension loaded
-- on the creating connection, and migrations must run on a plain connection.
-- MemoryStore.setup() creates them idempotently when the extension loads;
-- without it the harness degrades to keyword recall.

CREATE TABLE facts (
    id            INTEGER PRIMARY KEY,
    content       TEXT NOT NULL,
    category      TEXT NOT NULL,            -- profile|preference|project|contact|routine
    confidence    REAL NOT NULL DEFAULT 0.8,
    source_run_id TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_facts_active_category ON facts (active, category);

CREATE TABLE episodes (
    id         INTEGER PRIMARY KEY,
    run_id     TEXT,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
