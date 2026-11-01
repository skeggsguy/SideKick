"""Approval-preview resolvers: real, human-readable context for gated calls.

Many gated tools carry only opaque ids (file_id, trigger_id, message_id) in
their args — approving on raw args alone would be content-blind. Each resolver
here live-fetches what the id refers to at REVIEW time, via already-adapted
READ_ONLY tools (never auth/tokens directly), so the human decides on trusted
server-fetched facts — never on model-supplied labels, which injected content
could spoof (tests/test_mcp.py pins the dispatched reads READ_ONLY).

RESOLVER_TOOLS / SELF_DESCRIBING_GATED are the single source of truth for
"how does this gated tool justify itself at approval time" — every gated tool
must appear in exactly one (pinned in tests/test_invariants.py). Adding a
gated tool? See docs/adding-a-tool.md, "Approval context".
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .preview_browser import element_fields, typed_fields

if TYPE_CHECKING:
    from ..store.client_config import ClientConfigStore
    from ..store.triggers import TriggerStore
    from ..tools.base import Tool

#: Previewable tools -> the READ_ONLY reads their resolver dispatches
#: (() = resolved from local stores, no MCP). Keys drive the API's
#: `previewable` flag; values are documentation + the READ_ONLY test pin.
RESOLVER_TOOLS: dict[str, tuple[str, ...]] = {
    "gmail_send_draft": ("gmail_get_draft",),
    "gmail_trash_message": ("gmail_read",),
    "gmail_draft_reply": ("gmail_read",),
    "gmail_download_attachment": ("gmail_read",),
    "drive_trash_file": ("drive_file_info",),
    "drive_move": ("drive_file_info",),  # called twice: file + dest folder
    "drive_rename": ("drive_file_info",),
    "drive_download": ("drive_file_info",),
    "drive_update_content": ("drive_file_info",),
    "docs_append_text": ("drive_file_info",),
    "docs_edit_text": ("drive_file_info",),
    "sheets_append": ("drive_file_info",),
    "sheets_update": ("drive_file_info",),
    "calendar_event_update": ("calendar_get_event",),
    "calendar_event_attendees": ("calendar_get_event",),
    "calendar_event_respond": ("calendar_get_event",),
    "calendar_event_delete": ("calendar_get_event",),
    "task_update": ("tasks_list",),
    "task_complete": ("tasks_list",),
    "schedule_edit": (),
    "schedule_delete": (),
    "schedule_disable": (),
    # Lane B typing: when the text carries identity-ref placeholders the human
    # must see the REAL value that will be filled (fetched live from the local
    # client-config store, () = no MCP read — the schedule_* precedent). Without
    # placeholders it just echoes the text arg.
    "browser_type": (),
    # A bare [eN] handle names nothing reviewable (an approval read "handle: e74",
    # 2026-11-01): resolved from the parked run's live tab (preview_browser.py).
    "browser_click": (),
    "browser_select": (),
}

#: Gated tools whose raw args already carry everything a human needs
#: (recipients, titles, content — no opaque ids, nothing consequential
#: implicit). Reviewed explicitly on addition; the invariant test forces
#: every gated tool to choose between this set and RESOLVER_TOOLS.
SELF_DESCRIBING_GATED: frozenset[str] = frozenset(
    {
        # in-process
        "remember",  # content + category in args
        "forget",  # fact ids — the web UI fetches fact text client-side
        "notify",  # title + body; destination pinned server-side
        "edit_file",  # path + old/new strings (write-jailed)
        "copy_file",  # src/dest paths, overwrite flag (dest write-jailed)
        "sandbox_run",  # the command itself is the reviewed content
        "schedule_add",  # label/cron/prompt/autonomy/tools all in args
        # conditionally-gated egress tools (secret/probe taint, egress
        # budget, destination gate — core/policy.py): the url/query args
        # ARE the full reviewed content, nothing to resolve server-side
        "web_fetch",  # url in args
        "rss_fetch",  # url in args
        "reddit_comments",  # url in args
        "hn_comments",  # url in args
        "browser_fetch",  # url in args
        "web_search",  # query in args
        "weather",  # place in args
        "maps_search",  # query in args
        "maps_route",  # origin/destination in args
        # Lane B interactive browser — url / direction in args, nothing to
        # resolve server-side (snapshot/read/scroll act on the current tab).
        # click/select/type are in RESOLVER_TOOLS: a handle needs its element.
        "browser_navigate",  # url in args
        "browser_snapshot",  # no args — reads the current tab
        "browser_read",  # optional max_chars — reads the current tab
        "browser_scroll",  # direction/pixels in args
        # browser_delegate: the envelope IS the approval card — origin, task,
        # refs, and budget are all in args (the human approves the delegation
        # once at spawn). Nothing to resolve server-side.
        "browser_delegate",
        # google_workspace
        "gmail_send",  # to/subject/body in args
        "gmail_draft_create",
        "calendar_event_create",
        "task_create",  # title/notes/due in args
        "tasklist_create",
        "docs_create",  # title only
        "sheets_create",
        "drive_create_folder",
        "drive_upload",  # local path + name; folder_id minor (own Drive)
    }
)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_SNIPPET_CHARS = 300

#: Which arg holds the Drive file id, per drive-family tool.
_DRIVE_ID_ARG = {
    "drive_trash_file": "file_id",
    "drive_rename": "file_id",
    "drive_download": "file_id",
    "drive_update_content": "file_id",
    "docs_append_text": "doc_id",
    "docs_edit_text": "doc_id",
    "sheets_append": "spreadsheet_id",
    "sheets_update": "spreadsheet_id",
}


class PreviewError(Exception):
    """A resolver couldn't produce a preview. `status` maps to the HTTP reply:
    503 = the needed handle/store isn't wired (e.g. no Google connection),
    502 = the live read failed. Either way the UI degrades to raw args."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _clip(text: str, limit: int = _SNIPPET_CHARS) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


def _load_concatenated(text: str) -> "dict | list | None":
    """Decode a LIST-returning MCP tool's real wire shape: FastMCP serializes
    one TextContent per element and mcp/client.py joins the blocks with
    newlines, so json.loads sees `{...}\\n{...}` — concatenated objects, not
    an array. Returns the objects as a list (a lone object as a dict), or
    None when the text isn't pure concatenated JSON (plain-text MCP errors).
    """
    decoder = json.JSONDecoder()
    objs: list = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i == n:
            break
        try:
            obj, i = decoder.raw_decode(text, i)
        except ValueError:
            return None
        if not isinstance(obj, (dict, list)):  # "Error from ..." starts bare
            return None
        objs.append(obj)
    if not objs:
        return None
    return objs[0] if len(objs) == 1 else objs


class ApprovalPreviews:
    """Dispatches per-tool resolvers over live tool handles + local stores.

    `tool_handles` are LIVE adapted MCP tools by name (the tool_catalog
    snapshot has no callable handles); `triggers` backs the schedule_*
    resolvers. Missing pieces degrade per-call to PreviewError(503).
    """

    def __init__(
        self,
        *,
        tool_handles: dict[str, "Tool"] | None = None,
        triggers: "TriggerStore | None" = None,
        client_config: "ClientConfigStore | None" = None,
    ) -> None:
        self.handles = tool_handles or {}
        self.triggers = triggers
        self.client_config = client_config

    def previewable(self, tool: str) -> bool:
        return tool in RESOLVER_TOOLS

    async def resolve(self, tool: str, args: dict, run_id: str | None = None) -> dict:
        """Human-readable fields for a pending call, or PreviewError. `run_id`
        is the approval's run — the browser resolvers find its tab by it."""
        if tool == "gmail_send_draft":
            return await self._gmail_send_draft(args)
        if tool == "gmail_trash_message":
            return await self._gmail_message_summary(args, "message_id")
        if tool == "gmail_draft_reply":
            return await self._gmail_draft_reply(args)
        if tool == "gmail_download_attachment":
            return await self._gmail_download_attachment(args)
        if tool == "drive_move":
            return await self._drive_move(args)
        if tool in _DRIVE_ID_ARG:
            return await self._drive_info(args.get(_DRIVE_ID_ARG[tool], ""))
        if tool in (
            "calendar_event_update",
            "calendar_event_attendees",
            "calendar_event_respond",
            "calendar_event_delete",
        ):
            return await self._calendar_event(args)
        if tool in ("task_update", "task_complete"):
            return await self._task(args)
        if tool in ("schedule_edit", "schedule_delete", "schedule_disable"):
            return await self._schedule(args)
        if tool == "browser_type":
            typed = await typed_fields(args, self.client_config)
            if typed is None:
                raise PreviewError(503, "preview unavailable (no client config)")
            return {**(element_fields(args, run_id, "into") or {}), **typed}
        if tool in ("browser_click", "browser_select"):
            key = "will click" if tool == "browser_click" else "will select"
            fields = element_fields(args, run_id, key)
            if fields is None:
                raise PreviewError(404, "that element is no longer on the page")
            return fields
        raise PreviewError(404, f"{tool} is not previewable")

    # ---- shared -----------------------------------------------------------

    async def _call_json(self, handle_name: str, args: dict) -> dict | list:
        handle = self.handles.get(handle_name)
        if handle is None:
            raise PreviewError(503, f"preview unavailable (no {handle_name})")
        text = await handle.run(args)
        try:
            data = json.loads(text)
        except ValueError:
            data = _load_concatenated(text)  # list-returning tools' real shape
            if data is None:
                # MCP-level failures come back as plain text ("Error from ...").
                raise PreviewError(502, "preview failed") from None
        if not isinstance(data, (dict, list)):
            raise PreviewError(502, "preview failed")
        return data

    # ---- gmail ------------------------------------------------------------

    async def _gmail_send_draft(self, args: dict) -> dict:
        data = await self._call_json(
            "gmail_get_draft", {"draft_id": args.get("draft_id", "")}
        )
        if not isinstance(data, dict) or "body" not in data:
            raise PreviewError(502, "preview failed — review in Gmail")
        return data  # the full draft IS the content under review

    async def _gmail_message_summary(self, args: dict, id_arg: str) -> dict:
        data = await self._call_json(
            "gmail_read", {"message_id": args.get(id_arg, "")}
        )
        if not isinstance(data, dict) or "body" not in data:
            raise PreviewError(502, "preview failed — review in Gmail")
        # Compact — never ship the full (up to 50k-char) body.
        return {
            "id": data.get("id", ""),
            "from": data.get("from", ""),
            "subject": data.get("subject", ""),
            "date": data.get("date", ""),
            "snippet": _clip(data.get("body") or ""),
        }

    async def _gmail_draft_reply(self, args: dict) -> dict:
        # The consequential fact the args never show: who receives the reply.
        summary = await self._gmail_message_summary(args, "message_id")
        return {
            "replying_to": summary["from"],
            "subject": summary["subject"],
            "date": summary["date"],
            "snippet": summary["snippet"],
        }

    async def _gmail_download_attachment(self, args: dict) -> dict:
        data = await self._call_json(
            "gmail_read", {"message_id": args.get("message_id", "")}
        )
        if not isinstance(data, dict):
            raise PreviewError(502, "preview failed — review in Gmail")
        out = {"from": data.get("from", ""), "subject": data.get("subject", "")}
        idx = args.get("attachment_index", 0)
        attachments = data.get("attachments") or []
        if isinstance(idx, int) and 0 <= idx < len(attachments):
            att = attachments[idx]
            out["filename"] = att.get("filename", "")
            out["size_bytes"] = att.get("size", 0)
        else:
            out["note"] = f"no attachment at index {idx!r}"
        return out

    # ---- drive / docs / sheets --------------------------------------------

    async def _drive_info(self, file_id: str) -> dict:
        data = await self._call_json("drive_file_info", {"file_id": file_id})
        if not isinstance(data, dict) or "name" not in data:
            raise PreviewError(502, "preview failed — review in Drive")
        mime = data.get("mimeType", "")
        out = {
            "name": data.get("name", ""),
            "type": "folder" if mime == _FOLDER_MIME else mime,
            "modified": data.get("modifiedTime", ""),
        }
        if data.get("size"):
            out["size_bytes"] = data["size"]
        if data.get("trashed"):
            out["note"] = "already in Trash"
        return out

    async def _drive_move(self, args: dict) -> dict:
        file_info = await self._drive_info(args.get("file_id", ""))
        dest = await self._drive_info(args.get("folder_id", ""))
        return {
            "file": file_info["name"],
            "file_type": file_info["type"],
            "destination_folder": dest["name"],
        }

    # ---- calendar / tasks ---------------------------------------------------

    async def _calendar_event(self, args: dict) -> dict:
        # calendar_id must ride along or events on secondary calendars 404
        # (respond has no calendar_id arg — the default covers it).
        data = await self._call_json(
            "calendar_get_event",
            {
                "event_id": args.get("event_id", ""),
                "calendar_id": args.get("calendar_id") or "primary",
            },
        )
        if not isinstance(data, dict) or "summary" not in data:
            raise PreviewError(502, "preview failed — review in Calendar")
        out = {
            "current_title": data.get("summary", ""),
            "current_start": data.get("start", ""),
            "current_end": data.get("end", ""),
            "current_location": data.get("location", ""),
            "current_meet_link": data.get("meet_link", ""),
        }
        # Who the event belongs to and who is on it NOW — the facts a human
        # needs to judge an invite/remove/RSVP/delete (server-fetched, never
        # model-supplied). Additive: update's consumers read only current_*.
        if data.get("organizer"):
            out["organizer"] = data["organizer"]
        attendees = data.get("attendees") or []
        if attendees:
            out["attendees"] = _clip(
                ", ".join(
                    f"{a.get('email', '')} ({a.get('response') or 'no reply'})"
                    for a in attendees
                    if isinstance(a, dict)
                )
            )
        return out

    async def _task(self, args: dict) -> dict:
        # tasks has no get-by-id read; scan the list (best-effort past ~100).
        data = await self._call_json(
            "tasks_list", {"show_completed": True, "max_results": 100}
        )
        if not isinstance(data, list):
            raise PreviewError(502, "preview failed — review in Tasks")
        task_id = args.get("task_id", "")
        for t in data:
            if isinstance(t, dict) and t.get("id") == task_id:
                return {
                    "title": t.get("title", ""),
                    "list": t.get("list", ""),
                    "due": t.get("due", ""),
                    "status": t.get("status", ""),
                }
        return {"note": "task not found — it may be beyond the first 100"}

    # ---- schedule (local trigger store, no MCP) ------------------------------

    async def _schedule(self, args: dict) -> dict:
        if self.triggers is None:
            raise PreviewError(503, "preview unavailable (no trigger store)")
        trigger = await self.triggers.get(args.get("trigger_id", ""))
        if trigger is None:
            return {"note": "trigger no longer exists"}
        return {
            "label": trigger.label or trigger.playbook,
            "cron": trigger.spec,
            "kind": trigger.kind,
            "autonomy": trigger.autonomy,
            "tools": ", ".join(trigger.tools) if trigger.tools else "",
            "enabled": trigger.enabled,
            "prompt": _clip(trigger.playbook, 200),
        }
