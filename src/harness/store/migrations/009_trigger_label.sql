-- A short, human-facing name for a trigger. Additive — auto-applies on next
-- open. Optional: NULL for pre-migration rows and for jobs created without one,
-- in which case displays fall back to a truncated playbook/prompt. This is what
-- keeps inline reminders (whose whole prompt lives in the playbook column) from
-- rendering their entire text as a title.
ALTER TABLE triggers ADD COLUMN label TEXT;
