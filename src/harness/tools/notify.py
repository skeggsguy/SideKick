"""notify: send a Web Push notification to Tom's subscribed devices.

Destination is pinned to the push_subscriptions table (Tom's own devices), never
model-controlled. Classification is MUTATING_LOCAL (allowlistable) — the tool is
designed to be allowlisted so jobs can proactively notify without parking an
approval each time. Per-instance cap prevents spam.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..core.policy import ActionClass
from .base import Tool


class NotifyTool(Tool):
    name = "notify"
    description = (
        "Push a notification to the user's devices. Scheduled runs send no "
        "automatic completion push, so this is the only way to alert the user "
        "— use it for findings worth interrupting them for. Max 5 per run."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Max 80 chars."},
            "body": {"type": "string"},
        },
        "required": ["title", "body"],
    }
    classification = ActionClass.MUTATING_LOCAL
    remote = False

    def __init__(
        self, send: Callable[[str, str, str], Awaitable[int]]
    ) -> None:
        self._send = send
        self._sent = 0

    async def run(self, args: dict[str, Any]) -> str:
        if self._sent >= 5:
            return (
                "notify cap reached (5 per run). Summarize your findings instead "
                "of sending more notifications."
            )

        title = str(args.get("title", "")).strip()
        if not title:
            return "notify error: 'title' is required."
        if len(title) > 80:
            return "notify error: 'title' must be ≤80 characters."

        body = str(args.get("body", "")).strip()
        if not body:
            return "notify error: 'body' is required."

        count = await self._send(title, body, "/")
        self._sent += 1

        if count == 0:
            return (
                "no devices subscribed — the user must enable notifications in "
                "the web UI."
            )
        return f"notified {count} device(s)."
