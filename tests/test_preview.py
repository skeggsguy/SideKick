"""ApprovalPreviews resolvers (gateway/preview.py): per-tool happy paths,
degrade notes, and the 503 (handle missing) / 502 (read failed) semantics the
preview route maps to HTTP. All handles are stubs — no MCP, no network."""

from __future__ import annotations

import json

import pytest

from harness.gateway.preview import ApprovalPreviews, PreviewError
from harness.store.triggers import TriggerStore


class StubTool:
    """Stands in for an adapted READ_ONLY MCP tool handle."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def run(self, args):
        self.calls.append(args)
        return self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]


def _previews(handles=None, triggers=None):
    return ApprovalPreviews(tool_handles=handles or {}, triggers=triggers)


# ---- error semantics ---------------------------------------------------------

async def test_missing_handle_is_503():
    with pytest.raises(PreviewError) as e:
        await _previews().resolve("drive_trash_file", {"file_id": "f"})
    assert e.value.status == 503


async def test_mcp_plaintext_error_is_502():
    handles = {"drive_file_info": StubTool("Error from google_workspace: boom")}
    with pytest.raises(PreviewError) as e:
        await _previews(handles).resolve("drive_trash_file", {"file_id": "f"})
    assert e.value.status == 502


async def test_mcp_plaintext_error_with_a_json_prefix_is_still_502():
    """_load_concatenated must accept ONLY pure concatenated JSON: a plain-text
    failure whose message happens to start with a JSON object must not be
    half-decoded into a fake payload the resolvers would trust (the prefix here
    is a list the task resolver WOULD accept, so only the decoder can refuse)."""
    prefix = json.dumps([{"id": "t1", "list": "Inbox", "title": "Buy milk"}])
    handles = {
        "tasks_list": StubTool(prefix + "\nError from google_workspace: boom")
    }
    with pytest.raises(PreviewError) as e:
        await _previews(handles).resolve("task_complete", {"task_id": "t1"})
    assert e.value.status == 502


async def test_non_resolver_tool_is_404():
    with pytest.raises(PreviewError) as e:
        await _previews().resolve("gmail_send", {"to": "x"})
    assert e.value.status == 404


def test_previewable_matches_resolver_table():
    p = _previews()
    assert p.previewable("drive_trash_file")
    assert not p.previewable("gmail_send")


# ---- drive family ------------------------------------------------------------

def _drive_info(name, mime="text/plain", **extra):
    return json.dumps(
        {"id": "f1", "name": name, "mimeType": mime, "modifiedTime": "2026-07-01", **extra}
    )


async def test_drive_trash_resolves_name_and_type():
    handles = {"drive_file_info": StubTool(_drive_info("Q3 budget.xlsx"))}
    out = await _previews(handles).resolve("drive_trash_file", {"file_id": "f1"})
    assert out["name"] == "Q3 budget.xlsx"
    assert out["type"] == "text/plain"
    assert handles["drive_file_info"].calls == [{"file_id": "f1"}]


async def test_drive_trash_notes_already_trashed():
    handles = {"drive_file_info": StubTool(_drive_info("old.txt", trashed=True))}
    out = await _previews(handles).resolve("drive_trash_file", {"file_id": "f1"})
    assert out["note"] == "already in Trash"


async def test_drive_move_resolves_file_and_destination():
    handles = {
        "drive_file_info": StubTool(
            _drive_info("report.pdf", "application/pdf"),
            _drive_info("Archive", "application/vnd.google-apps.folder"),
        )
    }
    out = await _previews(handles).resolve(
        "drive_move", {"file_id": "f1", "folder_id": "d9"}
    )
    assert out == {
        "file": "report.pdf",
        "file_type": "application/pdf",
        "destination_folder": "Archive",
    }
    assert handles["drive_file_info"].calls == [
        {"file_id": "f1"},
        {"file_id": "d9"},
    ]


async def test_docs_and_sheets_ids_resolve_via_drive():
    # doc_id / spreadsheet_id ARE Drive file ids — one metadata tool covers all.
    handles = {"drive_file_info": StubTool(_drive_info("Meeting notes"))}
    out = await _previews(handles).resolve("docs_edit_text", {"doc_id": "d1"})
    assert out["name"] == "Meeting notes"
    assert handles["drive_file_info"].calls == [{"file_id": "d1"}]
    out = await _previews(handles).resolve("sheets_update", {"spreadsheet_id": "s1"})
    assert out["name"] == "Meeting notes"


# ---- gmail -------------------------------------------------------------------

def _message(**overrides):
    msg = {
        "id": "m1",
        "from": "Alice <alice@example.com>",
        "subject": "Hello",
        "date": "Sun, 13 Jul 2026 10:00:00 +0100",
        "body": "x" * 1000,
        "attachments": [
            {"index": 0, "filename": "a.pdf", "mime_type": "application/pdf", "size": 123}
        ],
    }
    msg.update(overrides)
    return json.dumps(msg)


async def test_draft_reply_resolves_recipient():
    """The consequential fact the args never show: who receives the reply."""
    handles = {"gmail_read": StubTool(_message())}
    out = await _previews(handles).resolve("gmail_draft_reply", {"message_id": "m1"})
    assert out["replying_to"] == "Alice <alice@example.com>"
    assert out["subject"] == "Hello"
    assert len(out["snippet"]) == 301  # 300 + ellipsis, never the full body


async def test_download_attachment_resolves_filename():
    handles = {"gmail_read": StubTool(_message())}
    out = await _previews(handles).resolve(
        "gmail_download_attachment", {"message_id": "m1", "attachment_index": 0}
    )
    assert out["filename"] == "a.pdf"
    assert out["size_bytes"] == 123
    assert out["from"] == "Alice <alice@example.com>"


async def test_download_attachment_bad_index_degrades():
    handles = {"gmail_read": StubTool(_message())}
    out = await _previews(handles).resolve(
        "gmail_download_attachment", {"message_id": "m1", "attachment_index": 5}
    )
    assert "no attachment at index 5" in out["note"]


# ---- calendar / tasks ----------------------------------------------------------

async def test_calendar_event_update_shows_current_state():
    handles = {
        "calendar_get_event": StubTool(json.dumps({
            "id": "e1", "summary": "Dentist", "start": "2026-07-20T09:00:00+01:00",
            "end": "2026-07-20T09:30:00+01:00", "location": "High St",
        }))
    }
    out = await _previews(handles).resolve(
        "calendar_event_update", {"event_id": "e1", "description": "bring form"}
    )
    assert out["current_title"] == "Dentist"
    assert out["current_start"] == "2026-07-20T09:00:00+01:00"


def _event_json(**extra) -> str:
    return json.dumps({
        "id": "e1", "summary": "Planning", "start": "2026-07-20T10:00:00+01:00",
        "end": "2026-07-20T11:00:00+01:00", "location": "",
        "organizer": "alice@example.com",
        "attendees": [
            {"email": "alice@example.com", "response": "accepted"},
            {"email": "me@example.com", "response": ""},
        ],
        **extra,
    })


@pytest.mark.parametrize(
    "tool, args",
    [
        ("calendar_event_attendees", {"event_id": "e1", "remove": ["me@example.com"]}),
        ("calendar_event_respond", {"event_id": "e1", "response": "declined"}),
        ("calendar_event_delete", {"event_id": "e1"}),
    ],
)
async def test_calendar_gated_tools_show_organizer_and_guests(tool, args):
    """The human judging an invite/remove/RSVP/delete sees whose event it is
    and who is on it NOW — live-fetched via calendar_get_event, never
    model-supplied args."""
    handles = {"calendar_get_event": StubTool(_event_json())}
    out = await _previews(handles).resolve(tool, args)
    assert out["current_title"] == "Planning"
    assert out["organizer"] == "alice@example.com"
    assert "alice@example.com (accepted)" in out["attendees"]
    assert "me@example.com (no reply)" in out["attendees"]
    # calendar_id rides along (default primary) so secondary-calendar events
    # resolve instead of 404ing.
    assert handles["calendar_get_event"].calls == [
        {"event_id": "e1", "calendar_id": "primary"}
    ]


async def test_task_resolves_title_by_id_scan():
    handles = {
        "tasks_list": StubTool(json.dumps([
            {"id": "t1", "list": "Inbox", "title": "Buy milk", "due": "", "status": "needsAction"},
            {"id": "t2", "list": "Inbox", "title": "File taxes", "due": "2026-07-31", "status": "needsAction"},
        ]))
    }
    out = await _previews(handles).resolve("task_complete", {"task_id": "t2"})
    assert out["title"] == "File taxes"
    assert out["list"] == "Inbox"
    # The scan must include completed tasks (an already-done task_update target).
    assert handles["tasks_list"].calls == [{"show_completed": True, "max_results": 100}]


async def test_task_not_found_degrades_with_note():
    handles = {"tasks_list": StubTool(json.dumps([]))}
    out = await _previews(handles).resolve("task_update", {"task_id": "nope"})
    assert "not found" in out["note"]


def _tasks_multidoc(*rows) -> str:
    """The shape the live wire actually carries: FastMCP serializes a
    list-returning tool as one TextContent per element and mcp/client.py
    joins the blocks with newlines, so the preview read arrives as
    `{...}\\n{...}` — concatenated objects, not one JSON array. This is the
    exact text that 502'd every live Tasks preview (2026-10-24)."""
    return "\n".join(json.dumps(r, indent=2) for r in rows)


async def test_task_complete_previews_the_real_multitext_wire_shape():
    """Regression for the live 502: `tasks_list` answers with concatenated
    pretty-printed objects, and _call_json must still hand the resolver the
    list it checks for — the stub used to serve a tidy array and hid this."""
    handles = {
        "tasks_list": StubTool(_tasks_multidoc(
            {"id": "t1", "list": "Inbox", "title": "Buy milk", "due": "", "status": "needsAction"},
            {"id": "t2", "list": "Inbox", "title": "File taxes", "due": "2026-07-31", "status": "needsAction"},
        ))
    }
    out = await _previews(handles).resolve("task_complete", {"task_id": "t2"})
    assert out == {"title": "File taxes", "list": "Inbox", "due": "2026-07-31", "status": "needsAction"}


# ---- schedule (trigger store, no MCP) -----------------------------------------

async def test_schedule_resolves_label_and_security_fields(db):
    triggers = TriggerStore(db)
    tid = await triggers.add(
        kind="cron", spec="0 8 * * *", playbook="check the news and summarize",
        channel="push", autonomy="autonomous", label="Morning brief",
        tools=["web_search", "notify"],
    )
    out = await _previews(triggers=triggers).resolve(
        "schedule_delete", {"trigger_id": tid}
    )
    assert out["label"] == "Morning brief"
    assert out["cron"] == "0 8 * * *"
    # Security-relevant fields must ride along: what the job may do unattended.
    assert out["autonomy"] == "autonomous"
    assert out["tools"] == "web_search, notify"
    assert out["enabled"] is True


async def test_schedule_missing_trigger_degrades(db):
    out = await _previews(triggers=TriggerStore(db)).resolve(
        "schedule_edit", {"trigger_id": "gone"}
    )
    assert out["note"] == "trigger no longer exists"


async def test_schedule_without_store_is_503():
    with pytest.raises(PreviewError) as e:
        await _previews().resolve("schedule_delete", {"trigger_id": "x"})
    assert e.value.status == 503


# ---- browser_type (identity-ref reveal) --------------------------------------

async def test_browser_type_reveals_stored_ref_value(db):
    """When the typed text carries {email}, the preview shows the REAL stored
    value (fetched live) so the human approves what will actually be filled —
    the value is never in the args, only the placeholder is."""
    from harness.store.client_config import REF_EMAIL_KEY, ClientConfigStore

    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, "test@example.invalid")
    previews = ApprovalPreviews(client_config=cc)
    out = await previews.resolve("browser_type", {"handle": "e1", "text": "{email}"})
    assert out == {"will fill": "email = test@example.invalid"}


async def test_browser_type_missing_value_shows_not_set(db):
    from harness.store.client_config import ClientConfigStore

    previews = ApprovalPreviews(client_config=ClientConfigStore(db))
    out = await previews.resolve("browser_type", {"handle": "e1", "text": "{phone}"})
    assert out == {"will fill": "phone = (not set)"}


async def test_browser_type_without_placeholder_echoes_text(db):
    from harness.store.client_config import ClientConfigStore

    previews = ApprovalPreviews(client_config=ClientConfigStore(db))
    out = await previews.resolve("browser_type", {"handle": "e1", "text": "hello there"})
    assert out == {"will type": "hello there"}


async def test_browser_type_without_client_config_is_503():
    """No store wired + placeholders present → 503; the UI degrades to raw args,
    which still show only the placeholder (never a value)."""
    with pytest.raises(PreviewError) as e:
        await _previews().resolve("browser_type", {"handle": "e1", "text": "{email}"})
    assert e.value.status == 503
