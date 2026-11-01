"""The provider interface every model backend implements.

Async, with optional streaming: providers call ``on_text_delta`` as text
arrives so interfaces can render token-by-token, then return the complete
normalized response (including usage for cost tracking).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..core.types import LLMResponse, Message, Usage
from ..tools.base import Tool

OnTextDelta = Callable[[str], None]
OnEvent = Callable[[], None]
#: A tool_use block STARTED streaming (its name is known, its args are not yet).
OnToolStart = Callable[[str], None]


def strip_code_fences(text: str) -> str:
    """Unwrap a ```/```json fence around a model reply. Models asked for
    "ONLY JSON" still fence it sometimes; callers strip before json.loads."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


class LLMProvider(ABC):
    #: Short identifier, e.g. "anthropic" or "ollama".
    name: str
    #: The model this provider instance talks to.
    model: str

    @abstractmethod
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
        """Send the conversation + available tools to the model and return one
        normalized response (text and/or tool calls), streaming text deltas to
        ``on_text_delta`` — and, for models with extended thinking, thinking
        summary deltas to ``on_thinking_delta`` with ``on_thinking_done`` at
        each thinking-block boundary, and ``on_tool_start(name)`` the moment a
        tool_use block begins (the live row's "Preparing …", BK-041 G3) — as they arrive. ``effort`` (off | low |
        medium | xhigh) overrides the provider's default for this call only."""
        ...

    def cost_usd(self, usage: Usage) -> float | None:
        """Estimated cost of one call; None if pricing is unknown."""
        return None
