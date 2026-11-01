/* fetch mock for the BK-017 scroll-follow probe (tests/fixtures/scroll_follow/).

   Loaded as a CLASSIC script by the generated probe page, so it is in place
   before the module graph (app.js and everything it boots) evaluates. It
   mocks ONLY /api/* — module scripts, CSS, marked and DOMPurify load for
   real from the mirrored site. No daemon, no token, no model: every answer
   is scripted here and streamed through a fake SSE body.

   WHAT MUST BE HONEST (learnings 2026-10-17: model the store's TIMING, not
   just its shape):
   - POST /api/chat persists the USER row when the run starts, and the
     ASSISTANT row only when the run's `done` frame is emitted — the store
     gains the answer when the model call RETURNS (core/agent.py). A
     /messages fetch mid-run therefore lacks the in-flight answer, exactly
     like the live gateway.
   - The SSE body is read with resp.body.getReader() exactly like the real
     stream, on a schedule (per-frame afterMs): the client's paced reveal,
     its 0.6 s lag and the spring all run against real wall-clock time
     (Playwright real time — rAF must actually tick for this probe).
   - Chat-run events are stamped {seq, run_id} at emit, like chat_run.py —
     app.js's replay.js watermark gates on them.

   Scenario SCRIPTS live here (window.__mock.scenarios): each builds an array
   of frames [{payload, afterMs, tag?, die?}] (BK-019 recover scenarios return
   {frames, serverDone?, chatFail?} instead — see the recover block). Every
   frame's payload is stamped {seq, run_id} at arm like chat_run.py's emit(),
   and ONLY the `session` frame carries session_id — exactly what the real
   SSE does (chat_sse_response yields {type:"session", session_id} first;
   queue frames carry just seq/run_id). app.js's T02 confirmation keys on
   that frame, so a mock without it cannot drive the T15/T16 recovery math.
   run_probe.py arms one, then a real keystroke-driven submit drives the real
   app.js send path.

   A frame carrying die:true KILLS the socket instead of delivering: the read
   REJECTS (HTTP stops handing over bytes — the CHAT transport cannot report a
   mid-stream death, it only notices the stream stopped). The run then lives
   SERVER-side: the scripted serverDone offset persists the answer (the call
   RETURNS — same timing rule as the done frame) and /live answers honestly
   until then. A script with no terminal frame ends the stream CLEANLY
   (reader done, runClosed true) — the transport cannot tell that from a dead
   socket either (app.js T14). chatFail arms the POST itself to fail:
   {mode:"status", status, detail} answers !ok; {mode:"fetch", message}
   rejects the fetch before any run exists.

   Non-fetch stubs (set here because they must exist before modules run):
   - EventSource: never opens, never fires. The real events.js would hold an
     /api/events socket against a static server (auto-reconnect noise); the
     probe delivers run events through window.__sk.handleRunEvent instead.
   - navigator.serviceWorker: register()/ready never resolve, so push.js's
     boot cannot register the mirrored sw.js (a live SW would intercept the
     probe site's own fetches) and cannot throw into the page-error list
     (pwa_resume precedent). */
window.__mock = {
  calls: [],       // every /api/* path fetched, in order (diagnostics)
  store: {},       // sid -> { messages: [...] } — persisted rows only
  chat: null,      // armed scenario script {sid, runId, frames}
  doneAt: null,    // performance.now() at the done-frame EMIT (the completion instant)
  runClosed: false, // true once the SSE body is exhausted (reader returned done)
  // The run as the SERVER knows it (BK-019 recover scenarios). A chat stream
  // that dies mid-run does not stop the model: the run keeps going, /live
  // answers from here, and the answer lands in the store when the scripted
  // serverDone offset passes — NOT at a done frame, because the reader that
  // would have received it is dead. null when no run is in flight.
  serverRun: null,  // {sid, runId, frames(stamped), doneAt(perf ms), answered}
  // performance.now() at the EMIT of each frame carrying a `tag` (the shrink
  // scenarios mark their shrink frame "shrink" so run_probe can window the
  // 8 ms sampler around the exact instant the client should clamp).
  // EMIT, not handling: the app processes the frame a task later — time a
  // gesture on a processing signature (label flip, text length), never on this.
  tagAt: {},

  // ---- scenario content ---------------------------------------------------
  // Deterministic (no Math.random): the same bytes stream on every run.
  _candidates: function () {
    var names = [
      "alpha-1", "bravo-2", "corvid-3", "delta-4", "echo-5", "foxtrot-6",
      "golf-7", "hotel-8", "india-9", "juliet-10", "kilo-11", "lima-12",
      "mike-13", "novel-14", "oscar-15", "papa-16", "quebec-17", "romeo-18",
      "sierra-19", "tango-20", "uniform-21", "victor-22", "whiskey-23", "xray-24",
      "yankee-25", "zulu-26", "acer-27", "boron-28", "cypher-29", "dynamo-30",
      "ember-31", "flint-32", "garnet-33", "helix-34", "iris-35", "junco-36",
      "kappa-37", "lumen-38", "mica-39", "nexus-40", "onyx-41", "prism-42",
      "quark-43", "rune-44", "solon-45", "tundra-46", "umbra-47", "vesper-48",
    ];
    var strengths = [
      "fast cold starts with a modest memory footprint and predictable p95 latency under load",
      "mature tooling, boring upgrade path, and the only option with first-class Windows support",
      "the strongest benchmark numbers on the ingestion path once the cache is warm",
      "smallest operational surface: one binary, no sidecars, nothing to babysit at 3am",
    ];
    var risks = [
      "requires a schema migration before rollout and a second one on rollback",
      "the licence turns commercial at our team size, procurement has flagged it twice",
      "young project, single maintainer, and the issue backlog is already slow",
      "memory usage grows with queue depth and the mitigation is not documented well",
    ];
    var decisions = ["shortlist", "drop", "hold for Q1", "pilot only"];
    var rows = [];
    for (var i = 0; i < names.length; i++) {
      rows.push("| " + names[i] +
        " | " + strengths[i % 4] +
        " | " + risks[(i + 1) % 4] +
        " | $" + (12 + ((i * 7) % 40)) + "k | " + decisions[i % 4] + " |");
    }
    return rows;
  },
  _intro: "Here is the full comparison the committee asked for, one row per candidate with the cost line and the vote:",
  _outro: "How to read this: the decision column reflects the committee vote from September, not any individual reviewer's recommendation.",
  _prose: function () {
    // Same total character mass as the table scenario (~8 paragraphs ≈ the
    // table's 24 rows of text), plain paragraphs: the control proves the
    // trigger's completion jump comes from the markdown RE-LAYOUT, not from
    // length, timing or the wheel itself. Must also overflow the log by well
    // over the wheel distance, or the wheel-up cannot unpin.
    var para = "The comparison itself is less important than how it was assembled, so " +
      "here is the method in full. We started from the twenty-four tools anyone had " +
      "named in the last two years, eliminated the ones with no release in eighteen " +
      "months, and asked each team to run two of the survivors against their own " +
      "workload for a fortnight. Every team wrote a short weekly note, and the " +
      "committee read all of them before voting; nothing here is a benchmark chart " +
      "someone found online. Where the notes disagreed with the benchmarks we kept " +
      "the notes, because operational pain is the thing this purchase is actually " +
      "buying. The cost column is three years of support plus the estimated " +
      "migration effort, which is why a couple of the cheap options are not cheap.";
    var out = [];
    for (var i = 0; i < 8; i++) out.push(para.replace("fortnight", "fortnight (" + (i + 1) + ")"));
    return out;
  },

  _reasoning: function () {
    // Long enough to exceed the strip's 600-char display tail. endThinking
    // removes that tail, but NOT at done: paintStream() closes the segment on
    // the FIRST text delta (transcript.js paintStream → endThinking), so the
    // strip has already collapsed long before done. (The earlier comment here
    // claimed the collapse happened AT DONE — measured false, see README.)
    var seed = "Weighing the candidates against last quarter's pilot notes: the " +
      "ingestion path carries our volume, the licence turns commercial at our " +
      "headcount, and every team that ran a fortnight trial reported the same " +
      "operational gripe twice before they reported anything good. ";
    var out = [];
    for (var i = 0; i < 8; i++) out.push(seed + "(" + (i + 1) + ")");
    return out;
  },
  _thinkFrames: function () {
    return window.__mock._reasoning().map(function (chunk, i) {
      return { payload: { type: "thinking_delta", text: chunk }, afterMs: i === 0 ? 120 : 90 };
    });
  },
  _thinkB: function () {
    // A SECOND thinking segment (multi-segment answer: thinking -> text ->
    // thinking, what a tool-loop answer streams). Long enough that the
    // pre-D2 strip's 600-char tail sat at its CSS max-height (3.8em ~= 47.5
    // px), so endThinking()'s tail removal was a full-height shrink. D2
    // (BK-030) dropped the tail: the close is flat now (README "BK-030 / D2").
    var seed = "Re-reading the pilot notes for the third pass: the ingestion numbers " +
      "move the ranking, the licence question is bigger than the committee framed " +
      "it, and two teams would carry the migration effort themselves if asked. ";
    var out = [];
    for (var i = 0; i < 6; i++) out.push(seed + "(" + (i + 1) + ")");
    return out;
  },
  // Shared spine of the three shrink scenarios: session -> thinking A
  // (+thinking_done) -> prose answer A -> thinking B. Ends on the LAST
  // thinking-B delta, tagged "thinkB": the probe parks the viewport between
  // that frame and the shrink frame, while the log is static (the tail is
  // fixed-height by CSS).
  _shrinkBase: function () {
    var m = window.__mock;
    var frames = [{ payload: { type: "session" }, afterMs: 60 }];
    frames = frames.concat(m._thinkFrames());
    frames.push({ payload: { type: "thinking_done" }, afterMs: 60 });
    var paras = m._prose().slice(0, 6);
    for (var i = 0; i < paras.length; i++) {
      frames.push({ payload: { type: "delta", text: paras[i] + "\n\n" }, afterMs: 130 });
    }
    var tb = m._thinkB();
    for (var j = 0; j < tb.length; j++) {
      frames.push({ payload: { type: "thinking_delta", text: tb[j] },
                    afterMs: 90, tag: j === tb.length - 1 ? "thinkB" : null });
    }
    return frames;
  },
  // The answer half AFTER the shrink: deliberately small and slow (30-char
  // chunks, 500 ms apart) so the paced reveal cannot grow the log inside the
  // clamp-measurement window and mask the clamp frame.
  _shrinkTail: function (firstText) {
    var s = "the committee will re-run the pilot in Q1 and publish the raw notes";
    var frames = [{ payload: { type: "delta", text: firstText }, afterMs: 90 }];
    for (var i = 0; i < 3; i++) {
      frames.push({ payload: { type: "delta", text: " " + s.slice(i * 30, (i + 1) * 30) }, afterMs: 500 });
    }
    frames.push({ payload: { type: "turn_complete" }, afterMs: 80 });
    frames.push({ payload: { type: "done", title: "Shrink probe" }, afterMs: 1200 });
    return frames;
  },

  scenarios: {
    // Trigger (the board's shape): thinking, then a long GFM table streamed
    // row by row. The answer overflows the log WELL before done (earlier
    // drafts were too short: the reveal lag kept the log under the fold until
    // the done flush itself, and the wheel landed after completion). done
    // lags the last delta ~1.2 s so the reveal backlog has drained: what is
    // left at done is the markdown re-layout and the thinking-strip collapse.
    trigger: function () {
      var rows = window.__mock._candidates();
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      frames = frames.concat(window.__mock._thinkFrames());
      frames.push({ payload: { type: "delta", text: window.__mock._intro + "\n\n" }, afterMs: 110 });
      frames.push({ payload: { type: "delta", text: "| Candidate | Strengths | Risks | Cost | Decision |\n| --- | --- | --- | --- | --- |\n" }, afterMs: 90 });
      for (var i = 0; i < rows.length; i += 2) {
        frames.push({ payload: { type: "delta", text: rows.slice(i, i + 2).join("\n") + "\n" }, afterMs: 55 });
      }
      frames.push({ payload: { type: "delta", text: "\n" + window.__mock._outro }, afterMs: 90 });
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Candidate comparison" }, afterMs: 1200 });
      return frames;
    },
    // Trigger variant: long bullet list (the board names lists too).
    "trigger-list": function () {
      var rows = window.__mock._candidates().map(function (r, i) {
        var parts = r.split(" | ");
        return "- **" + parts[0].replace(/^\| /, "") + "** — " + parts[1] +
          "; risk: " + parts[2] + ". Cost about " + parts[3] + ", committee said " + parts[4].replace(/ \|$/, "") + ".";
      });
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      frames = frames.concat(window.__mock._thinkFrames());
      frames.push({ payload: { type: "delta", text: window.__mock._intro + "\n\n" }, afterMs: 110 });
      for (var i = 0; i < rows.length; i += 2) {
        frames.push({ payload: { type: "delta", text: rows.slice(i, i + 2).join("\n") + "\n" }, afterMs: 55 });
      }
      frames.push({ payload: { type: "delta", text: "\n" + window.__mock._outro }, afterMs: 90 });
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Candidate notes" }, afterMs: 1200 });
      return frames;
    },
    // Control: same length and pacing, plain prose, NO thinking strip — the
    // only completion-path difference from the trigger is the re-layout, and
    // there is no strip left to collapse.
    control: function () {
      var paras = window.__mock._prose();
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      for (var i = 0; i < paras.length; i++) {
        frames.push({ payload: { type: "delta", text: paras[i] + "\n\n" }, afterMs: 140 });
      }
      frames.push({ payload: { type: "delta", text: window.__mock._outro }, afterMs: 120 });
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Method notes" }, afterMs: 1200 });
      return frames;
    },
    // Candidate (c) diagnostic: thinking strip first (leaves a focusable
    // .think-head so a KEYBOARD scroll-up can reach the log — follow.js
    // listens for wheel/touch only), then the same table.
    keyboard: function () {
      var rows = window.__mock._candidates();
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      frames = frames.concat(window.__mock._thinkFrames());
      frames.push({ payload: { type: "delta", text: window.__mock._intro + "\n\n" }, afterMs: 110 });
      frames.push({ payload: { type: "delta", text: "| Candidate | Strengths | Risks | Cost | Decision |\n| --- | --- | --- | --- | --- |\n" }, afterMs: 90 });
      for (var i = 0; i < rows.length; i += 2) {
        frames.push({ payload: { type: "delta", text: rows.slice(i, i + 2).join("\n") + "\n" }, afterMs: 55 });
      }
      frames.push({ payload: { type: "delta", text: "\n" + window.__mock._outro }, afterMs: 90 });
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Keyboard run" }, afterMs: 1200 });
      return frames;
    },
    // Following guard: the SAME answer as `trigger`, no gesture at all. The
    // view stays pinned and must settle at the bottom of a log that really
    // overflows. (`short` cannot prove this: its log never becomes
    // scrollable, so "at the bottom" is a tautology there.)
    follow: function () {
      return window.__mock.scenarios.trigger();
    },
    // Short plain reply, no scroll-up — a reply shorter than the log, the
    // reporter's "short replies don't snap" case: there is no scroll to move.
    short: function () {
      return [
        { payload: { type: "session" }, afterMs: 60 },
        { payload: { type: "delta", text: "Yes — the pilot covers two workloads and reports back after the fortnight." }, afterMs: 120 },
        { payload: { type: "turn_complete" }, afterMs: 60 },
        { payload: { type: "done", title: "Short answer" }, afterMs: 240 },
      ];
    },

    /* ---- shrink scenarios (candidate (b): browser scrollTop clamp) --------
       HISTORY since D2 (BK-030, 2026-10-31): the strip no longer has a tail,
       the closes below are height-neutral, and run_probe asserts that
       (`*-flat`). The pre-D2 account follows.
       The ONE content shrink this app definitely performs near completion is
       endThinking() removing a thinking strip's ~47.5 px tail
       (thinking.js:106, .think.done .think-tail / tail.remove()). It fires at
       a SEGMENT BOUNDARY: thinking_done (the gateway's normal close,
       chat_run.py:263), the first text delta (paintStream's guard,
       transcript.js:285), or closeSegments() on turn_complete/done/stopped.
       A view UNPINNED within the shrink distance of the bottom has no scroll
       call to intercept — the browser clamps scrollTop to the new maxTop
       itself. These scenarios park that view (fine wheel-up, no JS write)
       during a ~2 s static window, then land the close, tagged "shrink".
       A final render SHORTER than the streamed partial is NOT constructible
       honestly here: renderDelta reveals prefixes of the same _raw and
       finalizeAgentBubble renders _raw itself — the final re-layout can only
       GROW; see README "Mechanism verdict". */
    // (b)-PRIMARY: the gateway's NORMAL close. thinking_done lands after a
    // 2 s static window; endThinking() removes the tail — a pure ~47.5 px
    // shrink with no other DOM change in that frame.
    "shrink-close": function () {
      var frames = window.__mock._shrinkBase();
      frames.push({ payload: { type: "thinking_done" }, afterMs: 2000, tag: "shrink" });
      return frames.concat(window.__mock._shrinkTail("Closing note:"));
    },
    // GUARD (guard-follow-through-shrink): the shrink-close shape with the
    // view FOLLOWING (never parked) — a follower's spring must ride out the
    // same collapse without unpinning (BK-017 fix (c) Q3.4 iii).
    "follow-shrink": function () {
      var frames = window.__mock._shrinkBase();
      frames.push({ payload: { type: "thinking_done" }, afterMs: 2000, tag: "shrink" });
      return frames.concat(window.__mock._shrinkTail("Closing note:"));
    },
    // (b)-CLERIC-LITERAL: the SECOND thinking burst closed by a TEXT DELTA
    // (paintStream's dropped-thinking_done guard). The gateway does emit
    // thinking_done (chat_run.py:263); dropping it models the loss the guard
    // exists for. The first delta is "\n" so the fresh bubble renders NOTHING
    // (marked maps "\n" to "") and the shrink stays net-negative in the very
    // frame the next segment starts — the board's "re-lays out at a
    // different height" instant.
    "shrink-delta": function () {
      var frames = window.__mock._shrinkBase();
      frames.push({ payload: { type: "delta", text: "\n" }, afterMs: 2000, tag: "shrink" });
      var s = "the committee will re-run the pilot in Q1 and publish the raw notes";
      for (var i = 0; i < 3; i++) {
        frames.push({ payload: { type: "delta", text: "note " + s.slice(i * 25, (i + 1) * 25) }, afterMs: 500 });
      }
      frames.push({ payload: { type: "turn_complete" }, afterMs: 80 });
      frames.push({ payload: { type: "done", title: "Shrink probe" }, afterMs: 1200 });
      return frames;
    },
    // (b)-AT-COMPLETION: the strip is still OPEN when turn_complete arrives
    // (thinking_done dropped AND the turn's last segment is thinking) —
    // closeSegments() collapses it at the completion boundary itself; `done`
    // (naming tail) lands 0.4 s later over an already-quiet log.
    "shrink-turn": function () {
      var frames = window.__mock._shrinkBase();
      frames.push({ payload: { type: "turn_complete" }, afterMs: 2000, tag: "shrink" });
      frames.push({ payload: { type: "done", title: "Shrink probe", text: "" }, afterMs: 400 });
      return frames;
    },
    // (b)-VIEWPORT: NO thinking strip at all — the completion boundary itself
    // changes the VIEWPORT, not the content. app.js's turn_complete handler
    // calls status.stop(), hiding #chat-status, the IN-FLOW row between
    // #chat-jump and the composer (index.html; chat.css gives it its own
    // flex space): #chat-log grows ~28 px taller, maxTop drops by the same
    // ~28 px, and a browser clamps any unpinned view that was inside that
    // strip — again with no setter write and no scrollHeight change. The
    // last text delta is tagged "lastText" so the probe can wait for the
    // reveal to drain and park during the 2.4 s static window before the
    // tagged turn_complete.
    "status-clamp": function () {
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      var paras = window.__mock._prose().slice(0, 6);
      for (var i = 0; i < paras.length; i++) {
        frames.push({ payload: { type: "delta", text: paras[i] + "\n\n" }, afterMs: 130,
                      tag: i === paras.length - 1 ? "lastText" : null });
      }
      // 6 s of quiet: the probe waits for the 0.6 s reveal lag to drain and
      // for the spring to settle, then parks — the status row must still be
      // VISIBLE when the tagged frame lands, or there is nothing to clamp.
      frames.push({ payload: { type: "turn_complete" }, afterMs: 6000, tag: "shrink" });
      frames.push({ payload: { type: "done", title: "Status probe" }, afterMs: 1200 });
      return frames;
    },

    /* ---- Step-2 scenarios: the completion-adjacent vectors the wizard
       review named, as standing guards ------------------------------------- */
    // Shared spine of the stop-up pair: thinking + prose so the log overflows
    // WELL before the terminal frame — a wheel-up mid-run then has real slack.
    // The gateway really does end a stopped run on `stopped` (app.js's
    // handleChatEvent) and a failed one on `error`; both used to end the turn
    // with an unconditional jumpToBottom (addSystemMsg/addErrorMsg).
    _stopBase: function () {
      var m = window.__mock;
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      frames = frames.concat(m._thinkFrames());
      var paras = m._prose().slice(0, 6);
      for (var i = 0; i < paras.length; i++) {
        // "preTerm" on the last prose delta: the paced reveal keeps the log
        // UNDER the probe's tall threshold until the terminal frame's flush
        // (measured: h jumped 686 -> 1014 only AT stopped), so a height-based
        // wheel trigger lands after the terminal frame and the scenario
        // measures nothing. The tag is the wheel's deterministic cue.
        frames.push({ payload: { type: "delta", text: paras[i] + "\n\n" }, afterMs: 130,
                      tag: i === paras.length - 1 ? "preTerm" : null });
      }
      return frames;
    },
    "stop-stopped": function () {
      var frames = window.__mock.scenarios._stopBase();
      frames.push({ payload: { type: "stopped" }, afterMs: 300, tag: "term" });
      return frames;
    },
    "stop-error": function () {
      var frames = window.__mock.scenarios._stopBase();
      frames.push({ payload: { type: "error", error: "mock: the model call failed" },
                    afterMs: 300, tag: "term" });
      return frames;
    },
    // The #chat-status SUB-line (flex 0 0 100%, its own line inside the
    // in-flow row): shown by the 20 s slow-model hint, hidden by the next
    // phase; the whole row then hides at turn_complete. probe_scroll shifts
    // Date.now +21 s while this scenario is armed so the hint's first tick
    // shows the sub ~1 s after the model_wait frame instead of after 20 s —
    // the toggles are the point, not the wait. The clamp bites when the
    // viewport GROWS (maxTop drops), so the windows mean:
    //   show    — the sub line ADDS to the row: #chat-log gets SHORTER and
    //             maxTop RISES — a clamp is arithmetically impossible; the
    //             contract is the parked view does not move AT ALL and the
    //             gap grows by the sub line's height;
    //   subHide — a ZERO-GROWTH phase change (thinking_done -> "Deciding next step…")
    //             hides the sub: the log grows ~16 px back and the widened
    //             gap absorbs it — top stays valid, view stays put;
    //   shrink  — turn_complete hides the whole row (~28 px of log growth)
    //             onto the RESTORED ~4 px gap: the clamp BITES, the
    //             compensation write must land, the gap must survive — the
    //             proof the bracket compensates MEASURED deltas, not a
    //             hardcoded 28. Zero growth between subHide and the row hide
    //             is why subHide carries no text.
    "status-sub": function () {
      var m = window.__mock;
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      var tb = m._thinkB();
      for (var j = 0; j < tb.length; j++) {
        frames.push({ payload: { type: "thinking_delta", text: tb[j] }, afterMs: 90 });
      }
      frames.push({ payload: { type: "thinking_done" }, afterMs: 60 });
      var paras = m._prose().slice(0, 6);
      for (var i = 0; i < paras.length; i++) {
        frames.push({ payload: { type: "delta", text: paras[i] + "\n\n" }, afterMs: 130,
                      tag: i === paras.length - 1 ? "armPark" : null });
      }
      frames.push({ payload: { type: "model_wait" }, afterMs: 2500, tag: "subArm" });
      // +4500 (was +3000): the shift must land BETWEEN model_wait's show()
      // (which arms waitingSince) and the next 1 s tick — tagAt stamps the
      // EMIT, the app processes the frame a task later, and a busy park loop
      // can push that gap past a second. The label wait in run_probe syncs on
      // the processing; the extra 1.5 s here gives the shifted clock 3-4
      // ticks before this frame's phase("Deciding next step…") nulls waitingSince.
      // thinking_done (NOT a delta): hides the sub with ZERO content growth,
      // so the row hide below lands on the restored ~4 px gap and the clamp
      // bites deterministically. No thinking strip is open here, so the
      // transcript-side endThinking() is a no-op — only the phase moves.
      frames.push({ payload: { type: "thinking_done" }, afterMs: 4500, tag: "subHide" });
      // +600 (not +60): the HIDE window the probe measures is 400 ms wide and
      // must not be trampled by the row-hide right after it.
      frames.push({ payload: { type: "turn_complete" }, afterMs: 600, tag: "shrink" });
      frames.push({ payload: { type: "done", title: "Sub-line probe" }, afterMs: 1200 });
      return frames;
    },

    /* ---- Step-3 matrix shapes (plan criterion 2) ---------------------------
       Each streams a realistic answer of its shape over ~1.5-2.5 s, then a
       turn_complete + done pair 800 ms apart (800 > the 0.6 s reveal lag, so
       the DONE frame is the finalize flush + re-layout — exactly the moment
       the matrix measures). No thinking strip: the strip-collapse vectors are
       the shrink-* scenarios' job; the matrix varies the ANSWER SHAPE.
       Every outro ends in plain words: the b-state "no half-rendered gap"
       check asserts the rendered text ends with the same tail bytes. */
    // Fenced code block: streamed line-by-line inside an OPEN fence — the
    // completion re-layout turns partial fence text into one <pre>, the
    // board's "re-lays out at a different height" family. Tall enough to
    // overflow the iPhone-port log on its own (~900 px).
    "shape-code": function () {
      var code = [
        "def summarize(rows):",
        "    total = 0",
        "    by_owner = {}",
        "    for row in rows:",
        "        owner = row['owner'] or 'unassigned'",
        "        amount = int(row['amount'])",
        "        total += amount",
        "        by_owner.setdefault(owner, []).append(amount)",
        "    ranked = sorted(by_owner.items(), key=lambda kv: -sum(kv[1]))",
        "    return {'total': total, 'by_owner': ranked}",
        "",
        "def render(summary, out):",
        "    print(f\"total {summary['total']}\", file=out)",
        "    for owner, amounts in summary['by_owner']:",
        "        share = sum(amounts) / summary['total']",
        "        print(f'  {owner}: {share:.1%} across {len(amounts)} rows', file=out)",
        "    if not summary['by_owner']:",
        "        print('  (no rows — check the export job first)', file=out)",
        "",
        "def main():",
        "    rows = load_candidates('september.csv')",
        "    summary = summarize(rows)",
        "    with open('summary.txt', 'w') as out:",
        "        render(summary, out)",
        "",
        "if __name__ == '__main__':",
        "    main()",
      ];
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      frames.push({ payload: {
        type: "delta",
        text: "Here is the rollup script the committee asked for; the ranking is by owner share and the empty case is called out on purpose:\n\n",
      }, afterMs: 110 });
      frames.push({ payload: { type: "delta", text: "```python\n" }, afterMs: 70 });
      for (var i = 0; i < code.length; i += 3) {
        frames.push({ payload: { type: "delta", text: code.slice(i, i + 3).join("\n") + "\n" }, afterMs: 70 });
      }
      frames.push({ payload: { type: "delta", text: "```\n\n" }, afterMs: 70 });
      frames.push({ payload: {
        type: "delta",
        text: "The share line needs the total to be non-zero, which is what the guard at the end covers. The export job writes the csv nightly, so a rerun is safe.",
      }, afterMs: 110 });
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Rollup script" }, afterMs: 800 });
      return frames;
    },
    // Mixed shape: prose + bullet list + GFM table + emoji in one answer —
    // the widest re-layout surface, and it exercises renderDelta's
    // surrogate-pair guard (emoji never split mid-codepoint).
    // The reveal pacing is OFF for this shape (probe-side: nothing paints
    // until the finalize render): the gateway sends the whole answer, so the
    // mid-stream DOM is the streamed partial with the fence UNCLOSED — the
    // shape's point is the done re-layout rendering the whole bubble at a
    // DIFFERENT height (list + table + emoji appear), the board's "re-lays
    // out at a different height" family, and the b-state tail that must
    // finish rendering at finalize, not sit half-shown behind a pill.
    "shape-mixed": function () {
      var items = [
        "- 🚀 **ship** — the pilot hit every milestone ✅ and the notes are signed off",
        "- ⚠️ **hold** — procurement flagged the licence 🧾 twice, third time is a blocker",
        "- 🧪 **pilot** — two teams 🤝 volunteered, the fortnight trial starts in Q1",
        "- 🗑️ **drop** — single maintainer, the issue backlog is already slow 🐌",
        "- 🔁 **revisit** — re-score when the pricing page 📄 changes, not before",
      ];
      var rows = [
        "| Tool | Vote | Note |",
        "| --- | --- | --- |",
        "| alpha-1 | 🚀 ship | warm cache wins 🏆 the benchmarks |",
        "| boron-28 | ⚠️ hold | licence 🧾 turns commercial |",
        "| cypher-29 | 🧪 pilot | two teams 🤝 volunteered |",
        "| dynamo-30 | 🗑️ drop | backlog slow 🐌 since summer |",
      ];
      // Same stream LENGTH and cadence as `trigger` (the board's shape):
      // the committee's long list (12), the full 24-row voted table, prose
      // seams — the mixed shape is the widest re-layout surface AND a
      // full-length stream, so the a-state gesture lands with a real reveal
      // backlog running (a short script finishes its stream before the
      // gesture, and the a row then measures nothing; measured 2026-10-29).
      var seeds = [
        "- 🚀 **ship** — the pilot hit every milestone ✅ and the weekly notes are signed off; the team that ran it would carry the migration effort themselves if asked, which says more than any benchmark chart did",
        "- ⚠️ **hold** — procurement flagged the licence 🧾 twice, and the third flag is a blocker: the per-seat number turns commercial exactly at our headcount, the line the committee was never given in writing",
        "- 🧪 **pilot** — two teams 🤝 volunteered for the Q1 fortnight, on the explicit condition that the raw notes stay in the shared folder this time, not in one person's drafts",
        "- 🗑️ **drop** — a single maintainer and an issue backlog that has been slow 🐌 since summer; neither is a criticism of the work, both are a description of the bus factor",
        "- 🔁 **revisit** — re-score when the pricing page 📄 changes or the licence terms move, whichever comes first, and not before: the September numbers were honest, they are just no longer current",
      ];
      var items = [];
      for (var s = 0; s < 3; s++) {
        items = items.concat(seeds.map(function (t, i) {
          return t.replace(/—/, "—(" + (s + 1) + "." + (i + 1) + ")");
        }));
      }
      var base = [
        "| Tool | Vote | Note |",
        "| --- | --- | --- |",
      ];
      var bodies = [];
      for (var r = 0; r < 8; r++) {
        bodies = bodies.concat([
          "| alpha-" + (r + 1) + " | 🚀 ship | warm cache wins 🏆 the cold benchmarks, and the migration cost stayed under the estimate for once |",
          "| boron-" + (r + 1) + " | ⚠️ hold | the licence 🧾 turns commercial at our headcount; procurement wants the number in writing before anyone signs |",
          "| cypher-" + (r + 1) + " | 🧪 pilot | two teams 🤝 volunteered for the fortnight, conditional on the raw notes living in the shared folder |",
          "| dynamo-" + (r + 1) + " | 🗑️ drop | one maintainer, and the issue backlog has been slow 🐌 since summer — descriptive, not an insult |",
        ]);
      }
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      frames.push({ payload: {
        type: "delta",
        text: "The short version first, then the list the committee argued over, then the table it actually voted on:\n\n",
      }, afterMs: 110 });
      for (var i = 0; i < items.length; i += 2) {
        frames.push({ payload: { type: "delta", text: items.slice(i, i + 2).join("\n") + "\n" }, afterMs: 70 });
      }
      // The park seam: a ~1.4 s pause before the table block. With the
      // reveal off for this shape, the DOM is static here — the probe parks
      // the gesture inside this window (its DOM stopped changing), and the
      // TABLE BLOCK is the growth that arrives on the parked view: the
      // "block lands while you read" moment the fix exists for.
      frames.push({ payload: { type: "delta", text: "\nAnd the votes, as recorded:\n\n" },
                    afterMs: 1400 });
      frames.push({ payload: { type: "delta", text: base.join("\n") + "\n" }, afterMs: 90 });
      for (var j = 0; j < bodies.length; j += 2) {
        frames.push({ payload: { type: "delta", text: bodies.slice(j, j + 2).join("\n") + "\n" }, afterMs: 55 });
      }
      frames.push({ payload: {
        type: "delta",
        text: "\nOverall the room was warmer than the September vote — ship one, hold one, and revisit the rest when the pricing page changes.",
      }, afterMs: 110 });
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Mixed verdict" }, afterMs: 800 });
      return frames;
    },
    // Short plain reply, SLOW: the delta lands 900 ms after submit so the
    // matrix-a gesture (which needs the history session's scroll room, not
    // this answer's) has a window before completion. Same bytes as `short`,
    // different pacing — `short` is the mechanism-suite fit-check.
    "short-plain": function () {
      return [
        { payload: { type: "session" }, afterMs: 60 },
        { payload: { type: "delta", text: "Yes — the pilot covers two workloads; the notes are in the shared folder." }, afterMs: 900 },
        { payload: { type: "turn_complete" }, afterMs: 60 },
        { payload: { type: "done", title: "Short plain" }, afterMs: 300 },
      ];
    },
    // Regression-guard script (criterion 4): the delta waits 2.5 s so the
    // probe can assert the SEND jump (addUserMsg's jumpToBottom) on a still
    // short stream — the answer must not grow the log before the assert.
    "guard-send": function () {
      return [
        { payload: { type: "session" }, afterMs: 60 },
        { payload: { type: "delta", text: "Pong — the guard reply is short and lands at the bottom." }, afterMs: 2500 },
        { payload: { type: "turn_complete" }, afterMs: 60 },
        { payload: { type: "done", title: "Guard send" }, afterMs: 300 },
      ];
    },
    // Anchor residual R2 (gap-0 carry): long enough to overflow BOTH ports,
    // with the tail dripped word-by-word ~300 ms apart. The probe parks a
    // tap-unpinned view AT gap 0 during the drip: with the browser's own
    // bottom-edge anchoring every drip would carry the view down (and
    // maybeRepin would misread the carry as intent); with the shipped
    // `overflow-anchor: none` the view must stay put while the words grow
    // below it. Ends in plain words (the b-state bubble contract).
    gap0: function () {
      var m = window.__mock;
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      var paras = m._prose().slice(0, 6);
      for (var i = 0; i < paras.length; i++) {
        frames.push({ payload: { type: "delta", text: paras[i] + "\n\n" }, afterMs: 250 });
      }
      // Tagged STATIC window (1.7 s): the tap-unpin + park + insertAbove all
      // land inside it, so the only mover of the parked view is the browser's
      // own anchor — no reveal pacing, no spring, no drip to blur the verdict.
      frames.push({ payload: { type: "delta", text: "Parked. " }, afterMs: 2200,
                    tag: "gap0-static-window" });
      var tail = "And the gap-0 outro arrives word by word below the parked view.";
      var words = tail.split(" ");
      for (var w = 0; w < words.length; w++) {
        frames.push({ payload: { type: "delta", text: (w ? " " : "") + words[w] },
                      afterMs: 300 });
      }
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Gap-0 park" }, afterMs: 400 });
      return frames;
    },

    // BK-029 touch rules + D1 (guards family, run_touch_rules): the log
    // overflows BOTH ports first, then three tagged phases the probe acts in —
    // a slow THINKING segment (header on screen, streaming), a SLOW tool call
    // (pill running ~3 s), and a word DRIP (the answer streaming, ~3.5 s,
    // a paragraph every 3 words so the log keeps growing).
    touchrules: function () {
      var m = window.__mock;
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      var paras = m._prose().slice(0, 6);
      for (var i = 0; i < paras.length; i++) {
        frames.push({ payload: { type: "delta", text: paras[i] + "\n\n" }, afterMs: 150 });
      }
      for (var t = 0; t < 30; t++) {
        frames.push({ payload: { type: "thinking_delta", text: "weighing the touch rule " + t + ". " },
                      afterMs: t ? 100 : 500, tag: t ? undefined : "tr-think" });
      }
      frames.push({ payload: { type: "thinking_done" }, afterMs: 60 });
      frames.push({ payload: { type: "tool_call", tool: "runs_summary", args: { days: 7 } },
                    afterMs: 200, tag: "tr-tool" });
      frames.push({ payload: { type: "tool_result", is_error: false, digest: "job-1: 3 runs", chars: 13 },
                    afterMs: 3000 });
      for (var w = 0; w < 45; w++) {
        frames.push({ payload: { type: "delta", text: (w ? " " : "") + "drip" + w + (w % 3 === 2 ? ".\n\n" : "") },
                      afterMs: w ? 80 : 300, tag: w ? undefined : "tr-drip" });
      }
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Touch rules" }, afterMs: 400 });
      return frames;
    },

    // BK-029 un-scrollable case: a dripped reply that never overflows the log
    // on either port (fresh chat), streaming ~2 s so a press lands mid-answer.
    trshort: function () {
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      for (var w = 0; w < 12; w++) {
        frames.push({ payload: { type: "delta", text: (w ? " " : "") + "short" + w },
                      afterMs: w ? 160 : 200, tag: w ? undefined : "trs-drip" });
      }
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Short touch" }, afterMs: 300 });
      return frames;
    },

    /* ---- BK-019 Step-5b: CHAT-transport recovery scenarios (recover family) --
       runstate's recovery transitions (T15 socketDied, T16 catchingUp, T03
       screenLeave, T12 sendRejected, T13 sendFailed, T14 streamEndSilent) are
       driven by what the SERVER does when the transport fails, so these
       scripts model the server, not the client: a die:true frame REJECTS the
       reader (the frame is server-EMITTED — it rides the /live ring — the
       socket just never delivers it), the run keeps running server-side, and
       serverDone persists the answer when the scripted call returns. No done
       frame ever reaches a dead reader. chatFail scripts never start a run:
       the POST itself answers !ok (409 busy) or rejects (network down). */
    // T15/T16: die mid-stream at ~1.1 s. The server finishes ~16 s in —
    // after the watchdog's first tick (~10 s, honest 10 s interval, gap-fill
    // lands there) and before its second, where /live says active:false and
    // the persisted heal runs. Every post-death delta carries "t15ring": the
    // gap-fill / persisted-text assertions read those bytes.
    "t15-die": function () {
      var frames = [
        { payload: { type: "session" }, afterMs: 60 },
        { payload: { type: "thinking_delta", text: "t15 weighing the notes " }, afterMs: 120 },
        { payload: { type: "thinking_done" }, afterMs: 60 },
        { payload: { type: "delta", text: "t15 head " }, afterMs: 130 },
        { payload: { type: "delta", text: "streaming " }, afterMs: 130 },
        { payload: { type: "delta", text: "in. " }, afterMs: 130 },
        { payload: { type: "delta", text: "t15ring missed-one " }, afterMs: 200, die: true },
        { payload: { type: "delta", text: "t15ring missed-two " }, afterMs: 150 },
        { payload: { type: "delta", text: "t15ring missed-three " }, afterMs: 150 },
        { payload: { type: "delta", text: "and the tail word." }, afterMs: 150 },
      ];
      return { frames: frames, serverDone: 16000 };
    },
    // T14: the stream ends CLEANLY with no terminal frame (agentd restarted
    // mid-run — the reader sees done, the transport cannot tell it from a
    // finished stream). runClosed true, doneAt null.
    "t14-silent": function () {
      return { frames: [
        { payload: { type: "session" }, afterMs: 60 },
        { payload: { type: "delta", text: "t14 partial answer " }, afterMs: 120 },
        { payload: { type: "delta", text: "keeps going " }, afterMs: 130 },
        { payload: { type: "delta", text: "then the socket closes." }, afterMs: 130 },
      ] };
    },
    // T03: still streaming when the user switches sessions (tag switchNow
    // mid-run); the run then COMPLETES normally on the dead screen — the
    // latch must keep every later frame off the new screen's DOM.
    "t03-switch": function () {
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      var words = ["t03alpha one ", "t03alpha two ", "t03alpha three ", "t03alpha four ",
                   "t03alpha five ", "t03alpha six ", "t03alpha seven ", "final reading. "];
      for (var i = 0; i < words.length; i++) {
        frames.push({ payload: { type: "delta", text: words[i] }, afterMs: 150,
                      tag: i === 3 ? "switchNow" : null });
      }
      frames.push({ payload: { type: "turn_complete" }, afterMs: 120 });
      frames.push({ payload: { type: "done", title: "T03 switch" }, afterMs: 300 });
      return frames;
    },
    // BK-024 (smoothness `reopen` suite): the SENDER switches away mid-run and
    // back. liveJoin: the run's ring answers /messages' `live` (and /live)
    // while it is in flight — the real server's join — and the done frame is
    // the call returning (serverDone = its offset; persisted once).
    "reopen-mid": function () {
      var frames = [{ payload: { type: "session" }, afterMs: 60 }];
      for (var i = 0; i < 24; i++) {
        frames.push({ payload: { type: "delta", text: "rmword" + i + " " }, afterMs: 250 });
      }
      frames.push({ payload: { type: "delta", text: "rmfinal." }, afterMs: 250 });
      frames.push({ payload: { type: "turn_complete" }, afterMs: 60 });
      frames.push({ payload: { type: "done", title: "Reopen mid" }, afterMs: 200 });
      var at = 0;
      frames.forEach(function (f) { at += f.afterMs; });
      return { frames: frames, serverDone: at, liveJoin: true };
    },
    // T12: the POST answers 409 (a detached run holds the session) — the run
    // never starts, the store stays untouched, the detail line is the news.
    "t12-busy": function () {
      return { frames: [], chatFail: { mode: "status", status: 409,
                                       detail: "another run is in flight (mock)" } };
    },
    // T13: the fetch itself throws before any server answer — never-started
    // turn: an error line, NOT a detach (no resync, no watchdog).
    "t13-netfail": function () {
      return { frames: [], chatFail: { mode: "fetch", message: "mock: network down" } };
    },
  },

  arm: function (name, sid) {
    var m = window.__mock;
    var sc = m.scenarios[name]();
    var frames = Array.isArray(sc) ? sc : sc.frames;
    // chat_run.py's emit(): every run event stamped {seq, run_id} once,
    // before any sink. `session` included — and ONLY the session frame
    // carries session_id, exactly like chat_sse_response (the recovery math
    // reads the divergence against that confirmation).
    frames.forEach(function (f, i) {
      // Fresh payload per arm: builders may reuse frame objects across
      // scenarios, and stamping in place would renumber the SAME object
      // twice (replay.js's watermark would then read the second run's
      // events as already-painted).
      f.payload = Object.assign({}, f.payload, { seq: i + 1, run_id: "probe-run-" + name });
      if (f.payload.type === "session") f.payload.session_id = sid;
    });
    m.chat = {
      name: name, sid: sid, runId: "probe-run-" + name, frames: frames,
      fail: (Array.isArray(sc) ? null : sc.chatFail) || null,
      serverDone: Array.isArray(sc) ? null : (sc.serverDone != null ? sc.serverDone : null),
      liveJoin: !Array.isArray(sc) && !!sc.liveJoin,
      // The seq the die frame would have carried — the recover scenarios
      // number their rerouted /api/events deliveries right after it.
      dieSeq: (function () {
        for (var k = 0; k < frames.length; k++) {
          if (frames[k].die) return frames[k].payload.seq;
        }
        return null;
      })(),
    };
    // A previous run's server-side completion must not outlive its scenario
    // (a stale timer would persist into the next store window).
    if (m.serverRun && m.serverRun.timer) clearTimeout(m.serverRun.timer);
    m.serverRun = null;
    m.doneAt = null;
    m.runClosed = false;
    m.tagAt = {};
    return { sid: sid, runId: m.chat.runId, frames: frames.length };
  },

  reset: function () {
    var m = window.__mock;
    if (m.serverRun && m.serverRun.timer) clearTimeout(m.serverRun.timer);
    m.calls = [];
    m.chat = null;
    m.serverRun = null;
    m.doneAt = null;
    m.runClosed = false;
    m.tagAt = {};
    // store SURVIVES (the heal scenario reopens a finished session's rows)
  },

  // What the persisted store answers for a session (timing-honest).
  messagesFor: function (sid) {
    var row = this.store[sid];
    return row ? row.messages : [];
  },

  // The model call RETURNS — the store gains the answer (the timing rule in
  // the header). Shared by the done frame (a live reader watched the stream
  // finish) and the server-side completion of a run whose reader DIED
  // (serverRun): the call returns whether anyone is listening or not.
  _persistAnswer: function (sid, payloads, title) {
    var m = window.__mock;
    var full = payloads
      .filter(function (p) { return p.type === "delta"; })
      .map(function (p) { return p.text; })
      .join("");
    var think = payloads
      .filter(function (p) { return p.type === "thinking_delta"; })
      .map(function (p) { return p.text; })
      .join("");
    var blocks = [];
    if (think) blocks.push({ type: "thinking", thinking: think });
    blocks.push({ type: "text", text: full });
    var prior = m.store[sid] ? m.store[sid].messages : [];
    m.store[sid] = {
      messages: prior.concat([{
        role: "assistant", text: full,
        tool_calls: [], tool_results: [], content_blocks: blocks,
      }]),
      title: title || (m.store[sid] && m.store[sid].title) || "",
    };
  },

  _startRun: function (body) {
    var m = window.__mock;
    if (!m.chat) throw new Error("probe: POST /api/chat with no armed scenario");
    var sid = m.chat.sid;
    // The store persists the user turn as the run starts; the assistant row
    // arrives only when the call RETURNS (agent.py) — see done below.
    // APPEND, never replace: a turn in an existing session keeps its history
    // rows (the real backend does), or the second turn in a session silently
    // deletes the transcript the reporter is reading.
    var prior = m.store[sid] ? m.store[sid].messages : [];
    m.store[sid] = {
      messages: prior.concat([{ role: "user", text: body.text || "", attachments: [] }]),
    };
    var enc = new TextEncoder();
    var t0 = performance.now();
    var acc = 0;
    var sched = m.chat.frames.map(function (f) {
      acc += f.afterMs;
      return { payload: f.payload, at: acc, tag: f.tag, die: f.die };
    });
    var i = 0;
    var chat = m.chat;
    // A script with serverDone keeps running SERVER-side: /live answers from
    // this state, and the answer lands in the store when the call returns —
    // whether or not any reader is still alive to see a done frame (there
    // isn't one on the die scripts: the reader rejects).
    if (chat.serverDone != null) {
      m.serverRun = {
        sid: sid, runId: chat.runId, t0: t0,
        // The EventRing holds RUN events (chat_run's emit stamps them); the
        // `session` frame is yielded by the SSE generator itself, never the
        // ring — /live's batch must not carry it. `at` is the SCRIPT offset
        // the server generates the event at: the schedule is the SERVER's
        // cadence (unchanged by a dead socket), and the ring holds an event
        // only once its time has passed.
        frames: chat.frames
          .map(function (f, k) { return { payload: f.payload, at: sched[k].at }; })
          .filter(function (e) { return e.payload.type !== "session"; }),
        doneAt: t0 + chat.serverDone, answered: false, timer: null,
      };
      m.serverRun.timer = setTimeout(function () {
        if (!m.serverRun || m.serverRun.answered) return;
        m.serverRun.answered = true;   // the call returned: /live reads inactive
        m._persistAnswer(sid, m.serverRun.frames.map(function (e) { return e.payload; }), "");
      }, Math.max(0, chat.serverDone - (performance.now() - t0)));
    }
    return {
      ok: true,
      status: 200,
      statusText: "OK (mock SSE)",
      body: {
        getReader: function () {
          return {
            read: function () {
              if (i >= sched.length) {
                m.runClosed = true;
                return Promise.resolve({ done: true, value: undefined });
              }
              var f = sched[i++];
              var wait = Math.max(0, f.at - (performance.now() - t0));
              return new Promise(function (res, rej) {
                setTimeout(function () {
                  // die:true: the socket STOPS here — this frame was emitted
                  // server-side (it rides the /live ring for the gap-fill)
                  // but its bytes never arrive. A real mid-stream HTTP death
                  // is a REJECT, not an event: the transport cannot report it
                  // any other way (app.js's catch → runstate T15/T13 split).
                  if (f.die) { rej(new Error("probe: socket died")); return; }
                  // Tag the EMIT at the resolve tick, NOT before the done
                  // bookkeeping below: microtasks run before setTimeout
                  // callbacks, so a timestamp taken earlier would sit 1-2 ms
                  // BEHIND doneAt on the sampler's timeline (the shrink
                  // windows are 250 ms, so it must share that anchor).
                  if (f.tag) m.tagAt[f.tag] = performance.now();
                  // Terminal frames stamp doneAt — run_probe's wait_done is
                  // "the terminal frame landed", and the stopped/error runs
                  // (BK-017 stop-up) end the stream on stopped/error, never
                  // on done. The STORE bookkeeping below stays done-only
                  // (the answer persists only when the call RETURNS).
                  if (f.payload.type === "done" || f.payload.type === "stopped"
                      || f.payload.type === "error") m.doneAt = performance.now();
                  if (f.payload.type === "done" && !(m.serverRun && m.serverRun.answered)) {
                    // liveJoin runs: the call returned — /live and /messages see it once.
                    if (m.serverRun) m.serverRun.answered = true;
                    // The model call returned: NOW the store gains the answer.
                    m._persistAnswer(chat.sid,
                      chat.frames.map(function (g) { return g.payload; }),
                      f.payload.title || "");  // picker row title (the done frame carries it)
                    m.doneAt = performance.now();
                  }
                  res({ done: false, value: enc.encode("data: " + JSON.stringify(f.payload) + "\n\n") });
                }, wait);
              });
            },
            cancel: function () { return Promise.resolve(); },
          };
        },
      },
    };
  },
};

(function () {
  var real = window.fetch.bind(window);
  window.fetch = function (path, opts) {
    var p = String(path);
    if (p.indexOf("/api/") !== 0) return real(path, opts);
    var m = window.__mock;
    m.calls.push(p);
    var method = ((opts && opts.method) || "GET").toUpperCase();
    var okJson = function (body) {
      return Promise.resolve({
        ok: true, status: 200, statusText: "OK (mock)",
        json: function () { return Promise.resolve(body); },
      });
    };
    if (p === "/api/chat" && method === "POST") {
      var body = {};
      try { body = JSON.parse((opts && opts.body) || "{}"); } catch (_) { /* probe always sends JSON */ }
      var fail = m.chat && m.chat.fail;
      if (fail && fail.mode === "fetch") {
        // T13: the request never completes — no run, no store row, no
        // server-side state of any kind (the wire, not the gateway, failed).
        return Promise.reject(new Error(fail.message || "mock: fetch failed"));
      }
      if (fail) {
        // T12: !ok BEFORE a run exists — e.g. 409 while a detached run holds
        // the session (gw.busy_reason). The store stays untouched.
        return Promise.resolve({
          ok: false, status: fail.status || 409, statusText: "Rejected (mock)",
          json: function () { return Promise.resolve({ detail: fail.detail || "busy (mock)" }); },
        });
      }
      return Promise.resolve(m._startRun(body));
    }
    var mm = /^\/api\/sessions\/([^/?]+)\/messages(?:\?.*)?$/.exec(p);
    if (mm) {
      // The Step-4 route shape; `live` stays null (no ring in this mock — the
      // heal path only ever runs post-run here, when the store is complete).
      var body = { messages: m.messagesFor(decodeURIComponent(mm[1])), live: null, compactions: [] };
      // BK-041 (smoothness activity suite): a stored session may name a mid-run
      // join — `live` (chat run) / `bg_live` (cron run). Absent keys stay absent:
      // bg_live undefined = a pre-BK-041 server, so older scenarios are unchanged.
      var rec0 = m.store[decodeURIComponent(mm[1])];
      var lr = m.serverRun, chat0 = m.chat;
      if (chat0 && chat0.liveJoin && lr && lr.sid === decodeURIComponent(mm[1]) && !lr.answered) {
        var gen0 = lr.frames.filter(function (e) { return e.at <= performance.now() - lr.t0; })
          .map(function (e) { return e.payload; });
        body.live = { since: 0, seq: gen0.length ? gen0[gen0.length - 1].seq : 0,
                      oldest_seq: gen0.length ? gen0[0].seq : null, gap: false, events: gen0 };
      }
      if (rec0 && rec0.live) body.live = rec0.live;
      if (rec0 && "bg_live" in rec0) body.bg_live = rec0.bg_live;
      return okJson(body);
    }
    // BK-041: the pending approvals a scenario arms (default: none, as before).
    if (p === "/api/approvals") return okJson(m.approvals || []);
    var lm = /^\/api\/chat\/([^/?]+)\/live(?:\?.*)?$/.exec(p);
    if (lm) {
      // The resync read (replay.js fetchLive). Honest server: while the
      // call is out, {active:true, seq, events: ring entries with seq >
      // since, generated so far}; once it returned, {active:false} and the
      // store carries the row — so resyncVerdict's six branches decide the
      // scenario themselves (gap-fill while active, persisted heal after).
      // No serverRun for this session (every older scenario) = inactive,
      // exactly as the always-{active:false} answer this replaces.
      var run = m.serverRun;
      if (!run || run.sid !== decodeURIComponent(lm[1]) || run.answered) {
        return okJson({ active: false });
      }
      var since = 0;
      var sm = /[?&]since=(\d+)/.exec(p);
      if (sm) since = parseInt(sm[1], 10);
      var elapsed = performance.now() - run.t0;
      var gen = run.frames.filter(function (e) { return e.at <= elapsed; });
      return okJson({
        active: true,
        seq: gen.length ? gen[gen.length - 1].payload.seq : 0,
        oldest_seq: gen.length ? gen[0].payload.seq : null,
        gap: false,
        events: gen.filter(function (e) { return e.payload.seq > since; })
          .map(function (e) { return e.payload; }),
      });
    }
    if (p === "/api/health") return okJson({ ok: true, local: true, sonnet: false, stt: false });
    if (p === "/api/sessions") {
      // Honest picker rows: every stored session whose ANSWER persisted (the
      // row only exists once the call returned — same timing rule as the
      // header). Step-3's guard-picker-jumps drives the real picker change
      // handler, which needs the option to exist after loadSessions().
      var rows = Object.keys(m.store).map(function (id) {
        var rec = m.store[id];
        var done = rec.messages.some(function (msg) { return msg.role === "assistant"; });
        return done ? { id: id, title: rec.title || "", channel: "web", active: false } : null;
      }).filter(Boolean);
      return okJson(rows);
    }
    if (/^\/api\/sessions\/[^/?]+$/.test(p)) {
      return Promise.resolve({
        ok: false, status: 404, statusText: "Not Found (mock: absent mode)",
        json: function () { return Promise.resolve({ detail: "Not Found" }); },
      });
    }
    // Array consumers (approvals, tools, playbooks, runs…) and POST side
    // effects (timezone, detach, stop) — nothing in the probe reads these.
    return okJson([]);
  };

  // EventSource: constructed, never opens, never fires (see header).
  window.EventSource = function () {
    this.close = function () {};
    this.addEventListener = function () {};
  };
  window.EventSource.prototype = { OPEN: 1, CLOSED: 2, CONNECTING: 0 };

  // Service worker: register()/ready never settle (see header).
  try {
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        register: function () { return new Promise(function () {}); },
        ready: new Promise(function () {}),
        addEventListener: function () {},
      },
    });
  } catch (_) { /* defineProperty throws if the browser made serviceWorker
                   non-configurable; the probe targets chromium, where it is */ }
})();
