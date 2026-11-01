"""The one-session holder Lane B tools are built around: at most one
BrowserSession, lazily connected on first navigate, shared by all seven
interaction tools and the browser_delegate sub-agent so the tab persists
between calls."""

from __future__ import annotations

import weakref
from typing import Awaitable, Callable

from ._browser_session import BrowserSession
from ._netguard import Resolver, default_resolver
from .browser_fetch import (
    _CDP_PORT,
    _CHROME_MISSING_MSG,
    ChromeUnavailable,
    _browser_ws_url,
)


#: Every live manager (one per browser_delegate run), so an approval preview
#: can find the tab a parked click belongs to — see manager_for_run.
_LIVE: "weakref.WeakSet[BrowserSessionManager]" = weakref.WeakSet()


def manager_for_run(run_id: str | None) -> "BrowserSessionManager | None":
    """The manager of the delegate running under `run_id` (an approval row's
    run), or None — also when two run at once (parallel calls): ambiguous, so
    the card falls back to the raw args rather than name the wrong tab."""
    if run_id is None:
        return None
    found = [m for m in list(_LIVE) if m.run_id_fn() == run_id]
    return found[0] if len(found) == 1 else None


class BrowserSessionManager:
    """Holds at most one BrowserSession, lazily connected on first navigate.

    Injectable transport params mirror BrowserFetchTool so offline tests can
    swap the ws-url getter and resolver.
    """

    def __init__(
        self,
        *,
        cdp_port: int = _CDP_PORT,
        resolver: Resolver = default_resolver,
        ws_url_getter: Callable[[int], Awaitable[str]] = _browser_ws_url,
    ) -> None:
        self._cdp_port = cdp_port
        self._resolver = resolver
        self._ws_url_getter = ws_url_getter
        self._session: BrowserSession | None = None
        #: The run whose approvals this tab answers; browser_delegate binds it.
        self.run_id_fn: Callable[[], str | None] = lambda: None
        _LIVE.add(self)

    async def get_or_create(self) -> BrowserSession:
        """The live session, connecting one on first use. Surfaces an actionable
        message (naming the systemd units) when Chrome is unreachable."""
        if self._session is None:
            session = BrowserSession(
                cdp_port=self._cdp_port,
                resolver=self._resolver,
                ws_url_getter=self._ws_url_getter,
            )
            try:
                await session.connect()
            except ChromeUnavailable as exc:
                raise ChromeUnavailable(_CHROME_MISSING_MSG) from exc
            self._session = session
        return self._session

    def get(self) -> BrowserSession | None:
        """The live session, or None (so a tool can say 'call browser_navigate first')."""
        return self._session

    @property
    def resolver(self) -> Resolver:
        """The SSRF resolver this manager builds sessions with — so a tool can
        run the same check_target BEFORE get_or_create's lazy Chrome connect."""
        return self._resolver

    def current_host(self) -> str | None:
        return self._session.current_host if self._session else None

    def element_host(self, handle: str) -> str | None:
        """The host a handle's element actually belongs to (frame-aware) —
        the ref-use policy/resolver checks THIS, not the tab host."""
        return self._session.host_for_handle(handle) if self._session else None

    def element_label(self, handle: str) -> str | None:
        """What the handle names on the page (see BrowserSession.label_for_handle)."""
        return self._session.label_for_handle(handle) if self._session else None

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
