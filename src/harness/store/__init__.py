"""Durable state: one SQLite file holding sessions, runs, audit, approvals,
triggers, and (later) memory. Everything proactive — the scheduler, deferred
approvals, the audit trail — hangs off this layer, which is why it lands in
Phase 1 rather than late (see docs/PLAN-v2.md).
"""

from .db import Database, default_db_path

__all__ = ["Database", "default_db_path"]
