/* Runs browser: the flat run list with deprecated-delegate nesting and the
   per-run trace expander. */
"use strict";

import { $, api, el } from "./dom.js";

function runCard(r, sub) {
  const card = el("div", sub ? "card subrun" : "card");
  const label = r.playbook || r.session_title || r.trigger;
  card.appendChild(el("div", "title", `${sub ? "↳ " : ""}${label} · ${r.status}`));
  card.appendChild(el("div", "meta",
    `${r.started_at} · ${r.model || ""} · $${(r.cost_usd || 0).toFixed(4)} · ` +
    `${r.input_tokens}in/${r.output_tokens}out (cache ${r.cache_read_tokens}r/${r.cache_write_tokens}w)`));
  const btn = el("button", "quiet", "Trace");
  btn.onclick = async () => {
    const existing = $("pre", card);
    if (existing) { existing.remove(); return; }
    const events = await api(`/api/runs/${r.id}/events`).catch(() => []);
    const pre = el("pre", "trace",
      events.map((e) => `${e.ts} ${e.kind} ${JSON.stringify(e.data)}`).join("\n"));
    card.appendChild(pre);
  };
  card.appendChild(btn);
  return card;
}

export async function loadRuns() {
  const runs = await api("/api/runs").catch(() => []);
  const list = $("#runs-list");
  list.innerHTML = runs.length ? "" : '<div class="empty">No runs yet.</div>';
  const subs = new Map();
  const top = [];
  for (const r of runs) {
    /* The `delegate` TOOL was retired 2026-08-11, but the runs table is
       append-only and real rows still carry trigger "delegate:<parent_id>".
       This nests them under their parent in the runs browser — NOT dead
       code; deleting it silently flattens real history. */
    const m = /^delegate:(\w+)$/.exec(r.trigger);
    if (m && runs.some((p) => p.id === m[1])) {
      if (!subs.has(m[1])) subs.set(m[1], []);
      subs.get(m[1]).push(r);
    } else top.push(r);
  }
  for (const r of top) {
    list.appendChild(runCard(r, false));
    for (const s of subs.get(r.id) || []) list.appendChild(runCard(s, true));
  }
}
