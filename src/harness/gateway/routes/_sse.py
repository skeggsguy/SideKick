"""The SSE wire primitive shared by every streaming route."""

from __future__ import annotations

import json


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def compaction_event(phase: str, info: dict | None) -> dict:
    """The chat SSE payload for the Agent's on_compaction hook: a start frame
    (drives the live "Compacting…" pulse) and, after the summarizer call, a
    done frame carrying the trace info verbatim. The done frame's type matches
    the run_events kind ('compaction') the reload view reads its seam from —
    live and persisted speak one vocabulary."""
    if phase == "start":
        return {"type": "compaction_start"}
    return {"type": "compaction", **(info or {})}
