"""Google Drive read tools, plus private upload into the user's own Drive."""

from __future__ import annotations

import io
import mimetypes

from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from ._paths import resolve_download_dest, resolve_upload_source
from .auth import get_service

MAX_CHARS = 100_000
FOLDER_MIME = "application/vnd.google-apps.folder"

# Google-native types are exported; everything else texty is downloaded raw.
EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def drive_list(query: str = "", max_results: int = 20) -> list[dict]:
    """List Drive files, optionally filtered with a Drive query (e.g.
    "name contains 'budget'", "mimeType='application/pdf'"). Most recently
    modified first. modifiedTime is UTC — localize with convert_time
    (op='to_local') before quoting."""
    svc = get_service("drive", "v3")
    resp = (
        svc.files()
        .list(
            q=query or None,
            pageSize=min(max_results, 100),
            orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime,size)",
        )
        .execute()
    )
    return resp.get("files", [])


def drive_file_info(file_id: str) -> dict:
    """Metadata for any Drive file or folder by id — name, mimeType,
    modifiedTime, size, trashed, webViewLink — without fetching content.
    Google Docs/Sheets ids are Drive file ids, so this also resolves a
    doc_id or spreadsheet_id to its title. Cheap existence/type check
    before drive_read or drive_download. modifiedTime is UTC — localize with
    convert_time (op='to_local') before quoting."""
    svc = get_service("drive", "v3")
    return (
        svc.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,modifiedTime,size,trashed,webViewLink",
        )
        .execute()
    )


def drive_read(file_id: str) -> dict:
    """Read a Drive file's text content by id. Google Docs/Sheets/Slides are
    exported as text/CSV; plain text files are downloaded. Binary formats
    are not supported."""
    svc = get_service("drive", "v3")
    meta = svc.files().get(fileId=file_id, fields="id,name,mimeType").execute()
    mime = meta["mimeType"]

    if mime in EXPORT_AS:
        data = svc.files().export(fileId=file_id, mimeType=EXPORT_AS[mime]).execute()
    elif mime.startswith("text/") or mime in ("application/json",):
        data = svc.files().get_media(fileId=file_id).execute()
    else:
        return {
            "id": file_id,
            "name": meta["name"],
            "error": f"Unsupported binary type {mime}; cannot read as text.",
        }

    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    if mime in EXPORT_AS:
        # Google's export adds a BOM and \r\n line endings; the Doc's internal
        # text (what docs_edit_text matches against) uses bare \n — normalize
        # so copied snippets round-trip. Raw downloads stay byte-faithful.
        text = text.removeprefix("\ufeff").replace("\r\n", "\n")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[...truncated]"
    return {"id": file_id, "name": meta["name"], "mimeType": mime, "content": text}


def drive_upload(local_path: str, name: str = "", folder_id: str = "") -> dict:
    """Upload a local file into the user's own My Drive (private — no sharing).
    `local_path` is a path on this machine inside the agent's file area (e.g. a
    sandbox_run output in the workspace); `name` defaults to the file's
    basename; `folder_id` optionally places it in a specific Drive folder.
    Returns the new file's id, name, and view url."""
    path = resolve_upload_source(local_path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    metadata: dict = {"name": name or path.name}
    if folder_id:
        metadata["parents"] = [folder_id]
    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)
    svc = get_service("drive", "v3")
    created = (
        svc.files()
        .create(body=metadata, media_body=media, fields="id,name,webViewLink")
        .execute()
    )
    return {
        "id": created["id"],
        "name": created["name"],
        "url": created.get("webViewLink"),
    }


def drive_create_folder(name: str, parent_id: str = "") -> dict:
    """Create a new folder in the user's own My Drive. `parent_id` optionally
    nests it inside an existing folder. Returns the folder's id, name, and url."""
    metadata: dict = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    svc = get_service("drive", "v3")
    created = (
        svc.files().create(body=metadata, fields="id,name,webViewLink").execute()
    )
    return {
        "id": created["id"],
        "name": created["name"],
        "url": created.get("webViewLink"),
    }


def drive_move(file_id: str, folder_id: str) -> dict:
    """Move a file (or folder) into the Drive folder `folder_id`, removing it
    from its current parents. Returns the file's id, name, and new parents."""
    svc = get_service("drive", "v3")
    current = svc.files().get(fileId=file_id, fields="parents").execute()
    previous = ",".join(current.get("parents", []))
    updated = (
        svc.files()
        .update(
            fileId=file_id,
            addParents=folder_id,
            removeParents=previous or None,
            fields="id,name,parents",
        )
        .execute()
    )
    return {
        "id": updated["id"],
        "name": updated["name"],
        "parents": updated.get("parents", []),
    }


def drive_trash_file(file_id: str) -> dict:
    """Move a Drive file (or folder) to the Trash (restorable for 30 days;
    there is deliberately NO permanent-delete tool). DESTRUCTIVE — always
    needs explicit human approval, never runs unattended, and can never be
    allowlisted."""
    svc = get_service("drive", "v3")
    updated = (
        svc.files()
        .update(fileId=file_id, body={"trashed": True}, fields="id,name,trashed")
        .execute()
    )
    return {
        "id": updated["id"],
        "name": updated.get("name"),
        "trashed": updated.get("trashed", True),
    }


def drive_rename(file_id: str, name: str) -> dict:
    """Rename a Drive file (or folder) by id. Returns its id and new name."""
    svc = get_service("drive", "v3")
    updated = (
        svc.files().update(fileId=file_id, body={"name": name}, fields="id,name").execute()
    )
    return {"id": updated["id"], "name": updated["name"]}


def drive_download(file_id: str, local_path: str = "") -> dict:
    """Download a Drive file by id to this machine (handles binaries like PDFs
    and images that drive_read cannot). `local_path` may be a full file path or
    a directory; if empty, the file lands in the harness downloads dir under its
    own name. Google-native Docs/Sheets/Slides have no binary form — use
    drive_read for those. Returns the saved path, name, and byte count."""
    svc = get_service("drive", "v3")
    meta = svc.files().get(fileId=file_id, fields="id,name,mimeType").execute()
    if meta["mimeType"].startswith("application/vnd.google-apps"):
        raise RuntimeError(
            f"{meta['name']!r} is a Google-native file ({meta['mimeType']}); it "
            "has no binary form to download. Use drive_read to get its text."
        )

    dest = resolve_download_dest(local_path, meta["name"])

    request = svc.files().get_media(fileId=file_id)
    with io.FileIO(str(dest), "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return {"id": file_id, "name": meta["name"], "path": str(dest), "bytes": dest.stat().st_size}


def drive_update_content(file_id: str, local_path: str) -> dict:
    """Replace the contents of an existing Drive file with a local file's bytes
    (a new revision; the id and sharing stay the same). `local_path` must be
    inside the agent's file area. Returns the file's id and name."""
    path = resolve_upload_source(local_path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)
    svc = get_service("drive", "v3")
    updated = (
        svc.files().update(fileId=file_id, media_body=media, fields="id,name").execute()
    )
    return {"id": updated["id"], "name": updated["name"]}
