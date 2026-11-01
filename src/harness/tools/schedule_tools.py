"""Schedule tools: the agent manages its own triggers and reminders.

schedule_add writes a trigger row that the daemon's scheduler will fire;
"remind me Friday 4pm" becomes a one-shot trigger whose playbook column
holds the reminder text as an inline prompt.

Every mutation here (add/edit/delete) is in policy.ALWAYS_ASK_TOOLS: the
agent is editing its own future jobs — including their autonomy and tool
allocation, i.e. its own future permissions — so a human reviews every one.
They still work from background runs (deferred approval), which is what
makes self-rescheduling jobs possible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from croniter import croniter

from .. import playbooks
from ..core.policy import ActionClass
from ..store.triggers import Trigger, TriggerStore
from .base import Tool

AUTONOMY_VALUES = ("supervised", "autonomous")


def next_fire(spec: str) -> str:
    nxt = croniter(spec, datetime.now(timezone.utc).astimezone()).get_next(datetime)
    return nxt.strftime("%A %Y-%m-%d %H:%M %Z")


def validate_job(
    *,
    autonomy: str,
    tools: list[str] | None,
    catalog: set[str] | None,
) -> str | None:
    """Cross-field validation shared by the agent tools and the gateway's
    trigger routes (so the two surfaces can't drift). Returns an error
    message, or None when valid.

    Unknown tool names are an error, not a warning: an autonomous job's
    allocation is a security boundary, and a boundary built on typos is a
    silently broken job."""
    if autonomy not in AUTONOMY_VALUES:
        return f"autonomy must be one of {', '.join(AUTONOMY_VALUES)}."
    if tools:
        if catalog is not None:
            unknown = [t for t in tools if t not in catalog]
            if unknown:
                return f"unknown tool(s): {', '.join(unknown)}."
    if autonomy == "autonomous" and not tools:
        return (
            "an autonomous job needs an explicit tools list (its allocation "
            "is what bounds it) — pass tools, or use supervised."
        )
    return None


def _title(t: Trigger) -> str:
    """A short display name: the explicit label, else the playbook name/prompt
    truncated so an inline reminder's full text never becomes the title."""
    if t.label:
        return t.label
    text = t.playbook
    return text if len(text) <= 60 else text[:57] + "..."


class ScheduleAddTool(Tool):
    name = "schedule_add"
    description = (
        "Schedule a future or recurring agent run; the user approves every "
        "schedule change. For a one-off reminder, set one_shot=true and put "
        "the reminder text in prompt (e.g. 'Remind me: submit the report'). "
        "For a recurring job, pass the full prompt the run should execute — "
        "or a built-in playbook name (e.g. 'morning_briefing'), whose prompt "
        "and tool profile are copied into the job at creation. Pass a short "
        "label so the job shows a clean name in lists. autonomy='supervised' "
        "(default) pauses every change the run makes for the user's approval; "
        "'autonomous' lets tools on the user's always-allow list auto-run, and "
        "REQUIRES a minimal explicit tools list — the job gets exactly those "
        "tools and cannot discover more (sends and deletions still wait for "
        "approval). For dynamic cycles (dates that shift each time), create a "
        "one-shot job whose prompt tells the future run to work out the next "
        "date and reschedule itself via schedule_add/schedule_edit — schedule "
        "changes always pause for the user, even in autonomous jobs. cron is "
        "a standard 5-field cron expression evaluated in local time — call "
        "current_time first when computing it from a relative date."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "5-field cron expression, local time (e.g. '0 16 13 6 *' = 16:00 on 13 June).",
            },
            "prompt": {
                "type": "string",
                "description": "The prompt the run executes (or a built-in playbook name to copy from).",
            },
            "one_shot": {
                "type": "boolean",
                "description": "True for one-off reminders: the trigger disables itself after firing.",
            },
            "label": {
                "type": "string",
                "description": "Short display name for the job (a few words, e.g. 'Q3 report reminder').",
            },
            "autonomy": {
                "type": "string",
                "enum": ["supervised", "autonomous"],
                "description": "supervised (default): every change pauses for approval. autonomous: always-allowed tools auto-run within the job's tools list.",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool allocation for the job's runs. Required (and a hard boundary) for autonomous jobs; optional profile for supervised ones.",
            },
        },
        "required": ["cron", "prompt"],
    }
    classification = ActionClass.MUTATING_LOCAL  # + ALWAYS_ASK_TOOLS overlay

    def __init__(
        self, triggers: TriggerStore, catalog: Callable[[], set[str]] | None = None
    ) -> None:
        self.triggers = triggers
        self.catalog = catalog

    async def run(self, args: dict[str, Any]) -> str:
        spec = args["cron"]
        if not croniter.is_valid(spec):
            return f"Error: {spec!r} is not a valid cron expression."
        autonomy = args.get("autonomy") or "supervised"
        tools = args.get("tools") or None
        prompt = args["prompt"]

        # Template expansion: a playbook name is copied into the row at
        # creation (self-contained job), so later UI/agent edits see and
        # change the real prompt. Explicit tools beat the template's profile.
        pb = playbooks.resolve(prompt)
        if pb.name is not None:
            prompt = pb.prompt
            tools = tools if tools is not None else pb.tools

        error = validate_job(
            autonomy=autonomy,
            tools=tools,
            catalog=self.catalog() if self.catalog else None,
        )
        if error:
            return f"Error: {error}"
        label = (args.get("label") or "").strip() or None
        trigger_id = await self.triggers.add(
            kind="cron",
            spec=spec,
            playbook=prompt,
            autonomy=autonomy,
            one_shot=bool(args.get("one_shot", False)),
            label=label,
            tools=tools,
        )
        detail = f"autonomy {autonomy}"
        if tools:
            detail += f", tools: {', '.join(tools)}"
        return (
            f"Scheduled trigger {trigger_id}: cron {spec!r}, {detail}, next "
            f"fire {next_fire(spec)}."
        )


class ScheduleListTool(Tool):
    name = "schedule_list"
    description = (
        "List all scheduled triggers and reminders (including disabled ones). "
        "Cron specs are local time; 'last fired' is UTC — localize it with "
        "convert_time (op='to_local') before quoting."
    )
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    classification = ActionClass.READ_ONLY

    def __init__(self, triggers: TriggerStore) -> None:
        self.triggers = triggers

    async def run(self, args: dict[str, Any]) -> str:
        triggers = await self.triggers.list()
        if not triggers:
            return "No triggers scheduled."
        lines = []
        for t in triggers:
            state = "enabled" if t.enabled else "disabled"
            shot = ", one-shot" if t.one_shot else ""
            tools = f", tools: {', '.join(t.tools)}" if t.tools else ""
            lines.append(
                f"{t.id} [{state}{shot}, {t.autonomy}{tools}] {_title(t)!r} — "
                f"cron {t.spec!r} → {t.playbook!r} "
                f"(last fired: {t.last_fired_at or 'never'})"
            )
        return "\n".join(lines)


class ScheduleDisableTool(Tool):
    name = "schedule_disable"
    description = (
        "Disable a scheduled trigger/reminder by id (from schedule_list). "
        "Reversible — the row is kept and can be re-enabled."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string", "description": "Id of the trigger to disable."},
        },
        "required": ["trigger_id"],
    }
    classification = ActionClass.MUTATING_LOCAL

    def __init__(self, triggers: TriggerStore) -> None:
        self.triggers = triggers

    async def run(self, args: dict[str, Any]) -> str:
        trigger_id = args["trigger_id"]
        if await self.triggers.get(trigger_id) is None:
            return f"Error: no trigger with id {trigger_id!r}."
        await self.triggers.set_enabled(trigger_id, False)
        return f"Trigger {trigger_id} disabled."


class ScheduleEditTool(Tool):
    name = "schedule_edit"
    description = (
        "Edit an existing scheduled trigger/reminder in place (get its id from "
        "schedule_list); the user approves every schedule change. Change the "
        "cron time, the prompt, the label, the one-shot flag, the autonomy "
        "(supervised/autonomous), or the tools allocation without deleting and "
        "recreating it. Pass only the fields you want to change; an empty "
        "tools array clears the allocation. Autonomous jobs must keep a "
        "non-empty tools list."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string", "description": "Id of the trigger to edit."},
            "cron": {
                "type": "string",
                "description": "New 5-field cron expression, local time — call current_time first when computing it from a relative date.",
            },
            "prompt": {
                "type": "string",
                "description": "New prompt text for the job's runs.",
            },
            "label": {
                "type": "string",
                "description": "New short display name (empty string clears it).",
            },
            "one_shot": {
                "type": "boolean",
                "description": "Whether the trigger disables itself after firing.",
            },
            "autonomy": {
                "type": "string",
                "enum": ["supervised", "autonomous"],
                "description": "New autonomy mode for the job's runs.",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New tool allocation (empty array clears it).",
            },
        },
        "required": ["trigger_id"],
    }
    classification = ActionClass.MUTATING_LOCAL  # + ALWAYS_ASK_TOOLS overlay

    def __init__(
        self, triggers: TriggerStore, catalog: Callable[[], set[str]] | None = None
    ) -> None:
        self.triggers = triggers
        self.catalog = catalog

    async def run(self, args: dict[str, Any]) -> str:
        trigger_id = args["trigger_id"]
        current = await self.triggers.get(trigger_id)
        if current is None:
            return f"Error: no trigger with id {trigger_id!r}."
        spec = args.get("cron")
        if spec is not None and not croniter.is_valid(spec):
            return f"Error: {spec!r} is not a valid cron expression."

        updates: dict[str, Any] = {}
        if spec is not None:
            updates["spec"] = spec
        if args.get("prompt") is not None:
            updates["playbook"] = args["prompt"]
        if "label" in args:
            updates["label"] = (args["label"] or "").strip() or None
        if args.get("one_shot") is not None:
            updates["one_shot"] = bool(args["one_shot"])
        if args.get("autonomy") is not None:
            updates["autonomy"] = args["autonomy"]
        if "tools" in args:
            updates["tools"] = args["tools"] or None
        if not updates:
            return "Error: nothing to change — pass at least one field to edit."

        # Validate the MERGED row, not the delta: flipping a tools-less job to
        # autonomous, or clearing tools on an autonomous job, must be rejected.
        merged_autonomy = updates.get("autonomy", current.autonomy)
        merged_tools = updates["tools"] if "tools" in updates else current.tools
        error = validate_job(
            autonomy=merged_autonomy,
            tools=merged_tools,
            catalog=self.catalog() if self.catalog else None,
        )
        if error:
            return f"Error: {error}"

        await self.triggers.update(trigger_id, **updates)
        t = await self.triggers.get(trigger_id)
        assert t is not None  # just updated it
        tail = f", next fire {next_fire(t.spec)}" if t.enabled else " (disabled)"
        return (
            f"Updated trigger {trigger_id}: {_title(t)!r} — cron {t.spec!r}, "
            f"autonomy {t.autonomy}{tail}."
        )


class ScheduleDeleteTool(Tool):
    name = "schedule_delete"
    description = (
        "Permanently delete a scheduled trigger/reminder by id (from "
        "schedule_list). Unlike schedule_disable this cannot be undone — use it "
        "to remove a job for good rather than leaving a disabled row."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string", "description": "Id of the trigger to delete."},
        },
        "required": ["trigger_id"],
    }
    # MUTATING_LOCAL + the ALWAYS_ASK_TOOLS overlay, not DESTRUCTIVE: a
    # trigger row is low-stakes and reconstructible from the approval record,
    # and always-ask is the stronger guarantee where it matters — a human
    # sees every delete (never auto, never allowlistable) while background
    # jobs can still propose cleanup via a deferred approval, which a
    # DESTRUCTIVE classification would hard-deny.
    classification = ActionClass.MUTATING_LOCAL

    def __init__(self, triggers: TriggerStore) -> None:
        self.triggers = triggers

    async def run(self, args: dict[str, Any]) -> str:
        trigger_id = args["trigger_id"]
        t = await self.triggers.get(trigger_id)
        if t is None:
            return f"Error: no trigger with id {trigger_id!r}."
        await self.triggers.delete(trigger_id)
        return f"Deleted trigger {trigger_id} ({_title(t)!r})."


def schedule_tools(
    triggers: TriggerStore, catalog: Callable[[], set[str]] | None = None
) -> list[Tool]:
    return [
        ScheduleAddTool(triggers, catalog),
        ScheduleListTool(triggers),
        ScheduleEditTool(triggers, catalog),
        ScheduleDisableTool(triggers),
        ScheduleDeleteTool(triggers),
    ]
