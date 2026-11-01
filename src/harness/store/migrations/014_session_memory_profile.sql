-- The always-on profile/preference facts, rendered once and frozen for the
-- life of the session, appended to the END of the system prompt so a new chat
-- doesn't re-read unchanged prefix bytes. NULL = pre-column / legacy session:
-- its first message still carries the old combined <memory> block, so its
-- system prompt stays byte-identical to the base (no suffix).
ALTER TABLE sessions ADD COLUMN memory_profile TEXT;
