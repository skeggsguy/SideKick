"""Google Workspace write tools + the write-related safety glue.

All offline: the Google service is mocked, never a real account.
"""

from __future__ import annotations

import base64
import json
from email import message_from_bytes

import pytest

from mcp_servers.google_workspace import auth, docs, drive, gmail, sheets, gcal
from conftest import gotcha


@pytest.fixture(autouse=True)
def _file_roots(tmp_path, monkeypatch):
    """Local-file args in these tests live under tmp_path — make it the jail."""
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path))


class _FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeDocuments:
    """`reply` is one canned replaceAllText reply (repeated) or a list consumed
    per batchUpdate call; every request body is recorded in `bodies`."""

    def __init__(self, reply, doc=None):
        self._replies = list(reply) if isinstance(reply, list) else [reply]
        self._doc = doc or {}
        self.bodies = []

    @property
    def last_body(self):
        return self.bodies[-1] if self.bodies else None

    def batchUpdate(self, documentId, body):  # noqa: N803 (Google API casing)
        self.bodies.append(body)
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        return _FakeRequest({"replies": [reply]})

    def get(self, documentId):  # noqa: N803 (Google API casing)
        return _FakeRequest(self._doc)


class _FakeService:
    def __init__(self, reply, doc=None):
        self._docs = _FakeDocuments(reply, doc)

    def documents(self):
        return self._docs


def _doc_body(text: str) -> dict:
    """A minimal documents().get body whose internal plain text is `text`."""
    return {
        "body": {
            "content": [{"paragraph": {"elements": [{"textRun": {"content": text}}]}}]
        }
    }


def test_docs_edit_text_raises_when_absent(monkeypatch):
    svc = _FakeService(
        {"replaceAllText": {"occurrencesChanged": 0}}, doc=_doc_body("nothing here\n")
    )
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    with pytest.raises(RuntimeError, match="not found.*drive_read"):
        docs.docs_edit_text("doc1", "ghost", "x")


def test_docs_edit_text_reports_count(monkeypatch):
    svc = _FakeService({"replaceAllText": {"occurrencesChanged": 3}})
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    out = docs.docs_edit_text("doc1", "old", "new")
    assert out == {"id": "doc1", "occurrences_changed": 3}


def test_docs_edit_text_normalizes_export_artifacts(monkeypatch):
    """A snippet copied from a drive_read export (BOM + \\r\\n) must match the
    Doc's internal \\n text without the model hand-fixing line endings."""
    svc = _FakeService({"replaceAllText": {"occurrencesChanged": 1}})
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    docs.docs_edit_text("doc1", "\ufeffline one\r\nline two", "a\r\nb")
    req = svc.documents().last_body["requests"][0]["replaceAllText"]
    assert req["containsText"]["text"] == "line one\nline two", gotcha(
        "docs-export-crlf-bom",
        "old_str must be normalized (BOM stripped, \\r\\n -> \\n) before replaceAllText",
    )
    assert req["replaceText"] == "a\nb", gotcha(
        "docs-export-crlf-bom",
        "new_str must be normalized too or literal \\r lands in the document",
    )


def test_docs_edit_text_recovers_document_spacing(monkeypatch):
    """When old_str differs from the document only in whitespace (soft breaks,
    blank-paragraph runs, NBSP — divergences no exported view can show), the
    tool recovers the document's actual spacing and retries instead of failing."""
    doc = _doc_body("intro\n\nalpha  beta\n\n\ngamma ends\n")
    svc = _FakeService(
        [
            {"replaceAllText": {"occurrencesChanged": 0}},
            {"replaceAllText": {"occurrencesChanged": 1}},
        ],
        doc=doc,
    )
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    out = docs.docs_edit_text("doc1", "alpha beta\ngamma", "replacement")
    assert out["occurrences_changed"] == 1
    assert "whitespace recovery" in out["note"]
    retry = svc.documents().bodies[1]["requests"][0]["replaceAllText"]
    assert retry["containsText"]["text"] == "alpha  beta\n\n\ngamma"
    assert retry["replaceText"] == "replacement"


def test_docs_edit_text_ambiguous_spacing_raises(monkeypatch):
    """Two occurrences with different internal spacing: recovery must refuse to
    guess and ask for more context instead."""
    doc = _doc_body("x one two\nthree y one  two three z\n")
    svc = _FakeService({"replaceAllText": {"occurrencesChanged": 0}}, doc=doc)
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    with pytest.raises(RuntimeError, match="differing whitespace.*more"):
        docs.docs_edit_text("doc1", "one two three", "x")
    assert len(svc.documents().bodies) == 1  # no blind retry when ambiguous


def test_docs_edit_text_diagnoses_case_mismatch(monkeypatch):
    svc = _FakeService(
        {"replaceAllText": {"occurrencesChanged": 0}}, doc=_doc_body("Hello World\n")
    )
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    with pytest.raises(RuntimeError, match="case-sensitive"):
        docs.docs_edit_text("doc1", "hello world", "x")


def test_docs_edit_text_diagnoses_nbsp_mismatch(monkeypatch):
    svc = _FakeService(
        {"replaceAllText": {"occurrencesChanged": 0}},
        doc=_doc_body("price\u00a0100\n"),
    )
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    with pytest.raises(RuntimeError, match="non-breaking spaces"):
        docs.docs_edit_text("doc1", "price 100", "x")


def test_docs_edit_text_diagnoses_whitespace_mismatch(monkeypatch):
    svc = _FakeService(
        {"replaceAllText": {"occurrencesChanged": 0}},
        doc=_doc_body("alpha beta\ngamma\n"),
    )
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    with pytest.raises(RuntimeError, match="whitespace is collapsed"):
        docs.docs_edit_text("doc1", "beta\n\ngamma", "x")


def test_docs_edit_text_diagnosis_reads_table_text(monkeypatch):
    """_doc_text must recurse into tables or in-table snippets misreport as absent."""
    doc = {
        "body": {
            "content": [
                {
                    "table": {
                        "tableRows": [
                            {
                                "tableCells": [
                                    {
                                        "content": [
                                            {
                                                "paragraph": {
                                                    "elements": [
                                                        {"textRun": {"content": "Cell Text\n"}}
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            ]
        }
    }
    svc = _FakeService({"replaceAllText": {"occurrencesChanged": 0}}, doc=doc)
    monkeypatch.setattr(docs, "get_service", lambda *a, **k: svc)
    with pytest.raises(RuntimeError, match="case-sensitive"):
        docs.docs_edit_text("doc1", "cell text", "x")


class _FakeValues:
    """Fakes spreadsheets().values() — records the last call's kwargs."""

    def __init__(self, reply):
        self._reply = reply
        self.last = None

    def get(self, **kwargs):
        self.last = ("get", kwargs)
        return _FakeRequest(self._reply)

    def append(self, **kwargs):
        self.last = ("append", kwargs)
        return _FakeRequest(self._reply)

    def update(self, **kwargs):
        self.last = ("update", kwargs)
        return _FakeRequest(self._reply)


class _FakeSpreadsheets:
    def __init__(self, reply):
        self._values = _FakeValues(reply)
        self._reply = reply
        self.created_body = None

    def values(self):
        return self._values

    def create(self, body, fields):
        self.created_body = body
        return _FakeRequest(self._reply)


class _FakeSheetsService:
    def __init__(self, reply):
        self._spreadsheets = _FakeSpreadsheets(reply)

    def spreadsheets(self):
        return self._spreadsheets


def test_sheets_create_returns_id_and_url(monkeypatch):
    svc = _FakeSheetsService({"spreadsheetId": "sid1"})
    monkeypatch.setattr(sheets, "get_service", lambda *a, **k: svc)
    out = sheets.sheets_create("Budget")
    assert out["id"] == "sid1"
    assert out["url"].endswith("/spreadsheets/d/sid1/edit")


def test_sheets_read_parses_values_and_defaults_range(monkeypatch):
    svc = _FakeSheetsService({"values": [["a", "b"], ["1", "2"]]})
    monkeypatch.setattr(sheets, "get_service", lambda *a, **k: svc)
    out = sheets.sheets_read("sid1")
    assert out["values"] == [["a", "b"], ["1", "2"]]
    assert out["range"] == "Sheet1"  # empty range defaults to the first tab
    assert svc.spreadsheets().values().last == (
        "get",
        {"spreadsheetId": "sid1", "range": "Sheet1"},
    )


def test_sheets_read_empty_when_no_values(monkeypatch):
    svc = _FakeSheetsService({})  # API omits "values" for an empty range
    monkeypatch.setattr(sheets, "get_service", lambda *a, **k: svc)
    assert sheets.sheets_read("sid1", "Sheet1!A1:B2")["values"] == []


def test_sheets_append_builds_request_and_parses(monkeypatch):
    svc = _FakeSheetsService(
        {"updates": {"updatedRange": "Sheet1!A5:C5", "updatedRows": 1}}
    )
    monkeypatch.setattr(sheets, "get_service", lambda *a, **k: svc)
    out = sheets.sheets_append("sid1", [[1, 2, 3]])
    assert out == {
        "id": "sid1",
        "updated_range": "Sheet1!A5:C5",
        "rows_appended": 1,
    }
    kind, kwargs = svc.spreadsheets().values().last
    assert kind == "append"
    assert kwargs["valueInputOption"] == "USER_ENTERED"
    assert kwargs["body"] == {"values": [[1, 2, 3]]}


def test_sheets_update_builds_request_and_parses(monkeypatch):
    svc = _FakeSheetsService({"updatedRange": "Sheet1!A1:B1", "updatedCells": 2})
    monkeypatch.setattr(sheets, "get_service", lambda *a, **k: svc)
    out = sheets.sheets_update("sid1", "Sheet1!A1:B1", [["x", "y"]])
    assert out == {
        "id": "sid1",
        "updated_range": "Sheet1!A1:B1",
        "cells_updated": 2,
    }
    kind, kwargs = svc.spreadsheets().values().last
    assert kind == "update"
    assert kwargs["valueInputOption"] == "USER_ENTERED"


class _FakeFiles:
    """Fakes drive files(): create/get/update record the last call's kwargs.

    `replies` maps method name -> canned response (get may differ from update).
    """

    def __init__(self, replies):
        self._replies = replies
        self.create_kwargs = None
        self.update_kwargs = None
        self.get_kwargs = None

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return _FakeRequest(self._replies.get("create"))

    def get(self, **kwargs):
        self.get_kwargs = kwargs
        return _FakeRequest(self._replies.get("get"))

    def update(self, **kwargs):
        self.update_kwargs = kwargs
        return _FakeRequest(self._replies.get("update"))

    def get_media(self, **kwargs):
        return ("media-request", kwargs)

    def export(self, **kwargs):
        self.export_kwargs = kwargs
        return _FakeRequest(self._replies.get("export"))


class _FakeDriveService:
    def __init__(self, replies):
        # Accept a bare dict for the common create-only case.
        if "create" not in replies and "get" not in replies and "update" not in replies:
            replies = {"create": replies}
        self._files = _FakeFiles(replies)

    def files(self):
        return self._files


def test_drive_read_normalizes_google_doc_export(monkeypatch):
    """The text/plain export of a Google Doc carries a BOM + \\r\\n line endings;
    drive_read must fold them to the Doc's internal \\n form so a copied snippet
    round-trips into docs_edit_text."""
    svc = _FakeDriveService(
        {
            "get": {
                "id": "f1",
                "name": "Notes",
                "mimeType": "application/vnd.google-apps.document",
            },
            "export": "\ufeffline one\r\nline two\r\n".encode("utf-8"),
        }
    )
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    out = drive.drive_read("f1")
    assert out["content"] == "line one\nline two\n", gotcha(
        "docs-export-crlf-bom",
        "drive_read must strip the export BOM and normalize \\r\\n -> \\n for "
        "Google-native exports",
    )


def test_drive_file_info_fetches_metadata_only(monkeypatch):
    """drive_file_info asks for metadata fields only — no export/get_media —
    so it works on binaries and stays cheap for approval previews."""
    svc = _FakeDriveService(
        {
            "get": {
                "id": "f1",
                "name": "Q3 budget.xlsx",
                "mimeType": "application/vnd.ms-excel",
                "modifiedTime": "2026-07-01T10:00:00Z",
                "trashed": False,
            }
        }
    )
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    out = drive.drive_file_info("f1")
    assert out["name"] == "Q3 budget.xlsx"
    kwargs = svc.files().get_kwargs
    assert kwargs["fileId"] == "f1"
    assert "name" in kwargs["fields"] and "mimeType" in kwargs["fields"]


def test_drive_read_raw_text_download_stays_faithful(monkeypatch):
    """Only Google-native exports are normalized — raw text downloads keep
    their bytes as-is."""
    svc = _FakeDriveService(
        {"get": {"id": "f2", "name": "log.txt", "mimeType": "text/plain"}}
    )
    svc.files().get_media = lambda **kwargs: _FakeRequest(b"a\r\nb")
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    out = drive.drive_read("f2")
    assert out["content"] == "a\r\nb"


def test_drive_upload_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="No file to upload"):
        drive.drive_upload(str(tmp_path / "nope.txt"))


def test_drive_upload_creates_with_guessed_mime(tmp_path, monkeypatch):
    f = tmp_path / "report.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    svc = _FakeDriveService({"id": "fid1", "name": "report.csv", "webViewLink": "u"})
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    # MediaFileUpload would open a real file handle / sniff — stub it out.
    monkeypatch.setattr(drive, "MediaFileUpload", lambda *a, **k: ("media", a, k))

    out = drive.drive_upload(str(f))
    assert out == {"id": "fid1", "name": "report.csv", "url": "u"}
    body = svc.files().create_kwargs["body"]
    assert body == {"name": "report.csv"}  # no parents when folder_id omitted
    # csv mimetype is guessed and passed to MediaFileUpload
    assert svc.files().create_kwargs["media_body"][2]["mimetype"] == "text/csv"


def test_drive_upload_honors_name_and_folder(tmp_path, monkeypatch):
    f = tmp_path / "x.bin"
    f.write_bytes(b"\x00\x01")
    svc = _FakeDriveService({"id": "fid2", "name": "renamed", "webViewLink": "u2"})
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    monkeypatch.setattr(drive, "MediaFileUpload", lambda *a, **k: None)

    drive.drive_upload(str(f), name="renamed", folder_id="folder9")
    body = svc.files().create_kwargs["body"]
    assert body == {"name": "renamed", "parents": ["folder9"]}


def test_drive_create_folder_sets_folder_mime_and_parent(monkeypatch):
    svc = _FakeDriveService({"id": "dir1", "name": "Reports", "webViewLink": "u"})
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    out = drive.drive_create_folder("Reports", parent_id="root9")
    assert out == {"id": "dir1", "name": "Reports", "url": "u"}
    body = svc.files().create_kwargs["body"]
    assert body == {
        "name": "Reports",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["root9"],
    }


def test_drive_move_swaps_parents(monkeypatch):
    svc = _FakeDriveService(
        {
            "get": {"parents": ["old1", "old2"]},
            "update": {"id": "f1", "name": "n", "parents": ["new9"]},
        }
    )
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    out = drive.drive_move("f1", "new9")
    assert out["parents"] == ["new9"]
    assert svc.files().update_kwargs["addParents"] == "new9"
    assert svc.files().update_kwargs["removeParents"] == "old1,old2"


def test_drive_rename_updates_name(monkeypatch):
    svc = _FakeDriveService({"update": {"id": "f1", "name": "newname"}})
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    out = drive.drive_rename("f1", "newname")
    assert out == {"id": "f1", "name": "newname"}
    assert svc.files().update_kwargs["body"] == {"name": "newname"}


def test_drive_trash_sets_trashed_flag_never_deletes(monkeypatch):
    """Trash, not delete: the update body flips `trashed` (restorable ~30
    days); no files().delete call exists anywhere in the module."""
    svc = _FakeDriveService({"update": {"id": "f1", "name": "old", "trashed": True}})
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    out = drive.drive_trash_file("f1")
    assert out == {"id": "f1", "name": "old", "trashed": True}
    assert svc.files().update_kwargs["body"] == {"trashed": True}
    import inspect

    assert ".delete(" not in inspect.getsource(drive)


def test_drive_download_rejects_native_type(monkeypatch):
    svc = _FakeDriveService(
        {"get": {"id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document"}}
    )
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    with pytest.raises(RuntimeError, match="Google-native"):
        drive.drive_download("f1")


def test_drive_download_writes_binary(tmp_path, monkeypatch):
    svc = _FakeDriveService(
        {"get": {"id": "f1", "name": "report.pdf", "mimeType": "application/pdf"}}
    )
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)

    class _FakeDownloader:
        def __init__(self, fh, request):
            self._fh = fh

        def next_chunk(self):
            self._fh.write(b"%PDF-data")
            return (None, True)

    monkeypatch.setattr(drive, "MediaIoBaseDownload", _FakeDownloader)
    out = drive.drive_download("f1", str(tmp_path))
    dest = tmp_path / "report.pdf"
    assert dest.read_bytes() == b"%PDF-data"
    assert out == {"id": "f1", "name": "report.pdf", "path": str(dest), "bytes": 9}


def test_drive_download_defaults_to_downloads_dir(tmp_path, monkeypatch):
    svc = _FakeDriveService(
        {"get": {"id": "f1", "name": "report.pdf", "mimeType": "application/pdf"}}
    )
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)

    class _FakeDownloader:
        def __init__(self, fh, request):
            self._fh = fh

        def next_chunk(self):
            self._fh.write(b"%PDF-data")
            return (None, True)

    monkeypatch.setattr(drive, "MediaIoBaseDownload", _FakeDownloader)
    out = drive.drive_download("f1")
    dest = tmp_path / "downloads" / "report.pdf"
    assert dest.read_bytes() == b"%PDF-data"
    assert out["path"] == str(dest)


def test_drive_download_outside_file_area_raises(tmp_path_factory, monkeypatch):
    svc = _FakeDriveService(
        {"get": {"id": "f1", "name": "report.pdf", "mimeType": "application/pdf"}}
    )
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    outside = tmp_path_factory.mktemp("outside-dl")
    with pytest.raises(RuntimeError, match="outside the agent's file area"):
        drive.drive_download("f1", str(outside / "report.pdf"))


def test_drive_upload_outside_file_area_raises(tmp_path_factory, monkeypatch):
    outside = tmp_path_factory.mktemp("outside-up") / "leak.txt"
    outside.write_text("x")
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="outside the agent's file area"):
        drive.drive_upload(str(outside))


def test_drive_update_content_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="No file to upload"):
        drive.drive_update_content("f1", str(tmp_path / "nope.txt"))


def test_drive_update_content_sends_new_revision(tmp_path, monkeypatch):
    f = tmp_path / "v2.csv"
    f.write_text("a,b\n", encoding="utf-8")
    svc = _FakeDriveService({"update": {"id": "f1", "name": "v2.csv"}})
    monkeypatch.setattr(drive, "get_service", lambda *a, **k: svc)
    monkeypatch.setattr(drive, "MediaFileUpload", lambda *a, **k: "media")
    out = drive.drive_update_content("f1", str(f))
    assert out == {"id": "f1", "name": "v2.csv"}
    assert svc.files().update_kwargs["media_body"] == "media"
    assert svc.files().update_kwargs["fileId"] == "f1"


def _write_token_with_scopes(path, scopes) -> None:
    path.write_text(json.dumps({"scopes": list(scopes)}), encoding="utf-8")


def test_load_credentials_rejects_stale_scopes(tmp_path, monkeypatch):
    """A token issued for only the old read-only scopes must fail loudly with
    re-auth instructions, not 403 cryptically on the first write call. The check
    reads the scopes ACTUALLY granted in the token file."""
    token = tmp_path / "tok.json"
    _write_token_with_scopes(token, ["https://www.googleapis.com/auth/gmail.readonly"])
    monkeypatch.setattr(auth, "token_path", lambda: token)

    with pytest.raises(RuntimeError, match="scopes"):
        auth.load_credentials()


def test_load_credentials_rejects_missing_scope_before_refresh(tmp_path, monkeypatch):
    """Regression: a token granted all-but-one scope must raise BEFORE
    constructing creds / hitting the network — otherwise the refresh fails with
    a cryptic 'invalid_scope'. from_authorized_user_file overwrites creds.scopes
    with the passed list, so the guard must read the file directly. (This also
    covers the one-time re-auth when scopes broaden: an old narrow token is
    missing the new broad scopes and must hit this path, not refresh().)"""
    token = tmp_path / "tok.json"
    granted = list(auth.SCOPES)[1:]  # drop one required scope
    assert len(granted) == len(auth.SCOPES) - 1
    _write_token_with_scopes(token, granted)
    monkeypatch.setattr(auth, "token_path", lambda: token)

    def _boom(*a, **k):
        raise AssertionError("must not construct creds for a stale-scope token")

    monkeypatch.setattr(
        auth.Credentials, "from_authorized_user_file", staticmethod(_boom)
    )
    with pytest.raises(RuntimeError, match="scopes"):
        auth.load_credentials()


def test_load_credentials_accepts_full_scopes(tmp_path, monkeypatch):
    token = tmp_path / "tok.json"
    _write_token_with_scopes(token, auth.SCOPES)
    monkeypatch.setattr(auth, "token_path", lambda: token)

    class FakeCreds:
        scopes = list(auth.SCOPES)
        valid = True

    monkeypatch.setattr(
        auth.Credentials,
        "from_authorized_user_file",
        staticmethod(lambda *a, **k: FakeCreds()),
    )
    assert auth.load_credentials().valid is True


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


# A message with a plain-text body and one PDF attachment, nested under a
# multipart/mixed root, to exercise the depth-first MIME walk.
_MSG_WITH_ATTACHMENT = {
    "id": "msg1",
    "threadId": "thr1",
    "snippet": "hello",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "Subject", "value": "Invoice"}],
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64url(b"see attached")},
            },
            {
                "mimeType": "application/pdf",
                "filename": "invoice.pdf",
                "body": {"attachmentId": "att1", "size": 5},
            },
        ],
    },
}


class _FakeAttachments:
    def __init__(self, data: bytes):
        self._data = data

    def get(self, userId, messageId, id):  # noqa: N803 (Google API casing)
        return _FakeRequest({"data": _b64url(self._data), "size": len(self._data)})


class _FakeMessages:
    def __init__(self, message, attachment_bytes=b""):
        self._message = message
        self._attachments = _FakeAttachments(attachment_bytes)
        self.created_body = None
        self.trashed_id = None

    def get(self, **kwargs):
        return _FakeRequest(self._message)

    def trash(self, userId, id):  # noqa: N803 (Google API casing)
        self.trashed_id = id
        return _FakeRequest({"id": id, "labelIds": ["TRASH"]})

    def attachments(self):
        return self._attachments

    def create(self, userId, body):  # noqa: N803
        self.created_body = body
        return _FakeRequest({"id": "draft1"})


class _FakeDrafts:
    def __init__(self, messages):
        self._messages = messages

    def create(self, userId, body):  # noqa: N803
        # gmail_draft_create routes through drafts().create with the raw message.
        self._messages.create(userId=userId, body=body["message"])
        return _FakeRequest({"id": "draft1"})

    def get(self, userId, id, format="full"):  # noqa: N803
        return _FakeRequest({"id": id, "message": self._messages._message})


class _FakeUsers:
    def __init__(self, messages):
        self._messages = messages
        self._drafts = _FakeDrafts(messages)

    def messages(self):
        return self._messages

    def drafts(self):
        return self._drafts

    def getProfile(self, userId):  # noqa: N803 (Google API casing)
        return _FakeRequest({"emailAddress": "me@example.com"})


class _FakeGmailService:
    def __init__(self, message, attachment_bytes=b""):
        self._messages = _FakeMessages(message, attachment_bytes)
        self._users = _FakeUsers(self._messages)

    def users(self):
        return self._users


def test_gmail_read_surfaces_attachments(monkeypatch):
    svc = _FakeGmailService(_MSG_WITH_ATTACHMENT)
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: svc)
    out = gmail.gmail_read("msg1")
    assert out["body"] == "see attached"
    # Public view exposes a short, stable index — never the opaque attachmentId.
    assert out["attachments"] == [
        {
            "index": 0,
            "filename": "invoice.pdf",
            "mime_type": "application/pdf",
            "size": 5,
        }
    ]


def test_gmail_trash_moves_to_trash_never_deletes(monkeypatch):
    """Trash, not delete: messages().trash() (restorable ~30 days); no
    messages().delete call exists anywhere in the module."""
    svc = _FakeGmailService(_MSG_WITH_ATTACHMENT)
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: svc)
    out = gmail.gmail_trash_message("msg1")
    assert out == {"message_id": "msg1", "labels": ["TRASH"]}
    assert svc.users().messages().trashed_id == "msg1"
    import inspect

    assert ".delete(" not in inspect.getsource(gmail)


def test_gmail_get_draft_returns_full_content(monkeypatch):
    """The send_draft approval preview shows real recipients/subject/body —
    never just the opaque draft id (v3 audit Q1.2a)."""
    svc = _FakeGmailService(_MSG_WITH_ATTACHMENT)
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: svc)
    out = gmail.gmail_get_draft("draft1")
    assert out["draft_id"] == "draft1"
    assert out["subject"] == "Invoice"
    assert out["body"] == "see attached"
    assert out["attachments"] == ["invoice.pdf"]


def test_gmail_download_attachment_writes_bytes(tmp_path, monkeypatch):
    svc = _FakeGmailService(_MSG_WITH_ATTACHMENT, attachment_bytes=b"%PDF-1.4 data")
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: svc)
    out = gmail.gmail_download_attachment("msg1", 0, str(tmp_path))
    saved = tmp_path / "invoice.pdf"
    assert out["name"] == "invoice.pdf"
    assert out["path"] == str(saved)
    assert out["bytes"] == len(b"%PDF-1.4 data")
    assert saved.read_bytes() == b"%PDF-1.4 data"


def test_gmail_download_attachment_bad_index_raises(tmp_path, monkeypatch):
    svc = _FakeGmailService(_MSG_WITH_ATTACHMENT, attachment_bytes=b"x")
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: svc)
    with pytest.raises(RuntimeError, match="no index 5"):
        gmail.gmail_download_attachment("msg1", 5, str(tmp_path))


def test_gmail_draft_create_attaches_file(tmp_path, monkeypatch):
    f = tmp_path / "report.txt"
    f.write_text("the numbers")
    monkeypatch.setattr(gmail, "_my_address", None)
    svc = _FakeGmailService(_MSG_WITH_ATTACHMENT)
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: svc)
    gmail.gmail_draft_create("a@b.c", "Hi", "body", attachments=[str(f)])

    raw = svc._messages.created_body["raw"]
    parsed = message_from_bytes(base64.urlsafe_b64decode(raw))
    attached = [p for p in parsed.walk() if p.get_filename()]
    assert [p.get_filename() for p in attached] == ["report.txt"]
    assert attached[0].get_payload(decode=True) == b"the numbers"


def test_gmail_draft_create_sets_agent_from_and_signature(monkeypatch):
    monkeypatch.delenv("HARNESS_AGENT_SIGNATURE", raising=False)
    monkeypatch.setattr(gmail, "_my_address", None)
    svc = _FakeGmailService(_MSG_WITH_ATTACHMENT)
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: svc)
    gmail.gmail_draft_create("a@b.c", "Hi", "Here is the update.")

    parsed = message_from_bytes(
        base64.urlsafe_b64decode(svc._messages.created_body["raw"])
    )
    assert parsed["From"] == "Sidekick <me@example.com>"
    body = parsed.get_payload(decode=True).decode()
    assert body.startswith("Here is the update.")
    assert body.rstrip().endswith(
        "Sent by Sidekick, an AI assistant, on behalf of the account owner."
    )


def test_gmail_signature_env_override(monkeypatch):
    """HARNESS_AGENT_SIGNATURE replaces the sentence, not the separator.

    This text goes out on real mail, so the separator and the blank lines that
    keep it off the end of the body stay ours regardless of the override.
    """
    monkeypatch.setenv("HARNESS_AGENT_SIGNATURE", "  Sent by Robot, for Ada.  ")
    monkeypatch.setattr(gmail, "_my_address", None)
    svc = _FakeGmailService(_MSG_WITH_ATTACHMENT)
    monkeypatch.setattr(gmail, "get_service", lambda *a, **k: svc)
    gmail.gmail_draft_create("a@b.c", "Hi", "Here is the update.")

    body = message_from_bytes(
        base64.urlsafe_b64decode(svc._messages.created_body["raw"])
    ).get_payload(decode=True).decode()
    assert body.startswith("Here is the update.\n\n—\n")
    assert body.rstrip().endswith("Sent by Robot, for Ada.")
    assert "an AI assistant" not in body


def test_gmail_signature_blank_env_falls_back_to_default(monkeypatch):
    """An empty override is a misconfiguration, not a request for no signature."""
    monkeypatch.setenv("HARNESS_AGENT_SIGNATURE", "   ")
    assert gmail.signature() == f"\n\n—\n{gmail.DEFAULT_SIGNATURE}"


def test_gmail_build_message_missing_attachment_raises(tmp_path):
    with pytest.raises(RuntimeError, match="No attachment file"):
        gmail._build_message(
            "a@b.c", "Hi", "body", attachments=[str(tmp_path / "missing.bin")]
        )


def test_gmail_attachment_outside_file_area_raises(tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-att") / "leak.bin"
    outside.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="outside the agent's file area"):
        gmail._build_message("a@b.c", "Hi", "body", attachments=[str(outside)])


def test_approval_summary_renders_english():
    from harness.gateway.agentd import approval_summary

    s = approval_summary("gmail_send", {"to": "a@b.c", "subject": "Hi"})
    assert "a@b.c" in s and "Hi" in s
    s = approval_summary("gmail_trash_message", {"message_id": "abc123"})
    assert "abc123" in s and "rash" in s and "message_id=" not in s
    s = approval_summary(
        "calendar_event_attendees", {"event_id": "e1", "add": ["bob@x.com"]}
    )
    assert "invite bob@x.com" in s and "event_id=" not in s
    s = approval_summary("calendar_event_respond", {"event_id": "e1", "response": "declined"})
    assert "RSVP declined" in s and "event_id=" not in s
    s = approval_summary("calendar_event_delete", {"event_id": "e1", "notify": False})
    assert "Delete" in s and "Bin" in s and "restorable" in s
    assert "without notifying" in s and "event_id=" not in s
    s = approval_summary(
        "calendar_event_create", {"summary": "Call", "start": "X", "with_meet": True}
    )
    assert "Meet link" in s
    s = approval_summary(
        "calendar_event_update", {"event_id": "e1", "meet": "add", "calendar_id": "work@x"}
    )
    assert "add Meet link" in s and "[work@x]" in s
    # Primary calendar stays untagged — the [cal] suffix is non-primary only.
    s = approval_summary("calendar_event_create", {"summary": "Lunch", "start": "X"})
    assert "Meet" not in s and "[" not in s
    # Unknown tools fall back to a compact key=value join.
    assert "x=1" in approval_summary("mystery_tool", {"x": 1})


class _FakeEvents:
    def __init__(self, event):
        self._event = event
        self.get_kwargs = None
        self.patch_kwargs = None
        self.delete_kwargs = None
        self.insert_kwargs = None
        self.list_kwargs = None

    def get(self, **kwargs):
        self.get_kwargs = kwargs
        return _FakeRequest(self._event)

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        created = dict(kwargs["body"])
        if "conferenceData" in created:
            created["hangoutLink"] = "https://meet.google.com/fake-code"
        return _FakeRequest(created)

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return _FakeRequest({"items": [self._event] if self._event else []})

    def patch(self, **kwargs):
        self.patch_kwargs = kwargs
        return _FakeRequest({**self._event, **kwargs["body"]})

    def delete(self, **kwargs):
        self.delete_kwargs = kwargs
        return _FakeRequest(None)


class _FakeCalendarList:
    def __init__(self, items):
        self._items = items
        self.list_kwargs = None

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return _FakeRequest({"items": self._items})


class _FakeCalendarService:
    def __init__(self, event, calendars=None):
        self._events = _FakeEvents(event)
        self._calendar_list = _FakeCalendarList(calendars or [])

    def events(self):
        return self._events

    def calendarList(self):
        return self._calendar_list


def _own_event() -> dict:
    """An event Tom organizes, with himself + one guest on it."""
    return {
        "id": "e1",
        "summary": "Planning",
        "start": {"dateTime": "2026-07-20T10:00:00+01:00"},
        "end": {"dateTime": "2026-07-20T11:00:00+01:00"},
        "organizer": {"email": "me@example.com", "self": True},
        "attendees": [
            {"email": "me@example.com", "self": True, "responseStatus": "accepted"},
            {"email": "Bob@example.com", "responseStatus": "needsAction"},
        ],
    }


def _invite() -> dict:
    """An event someone else organizes, with Tom as an invited guest."""
    return {
        "id": "e2",
        "summary": "Dinner",
        "start": {"dateTime": "2026-07-21T19:00:00+01:00"},
        "end": {"dateTime": "2026-07-21T21:00:00+01:00"},
        "organizer": {"email": "alice@example.com"},
        "attendees": [
            {"email": "alice@example.com", "responseStatus": "accepted"},
            {"email": "me@example.com", "self": True, "responseStatus": "needsAction"},
        ],
    }


def _cal(monkeypatch, event) -> _FakeCalendarService:
    svc = _FakeCalendarService(event)
    monkeypatch.setattr(gcal, "get_service", lambda *a, **k: svc)
    return svc


def test_calendar_attendees_merges_dedupes_and_notifies(monkeypatch):
    """Existing guests keep their attendee dicts (RSVP state intact), adds
    already on the event are skipped (case-insensitive), and the patch always
    goes out with sendUpdates="all" — the whole point of the OUTWARD tier is
    that people actually get told."""
    svc = _cal(monkeypatch, _own_event())
    out = gcal.calendar_event_attendees("e1", add=["carol@x.com", "BOB@example.com"])

    kwargs = svc.events().patch_kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["sendUpdates"] == "all"
    body = kwargs["body"]["attendees"]
    # Bob's original dict survives untouched — patch replaces the whole array,
    # so dropping responseStatus would silently reset everyone to needsAction.
    assert {"email": "Bob@example.com", "responseStatus": "needsAction"} in body
    assert {"email": "carol@x.com"} in body
    assert out["added"] == ["carol@x.com"]
    assert out["skipped_already_attending"] == ["BOB@example.com"]
    assert sorted(out["attendees"]) == [
        "Bob@example.com", "carol@x.com", "me@example.com",
    ]


def test_calendar_attendees_remove_is_case_insensitive(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    out = gcal.calendar_event_attendees("e1", remove=["bob@EXAMPLE.com"])
    body = svc.events().patch_kwargs["body"]["attendees"]
    assert [a["email"] for a in body] == ["me@example.com"]
    assert out["removed"] == ["Bob@example.com"]


def test_calendar_attendees_requires_organizer(monkeypatch):
    svc = _cal(monkeypatch, _invite())
    with pytest.raises(RuntimeError, match="calendar_event_respond"):
        gcal.calendar_event_attendees("e2", add=["carol@x.com"])
    assert svc.events().patch_kwargs is None


def test_calendar_attendees_refuses_removing_organizer(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    with pytest.raises(RuntimeError, match="cannot remove the organizer"):
        gcal.calendar_event_attendees("e1", remove=["ME@example.com"])
    assert svc.events().patch_kwargs is None


def test_calendar_attendees_empty_and_noop_raise(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    with pytest.raises(RuntimeError, match="nothing to do"):
        gcal.calendar_event_attendees("e1")
    with pytest.raises(RuntimeError, match="nothing to do"):
        gcal.calendar_event_attendees("e1", add=["  "], remove=[""])
    # Everyone in add already attends + remove matches no one ⇒ no empty patch.
    with pytest.raises(RuntimeError, match="no change"):
        gcal.calendar_event_attendees(
            "e1", add=["bob@example.com"], remove=["stranger@x.com"]
        )
    assert svc.events().patch_kwargs is None


def test_calendar_attendees_rejects_non_email(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    with pytest.raises(RuntimeError, match="not an email address"):
        gcal.calendar_event_attendees("e1", add=["not-an-email"])
    assert svc.events().patch_kwargs is None


def test_calendar_respond_patches_own_entry(monkeypatch):
    svc = _cal(monkeypatch, _invite())
    out = gcal.calendar_event_respond("e2", "accepted")
    kwargs = svc.events().patch_kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["sendUpdates"] == "all"
    by_email = {a["email"]: a for a in kwargs["body"]["attendees"]}
    assert by_email["me@example.com"]["responseStatus"] == "accepted"
    # Other guests' entries ride along untouched.
    assert by_email["alice@example.com"]["responseStatus"] == "accepted"
    assert out["response"] == "accepted"
    assert out["organizer"] == "alice@example.com"


def test_calendar_respond_invalid_value_raises(monkeypatch):
    svc = _cal(monkeypatch, _invite())
    with pytest.raises(RuntimeError, match="invalid response"):
        gcal.calendar_event_respond("e2", "maybe")
    assert svc.events().patch_kwargs is None


def test_calendar_respond_own_event_and_not_invited_raise(monkeypatch):
    own = _own_event()
    own["attendees"] = []  # own event with no guests: nothing to RSVP to
    svc = _cal(monkeypatch, own)
    with pytest.raises(RuntimeError, match="calendar_event_attendees"):
        gcal.calendar_event_respond("e1", "declined")
    assert svc.events().patch_kwargs is None

    stranger = _invite()
    stranger["attendees"] = [{"email": "alice@example.com", "responseStatus": "accepted"}]
    svc = _cal(monkeypatch, stranger)
    with pytest.raises(RuntimeError, match="not on this event's guest list"):
        gcal.calendar_event_respond("e2", "declined")
    assert svc.events().patch_kwargs is None


def test_calendar_delete_notify_flag_maps_to_send_updates(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    out = gcal.calendar_event_delete("e1")
    assert svc.events().delete_kwargs == {
        "calendarId": "primary", "eventId": "e1", "sendUpdates": "all",
    }
    assert out["deleted"] is True and "Bin" in out["note"]

    svc = _cal(monkeypatch, _invite())
    gcal.calendar_event_delete("e2", notify=False)
    assert svc.events().delete_kwargs["sendUpdates"] == "none"


def test_calendar_get_event_surfaces_organizer(monkeypatch):
    """The approval resolver leans on these fields to show whose event a
    gated call touches — server-fetched, never model-supplied."""
    _cal(monkeypatch, _invite())
    out = gcal.calendar_get_event("e2")
    assert out["organizer"] == "alice@example.com"
    assert out["is_own_event"] is False
    assert {"email": "me@example.com", "response": "needsAction"} in out["attendees"]


def test_calendar_event_id_is_cross_process_deterministic():
    """crash-retry idempotency uses a content-based event id hash. Two calls
    with the same (summary, start, end) must return the same id, and a crash
    before Google 409s the second attempt will retrieve the existing event
    rather than double-booking.

    The id derivation must use hashlib, NOT builtins.hash() — the latter is
    salted per-process, so the same input hashes to different values on retry,
    defeating idempotency. This test pins both the determinism AND the absence
    of salted hashing in one go.

    Pinned in tests/test_workspace_writes.py."""
    summary = "Standup"
    start = "2026-01-05T09:00:00Z"
    end = "2026-01-05T09:15:00Z"

    eid1 = gcal._event_id(summary, start, end)
    eid2 = gcal._event_id(summary, start, end)

    # Deterministic: same inputs ⇒ same id (idempotency holds across processes).
    assert eid1 == eid2, gotcha(
        "calendar-event-id-deterministic",
        "event id must be deterministic — builtins.hash is salted per-process. "
        "Identical (summary, start, end) events 409 by design: the accepted "
        "crash-retry tradeoff (a 409 beats a double-booking), not a bug"
    )

    # Different inputs ⇒ different id (no false positives / accidental deduping).
    eid3 = gcal._event_id("Different Title", start, end)
    assert eid1 != eid3

    # Base32hex alphabet (Google's alphabet for event ids).
    assert set(eid1) <= set("0123456789abcdefghijklmnopqrstuv"), gotcha(
        "calendar-event-id-deterministic",
        f"event id must use base32hex alphabet, got chars: {set(eid1) - set('0123456789abcdefghijklmnopqrstuv')}"
    )


def test_calendar_create_with_meet_attaches_conference_request(monkeypatch):
    """conferenceDataVersion=1 must ride the insert — without it Google
    silently ignores conferenceData (HTTP 200, no link). The requestId reuses
    the deterministic event id, so a crash-retry replays the same request."""
    svc = _cal(monkeypatch, _own_event())
    out = gcal.calendar_event_create(
        "Call", "2026-07-21T10:00:00+01:00", "2026-07-21T10:30:00+01:00",
        with_meet=True,
    )
    kwargs = svc.events().insert_kwargs
    assert kwargs["conferenceDataVersion"] == 1, (
        "conferenceDataVersion=1 must ride ANY write carrying conferenceData — "
        "without it Google silently ignores it (HTTP 200, no Meet link)"
    )
    req = kwargs["body"]["conferenceData"]["createRequest"]
    assert req["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    assert req["requestId"] == kwargs["body"]["id"]
    assert out["meet_link"] == "https://meet.google.com/fake-code"

    svc2 = _cal(monkeypatch, _own_event())
    gcal.calendar_event_create(
        "Call", "2026-07-21T10:00:00+01:00", "2026-07-21T10:30:00+01:00",
        with_meet=True,
    )
    retry = svc2.events().insert_kwargs["body"]["conferenceData"]["createRequest"]
    assert retry["requestId"] == req["requestId"]


def test_calendar_create_without_meet_sends_no_conference(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    gcal.calendar_event_create("Lunch", "2026-07-21T12:00:00+01:00", "2026-07-21T13:00:00+01:00")
    kwargs = svc.events().insert_kwargs
    assert "conferenceData" not in kwargs["body"]
    assert "conferenceDataVersion" not in kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["sendUpdates"] == "none"


def test_calendar_create_calendar_id_passthrough(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    gcal.calendar_event_create(
        "Gig", "2026-07-21T20:00:00+01:00", "2026-07-21T22:00:00+01:00",
        calendar_id="family@group.calendar.google.com",
    )
    assert svc.events().insert_kwargs["calendarId"] == "family@group.calendar.google.com"


def test_calendar_update_meet_add_patches_conference(monkeypatch):
    event = _own_event()
    event["updated"] = "2026-07-20T09:00:00.000Z"
    svc = _cal(monkeypatch, event)
    gcal.calendar_event_update("e1", meet="add")
    kwargs = svc.events().patch_kwargs
    assert kwargs["conferenceDataVersion"] == 1
    assert kwargs["sendUpdates"] == "none"
    req = kwargs["body"]["conferenceData"]["createRequest"]
    assert req["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    # Stable for a crash-retry of the same event state, fresh after any change
    # — reusing a requestId after remove→re-add would replay the old, deleted
    # conference.
    assert req["requestId"] == gcal._meet_request_id("e1", "2026-07-20T09:00:00.000Z")
    assert req["requestId"] != gcal._meet_request_id("e1", "2026-07-21T09:00:00.000Z")


def test_calendar_update_meet_add_noop_when_link_exists(monkeypatch):
    event = _own_event()
    event["hangoutLink"] = "https://meet.google.com/abc"
    svc = _cal(monkeypatch, event)
    out = gcal.calendar_event_update("e1", meet="add")
    # Nothing else to change ⇒ no patch at all, just the current state + note.
    assert svc.events().patch_kwargs is None
    assert out["note"] == "event already has a Meet link"
    assert out["meet_link"] == "https://meet.google.com/abc"

    # Other fields still patch; conferenceData stays out of the body.
    svc = _cal(monkeypatch, event)
    out = gcal.calendar_event_update("e1", summary="New title", meet="add")
    kwargs = svc.events().patch_kwargs
    assert kwargs["body"] == {"summary": "New title"}
    assert "conferenceDataVersion" not in kwargs
    assert out["note"] == "event already has a Meet link"


def test_calendar_update_meet_remove_nulls_conference(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    gcal.calendar_event_update("e1", meet="remove")
    kwargs = svc.events().patch_kwargs
    # JSON null (not key-omission) is how a patch strips the conference.
    assert kwargs["body"]["conferenceData"] is None
    assert kwargs["conferenceDataVersion"] == 1


def test_calendar_update_meet_invalid_raises(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    with pytest.raises(RuntimeError, match="invalid meet"):
        gcal.calendar_event_update("e1", meet="yes")
    assert svc.events().patch_kwargs is None


def test_calendar_update_calendar_id_passthrough(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    gcal.calendar_event_update("e1", summary="New", calendar_id="work@example.com")
    assert svc.events().patch_kwargs["calendarId"] == "work@example.com"


def test_calendar_attendees_calendar_id_passthrough(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    gcal.calendar_event_attendees("e1", add=["carol@x.com"], calendar_id="work@example.com")
    assert svc.events().get_kwargs["calendarId"] == "work@example.com"
    assert svc.events().patch_kwargs["calendarId"] == "work@example.com"


def test_calendar_delete_calendar_id_passthrough(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    gcal.calendar_event_delete("e1", calendar_id="work@example.com")
    assert svc.events().delete_kwargs["calendarId"] == "work@example.com"


def test_calendar_list_events_query_passthrough(monkeypatch):
    svc = _cal(monkeypatch, _own_event())
    gcal.calendar_list_events("2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z", query="lunch")
    assert svc.events().list_kwargs["q"] == "lunch"

    svc = _cal(monkeypatch, _own_event())
    gcal.calendar_list_events("2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z")
    assert "q" not in svc.events().list_kwargs


def test_calendar_list_calendars_shapes_output(monkeypatch):
    svc = _FakeCalendarService(None, calendars=[
        {"id": "me@example.com", "summary": "Tom", "primary": True, "accessRole": "owner"},
        {"id": "share@example.com", "summary": "Shared"},
    ])
    monkeypatch.setattr(gcal, "get_service", lambda *a, **k: svc)
    out = gcal.calendar_list_calendars()
    assert out[0] == {
        "id": "me@example.com", "summary": "Tom", "primary": True, "access_role": "owner",
    }
    assert out[1] == {
        "id": "share@example.com", "summary": "Shared", "primary": False, "access_role": "",
    }


def test_event_summary_surfaces_meet_link():
    e = {"id": "x", "hangoutLink": "https://meet.google.com/abc"}
    assert gcal._event_summary(e)["meet_link"] == "https://meet.google.com/abc"
    assert gcal._event_summary({"id": "x"})["meet_link"] == ""
