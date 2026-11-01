"""Durable memory: facts about the user + episode summaries, with optional
vector indexes.

Plain rows live in the `facts`/`episodes` tables (migration 002). When the
sqlite-vec extension loads, setup() adds vec0 virtual tables and recall uses
cosine KNN; otherwise everything still works on keyword search. Forgetting
deactivates a fact (audit-friendly) — rows are never deleted, only their
vector index entries are.
"""

from __future__ import annotations

import asyncio
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..store.db import Database, utcnow
from .embeddings import Embedder

CATEGORIES = (
    "profile", "preference", "project", "contact", "routine",
    "interest", "procedure", "job-config", "ops-lesson",
)

# Cosine similarity above which a new fact is treated as a duplicate of an
# existing one (the existing row is touched instead of inserting).
DEDUPE_SIMILARITY = 0.92


@dataclass
class Fact:
    id: int
    content: str
    category: str
    confidence: float
    active: bool
    created_at: str
    updated_at: str
    #: When the fact last surfaced via similarity search; None = never recalled.
    last_recalled_at: str | None = None


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


class MemoryStore:
    def __init__(self, db: Database, embedder: Embedder | None = None) -> None:
        self.db = db
        self.embedder = embedder
        self.dim = embedder.dim if embedder is not None else 768
        self.vec_available = False
        #: Why vectors are off (extension missing, load failed) — for traces.
        self.vec_error: str | None = None
        self._index_lock = asyncio.Lock()  # one backfill/reindex at a time
        self._put_lock = asyncio.Lock()  # delete+insert of one vector is atomic

    async def setup(self) -> None:
        """Load sqlite-vec and create the vector tables; degrade on failure."""
        try:
            import sqlite_vec

            await self.db.conn.enable_load_extension(True)
            try:
                await self.db.conn.load_extension(sqlite_vec.loadable_path())
            finally:
                await self.db.conn.enable_load_extension(False)
            for table, key in (("vec_facts", "fact_id"), ("vec_episodes", "episode_id")):
                await self.db.conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
                    f"{key} INTEGER PRIMARY KEY, "
                    f"embedding float[{self.dim}] distance_metric=cosine)"
                )
            await self.db.conn.commit()
            self.vec_available = True
        except (ImportError, AttributeError, sqlite3.OperationalError) as e:
            self.vec_error = f"{type(e).__name__}: {e}"

    # -- write ---------------------------------------------------------------

    async def add_fact(
        self,
        content: str,
        category: str,
        confidence: float = 0.8,
        source_run_id: str | None = None,
    ) -> tuple[int, bool]:
        """Store a fact; returns (fact_id, created). created=False means it
        deduped against an existing fact, whose updated_at was refreshed."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown fact category {category!r}")
        content = content.strip()
        now = utcnow()

        existing = await self._find_duplicate(content)
        if existing is not None:
            await self.db.conn.execute(
                "UPDATE facts SET updated_at = ?, confidence = MAX(confidence, ?) "
                "WHERE id = ?",
                (now, confidence, existing),
            )
            await self.db.conn.commit()
            return existing, False

        cur = await self.db.conn.execute(
            "INSERT INTO facts (content, category, confidence, source_run_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (content, category, confidence, source_run_id, now, now),
        )
        fact_id = cur.lastrowid
        await self._index_fact(fact_id, content)
        await self.db.conn.commit()
        return fact_id, True

    async def _find_duplicate(self, content: str) -> int | None:
        # Active rows only — restating a forgotten fact must create a fresh
        # fact, never resurrect the one the user explicitly dropped. (The
        # vector branch is already active-only via get().)
        cur = await self.db.conn.execute(
            "SELECT id FROM facts WHERE content = ? AND active = 1 LIMIT 1",
            (content,),
        )
        row = await cur.fetchone()
        if row is not None:
            return row["id"]
        for fact_id, distance in await self._knn("vec_facts", "fact_id", content, k=1):
            if 1.0 - distance >= DEDUPE_SIMILARITY and await self.get(fact_id):
                return fact_id
        return None

    async def _index_fact(self, fact_id: int, content: str) -> None:
        vec = await self._embed_one(content)
        if vec is not None:
            await self._put_vector(fact_id, vec)

    async def _put_vector(self, fact_id: int, vec: list[float]) -> None:
        # vec0 rejects INSERT OR REPLACE: delete first, under a lock (a
        # backfill can index a fact while add_fact awaits its own embed).
        async with self._put_lock:
            await self.db.conn.execute(
                "DELETE FROM vec_facts WHERE fact_id = ?", (fact_id,)
            )
            await self.db.conn.execute(
                "INSERT INTO vec_facts (fact_id, embedding) VALUES (?, ?)",
                (fact_id, _pack(vec)),
            )

    async def update_fact(
        self,
        fact_id: int,
        *,
        content: str | None = None,
        category: str | None = None,
    ) -> bool:
        """Edit an active fact in place; None = leave unchanged. Returns False
        for an unknown or inactive id. Re-indexes the vector when content
        changes (no-op without an embedder)."""
        if content is None and category is None:
            raise ValueError("nothing to update")
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"unknown fact category {category!r}")
        sets, params = ["updated_at = ?"], [utcnow()]
        if content is not None:
            content = content.strip()
            sets.append("content = ?")
            params.append(content)
        if category is not None:
            sets.append("category = ?")
            params.append(category)
        cur = await self.db.conn.execute(
            f"UPDATE facts SET {', '.join(sets)} WHERE id = ? AND active = 1",
            (*params, fact_id),
        )
        if cur.rowcount == 0:
            return False
        if content is not None:
            # vec0 rejects INSERT OR REPLACE on an existing key — delete first.
            if self.vec_available:
                await self.db.conn.execute(
                    "DELETE FROM vec_facts WHERE fact_id = ?", (fact_id,)
                )
            await self._index_fact(fact_id, content)
        await self.db.conn.commit()
        return True

    async def forget(self, fact_id: int) -> bool:
        cur = await self.db.conn.execute(
            "UPDATE facts SET active = 0, updated_at = ? WHERE id = ? AND active = 1",
            (utcnow(), fact_id),
        )
        if cur.rowcount == 0:
            return False
        if self.vec_available:
            await self.db.conn.execute(
                "DELETE FROM vec_facts WHERE fact_id = ?", (fact_id,)
            )
        await self.db.conn.commit()
        return True

    async def forget_many(self, ids: list[int]) -> tuple[list[int], list[int]]:
        """Deactivate several facts in one commit so a whole prune is a single
        approval. Returns (forgotten, missing): missing = ids that weren't
        active (already inactive or unknown)."""
        forgotten: list[int] = []
        missing: list[int] = []
        now = utcnow()
        for fact_id in ids:
            cur = await self.db.conn.execute(
                "UPDATE facts SET active = 0, updated_at = ? "
                "WHERE id = ? AND active = 1",
                (now, fact_id),
            )
            if cur.rowcount == 0:
                missing.append(fact_id)
                continue
            if self.vec_available:
                await self.db.conn.execute(
                    "DELETE FROM vec_facts WHERE fact_id = ?", (fact_id,)
                )
            forgotten.append(fact_id)
        await self.db.conn.commit()
        return forgotten, missing

    async def add_episode(self, run_id: str | None, summary: str) -> int:
        cur = await self.db.conn.execute(
            "INSERT INTO episodes (run_id, summary, created_at) VALUES (?, ?, ?)",
            (run_id, summary.strip(), utcnow()),
        )
        episode_id = cur.lastrowid
        vec = await self._embed_one(summary)
        if vec is not None:
            await self.db.conn.execute(
                "INSERT OR REPLACE INTO vec_episodes (episode_id, embedding) "
                "VALUES (?, ?)",
                (episode_id, _pack(vec)),
            )
        await self.db.conn.commit()
        return episode_id

    async def backfill_vectors(self) -> int:
        """Index active facts that have no vector yet (stored while Ollama was
        down). Returns how many were indexed; 0, never a raise, when down."""
        return await self._index_facts(
            "SELECT id, content FROM facts WHERE active = 1 "
            "AND id NOT IN (SELECT fact_id FROM vec_facts)"
        )

    async def reindex_all(self) -> int:
        """Re-embed EVERY active fact and rebuild vec_facts (model change).
        Embeds first, so a down Ollama leaves the old index; one commit."""
        return await self._index_facts(
            "SELECT id, content FROM facts WHERE active = 1", rebuild=True
        )

    async def _index_facts(self, select_sql: str, *, rebuild: bool = False) -> int:
        if not self.vec_available or self.embedder is None:
            return 0
        # Serialized: overlapping backfills (extraction runs after every turn)
        # would select the same rows and collide on vec0's primary key.
        async with self._index_lock:
            rows = await (await self.db.conn.execute(select_sql)).fetchall()
            if not rows and not rebuild:
                return 0
            vectors = await self.embedder.embed([r["content"] for r in rows])
            if vectors is None:
                return 0
            if rebuild:
                await self.db.conn.execute("DELETE FROM vec_facts")
            for row, vec in zip(rows, vectors):
                await self._put_vector(row["id"], vec)
            await self.db.conn.commit()
            return len(rows)

    # -- read ----------------------------------------------------------------

    async def get(self, fact_id: int) -> Fact | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM facts WHERE id = ? AND active = 1", (fact_id,)
        )
        row = await cur.fetchone()
        return _to_fact(row) if row is not None else None

    async def list_facts(
        self, category: str | None = None, include_inactive: bool = False
    ) -> list[Fact]:
        sql = "SELECT * FROM facts"
        clauses, params = [], []
        if not include_inactive:
            clauses.append("active = 1")
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        cur = await self.db.conn.execute(sql, params)
        return [_to_fact(r) for r in await cur.fetchall()]

    async def count_active(self) -> int:
        cur = await self.db.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE active = 1"
        )
        return (await cur.fetchone())[0]

    async def count_indexed(self) -> int:
        """Active facts that have a vector (0 when vectors are off)."""
        if not self.vec_available:
            return 0
        sql = "SELECT COUNT(*) FROM facts WHERE active = 1 AND id IN (SELECT fact_id FROM vec_facts)"
        return (await (await self.db.conn.execute(sql)).fetchone())[0]

    async def recent_facts(self, days: int = 7, limit: int = 30) -> list[Fact]:
        """Active facts touched in the last `days`, newest-first — the
        extractor's dup-blindness window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.db.conn.execute(
            "SELECT * FROM facts WHERE active = 1 AND updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (cutoff, limit),
        )
        return [_to_fact(r) for r in await cur.fetchall()]

    async def always_on(
        self, categories: tuple[str, ...] = ("profile", "preference"), limit: int = 20
    ) -> list[Fact]:
        """The facts injected into every session start, newest-first.

        The final `id` tie-break makes the order total: equal
        confidence/updated_at rows must render in a fixed order, or the
        frozen per-session profile is not byte-stable."""
        marks = ",".join("?" * len(categories))
        cur = await self.db.conn.execute(
            f"SELECT * FROM facts WHERE active = 1 AND category IN ({marks}) "
            "ORDER BY confidence DESC, updated_at DESC, id LIMIT ?",
            (*categories, limit),
        )
        return [_to_fact(r) for r in await cur.fetchall()]

    async def search(
        self, query: str, k: int = 8, *, mark_recalled: bool = False
    ) -> list[Fact]:
        """Top-k facts by cosine similarity; keyword fallback without vectors.

        mark_recalled=True stamps last_recalled_at on the returned facts —
        pass it only where a fact genuinely matched a query (memory-block
        injection, the recall tool), never from browsing/listing surfaces.
        The returned Fact objects carry the PRE-stamp value on purpose: the
        hygiene run wants to see when a fact last mattered before today.
        """
        facts = []
        for fact_id, _dist in await self._knn("vec_facts", "fact_id", query, k=k * 2):
            fact = await self.get(fact_id)
            if fact is not None:
                facts.append(fact)
            if len(facts) == k:
                break
        if not facts:
            facts = await self._keyword_search(query, k)
        if mark_recalled and facts:
            marks = ",".join("?" * len(facts))
            await self.db.conn.execute(
                f"UPDATE facts SET last_recalled_at = ? WHERE id IN ({marks})",
                (utcnow(), *[f.id for f in facts]),
            )
            await self.db.conn.commit()
        return facts

    async def _keyword_search(self, query: str, k: int) -> list[Fact]:
        terms = [
            t for t in "".join(c if c.isalnum() else " " for c in query.lower()).split()
            if len(t) > 2
        ][:8]
        if not terms:
            return []
        # Rank by how many distinct terms match, then recency.
        hit = "(CASE WHEN lower(content) LIKE ? THEN 1 ELSE 0 END)"
        score = " + ".join([hit] * len(terms))
        patterns = [f"%{t}%" for t in terms]
        cur = await self.db.conn.execute(
            f"SELECT *, ({score}) AS score FROM facts WHERE active = 1 "
            f"AND ({score}) > 0 ORDER BY score DESC, updated_at DESC LIMIT ?",
            (*patterns, *patterns, k),
        )
        return [_to_fact(r) for r in await cur.fetchall()]

    # -- vector plumbing -------------------------------------------------------

    async def _embed_one(self, text: str) -> list[float] | None:
        if not self.vec_available or self.embedder is None:
            return None
        vectors = await self.embedder.embed([text])
        return vectors[0] if vectors else None

    async def _knn(
        self, table: str, key: str, text: str, k: int
    ) -> list[tuple[int, float]]:
        vec = await self._embed_one(text)
        if vec is None:
            return []
        cur = await self.db.conn.execute(
            f"SELECT {key}, distance FROM {table} "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (_pack(vec), k),
        )
        return [(row[key], row["distance"]) for row in await cur.fetchall()]


def _to_fact(row) -> Fact:
    return Fact(
        id=row["id"],
        content=row["content"],
        category=row["category"],
        confidence=row["confidence"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_recalled_at=row["last_recalled_at"],
    )
