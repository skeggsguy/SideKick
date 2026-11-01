"""Agent-facing schedule tools: add (with label), list title, edit, delete."""

from __future__ import annotations

from harness.store.triggers import Trigger, TriggerStore
from harness.tools.schedule_tools import (
    ScheduleAddTool,
    ScheduleDeleteTool,
    ScheduleEditTool,
    ScheduleListTool,
    _title,
    validate_job,
)

CATALOG = lambda: {  # noqa: E731
    "gmail_search", "calendar_event_create", "schedule_edit", "current_time",
}


def _trigger(**kw) -> Trigger:
    base = dict(
        id="abc123",
        kind="cron",
        spec="0 9 * * *",
        playbook="morning_briefing",
        channel="web",
        autonomy="supervised",
        enabled=True,
        one_shot=False,
        last_fired_at=None,
        created_at="2026-07-03T00:00:00Z",
        label=None,
    )
    base.update(kw)
    return Trigger(**base)


def test_title_prefers_label_then_truncates():
    assert _title(_trigger(label="Briefing")) == "Briefing"
    assert _title(_trigger(playbook="morning_briefing")) == "morning_briefing"
    long = "Remind me: " + "x" * 100
    out = _title(_trigger(playbook=long, label=None))
    assert len(out) == 60 and out.endswith("...")


async def test_add_stores_label_and_rejects_bad_cron(db):
    store = TriggerStore(db)
    add = ScheduleAddTool(store)

    bad = await add.run({"cron": "not a cron", "prompt": "hi"})
    assert bad.startswith("Error")
    assert await store.list() == []

    out = await add.run(
        {"cron": "0 9 * * *", "prompt": "Do the thing", "label": "  My job  "}
    )
    assert "Scheduled trigger" in out
    (t,) = await store.list()
    assert t.label == "My job"  # trimmed
    assert t.playbook == "Do the thing"

    # Blank label collapses to None rather than an empty string.
    out2 = await add.run({"cron": "0 9 * * *", "prompt": "x", "label": "   "})
    tid2 = out2.split()[2].rstrip(":")
    assert (await store.get(tid2)).label is None


async def test_edit_changes_fields_and_validates(db):
    store = TriggerStore(db)
    edit = ScheduleEditTool(store)
    tid = await store.add("cron", "0 9 * * *", "old prompt", label="Old")

    assert (await edit.run({"trigger_id": "nope"})).startswith("Error")
    assert (await edit.run({"trigger_id": tid})).startswith(
        "Error: nothing to change"
    )
    assert (await edit.run({"trigger_id": tid, "cron": "bad"})).startswith("Error")

    out = await edit.run(
        {"trigger_id": tid, "cron": "0 10 * * *", "prompt": "new prompt", "label": "New"}
    )
    assert "Updated trigger" in out
    t = await store.get(tid)
    assert t.spec == "0 10 * * *"
    assert t.playbook == "new prompt"
    assert t.label == "New"

    # Empty label clears it.
    await edit.run({"trigger_id": tid, "label": ""})
    assert (await store.get(tid)).label is None

    # Explicit null label clears it too (must not AttributeError on None).
    await edit.run({"trigger_id": tid, "label": "again"})
    out = await edit.run({"trigger_id": tid, "label": None})
    assert "Updated trigger" in out
    assert (await store.get(tid)).label is None


def test_schedule_mutations_are_always_ask():
    """add/edit/delete are MUTATING_LOCAL + the ALWAYS_ASK_TOOLS overlay:
    never auto-run, never allowlistable, but background runs park a deferred
    approval instead of a hard deny (which DESTRUCTIVE would force on delete
    — that's why it's deliberately NOT DESTRUCTIVE anymore). Don't remove a
    tool from ALWAYS_ASK_TOOLS without rethinking self-permission-editing."""
    from harness.core.policy import ALWAYS_ASK_TOOLS, ActionClass
    from harness.tools.schedule_tools import ScheduleDisableTool

    for cls in (ScheduleAddTool, ScheduleEditTool, ScheduleDeleteTool):
        assert cls.classification is ActionClass.MUTATING_LOCAL
        assert cls.name in ALWAYS_ASK_TOOLS
    # disable is the reversible, background-safe knob — deliberately not listed.
    assert ScheduleDisableTool.name not in ALWAYS_ASK_TOOLS


def test_validate_job_rules():
    ok = dict(autonomy="supervised", tools=None, catalog=CATALOG())
    assert validate_job(**ok) is None
    assert "autonomy" in validate_job(**{**ok, "autonomy": "interactive"})
    # Autonomous requires an explicit, known allocation.
    assert "explicit tools" in validate_job(
        autonomy="autonomous", tools=None, catalog=CATALOG()
    )
    assert "unknown tool" in validate_job(
        autonomy="autonomous", tools=["gmail_serach"], catalog=CATALOG()
    )
    assert validate_job(
        autonomy="autonomous", tools=["gmail_search"], catalog=CATALOG()
    ) is None
    # Catalog checks are skipped when no catalog is wired (CLI path).
    assert validate_job(
        autonomy="autonomous", tools=["anything"], catalog=None
    ) is None


async def test_add_with_autonomy_and_tools(db):
    store = TriggerStore(db)
    add = ScheduleAddTool(store, CATALOG)

    err = await add.run(
        {"cron": "0 9 * * *", "prompt": "track cycle", "autonomy": "autonomous"}
    )
    assert err.startswith("Error")
    assert await store.list() == []

    out = await add.run(
        {
            "cron": "0 9 * * *",
            "prompt": "track cycle",
            "autonomy": "autonomous",
            "tools": ["calendar_event_create", "schedule_edit"],
        }
    )
    assert "Scheduled trigger" in out and "autonomous" in out
    (t,) = await store.list()
    assert t.autonomy == "autonomous"
    assert t.tools == ["calendar_event_create", "schedule_edit"]


async def test_add_expands_playbook_template(db):
    """A playbook name is a creation-time template: the row stores the
    expanded prompt and inherits the frontmatter tools unless overridden."""
    store = TriggerStore(db)
    add = ScheduleAddTool(store)  # no catalog: playbook tools skip validation

    await add.run({"cron": "0 7 * * *", "prompt": "morning_briefing"})
    (t,) = await store.list()
    assert "morning briefing" in t.playbook  # expanded text, not the name
    assert t.tools and "calendar_list_events" in t.tools  # frontmatter copied

    await store.delete(t.id)
    await add.run(
        {"cron": "0 7 * * *", "prompt": "morning_briefing", "tools": ["gmail_search"]}
    )
    (t,) = await store.list()
    assert t.tools == ["gmail_search"]  # explicit allocation beats template


async def test_edit_validates_merged_row(db):
    store = TriggerStore(db)
    edit = ScheduleEditTool(store, CATALOG)
    tid = await store.add("cron", "0 9 * * *", "p")

    # Flipping to autonomous without an allocation is rejected...
    err = await edit.run({"trigger_id": tid, "autonomy": "autonomous"})
    assert err.startswith("Error")
    assert (await store.get(tid)).autonomy == "supervised"

    # ...but works together with (or after) a tools grant.
    out = await edit.run(
        {"trigger_id": tid, "autonomy": "autonomous", "tools": ["gmail_search"]}
    )
    assert "autonomy autonomous" in out
    t = await store.get(tid)
    assert (t.autonomy, t.tools) == ("autonomous", ["gmail_search"])

    # Clearing tools on an autonomous job is rejected; unknown names too.
    assert (await edit.run({"trigger_id": tid, "tools": []})).startswith("Error")
    assert (await edit.run({"trigger_id": tid, "tools": ["nope"]})).startswith("Error")
    assert (await store.get(tid)).tools == ["gmail_search"]

    # Back to supervised, then clearing the allocation is fine.
    await edit.run({"trigger_id": tid, "autonomy": "supervised"})
    await edit.run({"trigger_id": tid, "tools": []})
    assert (await store.get(tid)).tools is None


async def test_delete_removes_row(db):
    store = TriggerStore(db)
    delete = ScheduleDeleteTool(store)
    tid = await store.add("cron", "0 9 * * *", "gone soon")

    assert (await delete.run({"trigger_id": "nope"})).startswith("Error")
    out = await delete.run({"trigger_id": tid})
    assert "Deleted trigger" in out
    assert await store.get(tid) is None


async def test_list_shows_short_title(db):
    store = TriggerStore(db)
    lst = ScheduleListTool(store)
    long = "Remind me: " + "y" * 100
    await store.add("cron", "0 9 * * *", long, one_shot=True)

    out = await lst.run({})
    assert "y" * 100 not in out.split("cron")[0]  # title portion is truncated
    assert "..." in out
