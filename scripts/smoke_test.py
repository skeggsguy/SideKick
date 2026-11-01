"""Smoke test: drive one agent turn non-interactively.

Asks the agent a question it can only answer by calling read_file on
scratch/hello.txt, then checks the passphrase made it into the answer,
that text streamed via deltas, and that usage was tracked.

Runs on the local lane (HARNESS_LOCAL_URL, no API key needed). Pass
--sonnet to smoke the opt-in cloud chat path instead (needs
ANTHROPIC_API_KEY, costs cents). Calls the real model — run to verify,
never in loops.

Run with: uv run python scripts/smoke_test.py [--sonnet]
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from harness.core.session import Session
from harness.main import build_agent

EXPECTED = "blue-narwhal-42"


async def main() -> int:
    load_dotenv()
    chat_model = "sonnet" if "--sonnet" in sys.argv[1:] else None
    # no store: in-memory session, no run records
    agent, model = build_agent(chat_model=chat_model)

    tool_calls: list[str] = []
    deltas: list[str] = []
    agent.on_tool_call = lambda name, args: tool_calls.append(name)
    agent.on_text_delta = lambda text: deltas.append(text)

    session = Session()
    answer = await agent.run_turn(
        session,
        "Read the file scratch/hello.txt and tell me the secret passphrase.",
    )

    usage = agent.last_run["usage"] if agent.last_run else None
    print(f"\n--- model: {model} ---")
    print(f"tool calls: {tool_calls}")
    print(f"answer: {answer}")
    print(f"streamed deltas: {len(deltas)}")
    if usage:
        print(
            f"usage: {usage.input_tokens} in / {usage.output_tokens} out · "
            f"cache read {usage.cache_read_input_tokens} / "
            f"written {usage.cache_creation_input_tokens}"
        )

    ok = (
        "read_file" in tool_calls
        and EXPECTED in (answer or "")
        and len(deltas) > 1  # proves streaming, not a single blob
        and usage is not None
        and usage.output_tokens > 0
    )
    print("\nPASS ✅" if ok else "\nFAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
