/* Composer attachments: 📎 opens the native picker (camera + photo library +
   documents — NO capture attribute, which would force camera-only on mobile),
   each pick POSTs the raw blob to /api/media?name=<file> (mirrors how
   dictation.js posts audio) and lands as a removable chip above the composer.
   Photos chip in as thumbnails; documents as icon + filename + size chips.

   The chip pulses while the upload runs, then the stored photo shows via
   same-origin /media/<id> — NOT a blob: object URL: the CSP is img-src 'self'
   and whether 'self' matches blob: is engine-dependent, and both chip states
   must survive iPad Safari + Android Chrome unchanged. Doc chips fill in from
   the local File immediately — docs store raw bytes, so icon/name/size never
   change when the upload lands.

   index.html's accept= is a filter, not a gate: onPick re-checks every file
   (image/* or an allowed doc extension) because pickers still offer "Browse
   all files"; the server sniffs magic bytes as the backstop (415). Soft cap
   MAX_ATTACHMENTS with a gentle inline note; server caps are the backstop
   too (413 over 10 MB images / 20 MB docs). app.js blocks send while
   attachBusy() and attaches attachmentsReady()'s ids to the /api/chat body. */
"use strict";

import { $, api, el } from "./dom.js";
import { addErrorMsg } from "./transcript.js";
import { MEDIA_ID_RE, fmtSize } from "./thumbs.js";

const MAX_ATTACHMENTS = 6;
// index.html's accept= document list, kept in lockstep with
// gateway/media.py _DOC_EXTS (sync pinned by tests/test_conventions).
const DOC_EXT_RE = /\.(?:pdf|docx|xlsx|txt|md)$/i;
const btn = $("#chat-attach");
const fileInput = $("#chat-attach-input");
const bar = $("#attach-bar");
const note = el("span", "attach-note");
note.hidden = true;
bar.appendChild(note);

let items = [];     // { chip, id, mime, url, name, size, doc, done, gone }
let uploading = 0;

export function initAttach() {
  btn.addEventListener("click", () => { if (!uploading) fileInput.click(); });
  fileInput.addEventListener("change", onPick);
}

// Completed uploads — [{id, mime, url, name, size}]. Only the ids ride the
// /api/chat body (app.js maps a.id); name/size feed the optimistic bubble so
// it renders the same doc chip the replay will (thumbs.js).
export function attachmentsReady() {
  return items.filter((it) => it.done).map((it) => (
    { id: it.id, mime: it.mime, url: it.url, name: it.name, size: it.size }
  ));
}

// True while any upload is in flight — app.js refuses to send mid-upload.
export function attachBusy() { return uploading > 0; }

// Once the /api/chat request starts: the bubble takes over the thumbnails.
export function clearAttachments() {
  for (const it of items) { it.gone = true; it.chip.remove(); }
  items = [];
  setNote("");
  sync();
}

async function onPick(e) {
  const files = Array.from(e.target.files || []);
  fileInput.value = ""; // re-picking the same file must fire "change" again
  for (const file of files) {
    if (items.length >= MAX_ATTACHMENTS) { setNote(`up to ${MAX_ATTACHMENTS} attachments per message`); break; }
    const isImg = String(file.type || "").startsWith("image/");
    if (!isImg && !DOC_EXT_RE.test(file.name)) {
      setNote(`${file.name}: photos, PDF, DOCX, XLSX, TXT or MD only`);
      continue;
    }
    addChip(file);
  }
  sync();
}

function addChip(file) {
  const isDoc = !String(file.type || "").startsWith("image/"); // gate already passed
  const chip = el("div", "attach-chip pending" + (isDoc ? " doc" : ""));
  const x = el("button", "attach-x", "✕");
  x.type = "button";
  x.setAttribute("aria-label", `Remove ${file.name}`);
  const item = {
    chip, id: "", mime: "", url: "", name: isDoc ? file.name : "",
    size: file.size, doc: isDoc, done: false, gone: false,
  };
  x.addEventListener("click", () => removeChip(item));
  chip.appendChild(x);
  if (isDoc) {
    const icon = el("span", "doc-icon", "📄");
    icon.setAttribute("aria-hidden", "true");
    chip.appendChild(icon);
    chip.appendChild(el("span", "doc-name", file.name)); // textContent — untrusted
    chip.appendChild(el("span", "doc-size", fmtSize(file.size)));
  }
  bar.insertBefore(chip, note);
  items.push(item);
  upload(item, file);
}

async function upload(item, file) {
  uploading++; sync();
  try {
    // ?name= feeds the doc sidecar (the server sanitizes it); the image
    // branch ignores the parameter. Raw blob, not multipart — the route
    // sniffs magic bytes.
    const r = await api(`/api/media?name=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    if (!MEDIA_ID_RE.test(String(r && r.id || ""))) throw new Error("unexpected media id");
    item.id = r.id; item.mime = r.mime; item.url = `/media/${r.id}`; item.done = true;
    if (!item.gone) {
      if (item.doc) {
        item.name = r.name || file.name; // the sanitized sidecar name — what the bubble shows
      } else {
        const img = el("img");
        img.setAttribute("src", item.url); // same-origin, CSP img-src 'self'
        img.setAttribute("alt", "");
        img.setAttribute("aria-hidden", "true"); // the ✕ carries the accessible name
        item.chip.insertBefore(img, item.chip.firstChild);
      }
      item.chip.classList.remove("pending");
    }
  } catch (err) {
    removeChip(item);
    if (!item.gone) addErrorMsg(`upload failed: ${err.message}`);
  } finally {
    uploading--; sync();
  }
}

function removeChip(item) {
  const i = items.indexOf(item);
  if (i < 0) return;
  items.splice(i, 1);
  item.gone = true; // an in-flight upload may still land — its file is kept, harmless
  item.chip.remove();
  sync();
}

function setNote(msg) {
  note.textContent = msg || "";
  note.hidden = !msg;
  clearTimeout(setNote._t);
  if (msg) setNote._t = setTimeout(() => setNote(""), 4000);
}

function sync() {
  btn.classList.toggle("uploading", uploading > 0);
  btn.disabled = uploading > 0;
  bar.hidden = items.length === 0 && note.hidden;
}
