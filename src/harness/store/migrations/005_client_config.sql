-- Client-reported settings (key/value), e.g. the last-seen client timezone.
-- One row per key, updated in place.
CREATE TABLE client_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
