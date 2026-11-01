/* Smoothness instrument — injected by common.boot() after the probe page boots.
   Read-only over app state: wraps marked.parse / DOMPurify.sanitize / the
   innerHTML setter for TIMING only, and samples layout once per rAF. */
(() => {
  const log = document.querySelector("#chat-log");
  const S = window.__S = {
    t0: performance.now(), frames: [], parse: [], sanitize: [], inner: [],
    removed: 0, added: 0, retypes: [], shrinks: [], bigGrows: [],
    rawMarkerFrames: 0, bubbleFrames: 0, thinkCollapse: [], on: true,
  };
  const mp = marked.parse.bind(marked);
  marked.parse = (src, o) => { const t = performance.now(); const r = mp(src, o);
    S.parse.push([performance.now() - t, (src || "").length]); return r; };
  const ds = DOMPurify.sanitize.bind(DOMPurify);
  DOMPurify.sanitize = (h, o) => { const t = performance.now(); const r = ds(h, o);
    S.sanitize.push(performance.now() - t); return r; };
  const d = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  Object.defineProperty(Element.prototype, "innerHTML", { configurable: true,
    get() { return d.get.call(this); },
    set(v) { const t = performance.now(); d.set.call(this, v);
      if (this.classList && this.classList.contains("md")) S.inner.push(performance.now() - t); } });
  new MutationObserver((recs) => { for (const r of recs) { S.removed += r.removedNodes.length; S.added += r.addedNodes.length; } })
    .observe(log, { childList: true, subtree: true });

  let last = 0, prevMd = null, prevH = 0, prevSig = [];
  const proseText = (md) => { let t = ""; const w = document.createTreeWalker(md, NodeFilter.SHOW_TEXT);
    for (let n; (n = w.nextNode());) if (!n.parentElement.closest("pre, code")) t += n.data; return t; };
  const thinkH = new Map();
  const tick = (now) => {
    if (!S.on) return;
    if (last) S.frames.push(now - last);
    last = now;
    const mds = log.querySelectorAll(".msg.agent .md");
    const md = mds[mds.length - 1];
    if (md) {
      const h = md.offsetHeight;
      const sig = Array.from(md.children, (c) => c.tagName + (c.className ? "." + c.className : ""));
      if (md === prevMd) {
        S.bubbleFrames++;
        if (h < prevH - 1) S.shrinks.push(Math.round(prevH - h));
        if (h > prevH + 60) S.bigGrows.push(Math.round(h - prevH));
        const n = Math.min(sig.length, prevSig.length);
        for (let i = 0; i < n; i++) {
          if (sig[i] !== prevSig[i]) { S.retypes.push(`${prevSig[i]}→${sig[i]}`); break; }
        }
        // Prose text only: a real `**kwargs` or ``` inside a code block is content, not a raw marker.
        if (/\*\*|^\s*\|.*\|\s*$|```/m.test(proseText(md))) S.rawMarkerFrames++;
      }
      prevMd = md; prevH = h; prevSig = sig;
    }
    for (const th of log.querySelectorAll(".think")) {
      const h = th.offsetHeight, rec = thinkH.get(th);
      if (rec && rec.streaming && th.classList.contains("done")) S.thinkCollapse.push(Math.round(rec.h - h));
      thinkH.set(th, { h, streaming: th.classList.contains("streaming") });
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  // Realistic local-model answer: lumpy ~24-char chunks, periodic stalls, mixed markdown.
  const answer = [
    "## What I found\n\nThe **ingestion path** is the bottleneck, not the parser. Three things stand out, and the `batch_size` default is the biggest one:\n\n",
    "1. The worker pool is capped at **four** threads even on the Mac mini.\n2. Each batch re-opens the SQLite connection (`connect()` per call).\n3. Retries back off linearly, so a flaky upstream stalls the whole queue.\n\n",
    "Here is the change I would make first:\n\n```python\nasync def ingest(items, *, batch_size=256):\n    async with pool.acquire() as conn:\n" +
      Array.from({ length: 18 }, (_, i) => `        await conn.execute(SQL_${i}, items[${i}::batch_size])  # step ${i}\n`).join("") + "    return len(items)\n```\n\n",
    "And the measured effect across the last week of runs:\n\n| Run | Before (s) | After (s) | Items | Notes |\n| --- | --- | --- | --- | --- |\n" +
      Array.from({ length: 12 }, (_, i) => `| run-${i + 1} | ${40 + i * 3} | ${12 + i} | ${1200 + i * 90} | ${i % 3 ? "warm_cache_prefix_reused_from_slot_0" : "**cold**_start_full_prefill_of_the_context"} |\n`).join("") + "\n",
    Array.from({ length: 5 }, (_, i) => `Paragraph ${i + 1}: the remaining cost is mostly *prefill* on the local lane, which is why the second number barely moves when the cache is warm. If we wanted more, the next lever is the \`chunk_overlap\` setting, but that trades recall for speed and I would not touch it without an eval run first.\n\n`).join(""),
  ].join("");
  const think = Array.from({ length: 10 }, (_, i) => `Considering the ingestion numbers again (${i + 1}): the pool cap, the per-call connect, and the retry curve all compound. `).join("");
  const chunk = (s, n) => { const out = []; for (let i = 0; i < s.length; i += n) out.push(s.slice(i, i + n)); return out; };
  const M = window.__mock;
  M._parts = { answer, think, chunk };
  M.scenarios.smooth = () => {
    const f = [{ payload: { type: "session" }, afterMs: 60 }];
    chunk(think, 30).forEach((c) => f.push({ payload: { type: "thinking_delta", text: c }, afterMs: 45 }));
    f.push({ payload: { type: "thinking_done" }, afterMs: 40 });
    chunk(answer, 24).forEach((c, i) => f.push({ payload: { type: "delta", text: c }, afterMs: i % 30 === 29 ? 700 : 35 + (i % 4) * 10 }));
    f.push({ payload: { type: "turn_complete" }, afterMs: 60 });
    f.push({ payload: { type: "done", title: "Smooth probe" }, afterMs: 1200 });
    return f;
  };
  // Interaction scenario: text, a SLOW tool call (tap window), then a long answer.
  M.scenarios.tapper = () => {
    const f = [{ payload: { type: "session" }, afterMs: 60 }];
    chunk(think, 40).forEach((c) => f.push({ payload: { type: "thinking_delta", text: c }, afterMs: 40 }));
    f.push({ payload: { type: "thinking_done" }, afterMs: 40 });
    chunk("Let me check the run telemetry first.\n\n", 20).forEach((c) => f.push({ payload: { type: "delta", text: c }, afterMs: 40 }));
    f.push({ payload: { type: "tool_call", tool: "runs_summary", args: { days: 7, group_by: "job" } }, afterMs: 200, tag: "tool" });
    f.push({ payload: { type: "tool_result", is_error: false, digest: Array.from({ length: 30 }, (_, i) => `job-${i}: ${i * 3} runs, ${i}% failed`).join("\n"), chars: 900 }, afterMs: 4000 });
    chunk(answer, 24).forEach((c) => f.push({ payload: { type: "delta", text: c }, afterMs: 40 }));
    f.push({ payload: { type: "turn_complete" }, afterMs: 60 });
    f.push({ payload: { type: "done", title: "Tap probe" }, afterMs: 1200 });
    return f;
  };
})();
(() => {
  const M = window.__mock, base = M.scenarios.tapper(), sm = M.scenarios.smooth();
  const { answer, think, chunk } = M._parts;
  const prose = M._prose().join("\n\n");
  const pre = [];
  for (let i = 0; i < prose.length; i += 40) pre.push({ payload: { type: "delta", text: prose.slice(i, i + 40) }, afterMs: 8 });
  // tapper2: a long prose answer first so the log overflows before the tool pill appears.
  M.scenarios.tapper2 = () => { const f = base.map((x) => ({ ...x, payload: { ...x.payload } }));
    const k = f.findIndex((x) => x.payload.type === "tool_call"); f.splice(k, 0, ...pre.map((x) => ({ ...x, payload: { ...x.payload } }))); return f; };
  // thinker (step 4, D1): the log overflows first, THEN a slow thinking segment
  // streams (~4 s) — the strip's header is on screen and tappable mid-stream.
  M.scenarios.thinker = () => { const f = [{ payload: { type: "session" }, afterMs: 60 }];
    pre.forEach((x) => f.push({ payload: { ...x.payload }, afterMs: x.afterMs }));
    chunk(think, 12).forEach((c) => f.push({ payload: { type: "thinking_delta", text: c }, afterMs: 40 }));
    f.push({ payload: { type: "thinking_done" }, afterMs: 40 });
    chunk(answer, 24).forEach((c) => f.push({ payload: { type: "delta", text: c }, afterMs: 40 }));
    f.push({ payload: { type: "turn_complete" }, afterMs: 60 });
    f.push({ payload: { type: "done", title: "Thinker probe" }, afterMs: 1200 });
    return f; };
  // toggler (step 5, BK-030): the log overflows, then thinking, ONE tool call with a long result and a
  // SHORT final line — the strip and the pill row end up near the bottom, where a close can clamp.
  M.scenarios.toggler = () => { const f = [{ payload: { type: "session" }, afterMs: 60 }];
    pre.forEach((x) => f.push({ payload: { ...x.payload }, afterMs: x.afterMs }));
    chunk(think, 40).forEach((c) => f.push({ payload: { type: "thinking_delta", text: c }, afterMs: 15 }));
    f.push({ payload: { type: "thinking_done" }, afterMs: 40 });
    f.push({ payload: { type: "delta", text: "Checking the runs.\n\n" }, afterMs: 40 });
    f.push({ payload: { type: "tool_call", tool: "runs_summary", args: { days: 7 } }, afterMs: 100 });
    f.push({ payload: { type: "tool_result", is_error: false, chars: 700,
      digest: Array.from({ length: 22 }, (_, i) => `job-${i}: ${i * 3} runs, ${i}% failed`).join("\n") }, afterMs: 300 });
    f.push({ payload: { type: "delta", text: "Done — the summary is above." }, afterMs: 60 });
    f.push({ payload: { type: "turn_complete" }, afterMs: 60 });
    f.push({ payload: { type: "done", title: "Toggle probe" }, afterMs: 400 });
    return f; };
  // longbody (step 6, BK-031): the log overflows, then a LONG thinking segment streamed slowly (~5 s,
  // taller than its preview cap on every device once open), one tool call whose result is 90 lines
  // (taller than the drawer's cap everywhere), a short one, and a short final line.
  M.scenarios.longbody = () => { const f = [{ payload: { type: "session" }, afterMs: 60 }];
    pre.forEach((x) => f.push({ payload: { ...x.payload }, afterMs: x.afterMs }));
    chunk(think.repeat(4), 40).forEach((c) => f.push({ payload: { type: "thinking_delta", text: c }, afterMs: 40 }));
    f.push({ payload: { type: "thinking_done" }, afterMs: 40 });
    f.push({ payload: { type: "delta", text: "Checking the runs.\n\n" }, afterMs: 40 });
    f.push({ payload: { type: "tool_call", tool: "runs_summary", args: { days: 30 } }, afterMs: 100 });
    f.push({ payload: { type: "tool_result", is_error: false, chars: 3000,
      digest: Array.from({ length: 90 }, (_, i) => `job-${i}: ${i * 3} runs, ${i % 17}% failed`).join("\n") }, afterMs: 300 });
    // ...and a second pill in the same row whose drawer is SHORT (no "Show all" offered).
    f.push({ payload: { type: "tool_call", tool: "clock_now", args: {} }, afterMs: 60 });
    f.push({ payload: { type: "tool_result", is_error: false, digest: "12:00", chars: 5 }, afterMs: 100 });
    // A paragraph BELOW the drawer: centring it must leave log to scroll (the wheel-chain check needs a gap).
    chunk("Done — the summary is above. " + answer.slice(answer.lastIndexOf("Paragraph 4")), 40)
      .forEach((c) => f.push({ payload: { type: "delta", text: c }, afterMs: 20 }));
    f.push({ payload: { type: "turn_complete" }, afterMs: 60 });
    f.push({ payload: { type: "done", title: "Long body probe" }, afterMs: 400 });
    return f; };
  // long: the smooth answer x4 (a ~15k-char reply).
  M.scenarios.long = () => { const f = sm.map((x) => ({ ...x, payload: { ...x.payload } }));
    const ds = f.filter((x) => x.payload.type === "delta"); const end = f.findIndex((x) => x.payload.type === "turn_complete");
    const extra = []; for (let r = 0; r < 3; r++) for (const d of ds) extra.push({ payload: { ...d.payload }, afterMs: 25 });
    f.splice(end, 0, ...extra); return f; };
})();
