"""In-process speech-to-text via faster-whisper (ctranslate2).

Runs on the GPU when available: device="auto" tries CUDA first and falls back
to CPU, then stays on CPU for the process lifetime — CUDA failures (missing
libs, driver, VRAM) don't self-heal, and a failed init costs seconds per
request; restart agentd to retry the GPU. The faster_whisper import and model
load are deferred to first use so the repo imports and tests run without the
dependency installed. One lock serializes load AND inference: it prevents a
double-load on concurrent first requests, and parallel decodes would only
thrash anyway (single-user app).

stream() yields each whisper segment as it decodes. If the client disconnects
mid-stream the generator closes and the lock releases, but the decode thread
drains to completion in the background (ctranslate2 can't be interrupted) —
an immediate re-dictation may briefly overlap inference; thrash, not
corruption. Failures degrade like OllamaProvider: transcribe() returns None,
stream() raises, last_error is stashed either way.
"""

from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import AsyncIterator

# Derived per actual device when compute_type isn't given explicitly.
_COMPUTE = {"cuda": "int8_float16", "cpu": "int8"}

_DONE = object()  # worker → async-side sentinel: segment stream finished


def _preload_cuda_libs() -> None:
    """ctranslate2 dlopens libcublas/libcudnn by soname and won't search pip
    wheel dirs — preload every .so from the nvidia wheels (RTLD_GLOBAL so the
    later dlopen resolves symbols against these handles). The full glob is
    deliberate: preloading a subset is the classic way this breaks across
    cuDNN point releases."""
    import ctypes
    import importlib
    import pathlib

    for pkg in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
        mod = importlib.import_module(pkg)
        for so in sorted(pathlib.Path(mod.__path__[0]).glob("*.so*")):
            ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)


class WhisperTranscriber:
    def __init__(
        self,
        model_name: str = "distil-large-v3",
        device: str = "auto",
        compute_type: str | None = None,
    ) -> None:
        self._model_name = model_name
        self._device_pref = device  # auto | cuda | cpu
        self._compute_type = compute_type  # None → per-device default
        self._model = None
        self._lock = asyncio.Lock()
        self._cuda_failed = False  # sticky: auto never retries cuda after a failure
        self.device: str | None = None  # device actually loaded on
        self.last_error: str | None = None

    async def transcribe(self, data: bytes) -> str | None:
        """Audio bytes (any PyAV-decodable container) -> text, or None on failure."""
        try:
            parts = [t async for t in self.stream(data)]
        except Exception:
            return None  # stream() already stashed last_error
        return " ".join(parts).strip()

    async def stream(self, data: bytes) -> AsyncIterator[str]:
        """Audio bytes -> segment texts, yielded as whisper decodes them.

        Raises on failure (last_error set). A CUDA failure before any segment
        has been yielded gets one CPU retry (device="auto" only); after a
        partial yield it raises immediately — the caller already has text and
        a silent re-run would duplicate it.
        """
        async with self._lock:
            for attempt in (1, 2):
                yielded = False
                try:
                    if self._model is None:
                        # A failed load is not cached: the next request retries.
                        self._model = await asyncio.to_thread(self._load_best)
                    async for text in self._drain(data):
                        yielded = True
                        yield text
                    self.last_error = None
                    return
                except Exception as e:
                    if (
                        not yielded
                        and attempt == 1
                        and self.device == "cuda"
                        and self._device_pref == "auto"
                    ):
                        print(
                            f"[stt] cuda inference failed ({type(e).__name__}: {e}):"
                            " falling back to cpu for this process",
                            file=sys.stderr,
                        )
                        self._model = None  # drop the cuda model (frees VRAM)
                        self._cuda_failed = True
                        continue
                    self.last_error = f"{type(e).__name__}: {e}"
                    raise

    async def _drain(self, data: bytes) -> AsyncIterator[str]:
        # Caller holds the lock. A worker thread drains the lazy segment
        # generator, handing each segment to the loop; on failure the
        # exception object arrives as the queue's final item.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        model = self._model

        def worker() -> None:
            try:
                segments, _info = model.transcribe(
                    io.BytesIO(data), language="en", beam_size=5, vad_filter=True
                )
                for seg in segments:
                    loop.call_soon_threadsafe(queue.put_nowait, seg.text.strip())
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)
            except Exception as e:  # re-raised on the async side below
                loop.call_soon_threadsafe(queue.put_nowait, e)

        future = loop.run_in_executor(None, worker)
        while True:
            item = await queue.get()
            if item is _DONE:
                await future  # worker already returned; surfaces stray errors
                return
            if isinstance(item, Exception):
                raise item
            if item:  # skip segments that strip to empty
                yield item

    def _load_best(self):
        # Runs in a thread. Resolves the device *preference* to a real load;
        # sets self.device on success. An unknown preference behaves as auto.
        if self._device_pref == "cpu" or self._cuda_failed:
            device = "cpu"
        elif self._device_pref == "cuda":
            device = "cuda"  # explicit pin: failure raises, no fallback
        else:
            try:
                model = self._load("cuda")
                self.device = "cuda"
                return model
            except Exception as e:
                print(
                    f"[stt] cuda unavailable ({type(e).__name__}: {e}):"
                    " falling back to cpu for this process",
                    file=sys.stderr,
                )
                self._cuda_failed = True
                device = "cpu"
        model = self._load(device)
        self.device = device
        return model

    def _load(self, device: str):
        # Runs in a thread; the only faster_whisper import site.
        from faster_whisper import WhisperModel

        if device == "cuda":
            _preload_cuda_libs()
        compute = self._compute_type or _COMPUTE[device]
        return WhisperModel(self._model_name, device=device, compute_type=compute)
