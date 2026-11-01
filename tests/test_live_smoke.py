"""Live smoke tier: real model servers, run manually before releases.

Deselected by default (pyproject addopts `-m 'not live'`); run with:

    uv run pytest -m live

The main lane is the local llama.cpp server (HARNESS_LOCAL_URL) — this tier
smoke-tests that server: the second-turn cache check verifies the -np 1
server's prefix "bookmark" survives a turn. The Apple FM utilities run for
real too. The Sonnet chat test additionally needs ANTHROPIC_API_KEY and
costs real cents — it is skipped without one. `.env` HAS the key, so the bare
marker spends: run the live gate scoped to one test id.

Builds agents with no store, so nothing touches the live harness.db and no
run rows are written. Verifies the P1 invariants end-to-end: streaming,
tool dispatch, usage tracking, and prompt-cache health (a cold cache here
would mean a prefix invalidator crept in — see PLAN-v2 §7).
"""

from __future__ import annotations

import os

import pytest
from dotenv import dotenv_values, load_dotenv

from harness.core.session import Session
from harness.composition import build_agent

# The skip check reads .env WITHOUT loading it: this module is collected on
# every offline run too, and a module-level load_dotenv() would leak the real
# HARNESS_LOCAL_URL / HARNESS_EFFORT / HARNESS_MAX_TOKENS into every other
# test's environment. The real load happens in the fixture below, which only
# runs when a live test is selected.
_ENV = {**dotenv_values(), **os.environ}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _ENV.get("HARNESS_LOCAL_URL"),
        reason="HARNESS_LOCAL_URL not set (the live tier drives the local server)",
    ),
]


@pytest.fixture(autouse=True, scope="module")
def _load_env():
    load_dotenv()

PASSPHRASE = "blue-narwhal-42"  # fixture in scratch/hello.txt


async def test_streamed_turn_with_tool():
    agent, _ = build_agent()
    tool_calls: list[str] = []
    deltas: list[str] = []
    agent.on_tool_call = lambda name, args: tool_calls.append(name)
    agent.on_text_delta = lambda text: deltas.append(text)

    answer = await agent.run_turn(
        Session(),
        "Read the file scratch/hello.txt and tell me the secret passphrase.",
    )

    assert "read_file" in tool_calls
    assert PASSPHRASE in (answer or "")
    assert len(deltas) > 1  # streamed, not one blob
    usage = agent.last_run["usage"]
    assert usage.output_tokens > 0


@pytest.mark.skipif(
    not _ENV.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set (Sonnet chat smoke costs real cents)",
)
async def test_sonnet_chat_turn():
    """The one opt-in cloud path: a Sonnet chat turn end-to-end."""
    agent, model = build_agent(chat_model="sonnet")
    assert model == "claude-sonnet-5" and not agent.provider.is_local
    answer = await agent.run_turn(Session(), "Reply with the single word: ping")
    assert answer and agent.last_run["usage"].output_tokens > 0


async def test_prompt_cache_is_warm_on_second_turn():
    agent, _ = build_agent()
    session = Session()
    await agent.run_turn(session, "Reply with the single word: ping")
    await agent.run_turn(session, "Reply with the single word: pong")

    usage = agent.last_run["usage"]
    assert usage.cache_read_input_tokens > 0, (
        "second turn read nothing from the prompt cache — "
        "a silent prefix invalidator crept in (PLAN-v2 §7)"
    )
