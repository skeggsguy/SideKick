"""One-line English summaries of pending approvals for push bodies. Pure
formatting: tool name + raw args (+ an optional ApprovalPreviews resolve,
names/subjects only — never bodies or blobs) in, one lock-screen line out."""

from __future__ import annotations


def _cal_tag(args: dict) -> str:
    """' [<calendar_id>]' when the call targets a non-primary calendar."""
    cal = args.get("calendar_id") or "primary"
    return f" [{cal}]" if cal != "primary" else ""


def approval_summary(tool: str, args: dict, resolved: dict | None = None) -> str:
    """A one-line English summary of a pending action for the push body.
    `resolved` is the approval preview (gateway/preview.py) when a resolver
    produced one — names and subjects instead of opaque ids; templates fall
    back to id-based wording (and finally a compact key=value join) without
    it. Names/subjects only — never bodies or blobs."""
    a = args
    r = resolved or {}
    if tool in ("gmail_send", "gmail_draft_create"):
        return f"Email to {a.get('to', '?')} — “{a.get('subject', '(no subject)')}”"
    if tool == "gmail_send_draft":
        if r.get("to") or r.get("subject"):
            return f"Send draft to {r.get('to', '?')} — “{r.get('subject', '(no subject)')}”"
        return f"Send saved draft {a.get('draft_id', '?')}"
    if tool == "gmail_draft_reply":
        if r.get("replying_to"):
            return f"Reply draft to {r['replying_to']} — “{r.get('subject', '')}”"
        return f"Reply draft to message {a.get('message_id', '?')}"
    if tool == "gmail_trash_message":
        if r.get("from") or r.get("subject"):
            return (
                f"Trash email from {r.get('from', '?')} — "
                f"“{r.get('subject', '')}” (restorable ~30 days)"
            )
        return f"Trash email {a.get('message_id', '?')} (restorable ~30 days)"
    if tool == "gmail_download_attachment":
        if r.get("filename"):
            return f"Download “{r['filename']}” from {r.get('from', '?')}"
        return f"Download attachment {a.get('attachment_index', '?')} of message {a.get('message_id', '?')}"
    if tool == "drive_trash_file":
        if r.get("name"):
            return f"Trash Drive {r.get('type') or 'file'} “{r['name']}” (restorable ~30 days)"
        return f"Trash Drive file {a.get('file_id', '?')} (restorable ~30 days)"
    if tool == "drive_move":
        if r.get("file"):
            return f"Move “{r['file']}” → “{r.get('destination_folder', '?')}”"
        return f"Move Drive file {a.get('file_id', '?')} → folder {a.get('folder_id', '?')}"
    if tool == "drive_rename":
        target = f"“{r['name']}”" if r.get("name") else f"file {a.get('file_id', '?')}"
        return f"Rename Drive {target} → “{a.get('name', '?')}”"
    if tool == "drive_download":
        target = f"“{r['name']}”" if r.get("name") else a.get("file_id", "?")
        return f"Download {target} from Drive"
    if tool == "drive_update_content":
        target = f"“{r['name']}”" if r.get("name") else f"file {a.get('file_id', '?')}"
        return f"Overwrite contents of Drive {target}"
    if tool in ("docs_append_text", "docs_edit_text"):
        verb = "Append to" if tool == "docs_append_text" else "Edit"
        target = f"doc “{r['name']}”" if r.get("name") else f"doc {a.get('doc_id', '?')}"
        return f"{verb} {target}"
    if tool in ("sheets_append", "sheets_update"):
        target = (
            f"“{r['name']}”" if r.get("name") else a.get("spreadsheet_id", "?")
        )
        if tool == "sheets_update":
            return f"Overwrite {a.get('range', '?')} in {target}"
        return f"Append rows to {target}"
    if tool == "calendar_event_update":
        title = a.get("summary") or r.get("current_title") or "(untitled)"
        when = a.get("start", "")
        meet = {"add": " (add Meet link)", "remove": " (remove Meet link)"}.get(
            a.get("meet", ""), ""
        )
        return (
            f"Update event “{title}”"
            + (f" at {when}" if when else "")
            + meet
            + _cal_tag(a)
        )
    if tool == "calendar_event_create":
        when = a.get("start", "")
        meet = " + Meet link" if a.get("with_meet") else ""
        return (
            f"Event “{a.get('summary', '(untitled)')}”"
            + (f" at {when}" if when else "")
            + meet
            + _cal_tag(a)
        )
    if tool == "calendar_event_attendees":
        title = r.get("current_title") or f"event {a.get('event_id', '?')}"
        bits = []
        if a.get("add"):
            bits.append(f"invite {', '.join(a['add'])}")
        if a.get("remove"):
            bits.append(f"remove {', '.join(a['remove'])}")
        return f"“{title}”: {'; '.join(bits) or 'change guests'}" + _cal_tag(a)
    if tool == "calendar_event_respond":
        title = r.get("current_title") or f"event {a.get('event_id', '?')}"
        org = f" (organizer {r['organizer']})" if r.get("organizer") else ""
        return f"RSVP {a.get('response', '?')} to “{title}”{org}"
    if tool == "calendar_event_delete":
        target = (
            f"“{r['current_title']}”"
            if r.get("current_title")
            else f"event {a.get('event_id', '?')}"
        )
        when = f" at {r['current_start']}" if r.get("current_start") else ""
        quiet = "" if a.get("notify", True) else ", without notifying guests"
        return (
            f"Delete {target}{when}{quiet}"
            f"{_cal_tag(a)} (restorable from Calendar Bin ~30 days)"
        )
    if tool in ("task_create", "task_update"):
        title = a.get("title") or r.get("title") or "(untitled)"
        return f"Task “{title}”"
    if tool == "task_complete":
        title = f"“{r['title']}”" if r.get("title") else a.get("task_id", "?")
        return f"Complete task {title}"
    # Schedule self-edits are the agent changing its own future jobs — the
    # push body must surface the security-relevant fields (autonomy, tools)
    # so the user knows what they're approving from the notification alone.
    if tool == "schedule_add":
        s = f"New job “{a.get('label') or str(a.get('prompt', ''))[:40]}” cron {a.get('cron', '?')}"
        if a.get("autonomy") == "autonomous":
            s += f" — AUTONOMOUS, tools: {', '.join(a.get('tools') or [])}"
        return s
    if tool in ("schedule_edit", "schedule_delete", "schedule_disable"):
        job = (
            f"job “{r['label']}” ({r.get('cron', '?')})"
            if r.get("label")
            else f"job {a.get('trigger_id', '?')}"
        )
        if tool == "schedule_delete":
            return f"Delete {job} permanently"
        if tool == "schedule_disable":
            return f"Disable {job}"
        s = f"Edit {job}"
        changes = [k for k in ("cron", "prompt", "label", "one_shot") if k in a]
        if changes:
            s += f" ({', '.join(changes)})"
        if a.get("autonomy") == "autonomous":
            s += f" — make AUTONOMOUS, tools: {', '.join(a.get('tools') or [])}"
        elif "tools" in a:
            s += f" — tools: {', '.join(a.get('tools') or []) or '(cleared)'}"
        return s
    if tool in ("browser_click", "browser_select"):
        verb, key = ("Click", "will click") if tool == "browser_click" else ("Select", "will select")
        if r.get(key):
            return f"{verb} {r[key]}" + (f" on {r['on']}" if r.get("on") else "")
        return f"{verb} element {a.get('handle', '?')} in the browser"
    # Push-body blind spot for BATCH args: the in-app card renders every arg, but this fallback can
    # reach the lock screen without a batch's members — give batch tools an explicit template above.
    return ", ".join(f"{k}={v}" for k, v in list(a.items())[:4])
