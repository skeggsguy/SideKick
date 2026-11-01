"""Memory: store + dedupe + retrieval + extraction + first-turn injection.

Offline — embeddings come from a fake, the extraction model is scripted.
Vector tests skip when the sqlite-vec extension can't load in this Python.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

import pytest

from harness.core.agent import Agent
from harness.core.policy import PolicyEngine
from harness.core.registry import ToolRegistry
from harness.core.session import Session
from harness.core.types import LLMResponse, Message, ToolCall, Usage
from harness.memory import MemoryExtractor, MemoryStore, memory_block
from harness.memory.store import CATEGORIES
from harness.providers.base import LLMProvider, OnTextDelta
from harness.tools.base import Tool
from harness.tools.memory_tools import memory_tools


def _vec_loadable() -> bool:
    try:
        import sqlite_vec

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.close()
        return True
    except (ImportError, AttributeError, sqlite3.OperationalError):
        return False


needs_vec = pytest.mark.skipif(not _vec_loadable(), reason="sqlite-vec unavailable")


class FakeEmbedder:
    """Deterministic vectors: looked up by text, else derived from a STABLE
    digest. Never builtins.hash() — string hashing is salted per process, so
    two texts could land on the same one-hot dimension in ~1/8 of runs and
    trip the 0.92 cosine dedupe (the old test_forget_many flake)."""

    dim = 8

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.available = True

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.available:
            return None
        import hashlib

        out = []
        for text in texts:
            if text in self.vectors:
                out.append(self.vectors[text])
            else:
                digest = hashlib.md5(text.encode()).digest()
                # Centered so distinct texts don't share the high baseline
                # cosine of positive-only vectors.
                out.append([b / 255 - 0.5 for b in digest[: self.dim]])
        return out


from test_agent_loop import EchoTool, FakeFetchTool, ScriptedProvider  # noqa: E402


# -- store ---------------------------------------------------------------


async def test_add_list_and_forget(db):
    store = MemoryStore(db)
    fact_id, created = await store.add_fact("Tom works from home Fridays", "routine")
    assert created
    await store.add_fact("Tom prefers tea", "preference")

    facts = await store.list_facts()
    assert {f.content for f in facts} == {
        "Tom works from home Fridays",
        "Tom prefers tea",
    }
    assert await store.forget(fact_id)
    assert await store.get(fact_id) is None
    assert len(await store.list_facts()) == 1
    assert len(await store.list_facts(include_inactive=True)) == 2
    assert not await store.forget(fact_id)  # already inactive


async def test_forgotten_fact_not_resurrected_by_restatement(db):
    """forget must be durable: an exact restatement of a dropped fact creates
    a NEW fact instead of flipping the old row back to active."""
    store = MemoryStore(db)
    old_id, _ = await store.add_fact("Tom prefers tea", "preference")
    await store.forget(old_id)

    new_id, created = await store.add_fact("Tom prefers tea", "preference")
    assert created and new_id != old_id, (
        "add_fact dedupe must match ACTIVE facts only — matching a forgotten "
        "row would resurrect it, and forget must be durable"
    )
    assert await store.get(old_id) is None  # stays forgotten
    assert [f.id for f in await store.list_facts()] == [new_id]


async def test_bad_category_rejected(db):
    store = MemoryStore(db)
    with pytest.raises(ValueError):
        await store.add_fact("x", "nonsense")


@pytest.mark.parametrize(
    "category", ["interest", "procedure", "job-config", "ops-lesson"]
)
async def test_new_categories_accepted(db, category):
    store = MemoryStore(db)
    fact_id, created = await store.add_fact(f"a {category} fact", category)
    assert created
    assert (await store.get(fact_id)).category == category
    assert await store.update_fact(fact_id, category="routine")
    assert (await store.get(fact_id)).category == "routine"


async def test_always_on_is_profile_and_preference_only(db):
    """Every category EXCEPT profile/preference stays out of the always-on
    tier, no matter its confidence — new categories are similarity-recall
    only. A category joining the tier must be a deliberate decision, not a
    side effect of extending CATEGORIES."""
    store = MemoryStore(db)
    for cat in CATEGORIES:
        if cat not in ("profile", "preference"):
            await store.add_fact(f"a {cat} fact", cat, confidence=1.0)
    await store.add_fact("Tom lives in Springfield", "profile", confidence=0.7)
    assert [f.content for f in await store.always_on()] == [
        "Tom lives in Springfield"
    ]


async def test_remember_tool_schema_covers_all_categories(db):
    tools = {t.name: t for t in memory_tools(MemoryStore(db))}
    schema = tools["remember"].input_schema["properties"]["category"]
    assert schema["enum"] == list(CATEGORIES)


async def test_exact_duplicate_dedupes(db):
    store = MemoryStore(db)
    first, created = await store.add_fact("Tom prefers tea", "preference")
    again, created_again = await store.add_fact("Tom prefers tea", "preference")
    assert created and not created_again
    assert first == again
    assert len(await store.list_facts()) == 1


@needs_vec
async def test_vector_dedupe_near_duplicate(db):
    embedder = FakeEmbedder(
        {
            "Tom likes coffee": [1, 0, 0, 0, 0, 0, 0, 0],
            "Tom enjoys coffee": [0.99, 0.05, 0, 0, 0, 0, 0, 0],
        }
    )
    store = MemoryStore(db, embedder)
    await store.setup()
    assert store.vec_available, store.vec_error

    first, _ = await store.add_fact("Tom likes coffee", "preference")
    again, created = await store.add_fact("Tom enjoys coffee", "preference")
    assert not created and again == first


@needs_vec
async def test_vector_search_ranks_by_similarity(db):
    embedder = FakeEmbedder(
        {
            "Tom's dog is called Rex": [1, 0, 0, 0, 0, 0, 0, 0],
            "Tom plays squash on Tuesdays": [0, 1, 0, 0, 0, 0, 0, 0],
            "pets": [0.9, 0.1, 0, 0, 0, 0, 0, 0],
        }
    )
    store = MemoryStore(db, embedder)
    await store.setup()
    await store.add_fact("Tom's dog is called Rex", "profile")
    await store.add_fact("Tom plays squash on Tuesdays", "routine")

    results = await store.search("pets", k=1)
    assert [f.content for f in results] == ["Tom's dog is called Rex"]


async def test_keyword_fallback_without_vectors(db):
    store = MemoryStore(db)  # no embedder, no setup
    await store.add_fact("Tom prefers strong coffee in the morning", "preference")
    await store.add_fact("Tom plays squash on Tuesdays", "routine")

    results = await store.search("how does Tom take his coffee?")
    # Both mention Tom, but the coffee fact matches more terms and ranks first.
    assert results[0].content == "Tom prefers strong coffee in the morning"


@needs_vec
async def test_backfill_indexes_facts_stored_while_offline(db):
    embedder = FakeEmbedder()
    store = MemoryStore(db, embedder)
    await store.setup()
    embedder.available = False  # Ollama "down": fact stored without vector
    await store.add_fact("Tom's dentist is Dr Nemo", "contact")

    embedder.available = True
    assert await store.backfill_vectors() == 1
    assert await store.backfill_vectors() == 0  # nothing left to index


async def test_search_mark_recalled(db):
    store = MemoryStore(db)  # keyword path
    fid, _ = await store.add_fact("Tom prefers strong coffee", "preference")
    other, _ = await store.add_fact("Tom plays squash on Tuesdays", "routine")

    # Plain search never stamps (browsing surfaces use this).
    await store.search("coffee")
    assert (await store.get(fid)).last_recalled_at is None

    before = (await store.get(fid)).updated_at
    results = await store.search("coffee", mark_recalled=True)
    # Returned facts carry the PRE-stamp value (hygiene reads "before today").
    assert results[0].last_recalled_at is None
    after = await store.get(fid)
    assert after.last_recalled_at is not None
    # The stamp must never bump updated_at — that's the "saved" date.
    assert after.updated_at == before
    # Only matched facts are stamped.
    assert (await store.get(other)).last_recalled_at is None


async def test_update_fact(db):
    store = MemoryStore(db)
    fid, _ = await store.add_fact("Tom's boss is Bill", "contact")
    before = (await store.get(fid)).updated_at

    assert await store.update_fact(fid, content="Tom's boss is Phil")
    fact = await store.get(fid)
    assert fact.content == "Tom's boss is Phil"
    assert fact.category == "contact"
    assert fact.updated_at >= before

    assert await store.update_fact(fid, category="profile")
    assert (await store.get(fid)).category == "profile"

    with pytest.raises(ValueError):
        await store.update_fact(fid)  # nothing to update
    with pytest.raises(ValueError):
        await store.update_fact(fid, category="nonsense")
    assert not await store.update_fact(9999, content="x")  # unknown id
    await store.forget(fid)
    assert not await store.update_fact(fid, content="x")  # inactive id


@needs_vec
async def test_update_fact_reindexes_vector(db):
    embedder = FakeEmbedder(
        {
            "Tom's dog is called Rex": [1, 0, 0, 0, 0, 0, 0, 0],
            "Tom's dog is called Buster": [0, 1, 0, 0, 0, 0, 0, 0],
            "Buster": [0.1, 0.9, 0, 0, 0, 0, 0, 0],
        }
    )
    store = MemoryStore(db, embedder)
    await store.setup()
    fid, _ = await store.add_fact("Tom's dog is called Rex", "profile")

    assert await store.update_fact(fid, content="Tom's dog is called Buster")
    results = await store.search("Buster", k=1)
    assert [f.id for f in results] == [fid]


# -- retrieval -------------------------------------------------------------


async def test_similar_block_empty_and_live_header_counts(db):
    """Ports the legacy combined block's pins to the split shape: empty
    store → None; the `- [category, saved date] content` line form; the
    staleness hedge; the header's LIVE shown-of-total count; the recall
    nudge; and no fact rendered twice."""
    store = MemoryStore(db)
    assert await memory_block(store, "anything", set(), 0) is None

    beekeeper_id, _ = await store.add_fact("Tom is a beekeeper", "profile")
    await store.add_fact("Tom's report is due each Friday", "project")
    date = (await store.list_facts())[0].updated_at[:10]

    # Nothing frozen → both facts ride the block as plain similar facts.
    block = await memory_block(store, "tell me about tom", set(), 0)
    assert block is not None and block.startswith("<memory>")
    assert f"- [profile, saved {date}] Tom is a beekeeper" in block
    assert f"- [project, saved {date}] Tom's report is due each Friday" in block
    assert block.count("Tom's report is due each Friday") == 1  # no duplicates
    assert "may be stale" in block  # staleness hedge in the intro
    # The header advertises the block as PARTIAL (live counts) and points
    # at the recall tool — without this the model treats the slice as the
    # whole memory (the 2026-07-23 learning).
    assert "(2 of 2 active facts" in block, (
        "the 'N of M active facts' counts are what make the model call recall "
        "for broad questions — a header that reads as complete suppresses it"
    )
    assert "recall tool" in block
    assert "system prompt" not in block  # profile_count=0 → no pointer clause

    # With one fact frozen into the system prompt, the count is LIVE: 1 of
    # 2 shown here, and the clause says where the other went.
    block = await memory_block(
        store, "what's due for the report?", {beekeeper_id}, 1
    )
    assert block is not None
    assert "(1 of 2 active facts" in block
    assert "Tom is a beekeeper" not in block


async def test_budget_skipped_fact_is_not_excluded_from_similar(db):
    """Ports the skip-oversize pin to the split shape: the budget loop
    SKIPS the oversize always-on fact (never frozen → never in
    exclude_ids), so a real query match still surfaces it in the similar
    half, and the header's live total counts it."""
    from harness.memory.retrieval import ALWAYS_ON_CHAR_BUDGET, always_on_facts

    store = MemoryStore(db)
    fat = "Tom runs marathons " + "training log " * 400  # over budget, matchable
    assert len(fat) > ALWAYS_ON_CHAR_BUDGET
    fat_id, _ = await store.add_fact(fat, "profile", confidence=0.99)
    keep_id, _ = await store.add_fact("Tom is a beekeeper", "profile", confidence=0.5)
    assert fat_id not in {f.id for f in await always_on_facts(store)}

    block = await memory_block(store, "marathons", {keep_id}, 1)
    assert block is not None
    assert "Tom runs marathons" in block  # skipped ≠ excluded: it may still
    # surface as similar, because it never entered the frozen profile.
    assert "Tom is a beekeeper" not in block  # frozen facts never ride here
    assert "(1 of 2 active facts" in block  # live total includes the skipped
    # fact; the header still points at the system-prompt half.
    assert "1 always-on profile/preference fact is in your system prompt" in block


# -- profile split (always-on -> frozen system-prompt section) ----------------


async def test_always_on_order_ties_break_on_id(db):
    """Equal confidence AND equal updated_at must still render in a fixed
    order — the per-session profile is only byte-stable if always_on() is.

    Engine note: SQLite happens to return equal-key ties in rowid order
    anyway (any single-table scan ends at rowid = id ascending), so this
    test would PASS without the `id` tie-break on today's engine. It pins a
    contract, not a witnessed failure: a future query plan, index, or
    engine must not reorder the frozen profile."""
    from harness.memory.retrieval import always_on_facts, render_profile

    store = MemoryStore(db)
    a, _ = await store.add_fact("Tom fact a", "profile", confidence=0.9)
    b, _ = await store.add_fact("Tom fact b", "profile", confidence=0.9)
    c, _ = await store.add_fact("Tom fact c", "profile", confidence=0.9)
    same = "2026-01-01T00:00:00+00:00"
    await db.conn.execute("UPDATE facts SET updated_at = ?", (same,))
    await db.conn.commit()

    assert [f.id for f in await store.always_on()] == [a, b, c]
    # The order is what the frozen profile consumes: id `a` must be the
    # first rendered fact line, and a second render is byte-identical.
    profile = render_profile(await always_on_facts(store))
    first_line = profile.splitlines()[2]
    assert first_line == "- [profile, saved 2026-01-01] Tom fact a"
    assert render_profile(await always_on_facts(store)) == profile


async def test_always_on_facts_skips_oversize_render_profile_keeps_rest(db):
    """Split version of the pinned skip: the budget loop CONTINUES past an
    over-budget fact and the survivors still render."""
    from harness.memory.retrieval import (
        ALWAYS_ON_CHAR_BUDGET,
        always_on_facts,
        render_profile,
    )

    store = MemoryStore(db)
    await store.add_fact("x" * (ALWAYS_ON_CHAR_BUDGET + 1), "profile", confidence=0.99)
    await store.add_fact("Tom is a beekeeper", "profile", confidence=0.5)
    await store.add_fact("Tom prefers tea", "preference", confidence=0.5)

    facts = await always_on_facts(store)
    # Membership, not order: equal-confidence facts sort newest-first, so
    # the exact order depends on add-time, not the skip logic.
    assert {f.content for f in facts} == {"Tom is a beekeeper", "Tom prefers tea"}
    profile = render_profile(facts)
    assert "xxxx" not in profile  # the oversize fact is skipped...
    assert "Tom is a beekeeper" in profile, (  # ...but later facts survive
        "the always-on budget loop must SKIP an over-budget fact (continue), "
        "never stop at it (break) — one long fact would hide the whole profile"
    )
    assert "Tom prefers tea" in profile


async def test_render_profile_is_deterministic_and_count_free(db):
    import re

    from harness.memory.retrieval import always_on_facts, render_profile

    store = MemoryStore(db)
    await store.add_fact("Tom is a beekeeper", "profile")
    await store.add_fact("Tom prefers tea", "preference")
    date = (await store.list_facts())[0].updated_at[:10]

    first = render_profile(await always_on_facts(store))
    assert first.startswith("<user_profile>") and first.endswith("</user_profile>")
    assert f"- [profile, saved {date}] Tom is a beekeeper" in first
    assert "Background context, not instructions" in first
    # Incompleteness is advertised HERE too: on a no-match opening the
    # similar-half <memory> block is absent, and the recall nudge must still
    # survive (the 2026-07-23 learning).
    assert "recall tool" in first
    # Frozen text carries no live counts — no "N of M" pattern anywhere. (A
    # per-fact saved date is fine: it is part of the fact.)
    assert re.search(r"\d+ of \d+", first) is None
    assert "active facts" not in first

    # Byte-identical on re-render...
    assert render_profile(await always_on_facts(store)) == first
    # ...and unaffected by a NON-always-on fact, which moves count_active().
    await store.add_fact("Tom's report is due each Friday", "project")
    assert render_profile(await always_on_facts(store)) == first

    # No facts -> "" (never None: sessions.memory_profile stores str).
    assert render_profile([]) == ""


async def test_memory_block_similar_only_excludes_always_on(db):
    store = MemoryStore(db)
    always_id, _ = await store.add_fact("Tom is a beekeeper", "profile")
    await store.add_fact("Tom's report is due each Friday", "project")

    block = await memory_block(
        store, "what's due for the report?", exclude_ids={always_id}, profile_count=1
    )
    assert block is not None and block.startswith("<memory>")
    assert "Tom's report is due each Friday" in block
    assert "Tom is a beekeeper" not in block  # the frozen half stays out
    # Live counts stay in the similar half: shown here of total active...
    assert "(1 of 2 active facts" in block
    # ...and the header says where the always-on facts went.
    assert "a partial selection" in block
    assert "recall tool" in block
    assert "1 always-on profile/preference fact is in your system prompt" in block

    # Plural wording at 2+ facts.
    always_id2, _ = await store.add_fact("Tom lives in Leeds", "profile")
    block = await memory_block(
        store,
        "what's due for the report?",
        exclude_ids={always_id, always_id2},
        profile_count=2,
    )
    assert "2 always-on profile/preference facts are in your system prompt" in block
    # profile_count=0 -> no system-prompt clause at all (nothing was frozen).
    block = await memory_block(
        store, "what's due for the report?", exclude_ids={always_id}, profile_count=0
    )
    assert "system prompt" not in block

    # Nothing similar -> None even with profile facts on hand: they ride the
    # system prompt, not this block.
    assert (
        await memory_block(
            store, "zzz-no-match", exclude_ids={always_id}, profile_count=1
        )
        is None
    )


async def test_similar_block_stamps_matches_not_always_on(db):
    """Split version of the stamping pin: the similar half stamps its genuine
    query matches; an always-on fact that matched nothing is never stamped."""
    store = MemoryStore(db)
    always_id, _ = await store.add_fact("Tom prefers tea", "preference")
    await store.add_fact("Tom's report is due each Friday", "project")

    await memory_block(
        store, "what's due for the report?", exclude_ids={always_id}, profile_count=1
    )

    by_content = {f.content: f for f in await store.list_facts()}
    assert by_content["Tom's report is due each Friday"].last_recalled_at is not None
    assert by_content["Tom prefers tea"].last_recalled_at is None


async def test_always_on_fact_matching_query_is_stamped(db):
    """The positive half of the stamping pin: an always-on fact that ALSO
    matches the query is excluded from the similar half (it rides the
    system prompt) but the match is a genuine recall — last_recalled_at
    is stamped."""
    store = MemoryStore(db)
    always_id, _ = await store.add_fact("Tom prefers strong coffee", "preference")

    block = await memory_block(store, "coffee", {always_id}, 1)
    assert block is None  # excluded from the block...
    # ...but stamped: search marks recalled BEFORE the exclusion filters.
    assert (await store.get(always_id)).last_recalled_at is not None


# -- extraction --------------------------------------------------------------


async def test_extraction_stores_facts_and_episode(db):
    store = MemoryStore(db)
    provider = ScriptedProvider(
        [
            LLMResponse(
                text='{"facts": [{"content": "Tom prefers tea", "category": '
                '"preference", "source": "user", "horizon": "lasting"}], '
                '"episode": "Discussed hot drinks."}'
            )
        ]
    )
    extractor = MemoryExtractor(provider, store)
    await extractor.extract(None, "I always drink tea", "Noted!")

    facts = await store.list_facts()
    assert [f.content for f in facts] == ["Tom prefers tea"]
    # Horizon replaced confidence — kept facts store the fixed value.
    assert facts[0].confidence == 0.8
    cur = await db.conn.execute("SELECT summary FROM episodes")
    assert [r["summary"] for r in await cur.fetchall()] == ["Discussed hot drinks."]


async def test_prose_only_reply_is_empty_extraction_not_error(db):
    from harness.store.runs import RunStore
    from harness.store.sessions import SessionStore

    store = MemoryStore(db)
    runs = RunStore(db)
    session_id = await SessionStore(db).create(channel="cli")
    run_id = await runs.start(session_id, model="m", trigger="user", channel="cli")
    provider = ScriptedProvider(
        [LLMResponse(text="No durable facts in this conversation.")]
    )
    await MemoryExtractor(provider, store, runs).extract(run_id, "hi", "hello")

    assert await store.list_facts() == []
    cur = await db.conn.execute(
        "SELECT kind, data_json FROM run_events WHERE run_id = ?", (run_id,)
    )
    rows = await cur.fetchall()
    assert [r["kind"] for r in rows] == ["memory_extraction"]  # not _error
    assert "No durable facts" in rows[0]["data_json"]


async def test_extraction_tolerates_prose_around_json(db):
    store = MemoryStore(db)
    provider = ScriptedProvider(
        [
            LLMResponse(
                text='Here is the extraction:\n{"facts": [{"content": '
                '"Tom prefers tea", "category": "preference", "source": "user", '
                '"horizon": "lasting"}], "episode": null}'
            )
        ]
    )
    await MemoryExtractor(provider, store).extract(None, "a", "b")
    assert [f.content for f in await store.list_facts()] == ["Tom prefers tea"]


async def test_extraction_skips_invalid_categories(db):
    store = MemoryStore(db)
    provider = ScriptedProvider(
        [LLMResponse(text='{"facts": [{"content": "x", "category": "junk"}], "episode": null}')]
    )
    await MemoryExtractor(provider, store).extract(None, "a", "b")
    assert await store.list_facts() == []


async def _run_for_trace(db):
    from harness.store.runs import RunStore
    from harness.store.sessions import SessionStore

    runs = RunStore(db)
    session_id = await SessionStore(db).create(channel="cli")
    run_id = await runs.start(session_id, model="m", trigger="user", channel="cli")
    return runs, run_id


async def _extraction_trace(db, run_id) -> str:
    cur = await db.conn.execute(
        "SELECT data_json FROM run_events WHERE run_id = ? AND kind = 'memory_extraction'",
        (run_id,),
    )
    return (await cur.fetchone())["data_json"]


async def test_extraction_drops_days_horizon(db):
    store = MemoryStore(db)
    runs, run_id = await _run_for_trace(db)
    provider = ScriptedProvider(
        [
            LLMResponse(
                text='{"facts": ['
                '{"content": "Tom prefers tea", "category": "preference", '
                '"source": "user", "horizon": "lasting", "confidence": 0.3}, '
                '{"content": "Tom flies to Leeds on Friday", "category": "routine", '
                '"source": "user", "horizon": "days"}'
                '], "episode": null}'
            )
        ]
    )
    await MemoryExtractor(provider, store, runs).extract(run_id, "a", "b")

    # "lasting" kept (a stray confidence value is ignored — the field is
    # retired); "days" dropped and counted in the trace.
    facts = await store.list_facts()
    assert [f.content for f in facts] == ["Tom prefers tea"]
    assert facts[0].confidence == 0.8
    data = await _extraction_trace(db, run_id)
    assert '"facts_stored": 1' in data
    assert '"facts_skipped_transient": 1' in data


async def test_extraction_source_gate(db):
    store = MemoryStore(db)
    runs, run_id = await _run_for_trace(db)
    provider = ScriptedProvider(
        [
            LLMResponse(
                text='{"facts": ['
                '{"content": "Tom seems to like jazz", "category": "interest", '
                '"source": "observed", "horizon": "lasting"}, '
                '{"content": "Retry Drive uploads once on 503", "category": '
                '"ops-lesson", "source": "observed", "horizon": "lasting"}, '
                '{"content": "Tom has a dog", "category": "profile", '
                '"horizon": "lasting"}'
                '], "episode": null}'
            )
        ]
    )
    await MemoryExtractor(provider, store, runs).extract(run_id, "a", "b")

    # Observed non-ops-lesson dropped; observed ops-lesson kept; MISSING
    # source dropped too (strict default — drift shows in the counter).
    assert [f.content for f in await store.list_facts()] == [
        "Retry Drive uploads once on 503"
    ], (
        "extraction gates are STRICT: a fact missing source (or horizon) is "
        "dropped — don't loosen them to defaults"
    )
    data = await _extraction_trace(db, run_id)
    assert '"facts_stored": 1' in data
    assert '"facts_skipped_ungrounded": 2' in data


async def test_extraction_prompt_carries_recent_memory(db):
    store = MemoryStore(db)
    fid, _ = await store.add_fact("Tom prefers tea", "preference")
    provider = ScriptedProvider([LLMResponse(text='{"facts": [], "episode": null}')])
    await MemoryExtractor(provider, store).extract(None, "hi", "hello")

    sent = provider.sent[0][0].text
    assert "<existing_memory>" in sent
    assert f"[{fid}] (preference) Tom prefers tea" in sent
    assert sent.index("</existing_memory>") < sent.index("<user>")

    # Empty store → no block at all.
    await store.forget(fid)
    provider2 = ScriptedProvider([LLMResponse(text='{"facts": [], "episode": null}')])
    await MemoryExtractor(provider2, store).extract(None, "hi", "hello")
    assert "<existing_memory>" not in provider2.sent[0][0].text


async def test_extraction_recent_memory_capped(db):
    from harness.memory.extraction import RECENT_FACTS_LIMIT

    store = MemoryStore(db)
    for n in range(RECENT_FACTS_LIMIT + 5):
        await store.add_fact(f"Tom fact number {n}", "project")
    provider = ScriptedProvider([LLMResponse(text='{"facts": [], "episode": null}')])
    await MemoryExtractor(provider, store).extract(None, "hi", "hello")

    sent = provider.sent[0][0].text
    block = sent[sent.index("<existing_memory>") : sent.index("</existing_memory>")]
    fact_lines = [ln for ln in block.splitlines() if ln.startswith("[")]
    assert len(fact_lines) == RECENT_FACTS_LIMIT


async def test_extraction_supersedes_traced_not_forgotten(db):
    store = MemoryStore(db)
    old_id, _ = await store.add_fact("Tom prefers tea", "preference")
    runs, run_id = await _run_for_trace(db)
    provider = ScriptedProvider(
        [
            LLMResponse(
                text='{"facts": [{"content": "Tom prefers coffee now", '
                '"category": "preference", "source": "user", "horizon": '
                f'"lasting", "supersedes": {old_id}}}], "episode": null}}'
            )
        ]
    )
    await MemoryExtractor(provider, store, runs).extract(run_id, "a", "b")

    # Trace-only: BOTH facts stay active; the pair is recorded for review.
    contents = {f.content for f in await store.list_facts()}
    assert contents == {"Tom prefers tea", "Tom prefers coffee now"}
    data = await _extraction_trace(db, run_id)
    assert f'"old_id": {old_id}' in data
    assert '"old": "Tom prefers tea"' in data
    assert '"new": "Tom prefers coffee now"' in data


async def test_extraction_supersedes_ignores_unshown(db):
    store = MemoryStore(db)
    runs, run_id = await _run_for_trace(db)
    provider = ScriptedProvider(
        [
            LLMResponse(
                text='{"facts": [{"content": "Tom prefers coffee", "category": '
                '"preference", "source": "user", "horizon": "lasting", '
                '"supersedes": 9999}], "episode": null}'
            )
        ]
    )
    await MemoryExtractor(provider, store, runs).extract(run_id, "a", "b")

    # An id the model invented (never shown) is ignored, fact still stored.
    assert [f.content for f in await store.list_facts()] == ["Tom prefers coffee"]
    data = await _extraction_trace(db, run_id)
    assert '"supersedes": []' in data


# -- guided lane (Apple FM's extract()) ----------------------------------------


class GuidedStub:
    """The guided-lane provider contract: extract() returns the payload or
    None (with last_error). It has NO chat — the extractor must not fall back
    to one."""

    def __init__(self, payload: dict | None = None, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.last_error = "model unavailable" if fail else None
        self.calls: list[tuple[str, str]] = []

    async def extract(self, instruction: str, text: str) -> dict | None:
        self.calls.append((instruction, text))
        return None if self.fail else self.payload


async def test_guided_extraction_stores_payload_and_uses_short_prompt(db):
    store = MemoryStore(db)
    stub = GuidedStub(
        payload={
            "facts": [
                {
                    "content": "Tom prefers tea",
                    "category": "preference",
                    "source": "user",
                    "horizon": "lasting",
                    "supersedes": None,
                }
            ],
            "episode": "Discussed tea.",
        }
    )
    await MemoryExtractor(stub, store).extract(None, "I drink tea", "Noted")
    assert [f.content for f in await store.list_facts()] == ["Tom prefers tea"]

    assert len(stub.calls) == 1
    instruction, transcript = stub.calls[0]
    # The generable schema defines the shape — the prompt must not quote it
    # (quoting is what the model used to copy back).
    assert "ONLY a JSON object" not in instruction
    assert "Categories:" in instruction
    assert "<user>" in transcript


async def test_guided_failure_traces_error_never_silence(db):
    store = MemoryStore(db)
    runs, run_id = await _run_for_trace(db)
    await MemoryExtractor(GuidedStub(fail=True), store, runs).extract(
        run_id, "a", "b"
    )
    cur = await db.conn.execute(
        "SELECT kind, data_json FROM run_events WHERE run_id = ?", (run_id,)
    )
    rows = await cur.fetchall()
    assert [r["kind"] for r in rows] == ["memory_extraction_error"]
    assert "model unavailable" in rows[0]["data_json"]
    assert await store.list_facts() == []


async def test_copied_back_existing_fact_is_dropped_and_counted(db):
    """The failure this guards (seen replaying real turns): the model echoes
    <existing_memory> lines back — verbatim or reworded — and exact-text
    dedupe alone would re-store them."""
    store = MemoryStore(db)
    await store.add_fact("Tom prefers tea", "preference")
    runs, run_id = await _run_for_trace(db)
    provider = ScriptedProvider(
        [
            LLMResponse(
                text='{"facts": ['
                '{"content": "Tom prefers tea", "category": "preference", '
                '"source": "user", "horizon": "lasting"}, '
                '{"content": "Tom **strongly** prefers tea", "category": '
                '"interest", "source": "user", "horizon": "lasting"}, '
                '{"content": "Tom owns a sailboat", "category": "profile", '
                '"source": "user", "horizon": "lasting"}'
                '], "episode": null}'
            )
        ]
    )
    await MemoryExtractor(provider, store, runs).extract(run_id, "a", "b")

    contents = sorted(f.content for f in await store.list_facts())
    assert contents == ["Tom owns a sailboat", "Tom prefers tea"]
    data = await _extraction_trace(db, run_id)
    assert '"facts_skipped_existing": 2' in data


def test_prompts_chat_keeps_json_line_guided_drops_it():
    from harness.memory.extraction import (
        EXTRACTION_SYSTEM,
        EXTRACTION_SYSTEM_GUIDED,
        _INTRO,
        _JSON_SHAPE,
    )

    # Haiku's prompt is byte-identical to before the split, and the guided
    # prompt is the same body with ONLY the JSON-shape line removed.
    assert EXTRACTION_SYSTEM.replace(_JSON_SHAPE, "", 1) == EXTRACTION_SYSTEM_GUIDED
    assert EXTRACTION_SYSTEM_GUIDED.startswith(_INTRO)
    # Speaker attribution rides BOTH prompts.
    assert "assistant's words are not the user's" in EXTRACTION_SYSTEM
    assert "assistant's words are not the user's" in EXTRACTION_SYSTEM_GUIDED


async def test_recent_facts_window(db):
    store = MemoryStore(db)
    recent_id, _ = await store.add_fact("Tom recent fact", "project")
    old_id, _ = await store.add_fact("Tom old fact", "project")
    gone_id, _ = await store.add_fact("Tom forgotten fact", "project")
    await store.forget(gone_id)
    from datetime import datetime, timedelta, timezone

    backdated = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    await db.conn.execute(
        "UPDATE facts SET updated_at = ? WHERE id = ?", (backdated, old_id)
    )
    await db.conn.commit()

    facts = await store.recent_facts(days=7)
    assert [f.id for f in facts] == [recent_id]
    # Limit respected, newest-first.
    for n in range(3):
        await store.add_fact(f"Tom filler {n}", "project")
    limited = await store.recent_facts(days=7, limit=2)
    assert len(limited) == 2
    assert limited[0].updated_at >= limited[1].updated_at


# -- agent injection ---------------------------------------------------------


def _agent_for(provider: ScriptedProvider, store: MemoryStore, **kw) -> Agent:
    """Fresh Agent with memory wired — the shape the daemon builds per run."""
    return Agent(
        provider=provider,
        registry=kw.pop("registry", ToolRegistry()),
        system_prompt="test",
        policy=PolicyEngine(),
        memory=store,
        **kw,
    )


async def test_memory_injected_into_first_turn_only(db):
    store = MemoryStore(db)
    await store.add_fact("Tom is based in the UK", "profile")
    await store.add_fact("Tom's report is due each Friday", "project")
    provider = ScriptedProvider([LLMResponse(text="ok"), LLMResponse(text="ok again")])
    agent = _agent_for(provider, store)
    session = Session()  # storeless: the frozen profile can only live here
    await agent.run_turn(session, "what's due?")
    # Turn-2 wording matters: it matches a fact, so a re-injection (the
    # bug this test pins) WOULD produce a <memory> block here.
    await agent.run_turn(session, "and what else is due?")

    # Turn 1: the similar fact rides the USER message, the always-on fact
    # the END of the system prompt — never both.
    first_msg = provider.sent[0][0].text
    assert "<memory>" in first_msg and "Tom's report is due each Friday" in first_msg
    assert "Tom is based in the UK" not in first_msg
    assert first_msg.endswith("what's due?")
    assert "Tom is based in the UK" in provider.systems[0]
    assert session.memory_profile  # frozen on the attribute, both turns
    assert provider.systems[0] == provider.systems[1]  # frozen, not re-rendered
    # Turn 2: no re-injection — the session already has messages.
    second_msg = provider.sent[1][-1].text
    assert "<memory>" not in second_msg


async def test_extractor_receives_pre_injection_input(db):
    """The extractor must see the user's original text, not the mutated turn
    with the <memory> block prepended — otherwise it re-extracts facts it
    just recalled."""
    store = MemoryStore(db)
    await store.add_fact("Tom's report is due each Friday", "project")

    received: list[str] = []

    class SpyExtractor:
        async def extract(self, run_id, user_input, reply):
            received.append(user_input)

    provider = ScriptedProvider([LLMResponse(text="ok")])
    agent = _agent_for(provider, store, memory_extractor=SpyExtractor())
    await agent.run_turn(Session(), "what's due for the report?")

    # Injection happened (first turn, matching fact) ...
    assert "<memory>" in provider.sent[0][0].text
    # ... but the extractor got the untouched input.
    assert received == ["what's due for the report?"]
    assert "<memory>" not in received[0]


async def test_first_turn_freezes_profile_and_splits_halves(db):
    """End-to-end split: the first user message carries the similar fact
    and not the always-on one; the system prompt ENDS with the frozen
    profile (static prompt first); the snapshot is persisted once; the
    memory_injected trace carries both parts' chars."""
    from harness.store.runs import RunStore
    from harness.store.sessions import SessionStore

    store = MemoryStore(db)
    sessions, runs = SessionStore(db), RunStore(db)
    await store.add_fact("Tom is a beekeeper", "profile")
    await store.add_fact("Tom's report is due each Friday", "project")

    session = await Session.create(sessions, channel="cli")
    provider = ScriptedProvider([LLMResponse(text="ok")])
    agent = _agent_for(provider, store, run_store=runs)
    await agent.run_turn(session, "what's due for the report?")

    user_msg = provider.sent[0][0].text
    assert "<memory>" in user_msg and "Tom's report is due each Friday" in user_msg
    assert "Tom is a beekeeper" not in user_msg and "<user_profile>" not in user_msg

    system = provider.systems[0]
    assert system.startswith("test\n\n") and system.endswith("</user_profile>")
    assert "Tom is a beekeeper" in system  # the frozen half, at the END

    # Persisted exactly once, and the attribute matches the column.
    assert await sessions.get_memory_profile(session.id) == session.memory_profile
    assert session.memory_profile.startswith("<user_profile>")

    cur = await db.conn.execute(
        "SELECT data_json FROM run_events WHERE kind = 'memory_injected'"
    )
    payload = json.loads((await cur.fetchone())["data_json"])
    assert payload["profile_chars"] == len(session.memory_profile) > 0
    # block_chars is the real <memory> section's length: the user message
    # is exactly `{block}\n\n{input}`.
    assert payload["block_chars"] == (
        len(user_msg) - len("\n\n") - len("what's due for the report?")
    ) > 0


async def test_empty_profile_persisted_as_empty_string_not_null(db):
    """"Snapshotted, no facts" stores "" so it can never be confused with
    a legacy NULL — and a falsy snapshot sends the base prompt."""
    from harness.store.sessions import SessionStore

    store = MemoryStore(db)
    sessions = SessionStore(db)
    session = await Session.create(sessions, channel="cli")
    provider = ScriptedProvider([LLMResponse(text="ok")])
    agent = _agent_for(provider, store)
    await agent.run_turn(session, "hello")

    assert session.memory_profile == ""
    assert await sessions.get_memory_profile(session.id) == ""  # not NULL
    assert provider.systems[0] == "test"  # falsy → base prompt exactly


async def test_profile_frozen_byte_stable_across_runs(db):
    """The daemon shape: a FRESH Agent per run over the stored session.
    A preference saved between runs must not move a single byte of the
    system prompt — the snapshot is per SESSION, never per run."""
    from harness.store.sessions import SessionStore

    store = MemoryStore(db)
    sessions = SessionStore(db)
    await store.add_fact("Tom prefers tea", "preference")

    session = await Session.create(sessions, channel="cli")
    p1 = ScriptedProvider([LLMResponse(text="ok")])
    await _agent_for(p1, store).run_turn(session, "hello")
    frozen = p1.systems[0]
    assert "Tom prefers tea" in frozen
    mid = await sessions.get_memory_profile(session.id)
    assert mid and frozen == f"test\n\n{mid}"

    # Mid-chat fact save: takes effect NEXT session, never this chat.
    await store.add_fact("Tom prefers flat white", "preference")

    resumed = await Session.resume(sessions, session.id)
    p2 = ScriptedProvider([LLMResponse(text="ok2")])
    await _agent_for(p2, store).run_turn(resumed, "second turn")

    assert p2.systems[0] == frozen, (  # byte-identical to run 1
        "sessions.memory_profile is FROZEN per session — never re-render it "
        "(no counts/now-text); NULL = legacy row, \"\" = snapshotted-empty"
    )
    assert "flat white" not in p2.systems[0]
    assert await sessions.get_memory_profile(session.id) == mid  # never rewritten
    assert "<memory>" not in p2.sent[0][-1].text  # no re-injection either


async def test_empty_session_refreezes_after_freeze_without_message(db):
    """The freeze-window self-heal, pinned.

    A run that froze the profile and then died BEFORE add_user leaves the
    column written with zero messages behind it; a fact saved in that gap
    is absent from the stale snapshot. The next run re-freezes (the empty-
    messages gate in _inject_memory fires again) and overwrites the column,
    so the model's FIRST sight of the profile is the fresh render. The
    stale value is never shown to anyone: _step_chat — the only model call
    in run_turn — runs strictly after add_user, so a death before it means
    no model ever read the stale profile. (Not a shared gate on _seed_turn:
    that re-seeds the ledger from ALL prior user messages every turn.)"""
    from harness.store.sessions import SessionStore

    store = MemoryStore(db)
    sessions = SessionStore(db)
    await store.add_fact("Tom is a beekeeper", "profile")

    class DeadBeforeFirstMessage(Session):
        async def add_user(
            self, text: str, attachments: list[dict] | None = None
        ) -> None:
            raise RuntimeError("dies after the freeze, before the first message")

    session = await DeadBeforeFirstMessage.create(sessions, channel="cli")
    never = ScriptedProvider([])  # chat() is never reached — death comes first
    with pytest.raises(RuntimeError):
        await _agent_for(never, store).run_turn(session, "hello")
    assert never.systems == []  # no model call ever saw the stale profile

    stale = await sessions.get_memory_profile(session.id)
    assert stale and stale.startswith("<user_profile>")  # column written...
    assert "beekeeper" in stale
    assert await sessions.load_messages(session.id) == []  # ...zero messages

    # A fact saved inside the gap.
    await store.add_fact("Tom keeps bees on the roof", "profile")

    # Resume reads the stale snapshot verbatim — nothing re-renders on read.
    resumed = await Session.resume(sessions, session.id)
    assert resumed is not None and resumed.memory_profile == stale

    provider = ScriptedProvider([LLMResponse(text="ok")])
    await _agent_for(provider, store).run_turn(resumed, "hello again")

    # Re-frozen: the new fact is in the system string the model first reads,
    # the attribute and the column hold the new render, and the send matches.
    assert "Tom keeps bees on the roof" in provider.systems[0]
    assert provider.systems[0] == f"test\n\n{resumed.memory_profile}"
    new = await sessions.get_memory_profile(session.id)
    assert new != stale and new == resumed.memory_profile
    assert "beekeeper" in new and "on the roof" in new


async def test_legacy_session_gets_base_prompt_exactly(db):
    """NULL memory_profile = legacy session: its first message already
    carries the old combined block, so the system prompt stays byte-
    identical to the base, no snapshot is written, nothing is re-injected."""
    from harness.store.sessions import SessionStore

    store = MemoryStore(db)
    sessions = SessionStore(db)
    await store.add_fact("Tom is a beekeeper", "profile")
    old = await Session.create(sessions, channel="cli")
    # The pre-split shape: profile fact already baked into message 0.
    await old.add_user(
        "<memory>\nLong-term memory…\n- [profile, saved 2026-01-01] Tom is "
        "a beekeeper\n</memory>\n\nhello"
    )
    await old.add_assistant("hi")

    resumed = await Session.resume(sessions, old.id)
    assert resumed.memory_profile is None  # column NULL

    provider = ScriptedProvider([LLMResponse(text="ok")])
    await _agent_for(provider, store).run_turn(resumed, "and again")

    assert provider.systems[0] == "test"  # exactly the base prompt
    assert await sessions.get_memory_profile(old.id) is None  # never written
    assert "<memory>" not in provider.sent[0][-1].text  # never re-injected


async def test_every_step_of_a_tool_turn_sends_identical_system(db):
    """The frozen profile must not drift INSIDE a turn either: a multi-
    step tool loop sends the byte-identical system string at every step."""
    store = MemoryStore(db)
    await store.add_fact("Tom is a beekeeper", "profile")
    provider = ScriptedProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t1", name="echo", arguments={"text": "x"})],
            ),
            LLMResponse(text="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(EchoTool())
    session = Session()
    await _agent_for(provider, store, registry=registry).run_turn(session, "hello")

    assert len(provider.systems) == 2  # the tool loop made two model calls
    assert provider.systems[0] == provider.systems[1] == (
        f"test\n\n{session.memory_profile}"
    )


async def test_profile_and_memory_urls_do_not_seed_egress(db):
    """Moving the always-on facts to the system prompt opens no egress
    hole: the system prompt is NEVER seeded, and the remaining <memory>
    half is still stripped from the seed — including when a fresh Agent
    re-seeds from history on a later run (the daemon shape)."""
    store = MemoryStore(db)
    await store.add_fact("Tom's portfolio lives at https://profile.test/tom", "profile")
    await store.add_fact("Tom's club page is https://memory.test/club", "project")

    provider = ScriptedProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[
                    # Only frozen into the system prompt:
                    ToolCall(id="t1", name="web_fetch", arguments={"url": "https://profile.test/tom"}),
                    # Only inside the injected <memory> block:
                    ToolCall(id="t2", name="web_fetch", arguments={"url": "https://memory.test/club"}),
                    # Named by the user — the one that may run:
                    ToolCall(id="t3", name="web_fetch", arguments={"url": "https://user.test/x"}),
                ],
            ),
            LLMResponse(text="turn one"),
            # Run 2, fresh Agent → fresh ledger: re-seeding from history
            # must not admit either injected URL.
            LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(id="t4", name="web_fetch", arguments={"url": "https://profile.test/tom"}),
                    ToolCall(id="t5", name="web_fetch", arguments={"url": "https://memory.test/club"}),
                ],
            ),
            LLMResponse(text="turn two"),
        ]
    )
    registry = ToolRegistry()
    registry.register(FakeFetchTool())
    session = Session()
    await _agent_for(provider, store, registry=registry).run_turn(
        session, "check https://user.test and the club page"
    )

    # Sanity: both URLs really were in front of the model, one per half.
    assert "https://profile.test/tom" in provider.systems[0]
    assert "https://memory.test/club" in provider.sent[0][0].text

    results = {r.tool_call_id: r for r in session.messages[2].tool_results}
    assert results["t1"].is_error is True and "new destination" in results["t1"].content
    assert results["t2"].is_error is True and "new destination" in results["t2"].content
    assert results["t3"].is_error is False, results["t3"].content

    await _agent_for(provider, store, registry=registry).run_turn(
        session, "fetch those two pages again"
    )
    results2 = {r.tool_call_id: r for r in session.messages[-2].tool_results}
    assert results2["t4"].is_error is True and "new destination" in results2["t4"].content
    assert results2["t5"].is_error is True and "new destination" in results2["t5"].content


# -- tools --------------------------------------------------------------------


async def test_memory_tools_round_trip(db):
    store = MemoryStore(db)
    remember, recall, forget = memory_tools(store)

    out = await remember.run({"content": "Tom prefers tea", "category": "preference"})
    assert "Remembered" in out
    out = await remember.run({"content": "Tom prefers tea", "category": "preference"})
    assert "Already known" in out

    out = await recall.run({"query": "tea preference"})
    assert "Tom prefers tea" in out

    fact_id = (await store.list_facts())[0].id
    assert "Forgot 1" in await forget.run({"fact_ids": [fact_id]})
    assert "No matching facts" in await recall.run({"query": "tea preference"})
    assert "Error" in await forget.run({"fact_ids": [fact_id]})


async def test_recall_tool_stamps_query_not_list(db):
    store = MemoryStore(db)
    _remember, recall, _forget = memory_tools(store)
    fid, _ = await store.add_fact("Tom prefers strong coffee", "preference")

    # No-query listing is browsing — must NOT stamp, or the weekly hygiene
    # sweep would erase the never-recalled signal.
    out = await recall.run({})
    assert "last recalled never" in out
    assert (await store.get(fid)).last_recalled_at is None

    # Query recall stamps; output carries the saved date.
    out = await recall.run({"query": "coffee"})
    assert "saved 20" in out  # saved YYYY-MM-DD
    assert (await store.get(fid)).last_recalled_at is not None


async def test_recall_pagination_footer(db):
    store = MemoryStore(db)
    _remember, recall, _forget = memory_tools(store)
    recall.LIST_ALL_CAP = 3  # instance override — page size 3 for the test
    ids = []
    for n in range(7):
        fid, _ = await store.add_fact(f"Tom pagination fact {n}", "project")
        ids.append(fid)

    page1 = await recall.run({})
    assert page1.count("Tom pagination fact") == 3
    assert "showing 1–3 of 7 active facts" in page1
    assert "offset=3" in page1

    page2 = await recall.run({"offset": 3})
    assert "showing 4–6 of 7 active facts" in page2
    assert "offset=6" in page2

    # Last page: one fact, no footer.
    page3 = await recall.run({"offset": 6})
    assert page3.count("Tom pagination fact") == 1
    assert "showing" not in page3

    assert "No facts at offset 50" in await recall.run({"offset": 50})

    # Paging is browsing, not recall — never stamps last_recalled_at.
    for fid in ids:
        assert (await store.get(fid)).last_recalled_at is None


def test_hygiene_playbook_pages_recall():
    """The hygiene sweep must page through recall's listing — a playbook that
    still assumes one call sees every fact silently misses the oldest facts
    (the prune candidates) once the store exceeds one page."""
    from pathlib import Path

    import harness.playbooks as playbooks

    text = (Path(playbooks.__file__).parent / "memory_hygiene.md").read_text()
    assert "offset" in text
    assert "paged" in text


async def test_forget_tool_bulk_summary(db):
    store = MemoryStore(db)
    remember, _recall, forget = memory_tools(store)
    for n in range(3):
        await remember.run({"content": f"Tom fact {n}", "category": "profile"})
    ids = [f.id for f in await store.list_facts()]

    # One call, mixing real ids with an unknown one → single batch summary.
    out = await forget.run({"fact_ids": ids + [9999]})
    assert "Forgot 3" in out
    assert "9999" in out  # reported as already inactive/unknown
    assert await store.list_facts() == []


async def test_store_forget_many(db):
    store = MemoryStore(db)
    a, _ = await store.add_fact("Tom fact a", "profile")
    b, _ = await store.add_fact("Tom fact b", "profile")
    c, _ = await store.add_fact("Tom fact c", "profile")
    await store.forget(c)  # already inactive

    forgotten, missing = await store.forget_many([a, b, c, 4242])
    assert forgotten == [a, b]
    assert missing == [c, 4242]
    assert await store.list_facts() == []


@needs_vec
async def test_forget_many_removes_vec_rows(db):
    store = MemoryStore(db, FakeEmbedder())
    await store.setup()
    a, _ = await store.add_fact("Tom fact a", "profile")
    b, _ = await store.add_fact("Tom fact b", "profile")

    forgotten, missing = await store.forget_many([a, b])
    assert forgotten == [a, b] and missing == []
    cur = await db.conn.execute("SELECT COUNT(*) AS n FROM vec_facts")
    assert (await cur.fetchone())["n"] == 0


async def test_extractor_only_built_for_interactive_runs(db, monkeypatch):
    """Composition guard (v3 audit Q1.4): supervised/restricted runs process
    untrusted trigger content — build_agent must not wire the post-turn
    extractor into them, or injected email/web text auto-writes facts that are
    injected into every future session. Interactive chat keeps extraction."""
    from harness.core.policy import Autonomy
    from harness.composition import build_agent

    monkeypatch.setenv("HARNESS_LOCAL_URL", "http://127.0.0.1:8080")
    store = MemoryStore(db)
    await store.setup()

    interactive, _ = build_agent(memory=store, autonomy=Autonomy.INTERACTIVE)
    assert interactive.memory_extractor is not None

    for autonomy in (Autonomy.SUPERVISED, Autonomy.RESTRICTED):
        agent, _ = build_agent(memory=store, autonomy=autonomy)
        assert agent.memory_extractor is None, autonomy
        # The gated path stays: `remember` is still registered for the model.
        assert agent.registry.get("remember") is not None
