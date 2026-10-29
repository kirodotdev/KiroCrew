"""Tests for the Web Push subsystem: sender crypto, subscription store, and the
delivery-sink fan-out (including the zero-push-on-silenced/passive regression)."""

from __future__ import annotations

import base64
import contextlib
import json
import random
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from kiro_crew.notifications import vapid_keys, web_push
from kiro_crew.notifications.push_store import PushSubscriptionError, PushSubscriptionStore
from kiro_crew.testing.wait import await_until


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
        "keys": {"p256dh": _b64(raw), "auth": _b64(random.Random(7821).randbytes(16))},
    }


# ── vapid_keys ──────────────────────────────────────────────────────────────


def test_vapid_keys_generate_and_persist(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.notifications.vapid_keys.config_dir", lambda: tmp_path)
    vapid_keys.reset_cache_for_tests()
    pub1 = vapid_keys.public_key_b64url()
    assert isinstance(pub1, str) and len(pub1) > 40
    assert (tmp_path / "web-push" / "vapid_keys.json").exists()
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
        "kiro_crew.link_unfurl._default_resolve", lambda _host, _port: ["93.184.216.34"]
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
    for addr in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "100.64.0.1", "fec0::1"):
        monkeypatch.setattr(
            "kiro_crew.link_unfurl._default_resolve", lambda _host, _port, _addr=addr: [_addr]
        )
        with pytest.raises(PushSubscriptionError):
            store.add({"endpoint": "https://sneaky.example.com/x", "keys": good_keys})


def test_store_corrupt_file_degrades_to_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    (tmp_path / "web-push").mkdir()
    (tmp_path / "web-push" / "subscriptions.json").write_text("{not json", encoding="utf-8")
    assert PushSubscriptionStore().all() == []


def test_store_load_does_no_dns(monkeypatch, tmp_path):
    """Loading runs on the gateway loop at boot, so it must not resolve names.

    The address rule is enforced at connect time by ``PublicAddressResolver``.
    """
    import json as _json

    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    (tmp_path / "web-push").mkdir()
    (tmp_path / "web-push" / "subscriptions.json").write_text(
        _json.dumps(
            {
                "subscriptions": {
                    "https://push.example.com/x": {
                        "endpoint": "https://push.example.com/x",
                        "keys": {"p256dh": "a", "auth": "b"},
                    },
                    "http://push.example.com/y": {
                        "endpoint": "http://push.example.com/y",
                        "keys": {"p256dh": "a", "auth": "b"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    def _no_dns(*_a, **_k):
        raise AssertionError("load must not resolve DNS")

    monkeypatch.setattr("kiro_crew.link_unfurl._default_resolve", _no_dns)
    assert [e["endpoint"] for e in PushSubscriptionStore().all()] == ["https://push.example.com/x"]


def test_store_rejects_invalid_port(monkeypatch, tmp_path, _public_dns):
    """A malformed port is a 400-class PushSubscriptionError, not a bare ValueError."""
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    store = PushSubscriptionStore()
    for endpoint in ("https://push.example.com:99999/x", "https://push.example.com:abc/x"):
        with pytest.raises(PushSubscriptionError):
            store.add({"endpoint": endpoint, "keys": {"p256dh": "a", "auth": "b"}})


def test_store_files_live_in_the_masked_directory(monkeypatch, tmp_path, _public_dns):
    """Both stores and their atomic-write temps sit under the sandbox-masked dir."""
    from kiro_crew.notifications.push_store import WEB_PUSH_DIR_LEAF
    from kiro_crew.sandbox_leaves import _CREW_HIDDEN_LEAVES
    from kiro_crew.sandbox_precreate import _CREW_PRECREATE_HIDDEN_DIR_LEAVES
    from kiro_crew.security.paths_lists import _CREW_SECRET_LEAVES

    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.vapid_keys.config_dir", lambda: tmp_path)
    vapid_keys.reset_cache_for_tests()
    PushSubscriptionStore().add(_fake_subscription())
    vapid_keys.public_key_b64url()
    vapid_keys.reset_cache_for_tests()
    assert sorted(p.name for p in tmp_path.iterdir()) == [WEB_PUSH_DIR_LEAF]
    assert WEB_PUSH_DIR_LEAF in _CREW_HIDDEN_LEAVES
    assert WEB_PUSH_DIR_LEAF in _CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert WEB_PUSH_DIR_LEAF in _CREW_SECRET_LEAVES


@pytest.mark.asyncio
async def test_public_address_resolver_drops_internal_addresses(monkeypatch):
    """Delivery connects only to public addresses, even after a DNS change."""
    from kiro_crew.notifications.web_push import PublicAddressResolver

    def _row(addr):
        return {"hostname": "h", "host": addr, "port": 443, "family": 2, "proto": 6, "flags": 0}

    answers = [_row("127.0.0.1"), _row("93.184.216.34"), _row("10.0.0.5")]

    async def _fake_resolve(self, host, port=0, family=0):
        return list(answers)

    monkeypatch.setattr("aiohttp.resolver.ThreadedResolver.resolve", _fake_resolve)
    resolver = PublicAddressResolver()
    assert [r["host"] for r in await resolver.resolve("h", 443)] == ["93.184.216.34"]
    answers[:] = [_row("127.0.0.1"), _row("169.254.169.254"), _row("100.64.0.1")]
    with pytest.raises(OSError):
        await resolver.resolve("h", 443)
    await resolver.close()


# ── web_push sender crypto ──────────────────────────────────────────────────


def test_encrypt_produces_framed_body():
    sub = _fake_subscription()
    body = web_push._encrypt(b'{"hi":1}', sub["keys"]["p256dh"], sub["keys"]["auth"])
    # aes128gcm header: 16B salt + 4B record size + 1B key length (65) + 65B key.
    assert len(body) > 86
    assert body[16 + 4] == 65


def test_encrypt_matches_rfc8291_appendix_a_vector():
    """Pin _encrypt byte-for-byte against the RFC 8291 Appendix A test vector.

    This is the only check that validates the aes128gcm framing against an
    INDEPENDENT source rather than against our own round trip: the RFC publishes
    the plaintext, both key pairs, the auth secret, the salt, and the exact
    resulting body. Injecting the fixed salt + server key reproduces it, proving
    the ECDH/HKDF/AES-128-GCM implementation is interoperable, not merely
    self-consistent. Values verbatim from RFC 8291 §A (base64url, unpadded).
    """
    from cryptography.hazmat.primitives.asymmetric import ec

    plaintext = b"When I grow up, I want to be a watermelon"
    # Receiver (user agent) key pair + auth secret.
    ua_public_b64 = (
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
    )
    auth_b64 = "BTBZMqHH6r4Tts7J_aSIgg"
    # Sender (application server) private key d, and the salt, both fixed by §A.
    as_private_d_b64 = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
    salt_b64 = "DGv6ra1nlYgDCS1FRnbzlw"

    d = int.from_bytes(web_push._b64url_decode(as_private_d_b64), "big")
    server_private = ec.derive_private_key(d, ec.SECP256R1())
    salt = web_push._b64url_decode(salt_b64)

    body = web_push._encrypt(
        plaintext, ua_public_b64, auth_b64, salt=salt, server_private=server_private
    )
    # The full aes128gcm body published in RFC 8291 §A.
    expected = web_push._b64url_decode(
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
        "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )
    assert body == expected


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
        def __init__(self):
            self.post_kwargs = None

        def post(self, *a, **k):
            self.post_kwargs = k
            return _Resp()

    monkeypatch.setattr(
        web_push.vapid_keys, "get_private_key", lambda: ec.generate_private_key(ec.SECP256R1())
    )
    monkeypatch.setattr(web_push.vapid_keys, "public_key_b64url", lambda: "x")
    session = _Session()
    result = await web_push.send_web_push(
        session, sub, {"web_push": 8030, "notification": {"title": "t"}}
    )
    assert result.gone is True
    # Redirects must never be followed: a public endpoint 3xx-ing to an internal
    # address would otherwise turn the fan-out POST into an SSRF (the endpoint is
    # only SSRF-validated at subscribe time).
    assert session.post_kwargs.get("allow_redirects") is False


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
    from kiro_crew.dashboard.state_parts.dashboard_services import _web_push_payload

    payload = _web_push_payload(
        {"ts": "t", "title": "Hi", "body": "there", "url": "/chat/x", "channel": "c"}
    )
    assert payload["web_push"] == 8030
    assert payload["notification"]["title"] == "Hi"
    assert payload["notification"]["navigate"] == "/chat/x"
    assert payload["notification"]["data"]["url"] == "/chat/x"


def test_web_push_payload_truncates_oversized_body(monkeypatch, tmp_path):
    """Title/body/tag are capped on a UTF-8 BYTE budget so the push stays <4KB."""
    from kiro_crew.dashboard.notification_coordinator import NATIVE_TAG_MAX_BYTES
    from kiro_crew.dashboard.state_parts.dashboard_services import (
        _PUSH_BODY_BYTES,
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
    assert len(n["tag"].encode("utf-8")) <= NATIVE_TAG_MAX_BYTES

    # CJK: a body of 2000 chars is ~6000 UTF-8 bytes; the char-based cap missed
    # this, so assert the byte budget actually holds and no char was split.
    cjk = _web_push_payload(
        {"ts": "t", "title": "件", "body": "件" * 3000, "url": "/", "channel": "c"}
    )
    body = cjk["notification"]["body"]
    assert len(body.encode("utf-8")) <= _PUSH_BODY_BYTES
    assert body.encode("utf-8").decode("utf-8") == body  # no broken multibyte char
    # The SERIALIZED payload (as web_push sends it, ensure_ascii=False) must stay
    # under the 4 KB Web Push ceiling — ensure_ascii=True would expand each CJK
    # char to \\uXXXX (6 bytes) and blow the budget.
    serialized = json.dumps(cjk, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(serialized) < 4096


def test_web_push_payload_replaces_an_oversized_deep_link():
    """The deep link is copied twice into the payload; a long one is replaced
    by the dashboard root, never cut into a link that points nowhere."""
    from kiro_crew.dashboard.state_parts.dashboard_services import _web_push_payload

    long_url = "/apps/x/" + "a" * 2000
    n = _web_push_payload({"ts": "t", "title": "Hi", "body": "b", "url": long_url})["notification"]
    assert n["navigate"] == "/"
    assert n["data"]["url"] == "/"

    short = _web_push_payload({"ts": "t", "title": "Hi", "body": "b", "url": "/chat/x"})
    assert short["notification"]["navigate"] == "/chat/x"


def test_web_push_payload_worst_case_fits_the_push_limit():
    """Every field at its maximum, in characters that JSON escapes to 6 bytes
    or UTF-8 encodes to 3, still encrypts under the 4096 byte Web Push
    ceiling, with the plaintext inside the module's own payload budget."""
    from kiro_crew.dashboard.state_parts import dashboard_services
    from kiro_crew.dashboard.state_parts.dashboard_services import _web_push_payload

    # The aes128gcm framing adds 103 bytes (86 header, 16 tag, 1 delimiter).
    plain_budget = getattr(dashboard_services, "_PUSH_PAYLOAD_BYTES", 4096 - 103)

    for filler in ("\x01", "件", '"'):
        payload = _web_push_payload(
            {
                "ts": "2026-10-23T05:00:00.000000+00:00",
                "title": filler * 500,
                "body": filler * 20000,
                # Just under the replacement threshold, so it is kept as is.
                "url": "/" + "%" * 383,
                "channel": filler * 2000,
                "group_key": filler * 2000,
            }
        )
        plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        assert len(plain) <= plain_budget, (repr(filler), len(plain))
        sub = _fake_subscription()
        sealed = web_push._encrypt(plain, sub["keys"]["p256dh"], sub["keys"]["auth"])
        assert len(sealed) <= 4096, (repr(filler), len(sealed))


@pytest.mark.asyncio
async def test_send_web_push_loads_vapid_keys_off_the_event_loop(monkeypatch, tmp_path):
    """A cold key cache reads or generates the keypair file; that IO must run
    in a worker thread, never on the gateway's event loop."""
    import threading

    monkeypatch.setattr(vapid_keys, "config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    vapid_keys.reset_cache_for_tests()
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real_get_keys = vapid_keys._get_keys

    def _recording_get_keys():
        seen.append(threading.get_ident())
        return real_get_keys()

    monkeypatch.setattr(vapid_keys, "_get_keys", _recording_get_keys)

    class _Resp:
        status = 201

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    try:
        result = await web_push.send_web_push(
            _Session(), _fake_subscription(), {"notification": {"title": "t"}}
        )
    finally:
        vapid_keys.reset_cache_for_tests()
    assert result.gone is False
    assert seen, "the sender never read the VAPID keys"
    assert loop_thread not in seen
    assert (tmp_path / "web-push" / "vapid_keys.json").exists()


def test_store_construction_reads_no_file(monkeypatch, tmp_path):
    """Building the store (done in DashboardState.__init__ on the gateway loop)
    must not touch the subscriptions file; the first read loads it."""
    from kiro_crew.notifications import push_store

    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    (tmp_path / "web-push").mkdir()
    (tmp_path / "web-push" / "subscriptions.json").write_text(
        json.dumps(
            {
                "subscriptions": {
                    "https://push.example.com/x": {
                        "endpoint": "https://push.example.com/x",
                        "keys": {"p256dh": "a", "auth": "b"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    reads: list[int] = []
    real_store_path = push_store._store_path

    def _counting_store_path():
        reads.append(1)
        return real_store_path()

    monkeypatch.setattr(push_store, "_store_path", _counting_store_path)
    store = PushSubscriptionStore()
    assert reads == []
    assert [e["endpoint"] for e in store.all()] == ["https://push.example.com/x"]
    assert len(reads) == 1
    store.all()
    assert len(reads) == 1  # loaded once, then served from memory


@pytest.mark.asyncio
async def test_fan_out_reads_subscriptions_off_the_event_loop(monkeypatch, tmp_path):
    """The fan-out's first store read loads the file, so it runs in a worker
    thread, never on the gateway's event loop."""
    import threading

    state = _make_state(monkeypatch, tmp_path)
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _recording_all():
        seen.append(threading.get_ident())
        return []

    monkeypatch.setattr(state.push_subscription_store, "all", _recording_all)
    await state._send_web_push_all({"title": "t", "body": "b", "channel": "c"})
    assert seen, "the fan-out never read the subscriptions"
    assert loop_thread not in seen


# ── one OS-banner tag for the tab toast and the Web Push banner ─────────────


def _delivered(monkeypatch, tmp_path, note: dict) -> tuple[dict, dict]:
    """Deliver *note*; return (the note the tab receives, the push payload)."""
    from kiro_crew.dashboard.state_parts.dashboard_services import _web_push_payload

    state = _make_state(monkeypatch, tmp_path)
    state.broadcast_ws = MagicMock()
    scheduled: list[dict] = []
    monkeypatch.setattr(state, "_fan_out_web_push", lambda n: scheduled.append(n))
    state._deliver_note(note)
    assert len(scheduled) == 1
    tab_note = state._notification_log[-1]
    return tab_note, _web_push_payload(scheduled[0])


def test_tab_note_and_push_payload_share_one_tag(monkeypatch, tmp_path):
    """A hidden dashboard tab and the service worker both banner the same note.
    The OS collapses only equal tags, so the tag on the note the tab toasts from
    must equal the push payload's tag, or the user sees two banners."""
    tab_note, payload = _delivered(
        monkeypatch,
        tmp_path,
        {
            "ts": "1700000000.5",
            "kind": "job",
            "channel": "c",
            "group_key": "grp",
            "job_id": "j-42",
            "title": "Deploy done",
            "body": "b",
        },
    )
    assert tab_note["tag"] == "job_id:j-42"
    assert payload["notification"]["tag"] == tab_note["tag"]


def test_distinct_events_get_distinct_tags(monkeypatch, tmp_path):
    """Two notes on one channel with no ids are two events: neither path may
    collapse them (the old channel-derived push tag did)."""
    first, first_payload = _delivered(
        monkeypatch, tmp_path, {"ts": "1.1", "kind": "k", "channel": "c", "title": "a", "body": "b"}
    )
    second, second_payload = _delivered(
        monkeypatch, tmp_path, {"ts": "1.2", "kind": "k", "channel": "c", "title": "a", "body": "b"}
    )
    assert first["tag"] != second["tag"]
    assert first_payload["notification"]["tag"] != second_payload["notification"]["tag"]
    assert first_payload["notification"]["tag"] == first["tag"]
    assert second_payload["notification"]["tag"] == second["tag"]


def test_oversized_event_id_tag_fits_the_push_budget_unchanged(monkeypatch, tmp_path):
    """A tag over the push byte budget would be truncated on the push path only,
    which splits the two banners again; the builder hashes it to fit instead."""
    from kiro_crew.dashboard.notification_coordinator import NATIVE_TAG_MAX_BYTES

    tab_note, payload = _delivered(
        monkeypatch,
        tmp_path,
        {"ts": "1.3", "kind": "k", "channel": "c", "task_id": "t" * 500, "title": "a", "body": "b"},
    )
    assert len(tab_note["tag"].encode("utf-8")) <= NATIVE_TAG_MAX_BYTES
    assert payload["notification"]["tag"] == tab_note["tag"]


def test_escaped_event_id_tag_fits_the_push_budget_unchanged(monkeypatch, tmp_path):
    """A tag short in raw bytes but long once JSON escapes it (control
    characters escape to 6 bytes each) is hashed at stamping time, so the push
    payload carries the stamped tag unchanged and stays within its budget."""
    from kiro_crew.dashboard.notification_coordinator import NATIVE_TAG_MAX_BYTES

    tab_note, payload = _delivered(
        monkeypatch,
        tmp_path,
        {"ts": "1.4", "kind": "k", "channel": "c", "task_id": "\x01" * 120, "title": "a"},
    )
    serialized = len(json.dumps(tab_note["tag"], ensure_ascii=False).encode("utf-8")) - 2
    assert serialized <= NATIVE_TAG_MAX_BYTES
    assert payload["notification"]["tag"] == tab_note["tag"]


def test_lone_surrogate_event_id_still_delivers(monkeypatch, tmp_path):
    """An API caller can escape a lone surrogate (``"\\ud800"``) into a note's
    metadata; JSON accepts it, UTF-8 does not. Stamping the tag must not raise
    and abort the local delivery: the id is hashed into an encodable tag, and
    the push payload carries that same tag."""
    tab_note, payload = _delivered(
        monkeypatch,
        tmp_path,
        {"ts": "1.5", "kind": "k", "channel": "c", "job_id": "\ud800", "title": "a", "body": "b"},
    )
    assert tab_note["tag"].startswith("job_id#")
    tab_note["tag"].encode("utf-8")
    assert payload["notification"]["tag"] == tab_note["tag"]


def test_lone_surrogate_text_encodes_into_the_push_body():
    """A lone surrogate in a note's title reaches the push encoder as ``?``
    instead of failing that device's push."""
    from kiro_crew.dashboard.state_parts.dashboard_services import _web_push_payload

    payload = _web_push_payload({"ts": "t", "title": "a\ud800b", "body": "b"})
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace")
    assert b'"title":"a?b"' in raw


@pytest.mark.parametrize("splitter", ["\u200b", "**"])
def test_flattening_never_reassembles_a_credential_into_the_push_body(splitter):
    """The note's redaction ran on the raw body, where a zero-width character or
    emphasis markup splits a token past the credential pattern. Flattening for
    the banner removes the splitter and joins the token, so the flattened text
    must be redacted again before it is sent."""
    from kiro_crew.dashboard.state_parts.dashboard_services import _web_push_payload
    from kiro_crew.dashboard.state_parts.notification_log import _redact_note_value

    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    split = token[:10] + splitter + token[10:] + splitter
    raw = f"build done {split} ok"
    assert token not in _redact_note_value(raw)  # the delivery-time pass misses it
    body = _web_push_payload({"ts": "t", "title": "x", "body": _redact_note_value(raw)})[
        "notification"
    ]["body"]
    assert token not in body
    assert "[REDACTED" in body


def test_web_push_payload_body_is_plain_text():
    """An OS banner shows text literally, so the markdown body is flattened the
    way the tab banner's stripMd does; with the shared tag the push banner
    replaces the tab one and must read the same."""
    from kiro_crew.dashboard.state_parts.dashboard_services import _web_push_payload

    n = _web_push_payload(
        {"ts": "t", "title": "Skill", "body": "**deploy** \u2014 see [docs](https://x.example)"}
    )["notification"]
    assert n["body"] == "deploy \u2014 see docs"
    assert _web_push_payload({"ts": "t", "title": "x", "body": None})["notification"]["body"] == ""


@pytest.mark.asyncio
async def test_fan_out_from_a_worker_thread_runs_on_the_serving_loop(monkeypatch, tmp_path):
    """A note raised off the loop (``asyncio.to_thread(state.notify, ...)``) is
    still pushed: the send is handed to the serving loop, not dropped."""
    import asyncio

    state = _make_state(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    state.bind_serving_loop(loop)
    sent: list[dict] = []
    done = asyncio.Event()

    async def _record(note):
        assert asyncio.get_running_loop() is loop
        sent.append(note)
        done.set()

    monkeypatch.setattr(state, "_send_web_push_all", _record)
    await asyncio.to_thread(state._fan_out_web_push, {"title": "t"})
    await await_until(done, what="the push send on the serving loop")
    assert sent == [{"title": "t"}]


def test_fan_out_without_any_loop_is_a_no_op(monkeypatch, tmp_path):
    """A state that has never seen a loop skips the push instead of raising."""
    state = _make_state(monkeypatch, tmp_path)
    called: list[dict] = []

    async def _record(note):
        called.append(note)

    monkeypatch.setattr(state, "_send_web_push_all", _record)
    state._fan_out_web_push({"title": "t"})
    assert called == []


@pytest.mark.asyncio
async def test_fan_out_logs_a_failed_sender_import(monkeypatch, tmp_path, caplog):
    """The fan-out is a fire and forget task: a sender module that fails to
    import is logged and the task ends cleanly, never an unretrieved error."""
    import sys

    state = _make_state(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "kiro_crew.notifications.web_push", None)
    with caplog.at_level("WARNING"):
        await state._send_web_push_all({"title": "t", "body": "b", "channel": "c"})
    assert "Web push fan-out failed" in caplog.text


def test_web_push_imports_without_resolve_result(monkeypatch):
    """setup.cfg allows aiohttp 3.9, which has no ``aiohttp.abc.ResolveResult``;
    the sender module must still import there."""
    import importlib.util

    import aiohttp.abc

    monkeypatch.delattr(aiohttp.abc, "ResolveResult", raising=False)
    spec = importlib.util.find_spec("kiro_crew.notifications.web_push")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "PublicAddressResolver")


@pytest.mark.asyncio
async def test_fan_out_task_survives_a_gc_pass(monkeypatch, tmp_path):
    """The scheduled push task is held until it finishes.

    The loop keeps only a weak reference to a task. A send parked on a future
    nothing else references forms an unreachable cycle with its task, so a
    ``gc.collect()`` destroys the task mid-run unless the state holds it.
    """
    import asyncio
    import gc

    state = _make_state(monkeypatch, tmp_path)
    state.bind_serving_loop(asyncio.get_running_loop())

    async def _parked(note):
        await asyncio.get_running_loop().create_future()

    monkeypatch.setattr(state, "_send_web_push_all", _parked)
    state._fan_out_web_push({"title": "t"})
    await asyncio.sleep(0)  # let the task start and park on its future
    gc.collect()

    parked = [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_parked"]
    assert len(parked) == 1, "the push task was garbage-collected mid-run"
    assert parked[0] in state._background_tasks
    parked[0].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await parked[0]
    assert parked[0] not in state._background_tasks
