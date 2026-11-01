"""The Gateway composition dataclass — everything the routes need.

Lives apart from app.py so the route modules can type-hint it without
importing the app (which imports them). Composed by agentd; tests import it
via app.py's re-export.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .routes.chat_run import ChatRun

from ..core.agent import Agent
from ..core.approvals import ApprovalWaiter
from ..core.events import EventBus
from ..core.naming import SessionNamer
from ..memory import MemoryStore
from ..store.approvals import ApprovalStore
from ..store.audit import AllowlistStore
from ..store.client_config import ClientConfigStore
from ..store.runs import RunStore
from ..store.sessions import SessionStore
from ..store.triggers import TriggerStore
from .broadcast import RunBroadcaster
from .live import BgLive
from .preview import ApprovalPreviews
from .push import PushService
from .stt import WhisperTranscriber


@dataclass
class Gateway:
    """Everything the routes need, composed by agentd."""

    sessions: SessionStore
    runs: RunStore
    approval_store: ApprovalStore
    waiter: ApprovalWaiter
    triggers: TriggerStore
    bus: EventBus
    push: PushService
    #: Builds the agent for one run. The daemon's factory also accepts a
    #: chat_model="sonnet" keyword (opt-in Sonnet chat turns); background
    #: callers (orchestrator, catalog snapshot) never pass it — those runs are
    #: always local.
    agent_factory: Callable[..., Agent]
    memory: MemoryStore | None = None
    #: Last-seen client timezone is written here by /api/client/timezone and read
    #: by the in-process timezone tools (None disables the endpoint's persist).
    client_config: ClientConfigStore | None = None
    #: Live event hub for background runs (None disables /api/events).
    broadcaster: RunBroadcaster | None = None
    #: Allowlist store — backs the Tools-Security screen's standing grants.
    allowlist: AllowlistStore | None = None
    #: Immutable tool catalog snapshotted at startup ([{name, description,
    #: classification, remote}]); classifications never change at runtime, so
    #: GET /api/tools serves this instead of building an Agent per request.
    tool_catalog: list[dict] | None = None
    #: Names user-initiated chats from their first message (None disables it).
    namer: SessionNamer | None = None
    #: The daemon's externally-reachable base URL (HARNESS_PUBLIC_URL, e.g. the
    #: tailnet `https://<host>.ts.net`). Required for the in-app OAuth redirect;
    #: when None, /api/oauth/{vendor}/start returns 503.
    public_url: str | None = None
    #: LIVE resolver registry for approval previews (gateway/preview.py) —
    #: dispatches adapted READ_ONLY MCP tools + the trigger store to fetch
    #: what a pending call's opaque ids refer to. None degrades every
    #: approval modal to raw args.
    previews: ApprovalPreviews | None = None
    #: In-process Whisper dictation; None (HARNESS_STT=off) 503s /api/transcribe.
    stt: WhisperTranscriber | None = None
    #: Live interactive chat runs by session id. Triple duty: the 409 guard
    #: against concurrent turns on one session, the strong reference that keeps
    #: a detached run's task alive, and the `active` flag on /api/sessions.
    active_chats: dict[str, asyncio.Task] = field(default_factory=dict)
    #: The ChatRun handle for each live chat (session id -> ChatRun), read by
    #: POST /api/chat/{id}/stop for the agent reference and the answer-complete
    #: flag. Kept beside active_chats rather than replacing its value type,
    #: which several call sites already read as a Task.
    chat_runs: dict[str, ChatRun] = field(default_factory=dict)
    #: Session ids a background (cron/watcher) run is writing right now —
    #: agentd passes the Orchestrator's OWN set, shared by reference. Those
    #: runs never enter active_chats, so busy_reason() reads this too. Stays
    #: off the `active` flag deliberately: resume logic branches on it and a
    #: background run has no event ring to join.
    background_sessions: set[str] = field(default_factory=set)
    #: Start time + current phase of each live background run, fed by the
    #: orchestrator's run events (agentd tees them here) — /messages' bg_live.
    bg_live: BgLive = field(default_factory=BgLive)
    #: The local lane's llama.cpp base URL (HARNESS_LOCAL_URL) — always set
    #: (the harness is fully local). Enables the /slots SlotWatcher; Sonnet
    #: chat turns skip it (their tokens don't ride the local server).
    local_url: str | None = None
    #: The opt-in Sonnet chat option is available (ANTHROPIC_API_KEY is set).
    #: Gates the web UI's Sonnet dropdown entry; a request for Sonnet without
    #: it is a 422. Nothing else consults the key.
    sonnet_available: bool = False

    def busy_reason(self, session_id: str | None) -> str | None:
        """The 409 copy when a new chat turn must not start on this session,
        else None. A second writer on a live session keeps its own
        Session._next_idx, so the two collide on messages.idx: a live chat
        turn (no cancel-on-disconnect, so a resend could overlap it) or a
        background run that owns the session (the 2026-10-25 cron death).
        Any NEW entry point that resumes/writes a session must check this."""
        if not session_id:
            return None
        live = self.active_chats.get(session_id)
        if live is not None and not live.done():
            return "Sidekick is still finishing the last reply — try again in a moment."
        if session_id in self.background_sessions:
            return (
                "A scheduled run is still working in this chat — "
                "try again when it finishes."
            )
        return None
