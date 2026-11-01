---
name: boot
description: Use when bringing the harness up by hand on the Mac mini (after a reboot, or after stopping everything) — OrbStack, then the local LLM server, then agentd
---

# Boot the harness (manual, Mac mini)

agentd now starts itself at login (LaunchAgent `com.harness.agentd`, 6 min after boot) —
after a reboot, step 3 is usually already done: check `lsof -nP -iTCP:8787 -sTCP:LISTEN`.
If the LLM server isn't on 8080 at that point, the same wrapper runs `~/dev/ai/runner/library.sh start`
(check `~/Library/Logs/flashnext/library.log`). Otherwise it still starts BY HAND (don't install `~/dev/ai/setup/dev.ai.flashnext-runner.plist`
as part of a boot). Background: docs/macos-host.md.

Order matters: **OrbStack → wait 5 min → LLM server → agentd.**

## 1. OrbStack (Docker for `sandbox_run`)

```bash
orbctl start
orbctl status        # → Running
```

**Then wait 5 minutes.** OrbStack spikes system memory while its VM loads; starting
the model on top of that risks swap. Don't start step 2 until the 5 min are up
(Claude: `sleep 300` with `run_in_background`, or just tell Tom when to continue).

## 2. Local LLM server (Qwen3.8-Flash-Next, llama.cpp fork)

In its own tmux session, from `~/dev/ai`:

```bash
tmux new -s llm      # or attach if it exists
cd ~/dev/ai && python3.12 runner/serve.py --context 200000
```

- **Always pass `--context 200000`** (the server rounds it to 200,192). Without it `serve.py` falls
  back to the benchmark rung's 96K (`-c 98304`), while opencode and sidekick assume 200K: a long
  session then hits the server's cap and goes silent (finish reason `length`; this happened on
  2026-10-02 after a reboot). Check: `curl -s 127.0.0.1:8080/slots` shows `n_ctx: 200192`. See
  `~/dev/ai/PICKUP.md` ("Launch note") for memory notes.
- Serves `http://127.0.0.1:8080` (= `HARNESS_LOCAL_URL`). Records by default —
  the server log goes to `~/dev/ai/results/dogfood/server-<date>.log`, not the console.
- It refuses to start if another llama-server is running (one model at a time) —
  never run a benchmark rung alongside it.
- Ready check: `curl -s 127.0.0.1:8080/health` answers ok. Optional sanity:
  `sysctl -n iogpu.wired_limit_mb` reads `59392` (set at boot by the root wired-limit daemon).

## 3. agentd (daemon + scheduler + web UI) — yes, still `agentd` on the Mac

cwd and PATH matter (it loads `.env` from cwd, spawns MCP servers by bare name,
shells out to `docker`):

```bash
tmux new -s agentd   # or attach and Ctrl-C the old one
cd ~/dev/sidekick && PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH \
  uv run agentd 2>&1 | tee -a ~/Library/Logs/harness/agentd.log
```

Check: the log shows `listening` with no traceback
(`tail -f ~/Library/Logs/harness/agentd.log`), then open the web UI on the iPad/phone
(Tailscale serve restores itself — `tailscale serve status` if the URL doesn't load).

## Notes

- **This is a cold boot.** To deploy a harness change, do NOT follow this file end to
  end — `launchctl kickstart -k gui/501/com.harness.agentd` is the whole restart. See
  docs/macos-host.md → "Restart agentd".
- **Killing the LLM server takes its other clients down with it.** `:8080` is shared
  with `opencode` (which may be serving the conversation asking for the restart) and
  with cron/watcher runs. Observed 2026-10-10: a "restart clean" pass SIGTERM'd
  serve.py mid-session and stopped the agent serving its own session. Only restart it
  for a server-side change, and check `lsof -nP -iTCP:8080 -sTCP:ESTABLISHED` first.
- After provider/agent-loop/policy changes, run the `release-check` skill BEFORE step 3.
- Stopping: Ctrl-C agentd, then Ctrl-C serve.py. OrbStack can stay up.
- Not part of boot: Ollama (a brew service — starts by itself; embeddings only), the Chrome browser lane (not set up yet).
