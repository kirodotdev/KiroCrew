"""The "Reduce memory use" button on the tight and critical host-memory notes.

The hint reads two switches from a config object built here, and the notifier is
driven through its pure ``observe`` state machine against a real bus, so nothing
reads the runner's own config or memory.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import edition_locks
from kiro_crew.acp.chat_runtime_sharing import CHAT_RUNTIME_SHARING_PREVIEW
from kiro_crew.notifications import memory_sharing_hint as hint_mod
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.notifications.memory_sharing_hint import (
    ACP_SHARING_URL,
    ACTION_LABEL,
    MCP_SHARING_URL,
    SharingHint,
    sharing_hint,
)
from kiro_crew.notifications.resource_pressure import SUSTAINED_TIGHT_SECS, ResourcePressureNotifier
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    ResourceStatus,
)


def _cfg(*, mcp: bool, acp: bool) -> Any:
    return SimpleNamespace(
        mcp_gateway=SimpleNamespace(enabled=mcp),
        dashboard=SimpleNamespace(feature_previews={CHAT_RUNTIME_SHARING_PREVIEW: acp}),
    )


def _status(posture: str, gb: float = 1.5) -> ResourceStatus:
    return ResourceStatus(
        available_gb=gb,
        cpu_count=8,
        load_per_cpu=0.5,
        posture=posture,
        pressure_gb=4.0,
        critical_gb=2.0,
    )


@pytest.fixture
def unlocked(monkeypatch):
    """Feature Previews can be switched on in this build (not LTS)."""
    monkeypatch.setattr(edition_locks, "local_enable_locked", lambda _control: False)


@pytest.fixture
def mcp_can(monkeypatch):
    """``state["can"]`` is whether the MCP page's sharing switch can be turned on."""
    state = {"can": True}
    monkeypatch.setattr(hint_mod, "_mcp_sharing_can_turn_on", lambda: state["can"])
    return state


class TestWhichSwitchIsOffered:
    def test_mcp_sharing_off_is_offered_first(self, unlocked, mcp_can) -> None:
        assert sharing_hint(_cfg(mcp=False, acp=False)) == SharingHint(MCP_SHARING_URL)

    def test_an_mcp_switch_that_cannot_turn_on_falls_through_to_acp(
        self, unlocked, mcp_can
    ) -> None:
        mcp_can["can"] = False
        assert sharing_hint(_cfg(mcp=False, acp=False)) == SharingHint(ACP_SHARING_URL)

    def test_no_button_when_mcp_cannot_turn_on_and_acp_is_on(self, unlocked, mcp_can) -> None:
        mcp_can["can"] = False
        assert sharing_hint(_cfg(mcp=False, acp=True)) is None

    def test_acp_sharing_is_offered_once_mcp_sharing_is_on(self, unlocked, mcp_can) -> None:
        assert sharing_hint(_cfg(mcp=True, acp=False)) == SharingHint(ACP_SHARING_URL)

    def test_nothing_is_offered_when_both_are_on(self, unlocked, mcp_can) -> None:
        assert sharing_hint(_cfg(mcp=True, acp=True)) is None

    def test_a_build_that_locks_previews_never_offers_acp_sharing(
        self, monkeypatch, mcp_can
    ) -> None:
        monkeypatch.setattr(
            edition_locks, "local_enable_locked", lambda control: control == "feature_previews"
        )
        assert sharing_hint(_cfg(mcp=True, acp=False)) is None

    def test_an_unreadable_config_offers_nothing(self, monkeypatch) -> None:
        def _broken() -> Any:
            raise OSError("config unreadable")

        monkeypatch.setattr(hint_mod, "_load_config", _broken)
        assert sharing_hint() is None


class TestTheNotesCarryIt:
    @pytest.fixture
    def notes(self) -> list[dict[str, Any]]:
        return []

    def _notifier(self, notes: list[dict[str, Any]], hint: SharingHint | None):
        notifier = ResourcePressureNotifier(
            NotificationBus(sink=notes.append), sharing_hint_fn=lambda: hint
        )
        notifier.refresh_sharing_hint()
        return notifier

    def test_the_critical_note_carries_the_button(self, notes) -> None:
        notifier = self._notifier(notes, SharingHint(MCP_SHARING_URL))
        notifier.observe(_status(POSTURE_CRITICAL, gb=1.2), now=0.0)
        (note,) = notes
        assert note["actions"] == [
            {"id": "reduce_memory", "label": ACTION_LABEL, "url": MCP_SHARING_URL}
        ]
        assert "\n" not in note["body"], "no extra line for the banner to cut off"

    def test_the_sustained_tight_note_carries_it_too(self, notes) -> None:
        notifier = self._notifier(notes, SharingHint(ACP_SHARING_URL))
        notifier.observe(_status(POSTURE_TIGHT, gb=3.5), now=0.0)
        notifier.observe(_status(POSTURE_TIGHT, gb=3.5), now=SUSTAINED_TIGHT_SECS + 1)
        (note,) = notes
        assert note["actions"][0]["url"] == ACP_SHARING_URL
        assert "\n" not in note["body"]

    def test_no_button_while_both_switches_are_on(self, notes) -> None:
        notifier = self._notifier(notes, None)
        notifier.observe(_status(POSTURE_CRITICAL, gb=1.2), now=0.0)
        (note,) = notes
        assert "actions" not in note or not note["actions"]
        assert "sharing" not in note["body"]

    def test_the_recovery_note_never_carries_it(self, notes) -> None:
        notifier = self._notifier(notes, SharingHint(MCP_SHARING_URL))
        notifier.observe(_status(POSTURE_CRITICAL, gb=1.2), now=0.0)
        notifier.observe(_status(POSTURE_AMPLE, gb=12.0), now=30.0)
        recovery = notes[-1]
        assert "actions" not in recovery or not recovery["actions"]

    def test_a_failing_hint_still_sends_the_note(self, notes) -> None:
        def _boom() -> SharingHint | None:
            raise RuntimeError("hint broke")

        notifier = ResourcePressureNotifier(
            NotificationBus(sink=notes.append), sharing_hint_fn=_boom
        )
        notifier.refresh_sharing_hint()
        notifier.observe(_status(POSTURE_CRITICAL, gb=1.2), now=0.0)
        assert len(notes) == 1

    @pytest.mark.asyncio
    async def test_a_pressured_sample_reads_the_hint_off_the_loop(self, notes) -> None:
        reads: list[int] = []

        def _hint() -> SharingHint | None:
            reads.append(1)
            return SharingHint(MCP_SHARING_URL)

        clock = [0.0]
        notifier = ResourcePressureNotifier(
            NotificationBus(sink=notes.append),
            probe_fn=lambda: _status(POSTURE_CRITICAL, gb=1.2),
            clock=lambda: clock[0],
            sharing_hint_fn=_hint,
        )
        await notifier.maybe_sample()
        assert reads == [1]
        assert notes and notes[0]["actions"][0]["url"] == MCP_SHARING_URL
        clock[0] = 10.0
        await notifier.maybe_sample()
        assert reads == [1], "re-read at most once a minute"

    @pytest.mark.asyncio
    async def test_a_stalled_hint_read_is_never_started_twice(self, notes) -> None:
        reads: list[int] = []
        release = threading.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _hint() -> SharingHint | None:
            reads.append(1)
            release.wait(30)
            return SharingHint(MCP_SHARING_URL)

        clock = [0.0]
        notifier = ResourcePressureNotifier(
            NotificationBus(sink=notes.append),
            probe_fn=lambda: _status(POSTURE_CRITICAL, gb=1.2),
            clock=lambda: clock[0],
            sharing_hint_fn=_hint,
        )
        refresh = notifier.refresh_sharing_hint

        def _refresh() -> None:
            try:
                refresh()
            finally:
                loop.call_soon_threadsafe(finished.set)

        notifier.refresh_sharing_hint = _refresh  # type: ignore[method-assign]
        try:
            await notifier.maybe_sample()  # the read stalls past the timeout
            assert reads == [1]
            assert notifier._read_sharing_hint() is None
            clock[0] = 61.0  # past both the sample and the hint refresh intervals
            await notifier.maybe_sample()
            assert reads == [1], "a second read started beside the stalled one"
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 30)
        assert notifier._read_sharing_hint() == SharingHint(MCP_SHARING_URL)


class TestMcpSharingCanTurnOn:
    """``mcp_sharing_can_turn_on`` mirrors the MCP page's ``canEnableSharing``."""

    @pytest.fixture
    def host(self, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as routes

        state: dict[str, Any] = {
            "supported": True,
            "stub": [],
            "overrides": {},
            "cwd_use": {},
            "refused": {},
            "rows": {},
        }
        gw = SimpleNamespace(
            stub_servers=state["stub"],
            stub_overrides=state["overrides"],
            cwd_use=state["cwd_use"],
            cwd_use_launch={},
        )
        monkeypatch.setattr(routes, "is_gateway_supported", lambda: state["supported"])
        monkeypatch.setattr(
            routes.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace(mcp_gateway=gw))
        )
        monkeypatch.setattr(routes, "refused_servers", lambda: state["refused"])
        monkeypatch.setattr(routes, "_collect_server_rows", lambda: state["rows"])
        state["check"] = routes.mcp_sharing_can_turn_on
        return state

    @staticmethod
    def _row(transport: str = "stdio") -> dict[str, Any]:
        return {"transport": transport, "launch_ids": []}

    def test_a_server_marked_to_share_lets_it_turn_on(self, host) -> None:
        host["stub"].append("github")
        assert host["check"]() is True

    def test_a_crew_server_is_eligible(self, host) -> None:
        host["rows"]["kirocrew-core"] = self._row()
        assert host["check"]() is True

    def test_a_server_answered_not_used_is_eligible(self, host) -> None:
        host["rows"]["github"] = self._row()
        host["cwd_use"]["github"] = "not_used"
        assert host["check"]() is True

    def test_no_eligible_server_means_it_cannot_turn_on(self, host) -> None:
        host["rows"]["unanswered"] = self._row()
        host["rows"]["remote"] = self._row("http")
        host["rows"]["creds-agent"] = self._row()
        host["rows"]["kirocrew-cron"] = self._row()
        host["overrides"]["kirocrew-cron"] = False
        host["rows"]["kirocrew-core"] = self._row()
        host["refused"]["kirocrew-core"] = {"reason": "added_outside_dashboard"}
        assert host["check"]() is False

    def test_an_unsupported_host_cannot_turn_it_on(self, host) -> None:
        host["stub"].append("github")
        host["supported"] = False
        assert host["check"]() is False

    def test_an_unreadable_reading_answers_false(self, host, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import mcp as routes

        def _boom() -> dict:
            raise OSError("specs unreadable")

        monkeypatch.setattr(routes, "_collect_server_rows", _boom)
        assert host["check"]() is False
