"""Memory vector (re)indexing: backfill, `reindex --all`, and the
`harness memory reindex` CLI helper.

With ANY vector present, search() is KNN-only — a fact without a vector is
invisible until it is indexed. These pin the paths that index it.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from harness.main import _memory_reindex
from harness.memory import MemoryStore

from test_memory import FakeEmbedder, needs_vec


async def _vector_ids(db) -> set[int]:
    cur = await db.conn.execute("SELECT fact_id FROM vec_facts")
    return {row["fact_id"] for row in await cur.fetchall()}


async def _active_ids(db) -> set[int]:
    cur = await db.conn.execute("SELECT id FROM facts WHERE active = 1")
    return {row["id"] for row in await cur.fetchall()}


@needs_vec
async def test_unindexed_fact_invisible_to_knn_until_backfilled(db):
    """Mixed coverage (the live DB after the WSL cutover): an indexed fact
    makes search KNN-only, so the fact stored while Ollama was down never
    surfaces — until backfill_vectors() indexes it."""
    embedder = FakeEmbedder(
        {
            "Tom's dog is called Rex": [1, 0, 0, 0, 0, 0, 0, 0],
            "Tom shops at Woolworths for sparkling water": [0, 1, 0, 0, 0, 0, 0, 0],
            "supermarket sparkling water": [0, 0.95, 0.05, 0, 0, 0, 0, 0],
        }
    )
    store = MemoryStore(db, embedder)
    await store.setup()
    await store.add_fact("Tom's dog is called Rex", "profile")
    embedder.available = False
    shop_id, _ = await store.add_fact(
        "Tom shops at Woolworths for sparkling water", "interest"
    )
    embedder.available = True

    before = await store.search("supermarket sparkling water", k=3)
    assert shop_id not in [f.id for f in before], (
        "with ANY vector stored, search is KNN-only (keyword only when KNN is "
        "empty): a vector-less fact stays INVISIBLE until back-filled"
    )

    assert await store.backfill_vectors() == 1
    after = await store.search("supermarket sparkling water", k=1)
    assert [f.id for f in after] == [shop_id]
    assert await store.count_indexed() == await store.count_active() == 2


@needs_vec
async def test_reindex_all_recreates_a_vector_for_every_active_fact(db):
    embedder = FakeEmbedder()
    store = MemoryStore(db, embedder)
    await store.setup()
    a, _ = await store.add_fact("Tom fact a", "profile")
    embedder.available = False
    b, _ = await store.add_fact("Tom fact b", "routine")  # no vector
    embedder.available = True
    c, _ = await store.add_fact("Tom fact c", "contact")
    await store.forget(c)
    # An orphan vector (forgotten fact whose row survived) must not survive a rebuild.
    await db.conn.execute(
        "INSERT INTO vec_facts (fact_id, embedding) VALUES (?, ?)",
        (999, bytes(4 * embedder.dim)),
    )
    await db.conn.commit()

    # "New model": every text now embeds differently; the rebuild must store it.
    embedder.vectors = {
        "Tom fact a": [0, 0, 0, 0, 0, 0, 0, 1],
        "Tom fact b": [0, 0, 0, 0, 0, 0, 1, 0],
        "probe": [0, 0, 0, 0, 0, 0, 0.1, 0.9],
    }
    assert await store.reindex_all() == 2
    assert await _vector_ids(db) == {a, b}
    assert [f.id for f in await store.search("probe", k=1)] == [a]


@needs_vec
async def test_reindex_all_leaves_index_untouched_when_embedder_down(db):
    embedder = FakeEmbedder()
    store = MemoryStore(db, embedder)
    await store.setup()
    a, _ = await store.add_fact("Tom fact a", "profile")
    b, _ = await store.add_fact("Tom fact b", "profile")

    embedder.available = False
    assert await store.reindex_all() == 0
    assert await store.backfill_vectors() == 0
    assert await _vector_ids(db) == {a, b}


@needs_vec
async def test_concurrent_backfills_do_not_collide_on_vec0_keys(db):
    """Extraction backfills after every turn, so two runs can overlap and
    select the same unindexed rows. vec0 rejects a duplicate key (INSERT OR
    REPLACE included) — indexing is serialized, the second run finds nothing."""

    class YieldingEmbedder(FakeEmbedder):
        async def embed(self, texts):
            await asyncio.sleep(0.01)  # both callers select before either inserts
            return await super().embed(texts)

    embedder = YieldingEmbedder()
    store = MemoryStore(db, embedder)
    await store.setup()
    embedder.available = False
    ids = [(await store.add_fact(f"Tom fact {i}", "profile"))[0] for i in range(3)]
    embedder.available = True

    results = await asyncio.gather(store.backfill_vectors(), store.backfill_vectors())
    assert sorted(results) == [0, 3], (
        "_index_facts needs its asyncio.Lock: overlapping backfills insert the "
        "same rows across the embed await — vec0 UNIQUE fails even with delete-then-insert"
    )
    assert await _vector_ids(db) == set(ids)


@needs_vec
async def test_add_fact_racing_a_backfill_does_not_collide(db):
    """add_fact inserts its row, then awaits the embedder; a backfill that
    runs in that gap sees the new (unindexed) row and indexes it first. The
    single-fact write must then replace, not hit vec0's duplicate-key error."""

    class SlowEmbedder(FakeEmbedder):
        delays = iter([0.01, 0.06, 0.01])  # dup-check, add_fact's index, backfill

        async def embed(self, texts):
            await asyncio.sleep(next(self.delays))
            return await super().embed(texts)

    embedder = SlowEmbedder()
    store = MemoryStore(db, embedder)
    await store.setup()

    async def backfill_soon():
        await asyncio.sleep(0.015)  # after add_fact's INSERT, mid its embed
        return await store.backfill_vectors()

    (fact, _), backfilled = await asyncio.gather(
        store.add_fact("Tom fact raced", "profile"), backfill_soon()
    )
    assert backfilled == 1
    assert await _vector_ids(db) == {fact}


@needs_vec
@pytest.mark.parametrize("seed", range(5))
async def test_index_ops_keep_vectors_a_subset_of_active_facts(db, seed):
    """Invariant over random add/forget/outage sequences: vectors only ever
    cover active facts, backfill closes coverage, reindex_all equals it."""
    rng = random.Random(seed)
    embedder = FakeEmbedder()
    store = MemoryStore(db, embedder)
    await store.setup()
    for step in range(30):
        op = rng.choice(["add", "add", "forget", "outage", "backfill"])
        if op == "add":
            await store.add_fact(f"Tom fact {seed}-{step}", "profile")
        elif op == "forget" and (active := sorted(await _active_ids(db))):
            await store.forget(rng.choice(active))
        elif op == "outage":
            embedder.available = not embedder.available
        elif op == "backfill":
            await store.backfill_vectors()
        assert await _vector_ids(db) <= await _active_ids(db)
    embedder.available = True
    await store.backfill_vectors()
    assert await _vector_ids(db) == await _active_ids(db)
    assert await store.reindex_all() == len(await _active_ids(db))
    assert await _vector_ids(db) == await _active_ids(db)


@needs_vec
async def test_cli_reindex_reports_coverage(db, capsys):
    embedder = FakeEmbedder()
    store = MemoryStore(db, embedder)
    await store.setup()
    await store.add_fact("Tom fact a", "profile")
    embedder.available = False
    await store.add_fact("Tom fact b", "profile")
    embedder.available = True

    await _memory_reindex(store, rebuild=False)
    out = capsys.readouterr().out
    assert "back-filled 1 fact vector(s)" in out
    assert "coverage: 2 of 2 active facts have vectors" in out

    await _memory_reindex(store, rebuild=True)
    assert "re-embedded 2 fact vector(s)" in capsys.readouterr().out


@needs_vec
async def test_cli_reindex_exits_nonzero_when_ollama_down(db):
    class DownEmbedder(FakeEmbedder):
        last_error = "ConnectError: connection refused"

        async def embed(self, texts):
            return None

    store = MemoryStore(db, DownEmbedder())
    await store.setup()
    await store.add_fact("Tom fact a", "profile")  # stored without a vector
    with pytest.raises(SystemExit, match="embedder unavailable, index unchanged"):
        await _memory_reindex(store, rebuild=False)


async def test_cli_reindex_exits_nonzero_without_vec_extension(db):
    store = MemoryStore(db, FakeEmbedder())  # setup() not run: vectors off
    store.vec_error = "sqlite-vec missing"
    with pytest.raises(SystemExit, match="vectors off: sqlite-vec missing"):
        await _memory_reindex(store, rebuild=False)
