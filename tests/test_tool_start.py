"""BK-041 G3: the provider fires on_tool_start(name) the moment a tool_use
block STARTS streaming — before its args exist — so the live row can say
"Preparing web search…" instead of a vague "Working…" while the model
composes the call. Text and thinking blocks never fire it."""

from __future__ import annotations

from types import SimpleNamespace as NS

from harness.providers.anthropic_provider import AnthropicProvider

class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()

    async def get_final_message(self):
        return "final"

def _start(i, btype, name=None):
    block = NS(type=btype, name=name) if name else NS(type=btype)
    return NS(type="content_block_start", index=i, content_block=block)

async def test_tool_use_block_start_fires_on_tool_start_with_its_name():
    events = [
        _start(0, "thinking"),
        NS(type="content_block_stop", index=0),
        _start(1, "text"),
        NS(type="content_block_delta", index=1, delta=NS(type="text_delta", text="ok")),
        _start(2, "tool_use", "web_search"),
        NS(type="content_block_stop", index=2),
    ]
    p = AnthropicProvider(api_key="x", base_url="http://127.0.0.1:9")
    p.client = NS(messages=NS(stream=lambda **kw: _FakeStream(events)))
    started, texts, thought = [], [], []
    msg, _ = await p._stream({}, texts.append, None, lambda: thought.append(1), started.append)
    assert msg == "final"
    assert started == ["web_search"], "only the tool_use block fires, once, with its name"
    assert texts == ["ok"] and thought == [1]

async def test_no_hook_no_crash():
    events = [_start(0, "tool_use", "gmail_search")]
    p = AnthropicProvider(api_key="x", base_url="http://127.0.0.1:9")
    p.client = NS(messages=NS(stream=lambda **kw: _FakeStream(events)))
    await p._stream({}, None, None, None)  # on_tool_start omitted: default None
