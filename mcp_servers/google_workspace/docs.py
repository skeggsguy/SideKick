"""Google Docs read-light write tools: create, append, replace.

All writes land in the user's own My Drive (no sharing, no outward effect) so
they are MUTATING_LOCAL. The model never sees Docs `batchUpdate` indices —
all index math stays inside these functions.
"""

from __future__ import annotations

import re

from .auth import get_service

DOC_URL = "https://docs.google.com/document/d/{id}/edit"


def docs_create(title: str) -> dict:
    """Create a new, empty Google Doc in My Drive. Returns its id and edit url."""
    svc = get_service("docs", "v1")
    doc = svc.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]
    return {"id": doc_id, "url": DOC_URL.format(id=doc_id)}


def _end_index(svc, doc_id: str) -> int:
    """Index just before the document's final newline — the safe insert point
    for appending text to the end of the body."""
    doc = svc.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])
    # The body always ends with a structural newline; its endIndex is one past
    # the last valid insert position, so step back one.
    end = content[-1]["endIndex"] if content else 1
    return max(1, end - 1)


def docs_append_text(doc_id: str, text: str) -> dict:
    """Append text to the end of an existing Google Doc by id."""
    svc = get_service("docs", "v1")
    index = _end_index(svc, doc_id)
    svc.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": index}, "text": text}}]},
    ).execute()
    return {"id": doc_id, "appended_chars": len(text)}


def _normalize(text: str) -> str:
    """Fold export artifacts back to the Doc's internal text form: Google's
    text/plain export carries a leading BOM and \\r\\n line endings, but the
    document's internal text (what replaceAllText matches) uses bare \\n."""
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _collect_text(content: list, parts: list[str]) -> None:
    """Accumulate the plain text of a body/table-cell content list."""
    for element in content:
        if "paragraph" in element:
            for pe in element["paragraph"].get("elements", []):
                parts.append(pe.get("textRun", {}).get("content", ""))
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _collect_text(cell.get("content", []), parts)


def _doc_text(svc, doc_id: str) -> str:
    """The document's internal plain text — used only to diagnose a failed match."""
    doc = svc.documents().get(documentId=doc_id).execute()
    parts: list[str] = []
    _collect_text(doc.get("body", {}).get("content", []), parts)
    return "".join(parts)


def _match_hint(old: str, text: str) -> str | None:
    """Why an exact match failed, if a relaxed comparison would have hit."""
    if old.casefold() in text.casefold():
        return "it matches ignoring letter case — matching is case-sensitive, fix the casing"
    if old in text.replace("\u00a0", " "):
        return (
            "the document uses non-breaking spaces where old_str has regular "
            "spaces — copy the text exactly as drive_read returns it"
        )
    def collapse(s: str) -> str:
        return re.sub(r"\s+", " ", s)
    if collapse(old) in collapse(text):
        return (
            "it matches once whitespace is collapsed — the line breaks or "
            "spacing in old_str differ from the document's"
        )
    return None


def _replace_request(svc, doc_id: str, old: str, new: str) -> int:
    """One replaceAllText batchUpdate; returns the occurrence count."""
    resp = (
        svc.documents()
        .batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": old, "matchCase": True},
                            "replaceText": new,
                        }
                    }
                ]
            },
        )
        .execute()
    )
    return resp.get("replies", [{}])[0].get("replaceAllText", {}).get(
        "occurrencesChanged", 0
    )


def _spacing_matches(old: str, doc_text: str) -> list[str]:
    """Distinct substrings of the document that equal `old` up to whitespace.
    The document's internal text diverges from any exported view in ways the
    model cannot copy around (soft line breaks are \\v internally, NBSP vs
    space, blank-paragraph runs) — so whitespace-only differences are
    recoverable, not user error."""
    parts = [p for p in re.split(r"\s+", old) if p]
    if not parts:
        return []
    pattern = re.compile(r"\s+".join(re.escape(p) for p in parts))
    return sorted({m.group(0) for m in pattern.finditer(doc_text)})


def docs_edit_text(doc_id: str, old_str: str, new_str: str) -> dict:
    """Edit a Google Doc by exact text match: every occurrence of `old_str` is
    replaced with `new_str`. This is a targeted segment edit — it does NOT
    replace the whole document. Workflow: read the doc with drive_read first
    and copy `old_str` exactly from its content; include enough surrounding
    context to be unique if only one place should change. Matching is
    case-sensitive; line endings and a leading BOM are normalized, and
    whitespace-only differences (line breaks, non-breaking spaces) are
    recovered automatically when the match is unambiguous. Raises with a
    diagnostic hint if `old_str` is not found."""
    old, new = _normalize(old_str), _normalize(new_str)
    svc = get_service("docs", "v1")
    changed = _replace_request(svc, doc_id, old, new)
    if changed:
        return {"id": doc_id, "occurrences_changed": changed}

    doc_text = _doc_text(svc, doc_id)
    matches = _spacing_matches(old, doc_text)
    if len(matches) == 1 and matches[0] != old:
        changed = _replace_request(svc, doc_id, matches[0], new)
        if changed:
            return {
                "id": doc_id,
                "occurrences_changed": changed,
                "note": (
                    "old_str matched after whitespace recovery — the document's "
                    f"actual spacing is {matches[0]!r}"
                ),
            }
    if len(matches) > 1:
        raise RuntimeError(
            f"old_str not found in document {doc_id}: it appears "
            f"{len(matches)} times with differing whitespace — include more "
            "surrounding context so the intended spot is unambiguous"
        )
    hint = _match_hint(old, doc_text)
    detail = hint or (
        "the snippet is not in the document — re-read it with drive_read "
        "and copy the exact text"
    )
    raise RuntimeError(f"old_str not found in document {doc_id}: {detail}")
