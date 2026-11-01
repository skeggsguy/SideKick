"""WhisperTranscriber unit tests — no faster_whisper import anywhere here.

The real model load is monkeypatched out via the _load(device) seam; these pin
the lazy single-load behaviour under concurrency, the Ollama-style degradation
(None + last_error, failed load not cached), the auto cuda→cpu sticky
fallback, and the streaming segment order/error semantics.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from harness.gateway.stt import WhisperTranscriber


class StubModel:
    def transcribe(self, buf, **kwargs):
        segments = iter([SimpleNamespace(text=" hi "), SimpleNamespace(text=" there ")])
        return segments, {"language": "en"}


async def test_concurrent_first_requests_load_once(monkeypatch):
    t = WhisperTranscriber()
    loads = 0

    def fake_load(self, device):
        nonlocal loads
        loads += 1
        return StubModel()

    monkeypatch.setattr(WhisperTranscriber, "_load", fake_load)
    results = await asyncio.gather(*(t.transcribe(b"x") for _ in range(5)))
    assert loads == 1
    assert results == ["hi there"] * 5
    assert t.last_error is None
    assert t.device == "cuda"  # auto preference, cuda load succeeded


async def test_failed_load_degrades_and_retries(monkeypatch):
    t = WhisperTranscriber(device="cpu")
    calls = 0

    def fake_load(self, device):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("no model file")
        return StubModel()

    monkeypatch.setattr(WhisperTranscriber, "_load", fake_load)
    assert await t.transcribe(b"x") is None
    assert "no model file" in t.last_error
    # A failed load is not cached — the next request retries and succeeds.
    assert await t.transcribe(b"x") == "hi there"
    assert calls == 2
    assert t.last_error is None


async def test_auto_cuda_load_failure_falls_back_cpu_sticky(monkeypatch):
    t = WhisperTranscriber(device="auto")
    devices: list[str] = []

    def fake_load(self, device):
        devices.append(device)
        if device == "cuda":
            raise RuntimeError("libcudnn not found")
        return StubModel()

    monkeypatch.setattr(WhisperTranscriber, "_load", fake_load)
    assert await t.transcribe(b"x") == "hi there"
    assert t.device == "cpu"
    assert t.last_error is None  # the fallback succeeded; not an error
    assert devices == ["cuda", "cpu"]
    # Sticky: a fresh load (e.g. after the fallback model is dropped) goes
    # straight to cpu — cuda is never retried within the process.
    t._model = None
    assert await t.transcribe(b"x") == "hi there"
    assert devices == ["cuda", "cpu", "cpu"]


async def test_explicit_cuda_never_falls_back(monkeypatch):
    t = WhisperTranscriber(device="cuda")
    devices: list[str] = []

    def fake_load(self, device):
        devices.append(device)
        raise RuntimeError("cuda broken")

    monkeypatch.setattr(WhisperTranscriber, "_load", fake_load)
    assert await t.transcribe(b"x") is None
    assert "cuda broken" in t.last_error
    # No cpu attempt, and the failed load is still retried (on cuda) next call.
    assert await t.transcribe(b"x") is None
    assert devices == ["cuda", "cuda"]


async def test_explicit_cpu_never_tries_cuda(monkeypatch):
    t = WhisperTranscriber(device="cpu")
    devices: list[str] = []

    def fake_load(self, device):
        devices.append(device)
        return StubModel()

    monkeypatch.setattr(WhisperTranscriber, "_load", fake_load)
    assert await t.transcribe(b"x") == "hi there"
    assert devices == ["cpu"]
    assert t.device == "cpu"


async def test_stream_yields_segments_in_order(monkeypatch):
    t = WhisperTranscriber(device="cpu")
    monkeypatch.setattr(WhisperTranscriber, "_load", lambda self, device: StubModel())
    assert [s async for s in t.stream(b"x")] == ["hi", "there"]
    assert t.last_error is None


async def test_cuda_inference_failure_retries_on_cpu(monkeypatch):
    """A cuda failure at decode time (not load time) — e.g. VRAM exhaustion —
    still falls back to cpu, once, before any segment has been yielded."""

    class ExplodingModel:
        def transcribe(self, buf, **kwargs):
            raise RuntimeError("CUDA out of memory")

    t = WhisperTranscriber(device="auto")
    devices: list[str] = []

    def fake_load(self, device):
        devices.append(device)
        return ExplodingModel() if device == "cuda" else StubModel()

    monkeypatch.setattr(WhisperTranscriber, "_load", fake_load)
    assert await t.transcribe(b"x") == "hi there"
    assert devices == ["cuda", "cpu"]
    assert t.device == "cpu"
    assert t._cuda_failed is True
    assert t.last_error is None


async def test_midstream_error_raises_after_partial(monkeypatch):
    """After a partial yield there is no retry — the caller already has text
    and a silent re-run would duplicate it."""

    class PartialModel:
        def transcribe(self, buf, **kwargs):
            def gen():
                yield SimpleNamespace(text=" hi ")
                raise RuntimeError("decoder died")

            return gen(), {}

    t = WhisperTranscriber(device="auto")
    devices: list[str] = []

    def fake_load(self, device):
        devices.append(device)
        return PartialModel()

    monkeypatch.setattr(WhisperTranscriber, "_load", fake_load)
    got: list[str] = []
    with pytest.raises(RuntimeError, match="decoder died"):
        async for s in t.stream(b"x"):
            got.append(s)
    assert got == ["hi"]
    assert "decoder died" in t.last_error
    assert devices == ["cuda"]  # no cpu retry after the partial yield


async def test_lock_serializes_streams(monkeypatch):
    active = 0
    max_active = 0

    class SlowModel:
        def transcribe(self, buf, **kwargs):
            def gen():
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                time.sleep(0.02)
                yield SimpleNamespace(text=" hi ")
                active -= 1

            return gen(), {}

    monkeypatch.setattr(WhisperTranscriber, "_load", lambda self, device: SlowModel())
    t = WhisperTranscriber(device="cpu")
    results = await asyncio.gather(*(t.transcribe(b"x") for _ in range(3)))
    assert results == ["hi"] * 3
    assert max_active == 1
