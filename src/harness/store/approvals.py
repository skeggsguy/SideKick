"""Deferred approvals: the durable half of headless approval gating.

A supervised run that hits a gated action writes a pending row here and
pauses; the web UI lists pending rows and resolves them. The in-memory
wait/wake lives in core/approvals.py — this store is only the record.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from ..core.policy import (
    ALWAYS_ASK_TOOLS,
    GATE_EGRESS_BUDGET,
    GATE_INTERNAL_PROBE,
    GATE_NEW_DESTINATION,
    GATE_ORIGIN_CHANGE,
    GATE_REF_RESOLVE,
    GATE_SECRET_READ,
)
from .db import Database, utcnow


@dataclass
class Approval:
    id: str
    run_id: str | None
    tool: str
    args: dict
    status: str  # pending | approved | denied | expired
    created_at: str
    resolved_at: str | None
    classification: str | None = None  # NULL for pre-migration rows
    #: The owning run's session (joined from `runs`, NULL when run_id is NULL or
    #: predates a run). Lets the web chat surface the approval inline for the
    #: conversation on screen.
    session_id: str | None = None
    #: Utility-model forecast P(user approves), written fire-and-forget while
    #: the row waits (core/prediction.py). NULL = not captured.
    predicted_p: float | None = None
    #: Which egress gate parked a READ_ONLY call (policy GATE_* constant:
    #: secret_read | internal_probe | egress_budget | new_destination).
    #: NULL for classification-gated rows and pre-migration rows.
    gate_kind: str | None = None
    #: Derived at read time from runs.trigger (+ the cron trigger row when it
    #: still exists) — never persisted. chat | scheduled | watch | delegate |
    #: unknown, plus the human label and the "why did this park" explanation
    #: the approval UI shows.
    source_kind: str | None = None
    source_label: str | None = None
    why_gated: str | None = None


def derive_source(
    run_trigger: str | None,
    trigger_label: str | None,
    trigger_autonomy: str | None,
) -> tuple[str, str, str]:
    """(source_kind, source_label, effective_autonomy) from a run's trigger.

    Pure so the copy is unit-testable. Watch runs are always SUPERVISED and
    never match the triggers join (their trigger is 'watch:<name>', not a
    trigger-row id), so their autonomy/label are static here. Cron autonomy
    MUST come from the joined triggers.autonomy — a cron trigger can be
    autonomous, and mislabeling it changes the why_gated copy.
    """
    if run_trigger is None:
        return "unknown", "Origin unknown (run record missing)", "supervised"
    if run_trigger == "user":
        return "chat", "You asked for this in chat", "interactive"
    if run_trigger.startswith("cron:"):
        label = f"Scheduled run — {trigger_label}" if trigger_label else "Scheduled run"
        return "scheduled", label, trigger_autonomy or "supervised"
    if run_trigger.startswith("watch:"):
        watcher = run_trigger.removeprefix("watch:")
        if watcher == "gmail":
            label = (
                "Triggered by an incoming email — content Sidekick read "
                "may have influenced this request"
            )
        else:
            label = f"Triggered by a background watcher ({watcher})"
        return "watch", label, "supervised"
    if run_trigger.startswith("delegate:"):
        # Defensive only: RESTRICTED sub-runs deny gated calls outright and
        # can never park an approval.
        return "delegate", "Sub-task of another run", "restricted"
    if run_trigger.startswith("browser:"):
        # A browser subagent runs SUPERVISED under a delegation envelope the
        # human approved at spawn; its boundary prompts (origin change, first
        # ref use, budget) park approvals like any supervised run.
        return (
            "browser",
            "Browser task delegated by a run you approved",
            "supervised",
        )
    return "unknown", f"Run trigger: {run_trigger}", "supervised"


def why_gated(
    classification: str | None,
    autonomy: str,
    tool: str | None = None,
    gate_kind: str | None = None,
) -> str:
    """Human explanation of why this call parked an approval instead of
    auto-running. A READ_ONLY approval row can only come from an egress gate
    (the sole approver path for READ_ONLY in core/policy.py — everything else
    read-only auto-runs); WHICH gate is the persisted gate_kind. The rest
    follows from tool + classification + the run's effective autonomy."""
    if tool in ALWAYS_ASK_TOOLS:
        return (
            "Schedule changes always ask — Sidekick is editing its own future "
            "jobs (and their permissions), so you review every one. They're "
            "never blocked outright, so a job can propose rescheduling itself."
        )
    # Delegation-envelope boundaries arrive on OUTWARD (click/type) and
    # READ_ONLY (snapshot) rows alike, so the gate_kind checks come BEFORE the
    # classification copy — the boundary is why the call parked, not the class.
    if gate_kind == GATE_ORIGIN_CHANGE:
        return (
            "The browser task you approved was scoped to one site; the page is "
            "now on a different one, so continuing needs your OK."
        )
    if gate_kind == GATE_REF_RESOLVE:
        return (
            "Sidekick wants to fill one of your saved contact details "
            "(email/phone) on this site for the first time. The value is "
            "inserted mechanically and never shown to the model."
        )
    if classification == "READ_ONLY":
        if gate_kind == GATE_SECRET_READ:
            return (
                "Sidekick read a secret-shaped file earlier this turn, so "
                "outbound web requests are paused for your OK "
                "(anti-exfiltration guard)."
            )
        if gate_kind == GATE_INTERNAL_PROBE:
            return (
                "Sidekick tried to reach a private/internal network address "
                "earlier this turn, so outbound web requests are paused for "
                "your OK (anti-exfiltration guard)."
            )
        if gate_kind == GATE_EGRESS_BUDGET:
            return (
                "This run hit the per-turn limit on outbound web calls, so "
                "each further request needs your OK (a volume guard — long "
                "research runs can legitimately hit it)."
            )
        if gate_kind == GATE_NEW_DESTINATION:
            return (
                "This site wasn't named by you and wasn't surfaced by a "
                "search this session, so the first visit needs your OK "
                "(anti-exfiltration guard)."
            )
        # Pre-migration rows have no gate_kind — say only what is known.
        return "An outbound-request guard paused this call for your OK."
    if classification == "OUTWARD":
        return (
            "This leaves your accounts and can't be undone by Sidekick. Sends "
            "always need your OK and can never be made automatic."
        )
    if classification == "DESTRUCTIVE":
        return "This permanently removes something. It always asks, and never runs unattended."
    if classification == "MUTATING_LOCAL":
        if autonomy == "interactive":
            return "This changes something in your account and isn't on your always-allow list."
        if autonomy == "autonomous":
            return (
                "This tool isn't on your always-allow list, so even this "
                "autonomous job pauses it for your review."
            )
        return (
            "Unattended runs pause every change for your review — even "
            "'always allowed' tools — because whatever triggered the run "
            "(an email, a schedule) could be attacker-influenced."
        )
    return ""  # NULL / unknown classification (pre-migration rows): omit the line


def _row_to_approval(row) -> Approval:
    keys = row.keys()
    a = Approval(
        id=row["id"],
        run_id=row["run_id"],
        tool=row["tool"],
        args=json.loads(row["args_json"]),
        status=row["status"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        classification=row["classification"] if "classification" in keys else None,
        session_id=row["session_id"] if "session_id" in keys else None,
        predicted_p=row["predicted_p"] if "predicted_p" in keys else None,
        gate_kind=row["gate_kind"] if "gate_kind" in keys else None,
    )
    # Source/why are derived only when the query joined runs(+triggers);
    # a bare SELECT * row leaves them None.
    if "run_trigger" in keys:
        kind, label, autonomy = derive_source(
            row["run_trigger"],
            row["trigger_label"] if "trigger_label" in keys else None,
            row["trigger_autonomy"] if "trigger_autonomy" in keys else None,
        )
        a.source_kind = kind
        a.source_label = label
        a.why_gated = why_gated(a.classification, autonomy, a.tool, a.gate_kind)
    return a


class ApprovalStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        run_id: str | None,
        tool: str,
        args: dict,
        classification: str | None = None,
        gate_kind: str | None = None,
    ) -> str:
        approval_id = uuid.uuid4().hex[:12]
        await self.db.conn.execute(
            "INSERT INTO approvals "
            "(id, run_id, tool, args_json, status, created_at, classification, "
            "gate_kind) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                approval_id,
                run_id,
                tool,
                json.dumps(args, default=str),
                utcnow(),
                classification,
                gate_kind,
            ),
        )
        await self.db.conn.commit()
        return approval_id

    async def resolve(self, approval_id: str, status: str) -> bool:
        """Resolve a pending approval; returns False if it wasn't pending
        (already resolved or expired) so callers can't double-decide."""
        cur = await self.db.conn.execute(
            "UPDATE approvals SET status = ?, resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, utcnow(), approval_id),
        )
        await self.db.conn.commit()
        return cur.rowcount > 0

    async def set_prediction(self, approval_id: str, p: float) -> None:
        """Record the utility model's P(approve) forecast. Fire-and-forget
        caller (core/prediction.py) — never on the gate's critical path."""
        await self.db.conn.execute(
            "UPDATE approvals SET predicted_p = ? WHERE id = ?",
            (min(max(p, 0.0), 1.0), approval_id),
        )
        await self.db.conn.commit()

    async def tool_stats(self, tool: str) -> tuple[int, int]:
        """(approved, denied) lifetime counts for a tool — predictor input."""
        cur = await self.db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM approvals "
            "WHERE tool = ? AND status IN ('approved', 'denied') GROUP BY status",
            (tool,),
        )
        d = {r["status"]: r["n"] for r in await cur.fetchall()}
        return d.get("approved", 0), d.get("denied", 0)

    async def calibration_rows(self, days: int = 90) -> list[dict]:
        """Resolved approvals with a prediction, for the calibration report."""
        cur = await self.db.conn.execute(
            "SELECT tool, classification, status, predicted_p FROM approvals "
            "WHERE status IN ('approved', 'denied') AND predicted_p IS NOT NULL "
            "AND resolved_at >= datetime('now', ?) ORDER BY resolved_at",
            (f"-{int(days)} days",),
        )
        return [dict(r) for r in await cur.fetchall()]

    # Shared enrichment joins: LEFT JOIN runs for session_id + trigger (the
    # approval's origin), and the cron trigger row (when it still exists) for
    # its label/autonomy. Watch/delegate/user triggers never match the
    # triggers join — derive_source handles them statically. Single-sourced so
    # get/pending/history can't drift on the join condition.
    _JOINS = (
        "FROM approvals a "
        "LEFT JOIN runs r ON r.id = a.run_id "
        "LEFT JOIN triggers t ON r.trigger = 'cron:' || t.id "
    )
    _ENRICH_COLS = (
        "a.*, r.session_id, r.trigger AS run_trigger, "
        "t.label AS trigger_label, t.autonomy AS trigger_autonomy"
    )
    _SELECT = f"SELECT {_ENRICH_COLS} {_JOINS}"

    async def get(self, approval_id: str) -> Approval | None:
        cur = await self.db.conn.execute(
            self._SELECT + "WHERE a.id = ?", (approval_id,)
        )
        row = await cur.fetchone()
        return _row_to_approval(row) if row else None

    async def pending(self) -> list[Approval]:
        cur = await self.db.conn.execute(
            self._SELECT + "WHERE a.status = 'pending' ORDER BY a.created_at"
        )
        return [_row_to_approval(r) for r in await cur.fetchall()]

    async def history(self, limit: int = 50) -> list[dict]:
        """Resolved approvals (approved/denied/expired), newest first, enriched
        with the owning session and the tool's eventual outcome from the audit
        log. Resolved rows are retained (never deleted), so this needs no
        migration. Returns dicts (not Approval) to carry the joined outcome."""
        # The outcome is best-effort: audit_log has no FK to a specific approval,
        # so we only attach it when EXACTLY ONE executed (approved/auto) audit row
        # matches (run_id, tool) — `HAVING COUNT(*) = 1` returns NULL otherwise
        # rather than guessing the wrong call's result. Denied/expired approvals
        # never ran, so their outcome is suppressed in Python below.
        cur = await self.db.conn.execute(
            f"SELECT {self._ENRICH_COLS}, "
            "  (SELECT al.is_error FROM audit_log al "
            "     WHERE al.run_id = a.run_id AND al.tool = a.tool "
            "       AND al.decision IN ('approved', 'auto') "
            "     GROUP BY al.run_id, al.tool HAVING COUNT(*) = 1) AS outcome_is_error, "
            "  (SELECT al.result_digest FROM audit_log al "
            "     WHERE al.run_id = a.run_id AND al.tool = a.tool "
            "       AND al.decision IN ('approved', 'auto') "
            "     GROUP BY al.run_id, al.tool HAVING COUNT(*) = 1) AS outcome_digest "
            f"{self._JOINS}"
            "WHERE a.status != 'pending' ORDER BY a.resolved_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        out: list[dict] = []
        for r in rows:
            a = _row_to_approval(r)
            # Outcome only makes sense for an approved action that actually ran.
            err = r["outcome_is_error"]
            is_error = (
                None if (a.status != "approved" or err is None) else bool(err)
            )
            d = {
                "id": a.id,
                "run_id": a.run_id,
                "tool": a.tool,
                "args": a.args,
                "status": a.status,
                "created_at": a.created_at,
                "resolved_at": a.resolved_at,
                "classification": a.classification,
                "session_id": a.session_id,
                "gate_kind": a.gate_kind,
                # Hand-rolled dict (not asdict) — the derived fields must be
                # listed explicitly or history silently drops them.
                "source_kind": a.source_kind,
                "source_label": a.source_label,
                "why_gated": a.why_gated,
                "outcome_is_error": is_error,
                "outcome_digest": r["outcome_digest"] if is_error is not None else None,
            }
            out.append(d)
        return out
