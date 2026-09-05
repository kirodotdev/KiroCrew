"""Tests for the Web Push subsystem: sender crypto, subscription store, and the
delivery-sink fan-out (including the zero-push-on-silenced/passive regression)."""

from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from kiro_crew.notifications import vapid_keys, web_push
from kiro_crew.notifications.push_store import PushSubscriptionError, PushSubscriptionStore


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _fake_subscription() -> dict:
    """A structurally valid subscription with a real P-256 public key."""
    priv = ec.generate_private_key(ec.SECP256R1())
    raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {
        "endpoint": "https://push.example.com/abc123",
        "keys": {"p256dh": _b64(raw), "auth": _b64(os.urandom(16))},
    }


# ── vapid_keys ──────────────────────────────────────────────────────────────


def test_vapid_keys_generate_and_persist(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.notifications.vapid_keys.config_dir", lambda: tmp_path)
    vapid_keys.reset_cache_for_tests()
    pub1 = vapid_keys.public_key_b64url()
    assert isinstance(pub1, str) and len(pub1) > 40
    assert (tmp_path / "vapid_keys.json").exists()
    # A fresh cache reload from disk yields the SAME key (durable, not rotated).
    vapid_keys.reset_cache_for_tests()
    assert vapid_keys.public_key_b64url() == pub1
    # The private key parses and is EC.
    assert isinstance(vapid_keys.get_private_key(), ec.EllipticCurvePrivateKey)


# ── push_store ──────────────────────────────────────────────────────────────


def test_store_add_get_remove_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    store = PushSubscriptionStore()
    sub = _fake_subscription()
    entry = store.add(sub)
    assert entry["endpoint"] == sub["endpoint"]
    assert "user" not in entry  # no subscriber identity persisted
    assert len(store.all()) == 1
    # Re-adding the same endpoint overwrites in place (one push per device).
    store.add(sub)
    assert len(store.all()) == 1
    # Persisted across a fresh load.
    assert len(PushSubscriptionStore().all()) == 1
    assert store.remove(sub["endpoint"]) is True
    assert store.all() == []
    assert store.remove(sub["endpoint"]) is False  # idempotent


def test_store_rejects_malformed(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    store = PushSubscriptionStore()
    with pytest.raises(PushSubscriptionError):
        store.add({"endpoint": "https://x/y"})  # no keys
    with pytest.raises(PushSubscriptionError):
        store.add({"keys": {"p256dh": "a", "auth": "b"}})  # no endpoint
    with pytest.raises(PushSubscriptionError):
        store.add({"endpoint": "ftp://x/y", "keys": {"p256dh": "a", "auth": "b"}})


def test_store_corrupt_file_degrades_to_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    (tmp_path / "push_subscriptions.json").write_text("{not json", encoding="utf-8")
    assert PushSubscriptionStore().all() == []


# ── web_push sender crypto ──────────────────────────────────────────────────


def test_encrypt_produces_framed_body():
    sub = _fake_subscription()
    body = web_push._encrypt(b'{"hi":1}', sub["keys"]["p256dh"], sub["keys"]["auth"])
    # aes128gcm header: 16B salt + 4B record size + 1B key length (65) + 65B key.
    assert len(body) > 86
    assert body[16 + 4] == 65


@pytest.mark.asyncio
async def test_send_web_push_marks_gone_on_410(monkeypatch):
    sub = _fake_subscription()

    class _Resp:
        status = 410

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(
        web_push.vapid_keys, "get_private_key", lambda: ec.generate_private_key(ec.SECP256R1())
    )
    monkeypatch.setattr(web_push.vapid_keys, "public_key_b64url", lambda: "x")
    result = await web_push.send_web_push(
        _Session(), sub, {"web_push": 8030, "notification": {"title": "t"}}
    )
    assert result.gone is True


# ── delivery-sink fan-out ───────────────────────────────────────────────────


def _make_state(monkeypatch, tmp_path):
    from kiro_crew.dashboard.state import DashboardState

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def test_fanout_skips_silenced_and_passive(monkeypatch, tmp_path):
    """A muted/silenced or passive-priority note must NOT trigger a push, exactly
    as it does not bump the unread badge. This is the zero-push regression: a
    turn-completion note routed to a passive channel reaches history only."""
    state = _make_state(monkeypatch, tmp_path)
    state.broadcast_ws = MagicMock()
    scheduled: list[dict] = []
    monkeypatch.setattr(state, "_fan_out_web_push", lambda note: scheduled.append(note))

    state._deliver_note(
        {
            "ts": "t1",
            "kind": "subagent",
            "channel": "system.subagent",
            "priority": "passive",
            "title": "done",
            "body": "b",
        }
    )
    assert scheduled == []  # passive: history only, no push

    state._deliver_note(
        {
            "ts": "t2",
            "kind": "hb",
            "channel": "c",
            "priority": "default",
            "title": "x",
            "body": "y",
            "silenced": True,
        }
    )
    assert scheduled == []  # silenced: history only, no push


def test_fanout_fires_for_normal_note(monkeypatch, tmp_path):
    state = _make_state(monkeypatch, tmp_path)
    state.broadcast_ws = MagicMock()
    scheduled: list[dict] = []
    monkeypatch.setattr(state, "_fan_out_web_push", lambda note: scheduled.append(note))

    state._deliver_note(
        {
            "ts": "t3",
            "kind": "approval",
            "channel": "system.approval",
            "priority": "critical",
            "title": "approve",
            "body": "please",
        }
    )
    assert len(scheduled) == 1
    assert scheduled[0]["title"] == "approve"


def test_web_push_payload_shape(monkeypatch, tmp_path):
    from kiro_crew.dashboard.state import _web_push_payload

    payload = _web_push_payload(
        {"ts": "t", "title": "Hi", "body": "there", "url": "/chat/x", "channel": "c"}
    )
    assert payload["web_push"] == 8030
    assert payload["notification"]["title"] == "Hi"
    assert payload["notification"]["navigate"] == "/chat/x"
    assert payload["notification"]["data"]["url"] == "/chat/x"


def test_web_push_payload_truncates_oversized_body(monkeypatch, tmp_path):
    """A note body far past the 4 KB push ceiling is capped so the push arrives."""
    from kiro_crew.dashboard.state import _PUSH_BODY_MAX, _PUSH_TITLE_MAX, _web_push_payload

    payload = _web_push_payload(
        {"ts": "t", "title": "T" * 500, "body": "B" * 20000, "url": "/", "channel": "c"}
    )
    body = payload["notification"]["body"]
    title = payload["notification"]["title"]
    assert len(body) == _PUSH_BODY_MAX and body.endswith("…")
    assert len(title) == _PUSH_TITLE_MAX and title.endswith("…")
