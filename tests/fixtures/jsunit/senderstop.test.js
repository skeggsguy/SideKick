/* senderstop.test.js — BK-036 (plan frontend-smoothness step 2c): the
   SENDER's own Stop must stay while the server's /live says the run lives.

   The reproduced failure (scratch/smoothness_probe/senderstop.py, the `eof`
   scenarios): the chat stream ends CLEANLY mid-run — no terminal event, the
   reader just returns done — and T14 took that as "agentd restarted", handing
   the composer back to Send while the run carried on detached through a long
   silent tool call. Transitions under test (runstate.js unless noted):
     T14 streamEndSilent ...... a started stream's silent end is T15's
                                recoverable state (resync armed, Stop kept);
                                only a never-started one hands back
     T27 (resync.js) .......... the backstop: a "live" gap-fill verdict for
                                the screen arms Stop if no source holds it
     T10/T11 .................. an events-stream terminal ends ITS session's
                                turn only (the unscoped liveTurn clear, (e))
   Fixtures arm ui directly (the project's test seam); deps are replay.js's
   real arithmetic plus a scripted /live. DOM-free, rAF-free. */

import { ui } from "./state.js";
import {
  streamEndSilent, stopTarget, stopRequest, runStopped, runError, observeJoin,
  applyEffects,
} from "./runstate.js";
import { visibleResume, watchdogTick, resyncVerdict } from "./resync.js";
import {
  alreadyPainted, markPainted, joinReason, raiseWatermark, storedWatermark,
  isProvisionalSlot,
} from "./replay.js";
import { test, check, eq } from "./runner.js";

const S = "send-sess";
const T = "other-sess";
const R1 = "run-send-1";
const delta = (seq, runId = R1) => ({ type: "delta", text: "w", seq, run_id: runId });

const deps = (over) => ({
  isReplaying: () => false, alreadyPainted, markPainted, joinReason,
  raiseWatermark, storedWatermark, isProvisionalSlot, ...(over || {}),
});
const fetcher = (res) => {
  const calls = [];
  return { calls, fetchLive: async (id, since) => { calls.push([id, since]); return res; } };
};
const composerOf = (effects) => (effects || []).filter((e) => e[0] === "composer").map((e) => e[1]);

// The sender mid-run: its own stream painted seq 1..n into the slot (the chat
// sink marks as it paints), the screen is the run's session.
function sending(n = 5) {
  ui.sessionId = S;
  ui.liveTurn = { session: S };
  for (let i = 1; i <= n; i++) markPainted(S, delta(i));
}

test("T14 (BK-036): a STARTED stream's silent end keeps Stop — it is T15's recoverable state", () => {
  sending();
  const fx = streamEndSilent(S, true);
  eq("silent end mid-run: resync armed for the STREAM's session", ui.resync, { id: S, seen: false });
  check("silent end mid-run: liveTurn KEPT (Stop stays armed)", ui.liveTurn?.session === S);
  eq("silent end mid-run: no composer hand-back", composerOf(fx), []);
  eq("silent end mid-run: T15's effects — the honest row, the reroute POST, the watchdog", fx,
    [["statusWarn", "Reconnecting · still working"], ["netDetach", S], ["armWatchdog"]]);
  eq("silent end mid-run: Stop still targets the run", stopRequest(), [["netStop", S]]);
});

test("T14 (BK-036): a stream that ended BEFORE the run started still hands back (nothing is live)", () => {
  ui.sessionId = S;
  ui.liveTurn = { session: S };
  const fx = streamEndSilent(S, false);
  check("never started: liveTurn cleared", ui.liveTurn === null);
  check("never started: no resync armed", ui.resync === null);
  eq("never started: row + composer", fx, [["statusStop"], ["composer", "send"]]);
});

test("T14 (BK-036): the field guards still hold — after a terminal, or with a resync already armed", () => {
  ui.sessionId = S;
  check("idle (a terminal already cleared liveTurn): nothing", streamEndSilent(S, true) === null);
  sending();
  ui.resync = { id: S, seen: true };
  check("resync armed: recovery owns the state — nothing", streamEndSilent(S, true) === null);
  eq("resync armed: ... nothing written", [ui.liveTurn?.session, ui.resync], [S, { id: S, seen: true }]);
});

test("T14 → T27 (BK-036): after the silent end, /live decides — active keeps Stop, inactive heals to Send", async () => {
  sending();
  applyEffects(streamEndSilent(S, true), new Proxy({}, { get: () => () => {} }));
  const alive = fetcher({ active: true, seq: 5, oldest_seq: 1, gap: false, events: [] });
  const r = await watchdogTick(deps({ fetchLive: alive.fetchLive }));
  eq("still live: the watchdog's verdict is live", r.verdict, "live");
  eq("still live: no composer flip", composerOf(r.effects), []);
  check("still live: liveTurn + resync kept", ui.liveTurn?.session === S && ui.resync?.id === S);
  const gone = fetcher({ active: false });
  const r2 = await watchdogTick(deps({ fetchLive: gone.fetchLive }));
  eq("agentd really restarted (inactive): the heal", r2.verdict, "terminal");
  eq("inactive: Send + the persisted re-render", r2.effects,
    [["statusStop"], ["composer", "send"], ["rerenderHeal", S]]);
  check("inactive: every field cleared", !ui.liveTurn && !ui.resync && !ui.observed);
});

test("T27 backstop (BK-036): a live gap-fill for the screen with NO Stop source arms one", async () => {
  ui.sessionId = S;
  for (let i = 1; i <= 5; i++) markPainted(S, delta(i)); // a primed screen, but nothing armed
  const f = fetcher({ active: true, seq: 7, oldest_seq: 1, gap: false, events: [delta(6), delta(7)] });
  const res = await visibleResume(deps({ fetchLive: f.fetchLive }));
  eq("backstop: the verdict is a gap fill", res.verdict, "live");
  eq("backstop: composer → stop + the observe watch, after the gap fill", res.effects.map((e) => e[0]),
    ["gapFill", "composer", "observeWatch"]);
  eq("backstop: ... the composer reads stop", composerOf(res.effects), ["stop"]);
  eq("backstop: the run is observed under the slot's run id", ui.observed, { id: S, runId: R1 });
  eq("backstop: Stop targets the screen's run", stopTarget(), S);
});

test("T27 backstop (BK-036): never when a source already holds Stop, or the screen is elsewhere", async () => {
  sending();
  const live = { active: true, seq: 6, oldest_seq: 1, gap: false, events: [delta(6)] };
  let res = await resyncVerdict(S, false, deps({ fetchLive: fetcher(live).fetchLive }));
  eq("own stream attached: gap fill only, no composer", res.effects.map((e) => e[0]), ["gapFill"]);
  check("own stream attached: nothing observed", ui.observed === null);
  ui.resync = { id: S, seen: false };
  res = await watchdogTick(deps({ fetchLive: fetcher(live).fetchLive }));
  eq("resync armed: gap fill only", res.effects.map((e) => e[0]), ["gapFill"]);
  ui.resync = null; ui.liveTurn = null;
  observeJoin(S, { since: 0, seq: 6, oldest_seq: 1, gap: false, events: [delta(6)] });
  res = await resyncVerdict(S, false, deps({ fetchLive: fetcher(live).fetchLive }));
  eq("already observed: gap fill only (no per-tick composer re-measure)", res.effects.map((e) => e[0]), ["gapFill"]);
  ui.observed = null;
  ui.resync = { id: S, seen: false };
  ui.sessionId = T; // the user switched chats: the pending run is not on screen
  res = await watchdogTick(deps({ fetchLive: fetcher(live).fetchLive }));
  eq("screen elsewhere: no paint, no arm", res.effects, []);
  check("screen elsewhere: nothing observed", ui.observed === null);
});

test("T10/T11 (BK-036 (e)): an events terminal for the screen ends ITS session's turn only", () => {
  // This page's own turn on T is latched (the user switched to S); S's run —
  // another device's — is rerouted here and stops.
  ui.sessionId = S;
  ui.liveTurn = { session: T, detached: true };
  const fx = runStopped({ type: "stopped", session_id: S, seq: 9, run_id: "run-x" });
  check("stopped on S: the turn on T is KEPT", ui.liveTurn?.session === T);
  eq("stopped on S: no Send while T's turn lives (D4)", composerOf(fx), []);
  eq("stopped on S: ... Stop still targets T", stopTarget(), T);
  const fx2 = runError({ type: "error", session_id: S, error: "x", seq: 9, run_id: "run-x" });
  check("error on S: the turn on T is KEPT", ui.liveTurn?.session === T && composerOf(fx2).length === 0);
  ui.liveTurn = { session: S };
  const fx3 = runStopped({ type: "stopped", session_id: S, seq: 9, run_id: R1 });
  check("stopped naming the turn's OWN session: cleared", ui.liveTurn === null);
  eq("... and Send", composerOf(fx3), ["send"]);
});
