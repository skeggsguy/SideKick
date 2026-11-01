/* Approval field rendering + live previews.

   XSS contract: every innerHTML sink here interpolates untrusted values only
   through esc() (via argRow()/genericArgRows()/sourceLines()/riskBadge()).
   Preview completion re-renders through the refresh hook injected by
   approvals.js — this module never touches the modal or the poll directly. */
"use strict";

import { api, esc } from "./dom.js";

// One labeled arg value, collapsed behind <details> when long — strings AND
// serialized objects (a sheets_update values matrix must never dump raw).
// Every untrusted value passes through esc() — the contract all three
// consumers (pending cards, modal, history) rely on.
function argRow(k, v) {
  const isObj = v !== null && typeof v === "object";
  const s = isObj ? JSON.stringify(v, null, 2) : String(v);
  if (s.length > 600)
    return `<div><b>${esc(k)}:</b></div><details class="arg-long">` +
      `<summary>${esc(s.slice(0, 200))}… (${s.length} chars)</summary>` +
      `<pre class="arg-pre">${esc(s)}</pre></details>`;
  if (isObj)
    return `<div><b>${esc(k)}:</b></div><pre class="arg-pre">${esc(s)}</pre>`;
  return `<div><b>${esc(k)}:</b> ${esc(s)}</div>`;
}

// Generic fallback for tools without a hand-written template below: labeled
// key/value rows instead of a raw JSON dump.
function genericArgRows(args) {
  if (!args || typeof args !== "object" || !Object.keys(args).length)
    return '<div class="empty">No arguments.</div>';
  return Object.entries(args).map(([k, v]) => argRow(k, v)).join("");
}

// Source + why-gated lines shared by the pending and history cards (the modal
// uses textContent on its own elements instead). Both strings can embed
// model-written text (trigger labels) — esc() only, text position only.
function sourceLines(a) {
  return (a.source_label ? `<div class="src">${esc(a.source_label)}</div>` : "") +
    (a.why_gated ? `<div class="why">${esc(a.why_gated)}</div>` : "");
}

// Amber cue for approvals instigated by watched content (e.g. an incoming
// email) rather than Tom — the attacker-influenceable case.
const watchChip = (a) =>
  a.source_kind === "watch" ? ' <span class="chip-watch">email-triggered</span>' : "";

function approvalFields(a) {
  const { tool, args } = a;
  const row = (k, v) => `<div><b>${esc(k)}:</b> ${esc(v)}</div>`;
  const loadingLine = (what) => `<div class="empty">Loading ${esc(what)}…</div>`;
  const warnLine = (what, app) =>
    `<div class="empty">⚠ Couldn’t load ${esc(what)} — review in ${esc(app)} before approving.</div>`;
  const attach = Array.isArray(args.attachments) && args.attachments.length
    ? row("Attachments", args.attachments.map((x) => (x.split ? x.split("/").pop() : x)).join(", "))
    : "";

  // Live preview for resolver-backed tools: `previewable` comes from the API
  // (gateway/preview.py is the single source of truth — never hardcode tool
  // names here). History rows carry no flag, so they keep plain id rows (the
  // preview route only serves pending approvals; old targets may be gone).
  let p = null;
  if (a.previewable && (!a.status || a.status === "pending")) {
    ensurePreview(a);
    p = previews.get(a.id);
  }
  const d = p && p.state === "ok" ? p.data : null;

  if (tool === "gmail_send" || tool === "gmail_draft_create")
    return row("To", args.to) + row("Subject", args.subject) + row("Body", args.body) + attach;
  if (tool === "gmail_send_draft") {
    // The args carry only an opaque draft_id — show the real recipients/
    // subject/body so a send is never approved content-blind.
    if (d)
      return row("To", d.to) + (d.cc ? row("Cc", d.cc) : "") + row("Subject", d.subject) +
        row("Body", d.body) +
        (Array.isArray(d.attachments) && d.attachments.length
          ? row("Attachments", d.attachments.join(", ")) : "");
    const base = row("Draft", args.draft_id);
    if (p && p.state === "loading") return base + loadingLine("draft contents");
    if (p && p.state === "err") return base + warnLine("the draft", "Gmail");
    return base;
  }
  if (tool === "gmail_trash_message") {
    // Opaque message_id — show the real sender/subject/date so a deletion is
    // never approved content-blind.
    if (d)
      return row("From", d.from) + row("Subject", d.subject || "(no subject)") +
        row("Date", d.date) + (d.snippet ? row("Preview", d.snippet) : "");
    const base = row("Message", args.message_id);
    if (p && p.state === "loading") return base + loadingLine("email details");
    if (p && p.state === "err") return base + warnLine("the email", "Gmail");
    return base;
  }
  if (tool === "gmail_draft_reply") {
    // The recipient is derived from the original message server-side — the
    // resolved "Replying to" line is the consequential fact args never show.
    let head;
    if (d) head = row("Replying to", `${d.replying_to} — ${d.subject || "(no subject)"}`);
    else if (p && p.state === "loading") head = row("Reply to", args.message_id) + loadingLine("original message");
    else if (p && p.state === "err") head = row("Reply to", args.message_id) + warnLine("the original message", "Gmail");
    else head = row("Reply to", args.message_id);
    return head + row("Body", args.body) + attach;
  }
  const calRow = args.calendar_id && args.calendar_id !== "primary"
    ? row("Calendar", args.calendar_id) : "";
  if (tool === "calendar_event_create")
    return row("Event", args.summary) + row("Start", args.start) + row("End", args.end) +
      (args.with_meet ? row("Meet", "Google Meet link will be attached") : "") + calRow;
  if (tool === "calendar_event_update") {
    // All change-fields are optional — the id + resolved current state must
    // always render, or a description-only edit shows blank rows.
    let out = row("Event id", args.event_id);
    if (d) out += row("Currently", `${d.current_title} · ${d.current_start} – ${d.current_end}`);
    else if (p && p.state === "loading") out += loadingLine("current event");
    else if (p && p.state === "err") out += warnLine("the current event", "Calendar");
    for (const [k, label] of [["summary", "New title"], ["start", "New start"],
      ["end", "New end"], ["location", "New location"], ["description", "New description"]])
      if (args[k]) out += argRow(label, args[k]);
    if (args.meet === "add" || args.meet === "remove")
      out += argRow("Meet", args.meet === "add" ? "Add Google Meet link" : "Remove Google Meet link");
    return out + calRow;
  }
  if (tool === "task_create")
    return row("Task", args.title) + (args.due ? row("Due", args.due) : "");
  if (tool === "task_update" || tool === "task_complete") {
    let out = row("Task id", args.task_id);
    if (d) out += d.note ? row("Note", d.note)
      : row("Task", `${d.title}${d.list ? ` (${d.list})` : ""}`) +
        (d.due ? row("Due", d.due) : "") + (d.status ? row("Status", d.status) : "");
    else if (p && p.state === "loading") out += loadingLine("task details");
    else if (p && p.state === "err") out += warnLine("the task", "Tasks");
    for (const [k, label] of [["title", "New title"], ["notes", "New notes"], ["due", "New due"]])
      if (args[k]) out += argRow(label, args[k]);
    return out;
  }
  if (tool === "edit_file") {
    return row("File", args.path) +
      (args.old_str ? argRow("Replace", args.old_str)
        : '<div class="empty">New file (nothing replaced).</div>') +
      argRow("With", args.new_str);
  }
  if (tool === "schedule_add")
    return row("Job", args.label || "(unlabeled)") + row("Cron", args.cron) +
      (args.one_shot ? row("One-shot", "yes") : "") +
      row("Autonomy", args.autonomy || "supervised") +
      (Array.isArray(args.tools) && args.tools.length ? row("Tools", args.tools.join(", ")) : "") +
      argRow("Prompt", args.prompt);
  if (tool === "schedule_edit" || tool === "schedule_delete" || tool === "schedule_disable") {
    // Opaque trigger_id — resolve which job this touches. Autonomy flips and
    // tool allocations are the security-relevant fields: always visible.
    let out = row("Job id", args.trigger_id);
    if (d) out += d.note ? row("Note", d.note)
      : row("Job", `${d.label} · cron ${d.cron}`) +
        row("Currently", `${d.autonomy}${d.enabled ? "" : " · disabled"}`) +
        (d.tools ? row("Tools", d.tools) : "") +
        (d.prompt ? argRow("Prompt", d.prompt) : "");
    else if (p && p.state === "loading") out += loadingLine("job details");
    else if (p && p.state === "err") out += warnLine("the job", "Settings → Schedules");
    if (tool === "schedule_edit") {
      for (const [k, label] of [["cron", "New cron"], ["label", "New label"],
        ["prompt", "New prompt"], ["one_shot", "New one-shot"]])
        if (args[k] !== undefined && args[k] !== null) out += argRow(label, args[k]);
      if (args.autonomy) out += row("New autonomy", args.autonomy);
      if (args.tools !== undefined && args.tools !== null)
        out += row("New tools", Array.isArray(args.tools) && args.tools.length
          ? args.tools.join(", ") : "(cleared)");
    }
    return out;
  }
  if (tool === "remember")
    return row("Remember", args.content) + (args.category ? row("Category", args.category) : "");
  if (tool === "forget") {
    // The args are bare fact ids — show what each fact actually says so a
    // batch forget (memory_hygiene) isn't approved content-blind.
    const ids = Array.isArray(args.fact_ids) ? args.fact_ids : [];
    if (a.status && a.status !== "pending") return row("Forget ids", ids.join(", "));
    ensureFactPreview(a);
    const fp = factPreviews.get(a.id);
    if (!fp || fp.state !== "ok") return row("Forget", `${ids.length} fact(s): ${ids.join(", ")}`);
    return ids.map((id) =>
      row(`Forget #${id}`, fp.byId.has(String(id)) ? fp.byId.get(String(id)) : "(not an active fact)")
    ).join("");
  }
  // Any other resolver-backed tool (drive/docs/sheets family): resolved facts
  // first — what the opaque id actually names — then the raw args.
  if (a.previewable) {
    if (d) {
      const rows = Object.entries(d)
        .filter(([, v]) => v !== "" && v !== null && v !== undefined)
        .map(([k, v]) => row(k.replace(/_/g, " "), String(v))).join("");
      return rows + genericArgRows(args);
    }
    if (p && p.state === "loading") return loadingLine("details") + genericArgRows(args);
    if (p && p.state === "err") return warnLine("details", "the app") + genericArgRows(args);
  }
  return genericArgRows(args);
}

// Fact contents for `forget` approvals, keyed by approval id.
const factPreviews = new Map();

function ensureFactPreview(a) {
  if (factPreviews.has(a.id)) return;
  factPreviews.set(a.id, { state: "loading" });
  api("/api/memory/facts")
    .then((facts) => {
      const byId = new Map(facts.map((f) => [String(f.id), f.content]));
      factPreviews.set(a.id, { state: "ok", byId });
    })
    .catch(() => factPreviews.set(a.id, { state: "err" }))
    .then(() => {
      refresh(a);
    });
}

// Content previews for resolver-backed tools, keyed by approval id — the
// endpoint dispatches per tool (see gateway/preview.py). Which approvals are
// previewable arrives on the row itself (`a.previewable`).
const previews = new Map();

function ensurePreview(a) {
  if (previews.has(a.id)) return;
  previews.set(a.id, { state: "loading" });
  api(`/api/approvals/${a.id}/preview`)
    .then((d) => previews.set(a.id, { state: "ok", data: d }))
    .catch(() => previews.set(a.id, { state: "err" }))
    .then(() => {
      // Re-render both surfaces now the content (or the failure) is known.
      refresh(a);
    });
}

// Set by approvals.js: called after a preview resolves so both surfaces
// (modal + pending list) re-render with the fetched content.
let refresh = () => {};
export function setPreviewRefresh(fn) { refresh = fn; }

export function prunePreviews(pendingIds) {
  for (const cache of [previews, factPreviews])
    for (const id of cache.keys())
      if (!pendingIds.has(id)) cache.delete(id);
}

const isIrreversible = (a) => a.classification === "OUTWARD" || a.classification === "DESTRUCTIVE";

export { approvalFields, sourceLines, watchChip, isIrreversible };
