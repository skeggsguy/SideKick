-- Persist each deferred approval's ActionClass so the inbox, push summary,
-- and per-class timeout can see it. Additive — auto-applies on next open.
-- Pre-migration rows keep NULL (rendered as "unknown" risk in the UI).
ALTER TABLE approvals ADD COLUMN classification TEXT;
