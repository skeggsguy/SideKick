-- Extended thinking (Sonnet 5): break thinking spend out of output_tokens
-- for aggregation. Informational — thinking tokens are already included in
-- output_tokens, so cost_usd math is unchanged.
ALTER TABLE runs ADD COLUMN thinking_tokens INTEGER NOT NULL DEFAULT 0;
