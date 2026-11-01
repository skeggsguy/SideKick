/* Schedules panel: full management of cron jobs (create / edit / delete /
   fire / enable), per-job autonomy and tool allocation.

   Security invariants: prompts, labels, tool names and cron strings reach the
   DOM only via el()/textContent (prompt bodies via renderMd); autonomy +
   tools choices are validated server-side too (the form is a convenience,
   not the guard). */

import { $, api, el, esc, loadCards, renderMd, riskBadge } from "./dom.js";
import { expectRun } from "./state.js";

const TIER_ORDER = ["MUTATING_LOCAL", "OUTWARD", "READ_ONLY", "DESTRUCTIVE"];

let editingId = null;   // null ⇒ the form creates; else PATCHes this id
let pendingEdit = null; // trigger handed to openForm before the accordion opens
let toolCatalog = null; // cached /api/tools list (null until a load SUCCEEDS)
let templates = null;   // cached /api/playbooks list (null until success)

export async function loadTriggers() {
  await loadCards($("#triggers-list"), "/api/triggers", triggerCard, "No schedules yet.");
}

function triggerCard(t) {
  const card = el("div", "card");
  card.classList.toggle("off", !t.enabled);  // muted when disabled
  // Prefer the short label; else truncate so an inline reminder's full
  // prompt (stored in playbook) doesn't become a runaway title.
  const title = t.label || (t.playbook.length <= 60 ? t.playbook : t.playbook.slice(0, 57) + "...");
  const head = el("div", "title", title);
  if (t.autonomy === "autonomous") {
    head.appendChild(el("span", "autonomy-badge auto", "autonomous"));
  }
  card.appendChild(head);
  card.appendChild(el("div", "meta",
    `${t.kind} ${t.spec} · ${t.enabled ? "enabled" : "disabled"}` +
    `${t.one_shot ? " · one-shot" : ""}` +
    `${t.enabled && t.next_fire_at ? ` · next ${t.next_fire_at}` : ""}` +
    ` · last fired ${t.last_fired_at || "never"}`));
  if (t.effective_tools && t.effective_tools.length) {
    const chips = el("div", "tool-chips");
    for (const name of t.effective_tools) chips.appendChild(el("span", "tool-chip", name));
    if (t.autonomy !== "autonomous") {
      chips.appendChild(el("span", "tool-chip more", "+ discoverable"));
    }
    card.appendChild(chips);
  }
  const det = el("details", "trigger-detail");
  det.appendChild(el("summary", null,
    t.playbook_name ? `Instructions — playbook: ${t.playbook_name}` : "Instructions"));
  const body = el("div", "trigger-prompt");
  body.innerHTML = renderMd(t.prompt); // marked → DOMPurify
  det.appendChild(body);
  card.appendChild(det);

  const actions = el("div", "actions");
  const fire = el("button", null, "Fire now");
  fire.onclick = async () => {
    fire.disabled = true;
    expectRun();
    try { await api(`/api/triggers/${t.id}/fire`, { method: "POST" }); fire.textContent = "Fired ✓"; }
    catch (_) { fire.textContent = "Failed"; }
    setTimeout(() => { fire.textContent = "Fire now"; fire.disabled = false; }, 2000);
  };
  actions.appendChild(fire);

  const edit = el("button", "quiet", "Edit");
  edit.onclick = () => openForm(t);
  actions.appendChild(edit);

  const spent = t.one_shot && t.last_fired_at;  // a fired one-shot
  const toggle = el("button", "quiet", t.enabled ? "Disable" : (spent ? "Re-arm" : "Enable"));
  if (!t.enabled && spent) {
    toggle.title = "One-shot already fired — re-arming will fire it again on the next cron match.";
  }
  toggle.onclick = async () => {
    toggle.disabled = true;
    await api(`/api/triggers/${t.id}/${t.enabled ? "disable" : "enable"}`, { method: "POST" }).catch(() => {});
    loadTriggers();
  };
  actions.appendChild(toggle);

  const del = el("button", "quiet danger", "Delete");
  del.onclick = async () => {
    if (!confirm(`Delete “${title}” permanently? This cannot be undone (Disable keeps it).`)) return;
    await api(`/api/triggers/${t.id}`, { method: "DELETE" }).catch((e) => alert(e.message));
    loadTriggers();
  };
  actions.appendChild(del);
  card.appendChild(actions);
  return card;
}

/* ---- create / edit form (accordion) ---- */

/* Failed loads are NOT cached — a fetch that races a daemon restart must not
   leave the pickers permanently empty until a hard reload. null means "retry
   next open"; errors are returned so the form can show them. */
async function ensureFormData() {
  const errors = [];
  if (!toolCatalog) {
    try { toolCatalog = (await api("/api/tools")).tools; }
    catch (e) { errors.push(`tool catalog unavailable (${e.message})`); }
  }
  if (!templates) {
    try { templates = await api("/api/playbooks"); }
    catch (e) { errors.push(`templates unavailable (${e.message})`); }
  }
  const sel = $("#schedule-template");
  if (templates && sel.options.length <= 1) {
    for (const p of templates) {
      const opt = el("option", null, p.name);
      opt.value = p.name;
      sel.appendChild(opt);
    }
  }
  return errors;
}

function selectedAutonomy() {
  return $("#schedule-form input[name='autonomy']:checked").value;
}

function selectedTools() {
  return [...document.querySelectorAll("#schedule-tools input:checked")].map((c) => c.value);
}

/* The picker groups the catalog by classification with the shared risk
   badges; non-selectable rows stay honest about what auto-runs (only
   allowlisted local changes do). */
function renderToolPicker(selected) {
  const wrap = $("#schedule-tools");
  wrap.innerHTML = "";
  if (!toolCatalog || !toolCatalog.length) {
    wrap.appendChild(el("div", "empty",
      "Tool catalog unavailable — close and reopen this form to retry."));
    return;
  }
  const chosen = new Set(selected || []);
  for (const cls of TIER_ORDER) {
    const group = toolCatalog.filter((t) => t.classification === cls);
    if (!group.length) continue;
    const box = el("div", "tp-group");
    const head = el("div", "tp-head");
    head.innerHTML = riskBadge(cls);
    box.appendChild(head);
    for (const t of group) {
      const label = el("label", "tp-row");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = t.name;
      cb.checked = chosen.has(t.name);
      label.appendChild(cb);
      const name = el("span", "tn", t.name);
      label.appendChild(name);
      if (t.currently_allowlisted && cls === "MUTATING_LOCAL" && !t.always_ask) {
        label.appendChild(el("span", "granted", "✓ always allowed"));
      }
      box.appendChild(label);
    }
    wrap.appendChild(box);
  }
}

function setAutonomyNote() {
  const auto = selectedAutonomy() === "autonomous";
  $("#schedule-autonomy-note").textContent = auto
    ? "Runs its checked tools without asking when they're on your always-allow " +
      "list; sends and schedule changes still ask. It gets EXACTLY the tools " +
      "checked below — nothing else, and it can't discover more mid-run."
    : "Pauses every change for your approval (recommended). May discover extra " +
      "tools mid-run — each gated call still asks you.";
  $("#schedule-tools-required").hidden = !auto;
}

/* openForm: hand the trigger (or null) over and make sure the accordion is
   open — the actual field fill happens in initForm via the toggle event, so
   opening by tapping the summary and opening via an Edit button share one
   code path. */
function openForm(t) {
  pendingEdit = t;
  const acc = $("#schedule-acc");
  if (acc.open) initForm(t);
  else acc.open = true;  // fires the toggle listener → initForm
  acc.scrollIntoView({ block: "start", behavior: "smooth" });
}

async function initForm(t) {
  editingId = t ? t.id : null;
  $("#schedule-acc-title").textContent = t ? `Edit schedule — ${t.label || t.id}` : "＋ New schedule";
  const errors = await ensureFormData();
  $("#schedule-form-error").textContent = errors.join("; ");
  $("#schedule-label").value = (t && t.label) || "";
  $("#schedule-template").value = "";
  $("#schedule-prompt").value = t ? t.prompt : "";
  $("#schedule-cron").value = t ? t.spec : "";
  $("#schedule-oneshot").checked = !!(t && t.one_shot);
  const autonomy = (t && t.autonomy) === "autonomous" ? "autonomous" : "supervised";
  $(`#schedule-form input[name='autonomy'][value='${autonomy}']`).checked = true;
  setAutonomyNote();
  renderToolPicker(t ? t.tools : []);
  cronPreview();
}

function closeForm() {
  $("#schedule-acc").open = false;  // toggle listener clears editing state
}

let cronTimer = null;
function cronPreview() {
  clearTimeout(cronTimer);
  cronTimer = setTimeout(async () => {
    const spec = $("#schedule-cron").value.trim();
    const out = $("#schedule-cron-next");
    if (!spec) { out.textContent = ""; return; }
    try {
      const r = await api(`/api/cron/next?spec=${encodeURIComponent(spec)}`);
      out.textContent = `next: ${r.next_fire_at}`;
      out.classList.remove("bad");
    } catch (_) {
      out.textContent = "invalid cron expression";
      out.classList.add("bad");
    }
  }, 350);
}

export function wireScheduleForm() {
  const acc = $("#schedule-acc");
  acc.addEventListener("toggle", () => {
    if (acc.open) { initForm(pendingEdit); }
    else {
      pendingEdit = null;
      editingId = null;
      $("#schedule-acc-title").textContent = "＋ New schedule";
      $("#schedule-form-error").textContent = "";
    }
  });
  $("#schedule-cancel").onclick = closeForm;
  $("#schedule-cron").addEventListener("input", cronPreview);
  $("#schedule-template").addEventListener("change", () => {
    const pb = (templates || []).find((p) => p.name === $("#schedule-template").value);
    if (!pb) return;
    // Template = creation-time copy: fill the prompt and pre-check its tools.
    $("#schedule-prompt").value = pb.prompt;
    if (!$("#schedule-label").value) $("#schedule-label").value = pb.name.replace(/_/g, " ");
    renderToolPicker(pb.tools || []);
  });
  for (const r of document.querySelectorAll("#schedule-form input[name='autonomy']")) {
    r.addEventListener("change", () => { setAutonomyNote(); renderToolPicker(selectedTools()); });
  }
  $("#schedule-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      prompt: $("#schedule-prompt").value.trim(),
      cron: $("#schedule-cron").value.trim(),
      label: $("#schedule-label").value.trim() || null,
      one_shot: $("#schedule-oneshot").checked,
      autonomy: selectedAutonomy(),
      tools: selectedTools(),
    };
    if (!body.prompt || !body.cron) { alert("Prompt and cron are both required."); return; }
    const err = $("#schedule-form-error");
    err.textContent = "";
    try {
      if (editingId) {
        await api(`/api/triggers/${editingId}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...body, clear_label: body.label === null }),
        });
      } else {
        await api("/api/triggers", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      closeForm();
      loadTriggers();
    } catch (e2) {
      err.textContent = e2.message;
    }
  });
}
