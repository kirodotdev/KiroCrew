"""Tests for v1c -- namespaced ChannelLink + bare->slack: key migration.

Hard gate: an existing Slack thread must resume the SAME ``sid`` after
migrate + restart, on BOTH the native lookup and the challenge-redirect
(reverse-index) path.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.messaging.link import (
    ChannelLink,
    canonical_key,
    is_legacy_slack_key,
    legacy_key,
    session_key,
)
from kiro_crew.session_map import SessionMap


def _write_map(tmp_path, data: dict) -> None:
    (tmp_path / "session_map.json").write_text(json.dumps(data), encoding="utf-8")


def _make_kiro_session(kiro_dir, sid: str) -> None:
    kiro_dir.mkdir(parents=True, exist_ok=True)
    (kiro_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
    (kiro_dir / f"{sid}.jsonl").write_text('{"x":1}\n{"y":2}\n', encoding="utf-8")


@pytest.fixture()
def patched(tmp_path, monkeypatch):
    kiro = tmp_path / "kiro"
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", kiro)
    return tmp_path, kiro


class TestLinkHelpers:
    def test_session_key(self):
        assert session_key("slack", "123.456") == "slack:123.456"

    def test_is_legacy(self):
        assert is_legacy_slack_key("1718000000.123456") is True
        assert is_legacy_slack_key("dashboard:x") is False

    def test_canonical(self):
        assert canonical_key("123.456") == "slack:123.456"
        assert canonical_key("slack:123.456") == "slack:123.456"
        assert canonical_key("dashboard:x") == "dashboard:x"

    def test_legacy(self):
        assert legacy_key("slack:123.456") == "123.456"
        assert legacy_key("dashboard:x") is None

    def test_channel_link_round_trip(self):
        link = ChannelLink(channel_type="slack", channel_id="C1", thread_id="123.456")
        assert ChannelLink.from_dict(link.to_dict()) == link


class TestBareKeyMigration:
    def test_string_form_migrates(self, patched):
        tmp_path, kiro = patched
        _make_kiro_session(kiro, "sid-abc")
        _write_map(tmp_path, {"1718000000.123456": "sid-abc"})
        sm = SessionMap()
        assert "slack:1718000000.123456" in sm._data
        assert "1718000000.123456" not in sm._data
        assert sm.get("1718000000.123456") == "sid-abc"
        assert sm.get("slack:1718000000.123456") == "sid-abc"
        assert sm.get_session_for_thread("1718000000.123456") == "slack:1718000000.123456"
        link = sm.get_link("slack:1718000000.123456")
        assert link is not None and link.thread_id == "1718000000.123456"

    def test_dashboard_key_untouched(self, patched):
        tmp_path, kiro = patched
        _make_kiro_session(kiro, "sid-d")
        _write_map(tmp_path, {"dashboard:chat-1-x": {"sid": "sid-d", "slack_thread_ts": "111.222", "slack_channel_id": "C9"}})
        sm = SessionMap()
        assert "dashboard:chat-1-x" in sm._data
        ts, ch = sm.get_slack_link("dashboard:chat-1-x")
        assert ts == "111.222" and ch == "C9"
        assert sm.get_link("dashboard:chat-1-x") is None

    def test_early_discord_resume_slack_stamp_is_scrubbed(self, patched):
        tmp_path, _ = patched
        key = "dashboard:chat-1-x"
        _write_map(
            tmp_path,
            {
                key: {
                    "sid": "sid-d",
                    "slack_thread_ts": "",
                    "slack_channel_id": "discord:356163505868767244",
                }
            },
        )

        sm = SessionMap()

        assert sm.get_slack_link(key) == (None, None)
        assert sm.get_mirror_link(key) is None
        persisted = json.loads((tmp_path / "session_map.json").read_text(encoding="utf-8"))
        assert "slack_thread_ts" not in persisted[key]
        assert "slack_channel_id" not in persisted[key]
        assert persisted[key]["sid"] == "sid-d"

    def test_collision_prefers_sid(self, patched):
        tmp_path, kiro = patched
        _make_kiro_session(kiro, "keep")
        _write_map(tmp_path, {
            "1718000000.123456": {"sid": "", "slack_thread_ts": "1718000000.123456", "slack_channel_id": None},
            "slack:1718000000.123456": {"sid": "keep", "slack_thread_ts": "1718000000.123456", "slack_channel_id": None},
        })
        sm = SessionMap()
        assert sm.get("slack:1718000000.123456") == "keep"


class TestWritesCanonical:
    def test_set_canonicalizes(self, patched):
        tmp_path, kiro = patched
        _make_kiro_session(kiro, "sid2")
        sm = SessionMap()
        sm.set("1718000000.123456", "sid2")
        assert "slack:1718000000.123456" in sm._data
        assert sm.get("1718000000.123456") == "sid2"

    def test_set_slack_link_keeps_raw_thread(self, patched):
        tmp_path, kiro = patched
        sm = SessionMap()
        sm.set_slack_link("1718000000.123456", "1718000000.123456", "C1")
        assert "slack:1718000000.123456" in sm._data
        ts, ch = sm.get_slack_link("1718000000.123456")
        assert ts == "1718000000.123456" and ch == "C1"
        assert sm.get_session_for_thread("1718000000.123456") == "slack:1718000000.123456"


class TestResumeHardGate:
    def test_resume_both_paths(self, patched):
        tmp_path, kiro = patched
        _make_kiro_session(kiro, "sid-live")
        _write_map(tmp_path, {"1718000000.123456": "sid-live"})
        SessionMap()           # migrate + save
        sm2 = SessionMap()     # restart
        # native lookup
        assert sm2.get("1718000000.123456") == "sid-live"
        assert sm2.get("slack:1718000000.123456") == "sid-live"
        # challenge-redirect path (reverse index on raw thread_ts)
        resolved = sm2.get_session_for_thread("1718000000.123456")
        assert resolved == "slack:1718000000.123456"
        assert sm2.get(resolved) == "sid-live"

    def test_forward_only_no_sid_deletion(self, patched):
        tmp_path, kiro = patched
        _make_kiro_session(kiro, "sid-live")
        _write_map(tmp_path, {"1718000000.123456": "sid-live"})
        SessionMap()
        assert (kiro / "sid-live.json").exists()


class TestMigrationCollision:
    """A bare key and its namespaced form canonicalize to the same key. The
    migration must never clobber a live sid, deterministically regardless of
    dict iteration order."""

    def test_live_sid_survives_regardless_of_order(self, patched):
        tmp_path, _ = patched
        ts = "1718000000.123456"
        canon = canonical_key(ts)
        # Live sid on the namespaced key, no sid on the bare key — both orders.
        for data in (
            {f"slack:{ts}": {"sid": "LIVE"}, ts: {"sid": None}},   # live first
            {ts: {"sid": None}, f"slack:{ts}": {"sid": "LIVE"}},   # live second
        ):
            _write_map(tmp_path, data)
            sm = SessionMap()
            assert sm._data[canon]["sid"] == "LIVE", data

    def test_both_live_first_seen_wins_deterministic(self, patched):
        tmp_path, _ = patched
        ts = "1718000000.123456"
        canon = canonical_key(ts)
        # Genuine two-live collision: the first-seen entry wins and is never
        # silently clobbered by the later one (order-independent contract).
        _write_map(tmp_path, {f"slack:{ts}": {"sid": "A"}, ts: {"sid": "B"}})
        sm = SessionMap()
        assert sm._data[canon]["sid"] == "A"
