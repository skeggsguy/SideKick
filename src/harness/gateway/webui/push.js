/* Push notifications + PWA plumbing: service-worker registration, the bell
   button (subscribe / test-push), subscription-state resync on boot, and the
   SW → app message that deep-links a notification tap into its session. */
"use strict";

import { $, api } from "./dom.js";
import { openSession, sessionIdFromHash } from "./sessions.js";

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");

const pushBtn = $("#push-btn");

function markPushOn() {
  pushBtn.textContent = "🔔✓";
  pushBtn.title = "Notifications on — tap to send a test push";
}

async function subscribePush(reg) {
  const { key } = await api("/api/push/key");
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: b64urlToBytes(key),
  });
  await api("/api/push/subscribe", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
  return sub;
}

// Every launch: iOS can drop a subscription silently while Apple still 2xx's the
// old endpoint. Debugging a missing push? Check Notification Centre first.
async function syncPushState() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (sub) {
      await api("/api/push/subscribe", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub.toJSON()),
      });
    } else if (Notification.permission === "granted") {
      sub = await subscribePush(reg);
    }
    if (sub) markPushOn();
  } catch (err) { console.warn("push state sync failed:", err); }
}

pushBtn.addEventListener("click", async () => {
  try {
    const reg = await navigator.serviceWorker.ready;
    if (Notification.permission === "granted" && (await reg.pushManager.getSubscription())) {
      const { sent } = await api("/api/push/test", { method: "POST" });
      return alert(`Test push sent to ${sent} device(s). If it doesn't appear, check iOS Settings → Notifications and Focus mode.`);
    }
    const perm = await Notification.requestPermission();
    if (perm !== "granted") return alert("Notifications not granted.");
    await subscribePush(reg);
    markPushOn();
    await api("/api/push/test", { method: "POST" });
  } catch (err) { alert(`Push setup failed: ${err.message}`); }
});

syncPushState();

function b64urlToBytes(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=");
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", (e) => {
    const id = sessionIdFromHash(e.data && e.data.url);
    if (id) openSession(id);
  });
}
