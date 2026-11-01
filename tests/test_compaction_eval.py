"""Offline pins for scripts/compaction_summary_eval.py — the compaction
summary quality harness (quest: compaction-summary-quality, Step 2).

Acceptance (wizard ruling, 2026-10-22): the scoring half classifies the
captured parrot deterministically as a transcript-format-mimicking failure
— markers >= 1 (actual 4), sections == 0, and the verbatim-vs-rendered-
transcript pins (longest run 114c exact / 16w word-normalized, coverage
~1.2%) as EVIDENCE that the prose is not copied from the transcript the
summarizer saw: it is ungrounded in it (confabulated, or recited from the
lost previous summary — that channel is unrecoverable and stays open).
Groundedness (fact-shaped tokens present in the transcript: 14.3% of 84 since
Step 3 re-cut the input; the parrot's own Black_hole url absent) corroborates;
the human faithfulness read belongs to Step 4. Never live, never the DB: the
eval module is imported as a plain module (precedent: test_watchers loads
fm_triage_eval — plus the sys.modules registration dataclasses need).
The offline run also reports the transcript COMPOSITION (Step 3): what the cap
keeps per role, and what it drops.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from harness.core.context import estimate_tokens
from harness.core.summarize import render_transcript, summary_target_tokens
from harness.core.types import Message
from harness.tools._extract import UNTRUSTED_NOTE

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "compaction_summary_eval.py"
_FIXTURE = _REPO / "tests" / "fixtures" / "compaction" / "parrot_20261022.json"
# Both compaction fixtures are real chat captures, so publish_public.sh keeps
# them out of the public snapshot; there, the tests that read them skip.
_needs_parrot = pytest.mark.skipif(
    not _FIXTURE.exists(), reason="real-chat fixture, private checkout only"
)


def load_compaction_eval():
    """Import scripts/compaction_summary_eval.py as a plain module (it is
    not a package). Importing must be side-effect free: no DB, no network,
    no dotenv load."""
    spec = importlib.util.spec_from_file_location("compaction_summary_eval", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolves string annotations via sys.modules[cls.__module__]
    # — an unregistered module blows up at class-creation time.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cev = load_compaction_eval()


@pytest.fixture(scope="module")
def fixture():
    if not _FIXTURE.exists():
        pytest.skip("real-chat fixture, private checkout only")
    return cev.load_fixture(_FIXTURE)


@pytest.fixture(scope="module")
def transcript(fixture):
    return render_transcript(fixture.middle)


@pytest.fixture(scope="module")
def parrot_score(fixture, transcript):
    tu = fixture.trace["summary_usage"]
    return cev.score_output(
        fixture.parrot, transcript, summary_target_tokens(estimate_tokens(fixture.middle)),
        usage=cev.Usage(**tu), summarized=fixture.checkpoint["summarized"],
    )


# -- the fixture is a real capture, wired the way the product reads --------


def test_fixture_provenance(fixture):
    raw = json.loads(_FIXTURE.read_text())
    assert [r["idx"] for r in raw["middle"]] == list(range(57, 127))
    assert len(fixture.middle) == 70
    assert fixture.parrot == raw["parrot_summary"]
    assert len(fixture.parrot) == 19_660  # matches the persisted row
    assert fixture.checkpoint["id"] == "242b780a6587"
    assert fixture.checkpoint["summarized"] is True  # the guard let it through
    # The PREVIOUS SUMMARY in effect was overwritten in session_checkpoints
    # and exists in no backup — null is the honest capture, see _meta.
    assert fixture.prev_summary is None
    assert "unrecoverable" in raw["_meta"]["prev_summary_note"]


def test_target_chain_reproduces_the_live_trace(fixture):
    # Same estimator + band clamp the incident call ran: fixture integrity.
    assert summary_target_tokens(estimate_tokens(fixture.middle)) == 10_000
    assert fixture.trace["summary_target"] == 10_000


def test_rendered_transcript_keeps_the_real_dialogue(fixture, transcript):
    """The product's ONE renderer over the real middle. Pre-Step-3 this was
    60,023 chars of tail: a 1,500-char excerpt per tool result, and a cap that
    dropped the first 26,611 chars — 3,135 of them the dialogue. The 400-char
    tool-result piece leaves an 86,611→41,007-char render that FITS under the
    60k cap, so nothing is elided and the whole exchange survives."""
    assert len(transcript) == 41_007
    assert "chars of transcript elided" not in transcript
    assert "ASSISTANT CALLED" in transcript and "TOOL RESULT:" in transcript
    assert UNTRUSTED_NOTE in transcript


def test_step3_composition_numbers_on_the_real_middle(fixture):
    """The plan's Step-3 acceptance, measured through the product's own pieces
    and keep-split (the eval printed the before-half from the pre-Step-3
    renderer: rendered 86,611 / elided 26,611 / dropped dialogue 3,135 /
    tool-result share 76.8% of what the summarizer read):

      dialogue dropped by the cap   3,135 chars -> 0
      tool-result share of the read 76.8%       -> 48.4% (plan: <= 50%)
    """
    c = cev.composition(fixture.middle)
    assert (c.rendered, c.kept, c.elided) == (41_007, 41_007, 0)
    assert c.pieces == {"dialogue": 22, "tool-call": 53, "tool-result": 53, "other": 0}
    assert c.dropped == dict.fromkeys(cev.CATEGORIES, 0)  # nothing was dropped
    share = c.share(c.kept_chars)
    assert share["tool-result"] == pytest.approx(48.4, abs=0.1)
    assert share["tool-result"] <= 50.0  # the plan's ceiling
    assert share["dialogue"] == pytest.approx(29.5, abs=0.1)  # was 14.9%


def test_composition_measures_the_drops_it_cannot_show_on_the_fixture():
    """The fixture now fits under the cap, so its dropped==0 proves the
    measurement only if the arithmetic works where the cap BITES: 60 user
    turns at the real 1,500 piece cap render 90,419 chars, and the 30,469 the
    cap elides are all dialogue (30,449 piece chars + 20 joiners) — the failure
    this whole step exists to fix, on a synthetic middle where the numbers are
    known."""
    middle = [Message(role="user", text=f"turn{i:02d} " + "x" * 1_500) for i in range(60)]
    c = cev.composition(middle)
    assert (c.rendered, c.kept, c.elided) == (90_419, 59_988, 30_469)
    assert (c.head_kept, c.tail_kept) == (19_983, 39_967)
    assert c.joiners == 59
    assert c.chars == {
        "dialogue": 90_419 - 59, "tool-call": 0, "tool-result": 0, "other": 0,
    }
    assert c.dropped == {
        "dialogue": 30_449, "tool-call": 0, "tool-result": 0, "other": 0,
    }
    # `elided` is the marker's count (every transcript char between the two
    # keeps, the 20 joiners inside the span included); `dropped` attributes the
    # PIECE chars among them, which is what the shares are computed from.
    assert c.elided - sum(c.dropped.values()) == 20
    assert sum(c.chars.values()) + c.joiners == c.rendered  # pieces tile it
    assert sum(c.kept_chars.values()) == sum(c.chars.values()) - sum(c.dropped.values())


# -- the plan's acceptance: classify the captured parrot, deterministically -


def test_scores_captured_parrot_as_the_incident(parrot_score):
    assert parrot_score.marker_total >= 1  # plan: markers >= 1  (actual 4)
    assert parrot_score.markers["TOOL RESULT:"] == 2
    assert parrot_score.markers[UNTRUSTED_NOTE] == 2
    assert parrot_score.sections_found == []  # plan: sections 0
    # The plan predicted a verbatim run > 1,000 chars. Measured against the
    # real transcript (SAM + brute-force + word-8gram agreement): the prose
    # is NOT copied from the transcript the summarizer saw — it is ungrounded
    # in it (confabulated, or recited from the lost PREVIOUS SUMMARY; the
    # lost text is unrecoverable, so that channel stays open). These pins are
    # the EVIDENCE values of the acceptance line; do not "fix" them.
    assert parrot_score.longest_run == 114
    assert parrot_score.longest_word_run == 16
    assert parrot_score.coverage_pct == pytest.approx(1.2, abs=0.1)
    assert parrot_score.verdict.startswith("MIMIC")


def test_parrot_is_in_band_and_ends_clean(parrot_score):
    # The failure the guards were built for is invisible to length checks:
    # the parrot is in-band by real tokens AND ends on punctuation.
    assert parrot_score.text_tokens == 4892
    assert parrot_score.within_band is True
    assert parrot_score.mid_sentence is False
    assert parrot_score.summarized is True


def test_parrot_groundedness_pin(parrot_score):
    # Only 14.3% of the parrot's 84 unique fact-shaped tokens (urls, paths,
    # domains, numbers >= FACT_MIN chars) appear in the rendered transcript.
    # Grounded side = the Suez url + its dates (material genuinely in the
    # surviving excerpts); ungrounded side = fabricated precision: the
    # Black_hole url (ABSENT from the input), date clusters, ref numbers.
    # Step 3 moved this 13.1% -> 14.3% and the move is noise, not signal: '1859'
    # was a date inside fetched prose the 400-char piece no longer carries, and
    # '285'/'610' now match as SUBSTRINGS of the opaque trace/request ids in the
    # dialogue the cap no longer drops — a 3-digit token is a weak signature
    # (FACT_MIN). The claim the pin carries, "the prose is ungrounded in its
    # input", holds at 12/84 either way.
    assert parrot_score.fact_tokens == 84
    assert parrot_score.grounded_pct == pytest.approx(14.3, abs=0.1)


def test_groundedness_counts_fact_shaped_tokens():
    transcript = (
        "web_fetch https://example.com/report.md -> /srv/data/report.md "
        "returned 1,240 rows on 2026-10-21"
    )
    honest = "Fetched https://example.com/report.md to /srv/data/report.md: 1,240 rows, 2026-10-21."
    assert cev.groundedness(honest, transcript) == (100.0, 4)
    # Fabricated facts score 0; a 2-char number is below the signature floor.
    pct, n = cev.groundedness("Read https://example.com/other.doc at /srv/x (42 rows, 1999).", transcript)
    assert (pct, n) == (0.0, 3)


def test_scoring_is_deterministic(fixture, transcript, parrot_score):
    tu = fixture.trace["summary_usage"]
    again = cev.score_output(
        fixture.parrot, transcript, parrot_score.target_tokens,
        usage=cev.Usage(**tu), summarized=True,
    )
    assert asdict(again) == asdict(parrot_score)


# -- predicate units: synthetic, so the table means what it says -----------


def test_verbatim_finds_copied_runs_and_respects_threshold():
    seed = "".join(f"alpha{i:03d};" for i in range(20))  # 180 chars, unique
    transcript = f"head;{seed};tail"
    out_full = f"ownwords:{seed}:more"  # ':' never precedes seed in transcript
    pct, longest = cev.verbatim(out_full, transcript)
    assert longest == 180
    assert pct == pytest.approx(100.0 * 180 / len(out_full), abs=0.1)
    short = seed[:79]  # one char under MIN_RUN: visible, but not coverage
    pct, longest = cev.verbatim(f"x{short}", transcript)
    assert pct == 0.0 and longest == 79


def test_near_verbatim_survives_punctuation_drift():
    # Recited prose with retyped punctuation: exact runs break, word runs
    # hold. This pair is what separates "mimicked format + copied prose"
    # (bad) from "mimicked format + confabulated prose" (the fixture).
    transcript = "The Suez Canal, opened in 1869, links the Mediterranean Sea to the Red Sea!"
    recital = "the suez canal opened in 1869 links the mediterranean sea to the red sea"
    assert cev.verbatim(recital, transcript)[1] < 40
    pct, longest = cev.near_verbatim(recital, transcript)
    assert longest == 14 and pct == 100.0
    assert cev.near_verbatim("nothing alike at all here", transcript) == (0.0, 0)


def test_sections_only_count_heading_lines():
    structured = "\n".join([
        "# GOALS", "text", "**DECISIONS**", "text", "OPEN WORK:",
        "FACTS", "PREFERENCES",
    ])
    assert cev.sections(structured) == list(cev.SECTIONS)
    assert cev.sections("The GOALS we tracked included goals; see DECISIONS memo.") == []
    assert cev.sections("goals\ndecisions") == []  # case-sensitive headings


def test_markers_count_banner_and_roles():
    clean = "GOALS: ship it. We fetched a page and learned facts."
    assert cev.count_markers(clean) == {m: 0 for m in cev.MARKERS}
    echo = f"TOOL RESULT: {UNTRUSTED_NOTE}\nUSER: hi\nASSISTANT CALLED x: {{}}"
    counts = cev.count_markers(echo)
    assert sum(counts.values()) == 4


def test_verdict_flags_copies_and_failures():
    # The MIMIC branch's second half is the Step-4 gate's ceilings (200c /
    # 150w): a reply that "cures" ungroundedness by COPYING must not read as
    # a summary, even with zero markers. Word-normalized drift scores too.
    words = " ".join(f"w{i:03d}" for i in range(160))
    drift = ", ".join(f"w{i:03d}" for i in range(160))  # commas break char runs
    copied_words = cev.score_output(drift, words, 10_000)
    assert copied_words.marker_total == 0
    assert copied_words.longest_run < 200 and copied_words.longest_word_run >= 150
    assert copied_words.verdict.startswith("MIMIC")
    # One char under the char ceiling stays out of the copy branch.
    under = "".join(f"line{i:03d} " for i in range(24))  # 192 chars
    s = cev.score_output(f"own words. {under}done.", "PREFIX " + words, 10_000)
    assert s.longest_run < 200 and not s.verdict.startswith("MIMIC")
    # The live baseline's short-echo shape: the error owns the verdict.
    failed = cev.score_output(
        "(no summary is available for the 70 elided middle messages)", "x",
        10_000, summarized=False, error="summary implausibly short",
    )
    assert failed.verdict == "FAILED (summary implausibly short)"


def test_mid_sentence_flag():
    assert cev.ends_mid_sentence("the answer to everything i") is True
    assert cev.ends_mid_sentence("ends with a comma,") is True
    assert cev.ends_mid_sentence("") is True
    assert cev.ends_mid_sentence("Done.") is False
    assert cev.ends_mid_sentence('ends with a quote"') is False
    assert cev.ends_mid_sentence("(parenthetical)") is False


def test_ends_clean_verdict_is_max_tokens_or_human():
    """Wizard ruling 2026-10-22: ends-clean FAILs only on the failure mode
    it guards — stop_reason == "max_tokens" (production precedent
    agent.py:181). A non-truncating value OR a missing one goes to the
    human read; the machine never prints PASS, and the char-class flag can
    never auto-fail alone."""
    assert cev.ends_clean_verdict("max_tokens") == "FAIL(max_tokens)"
    assert cev.ends_clean_verdict("end_turn") == "HUMAN"
    assert cev.ends_clean_verdict("stop_sequence") == "HUMAN"
    assert cev.ends_clean_verdict(None) == "HUMAN"
    # Through Score: a char-flagged reply with no stop_reason (the offline
    # parrot's shape) is a human look, never a verdict.
    s = cev.score_output("cut off mid word", "x", 10_000)
    assert s.mid_sentence is True and s.ends_verdict == "HUMAN"
    t = cev.score_output("cut off mid word", "x", 10_000, stop_reason="max_tokens")
    assert t.ends_verdict == "FAIL(max_tokens)"


# -- the eval stays manual, offline-safe, and DB-free ----------------------


def test_eval_script_is_db_free_and_defers_dotenv():
    src = _SCRIPT.read_text()
    code = src.split('"""', 2)[2]
    for forbidden in ("sqlite3", "harness.db", "Database(", "mode=ro"):
        assert forbidden not in code, f"eval must never touch the DB: {forbidden}"
    # load_dotenv appears only inside main() (the live branch) — an offline
    # import must never leak .env into the test process (gotcha 2026-09-28).
    assert code.index("load_dotenv") > code.index("def main")


@_needs_parrot
def test_offline_cli_runs_and_classifies_the_parrot():
    r = subprocess.run(
        [sys.executable, str(_SCRIPT)], capture_output=True, text=True,
        cwd=_REPO, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "compaction summary baseline" in r.stdout
    assert "MIMIC" in r.stdout
    assert "live replay" not in r.stdout  # offline by default


# -- the second fixture: a NORMAL middle (post-fix follow-up, 2026-10-22) ---

_NORMAL = _REPO / "tests" / "fixtures" / "compaction" / "normal_20261022.json"


@pytest.mark.skipif(not _NORMAL.exists(), reason="real-chat fixture, private checkout only")
def test_normal_fixture_is_the_real_normal_middle():
    """Compaction #1's exact middle from the incident session — 27 messages of
    routine work (document analysis, background research, price
    comparison) ending on an ANSWERED question with a completed tool pair.
    Its persisted summary is UNRECOVERABLE (overwritten, one row per session),
    so the baseline row must say so, not borrow the parrot's label — fixture
    honesty applies to labels too. The None prev_summary is genuine (first
    compaction), unlike the parrot fixture's unrecoverable fold.
    Measured once live (2026-10-22, --live --effort low): markers 0, sections
    5/5, 120c/24w runs, groundedness 93.3% with all four machine misses shown
    to be tokenization artifacts (a site/path token verbatim in the render,
    a comma-grouped number vs the render's ungrouped one, a slash-joined
    acronym pair the regex caught in prose),
    stop_reason end_turn — and 1,883 text tokens, band OUT below the fixed
    MIN 2,000 against target 9,784: a small middle legitimately condenses
    small; the human sufficiency read found nothing the work depends on lost
    (T1 not tripped). One sample, temperature 1.0 — a measurement, not a pin.
    """
    fx = cev.load_fixture(_NORMAL)
    assert len(fx.middle) == 27 == fx.trace["messages_summarized"]
    assert fx.prev_summary is None
    assert fx.checkpoint["id"] == "ebdee87d67bc"  # reconstructed from trace 35864
    transcript = render_transcript(fx.middle)
    assert "elided" not in transcript  # 14,231 chars: the cap never engages
    assert "parrot" not in fx.baseline_label.lower()
    assert "UNRECOVERABLE" in fx.baseline_label and "UNRECOVERABLE" in fx.parrot
