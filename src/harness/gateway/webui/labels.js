/* labels.js — the activity row's words (BK-041): one pure map from a stream
   event to what #chat-status says. DOM-free, so the jsunit tier pins it;
   status.js applies it on every event, and a mid-run join derives its first
   label from the run's last phase event through the same map. Every label is
   fixed copy or a tool name — the caller still sets it via textContent. */
"use strict";

export const APPROVAL = "Waiting for your approval…";

/* [name pattern, running (tool_call), composing (tool_start)]. Whole phrases —
   "Using Searching email…" would be absurd; unknown tools fall back to the
   raw name. */
const TOOLS = [
  [/^gmail_/, "Searching email", "email search"],
  [/^calendar_/, "Checking your calendar", "calendar check"],
  [/^drive_/, "Searching Drive", "Drive search"],
  [/^task_/, "Checking your tasks", "task check"],
  [/^web_fetch$/, "Reading a web page", "page fetch"],
  [/^web_search$/, "Searching the web", "web search"],
  [/^load_tools$/, "Loading tools", "tool load"],
  [/^memory_/, "Checking memory", "memory check"],
  [/^schedule_/, "Checking schedules", "schedule check"],
  [/^browser_/, "Using the browser", "browser step"],
];

function lookup(name) {
  return TOOLS.find(([re]) => re.test(name)) || null;
}

/* A tool_call: call_tool (the deferred-MCP shim) wraps the real name in args. */
export function toolPhase(ev) {
  let name = ev.tool || "";
  if (name === "call_tool" && ev.args && typeof ev.args.name === "string") {
    name = ev.args.name;
  }
  const hit = lookup(name);
  return hit ? `${hit[1]}…` : `Using ${name}…`;
}

/* A tool_start: the tool_use block began and its args are still streaming —
   for call_tool that means the real name isn't known yet. */
export function preparePhase(ev) {
  const name = ev.tool || "";
  if (!name || name === "call_tool") return "Preparing a tool call…";
  const hit = lookup(name);
  return `Preparing ${hit ? hit[2] : name}…`;
}

/* {label, waiting} for an event that changes the row, else null. `waiting`
   phases earn the 20 s slow-model hint; active ones don't. */
export function labelFor(ev) {
  const t = ev && ev.type;
  if (t === "model_wait") return { label: "Waiting for model…", waiting: true };
  if (t === "status" && ev.phase === "busy")
    return { label: "Model busy with another request…", waiting: true };
  if (t === "status" && ev.phase === "reading")
    return { label: `Reading conversation · ${ev.pct}%`, waiting: true };
  if (t === "thinking_delta") return { label: "Thinking…", waiting: false };
  // Thinking ended; next comes answer text (Writing…) or a tool_use block.
  if (t === "thinking_done") return { label: "Deciding next step…", waiting: false };
  // The slow summarizer call gets the pulse + the 20 s hint (it IS the local
  // model taking a while). Cleared by the next phase, or — when the run dies
  // mid-summarizer with no compaction frame — by the terminal statusStop().
  if (t === "compaction_start") return { label: "Compacting…", waiting: true };
  if (t === "delta") return { label: "Writing…", waiting: false };
  if (t === "tool_start") return { label: preparePhase(ev), waiting: false };
  if (t === "tool_call") return { label: toolPhase(ev), waiting: false };
  if (t === "tool_result") return { label: "Reading results…", waiting: false };
  return null;
}

/* The approval overlay (BK-041 G1): while the on-screen chat has a pending
   approval, the row says APPROVAL instead of the tool's label; once it
   clears, the label it covered comes back. `shown` is what the row says now
   ({label, waiting}), `covered` what an earlier overlay remembered (or
   null). Returns the label to show (null = leave the row alone) and the new
   memory. Any real phase event drops the memory (status.js), so a run that
   moved on is never rolled back to a stale label. */
export function approvalOverlay(shown, covered, pending) {
  if (pending) return { show: { label: APPROVAL, waiting: false }, covered: covered || shown };
  if (covered) return { show: covered, covered: null };
  return { show: null, covered: null };
}
