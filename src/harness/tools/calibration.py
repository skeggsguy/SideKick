"""calibration_report: predicted vs actual approval outcomes, per tool.

Read-only view over resolved approvals that carry a predicted_p forecast
(core/prediction.py). Buckets forecasts against the human's actual verdicts
and flags MEASURED allowlist candidates — the promotion itself stays a human
action in the Tools-Security screen, and OUTWARD/DESTRUCTIVE can never be
promoted regardless (core/policy.py).
"""

from __future__ import annotations

from typing import Any

from ..core.policy import ActionClass
from ..store.approvals import ApprovalStore
from .base import Tool

# A tool is a promotion candidate only with real evidence: enough resolved
# samples, near-unanimous approval, and a forecast that agrees.
MIN_SAMPLES = 10
MIN_RATE = 0.9


def format_report(rows: list[dict], days: int) -> str:
    if not rows:
        return (
            f"No predicted-and-resolved approvals in the last {days} days — "
            "the calibration needs predictions (HARNESS_APPROVAL_PREDICTION) "
            "and human verdicts to accumulate first."
        )
    by_tool: dict[str, dict] = {}
    for r in rows:
        t = by_tool.setdefault(
            r["tool"],
            {"n": 0, "approved": 0, "p_sum": 0.0, "classification": r["classification"]},
        )
        t["n"] += 1
        t["approved"] += 1 if r["status"] == "approved" else 0
        t["p_sum"] += r["predicted_p"]

    lines = [f"Approval calibration, last {days} days ({len(rows)} resolved forecasts):"]
    candidates: list[str] = []
    for tool, t in sorted(by_tool.items(), key=lambda kv: -kv[1]["n"]):
        actual = t["approved"] / t["n"]
        mean_p = t["p_sum"] / t["n"]
        lines.append(
            f"  {tool}: {t['n']} decisions · approved {actual:.0%} · "
            f"forecast {mean_p:.0%} · gap {abs(actual - mean_p):.0%}"
        )
        allowlistable = t["classification"] == ActionClass.MUTATING_LOCAL.value
        if (
            allowlistable
            and t["n"] >= MIN_SAMPLES
            and actual >= MIN_RATE
            and mean_p >= MIN_RATE
            and t["approved"] == t["n"]  # zero denials
        ):
            candidates.append(
                f"{tool} ({t['n']} approvals, 0 denials, forecast {mean_p:.0%})"
            )
    if candidates:
        lines.append("Measured allowlist candidates (promote via Tools-Security):")
        lines.extend(f"  • {c}" for c in candidates)
    else:
        lines.append(
            f"No allowlist candidates yet (needs ≥{MIN_SAMPLES} samples, "
            f"≥{MIN_RATE:.0%} approval, zero denials, MUTATING_LOCAL only)."
        )
    return "\n".join(lines)


class CalibrationReportTool(Tool):
    name = "calibration_report"
    description = (
        "Compare the assistant's P(approve) forecasts against the user's "
        "actual approve/deny verdicts, per tool, and list measured allowlist "
        "candidates. Use for the weekly calibration report or when asked how "
        "well approval predictions are tracking reality."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Window in days (default 90).",
            },
        },
        "required": [],
    }
    classification = ActionClass.READ_ONLY

    def __init__(self, approvals: ApprovalStore) -> None:
        self.approvals = approvals

    async def run(self, args: dict[str, Any]) -> str:
        days = int(args.get("days", 90))
        return format_report(await self.approvals.calibration_rows(days=days), days)
