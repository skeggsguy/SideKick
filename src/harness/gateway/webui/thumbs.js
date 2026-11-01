/* Thumbnail + chip rows for message attachments inside user bubbles — one
   builder so the LIVE send and the history replay can never render differently.

   Images: same-origin /media/<id> thumbnail (tap → lightbox.js).
   Documents (plan doc-attachments Step 3): DISPLAY-ONLY chips — icon +
   filename + size, no link, no preview. A standalone PWA has no back
   affordance, so a doc tap must never navigate (lightbox.js header), and the CSP is
   untouched (no blob:/data: anywhere; GET /media/<doc> is a download we
   never point at).

   XSS/CSP rules (dom.js header): images never render through markdown (renderMd
   FORBID_TAGS img), and every node is built with el() + setAttribute — no
   string-interpolated innerHTML. src is same-origin /media/<id> (CSP
   img-src 'self'; no data: URIs). The id is the server's content hash —
   gateway/media.py MEDIA_ID_RE (16-hex + ext) — re-checked here before it
   touches src/href, so a malformed attachment is dropped, not followed.
   Filenames are server-sanitized ([A-Za-z0-9._-]) but STILL reach the DOM
   only through el()'s textContent. */
"use strict";

import { el } from "./dom.js";

// gateway/media.py MEDIA_ID_RE as a literal: a 16-hex content hash plus one
// approved extension (images + documents — attach.js guards POST responses
// for both branches). Shared so attach.js and thumbRow() guard src/href the
// same way.
export const MEDIA_ID_RE = /^[0-9a-f]{16}\.(?:jpg|png|webp|gif|pdf|docx|xlsx|txt|md)$/;

// "2.1 MB" for chip size labels. The wire size is the stored file's st_size
// — raw bytes for docs (no re-encode), so attach.js can use file.size while
// the upload is still in flight and both agree with the replay.
export function fmtSize(bytes) {
  const n = Math.max(0, Number(bytes) || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

export function thumbRow(attachments) {
  if (!attachments || !attachments.length) return null;
  const row = el("div", "attach-row");
  row.setAttribute("role", "group");
  row.setAttribute("aria-label", "Attachments");
  for (const a of attachments) {
    if (!a || !MEDIA_ID_RE.test(String(a.id || ""))) continue;
    if (String(a.mime || "").startsWith("image/")) {
      const url = `/media/${a.id}`;
      const link = el("a", "attach-open");
      link.setAttribute("href", url);          // tap opens the full-size file
      link.setAttribute("target", "_blank");   // no-JS fallback — lightbox.js intercepts
      link.setAttribute("rel", "noopener");
      const img = el("img", "attach-thumb");
      img.setAttribute("src", url);
      img.setAttribute("alt", "Attached photo");
      img.setAttribute("loading", "lazy");
      link.appendChild(img);
      row.appendChild(link);
    } else {
      row.appendChild(docChip(a));
    }
  }
  return row.children.length ? row : null;
}

// Last function on purpose: tests/test_conventions pins the region from
// "function docChip" to EOF as link-free (the display-only invariant).
// The server sniffs mime, so "valid id, not an image" is a document here by
// construction — never an unknown type dressed up as a chip.
function docChip(a) {
  const chip = el("span", "doc-chip");
  const icon = el("span", "doc-icon", "📄");
  icon.setAttribute("aria-hidden", "true");
  chip.appendChild(icon);
  chip.appendChild(el("span", "doc-name", a.name || a.id)); // textContent — untrusted
  const size = fmtSize(a.size);
  if (size) chip.appendChild(el("span", "doc-size", size));
  return chip;
}
