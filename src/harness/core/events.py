"""Event types + the bus that makes every invocation path uniform.

You typing, a cron firing, a watcher hit, an approval decision — each is an
event published here; the orchestrator consumes them and turns them into
agent runs. Interactive chat is one event type among several, which is what
makes proactivity the same code path as conversation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Union


@dataclass
class UserMessage:
    text: str
    channel: str = "web"
    session_id: str | None = None  # None ⇒ orchestrator creates a session


@dataclass
class CronFired:
    trigger_id: str
    playbook: str  # playbook name, or inline prompt for one-off reminders
    channel: str = "web"
    autonomy: str = "supervised"
    late: bool = False  # fired via catch-up after sleep/downtime
    #: The trigger row's tool allocation; None ⇒ fall back to playbook
    #: frontmatter (legacy name rows) or the full toolset.
    tools: list[str] | None = None
    #: The trigger row's display label; becomes the run's session title (and
    #: push title) so a job's chat isn't named by its prompt's first line.
    label: str | None = None


@dataclass
class WatchHit:
    watcher: str
    payload: dict = field(default_factory=dict)


@dataclass
class ApprovalDecision:
    approval_id: str
    approved: bool


Event = Union[UserMessage, CronFired, WatchHit, ApprovalDecision]


class EventBus:
    """A single asyncio queue; publishers put, the orchestrator gets."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

    def publish(self, event: Event) -> None:
        self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()
