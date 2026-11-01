"""Small key/value store for client-reported settings — currently just the
last-seen client (iPad) timezone, written by the gateway's
POST /api/client/timezone and read by the in-process timezone tools.

One row per key, last write wins. Modelled on WatchStateStore.
"""

from __future__ import annotations

from .db import Database, utcnow

TIMEZONE_KEY = "client_timezone"

#: Identity-ref values a delegated browser subagent fills into web forms. The
#: real values live ONLY here — never in LLM context, audit, traces, or session
#: history (resolved at dispatch by tools/_refs.py). REF_KEYS maps the {email}/
#: {phone} placeholder tokens (core/envelope.KNOWN_REFS) to their storage keys.
REF_EMAIL_KEY = "ref_email"
REF_PHONE_KEY = "ref_phone"
REF_KEYS = {"email": REF_EMAIL_KEY, "phone": REF_PHONE_KEY}


class ClientConfigStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, key: str) -> str | None:
        cur = await self.db.conn.execute(
            "SELECT value FROM client_config WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return row["value"] if row is not None else None

    async def set(self, key: str, value: str) -> None:
        await self.db.conn.execute(
            "INSERT INTO client_config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, utcnow()),
        )
        await self.db.conn.commit()

    async def get_timezone(self) -> str | None:
        """The last IANA timezone the client reported, or None if never set."""
        return await self.get(TIMEZONE_KEY)

    async def set_timezone(self, iana_name: str) -> None:
        await self.set(TIMEZONE_KEY, iana_name)
