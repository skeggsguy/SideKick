#!/bin/bash
# Double-click in Finder to start Sidekick now: the llama server (via ~/dev/ai's
# library.sh, guards intact) and agentd (via its LaunchAgent, com.harness.agentd).
# Safe to run when things are already up — it only starts what's missing.

LLM_LOG=~/Library/Logs/flashnext/library.log
AGENTD_LOG=~/Library/Logs/harness/agentd.log
PLIST=~/Library/LaunchAgents/com.harness.agentd.plist

up() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

echo "== Sidekick start =="

if /usr/bin/curl -s -m 5 http://127.0.0.1:8080/health | grep -q '"ok"'; then
    echo "llama server: healthy on 8080"
elif up 8080; then
    echo "llama server: on 8080 but not healthy yet (still loading? a permission prompt on screen?)"
else
    echo "llama server: starting (runner/library.sh start)…"
    /bin/bash ~/dev/ai/runner/library.sh start \
        || echo "llama server: library.sh refused/failed — see $LLM_LOG"
fi

if up 8787; then
    echo "agentd: already up on 8787"
else
    echo "agentd: starting (com.harness.agentd)…"
    launchctl print "gui/$(id -u)/com.harness.agentd" >/dev/null 2>&1 \
        || launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl kickstart "gui/$(id -u)/com.harness.agentd"
    echo "  (if the Mac booted under 6 min ago, it waits until then)"
    for _ in $(seq 60); do up 8787 && break; sleep 1; done
    if up 8787; then echo "agentd: up on 8787"; else echo "agentd: not up yet — tail -f $AGENTD_LOG"; fi
fi

echo
echo "Model server may take a few minutes more (doorman; 4-min boot gate): tail -f $LLM_LOG"
read -n 1 -s -r -p "Press any key to close…"
echo
