#!/bin/bash
# LaunchAgent wrapper for agentd (com.harness.agentd).
# Waits until the Mac has been up 6 min — OrbStack and the rest of login claim
# their memory in the first minutes after a reboot — then execs agentd so
# launchd's SIGTERM reaches it directly.
# If something already listens on 8787 (agentd started by hand in tmux), exit 0:
# KeepAlive {SuccessfulExit: false} then leaves it stopped instead of looping.

MIN_UPTIME_S=360
REPO=/Users/YOU/dev/sidekick

boot=$(/usr/sbin/sysctl -n kern.boottime | sed -E 's/^\{ sec = ([0-9]+).*/\1/')
if [[ "$boot" =~ ^[0-9]+$ ]]; then
    wait_s=$(( boot + MIN_UPTIME_S - $(date +%s) ))
else
    wait_s=$MIN_UPTIME_S   # can't read boot time: wait the full delay
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') agentd-wrapper: pid $$, waiting ${wait_s}s for ${MIN_UPTIME_S}s uptime" >&2
[ "$wait_s" -gt 0 ] && sleep "$wait_s"

# The llama server (HARNESS_LOCAL_URL, :8080) tries to open itself at login
# (~/dev/ai's dev.ai.library agent). If it isn't up by now, ask for it through
# library.sh, which keeps its own guards (HALT, one model process, the doorman,
# its own post-boot wait). Don't block on it: agentd only needs the model per turn.
# ONE 5s check, then agentd starts regardless — this never waits for /health and
# never restarts or stops llama (don't document it as "waits for health").
# Check /health, not the bare port: the port opens before the model has loaded
# (2026-10-05: a server stuck loading behind a privacy prompt held 8080 open).
# A server still loading makes library.sh refuse (one model at a time), harmlessly.
if ! /usr/bin/curl -s -m 5 http://127.0.0.1:8080/health | grep -q '"ok"'; then
    echo "agentd-wrapper: llama server not healthy on 8080 — runner/library.sh start" >&2
    /bin/bash /Users/YOU/dev/ai/runner/library.sh start >&2 \
        || echo "agentd-wrapper: library.sh start refused/failed (exit $?) — see ~/Library/Logs/flashnext/library.log" >&2
fi

if (exec 3<>"/dev/tcp/127.0.0.1/8787") 2>/dev/null; then
    echo "agentd-wrapper: 8787 already in use (agentd running by hand?) — not starting" >&2
    exit 0
fi

cd "$REPO" || exit 1
exec "$REPO/.venv/bin/agentd"
