"""Gmail read + write tools: search, read, draft, and send.

Drafts are local (MUTATING_LOCAL) — they sit in the user's mailbox and change
nothing in the world. Sending (gmail_send / gmail_send_draft) is OUTWARD: it
leaves the account, so it is never allowlistable and always needs a human to
see the full content first (see core/policy.py).
"""

from __future__ import annotations

import base64
import mimetypes
import os
from email.message import EmailMessage
from email.utils import formataddr

from ._htmlbody import resolve_body
from ._paths import resolve_download_dest, resolve_upload_source
from .auth import get_service

MAX_BODY_CHARS = 50_000

# The agent identifies itself on every email it drafts or sends: a From display
# name over the user's real address, plus a signature appended to the body.
AGENT_NAME = "Sidekick"
DEFAULT_SIGNATURE = (
    f"Sent by {AGENT_NAME}, an AI assistant, on behalf of the account owner."
)


def signature() -> str:
    """The signature appended to agent-authored mail, env-overridable.

    Read per message rather than at import: .env is loaded when the harness
    starts, which happens after this module is imported. HARNESS_AGENT_SIGNATURE
    supplies the sentence only — the separator and blank lines are ours, so an
    override can't accidentally run into the body.
    """
    text = os.environ.get("HARNESS_AGENT_SIGNATURE", "").strip() or DEFAULT_SIGNATURE
    return f"\n\n—\n{text}"


# The account's real address, needed for the From display name (Gmail honors a
# custom display name only when the address matches the authenticated user).
# Cached after the first lookup — it never changes for a given token.
_my_address: str | None = None


def _agent_from() -> str:
    """`Sidekick <you@addr>` — the From header for agent-authored mail."""
    global _my_address
    if _my_address is None:
        svc = get_service("gmail", "v1")
        _my_address = svc.users().getProfile(userId="me").execute()["emailAddress"]
    return formataddr((AGENT_NAME, _my_address))


def gmail_search(query: str, max_results: int = 10) -> list[dict]:
    """Search Gmail with a standard Gmail query (e.g. 'is:unread newer_than:2d',
    'from:alice subject:invoice'). Returns id, from, subject, date, and snippet
    for each match; use gmail_read with an id for the full message. `date` is
    the raw Date header in the sender's zone — pass it through convert_time
    (op='to_local') before quoting a time."""
    svc = get_service("gmail", "v1")
    resp = (
        svc.users()
        .messages()
        .list(userId="me", q=query, maxResults=min(max_results, 50))
        .execute()
    )
    results = []
    for ref in resp.get("messages", []):
        msg = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = _headers(msg)
        results.append(
            {
                "id": msg["id"],
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "snippet": msg.get("snippet", ""),
            }
        )
    return results


def gmail_read(message_id: str) -> dict:
    """Read one Gmail message in full (headers + body: plain text when the
    message has one, HTML stripped to readable text otherwise) by the id
    returned from gmail_search. `date` is in the sender's zone — localize it
    with convert_time (op='to_local')."""
    svc = get_service("gmail", "v1")
    msg = (
        svc.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    headers = _headers(msg)
    payload = msg.get("payload", {})
    body = resolve_body(payload)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[...truncated]"
    return {
        "id": msg["id"],
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "body": body or msg.get("snippet", ""),
        "attachments": [
            # Public view: a short, stable `index` the model can reliably pass to
            # gmail_download_attachment — NOT Gmail's ~340-char opaque
            # attachmentId, which an LLM cannot faithfully round-trip.
            {
                "index": i,
                "filename": a["filename"],
                "mime_type": a["mime_type"],
                "size": a["size"],
            }
            for i, a in enumerate(_attachments(payload))
        ],
    }


def gmail_download_attachment(
    message_id: str, attachment_index: int, local_path: str = ""
) -> dict:
    """Download one attachment from a Gmail message to this machine.
    `attachment_index` is the `index` shown for that attachment in gmail_read's
    `attachments` list (0 for the first attachment, 1 for the second, ...).
    `local_path` may be a full file path or a directory; if empty, the file
    lands in the harness downloads dir under its original filename. Returns the
    saved path, name, and byte count."""
    svc = get_service("gmail", "v1")
    msg = (
        svc.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    # Resolve the index against THIS fetch and use the attachmentId it returns —
    # never round-trip Gmail's opaque ~340-char attachmentId through the model.
    attachments = _attachments(msg.get("payload", {}))
    if not 0 <= attachment_index < len(attachments):
        raise RuntimeError(
            f"Message {message_id!r} has {len(attachments)} attachment(s); "
            f"no index {attachment_index}. Call gmail_read for the list."
        )
    match = attachments[attachment_index]
    att = (
        svc.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=match["attachment_id"])
        .execute()
    )
    data = base64.urlsafe_b64decode(att["data"])
    dest = resolve_download_dest(local_path, match["filename"])
    dest.write_bytes(data)
    return {
        "message_id": message_id,
        "index": attachment_index,
        "name": match["filename"],
        "path": str(dest),
        "bytes": len(data),
    }


def _encode(msg: EmailMessage) -> str:
    """base64url-encode a MIME message for the Gmail API `raw` field."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _add_attachments(msg: EmailMessage, attachments: list[str] | None) -> None:
    """Attach each local file path to `msg`. A missing path raises (never a
    silent drop). Attachments ride in the message's `raw` field, so total size
    is bounded (~35 MB API ceiling, less after base64 inflation) — large files
    are out of scope here."""
    for local_path in attachments or []:
        try:
            path = resolve_upload_source(local_path)
        except RuntimeError as e:
            if "No file to upload" in str(e):
                raise RuntimeError(f"No attachment file at {local_path!r}.") from None
            raise
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=path.name,
        )


def _build_message(
    to: str,
    subject: str,
    body: str,
    attachments: list[str] | None = None,
    from_addr: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if from_addr:
        msg["From"] = from_addr
    msg.set_content(body + signature())
    _add_attachments(msg, attachments)
    return msg


def gmail_draft_create(
    to: str, subject: str, body: str, attachments: list[str] | None = None
) -> dict:
    """Create a draft email (does NOT send). `attachments` is an optional list of
    local file paths to attach (e.g. a sandbox_run output, or a file saved by
    gmail_download_attachment / drive_download). Returns the draft id; the user
    can review/edit it in Gmail and send it themselves, or approve a send."""
    svc = get_service("gmail", "v1")
    msg = _build_message(to, subject, body, attachments, from_addr=_agent_from())
    draft = (
        svc.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": _encode(msg)}})
        .execute()
    )
    return {"draft_id": draft["id"], "to": to, "subject": subject}


def gmail_draft_reply(
    message_id: str, body: str, attachments: list[str] | None = None
) -> dict:
    """Create a draft reply to an existing message (does NOT send). Threads
    correctly (In-Reply-To/References + threadId) and lands in the same Gmail
    conversation, addressed back to the original sender. `attachments` is an
    optional list of local file paths to attach."""
    svc = get_service("gmail", "v1")
    original = (
        svc.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Message-ID", "References"],
        )
        .execute()
    )
    headers = _headers(original)
    subject = headers.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg = _build_message(
        headers.get("from", ""), subject, body, attachments, from_addr=_agent_from()
    )
    msg_id = headers.get("message-id", "")
    if msg_id:
        msg["In-Reply-To"] = msg_id
        # Chain References so threading clients keep the conversation intact.
        refs = headers.get("references", "")
        msg["References"] = f"{refs} {msg_id}".strip()
    draft = (
        svc.users()
        .drafts()
        .create(
            userId="me",
            body={
                "message": {
                    "raw": _encode(msg),
                    "threadId": original.get("threadId"),
                }
            },
        )
        .execute()
    )
    return {"draft_id": draft["id"], "to": headers.get("from", ""), "subject": subject}


def gmail_send(
    to: str, subject: str, body: str, attachments: list[str] | None = None
) -> dict:
    """Send an email immediately. `attachments` is an optional list of local file
    paths to attach. OUTWARD — always requires explicit human approval, never
    auto-fires. Returns the sent message id."""
    svc = get_service("gmail", "v1")
    msg = _build_message(to, subject, body, attachments, from_addr=_agent_from())
    sent = (
        svc.users()
        .messages()
        .send(userId="me", body={"raw": _encode(msg)})
        .execute()
    )
    return {"message_id": sent["id"], "to": to, "subject": subject}


def gmail_trash_message(message_id: str) -> dict:
    """Move a Gmail message to the Trash (Gmail keeps it ~30 days, restorable
    from the Trash label; there is deliberately NO permanent-delete tool).
    DESTRUCTIVE — always needs explicit human approval, never runs unattended,
    and can never be allowlisted."""
    svc = get_service("gmail", "v1")
    msg = svc.users().messages().trash(userId="me", id=message_id).execute()
    return {"message_id": msg["id"], "labels": msg.get("labelIds", [])}


def gmail_get_draft(draft_id: str) -> dict:
    """Read an existing draft in full (recipients, subject, body, attachment
    names) by the id returned from gmail_draft_create / gmail_draft_reply.
    Shows exactly what would go out if the draft were sent — the approval UI
    uses this so a human reviews real content, not an opaque id."""
    svc = get_service("gmail", "v1")
    draft = (
        svc.users().drafts().get(userId="me", id=draft_id, format="full").execute()
    )
    msg = draft.get("message", {})
    headers = _headers(msg)
    payload = msg.get("payload", {})
    body = resolve_body(payload)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[...truncated]"
    return {
        "draft_id": draft.get("id", draft_id),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "body": body or msg.get("snippet", ""),
        "attachments": [a["filename"] for a in _attachments(payload)],
    }


def gmail_send_draft(draft_id: str) -> dict:
    """Send an existing draft by its id. OUTWARD — always requires explicit
    human approval. Returns the sent message id."""
    svc = get_service("gmail", "v1")
    sent = svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {"message_id": sent["id"], "draft_id": draft_id}


def _headers(msg: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }


def _attachments(payload: dict) -> list[dict]:
    """Depth-first walk of the MIME tree for parts that are real attachments
    (have a filename and a fetchable attachmentId)."""
    found: list[dict] = []
    body = payload.get("body", {})
    if payload.get("filename") and body.get("attachmentId"):
        found.append(
            {
                "attachment_id": body["attachmentId"],
                "filename": payload["filename"],
                "mime_type": payload.get("mimeType", ""),
                "size": body.get("size", 0),
            }
        )
    for part in payload.get("parts", []):
        found.extend(_attachments(part))
    return found
