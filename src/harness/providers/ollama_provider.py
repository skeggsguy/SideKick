"""Ollama client — DORMANT since 2026-10-02, kept for two things: the
Classifier/LocalUtility contracts that providers/apple_fm.py implements
(triage, prediction, titles, extraction now ride the on-device Apple FM), and
memory embeddings, the one job Ollama is still the way to get locally.

There is no cloud utility tier to degrade to anymore; Classifier raises when
the local tier fails, and each caller degrades on its own terms (triage →
actionable, titles → NULL, extraction → traced error).
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx

from ..core.types import Message
from .base import LLMProvider, strip_code_fences

DEFAULT_TRIAGE_MODEL = "qwen3:4b"


class LocalUtility(Protocol):
    """The narrow local-model contract (classify / title) that OllamaProvider
    and AppleFMProvider both satisfy: a None result means unavailable or
    unusable (last_error explains), never an exception."""

    last_error: str | None

    async def classify(
        self, text: str, labels: list[str], instruction: str
    ) -> str | None: ...

    async def title(self, text: str, instruction: str, max_chars: int = 60) -> str | None: ...


class OllamaProvider:
    """Thin client for Ollama's /api/chat. None results mean 'unavailable',
    and the caller falls back — never an exception across this boundary."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("OLLAMA_HOST")
            or "http://127.0.0.1:11434"
        ).rstrip("/")
        self.model = model or os.environ.get(
            "HARNESS_TRIAGE_MODEL", DEFAULT_TRIAGE_MODEL
        )
        self._transport = transport
        self.last_error: str | None = None

    async def _chat_json(
        self,
        text: str,
        instruction: str,
        schema: dict,
        *,
        timeout: float = 120.0,
        temperature: float = 0.0,
    ) -> Any | None:
        """One schema-constrained /api/chat call, JSON-parsed. Returns the
        parsed value, or None (with last_error set) when Ollama is
        unavailable or replies unusably. `text` is untrusted data — it rides
        in the user role, never the instruction."""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "think": False,
            "format": schema,
            "options": {"temperature": temperature},
            "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "5m"),
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=2.0),
                transport=self._transport,
            ) as client:
                resp = await client.post(f"{self.base_url}/api/chat", json=body)
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
            data = json.loads(content)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None
        self.last_error = None
        return data

    async def classify(self, text: str, labels: list[str], instruction: str) -> str | None:
        """One label from `labels`, or None when Ollama is unavailable or
        replies unusably. Output is schema-constrained to the label enum."""
        schema = {
            "type": "object",
            "properties": {"label": {"type": "string", "enum": labels}},
            "required": ["label"],
        }
        data = await self._chat_json(text, instruction, schema)
        if data is None:
            return None
        label = data.get("label") if isinstance(data, dict) else None
        if label not in labels:
            self.last_error = f"model returned unknown label {label!r}"
            return None
        return label

    async def title(
        self, text: str, instruction: str, max_chars: int = 60
    ) -> str | None:
        """A short free-text title for `text` (e.g. a chat name from the first
        message), or None when Ollama is unavailable/unusable. Schema-constrained
        to {"title": string}. The caller treats `text` as untrusted data.

        Uses its OWN short timeout (not classify's 120s): naming is a tail
        side-job, so a cold/wedged Ollama must degrade to None fast rather than
        stall the turn that triggered it."""
        schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        }
        data = await self._chat_json(
            text, instruction, schema, timeout=8.0, temperature=0.2
        )
        if data is None:
            return None
        title = data.get("title") if isinstance(data, dict) else None
        if not isinstance(title, str):
            self.last_error = f"model returned non-string title {title!r}"
            return None
        # Collapse whitespace/newlines and bound length — the schema only
        # guarantees a string, not that it's a single clean line.
        title = " ".join(title.split())[:max_chars].strip()
        if not title:
            self.last_error = "model returned an empty title"
            return None
        return title


class Classifier:
    """classify() that prefers the local model and falls back to the cloud
    utility tier. Returns (label, source) where source is 'local'|'cloud'."""

    def __init__(
        self, local: LocalUtility | None, cloud: LLMProvider | None = None
    ) -> None:
        self.local = local
        self.cloud = cloud

    async def classify(
        self, text: str, labels: list[str], instruction: str
    ) -> tuple[str, str]:
        if self.local is not None:
            label = await self.local.classify(text, labels, instruction)
            if label is not None:
                return label, "local"
        if self.cloud is None:
            raise RuntimeError(
                "classification unavailable: the local utility tier failed "
                f"({self.local.last_error if self.local else 'not configured'}) "
                "and no cloud fallback exists anymore"
            )
        resp = await self.cloud.chat(
            instruction
            + f'\nReply with ONLY a JSON object: {{"label": <one of {labels}>}}',
            [Message(role="user", text=text)],
            tools=[],
        )
        reply = strip_code_fences((resp.text or "").strip())
        label = json.loads(reply).get("label")
        if label not in labels:
            raise ValueError(f"cloud classifier returned unknown label {label!r}")
        return label, "cloud"
