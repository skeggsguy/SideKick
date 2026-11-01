"""Read-only Outlook mail tools (Microsoft Graph)."""

from __future__ import annotations

from . import graph


def outlook_mail_search(query: str = "", max_results: int = 10) -> list[dict]:
    """Search Outlook mail. `query` uses Graph $search syntax (plain
    words, or e.g. 'from:alice subject:invoice'); empty lists the most
    recent messages. Returns id, sender, subject, received, read state and
    a short preview — use outlook_mail_read for the full body."""
    params: dict = {
        "$top": min(max_results, 25),
        "$select": "id,from,subject,receivedDateTime,isRead,bodyPreview",
    }
    headers = {}
    if query:
        params["$search"] = f'"{query}"'
        headers["ConsistencyLevel"] = "eventual"
    else:
        params["$orderby"] = "receivedDateTime desc"
    data = graph.get("/me/messages", params=params, headers=headers)
    return [
        {
            "id": m["id"],
            "from": (m.get("from") or {}).get("emailAddress", {}).get("address", ""),
            "subject": m.get("subject", ""),
            "received": m.get("receivedDateTime", ""),
            "is_read": m.get("isRead", True),
            "snippet": (m.get("bodyPreview") or "")[:200],
        }
        for m in data.get("value", [])
    ]


def outlook_mail_read(message_id: str) -> dict:
    """Read one message in full (plain-text body) by id from outlook_mail_search."""
    data = graph.get(
        f"/me/messages/{message_id}",
        params={"$select": "subject,from,toRecipients,receivedDateTime,body"},
        # Ask Graph to convert HTML bodies to text server-side.
        headers={"Prefer": 'outlook.body-content-type="text"'},
    )
    return {
        "subject": data.get("subject", ""),
        "from": (data.get("from") or {}).get("emailAddress", {}).get("address", ""),
        "to": [
            r.get("emailAddress", {}).get("address", "")
            for r in data.get("toRecipients", [])
        ],
        "received": data.get("receivedDateTime", ""),
        "body": (data.get("body") or {}).get("content", ""),
    }
