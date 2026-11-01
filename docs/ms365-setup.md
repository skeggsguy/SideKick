# Microsoft 365 MCP server — one-time setup

The `ms365` server (mcp_servers/ms365) gives the harness read-only access to
Outlook mail and calendar via Microsoft Graph. It is a read-only
connector for a personal Microsoft account.
Read-only scopes only (`Mail.Read`, `Calendars.Read`) — same read-first
stance as the Google server in P2.

## 1. Register an Azure app (once, in the browser)

1. Go to <https://portal.azure.com> → **Microsoft Entra ID** →
   **App registrations** → **New registration**.
   (A free personal Microsoft account works; no Azure subscription needed.)
2. Name: anything, e.g. `harness-ms365`.
3. **Supported account types**: choose **Personal Microsoft accounts only**
   (the server's default authority is `/consumers`; if you pick a broader
   option, set `MS365_AUTHORITY` accordingly).
4. Redirect URI: leave empty (device-code flow needs none).
5. After creation, open **Authentication** → set
   **Allow public client flows** to **Yes** (required for device code) → Save.
6. Copy the **Application (client) ID** from the Overview page.

No client secret is needed — this is a public client.

## 2. Configure and authenticate (once, in the terminal)

```bash
# .env
MS365_CLIENT_ID=<the Application (client) ID>

uv run ms365-mcp --auth
```

Follow the printed instructions (visit microsoft.com/devicelogin, enter the
code, sign in with the personal Microsoft account). The token cache lands at
`~/.local/share/harness/ms365_token_cache.json` (override with
`MS365_TOKEN_FILE`); MSAL refreshes it silently from then on.

## 3. Enable the server

In `src/harness/mcp/servers.toml`, set `enabled = true` under
`[servers.ms365]`. Restart the harness/daemon. The tools appear as
`outlook_mail_search`, `outlook_mail_read`, and
`outlook_calendar_list_events`, all classified READ_ONLY.

## Verify

In the REPL: "any unread mail on my Outlook account?" should call
`outlook_mail_search`. Before authenticating, every tool returns the
auth-help error instead of failing silently.

## Notes

- Scopes are read-only by design; adding write scopes later (send, event
  create) forces one re-auth and the new tools must be classified in
  servers.toml (unlisted MCP tools default to OUTWARD, i.e. gated).
- `$search` queries use Graph KQL-ish syntax: plain words, or
  `from:alice subject:invoice`.
