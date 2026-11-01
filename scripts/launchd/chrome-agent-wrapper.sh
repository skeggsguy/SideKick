#!/bin/bash
# LaunchAgent wrapper for the harness agent Chrome (Lane A).
# Waits for the egress proxy (com.harness.egress-proxy) to listen on 18080 —
# launchd has no unit ordering, and Chrome must never run without the SSRF
# filter. If the proxy never comes up we still exec Chrome: locked to a dead
# proxy it simply has no egress, i.e. fail closed.
# See docs/browser-fallback-setup.md — NONE of these flags may be weakened.

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

proxy_up=0
for _ in $(seq 1 60); do
    if (exec 3<>"/dev/tcp/127.0.0.1/18080") 2>/dev/null; then
        proxy_up=1
        break
    fi
    sleep 1
done
if [ "$proxy_up" -eq 1 ]; then
    echo "chrome-agent-wrapper: egress proxy up on 18080" >&2
else
    echo "chrome-agent-wrapper: WARNING proxy not up after 60s; Chrome will have no egress (fail closed)" >&2
fi

exec "$CHROME" \
    --user-data-dir="$HOME/.chrome-agent-profile" \
    --remote-debugging-port=19222 \
    --remote-debugging-address=127.0.0.1 \
    --proxy-server=127.0.0.1:18080 \
    '--proxy-bypass-list=<-loopback>' \
    --force-webrtc-ip-handling-policy=disable_non_proxied_udp \
    --disable-background-networking \
    --no-first-run \
    --no-default-browser-check \
    --disable-features=Translate,MediaRouter
