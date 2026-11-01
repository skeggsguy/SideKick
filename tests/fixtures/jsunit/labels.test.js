/* labels.test.js — BK-041: the activity row's words (labels.js). The row
   used to read "Working…" for everything between thinking and a tool's
   result; now each phase says what is happening. DOM-free. */

import { labelFor, toolPhase, preparePhase, approvalOverlay, APPROVAL } from "./labels.js";
import { test, eq, check } from "./runner.js";

test("labels: thinking_done → Deciding next step… (no 'Working…')", () => {
  eq("labels.thinking_done", labelFor({ type: "thinking_done" }),
     { label: "Deciding next step…", waiting: false });
});

test("labels: tool_start → Preparing <noun>…", () => {
  eq("labels.start.web_search", labelFor({ type: "tool_start", tool: "web_search" }),
     { label: "Preparing web search…", waiting: false });
  eq("labels.start.gmail", preparePhase({ tool: "gmail_search" }), "Preparing email search…");
  eq("labels.start.call_tool", preparePhase({ tool: "call_tool" }), "Preparing a tool call…");
  eq("labels.start.unknown", preparePhase({ tool: "sandbox_run" }), "Preparing sandbox_run…");
  eq("labels.start.empty", preparePhase({}), "Preparing a tool call…");
});

test("labels: tool_call keeps the running phrases (call_tool unwraps)", () => {
  eq("labels.call.web_search", toolPhase({ tool: "web_search" }), "Searching the web…");
  eq("labels.call.shim", toolPhase({ tool: "call_tool", args: { name: "calendar_list" } }),
     "Checking your calendar…");
  eq("labels.call.unknown", toolPhase({ tool: "sandbox_run" }), "Using sandbox_run…");
});

test("labels: waits earn the slow hint, active phases don't", () => {
  eq("labels.model_wait", labelFor({ type: "model_wait" }),
     { label: "Waiting for model…", waiting: true });
  eq("labels.reading", labelFor({ type: "status", phase: "reading", pct: 35 }),
     { label: "Reading conversation · 35%", waiting: true });
  eq("labels.busy", labelFor({ type: "status", phase: "busy" }).waiting, true);
  eq("labels.delta", labelFor({ type: "delta", text: "x" }).label, "Writing…");
  eq("labels.result", labelFor({ type: "tool_result" }).label, "Reading results…");
  eq("labels.bookkeeping", labelFor({ type: "turn_complete" }), null);
  eq("labels.nullsafe", labelFor(null), null);
});

test("labels: no event maps to the old catch-all", () => {
  const types = ["model_wait", "thinking_delta", "thinking_done", "compaction_start",
                 "delta", "tool_start", "tool_call", "tool_result"];
  const words = types.map((type) => (labelFor({ type, tool: "x" }) || {}).label);
  check("labels.no_working", !words.includes("Working…"), words.join(" / "));
  eq("labels.approval", APPROVAL, "Waiting for your approval…");
});

test("labels: the approval overlay covers, holds, then restores (G1)", () => {
  const tool = { label: "Checking your calendar…", waiting: false };
  const appr = { label: APPROVAL, waiting: false };
  const on = approvalOverlay(tool, null, true);
  eq("overlay.on", on, { show: appr, covered: tool });
  // The next poll while still pending: the row shows APPROVAL, memory kept.
  const held = approvalOverlay(appr, on.covered, true);
  eq("overlay.held", held, { show: appr, covered: tool });
  eq("overlay.restore", approvalOverlay(appr, held.covered, false), { show: tool, covered: null });
  // No overlay, nothing pending: the row is left alone.
  eq("overlay.idle", approvalOverlay(tool, null, false), { show: null, covered: null });
});
