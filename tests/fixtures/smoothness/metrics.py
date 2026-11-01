"""Pure verdicts over the instrument's numbers (no browser; pinned offline by
tests/test_smoothness_probe.py so each hard target is proven able to go red).

THE TIMING RULE (README "Hard asserts vs reported numbers"): zero/structural
targets are hard asserts; wall-clock numbers (frame p50/p95/max, parse and
sanitize ms) are only REPORTED, never asserted — a slow or busy host must not
flake the suite. DOM churn is a count, not a time, and only ever drops on a
slower host (fewer frames = fewer re-renders), so it is asserted, at the goal
line itself (generous: today's count is far below it).
"""
from __future__ import annotations

# Goal 3e: 62,322 nodes removed streaming the 16k-char "long" answer before
# step 3 (BK-028); the target is >= 90 % less.
LONG_CHURN_BASELINE = 62_322
LONG_CHURN_CEILING = LONG_CHURN_BASELINE // 10
# A sampler that saw this few frames / bubble frames did not watch a stream;
# a zero there is a vacuous green, not a pass.
MIN_FRAMES, MIN_BUBBLE_FRAMES = 200, 100
SCROLL_LEFT = 40
TRANSPARENT = "rgba(0, 0, 0, 0)"


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 2) if xs else None


def summarize(S: dict) -> dict:
    """The instrument's raw window.__S → the reported numbers."""
    fr = S["frames"]
    parse_ms = [p[0] for p in S["parse"]]
    return {
        "frames": len(fr), "frame_p50": pct(fr, .5), "frame_p95": pct(fr, .95),
        "frame_max": round(max(fr or [0]), 1),
        "frames_over_25ms": sum(f > 25 for f in fr), "frames_over_50ms": sum(f > 50 for f in fr),
        "md_parses": len(parse_ms), "parse_total_ms": round(sum(parse_ms)), "parse_p95": pct(parse_ms, .95),
        "largest_parse_input": max([p[1] for p in S["parse"]] or [0]),
        "sanitize_total_ms": round(sum(S["sanitize"])), "innerHTML_total_ms": round(sum(S["inner"])),
        "nodes_removed": S["removed"], "nodes_added": S["added"],
        "block_retypes": len(S["retypes"]), "retype_kinds": sorted(set(S["retypes"]))[:8],
        "bubble_shrink_frames": len(S["shrinks"]), "shrink_px_max": max(S["shrinks"] or [0]),
        "raw_marker_frames": S["rawMarkerFrames"], "bubble_frames": S["bubbleFrames"],
        "think_collapse_px": S["thinkCollapse"],
    }


TIMING_KEYS = ("frames", "frame_p50", "frame_p95", "frame_max", "frames_over_25ms", "frames_over_50ms",
               "parse_total_ms", "sanitize_total_ms", "nodes_removed", "nodes_added")


def stream_checks(s: dict) -> list[tuple[str, bool, object]]:
    """Goals 3a/3b/5 on the smooth stream: (name, ok, detail) rows."""
    sampled = s["frames"] >= MIN_FRAMES and s["bubble_frames"] >= MIN_BUBBLE_FRAMES
    collapse = s["think_collapse_px"]
    return [
        ("sampler-watched-the-stream", sampled, {"frames": s["frames"], "bubble_frames": s["bubble_frames"]}),
        ("block-retypes-0 (BK-028)", sampled and s["block_retypes"] == 0,
         {"block_retypes": s["block_retypes"], "kinds": s["retype_kinds"]}),
        ("raw-marker-frames-0 (BK-028)", sampled and s["raw_marker_frames"] == 0,
         {"raw_marker_frames": s["raw_marker_frames"]}),
        # Non-empty: the strip must have been SEEN going streaming -> done, or 0 px proves nothing.
        ("think-collapse-0px (BK-030)", bool(collapse) and max(collapse) == 0, {"think_collapse_px": collapse}),
    ]


def selection_check(sel: dict) -> tuple[str, bool, object]:
    ok = sel["before"] > 0 and sel["after"] == sel["before"] and sel["anchorConnected"]
    return ("selection-survives-streaming (BK-028)", ok, sel)


def highlight_check(named: list[str], sweep: dict) -> tuple[str, bool, object]:
    """Goal 1: every transcript tappable computes a transparent tap highlight —
    the two named controls exist AND the sweep found the tappable kinds."""
    named_ok = len(named) == 2 and all(n.endswith(": " + TRANSPARENT) for n in named)
    ok = named_ok and sweep["checked"] >= 5 and not sweep["default_highlight"]
    return ("tap-highlight-none (BK-026)", ok, {"named": named, **sweep})


def scroll_left_check(h: dict) -> tuple[str, bool, object]:
    """Goal 3d: a reader's sideways scroll of a code block survives the stream
    and the finalize (same node, still at 40)."""
    ok = (h["set"] == SCROLL_LEFT and h["mid_after250ms"] == SCROLL_LEFT and h["mid_sameNode"]
          and h["post_complete"] == SCROLL_LEFT and h["post_sameNode"])
    return ("code-scrollLeft-survives (BK-028)", ok, h)


def long_churn_check(s: dict) -> tuple[str, bool, object]:
    ok = s["frames"] >= MIN_FRAMES and s["nodes_removed"] <= LONG_CHURN_CEILING
    return ("long-answer-churn-under-goal (BK-028)", ok,
            {"nodes_removed": s["nodes_removed"], "ceiling": LONG_CHURN_CEILING,
             "baseline": LONG_CHURN_BASELINE, "frames": s["frames"]})
