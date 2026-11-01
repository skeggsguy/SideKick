# Browser fallback setup (`browser_fetch` + `browser_delegate`)

The harness's browser layer, replacing the retired Playwright-in-Docker
`js_fetch`:

- **Lane A — `browser_fetch`** (READ_ONLY): raw CDP over WebSocket to a
  *persistent real Chrome* (headed), dedicated profile.
  Deterministic: navigate → settle → read `outerHTML`, then the same extraction
  pipeline as `web_fetch`. Covers Cloudflare-gated and JS-rendered pages.

- **Lane B — `browser_delegate`** (OUTWARD): general browser *control* via a
  gated subagent. The main agent hands a whole task ("find a table, book it or
  give me the link") to a child agent bound to ONE site by a **delegation
  envelope** — origin + action budget + optional identity refs — that the human
  approves once at spawn. Inside the envelope the child navigates/clicks/types
  autonomously over the same agent Chrome; the policy engine re-prompts only
  at machinery-detected boundaries (origin change, first `{email}`/`{phone}`
  fill per site, budget exhaustion). It never logs in and can never enter
  payment details. See "Lane B" below.

## Why real Chrome, why no stealth libs

Real Chrome auto-updates, so its fingerprint stays current for free, forever —
the single most important maintenance decision. Stealth libraries and pinned
Playwright/Chromium versions rot against Cloudflare changes (and Playwright's
`connectOverCDP` calls `Runtime.enable`, a known detection signal — the CDP
client here never touches the Runtime domain). No CAPTCHA solvers: challenges
**surface, never escalate** — one patient retry, then the tab is left open and
the model tells you.

## Status on the Mac mini: RUNNING (since 2026-09-30)

The lane ran on WSL (systemd units + WSLg) until the 2026-09-28 cutover; the
launchd port landed 2026-09-30. Artifacts live in `scripts/launchd/` — copy
the three plists to `~/Library/LaunchAgents/` and
`launchctl bootstrap gui/501 <plist>` (egress-proxy first):

- `com.harness.egress-proxy.plist` → `.venv/bin/egress-proxy` on 127.0.0.1:18080
- `com.harness.chrome-agent.plist` → runs `chrome-agent-wrapper.sh` (waits for
  18080, then execs Chrome with the exact flag set below)
- `com.harness.chrome-agent-restart.plist` → Sun 04:00 `kickstart -k`

Logs: `~/Library/Logs/harness/{egress-proxy,chrome-agent}.log`. **First-launch
trap on a headless Mac**: Homebrew cask leaves `com.apple.quarantine` on
Chrome.app; the first launch then waits on a Gatekeeper consent dialog that a
headless machine can never show, and the pending verdict blocks every later
launch of the same cdhash (process freezes in `_dyld_start`). Strip the xattr
BEFORE the first launch (`xattr -dr com.apple.quarantine "/Applications/Google
Chrome.app"`); if already stuck, `sudo killall syspolicyd` clears the cache.

## launchd port spec (carry these over verbatim)

The retired systemd units (`git show da386d5:scripts/chrome-agent.service`
etc.) encoded these; the proxy is the load-bearing SSRF layer, so NONE of the
Chrome flags may be weakened:

1. **chrome-proxy** — `<repo>/.venv/bin/egress-proxy --host 127.0.0.1 --port 18080`,
   KeepAlive. Must be up BEFORE Chrome starts (launchd has no ordering: have
   the Chrome agent's wrapper wait for 18080, or accept that Chrome with the
   proxy down simply has no egress — fail closed).
2. **chrome-agent** — the real, auto-updating Google Chrome
   (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`), headed
   (no `--headless`), KeepAlive, with exactly:
   ```
   --user-data-dir=$HOME/.chrome-agent-profile
   --remote-debugging-port=19222 --remote-debugging-address=127.0.0.1
   --proxy-server=127.0.0.1:18080 --proxy-bypass-list=<-loopback>
   --force-webrtc-ip-handling-policy=disable_non_proxied_udp
   --disable-background-networking
   --no-first-run --no-default-browser-check
   --disable-features=Translate,MediaRouter
   ```
   (The WSL unit's `DISPLAY`/`PULSE_SERVER`/`MemoryMax=2G` have no macOS
   equivalent and are dropped.) Headed Chrome under launchd needs the GUI
   session: a LaunchAgent in the logged-in user's domain, auto-login on, and
   the screen lock verified not to block it.
3. **Weekly restart** (was Sun 04:00, `StartCalendarInterval` in launchd) —
   makes an auto-updated binary actually run AND clears tabs left open by
   blocked pages (gotchas.md relies on it).
4. **Verify**: `curl -s 127.0.0.1:19222/json/version` returns JSON with a
   `webSocketDebuggerUrl`; `lsof -nP -iTCP:18080 -sTCP:LISTEN` shows the
   proxy. Then restart agentd (docs/macos-host.md) so it picks up the tools.

## The egress proxy's role

A browser is a general-purpose network client: it re-resolves hostnames itself
and follows subresources, so `web_fetch`-style per-hop pinning can't be
reproduced tool-side, and host firewalls can't reliably filter it. Egress is
therefore enforced by `harness.egress_proxy` (the chrome-proxy service,
127.0.0.1:18080): Chrome is locked to it (`--proxy-server`, with the implicit
loopback bypass disabled via `<-loopback>`), the proxy resolves every target
itself, refuses anything not globally routable (private, loopback, link-local,
CGNAT/tailnet), restricts ports to http(s), and connects to the exact IP it
validated — closing DNS rebinding. `--force-webrtc-ip-handling-policy=
disable_non_proxied_udp` and `--disable-background-networking` close the
flag-level direct-connect paths. `browser_fetch` additionally pre-checks the
top URL with the same `check_target` guard as `web_fetch` (defense in depth).

The proxy must be up before Chrome starts (see the launchd spec above); if
the proxy is down, Chrome has no egress at all — fail closed.

## Profile strategy (do NOT copy a personal Chrome profile)

Lane A uses a dedicated `~/.chrome-agent-profile`. On the same residential IP,
running real auto-updating Chrome, it warms its own `cf_clearance` cookies over
normal use (~25 loads/week). Do **not** copy your personal profile over
(nor the old WSL `~/.chrome-agent-profile` — it didn't migrate):

1. **Cookies don't transplant** — Chrome encrypts the Cookies DB with an
   OS-bound key (the macOS Keychain here), so a copied profile's sessions
   (including every `cf_clearance`) are unreadable.
2. **It wouldn't help Cloudflare anyway** — clearance is IP + live fingerprint +
   a UA-bound cookie; a cookie minted elsewhere is re-challenged.
3. **Security** — a copy reintroduces your logged-in credentials into the blast
   radius of fallback URLs (prompt-injection → exfiltration), exactly what the
   dedicated profile exists to prevent.

**The one legitimate case** — reading a site you're logged into (an auth wall,
not a bot wall): because the agent Chrome is headed, log into that
specific site **once, by hand**, in the agent Chrome window. Fresh,
correctly-UA'd, single-site session; no wholesale credential import.

## Blocked pages

When a challenge persists past the one 6s retry, `browser_fetch` returns a
blocked message and **leaves the tab open** in the agent Chrome window — click
through it there, then have the model retry the URL. It never loops and never
solves. The weekly restart clears any accumulated open tabs (ported:
`com.harness.chrome-agent-restart`, Sun 04:00).

## Lane B (`browser_delegate` — envelope-gated subagent)

History: the original `browser_task` (a shell around `claude --chrome`) was
removed 2026-07-16 — Claude-in-Chrome was unsupported on the then-host (WSL)
and silently attached no browser tools, so it never drove a browser. The
replacement built 2026-07-17 is the v3 design (as-built summary:
docs/lane-b-v3.md): **in-process**, policy-gated CDP interaction
against the *same* agent Chrome, driven by a subagent inside a
once-approved delegation envelope.

How it works:

- The main agent calls `browser_delegate(origin, task, refs=[], budget=40)`.
  The call is OUTWARD, so the human approves the envelope — the args ARE the
  approval card. A fresh child agent (main model tier) gets a fresh tab and a
  registry holding ONLY the six interaction tools
  (`browser_navigate/snapshot/read/click/type/scroll`); those six are NOT in
  the main catalog — an envelope is the only way to use them.
- Inside the envelope the child is autonomous: every action decrements the
  budget and writes an audit row, but nothing prompts. Boundary re-prompts are
  machinery-fired only: the tab leaving the origin (`origin_change`), the first
  `{email}`/`{phone}` fill per site (`ref_resolve` — the approval card shows the
  real stored value; the model never sees it, the value is inserted at CDP
  dispatch from Settings → Contact details), and budget exhaustion. There is
  deliberately NO submit gate — intent is approved in the task at spawn, and
  capability is bounded by the refs list (empty refs = contact fills are
  mechanically impossible).
- Hard scope lines: the child never logs in, never creates accounts, and there
  is no card ref — payment flows stall at an unfillable field. Three
  first-class outcomes: data / deep link (expected to dominate) / completed.
  A challenge page ends the run as `blocked` and **leaves the tab open** in the
  headed window (same policy as Lane A; the weekly restart clears it).
