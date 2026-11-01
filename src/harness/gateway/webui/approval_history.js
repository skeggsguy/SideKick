/* Approvals history: the resolved-audit-trail cards + load-more paging.
   The "Open chat" button dispatches an `open-session` CustomEvent (handled in
   sessions.js) to avoid a module cycle. */
"use strict";

import { $, el, esc, loadCards, riskBadge } from "./dom.js";
import { approvalFields, sourceLines, watchChip } from "./approval_fields.js";

const STATUS_GLYPH = { approved: "✓", denied: "✗", expired: "⏱" };
const STATUS_COLOR = { approved: "ok", denied: "bad", expired: "text" };
let historyLimit = 50;

export async function loadApprovalsHistory() {
  const list = $("#approvals-history");
  const rows = await loadCards(
    list, `/api/approvals/history?limit=${historyLimit}`,
    historyCard, "No resolved approvals yet.");
  if (!rows || !rows.length) return;
  // Truncation indicator / load-more so the audit trail never silently cuts off.
  if (rows.length >= historyLimit) {
    const more = el("button", "quiet", `Load more (showing ${rows.length})`);
    more.onclick = () => { historyLimit += 50; loadApprovalsHistory(); };
    const wrap = el("div", "actions");
    wrap.appendChild(more);
    list.appendChild(wrap);
  } else {
    list.appendChild(el("div", "empty", `Showing all ${rows.length}.`));
  }
}

function historyCard(a) {
  const card = el("div", "card");
  const title = el("div", "title");
  const glyph = STATUS_GLYPH[a.status] || "·";
  const color = STATUS_COLOR[a.status] || "text";
  // Status carries a glyph (not colour-only) and is never rendered in --dim.
  title.innerHTML = `${riskBadge(a.classification)} ${esc(a.tool)}${watchChip(a)} ` +
    `<span style="color:var(--${color})">${glyph} ${esc(a.status)}</span>`;
  let outcome = "";
  if (a.outcome_is_error === true) outcome = " · result: failed ⚠";
  else if (a.outcome_is_error === false) outcome = " · result: done ✓";
  const meta = el("div", "meta");
  meta.innerHTML = `${approvalFields(a)}${sourceLines(a)}` +
    `<br>resolved ${esc(a.resolved_at || "?")}${esc(outcome)}`;
  card.append(title, meta);
  if (a.session_id) {
    const actions = el("div", "actions");
    const link = el("button", "quiet", "Open chat");
    // sessions.js owns tab switching — signal it without a module cycle.
    link.onclick = () => document.dispatchEvent(
      new CustomEvent("open-session", { detail: a.session_id }));
    actions.appendChild(link);
    card.appendChild(actions);
  }
  return card;
}
