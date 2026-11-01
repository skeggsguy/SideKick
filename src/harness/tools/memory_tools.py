"""Memory tools: the agent reads and writes its own long-term memory.

remember/forget mutate the local memory store (MUTATING_LOCAL — one "always"
approval makes them frictionless); recall is read-only. Automatic extraction
also runs after every turn, so remember is for facts the user explicitly asks
to keep or that matter mid-conversation.
"""

from __future__ import annotations

from typing import Any

from ..core.policy import ActionClass
from ..memory.store import CATEGORIES, MemoryStore
from .base import Tool


class RememberTool(Tool):
    name = "remember"
    description = (
        "Save a durable fact about the user to long-term memory (it will be "
        "recalled in future sessions). Write it third-person and "
        "self-contained, e.g. 'Tom prefers briefings before 7:30am.'"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to remember."},
            "category": {
                "type": "string",
                "enum": list(CATEGORIES),
                "description": "What kind of fact this is.",
            },
        },
        "required": ["content", "category"],
    }
    classification = ActionClass.MUTATING_LOCAL

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, args: dict[str, Any]) -> str:
        fact_id, created = await self.store.add_fact(args["content"], args["category"])
        if created:
            return f"Remembered (fact {fact_id})."
        return f"Already known — refreshed existing fact {fact_id}."


class RecallTool(Tool):
    name = "recall"
    description = (
        "Search long-term memory: facts about the user, their contacts, projects "
        "and preferences, and ops lessons (how a past task was best done — which "
        "site or tool worked, what failed). Call it before drafting to a person, "
        "before a multi-step task you've likely done before, and for broad "
        "questions about the user — the auto-injected <memory> block is only a "
        "small slice, so search several angles. Omit query to list ALL facts "
        "(100 per page via offset)."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for; omit to list everything.",
            },
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": [],
    }
    classification = ActionClass.READ_ONLY

    # Cap for the no-query listing — keeps the tool_result bounded.
    LIST_ALL_CAP = 100

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, args: dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        footer = ""
        if query:
            facts = await self.store.search(query, mark_recalled=True)
        else:
            # Listing everything is browsing, not recall — never stamps
            # last_recalled_at, or the weekly hygiene sweep would erase the
            # never-recalled signal it exists to read.
            offset = max(0, int(args.get("offset", 0)))
            all_facts = await self.store.list_facts()
            total = len(all_facts)
            facts = all_facts[offset : offset + self.LIST_ALL_CAP]
            if total and not facts:
                return f"No facts at offset {offset} — only {total} active facts."
            end = offset + len(facts)
            if end < total:
                footer = (
                    f"\nshowing {offset + 1}–{end} of {total} active facts — "
                    f"call recall again with offset={end} to continue"
                )
        if not facts:
            return "No matching facts in memory."
        return "\n".join(
            f"[{f.id}] ({f.category}, saved {f.updated_at[:10]}, last recalled "
            f"{f.last_recalled_at[:10] if f.last_recalled_at else 'never'}) "
            f"{f.content}"
            for f in facts
        ) + footer


class ForgetTool(Tool):
    name = "forget"
    description = (
        "Deactivate one or more facts in long-term memory by id (from recall). "
        "Use when the user corrects something, asks you to forget it, or when "
        "pruning during a memory review. Pass ALL ids to remove in a single "
        "call so they're approved together as one batch."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "fact_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "description": "Ids of the facts to forget (from recall).",
            },
        },
        "required": ["fact_ids"],
    }
    classification = ActionClass.MUTATING_LOCAL

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, args: dict[str, Any]) -> str:
        ids = [int(i) for i in args["fact_ids"]]
        forgotten, missing = await self.store.forget_many(ids)
        if not forgotten:
            return f"Error: no active facts among ids {missing}."
        parts = [f"Forgot {len(forgotten)} fact(s): {forgotten}."]
        if missing:
            parts.append(f"{len(missing)} already inactive/unknown: {missing}.")
        return " ".join(parts)


def memory_tools(store: MemoryStore) -> list[Tool]:
    return [RememberTool(store), RecallTool(store), ForgetTool(store)]
