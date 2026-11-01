# Adding a tool

A checklist for adding a new tool to the harness — written so an AI assistant
(or a human) can follow it end-to-end without missing a step. There are **two
kinds of tool**; pick the matching path. The most-forgotten steps are the
**safety classification** and the **post-change refresh** (daemon restart /
re-auth) — get those wrong and the tool either won't appear, won't fire, or
fires un-gated.

## First decide: in-process or MCP?

- **In-process tool** — pure Python that runs inside the harness (file I/O,
  local computation, DB reads, an HTTP call to a service). Lives in
  `src/harness/tools/`. Use this by default.
- **MCP server tool** — a capability exposed by one of our FastMCP servers
  under `mcp_servers/` (currently `google_workspace` for Gmail/Calendar/Drive/
  Sheets/Tasks, `ms365` for Outlook). Add here **only** when extending an
  existing integration; the tool is a plain function the server registers.

The safety tiers are the same for both (see below). What differs is where the
code lives and how it's classified.

---

## Path A — in-process tool

1. **Write the tool** in a new (or existing) module under `src/harness/tools/`.
   Subclass `Tool` from `tools/base.py`. Model it on a small existing one —
   `tools/run_stats.py` is the cleanest example. Set:
   - `name`, `description` (the model reads this — say what it does *and when to
     use it*), `input_schema` (JSON Schema for the args object).
   - `classification: ActionClass` — see **Safety tiers** below. This is the
     single most important line: it decides whether the call auto-runs or needs
     approval. **Key rule-of-thumb: download/fetch tools that write to disk are
     `MUTATING_LOCAL` regardless of their name** (classify by what `run()` *does*,
     not its label).
   - `remote = True` **iff** the tool reaches the network — the agent runs a
     connectivity pre-flight for those.
   - `async def run(self, args)` returns a **string** for the model. Do blocking
     work via `asyncio.to_thread`; never block the event loop. No bare `except`
     — return a clear error string, never swallow or raise into the turn.
   - **Approval context** — if the tool is gated (anything above `READ_ONLY`),
     decide how a human evaluates the call at approval time. Ask three
     questions: (a) do the args contain **opaque ids** (file_id, trigger_id)?
     (b) **blobs** the modal shouldn't dump raw? (c) **consequential info
     that's implicit/derived** (e.g. a reply's recipient)? If any: add a
     resolver in `src/harness/gateway/preview.py` (`RESOLVER_TOOLS`) that
     live-fetches the human-readable target via a READ_ONLY read, plus a push
     template in `approval_summary` (gateway/agentd.py); optionally a bespoke
     `approvalFields` template in webui/approvals.js (the generic preview
     renderer covers resolver-backed tools without one). If none: add the name
     to `SELF_DESCRIBING_GATED`. **The invariant test fails until you choose**
     (tests/test_invariants.py). Resolvers must only dispatch READ_ONLY tools —
     and never trust model-supplied "friendly name" args for display; injected
     content can lie about what's being deleted.
2. **Register it** in `src/harness/composition.py` → `_register_tools`, next to the other
   `registry.register(...)` calls. If it needs config (an API key, a store),
   register it **conditionally** so an unconfigured tool never appears in the
   catalog — copy the `if brave_key:` / `if run_store is not None:` pattern.
   Register before `browser_delegate`, which is registered last in `build_agent`
   (it needs the built agent).
3. **If the tool sends data outbound**, add it to `EGRESS_TOOLS` in
   `src/harness/core/policy.py` (~line 68) — a query/search/fetch string is an
   exfiltration channel exactly like web_search. The secret-read taint will then
   gate it. Also add a deny-when-tainted test case for it in `tests/test_policy.py`
   (`test_taint_gates_egress_tools`), alongside the web_fetch/web_search cases.
4. **Test it** — add an offline test under `tests/` (mock any network/HTTP via
   `httpx.MockTransport`, like `providers/ollama_provider.py` does). No real
   API calls; the suite is offline.

## Path B — MCP tool (extending a server like google_workspace)

**For a new server (not google_workspace/ms365), STOP and discuss first.** Two gotchas: per-server tool-name prefix (e.g. `outlook_*` to avoid collisions), and if it touches local files, HARNESS_FILE_ROOTS env injection (see gotchas.md).

1. **Write the function** in the right per-service module (e.g.
   `mcp_servers/google_workspace/sheets.py`). It's a **plain sync function**;
   FastMCP derives the schema from its **type hints + docstring**, so both must
   be accurate and complete. Keep all API-specific index/range math inside the
   function — the model only passes simple args. No bare `except`; raise a clear
   `RuntimeError` on bad input.
2. **Register it** in that server's `server.py` — add the function to the
   registration loop (group it under read / local-write / outward to match the
   existing comments).
3. **Classify it** in `src/harness/mcp/servers.toml` under the matching server.
   ⚠️ **A tool not listed in any list defaults to OUTWARD (gated).** Put the
   name in exactly one of `read_only`, `mutating_local`, or `outward`. (Reads →
   `read_only`; writes to the user's own account with no send/share →
   `mutating_local`; anything that sends/shares/invites externally → `outward`.)
4. **Approval context** (gated tools) — same requirement as Path A step 1's
   bullet: opaque ids / blobs / implicit-derived info ⇒ a resolver in
   `src/harness/gateway/preview.py` + an `approval_summary` push template;
   otherwise `SELF_DESCRIBING_GATED`. The invariant test enumerates any
   gated tool that hasn't chosen. MCP ids are the common case here — a
   `file_id`-only modal is a rubber-stamp gate.
5. **New OAuth scope?** For **Google**, almost certainly not: the scopes are now
   broad per service (full Gmail-modify, Calendar, Drive, Tasks, Docs, Sheets in
   `mcp_servers/google_workspace/auth.py`), so a new Google tool is steps 1–4
   only — no `SCOPES` edit, no re-auth. The rare exception is a capability
   outside those (e.g. Gmail permanent delete needs `https://mail.google.com/`).
   For **other servers**, if the tool needs a scope not already requested, add it
   to `SCOPES` in that server's `auth.py` — this forces a **one-time re-auth**.
   The user reconnects from the web UI **Connections** tab (Google), or re-runs
   the server's `--auth --manual` (CLI fallback; see `docs/google-oauth-setup.md`).
6. **Test it** — extend the server's offline test file (e.g.
   `tests/test_workspace_writes.py`) with a **mocked service object** — never a
   real account. See the `_FakeService` / `_FakeRequest` pattern there.
7. **Tool group** — on the local lane MCP definitions are deferred behind
   `load_tools` / `call_tool`, grouped by NAME PREFIX (`GROUP_PREFIXES` in
   `src/harness/core/tool_groups.py`). A name matching no prefix is not an
   error — it just stays always-on, costing prompt tokens every turn. New
   family (e.g. a new server's `foo_*`)? Add its prefix there;
   `tests/test_tool_groups.py::test_every_configured_mcp_tool_has_a_group`
   fails for any servers.toml name without one. An IN-PROCESS tool is always
   sent unless named in `GROUP_TOOLS` (same file) — and the default is to
   name it: always-on is reserved for what the system prompt tells the model
   to call unprompted (current_time, convert_time, recall), the everyday web
   tools (web_search, web_fetch, weather), and notify (tools-less reminders
   run grouped and must alert without a load_tools step). Even the file and
   sandbox tools are grouped (`files`). Put the tool's usage guidance in its
   own description, not the system prompt (`src/harness/prompt.py` holds only
   cross-cutting rules); measure with `scripts/prompt_budget.py`.

---

## Safety tiers (`ActionClass`)

Defined in `src/harness/core/policy.py`. Choosing the right one is the whole
game:

| Tier              | When                                              | Gating |
|-------------------|---------------------------------------------------|--------|
| `READ_ONLY`       | reads only, no side effects                       | auto-runs, never prompts |
| `MUTATING_LOCAL`  | writes confined to the user's own account/machine, recoverable, no outward effect (a draft, an own-calendar event, an own-Drive file) | needs approval; **allowlistable** ("always") |
| `OUTWARD`         | sends/shares/posts/invites — anything another person sees (send mail, share a file, invite attendees) | always prompts; **never** allowlisted |
| `DESTRUCTIVE`     | irreversible loss (delete, overwrite-without-recovery, drop a table) | always prompts; **never** allowlisted |

Rules of thumb that have bitten us before:
- **When unsure, pick the stricter tier.** Under-classifying an outward action
  is a real safety hole (autonomous send / prompt-injection → send).
- `MUTATING_LOCAL` is the **only** allowlistable tier — so OUTWARD always shows
  a human the full content before it leaves. Don't widen this.
- "Own calendar event with no attendees" is local; "the moment it invites
  someone" it's OUTWARD. Same shape for draft (local) vs send (outward).
- **Every gated tier implies the approval-context requirement** (Path A step 1 /
  Path B step 4): the human approving the call must be able to tell what it
  actually touches — an id-only prompt is a rubber-stamp gate.

## After the change — the easy-to-forget refresh

- **Run the tests:** `uv run pytest` (must stay green, offline).
- **Restart the daemon** so it reloads the code + `servers.toml`:
  restart agentd (docs/macos-host.md → Restart agentd) (MCP servers are spawned at agentd startup,
  so a running daemon has the *old* tools until restarted). Confirm it came up:
  `~/Library/Logs/harness/agentd.log` shows `listening` with no traceback.
- The interactive REPL (`uv run harness`) picks up changes on next launch — no
  restart needed.
- **Re-auth** if you added a scope (Path B step 5).

## Quick self-check before calling it done

- [ ] Tool has an accurate `description` / docstring (the model relies on it)
- [ ] Correct `ActionClass` / `servers.toml` list — *not* accidentally OUTWARD
- [ ] Classification by *action* (what does `run()` do?), not by name
- [ ] If it writes to disk OR sends outbound: classified correctly + added to EGRESS_TOOLS (if outbound) with a taint test
- [ ] Gated tool declared in `RESOLVER_TOOLS` or `SELF_DESCRIBING_GATED`
      (`src/harness/gateway/preview.py`; test_invariants pins it) — with an
      `approval_summary` push template if resolver-backed
- [ ] Registered (`main.py` for in-process, `server.py` for MCP)
- [ ] MCP tool name matches a `GROUP_PREFIXES` entry (core/tool_groups.py), or it stays always-on
- [ ] `remote = True` if it hits the network (in-process only)
- [ ] Offline test added, `uv run pytest` green
- [ ] Daemon restarted; re-auth done if a scope was added
