/* Cold-start restore scenario (plan pwa-accurate-resume, Step 2), invoked by
   probe.js at the END of the run.

   A cold start is app.js's boot code running with no #session= hash. The
   probe page swapped the app.js ENTRY tag out (mock_fetch + probe instead),
   so this module imports the REAL app.js and lets its module evaluation run
   the production boot exactly once. A re-import with a distinct query string
   is another cold boot: ES module identity includes the query, while every
   dependency (state/transcript/sessions/…) stays the SAME instance — so each
   boot manipulates the one app the earlier scenarios ran in.

   The store under test is REAL localStorage — asserted live, never by a
   source regex. The list is NOT the deletion verdict (the gateway caps it at
   the 50 most recent rows — boots build full 50-row pages to prove a miss is
   survivable). The verdict is the single-session route's three answers
   (mock_fetch.js `singleRow`/`singleGone`): a ROW restores the chat even off
   the page (its option is prepended, real title); 200 + NULL means deleted —
   id forgotten, never fetched; a 404 (an agentd predating the route) or any
   failure means UNKNOWN — greeting that KEEPS the id, because a blip or a
   version skew must not destroy the pointer. A FAILED list fetch is the
   offline relaunch: greeting, id kept.

   The SW/push machinery is environment-dependent in headless-shell and out
   of scope here: push.js guards every entry with `"serviceWorker" in
   navigator`, so a never-resolving stub keeps a registration rejection (and
   a mid-scenario clients.claim()) out of probe.js's page-error list. */
"use strict";

import { ui } from "./state.js";
import { resetTranscript } from "./transcript.js";
import { hideGreeting, showGreeting } from "./greeting.js";
import { loadSessions, openSession } from "./sessions.js";

const CHAT = "cafe0001"; // hex-ish, like real session ids
const GONE = "dead0000";
const TOAST = "b33fc0de";
const HIDDEN = "b1d00001"; // exists, but never on the 50-row page
const page50 = () =>
  Array.from({ length: 50 }, (_, i) => ({ id: `pad${i}`, title: `Pad ${i}`, channel: "web" }));

export async function runColdStart({ check, msgFetches, sleep }) {
  const log = document.querySelector("#chat-log");
  const greet = document.querySelector("#chat-greeting");
  const sel = document.querySelector("#session-select");
  const stored = () => localStorage.getItem("lastChat");
  const msgFor = (id) =>
    window.__mock.calls.filter((p) => p === `/api/sessions/${id}/messages`).length;

  // Control — localStorage is live in this harness (the store under test);
  // every assertion below depends on it, so say so loudly rather than let
  // four checks fail on an environment gap.
  let lsOk = true;
  try {
    localStorage.setItem("lastChat", "ls-probe");
    lsOk = localStorage.getItem("lastChat") === "ls-probe";
    localStorage.removeItem("lastChat");
  } catch (_) { lsOk = false; }
  check("cold-start-restore control: localStorage is live in the harness", lsOk);
  if (!lsOk) return;

  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { register: () => new Promise(() => {}), addEventListener: () => {} },
  });

  // --- boot #1: a hash-less boot reopens the stored chat --------------------
  resetTranscript();
  showGreeting();
  ui.sessionId = null;
  check("cold-start-restore control: the greeting detector sees the home view",
    greet.hidden === false);
  localStorage.setItem("lastChat", CHAT);
  window.__mock.settled = true;
  window.__mock.sessions = [{ id: CHAT, title: "Restored chat", channel: "web" }];
  await import("./app.js"); // module evaluation runs the production boot
  await sleep(400); // sessions (0ms) + /messages (150ms) + replay, with slack
  check("cold-start-restore: hash-less boot reopens the stored chat",
    ui.sessionId === CHAT && sel.value === CHAT,
    `ui.sessionId=${String(ui.sessionId)} select=${sel.value || "(blank)"}`);
  check("cold-start-restore: the reopened chat fetched ITS transcript and painted it",
    msgFor(CHAT) === 1 && log.textContent.includes("PROBE_HEAD"),
    `/messages fetches for ${CHAT}: ${msgFor(CHAT)}; painted=${log.textContent.includes("PROBE_HEAD")}`);
  check("cold-start-restore: the greeting is hidden and no error state appeared",
    greet.hidden === true && !log.querySelector(".msg.error"),
    `greet.hidden=${greet.hidden} errors=${log.querySelectorAll(".msg.error").length}`);

  // --- the write rule: opening stores, "+ New chat" clears ------------------
  const pick = async (value, waitMs) => {
    sel.value = value;
    sel.dispatchEvent(new Event("change"));
    await sleep(waitMs); // renderSession(null) is sync; the reopen refetches
  };
  await pick("", 50);
  check("cold-start-restore: picking \"+ New chat\" clears the stored id",
    stored() === null, `stored=${String(stored())}`);
  await pick(CHAT, 300);
  check("cold-start-restore: opening a chat stores its id again",
    stored() === CHAT, `stored=${String(stored())}`);

  // openSession() is the shared write site for deep links, push taps and the
  // boot restore (above) — clearing the slot first proves the write itself,
  // not just that a previous value happened to survive.
  localStorage.removeItem("lastChat");
  await openSession(CHAT);
  check("cold-start-restore: openSession stores the id it opens",
    stored() === CHAT, `stored=${String(stored())}`);

  // The toast/auto-open write site (events.js openLiveSession): what is on
  // screen IS the last chat — without it a relaunch reopens the PREVIOUS one.
  ui.expecting = true; // the auto-open branch; consumed by the handler
  window.__sk.handleRunEvent({ type: "run_start", session_id: TOAST, title: "Toast run" });
  await sleep(50); // openLiveSession's loadSessions().then() settles
  check("cold-start-restore: a run_start auto-open stores the opened chat",
    stored() === TOAST && ui.sessionId === TOAST,
    `stored=${String(stored())} ui.sessionId=${String(ui.sessionId)}`);

  // The other branch of the same handler: an engaged user gets a TOAST, and
  // tapping it opens the run — that tap must move the pointer too.
  const TOAST2 = "b33fc0df";
  ui.sessionId = CHAT; // engaged: not the idle auto-open branch
  localStorage.setItem("lastChat", CHAT);
  window.__sk.handleRunEvent({ type: "run_start", session_id: TOAST2, title: "Toasted run" });
  const toast = document.getElementById("run-toast");
  toast.click();
  await sleep(50);
  check("cold-start-restore: tapping a background-run toast stores the opened chat",
    stored() === TOAST2 && ui.sessionId === TOAST2,
    `stored=${String(stored())} ui.sessionId=${String(ui.sessionId)}`);

  // --- boot #2: the server SAYS the stored session is gone → forgotten ------
  resetTranscript();
  showGreeting();
  ui.sessionId = null;
  localStorage.setItem("lastChat", GONE);
  window.__mock.sessions = page50(); // miss in a FULL page: no verdict either way…
  window.__mock.singleGone = true; // …so the single-session route gives it: 200 null
  const fetchesBeforeStale = msgFetches();
  // Each ?coldboot=N re-runs app.js's boot over SHARED modules: let the previous
  // boot's mock fetches settle (the sleeps) before the next import.
  await import("./app.js?coldboot=2"); // fresh boot evaluation, shared modules
  await sleep(200);
  check("cold-start-restore: a stale stored id falls back to the greeting, never an error",
    ui.sessionId === null && greet.hidden === false && log.children.length === 0 &&
      !log.querySelector(".msg.error"),
    `ui.sessionId=${String(ui.sessionId)} greet.hidden=${greet.hidden} children=${log.children.length}`);
  check("cold-start-restore: the dead id is forgotten and never fetched",
    stored() === null && msgFetches() === fetchesBeforeStale,
    `stored=${String(stored())} messages fetches +${msgFetches() - fetchesBeforeStale}`);
  window.__mock.singleGone = false;

  // --- boot #2b: the id is off the FULL 50-row page but the route has its ---
  // row → restore it anyway. The live store holds ~950 sessions; the list's
  // 50-row cut means a miss proves nothing (cleric finding: this exact case
  // used to DESTROY the pointer). The option must be prepended, titled.
  resetTranscript();
  showGreeting();
  ui.sessionId = null;
  localStorage.setItem("lastChat", HIDDEN);
  window.__mock.sessions = page50(); // full page, HIDDEN not on it
  window.__mock.singleRow = { id: HIDDEN, title: "Old chat", channel: "web" };
  const fetchesBeforeHidden = msgFetches();
  await import("./app.js?coldboot=2b");
  await sleep(400); // list + single (0ms mocks) + /messages (150ms) + replay
  check("cold-start-restore: a chat off the 50-row page still reopens when the route has it",
    ui.sessionId === HIDDEN && stored() === HIDDEN && msgFor(HIDDEN) === 1 &&
      msgFetches() === fetchesBeforeHidden + 1 && log.textContent.includes("PROBE_HEAD") &&
      greet.hidden === true && !log.querySelector(".msg.error"),
    `ui.sessionId=${String(ui.sessionId)} stored=${String(stored())} ` +
    `fetches +${msgFetches() - fetchesBeforeHidden}`);
  check("cold-start-restore: the restored off-page chat is in the picker, selected and titled",
    sel.value === HIDDEN &&
      [...sel.options].some((o) => o.value === HIDDEN && o.textContent === "Old chat"),
    `select=${sel.value || "(blank)"} options=${sel.options.length}`);
  // A later picker rebuild (visibilitychange→visible, title/done, rename) must
  // not blank the select under the restored chat: the capped page still has no
  // row for it, so loadSessions keeps the option it is already showing.
  await loadSessions();
  check("cold-start-restore: a later picker rebuild keeps the off-page chat selected",
    sel.value === HIDDEN && sel.options.length === 52 &&
      [...sel.options].some((o) => o.value === HIDDEN && o.textContent === "Old chat"),
    `select=${sel.value || "(blank)"} options=${sel.options.length}`);
  // Rename rides the same carried option text: the PATCH lands server-side but
  // the capped page cannot show the new title, so the handler refreshes the
  // node before rebuilding (else the picker would keep the OLD title).
  const realPrompt = window.prompt;
  window.prompt = () => "Retitled chat";
  document.querySelector("#rename-btn").click();
  await sleep(100);
  window.prompt = realPrompt;
  check("cold-start-restore: renaming an off-page chat updates its picker option",
    sel.value === HIDDEN &&
      [...sel.options].some((o) => o.value === HIDDEN && o.textContent === "Retitled chat"),
    `select=${sel.value || "(blank)"} labels=[${[...sel.options].filter((o) => o.value === HIDDEN).map((o) => o.textContent).join(",")}]`);
  window.__mock.singleRow = null;

  // --- boot #2c: same miss, but the agentd PREDATES the route → its 404 says ---
  // nothing ("no route", not "no session") → greeting and the id SURVIVES.
  resetTranscript();
  showGreeting();
  ui.sessionId = null;
  localStorage.setItem("lastChat", HIDDEN); // singleRow null + singleGone false = absent mode
  const fetchesBeforeUnknown = msgFetches();
  await import("./app.js?coldboot=2c");
  await sleep(200);
  check("cold-start-restore: an unanswerable existence check greets but keeps the id",
    greet.hidden === false && stored() === HIDDEN && ui.sessionId === null &&
      msgFetches() === fetchesBeforeUnknown && !log.querySelector(".msg.error"),
    `greet.hidden=${greet.hidden} stored=${String(stored())} fetches +${msgFetches() - fetchesBeforeUnknown}`);
  window.__mock.sessions = [{ id: CHAT, title: "Restored chat", channel: "web" }];

  // --- boot #3: the sessions fetch FAILS → greeting, and the id SURVIVES ----
  localStorage.setItem("lastChat", CHAT);
  resetTranscript();
  showGreeting();
  ui.sessionId = null;
  window.__mock.sessionsFail = true;
  const fetchesBeforeDown = msgFetches();
  await import("./app.js?coldboot=3");
  await sleep(200);
  check("cold-start-restore: a failed sessions fetch shows the greeting but keeps the id",
    greet.hidden === false && stored() === CHAT &&
      msgFetches() === fetchesBeforeDown && !log.querySelector(".msg.error"),
    `greet.hidden=${greet.hidden} stored=${String(stored())} fetches +${msgFetches() - fetchesBeforeDown}`);
  window.__mock.sessionsFail = false;
  // boot #3's own failed-retry loadSessions() (the greeting path) is a 150ms
  // mock response still in flight; let it land, or it would rebuild the
  // picker after boot #4's render.
  await sleep(250);

  // --- boot #4: no stored id → the plain home view (the pre-existing boot) --
  localStorage.removeItem("lastChat");
  window.__mock.sessions = [{ id: CHAT, title: "Restored chat", channel: "web" }];
  resetTranscript();
  hideGreeting(); // prove the boot shows it rather than finding it already up
  ui.sessionId = null;
  await import("./app.js?coldboot=4");
  await sleep(150);
  check("cold-start-restore: no stored id still boots to the greeting + picker",
    ui.sessionId === null && greet.hidden === false && sel.options.length === 2 &&
      !log.querySelector(".msg.error"),
    `ui.sessionId=${String(ui.sessionId)} greet.hidden=${greet.hidden} options=${sel.options.length}`);
}
