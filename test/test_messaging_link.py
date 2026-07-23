"""Tests for the two-level DM session-key builder in ``messaging.link``.

Covers the canonical channel-first shape, generation rotation, dmScope
isolation vs unification, safe fallback on an unknown scope, and the reserved
``group`` chat-type slot.
"""

from __future__ import annotations

import time

from kiro_crew.messaging.link import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    DEFAULT_DM_SCOPE,
    DM_SCOPE_PER_CHANNEL_PEER,
    DM_SCOPE_UNIFIED,
    build_dm_session_key,
    should_rotate_generation,
)


class TestBuildDmSessionKey:
    def test_per_channel_peer_is_channel_first(self) -> None:
        assert (
            build_dm_session_key("telegram", "kirocrew", "123")
            == "telegram:kirocrew:direct:123"
        )

    def test_default_scope_is_per_channel_peer(self) -> None:
        assert DEFAULT_DM_SCOPE == DM_SCOPE_PER_CHANNEL_PEER
        assert build_dm_session_key(
            "telegram", "kirocrew", "123"
        ) == build_dm_session_key(
            "telegram", "kirocrew", "123", dm_scope=DM_SCOPE_PER_CHANNEL_PEER
        )

    def test_generation_zero_is_bare_bucket(self) -> None:
        assert build_dm_session_key("telegram", "a", "1", gen=0) == "telegram:a:direct:1"

    def test_generation_rotates_and_keeps_bucket_prefix(self) -> None:
        bucket = build_dm_session_key("telegram", "a", "1")
        g2 = build_dm_session_key("telegram", "a", "1", gen=2)
        assert g2 == "telegram:a:direct:1:gen2"
        assert g2.startswith(bucket)

    def test_per_channel_peer_isolates_channels(self) -> None:
        assert build_dm_session_key("telegram", "a", "1") != build_dm_session_key(
            "wecom", "a", "1"
        )

    def test_unified_collapses_channel_and_user(self) -> None:
        a = build_dm_session_key("telegram", "kirocrew", "1", dm_scope=DM_SCOPE_UNIFIED)
        b = build_dm_session_key("wecom", "kirocrew", "999", dm_scope=DM_SCOPE_UNIFIED)
        assert a == b == "unified:kirocrew"

    def test_unified_with_generation(self) -> None:
        assert (
            build_dm_session_key("telegram", "a", "1", gen=3, dm_scope=DM_SCOPE_UNIFIED)
            == "unified:a:gen3"
        )

    def test_unknown_scope_falls_back_to_per_channel_peer(self) -> None:
        assert build_dm_session_key(
            "telegram", "a", "1", dm_scope="bogus"
        ) == build_dm_session_key("telegram", "a", "1")

    def test_group_chat_type_uses_reserved_slot(self) -> None:
        assert (
            build_dm_session_key("telegram", "a", "1", chat_type="group")
            == "telegram:a:group:1"
        )

    def test_unified_scopes_only_direct_dms_not_forum(self) -> None:
        # SECURITY (issue #211, PR #219 Codex HIGH): dm_scope=unified must NOT
        # collapse a forum Topic into the shared DM bucket — that would leak
        # private DM content into a group Topic (and vice versa). A forum route
        # keeps its FULL bucket regardless of dm_scope.
        key = build_dm_session_key(
            "telegram",
            "kirocrew",
            "-1001234567890:5",
            dm_scope=DM_SCOPE_UNIFIED,
            chat_type=CHAT_TYPE_FORUM,
        )
        assert key == "telegram:kirocrew:forum:-1001234567890:5"
        assert not key.startswith(f"{DM_SCOPE_UNIFIED}:")

    def test_unified_still_collapses_direct_dm(self) -> None:
        # Regression: a direct (1:1) DM under unified still collapses to the
        # single shared bucket (cross-surface continuity preserved).
        assert (
            build_dm_session_key(
                "telegram",
                "kirocrew",
                "123",
                dm_scope=DM_SCOPE_UNIFIED,
                chat_type=CHAT_TYPE_DIRECT,
            )
            == "unified:kirocrew"
        )


def _local_epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    """Epoch seconds for a local wall-clock time (DST auto-resolved)."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


class TestShouldRotateGeneration:
    def test_first_message_never_rotates(self) -> None:
        assert should_rotate_generation(0.0, 1_000_000.0, idle_minutes=30) is False

    def test_idle_fires_at_threshold(self) -> None:
        assert should_rotate_generation(1000.0, 1000.0 + 30 * 60, idle_minutes=30) is True

    def test_idle_not_reached(self) -> None:
        assert should_rotate_generation(1000.0, 1000.0 + 29 * 60, idle_minutes=30) is False

    def test_idle_disabled(self) -> None:
        assert should_rotate_generation(1000.0, 1000.0 + 10_000, idle_minutes=0) is False

    def test_daily_boundary_crossed(self) -> None:
        last = _local_epoch(2026, 7, 9, 3, 0)
        now = _local_epoch(2026, 7, 10, 5, 0)
        assert should_rotate_generation(last, now, daily_reset_hour=4) is True

    def test_daily_same_day_after_boundary_no_rotate(self) -> None:
        last = _local_epoch(2026, 7, 10, 5, 0)
        now = _local_epoch(2026, 7, 10, 7, 0)
        assert should_rotate_generation(last, now, daily_reset_hour=4) is False

    def test_daily_disabled(self) -> None:
        last = _local_epoch(2026, 7, 9, 3, 0)
        now = _local_epoch(2026, 7, 10, 5, 0)
        assert should_rotate_generation(last, now, daily_reset_hour=-1) is False
