/* Streaming markdown renderer for the live agent bubble (BK-028; design:
   scratch/plans/frontend-smoothness-step3-wizard.md).

   Every reveal frame renders renderMd(settle(prefix)) — the SAME marked →
   DOMPurify path as everything else, no second sanitizer — and MORPHS the
   result into the live `.md`: a top-level node whose rendered HTML did not
   change since the last frame is left alone (selection, scrollLeft and a
   user-opened <details> survive), the node that did change keeps its common
   prefix (text grows by appendData). The final render is renderMd(raw) with
   no settling, so a finished bubble is byte-identical to a history replay.

   settle() works on the markdown TEXT, always before sanitizing: it holds
   back an unterminated last line that would re-type the block above it on
   its next character (`#`, `1.`, `-`, a fence line, a table header awaiting
   its delimiter row) and closes dangling inline markers on the last line
   only. It never synthesizes an href and never repairs `_`, `~~` or `***`.

   Module rules: imports only renderMd; never follow.js/transcript.js (the
   keepRead bracket stays at the call sites); no rAF (the jsunit tier runs
   this module directly). State lives ON the .md element (_last, _shownStr),
   so resetTranscript's wipe clears it with the element. */

import { renderMd } from "./dom.js";

const FENCE_SO_FAR = /^\s*(`{1,2}|~{1,2})$/;     // a fence opener still arriving
const FENCE_LINE = /^\s*(`{3,}|~{3,})[^\n]*$/;   // waits for its newline: P→PRE, language-p→language-py
// An unterminated last line matching one of these re-types the block on its
// next character, so it waits for that character (or its newline).
const HOLD_LINE = [
  /^\s*#{1,6}\s*$/,            // "#" → P becomes [P, H1]
  /^\s*\d{1,9}[.)]?\s*$/,      // "1" / "1." → OL; a lazy "2" joins the item above
  /^\s*[-*+_=][-*+_= ]*$/,     // bullet, hr, setext underline, a "**" / "__" opener
  FENCE_SO_FAR,
  FENCE_LINE,
  /^\s*<(\/?[A-Za-z!][^>\n]*)?$/, // a raw-HTML tag still arriving — "< 5 ms" is prose, not held
];
const FENCE = /^\s*(`{3,}|~{3,})/;
const PIPE = /^ {0,3}\|/;                  // only a line that STARTS with a pipe can be held
const DELIM_SO_FAR = /^\s*\|?[\s:|-]*$/;   // a delimiter row still arriving (or not begun)
const isDelimRow = (l) => /^[\s:|-]+$/.test(l) && l.includes("-") && l.includes("|");

// The markdown actually rendered for a streamed prefix.
export function settle(text) {
  let s = text;
  const c = s.charCodeAt(s.length - 1);
  if (c >= 0xd800 && c <= 0xdbff) s = s.slice(0, -1); // half an emoji renders as U+FFFD
  const lines = s.split("\n");
  const n = lines.length - 1;   // the unterminated last line ("" right after a newline)
  const cut = s.length - lines[n].length;
  const line = lines[n];
  let fences = 0;
  for (let i = 0; i < n; i++) if (FENCE.test(lines[i])) fences++;
  // Inside code: no repair, and only a closer line (or its first backticks) waits.
  if (fences % 2) return FENCE_SO_FAR.test(line) || FENCE_LINE.test(line) ? s.slice(0, cut) : s;
  if (HOLD_LINE.some((re) => re.test(line))) return s.slice(0, cut);
  const held = tableHold(lines, n, cut);
  if (held !== -1) return s.slice(0, held);
  return s.slice(0, cut) + repairLine(line);
}

// A GFM table re-types a paragraph only once its delimiter row arrives, so a
// header (plus the partial delimiter under it) waits — never once the tail
// block since the last blank line holds a complete delimiter row, and never
// a run of 3+ pipe lines (that is text, not a table being typed).
function tableHold(lines, n, cut) {
  let top = n;
  while (top > 0 && lines[top - 1].trim() !== "") top--;
  for (let k = top + 1; k < n; k++) if (isDelimRow(lines[k]) && lines[k - 1].includes("|")) return -1;
  const pipe = (i) => i >= top && PIPE.test(lines[i]);
  if (pipe(n - 1) && DELIM_SO_FAR.test(lines[n]) && !pipe(n - 2)) return cut - lines[n - 1].length - 1;
  if (pipe(n) && !pipe(n - 1)) return cut;
  return -1;
}

// Inline repair of the last line: (a) a partial link shows as its text, (b)
// a trailing marker run waits for what follows it, then the still-open code
// span and emphasis are closed, innermost first, before trailing whitespace
// (a closer after a space is not right-flanking and would render raw).
function repairLine(line) {
  const s = line.replace(/\[([^\]\n]*)\]\([^)\s]*$/, "$1").replace(/(?<!\\)[*_`~]+$/, "");
  const close = closers(s);
  return close ? s.replace(/\s*$/, (ws) => close + ws) : s;
}

function closers(s) {
  let code = false, emph = true;
  const open = [];
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "\\") { i++; continue; }
    let j = i;
    while (s[j] === ch) j++;
    const len = j - i;
    if (ch === "`") { if (len === 1) code = !code; }        // `` runs are never repaired
    else if (ch === "*" && !code) {
      if (len >= 3) emph = false;                           // *** is never repaired
      else {
        const tok = len === 2 ? "**" : "*", before = s[i - 1], after = s[j];
        const at = open.lastIndexOf(tok);
        if (at !== -1 && before && !/\s/.test(before)) open.length = at; // closes it
        else if (after && !/\s/.test(after) && (len === 2 || !before || /[\s(]/.test(before))) {
          open.push(tok); // single * only after start/space/( — spares 2*3 and a * b
        }
      }
    }
    i = j - 1;
  }
  return (code ? "`" : "") + (emph ? open.reverse().join("") : "");
}

// Inert parse target: template content never runs scripts or fetches, and
// only renderMd output is ever assigned to it.
const tpl = document.createElement("template");
const ser = (node) => (node.nodeType === 1 ? node.outerHTML : `#${node.nodeType}${node.data}`);

// Render `text` into the live bubble `md`. `final` renders it as-is (the
// finished answer: exactly renderMd(raw)); otherwise it is a streamed prefix.
export function streamRender(md, text, final = false) {
  const shown = final ? text : settle(text);
  if (shown === md._shownStr) return; // a held line grew: nothing new to show
  md._shownStr = shown;
  const html = renderMd(shown);
  if (!md.firstChild) { // history replay, or a first frame: today's write, byte for byte
    md.innerHTML = html;
    md._last = Array.from(md.childNodes, ser);
    return;
  }
  tpl.innerHTML = html;
  const fresh = Array.from(tpl.content.childNodes); // static: moving a node mutates the live list
  const live = Array.from(md.childNodes);
  const last = md._last || [];
  const next = fresh.map(ser);
  // Compare with the previous FRAME's serializations, never the live DOM:
  // a <details> the reader opened must not snap shut on the next frame.
  for (let i = 0; i < fresh.length; i++) {
    if (i >= live.length) md.appendChild(fresh[i]);
    else if (next[i] !== last[i]) morphNode(live[i], fresh[i]);
  }
  for (let i = fresh.length; i < live.length; i++) live[i].remove();
  md._last = next;
}

// Make `a` equal `b`, keeping as much of `a` as possible. Never wraps or
// re-parents: the bubble's children are exactly renderMd's top-level nodes.
function morphNode(a, b) {
  if (a.nodeType === 3 && b.nodeType === 3) { morphText(a, b.data); return; }
  if (a.nodeType !== 1 || b.nodeType !== 1 || a.tagName !== b.tagName || !sameAttrs(a, b)) {
    a.replaceWith(b);
    return;
  }
  const ak = Array.from(a.childNodes), bk = Array.from(b.childNodes);
  for (let i = 0; i < bk.length; i++) {
    if (i >= ak.length) a.appendChild(bk[i]);
    else if (!ak[i].isEqualNode(bk[i])) morphNode(ak[i], bk[i]);
  }
  for (let i = bk.length; i < ak.length; i++) ak[i].remove();
}

// Keep the common prefix (ranges and selection inside it survive), replace
// only the rest: a growing text node is a pure appendData.
function morphText(t, s) {
  const o = t.data;
  if (o === s) return;
  let k = 0;
  const m = Math.min(o.length, s.length);
  while (k < m && o.charCodeAt(k) === s.charCodeAt(k)) k++;
  if (k < o.length) t.deleteData(k, o.length - k);
  if (k < s.length) t.appendData(s.slice(k));
}

function sameAttrs(a, b) {
  if (a.attributes.length !== b.attributes.length) return false;
  for (const at of a.attributes) if (b.getAttribute(at.name) !== at.value) return false;
  return true;
}
