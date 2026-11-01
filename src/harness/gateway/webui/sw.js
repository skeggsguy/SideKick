/* Service worker: receives Web Push, shows notifications, opens the app. */
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// Network-only fetch handler; present so the app is installable. API and SSE
// requests (/api/*, notably the long-lived /api/events stream) are left
// untouched so they go straight to the network — routing an event stream
// through the worker can stall delivery on iOS.
self.addEventListener("fetch", (event) => {
  if (new URL(event.request.url).pathname.startsWith("/api/")) return;
  event.respondWith(fetch(event.request));
});

self.addEventListener("push", (event) => {
  let data = { title: "harness", body: "", url: "/" };
  try { data = { ...data, ...event.data.json() }; } catch (_) {}
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/icons/icon-192.png",
      data: { url: data.url },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const win of wins) {
        if ("focus" in win) {
          // Already-open page: tell it where to navigate — focusing alone
          // would leave a stale view with no sign of the new session.
          win.postMessage({ url });
          return win.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
