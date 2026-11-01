/* Approvals: pending cards + the in-chat modal + polling. Field rendering and
   previews live in approval_fields.js (refresh hook wired below); resolved
   history in approval_history.js (re-exported for app.js). */
"use strict";

import { $, api, el, esc, riskBadge } from "./dom.js";
import { ui } from "./state.js";
import { approvalWait } from "./status.js";
import {
  approvalFields, isIrreversible, prunePreviews, setPreviewRefresh,
  sourceLines, watchChip,
} from "./approval_fields.js";

export { loadApprovalsHistory } from "./approval_history.js";

// Preview completions re-render the open modal AND the pending list.
setPreviewRefresh((a) => {
  if (modalApprovalId === a.id)
    $(".modal-fields", $("#approval-modal")).innerHTML = approvalFields(a);
  loadApprovals();
});

/* ---- approvals (pending + in-chat bar) ---- */

export async function loadApprovals() {
  const approvals = await api("/api/approvals").catch(() => []);
  prunePreviews(new Set(approvals.map((a) => a.id)));
  const badge = $("#approvals-badge");
  badge.hidden = approvals.length === 0;
  badge.textContent = approvals.length;
  renderApprovalsList(approvals);
  // Strict equality (not truthiness): a NULL-session approval must never match
  // the on-screen chat, even when sessionId is itself null.
  renderChatApproval(
    approvals.filter((a) => ui.sessionId != null && a.session_id === ui.sessionId)
  );
}

function renderApprovalsList(approvals) {
  const list = $("#approvals-list");
  if (!approvals.length) {
    list.innerHTML = '<div class="empty">No pending approvals.</div>';
    return;
  }
  list.innerHTML = "";
  for (const a of approvals) {
    const card = el("div", "card");
    const title = el("div", "title");
    title.innerHTML = `${riskBadge(a.classification)} ${esc(a.tool)}${watchChip(a)}`;
    const meta = el("div", "meta");
    meta.innerHTML = `${approvalFields(a)}${sourceLines(a)}` +
      `<br>run ${esc(a.run_id || "?")} · ${esc(a.created_at)}`;
    const actions = el("div", "actions");
    for (const [label, approved, cls] of [["Approve", true, ""], ["Deny", false, "deny"]]) {
      const btn = el("button", cls, label);
      btn.onclick = async () => {
        btn.disabled = true;
        await api(`/api/approvals/${a.id}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved }),
        }).catch(() => {});
        loadApprovals();
      };
      actions.appendChild(btn);
    }
    card.append(title, meta, actions);
    list.appendChild(card);
  }
}

let modalApprovalId = null;   // the single approval pop-out

// ALL in-chat approvals route through the one modal — show the oldest pending
// for the on-screen session; close it when none remain.
function renderChatApproval(list) {
  approvalWait(list.length > 0); // BK-041 G1: the row says what the run waits on
  if (list.length) openApprovalModal(list[0], list.length);
  else closeApprovalModal();
}

function openApprovalModal(a, count) {
  if (a.id === modalApprovalId) return; // already open (don't re-wire under a poll)
  modalApprovalId = a.id;
  const modal = $("#approval-modal");
  const outward = isIrreversible(a);
  modal.querySelector(".modal-card").classList.toggle("outward", outward);
  $("#am-title").innerHTML = `${riskBadge(a.classification)} ${esc(a.tool)}${watchChip(a)}` +
    (count > 1 ? `<span class="approval-count">1 of ${count}</span>` : "");
  // Source + why-gated: textContent (both can embed model-written text).
  const src = $("#am-source");
  src.textContent = a.source_label || "";
  src.hidden = !a.source_label;
  const why = $("#am-why");
  why.textContent = a.why_gated || "";
  why.hidden = !a.why_gated;
  const note = $("#am-note");
  note.hidden = !outward;
  if (outward) note.textContent =
    "Outward action — this leaves your account and can’t be undone. Review before confirming.";
  $(".modal-fields", modal).innerHTML = approvalFields(a);
  const cancel = $("#am-cancel");
  const confirm = $("#am-confirm");
  cancel.disabled = confirm.disabled = false;
  confirm.textContent = outward ? "Confirm send" : "Approve";
  confirm.className = outward ? "confirm-send" : "approve-accent";
  cancel.onclick = () => resolveApproval(a.id, false);
  confirm.onclick = () => resolveApproval(a.id, true);
  modal.hidden = false;
  anchorModal();
  cancel.focus(); // safe default
}

function closeApprovalModal() {
  if (modalApprovalId === null) return;
  $("#approval-modal").hidden = true;
  modalApprovalId = null;
}

// Keep the modal inside the visible viewport so the keyboard never hides it.
function anchorModal() {
  const vv = window.visualViewport;
  const modal = $("#approval-modal");
  if (!vv || modal.hidden) return;
  modal.style.height = `${vv.height}px`;
  modal.style.top = `${vv.offsetTop}px`;
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", anchorModal);
  window.visualViewport.addEventListener("scroll", anchorModal);
}
$("#approval-modal").addEventListener("keydown", (e) => {
  if (modalApprovalId === null) return;
  if (e.key === "Escape") { e.preventDefault(); resolveApproval(modalApprovalId, false); }
  else if (e.key === "Tab") { // trap focus between the two buttons
    const f = [$("#am-cancel"), $("#am-confirm")];
    e.preventDefault();
    const i = f.indexOf(document.activeElement);
    f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
  }
});

async function resolveApproval(id, approved) {
  $("#am-cancel").disabled = $("#am-confirm").disabled = true;
  let res = null;
  try {
    res = await api(`/api/approvals/${id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
    });
  } catch (_) { /* 409 already decided elsewhere → re-sync below */ }
  if (res && approved && res.applied === false) {
    alert("Marked approved, but no live run was waiting (the daemon may have " +
      "restarted) — the action did not run. Re-issue the request if needed.");
  }
  loadApprovals();
}

export function pollApprovalsSoon() { setTimeout(loadApprovals, 800); }
setInterval(loadApprovals, 5000);
