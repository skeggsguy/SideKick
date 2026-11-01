---
name: new-tool
description: Use BEFORE writing any harness tool code (in-process or MCP server)
---

# Adding a tool — guided workflow

**Read this first:** `docs/adding-a-tool.md` is the single source of truth. This skill adds ordering and the traps that doc's amendments carry.

## Steps

1. **Read the full doc** — `docs/adding-a-tool.md`. It IS the checklist; this skill only layers process order on top.

2. **Decide in-process vs MCP.** For a *brand-new* server (not extending `google_workspace` or `ms365`), **STOP** — that's a design conversation. Cite the gotchas entries: per-server tool-name prefix (`outlook_*` collision guard), and HARNESS_FILE_ROOTS if it touches local files.

3. **Classify safety tier** — apply the doc's Safety tiers table. Explicitly answer three questions before settling:
   - *Does it write anything to local disk?* (downloads, cursors, temp files) → at least `MUTATING_LOCAL`, regardless of what the name suggests.
   - *Does it send data outbound over the network?* → must join `EGRESS_TOOLS` in `src/harness/core/policy.py` (~line 68) or the secret-read taint won't gate it.
   - *If gated: would the approval modal show only opaque ids or blobs?* → declare approval context per the doc's "Approval context" bullet: a resolver in `src/harness/gateway/preview.py` (`RESOLVER_TOOLS`) + an `approval_summary` push template, or `SELF_DESCRIBING_GATED`. The invariant test (`tests/test_invariants.py`) fails until the choice is made.

4. **Write + register per the doc** (Path A for in-process, Path B for MCP). Conditional registration if keyed.

5. **Offline test** — name the pattern files:
   - `httpx.MockTransport` for HTTP (see `tests/test_web_search.py`)
   - `_FakeService` for Google (see `tests/test_workspace_writes.py`)
   - If outbound, add a deny-when-tainted case in `tests/test_policy.py` (`test_taint_gates_egress_tools`) alongside web_fetch/web_search. Check if `tests/test_invariants.py`'s exfil-chain test should cover it too.

6. **`uv run pytest`** — full offline suite green.

7. **Daemon refresh** — restart agentd (docs/macos-host.md → Restart agentd); verify `~/Library/Logs/harness/agentd.log` shows `listening` with no traceback. Re-auth only if a scope changed.

8. **Self-check** — tick every box from the doc's final checklist verbatim.

## Guardrails

- Never classify by tool *name* (the `gmail_download_attachment` audit proves this). Unsure? Stricter tier.
- An approval prompt the user can't evaluate is a rubber-stamp gate — never ship id-only prompts. And never surface model-supplied "friendly name" args as approval context: injected content can lie about what's being deleted; resolvers live-fetch the truth via READ_ONLY reads.
- `MUTATING_LOCAL` is the only allowlistable tier — don't widen.
- Never test against real accounts; all fixtures are offline.
- Schedule-adjacent tools must consider the ALWAYS_ASK_TOOLS four-surface sync (gotchas.md entry: policy.py, /api/tools allowlistable/always_ask, POST /api/allowlist, settings.js locked label).
