/* Session picker: list/select/rename, openSession (deep links + push +
   approvals-history all route through it), and the #session=<id> hash. */
"use strict";

import { $, api, el } from "./dom.js";
import { ui } from "./state.js";
import { stop as statusStop } from "./status.js";
import { renderSession } from "./transcript.js";

export function updateRenameBtn() { $("#rename-btn").hidden = !ui.sessionId; }

// The `pickerOpenNew` effect body (BK-019 fx dictionary, design §5.1): a
// brand-new chat just BECAME a session (the stream's `session` frame) — put it
// in the picker NOW with a "New chat…" label so it isn't a blank select while
// we stream, select it, refresh the rename affordance, and remember it: the
// chat the user is reading is the one a kill/relaunch must reopen (sessions.js
// owns that pointer). Bound by BOTH transports' fx maps (dispatch totality).
export function showNewChatOption(id) {
  const sel = $("#session-select");
  if (!sel.querySelector(`option[value="${id}"]`)) {
    const o = el("option", null, "New chat…");
    o.value = id;
    // Insert at index 1, right after "+ New chat": a fresh session is the
    // newest by updated_at, and appending at the bottom made it jump to the
    // top when loadSessions() rebuilt the sorted list.
    sel.insertBefore(o, sel.children[1] || null);
  }
  sel.value = id;
  updateRenameBtn();
  rememberLastChat(id);
}

/* ---- last open chat (cold-start restore; plan pwa-accurate-resume) ----
   iOS kills backgrounded PWAs; a relaunch that lands on the greeting loses
   the chat being read. One localStorage slot: written wherever a session
   OPENS (openSession — deep link/push/boot-restore —, the picker, and app.js
   when a brand-new chat gets its id), cleared when the user deliberately
   picks "+ New chat", so leaving on the new-chat screen stays left there.
   Wrapped like theme.js/picker.js for private mode. */
const LAST_CHAT_KEY = "lastChat";
export function rememberLastChat(id) {
  try {
    if (id) localStorage.setItem(LAST_CHAT_KEY, id);
    else localStorage.removeItem(LAST_CHAT_KEY);
  } catch (_) { /* private mode */ }
}
export function lastChatId() {
  try { return localStorage.getItem(LAST_CHAT_KEY); } catch (_) { return null; }
}

// Boot-restore existence check (app.js): the picker list is capped at the 50
// most recent rows, so a miss there is not a deletion. THREE states, hence
// raw fetch instead of api() (which collapses ok/not-ok into a throw):
//   object    — the row: session exists (title/channel render the picker option)
//   null      — the server ANSWERED "deleted" (200 + null; see resources.py)
//   undefined — the server could not answer (offline, or an agentd predating
//               the route — its 404 means "no route", not "no session"):
//               callers must keep the stored id.
export async function fetchSession(id) {
  try {
    const r = await fetch(`/api/sessions/${encodeURIComponent(id)}`);
    if (!r.ok) return undefined;
    return await r.json();
  } catch (_) { return undefined; }
}

// preloaded (optional): a list the caller already fetched — boot-restore
// checks the stored id against it, then renders THAT fetch instead of
// refetching (loadSessions is otherwise the picker's only builder).
export async function loadSessions(preloaded) {
  const sessions = preloaded ?? await api("/api/sessions").catch(() => []);
  const sel = $("#session-select");
  const current = ui.sessionId || "";
  // The fetched page is capped at the 50 most recent rows, so the chat the
  // user is READING can be missing from it (boot-restore of an off-page id,
  // whose option app.js prepends, and any deep link to an old chat). Rebuild
  // would then select "+ New chat" under an open transcript. Keep the option
  // the select already shows for the current chat; its label stays live
  // because title events relabel the node in place.
  const prev = current ? [...sel.options].find((o) => o.value === current) : null;
  // Build off-DOM, swap atomically: innerHTML = "" then re-append would show a
  // blank dropdown mid-rebuild when it's open (flicker). The selected flag set
  // below carries over with the nodes.
  const frag = document.createDocumentFragment();
  const blank = el("option", null, "+ New chat");
  blank.value = "";
  frag.appendChild(blank);
  if (prev && !sessions.some((s) => s.id === current)) {
    const keep = el("option", null, prev.textContent);
    keep.value = current;
    keep.selected = true;
    frag.appendChild(keep); // index 1, like app.js's off-page restore prepend
  }
  for (const s of sessions) {
    // An active session with no title is the chat streaming right now —
    // "New chat…" beats "Untitled" while its name is still being generated.
    const label = s.title || (s.active ? "New chat…" : "Untitled");
    // Drop the redundant "· web" suffix (everything here is web); keep it only
    // for cli/scheduled sessions where the channel is informative.
    const o = el("option", null, s.channel === "web" ? label : `${label} · ${s.channel}`);
    o.value = s.id;                // attribute set via property — safe
    if (s.id === current) o.selected = true;
    frag.appendChild(o);
  }
  sel.replaceChildren(frag);
  updateRenameBtn();
  return sessions; // the visible handler reads the `active` flags
}

$("#session-select").addEventListener("change", (e) => {
  ui.sessionId = e.target.value || null;
  rememberLastChat(e.target.value); // "" = "+ New chat": deliberately a blank slate
  statusStop(); // the row describes the OLD session's turn, if any
  updateRenameBtn();
  renderSession(ui.sessionId);
});

$("#rename-btn").addEventListener("click", async () => {
  if (!ui.sessionId) return;
  // Prefill with the current title so it's an edit, not a blind retype.
  let cur = ($("#session-select").selectedOptions[0]?.textContent || "").replace(/ · [^·]+$/, "");
  if (cur === "Untitled" || cur === "+ New chat" || cur === "New chat…") cur = "";
  const t = prompt("Rename chat:", cur);
  if (t === null) return;
  const title = t.trim();
  if (!title) return;
  try {
    await api(`/api/sessions/${ui.sessionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    // An off-page current chat has an option but no fetched row: loadSessions
    // carries the option's TEXT across rebuilds, so refresh it here (keeping
    // any " · channel" suffix) or the picker would show the old title.
    const opt = [...$("#session-select").options].find((o) => o.value === ui.sessionId);
    if (opt) opt.textContent = title + (/ · [^·]+$/.exec(opt.textContent) || [""])[0];
  } catch (err) { alert(err.message); }
  loadSessions();
});

export async function openSession(id, preloaded) {
  ui.sessionId = id;
  rememberLastChat(id); // any open — deep link, push, toast, boot-restore — is "last"
  statusStop(); // navigating in from a push/history link: clear any stale row
  $('[data-tab="chat"]').click();
  await loadSessions(preloaded);
  $("#session-select").value = id;
  updateRenameBtn();
  await renderSession(id);
}
// Approvals history's "Open chat" buttons signal via this event (no cycle).
document.addEventListener("open-session", (e) => openSession(e.detail));

export function sessionIdFromHash(hash) {
  const m = /#session=([0-9a-f]+)/.exec(hash || "");
  return m ? m[1] : null;
}
