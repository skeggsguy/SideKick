"""The google_workspace MCP server: read-first Google Workspace tools.

Tool functions live in per-service modules as plain functions (FastMCP
derives schemas from their type hints and docstrings) and are registered
here. They are sync — FastMCP runs them in a worker thread.

Run `google-workspace-mcp --auth` once interactively to authenticate;
the server itself runs over stdio, spawned by the harness MCP manager.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from . import docs, drive, gcal, gmail, gtasks, sheets
from .auth import run_auth_flow, run_auth_flow_manual

mcp = FastMCP("google_workspace")

for fn in (
    # read
    gmail.gmail_search,
    gmail.gmail_read,
    gmail.gmail_get_draft,
    gmail.gmail_download_attachment,
    gcal.calendar_list_events,
    gcal.calendar_get_event,
    gcal.calendar_list_calendars,
    drive.drive_list,
    drive.drive_read,
    drive.drive_file_info,
    sheets.sheets_read,
    gtasks.tasklists_list,
    gtasks.tasks_list,
    # write — local (drafts, own calendar, tasks, docs, sheets, drive upload)
    gmail.gmail_draft_create,
    gmail.gmail_draft_reply,
    gcal.calendar_event_create,
    gcal.calendar_event_update,
    gtasks.task_create,
    gtasks.task_update,
    gtasks.task_complete,
    gtasks.tasklist_create,
    docs.docs_create,
    docs.docs_append_text,
    docs.docs_edit_text,
    sheets.sheets_create,
    sheets.sheets_append,
    sheets.sheets_update,
    drive.drive_upload,
    drive.drive_create_folder,
    drive.drive_move,
    drive.drive_rename,
    drive.drive_download,
    drive.drive_update_content,
    # write — outward (sending mail, inviting/RSVPing other people)
    gmail.gmail_send,
    gmail.gmail_send_draft,
    gcal.calendar_event_attendees,
    gcal.calendar_event_respond,
    # destructive: always prompts, denied unattended, never allowlisted.
    # gmail/drive are trash-only (never permanent delete); Calendar has no
    # trash API — events().delete moves the event to the Bin (~30 days,
    # web-UI restore only). Closes the laundering gap where "delete that"
    # had no gated path (2026-06-19 audit episode).
    gmail.gmail_trash_message,
    drive.drive_trash_file,
    gcal.calendar_event_delete,
):
    mcp.tool()(fn)


def main() -> None:
    if "--auth" in sys.argv:
        if "--manual" in sys.argv:
            run_auth_flow_manual()  # copy/paste; no local server or tunnel
        else:
            run_auth_flow()
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
