"""Effort + the local lane: the provider's request mapping (local llama.cpp
fork vs cloud), per-call overrides, composition's lane selection, and the web
UI picker's vocabulary. Offline — the SDK stream is faked, no network."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from harness import composition
from harness.core.context import SUMMARY_BAND_MAX
from harness.core.types import Message
from harness.providers import anthropic_provider
from harness.providers.anthropic_provider import (
    EFFORTS,
    LOCAL_ANSWER_HEADROOM,
    AnthropicProvider,
    _local_effort_params,
)
from harness.providers.apple_fm import AppleFMProvider
from harness.core.types import Usage

WEBUI = Path(composition.__file__).parent / "gateway" / "webui"


class _FakeStream:
    """Stands in for client.messages.stream(**kwargs): records kwargs, yields
    no events, and returns a minimal final message."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> "_FakeStream":
        self.calls.append(kwargs)
        return self

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self) -> anthropic.types.Message:
        return anthropic.types.Message.model_validate(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }
        )


def _with_fake(provider: AnthropicProvider) -> _FakeStream:
    fake = _FakeStream()
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=fake))
    return fake


async def _call(provider: AnthropicProvider, **kw: Any) -> dict[str, Any]:
    fake = _with_fake(provider)
    resp = await provider.chat("sys", [Message(role="user", text="hi")], [], **kw)
    assert resp.text == "ok"
    return fake.calls[-1]


def _local(effort: str | None = "xhigh", max_tokens: int = 24000) -> AnthropicProvider:
    return AnthropicProvider(
        api_key="local",
        model="qwen3.8-flash-next",
        max_tokens=max_tokens,
        effort=effort,
        base_url="http://127.0.0.1:8080",
    )


# -- local lane ---------------------------------------------------------------


def test_local_client_settings():
    """No SDK retries (a retry would enqueue a duplicate generation on the -np 1
    server), a long timeout (requests queue behind a long run), and never the
    real Anthropic key."""
    p = _local()
    assert str(p.client.base_url).rstrip("/") == "http://127.0.0.1:8080"
    assert p.client.max_retries == 0
    assert p.client.timeout == 1800
    assert p.client.api_key == "local"
    real = AnthropicProvider(
        api_key="sk-real", model="qwen", base_url="http://127.0.0.1:8080"
    )
    assert real.client.api_key == "local"


# -- chat-window lanes (the fork's X-Lane header) ------------------------------


def _cut(status: int = 200, err_type: str = "overloaded_error") -> anthropic.APIStatusError:
    """The fork's cut: status 200 + SSE error mid-stream, or a pre-stream 503."""
    req = httpx.Request("POST", "http://127.0.0.1:8080/v1/messages")
    return anthropic.APIStatusError(
        "cut",
        response=httpx.Response(status, request=req),
        body={"type": "error", "error": {"type": err_type, "message": "busy"}},
    )


class _CutThenOk(_FakeStream):
    """Raises the queued errors on enter, one per call, then succeeds."""

    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self.errors = errors

    async def __aenter__(self) -> "_FakeStream":
        if self.errors:
            raise self.errors.pop(0)
        return self


def _with_cuts(provider: AnthropicProvider, errors: list[Exception]) -> _CutThenOk:
    fake = _CutThenOk(errors)
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=fake))
    return fake


@pytest.fixture
def no_pause(monkeypatch):
    monkeypatch.setattr(anthropic_provider, "LANE_CUT_PAUSE_S", 0)


def test_lane_header_only_on_code_lane():
    """No header = chat on the fork, so only the code lane sends one; the
    cloud never carries it."""
    assert _local().client.default_headers.get("X-Lane") is None
    code = AnthropicProvider(
        api_key="local", model="q", base_url="http://127.0.0.1:8080", lane="code"
    )
    assert code.lane == "code"
    assert code.client.default_headers.get("X-Lane") == "code"
    cloud = AnthropicProvider(api_key="sk", model="claude-sonnet-5", lane="code")
    assert cloud.lane == "chat"
    assert cloud.client.default_headers.get("X-Lane") is None
    with pytest.raises(ValueError):
        AnthropicProvider(api_key="local", base_url="http://x", lane="batch")


@pytest.mark.parametrize("status", [200, 503])
async def test_code_lane_resends_after_chat_cut(no_pause, status):
    p = AnthropicProvider(
        api_key="local", model="q", base_url="http://127.0.0.1:8080", lane="code"
    )
    fake = _with_cuts(p, [_cut(status), _cut(status)])
    resp = await p.chat("sys", [Message(role="user", text="hi")], [])
    assert resp.text == "ok"
    assert len(fake.calls) == 3, (
        "_is_lane_cut keys on body error.type 'overloaded_error', never status "
        "— a mid-stream cut arrives as status 200"
    )
    assert fake.calls[0] == fake.calls[-1]  # the identical request re-sent


async def test_chat_lane_never_resends(no_pause):
    p = _local()
    fake = _with_cuts(p, [_cut()])
    with pytest.raises(anthropic.APIStatusError):
        await p.chat("sys", [Message(role="user", text="hi")], [])
    assert len(fake.calls) == 1


async def test_code_lane_only_resends_overloaded(no_pause):
    """Any other 503 (e.g. the server still loading) is a real error."""
    p = AnthropicProvider(
        api_key="local", model="q", base_url="http://127.0.0.1:8080", lane="code"
    )
    fake = _with_cuts(p, [_cut(503, "unavailable_error")])
    with pytest.raises(anthropic.APIStatusError):
        await p.chat("sys", [Message(role="user", text="hi")], [])
    assert len(fake.calls) == 1


async def test_code_lane_resend_is_bounded(no_pause):
    p = AnthropicProvider(
        api_key="local", model="q", base_url="http://127.0.0.1:8080", lane="code"
    )
    n = anthropic_provider.LANE_CUT_RETRIES
    fake = _with_cuts(p, [_cut() for _ in range(n + 1)])
    with pytest.raises(anthropic.APIStatusError):
        await p.chat("sys", [Message(role="user", text="hi")], [])
    assert len(fake.calls) == n + 1


async def test_local_lane_sends_prior_turn_thinking():
    """The prompt must re-render byte-identically across turns or the -np 1
    server loses its cached prefix; the cloud still strips cross-turn."""
    think = {"type": "thinking", "thinking": "hmm", "signature": ""}
    history = [
        Message(role="user", text="first"),
        Message(
            role="assistant",
            text="one",
            content_blocks=[think, {"type": "text", "text": "one"}],
        ),
        Message(role="user", text="second"),
    ]
    for provider, expected in (
        (_local("low"), [think, {"type": "text", "text": "one"}]),
        (
            AnthropicProvider(api_key="k", model="claude-sonnet-5"),
            [{"type": "text", "text": "one"}],
        ),
    ):
        fake = _with_fake(provider)
        await provider.chat("sys", history, [])
        assert fake.calls[-1]["messages"][1]["content"] == expected


async def test_local_off_disables_thinking_explicitly():
    kw = await _call(_local("off"))
    assert "thinking" not in kw
    assert "output_config" not in kw
    assert kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("effort", ["low", "medium", "xhigh"])
async def test_local_levels_send_explicit_template_kwargs(effort):
    kw = await _call(_local(effort))
    assert kw["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": effort}
    }, (
        "the local lane ignores adaptive thinking/output_config and the Qwen "
        "template defaults to xhigh — always send explicit chat_template_kwargs"
    )
    assert kw["thinking"] == {
        "type": "enabled",
        "budget_tokens": 24000 - LOCAL_ANSWER_HEADROOM,
    }
    # The fork ignores output_config; never send it on the local lane.
    assert "output_config" not in kw


def test_local_effort_params_answer_headroom_is_parameterized():
    """Step 4: compaction reserves the WHOLE summary band for text, so the
    guaranteed think space stops competing with the band ceiling. Exact
    equality (learnings 2026-10-10) of both shapes — the default must stay
    byte-identical for every existing caller."""
    from harness.composition import COMPACTION_MAX_TOKENS, COMPACTION_THINKING_TOKENS

    # The stock reserve: unchanged for the main chat provider.
    assert _local_effort_params("low", 20_000) == {
        "thinking": {"type": "enabled", "budget_tokens": 20_000 - LOCAL_ANSWER_HEADROOM},
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "low"}
        },
    }
    # The compaction split: max_tokens = think + text, both guaranteed.
    assert COMPACTION_THINKING_TOKENS == 10_000
    assert COMPACTION_MAX_TOKENS == SUMMARY_BAND_MAX + COMPACTION_THINKING_TOKENS
    assert _local_effort_params("low", COMPACTION_MAX_TOKENS, SUMMARY_BAND_MAX) == {
        "thinking": {"type": "enabled", "budget_tokens": COMPACTION_THINKING_TOKENS},
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "low"}
        },
    }
    # The 1024 floor survives the parameterization: a max_tokens below the
    # reserve still asks for a real budget, never a negative one.
    assert (
        _local_effort_params("low", 500, SUMMARY_BAND_MAX)["thinking"]["budget_tokens"]
        == 1024
    )


async def test_provider_answer_headroom_reaches_the_request():
    """The reserve is a constructor kwarg, not a global: chat keeps 4,096,
    compaction passes the band, and both flow through _effort_params into
    the actual request bytes."""
    kw = await _call(_local("low", max_tokens=20_000))
    assert kw["thinking"]["budget_tokens"] == 20_000 - LOCAL_ANSWER_HEADROOM
    p = AnthropicProvider(
        api_key="local",
        model="q",
        max_tokens=20_000,
        effort="low",
        base_url="http://127.0.0.1:8080",
        answer_headroom=SUMMARY_BAND_MAX,
    )
    assert p.answer_headroom == SUMMARY_BAND_MAX
    kw = await _call(p)
    assert kw["thinking"]["budget_tokens"] == 20_000 - SUMMARY_BAND_MAX
    # None normalizes to the default at construction.
    assert _local("low").answer_headroom == LOCAL_ANSWER_HEADROOM


async def test_local_none_effort_is_still_explicit():
    """No configured effort must not fall through to the template's implicit
    default — the kwargs are always sent."""
    kw = await _call(_local(None))
    assert kw["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert kw["extra_body"]["chat_template_kwargs"]["reasoning_effort"] == "xhigh"


async def test_per_call_effort_overrides_default():
    p = _local("xhigh")
    kw = await _call(p, effort="low")
    assert kw["extra_body"]["chat_template_kwargs"]["reasoning_effort"] == "low"
    kw = await _call(p, effort="off")
    assert kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    # The override is per call: the default is untouched.
    kw = await _call(p)
    assert kw["extra_body"]["chat_template_kwargs"]["reasoning_effort"] == "xhigh"


def test_local_cost_is_zero():
    usage = Usage(input_tokens=1000, output_tokens=1000)
    assert _local().cost_usd(usage) == 0.0


# -- cloud lane (unchanged behaviour) -------------------------------------------


async def test_cloud_sonnet_mapping_unchanged():
    p = AnthropicProvider(
        api_key="x", model="claude-sonnet-5", thinking="adaptive", effort="medium"
    )
    kw = await _call(p)
    assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert kw["output_config"] == {"effort": "medium"}
    assert "extra_body" not in kw
    # The system+tools breakpoint is still there.
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert list(kw)[-2:] == ["thinking", "output_config"]
    # xhigh passes straight through to output_config on the cloud.
    kw = await _call(p, effort="xhigh")
    assert kw["output_config"] == {"effort": "xhigh"}


async def test_cloud_off_disables_thinking():
    p = AnthropicProvider(
        api_key="x", model="claude-sonnet-5", thinking="adaptive", effort="medium"
    )
    kw = await _call(p, effort="off")
    assert kw["thinking"] == {"type": "disabled"}
    assert "output_config" not in kw


async def test_cloud_non_thinking_model_gets_no_effort_params():
    p = AnthropicProvider(
        api_key="x", model="claude-haiku-4-5", thinking="adaptive", effort="medium"
    )
    for effort in (None, "off", "low", "xhigh"):
        kw = await _call(p, effort=effort)
        assert "thinking" not in kw
        assert "output_config" not in kw
        assert "extra_body" not in kw


def test_cloud_client_keeps_default_settings():
    p = AnthropicProvider(api_key="x")
    assert p.base_url is None
    assert p.client.max_retries == anthropic.DEFAULT_MAX_RETRIES
    assert p.cost_usd(Usage(input_tokens=1_000_000)) == 3.00


# -- validation -----------------------------------------------------------------


def test_bad_effort_raises_at_construction():
    with pytest.raises(ValueError, match="unknown effort"):
        AnthropicProvider(api_key="x", effort="high")


async def test_bad_effort_raises_at_call_time():
    p = _local()
    fake = _with_fake(p)
    with pytest.raises(ValueError, match="unknown effort"):
        await p.chat("sys", [Message(role="user", text="hi")], [], effort="max")
    assert fake.calls == []


# -- composition ----------------------------------------------------------------

_ENV = (
    "ANTHROPIC_API_KEY",
    "HARNESS_LOCAL_URL",
    "HARNESS_LOCAL_MODEL",
    "HARNESS_EFFORT",
    "HARNESS_TOOL_GROUPS",
    "HARNESS_THINKING",
    "HARNESS_MAX_TOKENS",
    "HARNESS_APPLE_FM",
)


@pytest.fixture
def clean_env(monkeypatch):
    # test_live_smoke's module-level load_dotenv() can leak the real .env into
    # this process at collection — pin every knob explicitly. The local
    # server is REQUIRED (no cloud fallback), so the URL is pinned for every
    # composition test unless the test deletes it.
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HARNESS_LOCAL_URL", "http://127.0.0.1:8080")
    return monkeypatch


def test_composition_local_lane(clean_env):
    clean_env.setenv("HARNESS_LOCAL_URL", "http://127.0.0.1:8080")
    clean_env.setenv("HARNESS_EFFORT", "xhigh")
    clean_env.setenv("HARNESS_MAX_TOKENS", "24000")
    main, utility, compaction, model = composition._providers()
    assert main.is_local and model == "qwen3.8-flash-next" == main.model
    assert main.effort == "xhigh" and main.max_tokens == 24000
    assert main.thinking is None  # thinking params are Sonnet-chat-only
    assert main.client.api_key == "local"
    # Small tier: the on-device Apple model. Compaction rides the local
    # server too, thinking on LOW with the summary band reserved for text
    # (Step 4: think >= 10k, text >= 10k, one call) — the -np 1 prefix cache
    # is destroyed by compaction anyway, so the eviction objection is moot.
    assert isinstance(utility, AppleFMProvider)
    assert compaction.is_local and compaction.model == model
    assert compaction.effort == "low"
    assert compaction.answer_headroom == SUMMARY_BAND_MAX
    assert compaction.max_tokens == composition.COMPACTION_MAX_TOKENS


def test_compaction_provider_wiring_sentinel(clean_env, monkeypatch):
    """Wiring pin via CALLEE sentinel (learnings 2026-10-10): compaction is
    CONSTRUCTED think-on-low with the band-sized reserve — not merely equal
    to it afterwards — and the MAIN provider's construction is untouched."""
    built: list[dict] = []

    class Sentinel:
        def __init__(self, **kw):
            built.append(kw)

    monkeypatch.setattr(composition, "AnthropicProvider", Sentinel)
    composition._providers()
    assert len(built) == 2  # main first, compaction second
    main_kw, comp_kw = built
    assert comp_kw["effort"] == "low"
    assert comp_kw["max_tokens"] == composition.COMPACTION_MAX_TOKENS
    assert comp_kw["answer_headroom"] == SUMMARY_BAND_MAX
    assert comp_kw["base_url"] == "http://127.0.0.1:8080"
    # The main provider never passes the reserve: stock 4,096 behavior.
    assert "answer_headroom" not in main_kw
    assert main_kw["effort"] == "medium"


def test_composition_lane_rides_main_and_compaction(clean_env):
    """lane= reaches both local providers (main + compaction); chat default.
    The code lane stays a supported provider option — agentd no longer uses
    it (background runs ride chat, decision 2026-10-25)."""
    main, _, compaction, _ = composition._providers()
    assert main.lane == compaction.lane == "chat"
    main, _, compaction, _ = composition._providers("code")
    assert main.lane == compaction.lane == "code"
    assert compaction.client.default_headers.get("X-Lane") == "code"


def test_composition_local_model_override(clean_env):
    clean_env.setenv("HARNESS_LOCAL_URL", "http://127.0.0.1:8080")
    clean_env.setenv("HARNESS_LOCAL_MODEL", "other-gguf")
    main, _, compaction, model = composition._providers()
    assert model == main.model == compaction.model == "other-gguf"


def test_sonnet_chat_provider(clean_env):
    """The only cloud path left in the app: an opt-in CHAT provider — Sonnet
    5, adaptive thinking, pinned to medium effort, on the real API."""
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert composition.sonnet_available() is True
    p = composition.sonnet_chat_provider()
    assert not p.is_local and p.base_url is None
    assert p.model == "claude-sonnet-5"
    assert p.thinking == "adaptive"
    assert p.effort == composition.CHAT_SONNET_EFFORT == "medium"


def test_sonnet_unavailable_without_key(clean_env):
    assert composition.sonnet_available() is False
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        composition.sonnet_chat_provider()


def test_chat_model_swaps_only_the_main(clean_env):
    """A Sonnet chat turn replaces ONLY its main provider — compaction and
    the utility tier stay local, and tool groups (a local-prefix-cache
    feature) stand down."""
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-test")
    agent, model = composition.build_agent(chat_model="sonnet")
    assert model == agent.provider.model == "claude-sonnet-5"
    assert not agent.provider.is_local
    assert agent.provider.effort == "medium"
    assert agent.compactor.summarizer.is_local
    assert agent.toolset.grouped is False


def test_composition_requires_local_url(clean_env):
    """The cloud is not a fallback: with no local server the harness refuses
    to start rather than silently spending API credits."""
    clean_env.delenv("HARNESS_LOCAL_URL")
    with pytest.raises(SystemExit, match="HARNESS_LOCAL_URL"):
        composition._providers()


@pytest.mark.parametrize("legacy", ["high", "max"])
def test_composition_maps_legacy_effort_with_note(clean_env, capsys, legacy):
    """A pre-picker HARNESS_EFFORT (the cloud API's high/max) must not stop the
    daemon at startup: it folds into xhigh with a stderr note. Anything else
    unknown is still the provider's ValueError."""
    clean_env.setenv("HARNESS_EFFORT", legacy)
    main, _, _, _ = composition._providers()
    assert main.effort == "xhigh", (
        "HARNESS_EFFORT high/max fold to xhigh in composition._env_effort; the "
        "provider stays strict — don't move the mapping into it"
    )
    assert f"HARNESS_EFFORT={legacy}" in capsys.readouterr().err
    clean_env.setenv("HARNESS_EFFORT", "turbo")
    with pytest.raises(ValueError, match="unknown effort"):
        composition._providers()


def test_composition_local_lane_needs_no_api_key(clean_env):
    """The whole local lane (main + Apple FM utilities + local compaction)
    runs without any Anthropic key — clean_env sets no key."""
    main, utility, compaction, _ = composition._providers()
    assert main.is_local and isinstance(utility, AppleFMProvider)
    assert compaction.is_local


def test_build_agent_lane(clean_env):
    """build_agent's lane= option: default chat, "code" on request. Nothing
    in the app passes "code" any more — agentd's background runs ride chat
    (pinned by test_agentd_background_runs_ride_the_chat_lane)."""
    agent, _ = composition.build_agent()
    assert agent.provider.lane == "chat"
    agent, _ = composition.build_agent(lane="code")
    assert agent.provider.lane == "code"
    assert agent.compactor.summarizer.lane == "code"


def test_build_agent_wires_parent_effort_into_browser_delegate(clean_env):
    agent, _ = composition.build_agent()
    tool = agent.registry.get("browser_delegate")
    assert tool.parent_effort() is None
    agent.current_effort = "low"
    assert tool.parent_effort() == "low"


# -- web UI picker ----------------------------------------------------------------


def test_webui_effort_picker_matches_vocabulary():
    html = (WEBUI / "index.html").read_text()
    m = re.search(r'<select id="chat-effort".*?</select>', html, re.S)
    assert m, "#chat-effort select missing from index.html"
    values = re.findall(r'<option value="(\w+)"', m.group(0))
    # The effort vocabulary, plus exactly ONE Sonnet entry (pinned medium
    # server-side), shipped hidden until /api/health reveals availability.
    assert values == list(EFFORTS) + ["sonnet"]
    assert re.search(r'<option value="low" selected>', m.group(0))
    assert re.search(r'<option value="sonnet" hidden>', m.group(0))
    js = (WEBUI / "picker.js").read_text()
    assert 'const EFFORTS = ["off", "low", "medium", "xhigh"];' in js


def test_webui_effort_picker_is_16px():
    """iPad Safari zooms on focus of any control under 16px."""
    css = (WEBUI / "chat.css").read_text()
    rule = re.search(r"#session-select, #chat-effort \{([^}]*)\}", css)
    assert rule and "font-size: 16px" in rule.group(1)


def test_agentd_background_runs_ride_the_chat_lane(clean_env):
    """Pin (decision 2026-10-25): agentd's ORCHESTRATOR factory builds cron and
    watcher agents on the CHAT lane — main provider AND compaction — so a chat
    waits behind a run's in-flight call instead of cutting it (the cut + 300s
    hold stalled a background run for ~an hour). Drives agentd's real factory
    seam over the real build_agent."""
    from harness.core.policy import Autonomy
    from harness.gateway.agentd import background_agent_factory

    def daemon_factory(autonomy, **kwargs):
        agent, _ = composition.build_agent(autonomy=autonomy, **kwargs)
        return agent

    for autonomy in (Autonomy.SUPERVISED, Autonomy.AUTONOMOUS):
        agent = background_agent_factory(daemon_factory)(autonomy)
        assert agent.provider.lane == "chat"
        assert agent.compactor.summarizer.lane == "chat"
        assert agent.provider.client.default_headers.get("X-Lane") is None
