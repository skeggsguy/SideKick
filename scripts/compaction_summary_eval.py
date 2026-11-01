"""Manual eval: compaction summary QUALITY on captured real middles.

Fixtures (--fixture PATH, default the parrot):
- parrot_20261022.json — the designed worst case: the persisted parrot scored
  against the transcript of its real middle (store idx 57-126).
- normal_20261022.json — the follow-up measurement (2026-10-22): compaction
  #1's routine middle (idx 30-56, same session). Its persisted summary is
  unrecoverable (one checkpoint row per session, overwritten), so its
  baseline row is a labelled placeholder, not a measurement — this fixture
  gates only the live candidate. Measured once live: shape-clean, 93.3%
  grounded (all misses tokenization), band OUT at 1,883 tok against a small
  middle's band-max target — the fixed band MIN is a garbage detector, and a
  concise-but-sufficient summary of a small middle is not garbage (human
  sufficiency read; tripwire T1 not tripped).

Offline (default): score the fixture's persisted summary — for the parrot,
the 19,660-char "summary" opening `TOOL RESULT: [fetched page is untrusted...`
with Wikipedia-shaped prose — against the transcript the product's
`render_transcript` builds from the fixture's real middle. Deterministic,
no network, no DB.

--live: rebuild the compaction provider as composition builds it (same
model, COMPACTION_MAX_TOKENS, lane, effort, and the Step-4 answer reserve —
answer_headroom=SUMMARY_BAND_MAX), call the product's `summarize_middle`
ONCE, scored identically. Default effort mirrors the product (low); --effort
overrides for diagnostics only (e.g. re-running the off-effort baseline).
The server is SHARED with live chat (-np 1): check the slot is idle; one run
per verdict.

Predicates are MEASUREMENT ONLY — they classify a finished output for this
table, gating nothing in production (Tom 2026-10-22: no guards in src/):
  1. transcript markers ("TOOL RESULT:", "ASSISTANT CALLED", "USER:", the
     untrusted-data banner) — per-marker + total counts;
  2. verbatim coverage vs the rendered transcript: % in copied runs >=
     MIN_RUN chars + longest run, plus a word-normalized twin (>= MIN_WORDS
     words, case/punctuation-insensitive). Fixture: longest 114c / 16w —
     the prose is NOT copied from the transcript the summarizer saw: it is
     ungrounded in it (confabulated, or recited from the lost PREV SUMMARY),
  3. groundedness: % of unique fact-shaped tokens (urls, paths, domains,
     numbers >= FACT_MIN chars) appearing verbatim in the transcript —
     parrot 14.3% of 84 (Suez url/dates in, Black_hole url out; Step 3's
     render moved this from 13.1% — noise, not signal). Seeing factuality
     needs a human: that read belongs to Step 4,
  4. structure: GOALS / DECISIONS / FACTS / OPEN WORK / PREFERENCES as
     their own line (a heading), not merely mentioned,
  5. length vs band + usage tokens (thinking vs text) + wall + stop_reason.

ENDS-CLEAN, wizard-ratified 2026-10-22 (the criterion's meaning — do not
drift): FAIL iff stop_reason == "max_tokens" — the actual failure mode the
criterion guards (production precedent agent.py:181; the wire carries it,
anthropic_provider.py:383 -> core/types.py:95). PASS iff stop_reason is a
non-truncating value AND the human faithfulness read judges the ending
semantically complete — end_turn does not prove completeness; the human read
is the adjudicator, as it is for the groundedness floor. If the fork omits
stop_reason (None on the wire), the char-class flag (mid_sentence, kept as a
warning column) triggers the human look but NEVER auto-fails alone. The
machine verdict is therefore FAIL(max_tokens) or HUMAN; it never prints PASS.
The offline parrot row has no stop_reason: proxy-only, a baseline, not a
gate sample.

The Step-4 gate is ABSOLUTE, not a delta (the off-effort failure shape is
nondeterministic): markers == 0, sections >= 4, runs <= 200c AND <= 150w
(no "fix" that cures confabulation by copying), groundedness >= 80, in-band,
ends clean per the ruling above. GROUNDEDNESS FLOOR = 80 (the Step-4 human
faithfulness read, 2026-10-22): the gate sample is factually 100% grounded
at a 94.5% measure — its whole gap is unit-suffix tokenization ($117/ vs
$117), not factuality — while the confabulating parrot sits at 14.3, so 80
fails confabulation by >5x and keeps ~15 pts of margin for honest
restatement (the denominator is the middle only: opening-held facts and
paraphrase read as ungrounded). Any floor in (14.3, 94.5] preserves every
recorded verdict; this number is the build record for the decisions entry.
Parrot fails: 4 mk, 0 sect.

Composition (offline too, measurement only): the rendered chars by role,
which of them the cap drops, and the tool-result share of what the summarizer
reads. Step 3's garbage-in half, on this fixture — before: an 86,611-char
render, the tail-only cap dropped 26,611 chars of it (3,135 dialogue) and the
summarizer read 76.8% tool-result prose; after: 41,007 chars, nothing dropped,
48.4% tool result.

Run with: uv run python scripts/compaction_summary_eval.py [--live]
          [--effort off|low|medium|xhigh] [--fixture PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from harness.core.context import estimate_tokens
from harness.core.summarize import (
    CHARS_PER_TOKEN,
    SUMMARY_BAND_MAX,
    SUMMARY_BAND_MIN,
    TRANSCRIPT_CHARS,
    elision_split,
    render_pieces,
    render_transcript,
    summarize_middle,
    summary_target_tokens,
)
from harness.core.types import Usage
from harness.store.sessions import _message_from_dict
from harness.tools._extract import UNTRUSTED_NOTE

FIXTURE_PATH = (Path(__file__).resolve().parent.parent
                / "tests/fixtures/compaction/parrot_20261022.json")

# The five sections the summarizer prompt names as the structure to retain.
SECTIONS = ("GOALS", "DECISIONS", "FACTS", "OPEN WORK", "PREFERENCES")
# Role/format markers: a summary repeating them echoes the transcript.
MARKERS = ("TOOL RESULT:", "ASSISTANT CALLED", "USER:", UNTRUSTED_NOTE)
MIN_RUN = 80  # chars: a copied run this short is a phrase, not a recital
MIN_WORDS = 8  # words: a word run this short is a shared idiom, not one
FACT_MIN = 3  # chars: under this a number is too weak a signature to credit
SENTENCE_END = tuple('.!?)\'"') + ("\u201d",)  # . ! ? ) ' " ”
# Fact-shaped tokens: urls, multi-segment paths, domains, numbers.
FACT_TOKEN = re.compile(
    r"https?://[^\s)\]]+|(?:/[\w.\-]+){2,}|\d[\d.,:/%-]*"
    r"|\b[\w\-]+\.(?:com|org|net|edu|gov|io)\b"
)
FACT_STRIP = ".,:;()%<>[]\"'-"


@dataclass
class Fixture:
    middle: list  # list[Message], store idx 57-126 of the live session
    prev_summary: str | None
    parrot: str
    trace: dict
    checkpoint: dict
    # The persisted-summary row's label. A second fixture whose baseline is
    # NOT the parrot must not print the parrot's name (fixture honesty).
    baseline_label: str = "captured parrot (2026-10-22 off)"


def load_fixture(path: Path = FIXTURE_PATH) -> Fixture:
    fx = json.loads(Path(path).read_text())
    return Fixture(
        middle=[_message_from_dict(r["payload"]) for r in fx["middle"]],
        prev_summary=fx["prev_summary"],
        parrot=fx["parrot_summary"],
        trace=fx["trace"],
        checkpoint=fx["checkpoint"],
        baseline_label=fx.get("_meta", {}).get(
            "baseline_label", "captured parrot (2026-10-22 off)"
        ),
    )


# --- the summarizer's INPUT: what the cap keeps, and what it drops ---------


#: The three kinds of piece `render_pieces` emits, plus the bucket anything
#: else lands in (it should stay empty — a new piece shape shows up here).
CATEGORIES = ("dialogue", "tool-call", "tool-result", "other")
_PIECE_PREFIXES = (
    ("dialogue", ("USER: ", "ASSISTANT: ")),
    ("tool-call", ("ASSISTANT CALLED ",)),
    ("tool-result", ("TOOL RESULT: ",)),
)


def category(piece: str) -> str:
    for name, prefixes in _PIECE_PREFIXES:
        if piece.startswith(prefixes):
            return name
    return "other"


@dataclass
class Composition:
    """Where the rendered transcript's chars come from and which ones the cap
    removes. The 2026-10-22 failure was an INPUT failure: the tail-only cap
    dropped the head that held the conversation and kept fetched-page prose,
    so "what did the summarizer read" is the number that matters."""

    rendered: int  # uncapped chars (piece chars + joiners)
    kept: int  # chars the summarizer reads (elision marker included)
    joiners: int  # the "\n" between pieces, inside `rendered`
    head_kept: int
    tail_kept: int
    elided: int  # the marker's count: transcript chars between the two keeps
    pieces: dict[str, int] = field(default_factory=dict)  # count per category
    chars: dict[str, int] = field(default_factory=dict)  # uncapped chars
    dropped: dict[str, int] = field(default_factory=dict)  # of those, removed

    # `elided` and `sum(dropped.values())` differ by the joiners that fell
    # inside the elided span: the marker counts transcript chars, `dropped`
    # attributes PIECE chars, which is what the shares are computed from.

    @property
    def kept_chars(self) -> dict[str, int]:
        return {k: self.chars[k] - self.dropped[k] for k in CATEGORIES}

    def share(self, basis: dict[str, int]) -> dict[str, float]:
        total = sum(basis.values())
        return {k: (100.0 * v / total if total else 0.0) for k, v in basis.items()}


def composition(middle: list) -> Composition:
    """Attribute the cap's drops to the roles that caused them, on the real
    middle — built from the product's own pieces and keep-split, never from a
    second copy of the renderer."""
    pieces = render_pieces(middle)
    full = render_transcript(middle, char_cap=None)
    capped = render_transcript(middle)
    head, tail = elision_split(TRANSCRIPT_CHARS)
    lo, hi = head, len(full) - tail  # the elided span inside `full`
    counts = dict.fromkeys(CATEGORIES, 0)
    chars = dict.fromkeys(CATEGORIES, 0)
    dropped = dict.fromkeys(CATEGORIES, 0)
    pos = 0
    for p in pieces:
        start, end = pos, pos + len(p)
        pos = end + 1  # the joiner
        name = category(p)
        counts[name] += 1
        chars[name] += len(p)
        dropped[name] += max(0, min(end, hi) - max(start, lo))
    elided = len(full) - head - tail if len(full) > TRANSCRIPT_CHARS else 0
    return Composition(
        rendered=len(full),
        kept=len(capped),
        joiners=len(pieces) - 1,
        head_kept=head,
        tail_kept=tail,
        elided=elided,
        pieces=counts,
        chars=chars,
        dropped=dropped,
    )


def _fmt_shares(shares: dict[str, float]) -> str:
    return "  ".join(f"{k} {shares[k]:5.1f}%" for k in CATEGORIES)


def _print_composition(c: Composition) -> None:
    print("\n=== transcript composition (the summarizer's input) ===")
    print(
        f"  uncapped render : {c.rendered:>6} chars  ({sum(c.pieces.values())} pieces:"
        f" {c.pieces['dialogue']} dialogue, {c.pieces['tool-call']} calls,"
        f" {c.pieces['tool-result']} tool results)"
    )
    keep = (
        f"(head {c.head_kept} + tail {c.tail_kept}, marker reserved)"
        if c.elided
        else "(fits — nothing elided)"
    )
    print(f"  kept under cap  : {c.kept:>6} chars  {keep}  elided {c.elided}")
    print(f"  chars uncapped  : {_fmt_shares(c.share(c.chars))}  (+{c.joiners} joiners)")
    print(f"  chars kept      : {_fmt_shares(c.share(c.kept_chars))}")
    print(
        "  dropped by cap  : "
        + "  ".join(f"{k} {c.dropped[k]}" for k in CATEGORIES)
    )


def _longest_match_lengths(ref: str, text: str) -> list[int]:
    """L[i] = length of the longest substring of `ref` starting at text[i].
    A suffix automaton over reversed(ref) scanned by reversed(text): longest
    match ENDING at a reversed position = longest match STARTING at the
    original one. O(len(ref) + len(text)), exact, no hashing."""
    nxt: list[dict[str, int]] = [{}]  # SAM: transitions per state, link, len
    link = [-1]
    length = [0]
    last = 0
    for c in reversed(ref):
        cur = len(nxt)
        nxt.append({})
        link.append(0)
        length.append(length[last] + 1)
        p = last
        while p != -1 and c not in nxt[p]:
            nxt[p][c] = cur
            p = link[p]
        if p == -1:
            link[cur] = 0
        else:
            q = nxt[p][c]
            if length[p] + 1 == length[q]:
                link[cur] = q
            else:
                clone = len(nxt)
                nxt.append(dict(nxt[q]))
                link.append(link[q])
                length.append(length[p] + 1)
                while p != -1 and nxt[p].get(c) == q:
                    nxt[p][c] = clone
                    p = link[p]
                link[q] = link[cur] = clone
        last = cur
    end_len = []  # longest match ending at each position of reversed(text)
    v = l = 0
    for c in reversed(text):
        while v and c not in nxt[v]:
            v = link[v]
            l = length[v]
        if c in nxt[v]:
            v = nxt[v][c]
            l += 1
        else:
            v = l = 0
        end_len.append(l)
    return list(reversed(end_len))


def verbatim(output: str, transcript: str) -> tuple[float, int]:
    """(% of output covered by copied runs of >= MIN_RUN chars, longest
    run). Greedy left-to-right so overlapping seeds never double-count."""
    if not output:
        return 0.0, 0
    lengths = _longest_match_lengths(transcript, output)
    covered = longest = i = 0
    n = len(output)
    while i < n:
        run = lengths[i]
        longest = max(longest, run)
        if run >= MIN_RUN:
            covered += run
            i += run
        else:
            i += 1
    return 100.0 * covered / n, longest


def near_verbatim(output: str, transcript: str) -> tuple[float, int]:
    """The word-normalized twin of `verbatim`: runs of consecutive words the
    output shares with the transcript (lowercased alphanumerics), so recited
    prose scores even when punctuation/token drift breaks the char runs.
    (% of words inside runs of >= MIN_WORDS words, longest run)."""
    tw = re.findall(r"[a-z0-9]+", transcript.lower())
    pw = re.findall(r"[a-z0-9]+", output.lower())
    if not pw:
        return 0.0, 0
    grams = {" ".join(tw[i : i + MIN_WORDS]) for i in range(len(tw) - MIN_WORDS + 1)}

    def hit(i: int) -> bool:
        return i + MIN_WORDS <= len(pw) and " ".join(pw[i : i + MIN_WORDS]) in grams

    covered = longest = i = 0
    while i < len(pw):
        if hit(i):
            j = i
            while hit(j):
                j += 1
            run = j - i + MIN_WORDS - 1  # every word the seed 8-grams span
            longest = max(longest, run)
            covered += run
            i = j + MIN_WORDS - 1
        else:
            i += 1
    return 100.0 * covered / len(pw), longest


def groundedness(output: str, transcript: str) -> tuple[float, int]:
    """(% of the summary's UNIQUE fact-shaped tokens appearing verbatim in
    the transcript, count). The strongest automatic signal that survives the
    finding that the parrot's prose is uncopied. Not a factuality proof —
    the human faithfulness read still belongs to the Step-4 review."""
    toks = {s for m in FACT_TOKEN.findall(output) if len(s := m.strip(FACT_STRIP)) >= FACT_MIN}
    if not toks:
        return 0.0, 0
    return 100.0 * sum(x in transcript for x in toks) / len(toks), len(toks)


def _is_section_line(line: str, name: str) -> bool:
    """A line that IS the section name (heading/bold form, optional colon)."""
    s = line.strip().lstrip("#").strip()
    s = s[2:-2].strip() if s.startswith("**") and s.endswith("**") else s
    return s == name or s.startswith(name + ":")


def sections(output: str) -> list[str]:
    return [n for n in SECTIONS if any(_is_section_line(ln, n) for ln in output.splitlines())]


def count_markers(output: str) -> dict[str, int]:
    return {m: output.count(m) for m in MARKERS}


def ends_mid_sentence(text: str) -> bool:
    """A reply cut at max_tokens ends mid-word, not on sentence-final
    punctuation — a WARNING flag and human-look trigger, never a verdict:
    ends-clean is adjudicated by `ends_clean_verdict` + the human read."""
    text = text.rstrip()
    return not text or not text.endswith(SENTENCE_END)


def ends_clean_verdict(stop_reason: str | None) -> str:
    """The wizard-ratified ends-clean adjudication (2026-10-22): FAIL only
    on stop_reason == "max_tokens" (the failure mode the criterion guards);
    anything else — including a missing stop_reason — goes to the human
    read, which alone can print PASS. Never returns "PASS"."""
    return "FAIL(max_tokens)" if stop_reason == "max_tokens" else "HUMAN"


@dataclass
class Score:
    chars: int
    target_tokens: int
    band: tuple[int, int]
    text_tokens: int | None  # None offline without trace usage; chars//3 est
    est_text_tokens: int  # always: chars // CHARS_PER_TOKEN
    within_band: bool
    mid_sentence: bool
    markers: dict[str, int] = field(default_factory=dict)
    marker_total: int = 0
    sections_found: list[str] = field(default_factory=list)
    coverage_pct: float = 0.0
    longest_run: int = 0
    word_coverage_pct: float = 0.0
    longest_word_run: int = 0
    grounded_pct: float = 0.0
    fact_tokens: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    wall_s: float | None = None
    stop_reason: str | None = None
    summarized: bool | None = None
    error: str | None = None
    head: str = ""

    @property
    def ends_verdict(self) -> str:
        """FAIL(max_tokens) or HUMAN — see ends_clean_verdict; PASS is the
        human read's to print, never the machine's."""
        return ends_clean_verdict(self.stop_reason)

    @property
    def verdict(self) -> str:
        # Run ceilings 200c/150w are the Step-4 gate's ceilings — copied
        # prose this long fails the gate whether or not markers are present.
        if self.error:
            return f"FAILED ({self.error})"
        if self.marker_total or self.longest_run >= 200 or self.longest_word_run >= 150:
            return "MIMIC — mimics transcript format / ungrounded prose"
        if len(self.sections_found) >= 4:
            return "SUMMARY — structured, no echo"
        return "MIXED — no echo, thin structure"


def score_output(
    text: str,
    transcript: str,
    target_tokens: int,
    usage=None,
    wall_s: float | None = None,
    summarized: bool | None = None,
    error: str | None = None,
    stop_reason: str | None = None,
) -> Score:
    est = len(text) // CHARS_PER_TOKEN
    thinking = usage.thinking_tokens if usage is not None else None
    out_tokens = usage.output_tokens if usage is not None else None
    text_tokens = out_tokens - (thinking or 0) if out_tokens is not None else None
    measure = text_tokens if text_tokens is not None else est
    markers = count_markers(text)
    cov, longest = verbatim(text, transcript)
    wcov, wlongest = near_verbatim(text, transcript)
    gpct, ntok = groundedness(text, transcript)
    return Score(
        chars=len(text),
        target_tokens=target_tokens,
        band=(SUMMARY_BAND_MIN, SUMMARY_BAND_MAX),
        text_tokens=text_tokens,
        est_text_tokens=est,
        within_band=SUMMARY_BAND_MIN <= measure <= SUMMARY_BAND_MAX,
        mid_sentence=ends_mid_sentence(text),
        markers=markers,
        marker_total=sum(markers.values()),
        sections_found=sections(text),
        coverage_pct=round(cov, 1),
        longest_run=longest,
        word_coverage_pct=round(wcov, 1),
        longest_word_run=wlongest,
        grounded_pct=round(gpct, 1),
        fact_tokens=ntok,
        input_tokens=usage.input_tokens if usage is not None else None,
        output_tokens=out_tokens,
        thinking_tokens=thinking,
        wall_s=wall_s,
        stop_reason=stop_reason,
        summarized=summarized,
        error=error,
        head=text[:100],
    )


def _fmt_row(label: str, s: Score) -> str:
    tok = (
        f"{s.text_tokens}/{s.thinking_tokens}" if s.text_tokens is not None
        else f"~{s.est_text_tokens}/-"
    )
    wall = f"{s.wall_s:.0f}s" if s.wall_s is not None else "n/a"
    return (
        f"{label:<38} {s.chars:>6}ch  tok(t/th) {tok:>10}  band "
        f"{'IN ' if s.within_band else 'OUT'}{'*' if s.mid_sentence else ' '}  "
        f"mk {s.marker_total:>2}  sect {len(s.sections_found)}/5  "
        f"verb {s.coverage_pct:>5.1f}%  run {s.longest_run:>4}c/{s.longest_word_run:>3}w  "
        f"gnd {s.grounded_pct:>5.1f}%/{s.fact_tokens:<3} {wall:>5}  {s.verdict}"
    )


def _print_details(label: str, s: Score) -> None:
    hit = {m: n for m, n in s.markers.items() if n}
    usage = ""
    if s.input_tokens is not None:
        usage = (
            f"  usage: in={s.input_tokens} out={s.output_tokens} thinking="
            f"{s.thinking_tokens} text={s.text_tokens}"
            + (f"  summarized={s.summarized}" if s.summarized is not None else "")
        )
    lines = (
        f"\n  {label}:",
        f"    markers: {hit or 'none'}  sections: {s.sections_found or 'none'}\n"
        f"    output opens: {s.head!r}",
        f"    length: {s.chars} chars (~{s.est_text_tokens} est tok) vs target "
        f"{s.target_tokens} band [{s.band[0]}, {s.band[1]}] -> "
        f"{'within' if s.within_band else 'OUTSIDE'}; ends-clean: "
        f"{s.ends_verdict} (stop_reason={s.stop_reason!r}, char-flag="
        f"{'mid-sentence' if s.mid_sentence else 'clean'})" + usage,
        f"    verbatim: {s.coverage_pct}% in runs >= {MIN_RUN} chars (longest "
        f"{s.longest_run}); word-normalized: {s.word_coverage_pct}% in runs >= "
        f"{MIN_WORDS} (longest {s.longest_word_run}); groundedness: "
        f"{s.grounded_pct}% of {s.fact_tokens} unique fact tokens in transcript",
    ) + ((f"    error: {s.error}",) if s.error else ())
    for line in lines:
        print(line)


async def run_live(fixture: Fixture, target_tokens: int, effort: str):
    """One summarize_middle call on the compaction provider's request shape —
    MIRRORS composition._providers exactly (max_tokens, effort default, and
    the Step-4 band-sized answer reserve), or this gate measures a request
    shape the product never sends. Tee's the raw resp.text: a reply the echo
    guard rejects is replaced by the fallback inside summarize_middle, and
    the failure SHAPE is exactly what a retry needs to see."""
    from harness.composition import COMPACTION_MAX_TOKENS
    from harness.providers.anthropic_provider import DEFAULT_LOCAL_MODEL, AnthropicProvider

    provider = AnthropicProvider(
        api_key="local", effort=effort, lane="chat", max_tokens=COMPACTION_MAX_TOKENS,
        answer_headroom=SUMMARY_BAND_MAX,
        model=os.environ.get("HARNESS_LOCAL_MODEL", DEFAULT_LOCAL_MODEL),
        base_url=os.environ["HARNESS_LOCAL_URL"])
    raw: dict = {}
    real_chat = provider.chat

    async def tee(*a, **k):
        resp = await real_chat(*a, **k)
        raw["text"] = resp.text
        raw["stop_reason"] = resp.stop_reason
        blocks = resp.content_blocks or []
        raw["blocks"] = [b.get("type") for b in blocks]
        # The fork never reports thinking_tokens in usage (measured 2026-10-22),
        # so the think block's own text is the only evidence of what was
        # planned — length + opening decide "plans fetches vs plans summary".
        raw["thinking"] = "".join(
            b.get("thinking") or "" for b in blocks if b.get("type") == "thinking"
        )
        # Never lose a candidate: the scored row may be the fallback, and a
        # mid-sentence flag needs the reply's actual ENDING (learned the hard
        # way — sample 3's 12,134-char summary was printed nowhere).
        out = Path("scratch/compaction_eval_last_raw.txt")
        if out.parent.is_dir():
            out.write_text(
                f"stop_reason: {raw['stop_reason']!r}\nblocks: {raw['blocks']}\n"
                f"usage: {resp.usage}\n\n=== THINKING ===\n{raw['thinking']}\n"
                f"\n=== REPLY ===\n{resp.text or ''}\n"
            )
        return resp

    provider.chat = tee
    t0 = time.monotonic()
    text, summarized, error, usage = await summarize_middle(
        provider, fixture.middle, fixture.prev_summary, target_tokens
    )
    wall = time.monotonic() - t0
    return text, usage, wall, summarized, error, raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--live", action="store_true", help="generate one reply on the local llama")
    parser.add_argument("--effort", default="low", choices=["off", "low", "medium", "xhigh"],
                        help="default mirrors the product's compaction effort; "
                             "override only for diagnostics")
    parser.add_argument("--fixture", default=str(FIXTURE_PATH))
    args = parser.parse_args()
    fixture = load_fixture(Path(args.fixture))
    transcript = render_transcript(fixture.middle)
    target = summary_target_tokens(estimate_tokens(fixture.middle))
    print(
        f"fixture: {len(fixture.middle)} msgs -> transcript {len(transcript)} "
        f"chars, target {target} (trace {fixture.trace.get('summary_target')}), "
        f"prev_summary={'yes' if fixture.prev_summary else 'NONE (see _meta)'}"
    )
    _print_composition(composition(fixture.middle))

    tu = fixture.trace.get("summary_usage")
    parrot = score_output(
        fixture.parrot, transcript, target, usage=Usage(**tu) if tu else None,
        summarized=fixture.checkpoint.get("summarized"))
    rows: list[tuple[str, Score]] = [(fixture.baseline_label, parrot)]
    if args.live:
        from dotenv import load_dotenv
        load_dotenv()
        if not os.environ.get("HARNESS_LOCAL_URL"):
            print("--live needs HARNESS_LOCAL_URL (local llama server).", file=sys.stderr)
            return 2
        print(f"live call: effort={args.effort} (one generation on the shared -np 1 slot)…")
        text, usage, wall, summarized, error, raw = asyncio.run(
            run_live(fixture, target, args.effort)
        )
        rows.append((f"live replay effort={args.effort}", score_output(
            text, transcript, target, usage=usage, wall_s=wall,
            summarized=summarized, error=error,
            stop_reason=raw.get("stop_reason"))))
        think = raw.get("thinking") or ""
        print(f"  thinking: {len(think)} chars, blocks {raw.get('blocks')!r}, "
              f"stop_reason={raw.get('stop_reason')!r}, opens: {think[:200]!r}")
        rt = raw.get("text") or ""
        if rt:
            print(f"  reply ends ({len(rt)} chars): {rt[-200:]!r}")
            print("  raw reply + thinking saved: scratch/compaction_eval_last_raw.txt")
        if error and raw.get("text") is not None:
            print(f"  RAW reply (guard-rejected, {len(rt)} chars), "
                  f"opens: {rt[:400]!r}")

    print("\n=== compaction summary baseline ===")
    for label, s in rows:
        print(_fmt_row(label, s))
        _print_details(label, s)
    print("\n(* = mid-sentence char-flag — a human-look WARNING only; ends-clean is "
          "adjudicated by stop_reason + the human read, see docstring ruling; "
          "tok cols: text = output - thinking)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
