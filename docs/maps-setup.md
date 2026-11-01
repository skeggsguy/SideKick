# Google Maps Platform setup

The `maps_search` and `maps_route` tools let the agent find places ("is the nearest supermarket open") and route between locations ("how long to drive home in traffic"). Both require a Google Maps API key and work offline — no credentials or OAuth needed.

## Billing note

Google retired the $200/month free credit on 2025-03-01 in favor of per-SKU monthly allowances:
- **Places API (New) Text Search**: ~$35 per 1k calls beyond free (the opening hours field costs the Enterprise SKU)
- **Routes API**: ~$5–15 per 1k calls beyond free, depending on the tier

Single-user reality: even a daily scheduled maps check plus heavy chat use is well under 300 calls/month — **expect $0** within the free allowances. Always set a budget alert (e.g. $5) to catch surprises.

## Steps

1. **Enable the APIs** on your Google Cloud project:
   - [Go to the Cloud Console](https://console.cloud.google.com/)
   - Select your project (use the same one as Google Workspace OAuth if you already have one set up)
   - Search for and enable **Places API (New)**
   - Search for and enable **Routes API**

2. **Create an API key**:
   - In Cloud Console: APIs & Services → Credentials
   - Click **Create Credentials** → **API Key**
   - Copy the new key (you'll paste it into `.env` next)

3. **Restrict the key to the two APIs** (optional but recommended to prevent abuse):
   - In Credentials, click on your new key to edit it
   - Under "API restrictions", select **Restrict key**
   - Choose **Places API (New)** and **Routes API** only
   - Save

4. **Set a budget alert** (optional but recommended):
   - Billing → Budgets & alerts
   - Create a budget for the project with a threshold you're comfortable with (e.g. $5)
   - Add an alert at 100% so you're notified if spend reaches the limit

5. **Add the key to `.env`**:
   ```bash
   GOOGLE_MAPS_API_KEY=<paste your key here>
   ```

6. **Restart the daemon**:
   see docs/macos-host.md → Restart agentd.
   The tools will appear in the catalog within ~5 seconds (watch `tail -f ~/Library/Logs/harness/agentd.log` for "listening").

## Testing

In an interactive chat:
- Ask "is the nearest supermarket open" and confirm a nearby supermarket appears with hours and a `place_id:` line.
- Ask "how long to drive from home to the airport right now" and confirm the time with traffic, without traffic, and distance appear.

Both should auto-run with no approval prompt (READ_ONLY). If you then read a secret file in the same turn, a subsequent maps tool call should **prompt for approval instead of auto-running** (and be denied outright in unattended runs) — that's the secret-read taint working correctly.

Without GOOGLE_MAPS_API_KEY set, the tools simply won't appear.
