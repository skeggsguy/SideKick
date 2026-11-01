"""Provider-agnostic conversation types.

The agent loop and session only ever speak in these types. Each provider
(Anthropic, Ollama, ...) translates to and from its own wire format at the
boundary, so the loop never special-cases a vendor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]


@dataclass
class ToolCall:
    """A request from the model to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """The outcome of running a tool, fed back to the model."""

    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """One turn in the conversation.

    - role="user":      free-text input (``text``)
    - role="assistant": model output; ``text`` and/or ``tool_calls``
    - role="tool":      results for the assistant's tool calls (``tool_results``)

    ``content_blocks`` is the raw ordered Anthropic content list (thinking /
    redacted_thinking / text / tool_use dicts, verbatim including signatures),
    set only on assistant messages produced by the Anthropic provider. It is
    the round-trip payload for extended thinking — the API rejects *modified*
    thinking blocks, so we never reassemble them from the derived
    ``text``/``tool_calls`` view. One Message per API call, so concatenating
    messages reproduces the live interleaved order for the UI.
    """

    role: Role
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    content_blocks: list[dict] = field(default_factory=list)
    #: Attachments on a user message: [{"id", "mime", "path"}, ...];
    #: documents also carry "name" + "size" (plan doc-attachments). Paths,
    #: never base64 — the file jail confines the files and the DB (and daily
    #: backups) stay lean; the provider reads image files at request time and
    #: hands document paths to read_document/read_file as an in-text hint.
    attachments: list[dict] = field(default_factory=list)


@dataclass
class Usage:
    """Token accounting for one model call (or a whole run, when summed).

    ``thinking_tokens`` is informational — already included in
    ``output_tokens``, so cost math never adds it separately.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    thinking_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.thinking_tokens += other.thinking_tokens


@dataclass
class LLMResponse:
    """A single model response, normalized across providers."""

    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    raw: Any = None
    content_blocks: list[dict] = field(default_factory=list)
    stop_reason: str | None = None
