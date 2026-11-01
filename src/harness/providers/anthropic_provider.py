"""Anthropic (Claude) provider.

Translates the harness's internal Message list into Anthropic's content-block
format and back. This is the only place that knows Anthropic's wire shape.
Requests reach the actual cloud only through the opt-in Sonnet chat option
(composition.sonnet_chat_provider); everything else runs on the local lane.

Streaming + prompt caching: responses stream token-by-token, and the system
prompt carries a cache_control breakpoint. Tools render before system in the
prompt, so that one breakpoint caches tools + system together. Note: prefixes
under the model's minimum (~2048 tokens on Sonnet 5) silently don't cache —
watch usage.cache_read_input_tokens to verify caching once the prefix grows.

Extended thinking (Sonnet 5): adaptive thinking streams summarized thinking
deltas alongside text. Thinking blocks (text + signature) must be sent back
UNCHANGED on the next call within the same tool loop; across turns the API
ignores them, so we strip them there (pure input-token cost otherwise). The
local lane keeps them everywhere: its template re-renders them, and stripping
would break the llama.cpp prefix cache at every new turn.

Local lane (``base_url`` set): the same wire format against the llama.cpp
fork's Anthropic-compatible /v1/messages (Qwen3.8-Flash-Next). That server
IGNORES adaptive/disabled thinking and output_config, so effort travels as
explicit ``chat_template_kwargs`` (enable_thinking + reasoning_effort) plus an
``enabled`` thinking budget — never left to the template default.

Lanes (``lane="code"``): the fork's chat window treats a request WITHOUT an
``X-Lane: code`` header as chat — chat cuts a running code request off
(``overloaded_error``: HTTP 503 before streaming, an SSE error event after) and
holds code requests until the window after the last chat lapses. A code-lane
caller never stalls a chat; a cut call is re-sent (held server-side, so no
busy loop), bounded by LANE_CUT_RETRIES. No app caller selects it now: agentd's
background runs ride chat since 2026-10-25 (.claude/decisions.md).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

import anthropic

from ..core.types import LLMResponse, Message, ToolCall, Usage
from ..tools.base import Tool
from .base import LLMProvider, OnEvent, OnTextDelta, OnToolStart
from ._anthropic_wire import (  # re-exported for tests/callers
    LOCAL_ANSWER_HEADROOM,
    _local_effort_params,
    _mark_history_breakpoint,
    _to_anthropic_message,
    _to_api_messages,
)

# The opt-in Sonnet chat option (composition.sonnet_chat_provider — the one
# remaining cloud code path). Claude Sonnet 5 is the current speed/
# intelligence sweet spot for agentic tool use.
DEFAULT_MODEL = "claude-sonnet-5"

# The local lane's model name (override via HARNESS_LOCAL_MODEL); informational
# to the llama.cpp server, which serves whatever it loaded.
DEFAULT_LOCAL_MODEL = "qwen3.8-flash-next"

# Models that accept the adaptive `thinking` and `output_config.effort`
# request params. Never send them to anything else (only Sonnet 5 is
# reachable today: the local lane drives thinking via chat_template_kwargs).
THINKING_MODELS = frozenset({"claude-sonnet-5"})

# The effort vocabulary, shared by both lanes (the web UI's picker, the
# /api/chat body and HARNESS_EFFORT). "off" disables thinking; the others map
# to output_config.effort (cloud) or the chat template's reasoning_effort
# (local — whose template accepts exactly low/medium/xhigh).
EFFORTS = ("off", "low", "medium", "xhigh")

# Local lane with no effort configured: the template's own default, but sent
# explicitly (the server must never fall back to its implicit default).
LOCAL_DEFAULT_EFFORT = "xhigh"

# Local lane client settings: requests queue behind a long run on the -np 1
# server, and an SDK retry would enqueue a duplicate generation.
LOCAL_TIMEOUT_S = 1800

# The llama.cpp fork's lanes (its --chat-window): no header = chat, which
# preempts "code". Only "code" sends a header.
LANES = ("chat", "code")
# A code-lane call cut off by a chat is re-sent at most this many times; the
# re-send is held server-side until the chat window lapses, so each attempt
# means a fresh chat interrupted it. The pause guards a server that 503s
# instantly instead of holding.
LANE_CUT_RETRIES = 10
LANE_CUT_PAUSE_S = 5.0

# USD per million tokens: (input, output, cache_write_5m, cache_read).
# Cache writes are 1.25x input; cache reads are 0.1x input. Only the Sonnet
# chat option bills — the local lane reports $0 honestly (cost_usd).
PRICING: dict[str, tuple[float, float, float, float]] = {
    # Intro pricing ended 2026-08-31; these are the standard rates.
    "claude-sonnet-5": (3.00, 15.00, 3.75, 0.30),
}


def _check_effort(effort: str | None) -> str | None:
    if effort is not None and effort not in EFFORTS:
        raise ValueError(
            f"unknown effort {effort!r} — expected one of {', '.join(EFFORTS)}"
        )
    return effort


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 8192,
        thinking: str | None = None,
        effort: str | None = None,
        base_url: str | None = None,
        lane: str = "chat",
        answer_headroom: int | None = None,
    ) -> None:
        if lane not in LANES:
            raise ValueError(f"unknown lane {lane!r} — expected one of {', '.join(LANES)}")
        #: Set = the local lane (llama.cpp fork's /v1/messages), not the cloud.
        self.base_url = base_url
        #: The fork's chat-window lane; meaningless (and never sent) on the cloud.
        self.lane = lane if base_url else "chat"
        if base_url:
            # Never the real key: the local server needs none, and the Anthropic
            # key must not ride requests (or logs) of another server.
            self.client = anthropic.AsyncAnthropic(
                base_url=base_url,
                api_key="local",
                timeout=LOCAL_TIMEOUT_S,
                max_retries=0,
                default_headers={"X-Lane": "code"} if self.lane == "code" else None,
            )
        else:
            self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        # "adaptive" | "off" | None (None = send nothing; on Sonnet 5 the API
        # then defaults to adaptive with display "omitted" — empty thinking
        # text — so configured callers should pass "adaptive" explicitly).
        # Sonnet chat option only: the local lane is driven by effort alone.
        self.thinking = thinking
        self.effort = _check_effort(effort)
        # Local lane only: the output reserve beyond the thinking budget
        # (None = LOCAL_ANSWER_HEADROOM). Compaction passes its full summary-
        # band ceiling so 10k of think and 10k of text are BOTH guaranteed
        # within COMPACTION_MAX_TOKENS (Step 4); chat keeps the default.
        self.answer_headroom = (
            LOCAL_ANSWER_HEADROOM if answer_headroom is None else answer_headroom
        )

    @property
    def is_local(self) -> bool:
        return bool(self.base_url)

    @property
    def _supports_thinking(self) -> bool:
        return self.model in THINKING_MODELS

    def _thinking_param(self, effort: str | None = None) -> dict[str, Any] | None:
        """The cloud request `thinking` payload, or None to omit it
        (model-gated). effort "off" disables thinking outright."""
        if not self._supports_thinking:
            return None
        if (effort or self.effort) == "off":
            return {"type": "disabled"}
        if self.thinking == "adaptive":
            # display defaults to "omitted" on Sonnet 5 (empty thinking text);
            # "summarized" is required for the UI to see anything.
            return {"type": "adaptive", "display": "summarized"}
        if self.thinking == "off":
            return {"type": "disabled"}
        return None

    def _effort_params(self, effort: str | None) -> dict[str, Any]:
        """The thinking/effort request kwargs for one call (``effort`` already
        resolved: the per-call override or the provider default)."""
        if self.is_local:
            return _local_effort_params(
                effort or LOCAL_DEFAULT_EFFORT,
                self.max_tokens,
                self.answer_headroom,
            )
        params: dict[str, Any] = {}
        thinking_param = self._thinking_param(effort)
        if thinking_param is not None:
            params["thinking"] = thinking_param
        if effort is not None and effort != "off" and self._supports_thinking:
            params["output_config"] = {"effort": effort}
        return params

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[Tool],
        on_text_delta: Optional[OnTextDelta] = None,
        *,
        on_thinking_delta: Optional[OnTextDelta] = None,
        on_thinking_done: Optional[OnEvent] = None,
        on_tool_start: Optional[OnToolStart] = None,
        effort: str | None = None,
    ) -> LLMResponse:
        effective = _check_effort(effort) or self.effort
        effort_params = self._effort_params(effective)
        thinking_param = effort_params.get("thinking")
        thinking_on = bool(thinking_param) and thinking_param["type"] != "disabled"
        # Both lanes (local fork + opt-in Sonnet cloud) accept base64 image
        # blocks; the file jail's attachment paths are read here, per request.
        api_messages = _to_api_messages(
            messages,
            thinking_on,
            keep_all_thinking=self.is_local,
            load_image=lambda p: Path(p).read_bytes(),
        )
        api_tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]
        # Breakpoint on the last system block caches the tools+system prefix.
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_blocks,
            messages=api_messages,
            tools=api_tools or anthropic.NOT_GIVEN,
            **effort_params,
        )

        attempt = 0
        while True:
            try:
                resp, thinking_tokens = await self._stream(
                    kwargs, on_text_delta, on_thinking_delta, on_thinking_done,
                    on_tool_start,
                )
                break
            except anthropic.APIStatusError as e:
                if (
                    self.lane != "code"
                    or not _is_lane_cut(e)
                    or attempt >= LANE_CUT_RETRIES
                ):
                    raise
                attempt += 1
                # Partial deltas already streamed are replayed by the re-send
                # (the live view may repeat a fragment; the stored turn won't).
                print(
                    f"[provider] code-lane call cut off by a chat — re-sending "
                    f"({attempt}/{LANE_CUT_RETRIES})",
                    file=sys.stderr,
                )
                await asyncio.sleep(LANE_CUT_PAUSE_S)
        return _to_response(resp, thinking_tokens)

    async def _stream(
        self,
        kwargs: dict[str, Any],
        on_text_delta: Optional[OnTextDelta],
        on_thinking_delta: Optional[OnTextDelta],
        on_thinking_done: Optional[OnEvent],
        on_tool_start: Optional[OnToolStart] = None,
    ) -> tuple[anthropic.types.Message, int]:
        """One streamed request: fires the delta hooks, returns the final
        message plus the thinking-token count read off message_delta."""
        async with self.client.messages.stream(**kwargs) as stream:
            # Raw event iteration (not .text_stream) so thinking deltas stream
            # too. Track block type per index: thinking_delta must only fire
            # for real `thinking` blocks, never `redacted_thinking`.
            block_types: dict[int, str] = {}
            # The API reports output_tokens_details only on message_delta
            # events; the SDK's accumulated final-message usage drops it
            # (anthropic 0.107.1) — capture it from the stream ourselves. The
            # local fork never sends it: thinking_tokens is structurally 0 there.
            thinking_tokens = 0
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "content_block_start":
                    block_types[event.index] = event.content_block.type
                    if event.content_block.type == "tool_use" and on_tool_start:
                        on_tool_start(event.content_block.name)
                elif etype == "content_block_delta":
                    dtype = event.delta.type
                    if dtype == "text_delta":
                        if on_text_delta:
                            on_text_delta(event.delta.text)
                    elif dtype == "thinking_delta":
                        if (
                            on_thinking_delta
                            and block_types.get(event.index) == "thinking"
                        ):
                            on_thinking_delta(event.delta.thinking)
                elif etype == "content_block_stop":
                    if (
                        block_types.get(event.index) == "thinking"
                        and on_thinking_done
                    ):
                        on_thinking_done()
                elif etype == "message_delta":
                    details = getattr(
                        getattr(event, "usage", None), "output_tokens_details", None
                    )
                    if details is not None:
                        thinking_tokens = (
                            getattr(details, "thinking_tokens", 0) or 0
                        )
            return await stream.get_final_message(), thinking_tokens

    def cost_usd(self, usage: Usage) -> float | None:
        if self.is_local:
            return 0.0  # own hardware: `runs summary` shows $0 honestly
        prices = PRICING.get(self.model)
        if prices is None:
            return None
        inp, out, cache_write, cache_read = prices
        # thinking_tokens are already included in output_tokens.
        return (
            usage.input_tokens * inp
            + usage.output_tokens * out
            + usage.cache_creation_input_tokens * cache_write
            + usage.cache_read_input_tokens * cache_read
        ) / 1_000_000


def _is_lane_cut(e: anthropic.APIStatusError) -> bool:
    """The chat window's cut: `overloaded_error` in either shape the fork
    sends — llama.cpp's own pre-stream 503 body or Anthropic's SSE error."""
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error")
    return isinstance(err, dict) and err.get("type") == "overloaded_error"


def _to_response(resp: anthropic.types.Message, thinking_tokens: int) -> LLMResponse:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=dict(block.input) if block.input else {},
                )
            )

    # Prefer the final message's details when the SDK provides them;
    # otherwise use the value _stream captured off the message_delta events.
    details = getattr(resp.usage, "output_tokens_details", None)
    if details is not None:
        thinking_tokens = getattr(details, "thinking_tokens", 0) or 0
    usage = Usage(
        input_tokens=resp.usage.input_tokens or 0,
        output_tokens=resp.usage.output_tokens or 0,
        cache_creation_input_tokens=getattr(
            resp.usage, "cache_creation_input_tokens", 0
        )
        or 0,
        cache_read_input_tokens=getattr(resp.usage, "cache_read_input_tokens", 0)
        or 0,
        thinking_tokens=thinking_tokens,
    )

    return LLMResponse(
        text="".join(text_parts) or None,
        tool_calls=tool_calls,
        usage=usage,
        raw=resp,
        content_blocks=[b.model_dump(exclude_none=True) for b in resp.content],
        stop_reason=resp.stop_reason,
    )
