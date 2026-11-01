-- When a fact last surfaced via similarity search (memory_block injection or
-- the recall tool's query branch). NULL = never recalled — the hygiene job's
-- staleness signal; browsing/listing surfaces deliberately do not stamp it.
ALTER TABLE facts ADD COLUMN last_recalled_at TEXT;
