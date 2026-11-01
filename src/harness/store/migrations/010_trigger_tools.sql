-- Per-job tool allocation: JSON array of catalog tool names. NULL = no
-- allocation (legacy rows resolve playbook frontmatter at fire time, or get
-- the full toolset). Autonomous jobs require a non-empty allocation, enforced
-- at create/edit (tools/schedule_tools.validate_job) — the column itself
-- stays permissive.
ALTER TABLE triggers ADD COLUMN tools TEXT;
