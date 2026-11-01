-- Proactivity: cron/watch triggers, watcher cursors, and Web Push
-- subscriptions for the iPad PWA. (002 is reserved for memory, built in P4.)

CREATE TABLE triggers (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,              -- cron | watch
    spec          TEXT NOT NULL,              -- '30 7 * * 1-5' | watcher name
    playbook      TEXT NOT NULL,              -- playbook name, or inline prompt for one-off reminders
    channel       TEXT NOT NULL DEFAULT 'web',
    autonomy      TEXT NOT NULL DEFAULT 'supervised',
    enabled       INTEGER NOT NULL DEFAULT 1,
    one_shot      INTEGER NOT NULL DEFAULT 0, -- reminders: disable after first fire
    last_fired_at TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE watch_state (
    watcher    TEXT PRIMARY KEY,
    cursor     TEXT,                          -- e.g. Gmail historyId
    updated_at TEXT NOT NULL
);

CREATE TABLE push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint   TEXT NOT NULL UNIQUE,
    keys_json  TEXT NOT NULL,                 -- {p256dh, auth} from the browser
    created_at TEXT NOT NULL
);
