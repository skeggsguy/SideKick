# STT setup — dictation (mic button → local Whisper)

The chat composer has a 🎤 button: tap to record, tap again to stop; the audio
is transcribed **locally** by faster-whisper (in-process; CUDA GPU when
present, else CPU) and the text streams into the chat input for review — never
auto-sent, never sent to a cloud STT service.

## Install

> **Status (Mac mini, 2026-09-28): off** (`HARNESS_STT=off`, mic hidden). On
> the Mac it would run on CPU; an MLX Whisper/Parakeet backend is an open item
> in docs/macos-host.md.

Nothing to install beyond the repo's own deps — `faster-whisper` is a normal
project dependency (no sidecar service, no ffmpeg: bundled PyAV decodes the
browser containers). The NVIDIA `cuda` extra (`uv sync --extra cuda`, Linux
only) adds the cuBLAS/cuDNN wheels, which `stt.py:_preload_cuda_libs`
ctypes-preloads because ctranslate2's dlopen doesn't search pip package dirs. The model downloads from HuggingFace to
`~/.cache/huggingface` on the **first** transcription (~1.5 GB for
distil-large-v3) and lazy-loads into the daemon from then on.

Knobs (.env):

- `HARNESS_STT=off` — disable entirely: POST `/api/transcribe` returns 503
  and the mic button is hidden too (the web UI shows it only when GET
  `/api/transcribe` reports `{"enabled": true}`; restart agentd, then reload).
- `HARNESS_STT_MODEL=distil-large-v3` — any faster-whisper model name.
  `small.en` is the light/fast fallback (~190 MB), `large-v3-turbo` the
  multilingual alternative. Note the transcribe call pins `language="en"`
  regardless of model.
- `HARNESS_STT_DEVICE=auto` — `auto` tries CUDA and falls back to CPU;
  `cuda`/`cpu` pin it (a pinned `cuda` failure is an error, retried per
  request, never silently downgraded).

## CPU vs GPU

On the Mac there is no CUDA: `auto` probes it once, logs a single
`[stt] cuda unavailable (...)` note, and runs on CPU (compute_type int8) from
then on — expected, not an error. CPU distil-large-v3 adds ~1.5–2 GB RSS to
agentd; drop to `HARNESS_STT_MODEL=small.en` if dictation feels slow.

**Fallback is sticky** (on a CUDA box): if a CUDA load or first-segment
inference fails (missing libs, driver, VRAM spike), stt prints that note,
switches to CPU and stays there — CUDA failures don't self-heal and a failed
init costs seconds per request. Restart agentd to retry the GPU.

## Verify

1. `uv run pytest tests/test_stt.py tests/test_webui_v2.py` — offline, the
   real model never loads.
2. Optional pre-pull (avoids the ~1.5 GB download inside the first dictation):
   `uv run python -c "from faster_whisper import WhisperModel; WhisperModel('distil-large-v3', device='cpu', compute_type='int8')"`
3. Restart agentd (docs/macos-host.md), open the web UI on desktop (localhost)
   → record a sentence → words stream into the input while the mic pulses
   amber. Or curl it:
   `curl -N -X POST --data-binary @clip.m4a http://127.0.0.1:8787/api/transcribe`
   → `: connected`, `data: {"type":"segment",...}`, `data: {"type":"done"}`.
4. iPad Safari over the tailnet https URL: permission prompt on first tap,
   red pulsing button while recording, amber + input glow while transcribing,
   text streaming in (mp4/AAC path). Deny the permission once to see the
   error message path.
5. Troubleshooting: 503 = `HARNESS_STT=off`; an in-stream error event =
   decode/model failure — check `~/Library/Logs/harness/agentd.log`. Dictation
   slow = CPU inference; see "CPU vs GPU".
