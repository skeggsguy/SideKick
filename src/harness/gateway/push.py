"""Web Push to the iPad PWA.

VAPID keys are generated once and kept in the harness data dir; browser
subscriptions live in the push_subscriptions table. pywebpush is sync, so
sends run off the event loop. Dead subscriptions (404/410 from the push
service) are pruned on send.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from ..store.db import Database, utcnow

# VAPID's `sub` claim: a contact the push service can reach if this
# application misbehaves. Push services require the claim to be present and to
# be a mailto:/https: URI. Nobody checks the mailbox exists, so the default is a
# placeholder rather than anyone's real address — but Apple DOES validate the
# shape: a localhost domain is rejected with 403 BadJwtToken (FCM accepts it,
# so the breakage is Apple-only and silent). Keep the default on a real-looking
# domain. Set HARNESS_VAPID_SUB to a real mailto: if you want the push service
# to be able to reach you.
DEFAULT_VAPID_SUB = "mailto:agent@example.com"


def vapid_sub() -> str:
    """The VAPID `sub` claim, env-overridable.

    Read per send rather than at import: .env is loaded during startup, which
    happens after this module is imported. Blank or whitespace-only values fall
    back to the default (same rule as gmail.signature()) — a whitespace `sub`
    would make the push service reject the JWT, and this path fails silently.
    """
    return os.environ.get("HARNESS_VAPID_SUB", "").strip() or DEFAULT_VAPID_SUB

# How long the push service may hold an undeliverable notification. The
# pywebpush default is ttl=0 — "deliver this instant or drop" — which
# silently loses pushes whenever the device is locked or briefly offline.
TTL_SECONDS = 86400


class PushService:
    def __init__(self, db: Database, key_path: Path) -> None:
        self.db = db
        self.key_path = key_path
        self._vapid = self._load_or_create_keys()

    def _load_or_create_keys(self) -> Vapid:
        if self.key_path.exists():
            return Vapid.from_file(str(self.key_path))
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        vapid = Vapid()
        vapid.generate_keys()
        vapid.save_key(str(self.key_path))
        return vapid

    @property
    def public_key_b64url(self) -> str:
        """The applicationServerKey the browser needs: raw uncompressed
        P-256 point, base64url without padding."""
        raw = self._vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    async def subscribe(self, subscription: dict) -> None:
        await self.db.conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions (endpoint, keys_json, created_at) "
            "VALUES (?, ?, ?)",
            (
                subscription["endpoint"],
                json.dumps(subscription.get("keys", {})),
                utcnow(),
            ),
        )
        await self.db.conn.commit()

    async def notify_all(self, title: str, body: str, url: str = "/") -> int:
        """Send to every subscription; prune dead ones. Returns sends."""
        cur = await self.db.conn.execute(
            "SELECT endpoint, keys_json FROM push_subscriptions"
        )
        rows = await cur.fetchall()
        payload = json.dumps({"title": title, "body": body[:500], "url": url})
        sent = 0
        for row in rows:
            sub = {"endpoint": row["endpoint"], "keys": json.loads(row["keys_json"])}
            try:
                resp = await asyncio.to_thread(
                    webpush,
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=str(self.key_path),
                    vapid_claims={"sub": vapid_sub()},
                    ttl=TTL_SECONDS,
                    headers={"Urgency": "high"},
                )
                # Endpoint is a capability URL — log only its tail.
                print(
                    f"[push] {getattr(resp, 'status_code', '?')} "
                    f"…{row['endpoint'][-12:]}: {title!r}",
                    file=sys.stderr,
                )
                sent += 1
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status in (404, 410):  # subscription gone — prune it
                    await self.db.conn.execute(
                        "DELETE FROM push_subscriptions WHERE endpoint = ?",
                        (row["endpoint"],),
                    )
                    await self.db.conn.commit()
                else:
                    # The body carries the push service's reason (Apple:
                    # {"reason":"BadJwtToken"}) — the status alone doesn't.
                    reason = (getattr(e.response, "text", "") or "")[:200]
                    print(
                        f"[push] send failed ({status}) …{row['endpoint'][-12:]}: "
                        f"{reason or e}",
                        file=sys.stderr,
                    )
        return sent
