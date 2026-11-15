"""A dashboard Stop pressed during the reset-to-reclaim replay gap must not read
as idle.

While ``spec_hooks_reproject.reproject_unattended_claim`` (or a compaction
replay) holds a key across its reset, the live session is popped, so
``SessionManager.is_busy`` reads False. Without the fix the dashboard Stop path
(`stop_slot_turn` -> `_ChatSlot.linked_turn_running` -> the linked-session probe)
returns "not running" and never records the Stop, so the re-claimed cron or
channel turn runs anyway. The probe now reads a key held in a replay gap as
running, so the Stop routes through ``stop_turn`` (which records it via
``note_stop``) / cron cancellation instead. This also closes the pre-existing
compaction-replay Stop gap, which opens the same replay gap.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
from kiro_crew.dashboard.state_parts.dashboard_services import linked_session_busy

_CHANNEL_KEY = "slack:1700000000.000100"
_CRON_KEY = "cron:job-1"


class _Sessions:
    """The two probes ``linked_session_busy`` reads, as sets."""

    def __init__(self, *, busy: set[str] | None = None, gap: set[str] | None = None) -> None:
        self._busy = busy or set()
        self._gap = gap or set()

    def is_busy(self, key: str) -> bool:
        return key in self._busy

    def has_open_replay_gap(self, key: str) -> bool:
        return key in self._gap


class _State:
    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions


class TestLinkedSessionBusy:
    def test_a_key_in_a_replay_gap_reads_busy_even_with_no_live_session(self) -> None:
        # The gap case for a channel key and a cron key: is_busy is False (session
        # popped) but the key is mid-reprojection.
        assert linked_session_busy(_State(_Sessions(gap={_CHANNEL_KEY})), _CHANNEL_KEY) is True
        assert linked_session_busy(_State(_Sessions(gap={_CRON_KEY})), _CRON_KEY) is True

    def test_a_live_lease_still_reads_busy(self) -> None:
        assert linked_session_busy(_State(_Sessions(busy={_CHANNEL_KEY})), _CHANNEL_KEY) is True

    def test_neither_busy_nor_gapped_reads_idle(self) -> None:
        assert linked_session_busy(_State(_Sessions()), _CHANNEL_KEY) is False

    def test_a_manager_without_the_gap_probe_falls_back_to_is_busy(self) -> None:
        class _Old:
            def is_busy(self, key: str) -> bool:
                return False

        assert linked_session_busy(_State(_Old()), _CHANNEL_KEY) is False


def _slot(linked_key: str, gap: set[str]) -> _ChatSlot:
    slot = _ChatSlot("tab-1")
    slot.linked_session_key = linked_key
    slot._linked_session_busy = lambda k, _g=gap: k in _g
    return slot


class TestLinkedTurnRunning:
    def test_a_channel_tab_reads_running_while_its_key_is_in_a_gap(self) -> None:
        assert _slot(_CHANNEL_KEY, {_CHANNEL_KEY}).linked_turn_running is True

    def test_a_cron_tab_reads_running_while_its_key_is_in_a_gap(self) -> None:
        assert _slot(_CRON_KEY, {_CRON_KEY}).linked_turn_running is True

    def test_a_non_channel_non_cron_key_is_never_consulted(self) -> None:
        # A dashboard-born key holds its own lease for runs of its own; the
        # linked-turn probe does not apply, so the gap does not read as running.
        assert _slot("dashboard:s1", {"dashboard:s1"}).linked_turn_running is False
