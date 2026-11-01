"""run_stats: the agent reads its own usage and spend.

Read-only view over the runs table so the weekly cost-report playbook (and
any ad-hoc "what did you cost me this week?" question) can answer from real
numbers instead of guessing.
"""

from __future__ import annotations

from typing import Any

from ..core.policy import ActionClass
from ..store.runs import RunStore
from .base import Tool


def _tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def format_summary(s: dict) -> str:
    if s["runs"] == 0:
        return f"No runs in the last {s['days']} days."
    cached_input = s["input_tokens"] + s["cache_read_tokens"] + s["cache_write_tokens"]
    cache_pct = 100 * s["cache_read_tokens"] / cached_input if cached_input else 0
    lines = [
        f"Last {s['days']} days: {s['runs']} runs"
        + (f" ({s['errors']} errors)" if s["errors"] else "")
        + f" · ${s['cost_usd']:.2f}",
        f"Tokens: {_tokens(s['input_tokens'])} in / {_tokens(s['output_tokens'])} out"
        + (
            f" (thinking {_tokens(s['thinking_tokens'])})"
            if s.get("thinking_tokens")
            else ""
        )
        + f" · cache read {_tokens(s['cache_read_tokens'])} ({cache_pct:.0f}% of input)",
        "By source:",
    ]
    for g in s["by_group"]:
        lines.append(
            f"  {g['group']}: {g['runs']} runs"
            + (f" ({g['errors']} errors)" if g["errors"] else "")
            + f" · ${g['cost_usd']:.2f}"
            f" · {_tokens(g['input_tokens'])} in / {_tokens(g['output_tokens'])} out"
        )
    return "\n".join(lines)


class RunStatsTool(Tool):
    name = "run_stats"
    description = (
        "Summarize the agent's own activity and API spend over the last N "
        "days: run counts, errors, token usage, cache-read share, and cost "
        "in USD, broken down by source (playbook or trigger kind). Use for "
        "cost reports and questions about what the agent has been doing."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Window in days (default 7).",
            },
        },
        "required": [],
    }
    classification = ActionClass.READ_ONLY

    def __init__(self, runs: RunStore) -> None:
        self.runs = runs

    async def run(self, args: dict[str, Any]) -> str:
        days = int(args.get("days", 7))
        return format_summary(await self.runs.summary(days=days))
