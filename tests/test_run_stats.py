"""RunStore.summary aggregation + the run_stats tool formatting (P7)."""

from __future__ import annotations

from harness.core.types import Usage
from harness.store.runs import RunStore
from harness.tools.run_stats import RunStatsTool, format_summary
from conftest import gotcha


async def finished_run(
    runs: RunStore,
    trigger: str = "user",
    playbook: str | None = None,
    status: str = "ok",
    cost: float = 0.01,
) -> str:
    run_id = await runs.start(
        None, "claude-sonnet-5", trigger=trigger, playbook=playbook
    )
    usage = Usage(
        input_tokens=1000,
        output_tokens=100,
        cache_read_input_tokens=4000,
        cache_creation_input_tokens=500,
        thinking_tokens=40,
    )
    await runs.finish(run_id, status, usage, cost)
    return run_id


async def test_summary_groups_by_playbook_then_trigger_kind(db):
    runs = RunStore(db)
    await finished_run(runs, trigger="user")
    await finished_run(runs, trigger="cron:abc", playbook="morning_briefing", cost=0.05)
    await finished_run(runs, trigger="cron:abc", playbook="morning_briefing", cost=0.05)
    await finished_run(runs, trigger="delegate:xyz", status="error")

    s = await runs.summary(days=7)
    assert s["runs"] == 4
    assert s["errors"] == 1
    assert s["input_tokens"] == 4000
    assert s["output_tokens"] == 400
    assert abs(s["cost_usd"] - 0.12) < 1e-9

    # The `delegate` TOOL was retired 2026-08-11, but the runs table is
    # append-only history: real rows carry channel/trigger "delegate:<id>",
    # and summary() must keep grouping them. NOT dead code.
    groups = {g["group"]: g for g in s["by_group"]}
    assert set(groups) == {"morning_briefing", "user", "delegate"}, gotcha(
        "delegate-history-interpreters",
        "RunStore.summary must still group legacy 'delegate:<id>' runs under "
        "'delegate' — the tool is gone, its run history is not",
    )
    assert groups["morning_briefing"]["runs"] == 2
    assert groups["delegate"]["errors"] == 1
    # sorted by cost, most expensive first
    assert s["by_group"][0]["group"] == "morning_briefing"


async def test_summary_excludes_runs_outside_window(db):
    runs = RunStore(db)
    run_id = await finished_run(runs)
    await db.conn.execute(
        "UPDATE runs SET started_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (run_id,),
    )
    await db.conn.commit()
    s = await runs.summary(days=7)
    assert s["runs"] == 0


async def test_run_stats_tool_formats_summary(db):
    runs = RunStore(db)
    await finished_run(runs, trigger="cron:abc", playbook="morning_briefing", cost=0.05)

    text = await RunStatsTool(runs).run({"days": 7})
    assert "Last 7 days: 1 runs" in text
    assert "$0.05" in text
    assert "morning_briefing" in text
    # cache read share of total input: 4000 / (1000 + 4000 + 500)
    assert "(73% of input)" in text


def test_format_summary_empty():
    s = {"days": 7, "runs": 0}
    assert "No runs" in format_summary(s)
