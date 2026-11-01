"""Apple's on-device Foundation Model (apple-fm-sdk): the small utility tier.

Gmail triage, approval forecasting, chat titles, memory extraction — the
jobs that must stay on-device, private, and free. Guided generation
(constrained decoding) backs the narrow local contract (classify / title /
extract), mirroring the OllamaProvider degrade contract: a None result means
"unavailable or unusable", never an exception across that boundary.

The window is small (8192 tokens on macOS 27): anything that must read more —
compaction transcripts — belongs on the local Qwen server, and chat() refuses
tools outright (this is a utility worker, never the agent loop). Availability
is checked per call: when Apple Intelligence is off, chat() raises
AppleFMUnavailableError (callers trace and degrade) while classify / title /
extract return None with last_error set.
"""

from __future__ import annotations

import asyncio
import os
import re
import string
import sys
from typing import Optional

import apple_fm_sdk as fm

from ..core.types import LLMResponse, Message
from ..tools.base import Tool
from .base import LLMProvider, OnEvent, OnTextDelta

# Tokens held back from the context window for the reply itself.
RESPONSE_RESERVE = 512

_probe_note_printed = False
_label_classes: dict[tuple[str, ...], type] = {}


# The sdk's json_schema wants Apple's NATIVE dialect: build every shape as an
# @fm.generable class. A generic status-255 = oversize OR an unmapped refusal.
def _label_class(labels: tuple[str, ...]) -> type:
    """A generable {label: <one of labels>} class, built once per label set
    (the SDK builds the native schema when the class is decorated)."""
    cls = _label_classes.get(labels)
    if cls is None:
        cls = type("Classification", (), {"__annotations__": {"label": str}})
        cls.label = fm.guide("the chosen label", anyOf=list(labels))
        cls = fm.generable("classification result")(cls)
        _label_classes[labels] = cls
    return cls


@fm.generable("chat title")
class _Title:
    title: str = fm.guide("a short title for the conversation")


_extraction_cls: type | None = None


def _extraction_generable() -> type:
    """The memory-extraction generable, built ONCE from
    memory.store.CATEGORIES so the schema can never drift from the store's
    category tuple (the SDK bakes the native schema at decoration time).
    Guided generation is the only trustworthy extraction path on this model:
    free-text JSON copies the prompt's schema notation and invents
    per-category keys (learnings 2026-10-01). A list of nested generables works —
    verified on-device, scratch/generable_spike.py."""
    global _extraction_cls
    if _extraction_cls is None:
        from ..memory.store import CATEGORIES

        fact = type(
            "Fact",
            (),
            {
                "__annotations__": {
                    "content": str,
                    "category": str,
                    "source": str,
                    "horizon": str,
                    "supersedes": Optional[int],
                }
            },
        )
        fact.content = fm.guide("the fact, third person and self-contained")
        fact.category = fm.guide("the fact's category", anyOf=list(CATEGORIES))
        fact.source = fm.guide(
            'whose words it rests on', anyOf=["user", "observed"]
        )
        fact.horizon = fm.guide(
            "how long it stays true", anyOf=["days", "months", "lasting"]
        )
        fact.supersedes = fm.guide("id of an <existing_memory> fact this corrects")
        fact = fm.generable("one durable memory fact")(fact)

        ext = type(
            "Extraction",
            (),
            {"__annotations__": {"episode": Optional[str], "facts": list[fact]}},
        )
        ext.episode = fm.guide("one sentence for what happened this turn")
        ext.facts = fm.guide("the durable facts worth saving (usually none)")
        _extraction_cls = fm.generable("memory extracted from one turn")(ext)
    return _extraction_cls


class AppleFMUnavailableError(RuntimeError):
    """Apple Intelligence is off / the model is not ready / HARNESS_APPLE_FM=off."""


# The status-255 shape apple-fm-sdk surfaces for a macOS refusal (the sdk
# doesn't map it to RefusalError — verified on the installed 0.1.0; the
# message is the only marker — belt to _is_refusal's type check's braces).
_REFUSAL_MARKERS = ("LanguageModelError:3", "refused to answer")

# Quote/punctuation shell a free-text reply wraps a label in.
_WRAP_CHARS = string.punctuation + "“”‘’«»"


def _is_refusal(exc: BaseException) -> bool:
    """Refusal by error type when the sdk maps it (getattr: the fake SDK in
    tests lacks these classes), else by the installed-sdk status-255 message
    shape."""
    for name in ("RefusalError", "GuardrailViolationError"):
        cls = getattr(fm, name, None)
        if isinstance(cls, type) and isinstance(exc, cls):
            return True
    msg = str(exc)
    return any(marker in msg for marker in _REFUSAL_MARKERS)


def _norm_label_text(s: str) -> str:
    """Lowercase, underscores as spaces, collapsed whitespace, quotes and
    punctuation stripped from the ends — the equality space for label matching."""
    s = " ".join(s.strip().lower().replace("_", " ").split())
    return s.strip(_WRAP_CHARS)


def _match_label(reply, labels: list[str]) -> str | None:
    """One free-text reply to the exact set of labels: exact match after
    normalisation, else the single label appearing as a whole word, else
    None (ambiguous or absent replies are the caller's unparsable case)."""
    raw = reply if isinstance(reply, str) else str(reply)
    norm = _norm_label_text(raw)
    normed = {label: _norm_label_text(label) for label in labels}
    for label, target in normed.items():
        if norm == target:
            return label
    found = [
        label
        for label, target in normed.items()
        if re.search(rf"(?<!\w){re.escape(target)}(?!\w)", norm)
    ]
    return found[0] if len(found) == 1 else None


def _flatten(messages: list[Message]) -> str:
    """Utility callers send one user message; anything else flattens into
    role-labelled blocks (the model sees data, not transcript structure)."""
    parts: list[str] = []
    for m in messages:
        body = m.text or ""
        for tc in m.tool_calls:
            body += f"\n[tool_call {tc.name}: {tc.arguments}]"
        for tr in m.tool_results:
            body += f"\n[tool_result: {tr.content}]"
        parts.append(f"{m.role}:\n{body.strip()}")
    return "\n\n".join(parts)


class AppleFMProvider(LLMProvider):
    name = "apple_fm"
    model = "apple-foundation-model"

    def __init__(self) -> None:
        self.enabled = os.environ.get(
            "HARNESS_APPLE_FM", "on"
        ).strip().lower() not in ("off", "0", "false")
        self.last_error: str | None = None
        self._model = fm.SystemLanguageModel() if self.enabled else None
        self._note_unavailable_once()

    def unavailable_reason(self) -> str | None:
        """None when a call should work; a human-readable reason otherwise."""
        if not self.enabled:
            return "HARNESS_APPLE_FM=off"
        available, reason = self._model.is_available()
        return None if available else f"Apple Intelligence unavailable ({reason})"

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[Tool],
        on_text_delta: Optional[OnTextDelta] = None,
        *,
        on_thinking_delta: Optional[OnTextDelta] = None,
        on_thinking_done: Optional[OnEvent] = None,
        on_tool_start=None,  # no tools on this tier: never fires
        effort: str | None = None,
    ) -> LLMResponse:
        if tools:
            raise ValueError("the Apple Foundation Model is a utility tier: no tools")
        text = await self._respond(system, _flatten(messages))
        if on_text_delta and text:
            on_text_delta(text)
        return LLMResponse(text=text or None)

    async def classify(
        self, text: str, labels: list[str], instruction: str
    ) -> str | None:
        """OllamaProvider's local contract: enum-constrained label, or None
        with last_error set (the caller falls back or degrades).

        Guided generation refuses finance/insurance-shaped raw input (quest
        fm-triage-refusals: gmail snippets read as the user's own affairs);
        free text answers the same prompt, so a refusal gets exactly ONE
        unguided retry before degrading. Other errors keep the single-call
        behaviour."""
        try:
            result = await self._respond(
                instruction, text, generable=_label_class(tuple(labels))
            )
        except Exception as e:  # noqa: BLE001 - contract: never raise, last_error carries it
            if not _is_refusal(e):
                self.last_error = f"{type(e).__name__}: {e}"
                return None
            return await self._refusal_retry(text, labels, instruction)
        label = getattr(result, "label", None)
        if label not in labels:
            self.last_error = f"model returned unknown label {label!r}"
            return None
        self.last_error = None
        return label

    async def _refusal_retry(
        self, text: str, labels: list[str], instruction: str
    ) -> str | None:
        """The one unguided retry: same _respond (the token_count pre-check
        stays load-bearing), no generable, the label list spelled out in the
        instruction. One stderr line per refusal — agentd.log counts them;
        the last_error wording carries "refused" so eval buckets stay honest."""
        ask = f"{instruction}\nReply with exactly one of: {', '.join(labels)}."
        label: str | None = None
        try:
            reply = await self._respond(ask, text)
        except Exception as e:  # noqa: BLE001 - never raise; the refusal is still the story
            self.last_error = (
                f"classify refused; free-text retry failed {type(e).__name__}: {e}"
            )
            outcome = "error"
        else:
            label = _match_label(reply, labels)
            if label is None:
                self.last_error = (
                    "classify refused; free-text fallback reply unparsable: "
                    f"{str(reply)[:120]!r}"
                )
                outcome = "unparsable"
            else:
                self.last_error = None
                outcome = label
        print(
            "[apple-fm] classify refused under guided generation; "
            f"free-text retry → {outcome}",
            file=sys.stderr,
        )
        return label

    async def title(
        self, text: str, instruction: str, max_chars: int = 60
    ) -> str | None:
        """OllamaProvider's local contract: a clean one-line title, or None
        with last_error set."""
        try:
            result = await self._respond(instruction, text, generable=_Title)
        except Exception as e:  # noqa: BLE001 - contract: never raise, last_error carries it
            self.last_error = f"{type(e).__name__}: {e}"
            return None
        raw = getattr(result, "title", None)
        title = " ".join(raw.split())[:max_chars].strip() if isinstance(raw, str) else ""
        if not title:
            self.last_error = f"model returned unusable title {raw!r}"
            return None
        self.last_error = None
        return title

    async def extract(self, instruction: str, text: str) -> dict | None:
        """Guided memory extraction (MemoryExtractor's guided lane): the
        reply is CONSTRUCTED against the Extraction schema, so it cannot be
        invalid JSON, an unknown category, or the prompt's notation copied
        back. Returns the payload dict (facts + episode), or None with
        last_error set."""
        try:
            result = await self._respond(
                instruction, text, generable=_extraction_generable()
            )
        except Exception as e:  # noqa: BLE001 - contract: never raise, last_error carries it
            self.last_error = f"{type(e).__name__}: {e}"
            return None
        facts = []
        for f in getattr(result, "facts", None) or []:
            supersedes = getattr(f, "supersedes", None)
            facts.append(
                {
                    "content": str(getattr(f, "content", "") or "").strip(),
                    "category": getattr(f, "category", None),
                    "source": getattr(f, "source", None),
                    "horizon": getattr(f, "horizon", None),
                    "supersedes": supersedes if isinstance(supersedes, int) else None,
                }
            )
        episode = getattr(result, "episode", None)
        episode = episode.strip() if isinstance(episode, str) else ""
        self.last_error = None
        return {"facts": facts, "episode": episode or None}

    async def _respond(self, instructions: str, prompt: str, *, generable=None):
        """One generation on a fresh session (sessions accumulate transcripts;
        utility calls are independent). The SDK serializes per session and
        tolerates concurrent sessions, so no external lock."""
        reason = self.unavailable_reason()
        if reason is not None:
            raise AppleFMUnavailableError(reason)
        prompt_tokens, instruction_tokens = await asyncio.gather(
            self._model.token_count(prompt),
            self._model.token_count(instructions=instructions),
        )
        budget = self._model.context_size - RESPONSE_RESERVE
        if prompt_tokens + instruction_tokens > budget:
            raise ValueError(
                f"apple fm context overflow: ~{prompt_tokens}+{instruction_tokens}"
                f" prompt tokens > {budget} available"
            )
        session = fm.LanguageModelSession(
            instructions=instructions, model=self._model
        )
        return await session.respond(prompt, generating=generable)

    def _note_unavailable_once(self) -> None:
        """One stderr note per process (the daemon builds a provider per run)."""
        global _probe_note_printed
        if _probe_note_printed or not self.enabled:
            return
        _probe_note_printed = True
        reason = self.unavailable_reason()
        if reason is not None:
            print(
                f"[apple-fm] {reason} — triage, titles and extraction degrade "
                "(enable Apple Intelligence in System Settings)",
                file=sys.stderr,
            )
