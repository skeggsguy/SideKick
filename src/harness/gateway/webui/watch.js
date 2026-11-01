/* watch.js — BK-034's observe watch: the TIMER that drives T32 (resync.js
   observeTick) while ui.observed is armed, i.e. while this page shows a chat
   run that another device started.

   Why a poll: chat events go to exactly ONE sink (gateway/routes/chat_run.py)
   — while the sender's stream is ATTACHED nothing about its run reaches
   /api/events, so without this an observer would paint the /messages join
   and then freeze, and never learn that the run stopped or finished. Once
   the sender detaches, rerouted events reach /api/events as well; the
   painted-interval dedupe (replay.js) makes the overlap harmless, and a
   rerouted `stopped`/`run_done` ends observing at once (T10/T18).

   Timer state lives HERE, never in a transition (the T21 watchdog's rule,
   §10.4): cadence, the hidden skip (the socket is closed while hidden and
   T20 asks on resume), and one tick in flight at a time. events.js supplies
   its RUN_DEPS/RUN_FX through armObserveWatch — this module imports no
   router, so the graph stays a DAG. */
"use strict";

import { applyEffects } from "./runstate.js";
import { observeTick } from "./resync.js";

// Cadence of the /live ask while observing: an attached sender's stream
// reaches this page in ~2 s lumps (the paced reveal smooths them), and a Stop
// from here reads back as Send within one tick of the run actually ending.
const OBSERVE_MS = 2000;

let timer = null;
let busy = false;

export function watchObserved(deps, fx) {
  if (timer) return; // one watch per page; re-arming is idempotent
  timer = setInterval(async () => {
    if (busy || document.visibilityState !== "visible") return;
    busy = true;
    try {
      const res = await observeTick(deps); // null: nothing observed on screen
      if (!res || res.verdict === "missing") {
        // "missing" = an agentd predating /live: probing it forever is the
        // worst skew behaviour (the T21 rule); Stop stays usable regardless.
        clearInterval(timer);
        timer = null;
        return;
      }
      applyEffects(res.effects, fx);
    } catch (e) {
      // fetchLive already maps network failure to the "offline" verdict, so
      // a throw here is a bug (an unbound fx token, a renderer error) — loud.
      console.error("observe watch: tick failed", e);
    } finally {
      busy = false;
    }
  }, OBSERVE_MS);
}
