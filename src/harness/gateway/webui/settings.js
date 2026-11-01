/* Settings panels: memory, connections, tools security.
   (The Schedules/triggers panel lives in schedules.js.) */

import { $, api, el, esc, riskBadge } from "./dom.js";

/* ---- memory ---- */
// Keep in sync with CATEGORIES in src/harness/memory/store.py.
const FACT_CATEGORIES = [
  "profile", "preference", "project", "contact", "routine",
  "interest", "procedure", "job-config", "ops-lesson",
];

// Inline edit form swapped in place of a fact card's content. Fact text only
// ever enters via `.value` assignment / el() textContent — never innerHTML.
function factEditForm(f, card) {
  const form = el("form", "schedule-form");
  const contentLabel = el("label", "sf-field", "Fact");
  const content = el("textarea");
  content.rows = 2;
  content.value = f.content;
  contentLabel.appendChild(content);
  form.appendChild(contentLabel);
  const catLabel = el("label", "sf-field", "Category");
  const cat = el("select");
  for (const c of FACT_CATEGORIES) {
    const opt = el("option", null, c);
    opt.value = c;
    if (c === f.category) opt.selected = true;
    cat.appendChild(opt);
  }
  catLabel.appendChild(cat);
  form.appendChild(catLabel);
  const actions = el("div", "actions");
  actions.appendChild(el("button", null, "Save"));
  const cancel = el("button", "quiet", "Cancel");
  cancel.type = "button";
  cancel.onclick = () => loadMemory();
  actions.appendChild(cancel);
  form.appendChild(actions);
  form.onsubmit = async (e) => {
    e.preventDefault();
    await api(`/api/memory/facts/${f.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: content.value, category: cat.value }),
    }).catch((err) => alert(err.message));
    loadMemory();
  };
  card.replaceChildren(form);
  content.focus();
}

function factCard(f) {
  const card = el("div", "card");
  card.appendChild(el("div", "title", f.content));
  const recalled = f.last_recalled_at
    ? `recalled ${f.last_recalled_at.slice(0, 10)}` : "never recalled";
  card.appendChild(el("div", "meta",
    `#${f.id} · ${f.category} · confidence ${f.confidence.toFixed(2)}` +
    ` · saved ${f.updated_at.slice(0, 10)} · added ${f.created_at.slice(0, 10)}` +
    ` · ${recalled}${f.active ? "" : " · forgotten"}`));
  if (f.active) {
    const actions = el("div", "actions");
    const edit = el("button", "quiet", "Edit");
    edit.onclick = () => factEditForm(f, card);
    actions.appendChild(edit);
    const btn = el("button", "quiet", "Forget");
    btn.onclick = async () => {
      await api(`/api/memory/facts/${f.id}/forget`, { method: "POST" }).catch(() => {});
      loadMemory();
    };
    actions.appendChild(btn);
    card.appendChild(actions);
  }
  return card;
}

export async function loadMemory() {
  const list = $("#memory-list");
  const q = $("#memory-q").value.trim();
  const url = q
    ? `/api/memory/facts?q=${encodeURIComponent(q)}`
    : `/api/memory/facts?include_inactive=${$("#memory-all").checked}`;
  let facts;
  try { facts = await api(url); }
  catch (err) { list.innerHTML = `<div class="empty">Memory unavailable: ${esc(err.message)}</div>`; return; }
  list.innerHTML = facts.length ? "" : '<div class="empty">No facts remembered yet.</div>';
  for (const f of facts) list.appendChild(factCard(f));
}

$("#memory-form").addEventListener("submit", (e) => { e.preventDefault(); loadMemory(); });
$("#memory-all").addEventListener("change", loadMemory);

$("#memory-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const content = $("#memory-add-content").value.trim();
  const note = $("#memory-add-note");
  if (!content) return;
  let res;
  try {
    res = await api("/api/memory/facts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, category: $("#memory-add-category").value }),
    });
  } catch (err) { note.textContent = err.message; return; }
  note.textContent = res.created ? "" : "Already known — refreshed the existing fact.";
  $("#memory-add-content").value = "";
  if (res.created) $("#memory-add-acc").open = false;
  loadMemory();
});

/* ---- contact details (identity refs for delegated browser fills) ---- */
// Values enter the DOM via `.value` only (never innerHTML) and are stored
// server-side; the assistant never sees them until it fills an approved form.
export async function loadContacts() {
  const note = $("#contacts-note");
  note.textContent = "";
  let refs;
  try { refs = await api("/api/client/refs"); }
  catch (err) { note.textContent = `Couldn't load: ${err.message}`; return; }
  $("#contact-email").value = refs.email || "";
  $("#contact-phone").value = refs.phone || "";
}

$("#contacts-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const note = $("#contacts-note");
  try {
    await api("/api/client/refs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("#contact-email").value.trim(),
        phone: $("#contact-phone").value.trim(),
      }),
    });
    note.textContent = "Saved.";
  } catch (err) { note.textContent = err.message; }
});

/* ---- connections (per-scope transparency; whole-vendor reconnect) ---- */
const G = "https://www.googleapis.com/auth/";
// `tier` describes the BREADTH of what the token can reach — NOT danger. Danger
// (whether an action runs without asking) is the policy engine's job; outward
// tools always prompt regardless of scope. Tiers get their own palette so they
// don't read as the action-risk badges used elsewhere.
const SCOPE_INFO = {
  [G + "gmail.modify"]: {
    svc: "Gmail", tier: ["Broad access", "broad"],
    cap: "Read all mail, manage drafts, label, and send",
    tools: "gmail_read, gmail_search, gmail_draft_create, gmail_send…",
    warn: "Sending always asks you first (and can never be made automatic).",
  },
  [G + "drive"]: {
    svc: "Drive", tier: ["Broad access", "broad"],
    cap: "Read, upload, move, rename and modify all Drive files",
    tools: "drive_list, drive_read, drive_upload, drive_update_content…",
    warn: "Uploads/changes run through tools that still ask you first.",
  },
  [G + "calendar"]: {
    svc: "Calendar", tier: ["Medium access", "med"],
    cap: "Read and write your calendars and events",
    tools: "calendar_list_events, calendar_event_create…",
  },
  [G + "documents"]: {
    svc: "Docs", tier: ["Medium access", "med"],
    cap: "Read and edit your Google Docs",
    tools: "docs_create, docs_append_text…",
  },
  [G + "spreadsheets"]: {
    svc: "Sheets", tier: ["Medium access", "med"],
    cap: "Read and edit your Google Sheets",
    tools: "sheets_read, sheets_update…",
  },
  [G + "tasks"]: {
    svc: "Tasks", tier: ["Limited access", "low"],
    cap: "Read and manage (create / complete) your task lists",
    tools: "tasks_list, task_create, task_complete…",
  },
};
function scopeInfo(scope) {
  return SCOPE_INFO[scope] ||
    { svc: scope, tier: ["Unrecognized scope", "unknown"], cap: "Other access granted by the provider", tools: "" };
}

function hashParams() {
  const q = location.hash.indexOf("?");
  return new URLSearchParams(q >= 0 ? location.hash.slice(q + 1) : "");
}

export async function loadConnections() {
  const list = $("#connections-list");
  const params = hashParams();
  let vendors;
  try { vendors = await api("/api/oauth/status"); }
  catch (err) { list.innerHTML = `<div class="empty">Connections unavailable: ${esc(err.message)}</div>`; return; }
  list.innerHTML = "";

  const ex = el("div", "explainer",
    "A granted scope is what Sidekick's token can reach — not permission to act. " +
    "Every outward action (e.g. sending mail) still asks you first, enforced by the " +
    "policy engine, independent of this token.");
  list.appendChild(ex);

  if (params.get("connected"))
    list.appendChild(el("div", "empty", `Connected ${params.get("connected")} ✓`));
  else if (params.get("error"))
    list.appendChild(el("div", "empty", `Connection failed: ${params.get("error")}`));

  for (const v of vendors) {
    const card = el("div", "card");
    const state = !v.enabled ? "paused"
      : v.connected ? "connected ✓"
      : (v.missing && v.missing.length && v.scopes && v.scopes.length) ? "needs re-auth"
      : "not connected";
    card.appendChild(el("div", "title", `${v.label} · ${state}`));
    const metaBits = [];
    if (v.scopes && v.scopes.length) metaBits.push(`${v.scopes.length} scope(s) granted`);
    else metaBits.push("no token");
    if (v.last_refresh) metaBits.push(`last refreshed ${v.last_refresh}`);
    if (v.auto_refreshing) metaBits.push("stays connected (auto-refreshes)");
    card.appendChild(el("div", "meta", metaBits.join(" · ")));

    const scopeRow = (scope, granted) => {
      const info = scopeInfo(scope);
      const row = el("div", "scope-row" + (granted ? "" : " missing"));
      const h = el("div", "sc-head");
      h.innerHTML = `<b>${esc(info.svc)}</b> <span class="scope-tier ${info.tier[1]}">${esc(info.tier[0])}</span>` +
        (granted ? "" : ` <span class="sc-missing">not yet granted</span>`);
      row.appendChild(h);
      row.appendChild(el("div", "sc-cap", info.cap));
      if (info.tools) row.appendChild(el("div", "sc-tools", `Tools: ${info.tools}`));
      if (info.warn) row.appendChild(el("div", "sc-warn", `⚠ ${info.warn}`));
      return row;
    };

    if ((v.scopes && v.scopes.length) || (v.missing && v.missing.length)) {
      const det = el("details", "conn");
      det.appendChild(el("summary", null, "What this connection can access"));
      const wrap = el("div", "conn-scopes");
      for (const scope of v.scopes || []) wrap.appendChild(scopeRow(scope, true));
      // Show what re-authorizing would ADD, before the consent screen.
      for (const scope of v.missing || []) wrap.appendChild(scopeRow(scope, false));
      det.appendChild(wrap);
      card.appendChild(det);
    }

    if (v.enabled) {
      const actions = el("div", "actions");
      const reAuth = state === "needs re-auth";
      const btn = el("button", null,
        reAuth ? `Re-authorize${v.missing && v.missing.length ? ` — ${v.missing.length} new` : ""}`
          : v.connected ? "Reconnect" : "Connect");
      btn.onclick = () => { location.href = `/api/oauth/${v.key}/start`; };
      actions.appendChild(btn);
      if (v.key === "google" && v.connected) {
        const manage = el("a", "manage-link", "Manage / revoke at Google ↗");
        manage.href = "https://myaccount.google.com/permissions";
        manage.target = "_blank";
        manage.rel = "noopener";
        actions.appendChild(manage);
      }
      card.appendChild(actions);
    }
    list.appendChild(card);
  }
}

/* ---- tools security ---- */
// Actionable tiers first; READ_ONLY (the long, inert list) is folded away below.
const TIERS = [
  ["MUTATING_LOCAL", "Local changes", "Asks each time; you can grant a standing “always allow”."],
  ["OUTWARD", "Outward (sends / external)", "Always asks — by design (can’t be made automatic; prompt-injection guard)."],
  ["DESTRUCTIVE", "Destructive", "Always asks; blocked entirely in unattended runs."],
];

function renderToolRow(t) {
  const row = el("div", "tool-row");
  const meta = el("div", "tool-meta");
  meta.appendChild(el("div", "tn", t.name));
  meta.appendChild(el("div", "td", t.description || ""));
  if (t.remote) meta.appendChild(el("div", "net", "↗ remote (runs on an external server)"));
  row.appendChild(meta);
  if (t.allowlistable) {
    if (t.currently_allowlisted) {
      row.appendChild(el("span", "granted", "✓ always allowed"));
      const rm = el("button", "quiet icon", "×");
      rm.setAttribute("aria-label", `Revoke standing approval for ${t.name}`);
      rm.title = "Revoke standing approval";
      rm.onclick = async () => {
        await api(`/api/allowlist/${encodeURIComponent(t.name)}`, { method: "DELETE" }).catch(() => {});
        loadTools();
      };
      row.appendChild(rm);
    } else {
      const add = el("button", "quiet", "Always allow");
      add.onclick = async () => {
        if (!confirm(`Always allow “${t.name}”? Sidekick will run it without asking — ` +
          `including in unattended scheduled runs — until you revoke it here.`)) return;
        await api("/api/allowlist", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tool: t.name }),
        }).catch((e) => alert(e.message));
        loadTools();
      };
      row.appendChild(add);
    }
  } else {
    // Stated fact, not a disabled control to "unlock". Classification-keyed:
    // READ_ONLY auto-runs (the old one-size "Always asks" label here flatly
    // contradicted the read-only fold summary). always_ask (schedule
    // self-edits) overrides its class — keep in sync with policy.py.
    const LOCKED_LABEL = {
      READ_ONLY: ["locked auto", "Auto-runs — read-only"],
      OUTWARD: ["locked", "Always asks — by design"],
      DESTRUCTIVE: ["locked", "Always asks — never runs unattended"],
    };
    const [cls, label] = t.always_ask
      ? ["locked", "Always asks — schedule self-edits are always reviewed"]
      : LOCKED_LABEL[t.classification] || ["locked", "Always asks"];
    row.appendChild(el("span", cls, label));
  }
  return row;
}

export async function loadTools() {
  const list = $("#tools-list");
  let data;
  try { data = await api("/api/tools"); }
  catch (err) { list.innerHTML = `<div class="empty">Tools unavailable: ${esc(err.message)}</div>`; return; }
  list.innerHTML = "";
  list.appendChild(el("div", "explainer",
    "Every tool is classified in code — you can’t change a tool’s category here. " +
    "You can only grant a standing “always allow” to local-change tools. Sending, " +
    "external and destructive actions ALWAYS ask first and can never be made automatic."));

  for (const [cls, label, note] of TIERS) {
    // Always-ask tools get their own tier below — never mixed into their
    // nominal class, where "you can grant always allow" would misdescribe them.
    const group = data.tools.filter((t) => t.classification === cls && !t.always_ask);
    if (!group.length) continue;
    const card = el("div", "card tier-group");
    const head = el("div", "tier-head");
    head.innerHTML = `${riskBadge(cls)} <b>${esc(label)}</b> <span class="tier-note">${esc(note)}</span>`;
    card.appendChild(head);
    for (const t of group) card.appendChild(renderToolRow(t));
    list.appendChild(card);
  }

  // Schedule self-edits: a tier of their own, like Outward and Destructive —
  // never auto-approved in any mode (but never blocked in background runs
  // either; they park as approvals so jobs can propose rescheduling).
  const alwaysAsk = data.tools.filter((t) => t.always_ask);
  if (alwaysAsk.length) {
    const card = el("div", "card tier-group");
    const head = el("div", "tier-head");
    head.innerHTML = `<span class="risk always">always asks</span> <b>Schedule changes</b> ` +
      `<span class="tier-note">${esc("Sidekick editing its own future jobs — and their permissions. " +
        "Always asks, in every mode; background runs pause these as approvals instead of being blocked.")}</span>`;
    card.appendChild(head);
    for (const t of alwaysAsk) card.appendChild(renderToolRow(t));
    list.appendChild(card);
  }

  // READ_ONLY: auto-runs, no controls — fold it away so the actionable tiers
  // stay above the fold.
  const ro = data.tools.filter((t) => t.classification === "READ_ONLY");
  if (ro.length) {
    const card = el("div", "card tier-group");
    const det = el("details");
    det.appendChild(el("summary", null, `Read-only · ${ro.length} tools · auto-runs, never asks`));
    det.appendChild(el("div", "tier-note",
      "One exception: outbound web tools (web_fetch, web_search, …) ask first " +
      "when an egress guard trips — after Sidekick reads a secret-shaped file " +
      "(keys, tokens, credentials) or probes a private/internal address that " +
      "turn, past the per-turn budget of outbound calls, or on the first visit " +
      "to a site you never named and no search surfaced. Anti-exfiltration " +
      "guards; the approval card says which one fired."));
    for (const t of ro) det.appendChild(renderToolRow(t));
    card.appendChild(det);
    list.appendChild(card);
  }

  if (data.orphans && data.orphans.length) {
    const card = el("div", "card");
    card.appendChild(el("div", "title", "Orphaned grants"));
    card.appendChild(el("div", "meta", "Standing approvals for tools no longer in the catalog."));
    for (const name of data.orphans) {
      const row = el("div", "tool-row");
      row.appendChild(el("div", "tool-meta", name));
      const rm = el("button", "quiet", "Remove");
      rm.onclick = async () => {
        await api(`/api/allowlist/${encodeURIComponent(name)}`, { method: "DELETE" }).catch(() => {});
        loadTools();
      };
      row.appendChild(rm);
      card.appendChild(row);
    }
    list.appendChild(card);
  }
}
