# Google OAuth setup (one-time)

The `google_workspace` MCP server needs an OAuth client of your own. This is a
one-time, ~10-minute setup in Google Cloud Console, then a single login — after
that you re-auth from the iPad/web UI with a tap, and new Google tools never
need another login.

## 1. Create the OAuth client in Google Cloud Console

1. Go to [console.cloud.google.com](https://console.cloud.google.com) (signed
   in to the Google account the assistant will use) and create a project, e.g.
   `harness`.
2. **Enable APIs** (APIs & Services → Library): enable each of
   **Gmail API**, **Google Calendar API**, **Google Drive API**, **Tasks API**,
   **Google Docs API**, **Google Sheets API**.
3. **Consent screen** (APIs & Services → OAuth consent screen / "Google Auth
   Platform"): choose **External**, fill in the app name + your email, and add
   your own Google address as a **test user**.
4. **Credentials** → Create credentials → **OAuth client ID** →
   Application type **Web application**. Under **Authorized redirect URIs** add
   both of:
   - `https://<host>.<tailnet>.ts.net/api/oauth/google/callback` — the daemon's
     tailnet URL (run `tailscale serve status` to find it). This is what makes
     the in-app "tap to Connect" flow work with no copy/paste.
   - `http://localhost:8765/` — only needed for the CLI copy/paste fallback
     (§2b). Skip it if you'll only ever connect from the web UI.

   Download the JSON.
5. Save it as:

   ```
   ~/.local/share/harness/google_client_secret.json
   ```

   (or point `GOOGLE_OAUTH_CLIENT_FILE` at wherever you put it.)

> Why a **Web application** client (not Desktop)? Only a web client may redirect
> to an arbitrary HTTPS URL — the daemon's tailnet address — so Google can hand
> the token straight back to the app. A desktop client can only redirect to
> `localhost`, which an iPad can't reach (the old copy/paste pain).

## 2a. Authenticate from the app (recommended)

1. Set the daemon's external URL so it knows where the redirect comes back to:

   ```
   HARNESS_PUBLIC_URL=https://<host>.<tailnet>.ts.net
   ```

   (in `.env`), then restart agentd (docs/macos-host.md → Restart agentd).
2. Open the web UI (on the iPad over `tailscale serve`, or
   `http://127.0.0.1:8787`) → **Connections** tab → **Connect** next to Google.
3. Approve on Google's screen. It redirects back to the daemon, which caches the
   token at `~/.local/share/harness/google_token.json` (mode 0600) and shows
   **Connected ✓**. No terminal, no SSH, no copy/paste.

The running MCP server picks up the new token automatically (the service client
rebuilds when the token file changes), so no daemon restart is needed after a
reconnect.

## 2b. Authenticate from the CLI (fallback)

No daemon / Tailscale handy? The copy/paste flow still works — it needs the
`http://localhost:8765/` redirect URI registered in step 1:

```bash
uv run google-workspace-mcp --auth --manual
```

It prints a Google URL and waits. Open the URL in any browser, approve, and the
browser will then fail to load a `http://localhost:8765/?code=...` page — that's
expected. Copy that whole failed-redirect URL from the address bar and paste it
back at the prompt; the auth code rides in its query string.

(`--auth` without `--manual` runs a local catcher; `GOOGLE_OAUTH_PORT=<n>` pins
its port. Less useful now that the in-app flow exists.)

## Re-auth: rare, and a tap when it happens

Scopes are now **broad per service** (full Gmail-modify, Calendar, Drive, Tasks,
Docs, Sheets), granted in one login. Because of that, **adding a new Google tool
no longer needs a new scope or another re-auth** — it's code only. A token
issued before the broad scopes existed is missing some, so `load_credentials()`
fails fast with re-auth instructions (rather than a cryptic `invalid_scope` on
the first call); just reconnect once from the Connections tab (§2a).

## 3. Verify

```bash
uv run harness
> what's on my calendar tomorrow, and any unread important mail?
```

The agent should answer using `calendar_list_events` and `gmail_search`
without any approval prompts (they're READ_ONLY). Sending mail still prompts.

## Gotchas

- **Testing-mode tokens expire after 7 days.** While the consent screen is in
  "Testing" status, Google expires refresh tokens weekly — you'd re-auth every
  week. Fix: publish the app to **Production** (consent screen → Publish).
  You'll see an "unverified app" warning during login — click *Advanced →
  continue*. For a single-user personal app this is fine.
- **Scopes are broad on purpose.** The harness's security boundary is the policy
  engine (`servers.toml` ActionClass + `core/policy.py`), not the OAuth scope:
  `gmail_send` is OUTWARD and always prompts no matter how broad the token is.
  Broad scopes only widen blast radius if the on-disk token file leaks, and in
  return new tools never trigger a re-auth. Gmail tops out at `gmail.modify`
  (not `https://mail.google.com/`, which would add permanent delete — a
  DESTRUCTIVE capability we don't expose). See `.claude/decisions.md`.
- Both JSON files live outside the repo, and `.gitignore` catches
  `*token*.json` regardless — never commit either.
