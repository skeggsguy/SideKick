"""Tests for Web Push service."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from conftest import gotcha
from harness.gateway.push import DEFAULT_VAPID_SUB, PushService, vapid_sub
from pywebpush import WebPushException


@pytest.mark.asyncio
async def test_notify_all_sends_real_ttl(db, tmp_path, monkeypatch):
    """Web Push must send with ttl >= 3600; pywebpush defaults to ttl=0,
    which silently drops pushes on devices that are briefly offline or locked.

    Pinned in tests/test_push.py."""
    push = PushService(db, tmp_path / "vapid.pem")

    # Subscribe a test subscription
    await push.subscribe({
        "endpoint": "https://push.example.com/x",
        "keys": {"p256dh": "test_key", "auth": "test_auth"}
    })

    # Monkeypatch webpush to record the call
    recorded = {}
    def fake_webpush(**kwargs):
        recorded.update(kwargs)
        resp = Mock()
        resp.status_code = 201
        return resp

    monkeypatch.setattr("harness.gateway.push.webpush", fake_webpush)

    # Send the notification
    sent = await push.notify_all("Test Title", "Test Body", url="/test")

    assert sent == 1, "One subscription should have been sent"
    assert "ttl" in recorded, "ttl kwarg must be passed to webpush"
    assert recorded["ttl"] >= 3600, gotcha(
        "web-push-ttl",
        "notify_all sent ttl too low — Apple drops the push unless the device is reachable that instant"
    )


@pytest.mark.asyncio
async def test_vapid_sub_claim_sent_and_env_overridable(db, tmp_path, monkeypatch):
    """Every send carries a `sub` claim; HARNESS_VAPID_SUB overrides the default.

    Push services reject a VAPID JWT with no `sub`, so an unset env var must
    still yield a valid mailto:/https: URI rather than an empty string.
    """
    push = PushService(db, tmp_path / "vapid.pem")
    await push.subscribe({
        "endpoint": "https://push.example.com/x",
        "keys": {"p256dh": "test_key", "auth": "test_auth"},
    })

    recorded = {}

    def fake_webpush(**kwargs):
        recorded.update(kwargs)
        resp = Mock()
        resp.status_code = 201
        return resp

    monkeypatch.setattr("harness.gateway.push.webpush", fake_webpush)

    monkeypatch.delenv("HARNESS_VAPID_SUB", raising=False)
    await push.notify_all("T", "B")
    assert recorded["vapid_claims"] == {"sub": DEFAULT_VAPID_SUB}
    assert DEFAULT_VAPID_SUB.startswith(("mailto:", "https://"))
    # Apple 403s (BadJwtToken) a localhost sub while FCM accepts it — the
    # 2026-09 iOS-only push outage. Never default to a localhost address.
    assert "localhost" not in DEFAULT_VAPID_SUB

    monkeypatch.setenv("HARNESS_VAPID_SUB", "mailto:ops@example.org")
    await push.notify_all("T", "B")
    assert recorded["vapid_claims"] == {"sub": "mailto:ops@example.org"}


@pytest.mark.parametrize("value", ["", "   "])
def test_vapid_sub_blank_env_falls_back_to_default(monkeypatch, value):
    """A blank or whitespace override would produce a JWT the push service
    rejects, and this path fails silently — fall back to the default."""
    monkeypatch.setenv("HARNESS_VAPID_SUB", value)
    assert vapid_sub() == DEFAULT_VAPID_SUB


@pytest.mark.asyncio
async def test_dead_subscription_pruned_on_410(db, tmp_path, monkeypatch):
    """Dead subscriptions (410 Gone) are pruned from the database on send."""
    push = PushService(db, tmp_path / "vapid.pem")

    endpoint = "https://push.example.com/dead"
    await push.subscribe({
        "endpoint": endpoint,
        "keys": {"p256dh": "test_key", "auth": "test_auth"}
    })

    # Verify subscription exists
    cur = await db.conn.execute("SELECT COUNT(*) as cnt FROM push_subscriptions")
    row = await cur.fetchone()
    assert row["cnt"] == 1

    # Monkeypatch webpush to raise 410
    def fake_webpush(**kwargs):
        resp = Mock()
        resp.status_code = 410
        raise WebPushException("Gone", response=resp)

    monkeypatch.setattr("harness.gateway.push.webpush", fake_webpush)

    # Send the notification
    sent = await push.notify_all("Test Title", "Test Body")

    assert sent == 0, "Dead subscription should not count as sent"

    # Verify subscription was deleted
    cur = await db.conn.execute("SELECT COUNT(*) as cnt FROM push_subscriptions")
    row = await cur.fetchone()
    assert row["cnt"] == 0, "Dead subscription must be pruned from the database"
