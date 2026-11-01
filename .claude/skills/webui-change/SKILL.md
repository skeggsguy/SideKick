---
name: webui-change
description: Use when making changes to the web UI (HTML, CSS, JavaScript)
---

# Web UI changes — guided workflow

## Steps

1. **Before moving/renaming any webui file**, grep for it in tests:
   ```bash
   grep -rn <filename> tests/
   ```
   `tests/test_connections.py` regex-pins SCOPE_INFO by file path — update the regex if the filename moves.

2. **Make the change** under these standing rules (each a one-line pointer to its gotcha.md entry):
   - Untrusted text (tool args, results, model/Qwen-generated chat titles) → `textContent` or `marked→DOMPurify` only. Never innerHTML of model-influenced content.
   - Shared transcript state via the `ui` object properties + `closeSegments()`, never cross-module writes.
   - Focusable inputs ≥16px or iPad Safari zooms the page.
   - CSP stays on static responses (NoCacheStatic), never middleware.
   - Both stream renderers (chat SSE, /api/events) guard on `ui.sessionId`.
   - Client devices are an iPad and an iPhone, both Safari home-screen PWAs (Apple-only since 2026-10-17, no Android) — WebKit is the only engine to satisfy, and a WebKit-only assumption is now safe.

3. **Cache-bust**: bump `?v=` on `style.css` and the app.js ENTRY tag in `src/harness/gateway/webui/index.html`:
   ```html
   <link rel="stylesheet" href="style.css?v=NN">
   <script type="module" src="app.js?v=MM" async></script>
   ```
   ES submodules (dom.js, runs.js, settings.js, etc.) ride no-cache revalidation — no per-file version needed.

4. **Python gateway changes** → restart agentd (docs/macos-host.md → Restart agentd). Pure static changes are picked up on reload (NoCacheStatic revalidates).

5. **`uv run pytest`** — gateway/webui tests green.

6. **For any change with visible behavior**, spawn the **webui-verify** subagent and act on its verdict. Skip only for trivial copy changes.

## Guardrails

- Never let a file move silently break a test pointer.
- Don't revert the XSS/transcript/input-size/CSP rules — they're load-bearing.
- Always bump both version tags per phase, or cache will serve stale code.
