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


@pytest.fixture
def _public_dns(monkeypatch):
    """Resolve every host to a public IP so endpoint validation is deterministic
    (no live DNS in the store tests)."""
    monkeypatch.setattr(
        "kiro_crew.notifications.push_store.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


def test_store_add_get_remove_roundtrip(monkeypatch, tmp_path, _public_dns):
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


def test_store_rejects_malformed(monkeypatch, tmp_path, _public_dns):
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    store = PushSubscriptionStore()
    with pytest.raises(PushSubscriptionError):
        store.add({"endpoint": "https://x/y"})  # no keys
    with pytest.raises(PushSubscriptionError):
        store.add({"keys": {"p256dh": "a", "auth": "b"}})  # no endpoint
    with pytest.raises(PushSubscriptionError):
        store.add({"endpoint": "ftp://x/y", "keys": {"p256dh": "a", "auth": "b"}})
    with pytest.raises(PushSubscriptionError):  # http, not https
        store.add({"endpoint": "http://push.example.com/x", "keys": {"p256dh": "a", "auth": "b"}})


def test_store_rejects_internal_ssrf_endpoint(monkeypatch, tmp_path):
    """A host resolving to a loopback/private/link-local address is rejected."""
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    store = PushSubscriptionStore()
    good_keys = {"p256dh": "a", "auth": "b"}
    for addr in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
        family = 10 if ":" in addr else 2
        monkeypatch.setattr(
            "kiro_crew.notifications.push_store.socket.getaddrinfo",
            lambda *a, _addr=addr, _fam=family, **k: [(_fam, 1, 6, "", (_addr, 443))],
        )
        with pytest.raises(PushSubscriptionError):
            store.add({"endpoint": "https://sneaky.example.com/x", "keys": good_keys})


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
    """Title/body/tag are capped on a UTF-8 BYTE budget so the push stays <4KB."""
    from kiro_crew.dashboard.state import (
        _PUSH_BODY_BYTES,
        _PUSH_TAG_BYTES,
        _PUSH_TITLE_BYTES,
        _web_push_payload,
    )

    # ASCII well past the budget.
    payload = _web_push_payload(
        {
            "ts": "t",
            "title": "T" * 1000,
            "body": "B" * 20000,
            "url": "/",
            "channel": "c",
            "group_key": "g" * 2000,
        }
    )
    n = payload["notification"]
    assert len(n["body"].encode("utf-8")) <= _PUSH_BODY_BYTES and n["body"].endswith("…")
    assert len(n["title"].encode("utf-8")) <= _PUSH_TITLE_BYTES and n["title"].endswith("…")
    assert len(n["tag"].encode("utf-8")) <= _PUSH_TAG_BYTES

    # CJK: a body of 2000 chars is ~6000 UTF-8 bytes; the char-based cap missed
    # this, so assert the byte budget actually holds and no char was split.
    cjk = _web_push_payload(
        {"ts": "t", "title": "件", "body": "件" * 3000, "url": "/", "channel": "c"}
    )
    body = cjk["notification"]["body"]
    assert len(body.encode("utf-8")) <= _PUSH_BODY_BYTES
    assert body.encode("utf-8").decode("utf-8") == body  # no broken multibyte char
