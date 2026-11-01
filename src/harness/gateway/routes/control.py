"""Control-surface routes: approvals inbox, triggers, and tools security.

These are the state-changing surfaces the same-origin guard exists for —
resolving a parked approval can release an OUTWARD action.
"""

from __future__ import annotations

import sys
from dataclasses import asdict

from croniter import croniter
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ... import playbooks
from ...core.events import CronFired
from ...core.policy import ALWAYS_ASK_TOOLS, ActionClass
from ...playbooks import resolve as resolve_playbook
from ...tools.schedule_tools import next_fire, validate_job
from ..preview import PreviewError
from ..state import Gateway


class ApprovalRequest(BaseModel):
    approved: bool


class AllowlistRequest(BaseModel):
    tool: str


class TriggerCreate(BaseModel):
    prompt: str
    cron: str
    label: str | None = None
    one_shot: bool = False
    autonomy: str = "supervised"
    tools: list[str] | None = None


class TriggerPatch(BaseModel):
    """Partial update; a field left None is untouched. tools=[] clears the
    allocation (there's no legitimate 'set tools to null' distinct from
    clearing, so the empty list carries that meaning)."""

    prompt: str | None = None
    cron: str | None = None
    label: str | None = None
    clear_label: bool = False
    one_shot: bool | None = None
    autonomy: str | None = None
    tools: list[str] | None = None


def build_router(gw: Gateway) -> APIRouter:
    router = APIRouter()

    # ---- approvals --------------------------------------------------------

    @router.get("/api/approvals")
    async def pending_approvals():
        # `previewable` tells the UI to fetch /preview instead of hardcoding
        # tool names — computed at read time, never persisted.
        return [
            {
                **asdict(a),
                "previewable": gw.previews is not None
                and gw.previews.previewable(a.tool),
            }
            for a in await gw.approval_store.pending()
        ]

    @router.get("/api/approvals/history")
    async def approvals_history(limit: int = 50):
        return await gw.approval_store.history(limit=min(max(limit, 1), 200))

    @router.get("/api/approvals/{approval_id}/preview")
    async def approval_preview(approval_id: str):
        """Real content for approval modals whose args carry only opaque ids
        (draft_id, file_id, trigger_id, ...), fetched at review time so the
        human never approves content-blind. Dispatches gateway/preview.py's
        resolvers over the daemon's LIVE adapted READ_ONLY tools — the
        tool_catalog snapshot has no callable handles, so this is the one
        route that touches MCP."""
        a = await gw.approval_store.get(approval_id)
        if (
            a is None
            or a.status != "pending"
            or gw.previews is None
            or not gw.previews.previewable(a.tool)
        ):
            raise HTTPException(404, "no previewable pending approval")
        try:
            return await gw.previews.resolve(a.tool, a.args, run_id=a.run_id)
        except PreviewError as e:
            raise HTTPException(e.status, e.detail) from None

    @router.post("/api/approvals/{approval_id}")
    async def decide_approval(approval_id: str, req: ApprovalRequest):
        ok, applied = await gw.waiter.resolve_detailed(approval_id, req.approved)
        if not ok:
            raise HTTPException(409, "approval is not pending (already decided?)")
        # applied=False ⇒ row marked but no live run was waiting (e.g. daemon
        # restarted) — the UI warns rather than implying the action ran.
        return {"ok": True, "applied": applied}

    # ---- triggers ---------------------------------------------------------

    # gw.tool_catalog is the STARTUP snapshot: a just-added tool 422s here until
    # the daemon restarts — order is register → respawn → PATCH the job.
    def _catalog_names() -> set[str] | None:
        return {t["name"] for t in gw.tool_catalog} if gw.tool_catalog else None

    def _check_job(*, cron: str | None, autonomy: str, tools: list[str] | None):
        """422 on invalid cron or job fields — same validator as the agent's
        schedule tools, so the two write surfaces can't drift."""
        if cron is not None and not croniter.is_valid(cron):
            raise HTTPException(422, f"invalid cron expression: {cron!r}")
        error = validate_job(autonomy=autonomy, tools=tools, catalog=_catalog_names())
        if error:
            raise HTTPException(422, error)

    @router.get("/api/triggers")
    async def list_triggers():
        out = []
        for t in await gw.triggers.list():
            d = asdict(t)
            pb = resolve_playbook(t.playbook)
            d["playbook_name"] = pb.name  # null ⇒ inline prompt
            d["prompt"] = pb.prompt
            # What a run would actually get: row allocation, else the legacy
            # name-row's frontmatter profile, else null (full toolset).
            d["effective_tools"] = t.tools if t.tools is not None else pb.tools
            d["next_fire_at"] = None
            if t.kind == "cron":
                try:
                    d["next_fire_at"] = next_fire(t.spec)
                except Exception as e:  # noqa: BLE001 — malformed spec must not 500 the list
                    print(
                        f"[gateway] trigger {t.id}: bad cron spec {t.spec!r}: {e}",
                        file=sys.stderr,
                    )
            out.append(d)
        return out

    @router.post("/api/triggers")
    async def create_trigger(req: TriggerCreate):
        _check_job(cron=req.cron, autonomy=req.autonomy, tools=req.tools)
        trigger_id = await gw.triggers.add(
            kind="cron",
            spec=req.cron,
            playbook=req.prompt,
            autonomy=req.autonomy,
            one_shot=req.one_shot,
            label=(req.label or "").strip() or None,
            tools=req.tools or None,
        )
        return {"ok": True, "id": trigger_id}

    @router.patch("/api/triggers/{trigger_id}")
    async def patch_trigger(trigger_id: str, req: TriggerPatch):
        current = await gw.triggers.get(trigger_id)
        if current is None:
            raise HTTPException(404, "no such trigger")
        # Validate the MERGED row: flipping to autonomous without tools, or
        # clearing tools on an autonomous job, must be rejected.
        merged_autonomy = req.autonomy or current.autonomy
        if req.tools is not None:
            merged_tools = req.tools or None  # [] clears
        else:
            merged_tools = current.tools
        _check_job(cron=req.cron, autonomy=merged_autonomy, tools=merged_tools)

        updates: dict = {}
        if req.cron is not None:
            updates["spec"] = req.cron
        if req.prompt is not None:
            updates["playbook"] = req.prompt
        if req.one_shot is not None:
            updates["one_shot"] = req.one_shot
        if req.autonomy is not None:
            updates["autonomy"] = req.autonomy
        if req.tools is not None:
            updates["tools"] = req.tools or None
        if req.clear_label:
            updates["label"] = None
        elif req.label is not None:
            updates["label"] = req.label.strip() or None
        if not updates:
            raise HTTPException(422, "nothing to change")
        await gw.triggers.update(trigger_id, **updates)
        return {"ok": True}

    @router.delete("/api/triggers/{trigger_id}")
    async def delete_trigger(trigger_id: str):
        if await gw.triggers.get(trigger_id) is None:
            raise HTTPException(404, "no such trigger")
        await gw.triggers.delete(trigger_id)
        return {"ok": True}

    @router.get("/api/playbooks")
    async def list_playbooks():
        """Built-in playbooks as creation templates: the UI copies a
        template's prompt/tools into the form (the stored job is
        self-contained; the .md files stay read-only)."""
        out = []
        for name in playbooks.available():
            pb = playbooks.resolve(name)
            out.append({"name": name, "prompt": pb.prompt, "tools": pb.tools})
        return out

    @router.get("/api/cron/next")
    async def cron_next(spec: str):
        """Live next-fire preview for the schedule form (croniter has no
        client-side equivalent)."""
        if not croniter.is_valid(spec):
            raise HTTPException(422, f"invalid cron expression: {spec!r}")
        return {"next_fire_at": next_fire(spec)}

    @router.post("/api/triggers/{trigger_id}/fire")
    async def fire_trigger(trigger_id: str):
        trigger = await gw.triggers.get(trigger_id)
        if trigger is None:
            raise HTTPException(404, "no such trigger")
        gw.bus.publish(
            CronFired(
                trigger_id=trigger.id,
                playbook=trigger.playbook,
                channel=trigger.channel,
                autonomy=trigger.autonomy,
                tools=trigger.tools,
                label=trigger.label,
            )
        )
        return {"ok": True}

    @router.post("/api/triggers/{trigger_id}/disable")
    async def disable_trigger(trigger_id: str):
        if await gw.triggers.get(trigger_id) is None:
            raise HTTPException(404, "no such trigger")
        await gw.triggers.set_enabled(trigger_id, False)
        return {"ok": True}

    @router.post("/api/triggers/{trigger_id}/enable")
    async def enable_trigger(trigger_id: str):
        if await gw.triggers.get(trigger_id) is None:
            raise HTTPException(404, "no such trigger")
        await gw.triggers.set_enabled(trigger_id, True)
        return {"ok": True}

    # ---- tools security (policy hierarchy + allowlist management) ----------

    @router.get("/api/tools")
    async def list_tools():
        """The full tool catalog with each tool's policy classification and
        whether it currently has a standing 'always allow' grant. Read-only:
        classifications are code-defined and immutable from the UI."""
        catalog = gw.tool_catalog or []
        granted = {
            row["tool"] for row in (await gw.allowlist.list() if gw.allowlist else [])
        }
        known = {t["name"] for t in catalog}
        tools = [
            {
                **t,
                # Always-ask tools (schedule self-edits) are never
                # allowlistable regardless of class — keep in sync with
                # policy.check and the POST guard below.
                "allowlistable": (
                    t["classification"] == ActionClass.MUTATING_LOCAL.value
                    and t["name"] not in ALWAYS_ASK_TOOLS
                ),
                "always_ask": t["name"] in ALWAYS_ASK_TOOLS,
                "currently_allowlisted": t["name"] in granted,
            }
            for t in catalog
        ]
        # Surface orphaned grants (tool no longer in the catalog) so they can be
        # cleaned up rather than lingering invisibly.
        orphans = sorted(granted - known)
        return {"tools": tools, "orphans": orphans}

    @router.post("/api/allowlist")
    async def allowlist_add(req: AllowlistRequest):
        if gw.allowlist is None:
            raise HTTPException(503, "allowlist unavailable")
        match = next(
            (t for t in (gw.tool_catalog or []) if t["name"] == req.tool), None
        )
        if match is None:
            raise HTTPException(404, "no such tool")
        # The hard guard: only MUTATING_LOCAL is allowlistable. This mirrors
        # policy.py's call-time check so the store can't be poisoned with an
        # OUTWARD/DESTRUCTIVE entry even by a hand-crafted request.
        if match["classification"] != ActionClass.MUTATING_LOCAL.value:
            raise HTTPException(
                422, "only local-change tools can be set to always allow"
            )
        # Same belt-and-braces for always-ask tools: policy.check would
        # ignore the grant anyway, but the store must not carry the row.
        if req.tool in ALWAYS_ASK_TOOLS:
            raise HTTPException(
                422, "schedule changes always ask and can't be always-allowed"
            )
        await gw.allowlist.add(req.tool)
        return {"ok": True}

    @router.delete("/api/allowlist/{tool}")
    async def allowlist_remove(tool: str):
        if gw.allowlist is None:
            raise HTTPException(503, "allowlist unavailable")
        if not await gw.allowlist.remove(tool):
            raise HTTPException(404, "no such allowlist entry")
        return {"ok": True}

    return router
