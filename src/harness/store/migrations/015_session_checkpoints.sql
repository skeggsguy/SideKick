-- Compaction checkpoints: the summary text + view metadata that let a resumed
-- session rebuild its compacted context byte-identically instead of
-- re-summarizing every turn (the per-turn cold re-prefill, learnings
-- 2026-10-20). ONE row per session — a later compaction REPLACES it; the full
-- conversation stays in the append-only messages table (decision 2026-06-12,
-- untouched). The compacted view is three parts: frozen opening rows
-- [0, covers_upto_idx) verbatim (oversized tool results truncated), the
-- synthesized <context_checkpoint> message, and tail rows [anchor_idx, ...).
--   covers_upto_idx — exclusive end of the frozen opening; the summarized
--                     middle starts here. FROZEN once written (the opening
--                     never moves)
--   anchor_idx      — the store idx of the first tail message, kept verbatim
--                     below the checkpoint message; grows each compaction
--   created_at      — when the summary froze; rides the framing text so the
--                     checkpoint message re-renders byte-identically
CREATE TABLE IF NOT EXISTS session_checkpoints (
  session_id TEXT PRIMARY KEY,
  id TEXT NOT NULL,
  covers_upto_idx INTEGER NOT NULL,
  anchor_idx INTEGER NOT NULL,
  summary_text TEXT NOT NULL,
  summarized INTEGER NOT NULL,
  est_before INTEGER NOT NULL,
  est_after INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
