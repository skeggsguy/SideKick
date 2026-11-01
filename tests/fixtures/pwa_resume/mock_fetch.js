/* fetch mock for the PWA-resume probe (tests/fixtures/pwa_resume/).

   Loaded as a CLASSIC script by the generated probe page, so it is in place
   before the deferred module scripts evaluate — nothing fetches before this
   exists. The messages route answers after a DELAY: that delay is the blank-
   frame window the probe measures (the browser gets real paint opportunities
   inside it, exactly like the suspended PWA's real re-fetch).

   MID-CALL STORE: `settled` models WHEN the store receives the in-flight
   assistant message — core/agent.py commits it when the model call RETURNS,
   so while a call is mid-flight (settled=false) the messages route omits it.
   A re-sync in that window has nothing to put back for the partial answer
   already on screen; the probe's no-premature-resync guard relies on this to
   make any early renderSession visibly wipe the head. The probe flips
   `settled` true at run_done (the call returned, the store committed).

   LIVE RUN (pwa-accurate-resume Step 5): `liveRun` models a run the server
   is INSIDE — the per-run event ring (gateway/event_ring.py) plus the two
   projections built on it (gateway/live.py). The TIMING discipline extends
   here: while `liveRun.active` the messages route answers `messagesMid`
   (the in-flight assistant step is NOT in the store, whatever the loop step
   count — persisted rows exist only for model calls that already RETURNED)
   and `live` carries the ring tail joined at the SERVER's persisted
   watermark. Every /messages answer also carries `compactions` (Step 5,
   compaction-checkpoint-bookmark: the reload-seam anchors — a scenario
   sets `compactions` on its `liveRun`; message rows carry `idx`, the
   store idx anchor_idx addresses). `active` is the ONE liveness predicate
   both routes read — they
   can never disagree, mirroring gateway/live.py. When `liveRun` is null the
   old `settled`-flag behavior stands, so every pre-Step-5 scenario is
   unaffected.

   /api/sessions answers `sessions` (the cold-start restore checks the stored
   id against that list, and `sessionsFail` flips it to a 500 for the offline
   relaunch). Everything else on /api/* resolves an empty array — loadApprovals
   etc. are array-consumers and must not throw; responses nobody reads (detach)
   don't care. Non-/api requests pass through to the real fetch. */
window.__mock = {
  delayMs: 150, // the simulated re-fetch duration (probe samples at 40ms)
  calls: [],
  settled: false, // false = a model call is mid-flight: the store lacks the answer
  // /api/sessions body — boot-restore checks the stored id against this list
  // (probe_coldstart.js); the gateway answers the picker with exactly this.
  // A miss here is NOT the deletion verdict: the real list is capped at the
  // 50 most recent rows, so probe_coldstart.js builds full 50-row pages.
  sessions: [],
  sessionsFail: false, // true = the route 500s: the offline-relaunch case
  // GET /api/sessions/{id} (the precise existence check) has three modes:
  // singleRow set → 200 + row (chat exists, off the page); else singleGone →
  // 200 + null (the server's deletion verdict); else 404 "Not Found" — the
  // ABSENT mode, i.e. an agentd predating the route, where a 404 means "no
  // route" and the app must keep the stored id rather than destroy it.
  singleRow: null,
  singleGone: false,
  // ---- LIVE RUN (pwa-accurate-resume Step 5) --------------------------------
  // A scenario installs a scripted live run here (probe_live.js):
  //   { sid, runId, active, persistedSeq, lastSeq, events,
  //     messagesMid, messagesFull }
  // `events` holds the run's stamped ring entries ({seq, run_id, ...payload}
  // — exactly what chat_run.py's emit() stamps once before routing); the
  // scenario's emit() helper appends them, mirroring "written at EMIT, never
  // via the publisher". `persistedSeq` is ChatRun.persisted_seq: the SERVER's
  // watermark — events at or below it are exactly what `messagesMid` covers
  // (a tool-loop run's mid-call state: completed steps persisted, the
  // in-flight step nowhere but the ring). `messagesFull` is what the route
  // answers after the run completes (`live: null` then — the persisted store
  // is the whole truth). null = no live run: the `settled` model above rules.
  liveRun: null,
  // Every GET /api/chat/{sid}/live the app makes, in order: {path, sid,
  // since}. The Step-5 assertions pin WHICH watermark the client resends —
  // `live.seq` (the client's own), never `live.since` (the server's).
  liveCalls: [],
  // The committed transcript (what run_done's refetch must return):
  messages: [
    {
      role: "user",
      text: "PROBE_HEAD question",
      attachments: [],
    },
    {
      role: "assistant",
      text: "PROBE_HEAD partial answer that was streaming when the socket died",
      tool_calls: [],
      tool_results: [],
      // content_blocks is the ordered record renderSession replays from:
      // thinking + tool + text, so the recovered DOM exercises every
      // segment type recovery cares about ("late tool/thinking segments").
      content_blocks: [
        { type: "thinking", thinking: "PROBE_THINKING segment" },
        { type: "tool_use", id: "probe-t1", name: "probe_tool", input: { a: 1 } },
        { type: "text", text: "PROBE_HEAD question answered in full: HEALED_FULL_ANSWER" },
      ],
    },
  ],
  // Mid-call: the assistant turn is not in the store yet (agent.py persists
  // when the model call returns) — a re-sync now would paint the head-less
  // transcript over the on-screen partial answer.
  inflightMessages: [
    {
      role: "user",
      text: "PROBE_HEAD question",
      attachments: [],
    },
  ],
};

(function () {
  const okJson = (body, delayMs) =>
    new Promise((res) =>
      setTimeout(
        () =>
          res({
            ok: true,
            status: 200,
            statusText: "OK (mock)",
            json: () => Promise.resolve(body),
          }),
        delayMs || 0
      )
    );
  const real = window.fetch.bind(window);
  window.fetch = function (path, opts) {
    const p = String(path);
    window.__mock.calls.push(p);
    if (p.indexOf("/messages") !== -1) {
      const m = window.__mock;
      // The live server's shape since pwa-accurate-resume Step 4:
      // {messages, live}. `live` is the run's ring tail joined at the
      // SERVER's persisted watermark (gateway/live.py persisted_tail_
      // projection), or null when nothing is in flight. `liveRun` belongs to
      // ONE session: the real route answers the path's session from the
      // store, so another session's transcript must never see this run.
      const mm = /^\/api\/sessions\/([^/?]+)\/messages(?:\?.*)?$/.exec(p);
      const lr = mm && m.liveRun && m.liveRun.sid === decodeURIComponent(mm[1])
        ? m.liveRun
        : null;
      if (lr) {
        if (!lr.active) {
          // The run completed: its task is done, so the final persist has
          // happened — the store holds the whole answer and `live` is null
          // forever after ("not live" never pairs with a missing answer).
          return okJson(
            {
              messages: lr.messagesFull,
              live: null,
              // Step 5 (compaction-checkpoint-bookmark): the reload seams —
              // [{checkpoint_id, anchor_idx, created_at, est_before,
              //   est_after}], a scripted scenario sets `compactions` on
              //   the run; message rows carry `idx` (store idx) in the
              //   same space anchor_idx addresses.
              compactions: lr.compactions || [],
            },
            m.delayMs
          );
        }
        // MID-CALL, TIMING HONEST: messagesMid never contains the in-flight
        // assistant step (any loop step's rows exist only once that model
        // call RETURNED — agent.py), so live.events is its only source.
        const oldest = lr.events.length ? lr.events[0].seq : null;
        return okJson(
          {
            messages: lr.messagesMid,
            live: {
              since: lr.persistedSeq,
              seq: lr.lastSeq,
              oldest_seq: oldest,
              // `gap` here means the IN-FLIGHT STEP outgrew the ring: the
              // persisted half stays whole, the suffix paints from
              // oldest_seq, run_done heals (the /messages kind of gap).
              gap: oldest !== null && lr.persistedSeq + 1 < oldest,
              events: lr.events.filter((e) => e.seq > lr.persistedSeq),
            },
            compactions: lr.compactions || [],
          },
          m.delayMs
        );
      }
      const messages = m.settled ? m.messages : m.inflightMessages;
      return okJson({ messages, live: null, compactions: m.compactions || [] }, m.delayMs);
    }
    const lm = /^\/api\/chat\/([^/?]+)\/live(?:\?(.*))?$/.exec(p);
    if (lm) {
      // GET /api/chat/{sid}/live?since=N — the CLIENT-watermark resync route.
      // `since` means "I have painted through seq N"; gap follows event_
      // ring.replay's arithmetic (since + 1 < oldest_seq), never seq guesses
      // from the client. A different session's /live is inactive, never a
      // 404 (existence is /api/sessions/{id}'s question).
      const m = window.__mock;
      const sid = decodeURIComponent(lm[1]);
      const params = new URLSearchParams(lm[2] || "");
      // Clamp at 0 like event_ring.replay (`n = max(since, 0)`) — a negative
      // since means "from the beginning", never an inferred gap.
      const since = Math.max(0, Number.parseInt(params.get("since") || "0", 10) || 0);
      m.liveCalls.push({ path: p, sid, since });
      // liveMissing models an agentd that PREDATES the route: a 404 no
      // amount of retrying can fix (the client's sentinel + watchdog-stop
      // branch is what this drives).
      if (m.liveMissing) {
        return Promise.resolve({
          ok: false, status: 404, statusText: "Not Found (mock: agentd predates /live)",
          json: () => Promise.resolve({ detail: "Not Found" }),
        });
      }
      const lr = m.liveRun;
      if (!lr || !lr.active || lr.sid !== sid) return okJson({ active: false }, m.delayMs);
      const oldest = lr.events.length ? lr.events[0].seq : null;
      return okJson(
        {
          active: true,
          seq: lr.lastSeq,
          oldest_seq: oldest,
          gap: oldest !== null && since + 1 < oldest,
          events: lr.events.filter((e) => e.seq > since),
        },
        m.delayMs
      );
    }
    if (/^\/api\/sessions\/[^/]+$/.test(p)) {
      // GET /api/sessions/{id} — the three modes above. "/messages" paths
      // already returned above; POST/PATCH callers are not in the probe.
      const m = window.__mock;
      if (m.singleRow !== null) return okJson(m.singleRow, 0);
      if (m.singleGone) return okJson(null, 0);
      return Promise.resolve({
        ok: false, status: 404, statusText: "Not Found (mock: agentd predates the route)",
        json: () => Promise.resolve({ detail: "Not Found" }),
      });
    }
    if (p === "/api/sessions") {
      // (exact match — the single-session pattern above cannot hit this path)
      const m = window.__mock;
      // sessionsFail models the offline cold start: a non-ok response the
      // gateway would give when agentd is unreachable (fetch REJECTS in the
      // real app; a 500 exercises the same catch path in openSession/boot).
      if (m.sessionsFail) {
        return new Promise((res) => setTimeout(() => res({
          ok: false, status: 500, statusText: "mock down",
          json: () => Promise.resolve({ detail: "mock agentd unreachable" }),
        }), m.delayMs));
      }
      return okJson(m.sessions, 0);
    }
    if (p.indexOf("/api/") === 0) return okJson([], 0);
    return real(path, opts);
  };
})();
