/* Dictation: mic button, MediaRecorder capture, POST to /api/transcribe,
   finite-SSE segment streaming into the composer. iPad Safari records
   mp4/AAC, Chrome/Firefox webm/opus; the backend (PyAV) decodes either.
   Button stays hidden unless recording can actually work here (secure
   context — https or localhost — is a hard getUserMedia requirement) AND
   the server has STT on (HARNESS_STT; a failed status fetch = hidden). */
"use strict";

import { $, api } from "./dom.js";
import { addErrorMsg } from "./transcript.js";

const MIC_MIME = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"]
  .find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m));
const micBtn = $("#chat-mic");
let micState = "idle"; // idle → recording → transcribing → idle
let micRecorder = null, micStream = null, micChunks = [], micTimer = null;
let autosize = () => {}; // injected by app.js (composer owns the textarea)

export function initDictation(autosizeFn) {
  autosize = autosizeFn;
  if (micBtn && window.isSecureContext && navigator.mediaDevices?.getUserMedia && MIC_MIME) {
    api("/api/transcribe")
      .then((s) => { if (s?.enabled === true) enableMic(); })
      .catch(() => { /* status unknown → leave the mic hidden */ });
  }
}

function enableMic() {
  micBtn.hidden = false;
  syncMicWidth();
  // The phone media query resizes Send, so re-measure when the query flips
  // (orientation change across 600px) — rAF lets layout settle first.
  matchMedia("(max-width: 600px)").addEventListener("change", () =>
    requestAnimationFrame(syncMicWidth)
  );
  micBtn.addEventListener("click", onMicTap);
}

// Match Send's content-driven width exactly (emoji is narrower than "Send").
// Called from app.js's setSendMode too — Send changes width with its label.
export function syncMicWidth() {
  const w = $("#chat-send").offsetWidth;
  if (w) micBtn.style.width = `${w}px`;
}

async function onMicTap() {
  if (micState === "recording") {
    clearTimeout(micTimer);
    micRecorder.stop(); // → onstop → finishDictation
    return;
  }
  if (micState !== "idle") return; // transcribing (button disabled anyway)
  try {
    // The iPad home-screen PWA re-prompts for the mic EVERY launch (webkit 215884) — not
    // ours to fix: iOS Settings → Apps → Safari → Microphone → Allow.
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    addErrorMsg(`microphone: ${err.message}`);
    return;
  }
  micChunks = [];
  micRecorder = new MediaRecorder(micStream, { mimeType: MIC_MIME });
  micRecorder.ondataavailable = (e) => { if (e.data.size) micChunks.push(e.data); };
  micRecorder.onstop = finishDictation;
  micRecorder.start();
  micState = "recording";
  micBtn.classList.add("recording");
  micBtn.setAttribute("aria-pressed", "true");
  micTimer = setTimeout(() => {
    if (micRecorder.state === "recording") micRecorder.stop();
  }, 60000);
}

async function finishDictation() {
  micStream.getTracks().forEach((t) => t.stop());
  micBtn.classList.remove("recording");
  micBtn.setAttribute("aria-pressed", "false");
  micState = "transcribing";
  micBtn.disabled = true;
  micBtn.classList.add("transcribing");
  const inp = $("#chat-input");
  inp.classList.add("dictating");
  try {
    const blob = new Blob(micChunks, { type: MIC_MIME });
    const resp = await fetch("/api/transcribe", {
      method: "POST",
      headers: { "Content-Type": blob.type },
      body: blob,
    });
    if (!resp.ok) {
      const detail = (await resp.json().catch(() => null))?.detail;
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    // Finite SSE: segments land in the textarea's .value as whisper decodes
    // them (inert — no DOM parsing, so untrusted transcript text is safe).
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let first = true;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!line.startsWith("data: ")) continue; // ": connected" / ": ping"
        const ev = JSON.parse(line.slice(6));
        if (ev.type === "segment") {
          if (first) {
            inp.value = inp.value.trim()
              ? inp.value.replace(/\s*$/, " ") + ev.text
              : ev.text;
            first = false;
          } else {
            inp.value += ` ${ev.text}`;
          }
          autosize(); // grow live as segments stream in
        } else if (ev.type === "error") {
          throw new Error(ev.detail);
        }
      }
    }
    inp.focus();
  } catch (err) {
    addErrorMsg(`dictation failed: ${err.message}`);
  } finally {
    micBtn.disabled = false;
    micBtn.classList.remove("transcribing");
    inp.classList.remove("dictating");
    micState = "idle";
    micRecorder = null; micStream = null; micChunks = [];
  }
}
