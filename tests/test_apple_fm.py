"""AppleFMProvider: the on-device utility tier, fully faked.

Offline — apple_fm_sdk is replaced wholesale (the provider reaches the SDK
only through the module-level `fm` name), so no model, no Apple Intelligence,
no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.providers import apple_fm
from harness.providers.apple_fm import AppleFMProvider, AppleFMUnavailableError
from harness.core.types import Message


class FakeModel:
    context_size = 8192

    def __init__(self, sdk: "FakeSDK") -> None:
        self._sdk = sdk

    def is_available(self):
        return (self._sdk.available, self._sdk.reason)

    async def token_count(self, value=None, *, instructions=None):
        assert (value is None) != (instructions is None), "one of value|instructions"
        return len(value or instructions) // 4


class FakeSession:
    def __init__(self, sdk: "FakeSDK", *, instructions=None, model=None) -> None:
        self.sdk = sdk
        self.instructions = instructions
        self.model = model

    async def respond(self, prompt, *, generating=None):
        self.sdk.calls.append(
            {"prompt": prompt, "generating": generating, "instructions": self.instructions}
        )
        if self.sdk.outcomes is not None:
            # scripted per-call outcome; pop() on an empty list makes an
            # unexpected extra call fail loudly instead of silently succeeding
            outcome = self.sdk.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if self.sdk.error is not None:
            raise self.sdk.error
        return self.sdk.reply


class FakeSDK:
    def __init__(self, *, available=True, reason=None, reply=None, error=None,
                 outcomes=None):
        self.available = available
        self.reason = reason
        self.reply = reply
        self.error = error
        # optional list of per-call reply-or-exception outcomes (scripted
        # sequences, e.g. refusal then free-text label)
        self.outcomes: list | None = (
            list(outcomes) if outcomes is not None else None
        )
        self.calls: list[dict] = []

    def SystemLanguageModel(self):
        return FakeModel(self)

    def LanguageModelSession(self, *, instructions=None, model=None):
        return FakeSession(self, instructions=instructions, model=model)

    def guide(self, description=None, **kwargs):
        return SimpleNamespace(description=description, **kwargs)

    def generable(self, description=None):
        def deco(cls):
            cls.generation_description = description
            return cls

        return deco


@pytest.fixture
def sdk(monkeypatch):
    fake = FakeSDK()
    monkeypatch.setattr(apple_fm, "fm", fake)
    monkeypatch.delenv("HARNESS_APPLE_FM", raising=False)
    return fake


def provider() -> AppleFMProvider:
    return AppleFMProvider()


# -- chat ---------------------------------------------------------------------


async def test_chat_returns_plain_text_and_deltas(sdk):
    sdk.reply = "here you go"
    seen: list[str] = []
    resp = await provider().chat(
        "sys", [Message(role="user", text="hi")], [], on_text_delta=seen.append
    )
    assert resp.text == "here you go"
    assert resp.usage is None
    assert seen == ["here you go"]
    assert sdk.calls[0]["prompt"] == "user:\nhi"
    assert sdk.calls[0]["instructions"] == "sys"


async def test_chat_refuses_tools(sdk):
    with pytest.raises(ValueError, match="no tools"):
        await provider().chat("sys", [Message(role="user", text="hi")], ["something"])


async def test_chat_raises_when_unavailable(sdk, monkeypatch):
    monkeypatch.setattr(apple_fm, "_probe_note_printed", True)
    sdk.available, sdk.reason = False, "apple intelligence off"
    with pytest.raises(AppleFMUnavailableError, match="apple intelligence off"):
        await provider().chat("sys", [Message(role="user", text="hi")], [])


async def test_chat_overflow_raises_before_responding(sdk, monkeypatch):
    monkeypatch.setattr(apple_fm, "RESPONSE_RESERVE", 8192 - 40)
    with pytest.raises(ValueError, match="context overflow"):
        await provider().chat("sys", [Message(role="user", text="x" * 1000)], [])
    assert sdk.calls == []


async def test_env_off_disables_everything(sdk, monkeypatch):
    monkeypatch.setattr(apple_fm, "_probe_note_printed", True)
    monkeypatch.setenv("HARNESS_APPLE_FM", "off")
    p = provider()
    assert await p.classify("mail", ["urgent"], "triage") is None
    assert "HARNESS_APPLE_FM=off" in p.last_error
    with pytest.raises(AppleFMUnavailableError):
        await p.chat("sys", [Message(role="user", text="hi")], [])


# -- classify (local contract) --------------------------------------------------


async def test_classify_returns_label_and_builds_enum_class_once(sdk):
    sdk.reply = SimpleNamespace(label="urgent")
    p = provider()
    assert await p.classify("mail", ["urgent", "noise"], "triage") == "urgent"
    assert sdk.calls[0]["generating"] is apple_fm._label_class(tuple(["urgent", "noise"]))
    sdk.reply = SimpleNamespace(label="noise")
    assert await p.classify("more", ["urgent", "noise"], "triage") == "noise"
    assert len(sdk.calls) == 2


async def test_classify_rejects_unknown_label(sdk):
    sdk.reply = SimpleNamespace(label="spam")
    p = provider()
    assert await p.classify("mail", ["urgent"], "triage") is None
    assert "unknown label" in p.last_error


async def test_classify_swallows_model_errors_into_last_error(sdk):
    sdk.error = RuntimeError("boom")
    p = provider()
    assert await p.classify("mail", ["urgent"], "triage") is None
    assert p.last_error == "RuntimeError: boom"
    # and a later success clears it
    sdk.error = None
    sdk.reply = SimpleNamespace(label="urgent")
    assert await p.classify("mail", ["urgent"], "triage") == "urgent"
    assert p.last_error is None


# -- classify refusal backstop (quest fm-triage-refusals step 3) ------------------

TRIAGE = ["urgent", "actionable", "newsletter", "ignore"]


def refusal() -> RuntimeError:
    """The shape apple-fm-sdk (installed 0.1.0) surfaces a macOS refusal in:
    a generic status-255 error whose message carries the LanguageModelError:3
    marker."""
    return RuntimeError(
        "apple fm generation failed: FoundationModels.LanguageModelError:3 - "
        "The model refused to answer. (status 255)"
    )


async def test_classify_refusal_retries_unguided_once(sdk, capsys):
    sdk.outcomes = [refusal(), "newsletter"]
    p = provider()
    assert await p.classify("mail", TRIAGE, "triage") == "newsletter"
    assert p.last_error is None  # a successful fallback clears it
    assert len(sdk.calls) == 2  # exactly one retry, same _respond path
    assert sdk.calls[0]["generating"] is apple_fm._label_class(tuple(TRIAGE))
    assert sdk.calls[1]["generating"] is None  # the retry is unguided
    assert sdk.calls[1]["instructions"] == (
        "triage\nReply with exactly one of: urgent, actionable, newsletter, ignore."
    )
    err = capsys.readouterr().err
    assert err.count("[apple-fm] classify refused") == 1
    assert "free-text retry → newsletter" in err


async def test_classify_refusal_detected_by_error_type(sdk):
    """Type detection must work off the marker message (getattr path: the
    fake SDK gains the class the real one has)."""

    class RefusalError(Exception):
        pass

    sdk.RefusalError = RefusalError
    sdk.outcomes = [RefusalError("no marker in this message"), "ignore"]
    p = provider()
    assert await p.classify("mail", ["urgent", "ignore"], "i") == "ignore"
    assert len(sdk.calls) == 2


async def test_classify_refusal_unparsable_returns_none(sdk, capsys):
    sdk.outcomes = [refusal(), "I'm not able to help with financial matters."]
    p = provider()
    assert await p.classify("mail", TRIAGE, "triage") is None
    assert "unparsable" in p.last_error
    assert "refused" in p.last_error  # eval's refusal bucket keys on this
    err = capsys.readouterr().err
    assert "classify refused" in err and "→ unparsable" in err


async def test_classify_refusal_ambiguous_reply_is_unparsable(sdk):
    sdk.outcomes = [refusal(), "actionable, maybe urgent — you decide."]
    p = provider()
    assert await p.classify("mail", TRIAGE, "triage") is None
    assert "unparsable" in p.last_error


async def test_classify_refusal_fallback_normalises_underscore_labels(sdk):
    labels = ["probably_deny", "probably_escalate"]
    sdk.outcomes = [refusal(), "Probably Deny."]  # exact after normalisation
    p = provider()
    assert await p.classify("mail", labels, "i") == "probably_deny"
    sdk.outcomes = [refusal(), "my label: `probably_escalate`"]  # whole word
    assert await p.classify("mail", labels, "i") == "probably_escalate"


async def test_classify_refusal_retry_failure_still_never_raises(sdk, capsys):
    sdk.outcomes = [refusal(), RuntimeError("connection reset")]
    p = provider()
    assert await p.classify("mail", TRIAGE, "triage") is None
    assert "refused" in p.last_error and "connection reset" in p.last_error
    assert "free-text retry → error" in capsys.readouterr().err


async def test_classify_generic_error_makes_exactly_one_call(sdk, capsys):
    sdk.error = RuntimeError("boom")  # not a refusal: today's single-call path
    p = provider()
    assert await p.classify("mail", TRIAGE, "triage") is None
    assert p.last_error == "RuntimeError: boom"
    assert len(sdk.calls) == 1
    assert "classify refused" not in capsys.readouterr().err


# -- title (local contract) -------------------------------------------------------


async def test_title_cleans_and_truncates(sdk):
    p = provider()
    sdk.reply = SimpleNamespace(title="  weekend   trip\nplans ")
    assert await p.title("hi", "name it") == "weekend trip plans"
    sdk.reply = SimpleNamespace(title="x" * 100)
    assert await p.title("hi", "name it", max_chars=10) == "x" * 10
    sdk.reply = SimpleNamespace(title="   ")
    assert await p.title("hi", "name it") is None
    assert "unusable title" in p.last_error


async def test_title_error_sets_last_error(sdk):
    sdk.error = RuntimeError("nope")
    p = provider()
    assert await p.title("hi", "name it") is None
    assert p.last_error == "RuntimeError: nope"


# -- extract (guided memory extraction) -----------------------------------------


@pytest.fixture
def fresh_class(sdk, monkeypatch):
    # The generable is module-cached (the SDK bakes schemas at decoration);
    # each extract test builds its own against the fake SDK.
    monkeypatch.setattr(apple_fm, "_extraction_cls", None)
    return sdk


async def test_extract_returns_clean_payload_from_the_generated_object(
    sdk, fresh_class
):
    sdk.reply = SimpleNamespace(
        episode="  Discussed drinks. ",
        facts=[
            SimpleNamespace(
                content=" Tom prefers tea ",
                category="preference",
                source="user",
                horizon="lasting",
                supersedes="7",  # a str is not an id -> None
            )
        ],
    )
    payload = await provider().extract("instr", "transcript")
    assert payload == {
        "facts": [
            {
                "content": "Tom prefers tea",
                "category": "preference",
                "source": "user",
                "horizon": "lasting",
                "supersedes": None,
            }
        ],
        "episode": "Discussed drinks.",
    }
    assert sdk.calls[0]["instructions"] == "instr"
    assert sdk.calls[0]["prompt"] == "transcript"
    assert sdk.calls[0]["generating"] is apple_fm._extraction_cls


async def test_extract_empty_turn(sdk, fresh_class):
    sdk.reply = SimpleNamespace(episode=None, facts=[])
    assert await provider().extract("i", "t") == {"facts": [], "episode": None}


async def test_extract_schema_is_a_fact_list_in_sync_with_categories(
    fresh_class,
):
    """The generable must be a LIST of nested Fact objects (the supported
    shape, verified on-device) whose enums cannot drift from the store."""
    from harness.memory.store import CATEGORIES

    gen = apple_fm._extraction_generable()
    assert apple_fm._extraction_generable() is gen  # built once
    facts = gen.__annotations__["facts"]
    assert facts.__origin__ is list
    fact = facts.__args__[0]
    assert fact.category.anyOf == list(CATEGORIES)
    assert fact.source.anyOf == ["user", "observed"]
    assert fact.horizon.anyOf == ["days", "months", "lasting"]


async def test_extract_error_degrades_to_none_with_last_error(sdk, fresh_class):
    sdk.error = RuntimeError("boom")
    p = provider()
    assert await p.extract("i", "t") is None
    assert p.last_error == "RuntimeError: boom"


# -- startup note -------------------------------------------------------------


async def test_unavailable_note_printed_once_per_process(sdk, capsys, monkeypatch):
    monkeypatch.setattr(apple_fm, "_probe_note_printed", False)
    sdk.available, sdk.reason = False, "model not ready"
    provider()
    provider()
    err = capsys.readouterr().err
    assert err.count("[apple-fm]") == 1
    assert "model not ready" in err
