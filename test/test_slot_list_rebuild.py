"""Partial slot-list labelling during dashboard restart."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard import server as srv
from kiro_crew.dashboard import state as srv_state
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history_projection import (
    METADATA_LINE_CORRUPT,
    METADATA_LINE_READABLE,
    METADATA_LINE_TRANSIENT,
)
from kiro_crew.testing.wait import await_until


@pytest.fixture
def state(_floor_monkeypatch, tmp_path) -> DashboardState:
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _slots_broadcasts(broadcast: MagicMock) -> list[dict]:
    return [
        call.args[0] for call in broadcast.call_args_list if call.args[0].get("_type") == "slots"
    ]


class TestPushes:
    def test_a_push_during_rebuild_carries_the_real_partial_list(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        slots = [{"key": "chat-1-1", "title": "restored"}]
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        _floor_monkeypatch.setattr(
            state, "serialize_slot_views", lambda **_kwargs: (slots, slots, None)
        )
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": ["chat-held"],
            "unknowable": False,
        }
        state.replace_channel_slot_holds(["slack_held"], hold_channels=True)
        state.begin_slot_list_rebuild()
        state.replace_channel_slot_holds(["slack_held"], hold_channels=True)

        state.push_slots_update()

        frames = _slots_broadcasts(broadcast)
        assert len(frames) == 1
        assert frames[0]["slotsPartial"] is True
        assert frames[0]["slotsHeldKeys"] == ["chat-held", "slack_held"]
        assert frames[0]["slotsHoldChannels"] is True
        assert json.loads(frames[0]["slots"]) == slots

    def test_finishing_pushes_the_label_flip_once(self, state, _floor_monkeypatch) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        state.begin_slot_list_rebuild()
        state.replace_channel_slot_holds((), hold_channels=False)
        state.push_slots_update()

        state.finish_slot_list_rebuild()
        state.finish_slot_list_rebuild()

        frames = _slots_broadcasts(broadcast)
        assert [frame["slotsPartial"] for frame in frames] == [True, False]
        assert state.slot_list_rebuilding is False

    def test_an_unknowable_restore_keeps_the_partial_label(self, state) -> None:
        state.unrestored_slot_notice = {
            "count": 0,
            "keys": [],
            "unknowable": True,
        }
        state.begin_slot_list_rebuild()

        state.finish_slot_list_rebuild()

        assert state.slot_list_rebuilding is True
        assert state._complete_slot_list_rebuild_if_ready() is False

    def test_suspended_construction_keeps_a_revived_dropped_tab_partial(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        dropped_key = "chat-2-unreadable"
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        _floor_monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": [dropped_key],
            "unknowable": False,
        }
        state.begin_slot_list_rebuild()
        state.replace_channel_slot_holds((), hold_channels=False)
        state.finish_slot_list_rebuild()

        with state.suspend_slots_push():
            slot = state.get_or_create_slot(dropped_key)
            state.begin_slot_construction(slot.key)

        frames = _slots_broadcasts(broadcast)
        assert state.slot_list_rebuilding is True
        assert [frame["slotsPartial"] for frame in frames] == [True]
        assert dropped_key not in {row["key"] for row in json.loads(frames[0]["slots"])}

        state.end_slot_construction(slot.key)
        state.push_slots_update()

        frames = _slots_broadcasts(broadcast)
        assert state.slot_list_rebuilding is False
        assert [frame["slotsPartial"] for frame in frames] == [True, False]
        assert dropped_key in {row["key"] for row in json.loads(frames[-1]["slots"])}

    def test_a_state_that_never_rebuilds_labels_lists_complete(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)

        state.push_slots_update()

        assert _slots_broadcasts(broadcast)[0]["slotsPartial"] is False


class TestSlotsGet:
    @pytest.mark.asyncio
    async def test_get_remains_a_bare_immediate_array(self, state, _floor_monkeypatch) -> None:
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.chat_handlers import api_chat_slots
        from kiro_crew.dashboard.handlers import source_providers

        async def _no_allowlist() -> None:
            return None

        _floor_monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _no_allowlist)
        _floor_monkeypatch.setattr(
            source_providers, "is_owner_dashboard_request", lambda _request: False
        )
        restored = [{"key": "chat-1-1", "title": "restored"}]
        _floor_monkeypatch.setattr(state, "serialize_slots", lambda **_kwargs: restored)
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": ["chat-held"],
            "unknowable": False,
        }
        state.begin_slot_list_rebuild()
        state.replace_channel_slot_holds(["slack_held"], hold_channels=True)
        request = make_mocked_request("GET", "/api/chat/slots", app={"state": state})
        request["is_dashboard_user"] = True

        response = await await_until(asyncio.ensure_future(api_chat_slots(request)))

        assert json.loads(response.text) == restored
        assert "X-Kiro-Slots-Held-Keys" not in response.headers
        assert "X-Kiro-Slots-Hold-Channels" not in response.headers


class _FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []
        self._flags: dict = {"_is_dashboard_user": True}

    def __setitem__(self, key: str, value) -> None:
        self._flags[key] = value

    def __getitem__(self, key: str):
        return self._flags[key]

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def prepare(self, request) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class TestConnectSnapshot:
    async def _connect(
        self, _floor_monkeypatch, *, rebuilding: bool, dashboard_user: bool = True
    ) -> tuple[list[dict], MagicMock]:
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state._yolo = False
        state.source_link_urls.return_value = []
        state.serialize_slots.return_value = [{"key": "chat-1-1", "title": "restored"}]
        state._folders = []
        state.folders_generation.return_value = 0
        state.slot_list_rebuilding = rebuilding
        state.slot_list_held_keys.return_value = ["chat-held"]
        state.slot_list_hold_channels.return_value = True

        class Request(dict):
            def __init__(self) -> None:
                super().__init__(
                    {
                        "user": "U_OWNER" if dashboard_user else "U_APP",
                        "app": "" if dashboard_user else "test-app",
                        "is_dashboard_user": dashboard_user,
                    }
                )
                self.app = {"state": state}

        async def _no_allowlist() -> frozenset:
            return frozenset()

        fake_ws = _FakeWebSocket()
        _floor_monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        _floor_monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        _floor_monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _no_allowlist)
        _floor_monkeypatch.setattr(
            dashboard_ws,
            "load_declared_events_for_connect",
            lambda _app: (True, frozenset()),
        )
        _floor_monkeypatch.setattr(
            dashboard_ws,
            "filter_slots_for_app",
            lambda slots, *_args: slots,
        )

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]
        return [frame for frame in fake_ws.sent if frame.get("type") == "slots"], state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rebuilding", [True, False])
    async def test_snapshot_carries_the_real_list_and_partial_label(
        self, _floor_monkeypatch, rebuilding
    ) -> None:
        frames, state = await self._connect(_floor_monkeypatch, rebuilding=rebuilding)

        assert [row["key"] for row in frames[0]["data"]] == ["chat-1-1"]
        assert frames[0]["slotsPartial"] is rebuilding
        assert frames[0]["slotsHeldKeys"] == ["chat-held"]
        assert frames[0]["slotsHoldChannels"] is True
        state.serialize_slots.assert_called_once()

    @pytest.mark.asyncio
    async def test_app_token_snapshot_omits_foreign_hold_state(self, _floor_monkeypatch) -> None:
        frames, state = await self._connect(
            _floor_monkeypatch, rebuilding=False, dashboard_user=False
        )

        assert [row["key"] for row in frames[0]["data"]] == ["chat-1-1"]
        assert "slotsHeldKeys" not in frames[0]
        assert "slotsHoldChannels" not in frames[0]
        state.slot_list_held_keys.assert_not_called()
        state.slot_list_hold_channels.assert_not_called()


def _restore_cfg(*, surface_channel_sessions: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        dashboard=SimpleNamespace(
            restore_window_minutes=30,
            restore_sessions=True,
            idle_slot_sweep_days=0,
            surface_channel_sessions=surface_channel_sessions,
        )
    )


class TestBootRestore:
    @pytest.fixture(autouse=True)
    def _quiet_restore(self, _floor_monkeypatch) -> None:
        _floor_monkeypatch.setattr(srv.cautious_boot, "pause_before", AsyncMock(return_value=None))
        _floor_monkeypatch.setattr(srv.chat, "restore_open_slots_async", AsyncMock(return_value=0))
        _floor_monkeypatch.setattr(
            srv.chat, "restore_recent_sessions_async", AsyncMock(return_value=0)
        )
        _floor_monkeypatch.setattr(channel_slots, "RECONCILE_INTERVAL_SECS", 3600)

    @staticmethod
    async def _stop_reconciler(state) -> None:
        task = state._channel_slot_reconciler
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    @pytest.mark.asyncio
    async def test_the_boot_path_does_not_wait_for_the_channel_pass(
        self, state, _floor_monkeypatch
    ) -> None:
        never = asyncio.Event()

        async def _reconcile(_state, _window) -> bool:
            await never.wait()
            return False

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.begin_slot_list_rebuild()
        try:
            await await_until(
                asyncio.ensure_future(srv._restore_dashboard_sessions(state, _restore_cfg()))
            )

            assert state.slot_list_rebuilding is True
            assert state.slot_list_hold_channels() is True
            assert not state._channel_slot_reconciler.done()
        finally:
            await self._stop_reconciler(state)

    @pytest.mark.asyncio
    async def test_first_complete_channel_pass_releases_the_startup_hold(self, state) -> None:
        state.conversation_log = SimpleNamespace(list_sessions_status=lambda: ([], True))
        state.begin_slot_list_rebuild()

        assert state.slot_list_hold_channels() is True

        surfaced, complete = await channel_slots._reconcile_with_status(state, 30)

        assert surfaced == 0
        assert complete is True
        assert state.slot_list_hold_channels() is False

    @pytest.mark.asyncio
    async def test_first_complete_pass_flips_the_label_and_pushes(
        self, state, _floor_monkeypatch
    ) -> None:
        completed = asyncio.Event()
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)

        async def _reconcile(_state, _window) -> bool:
            _state.replace_channel_slot_holds((), hold_channels=False)
            completed.set()
            return True

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.begin_slot_list_rebuild()
        try:
            await srv._restore_dashboard_sessions(state, _restore_cfg())
            await await_until(completed)
            for _ in range(5):
                await asyncio.sleep(0)

            assert state.slot_list_rebuilding is False
            assert _slots_broadcasts(broadcast)[-1]["slotsPartial"] is False
        finally:
            await self._stop_reconciler(state)

    @pytest.mark.asyncio
    async def test_complete_channel_pass_keeps_partial_while_an_open_tab_is_unrestored(
        self, state, _floor_monkeypatch
    ) -> None:
        completed = asyncio.Event()

        async def _reconcile(_state, _window) -> bool:
            _state.replace_channel_slot_holds((), hold_channels=False)
            completed.set()
            return True

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.unrestored_slot_keys = {"chat-2-unreadable"}
        state.begin_slot_list_rebuild()
        try:
            await srv._restore_dashboard_sessions(state, _restore_cfg())
            await await_until(completed)
            for _ in range(5):
                await asyncio.sleep(0)

            assert state.slot_list_rebuilding is True
        finally:
            await self._stop_reconciler(state)

    @pytest.mark.asyncio
    async def test_complete_channel_pass_clears_partial_on_a_remote_authority_boot(
        self, tmp_path, _floor_monkeypatch
    ) -> None:
        """The routine container boot: a remote-only reopen seed must not hold the label.

        Under ``KIROCREW_CONTAINER_AUTHORITY_RESTORED=1`` a listed tab whose transcript
        is not on local disk stays in ``unrestored_slot_keys`` on EVERY boot, by design
        (its transcript is fetched lazily, if ever). A finish that waited on that set
        kept ``slotsPartial`` for the process lifetime, so the dashboard never drained
        a deleted session's unread dot. The seed and the notice both come from the
        real restore here, so this proves the condition against what a boot produces.
        """
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat_utils import _history_key_for

        _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _floor_monkeypatch.setenv(chat_persistence.ENV_AUTHORITY_RESTORED, "1")
        seeded = _make_state(tmp_path / "sessions")
        seeded.conversation_log.append(_history_key_for("chat-1-local"), "user", "hello")
        (tmp_path / "open_slots.json").write_text(
            json.dumps({"keys": ["chat-1-local", "chat-9-remote"], "ts": 0.0})
        )
        booted = _make_state(tmp_path / "sessions")
        booted.begin_slot_list_rebuild()
        completed = asyncio.Event()

        async def _reconcile(_state, _window) -> bool:
            _state.replace_channel_slot_holds((), hold_channels=False)
            completed.set()
            return True

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)

        assert chat_persistence.restore_open_slots(booted) == 1
        assert "chat-9-remote" in booted.unrestored_slot_keys, "the seed the finish must ignore"
        try:
            await srv._restore_dashboard_sessions(booted, _restore_cfg())
            await await_until(completed)
            for _ in range(5):
                await asyncio.sleep(0)

            assert booted.slot_list_rebuilding is False
            assert "chat-9-remote" in booted.unrestored_slot_keys, "the reopen seed is kept"
        finally:
            await self._stop_reconciler(booted)

    @pytest.mark.asyncio
    async def test_complete_channel_pass_keeps_partial_for_a_dropped_tab_only_until_it_is_listed(
        self, state, _floor_monkeypatch
    ) -> None:
        """A dropped tab holds the label; the same tab rebuilt as a slot releases it."""
        completed = asyncio.Event()

        async def _reconcile(_state, _window) -> bool:
            _state.replace_channel_slot_holds((), hold_channels=False)
            completed.set()
            return True

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.unrestored_slot_keys = {"chat-2-unreadable", "chat-9-remote"}
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": ["chat-2-unreadable"],
            "unknowable": False,
        }
        state.begin_slot_list_rebuild()
        try:
            await srv._restore_dashboard_sessions(state, _restore_cfg())
            await await_until(completed)
            for _ in range(5):
                await asyncio.sleep(0)
            assert state.slot_list_rebuilding is True, "the dropped tab's dot would drain"

            state.get_or_create_slot("chat-2-unreadable")
            state.finish_slot_list_rebuild()

            assert state.slot_list_rebuilding is False, "listed again, so nothing drains"
        finally:
            await self._stop_reconciler(state)

    @pytest.mark.asyncio
    async def test_incomplete_pass_keeps_the_label_and_lists_flow(
        self, state, _floor_monkeypatch
    ) -> None:
        incomplete = asyncio.Event()
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)

        async def _reconcile(_state, _window) -> bool:
            incomplete.set()
            return False

        _floor_monkeypatch.setattr(channel_slots, "_reconcile_pass", _reconcile)
        state.begin_slot_list_rebuild()
        try:
            await srv._restore_dashboard_sessions(state, _restore_cfg())
            await await_until(incomplete)
            state.push_slots_update()

            assert state.slot_list_rebuilding is True
            assert _slots_broadcasts(broadcast)[-1]["slotsPartial"] is True
        finally:
            await self._stop_reconciler(state)

        assert state.slot_list_rebuilding is True

    @pytest.mark.asyncio
    async def test_restore_failure_before_reconciler_clears_the_label(
        self, state, _floor_monkeypatch
    ) -> None:
        _floor_monkeypatch.setattr(
            srv.chat,
            "restore_open_slots_async",
            AsyncMock(side_effect=RuntimeError("restore died")),
        )
        state.begin_slot_list_rebuild()
        assert state.slot_list_hold_channels() is True

        with pytest.raises(RuntimeError, match="restore died"):
            await srv._restore_dashboard_sessions(state, _restore_cfg())

        assert state.slot_list_rebuilding is False
        assert state.slot_list_hold_channels() is False
        assert state._channel_slot_reconciler is None

    @pytest.mark.asyncio
    async def test_without_channel_surfacing_restore_releases_the_startup_hold(self, state) -> None:
        state.begin_slot_list_rebuild()
        assert state.slot_list_hold_channels() is True

        await srv._restore_dashboard_sessions(state, _restore_cfg(surface_channel_sessions=False))

        assert state.slot_list_rebuilding is False
        assert state.slot_list_hold_channels() is False
        assert state._channel_slot_reconciler is None

    @pytest.mark.asyncio
    async def test_without_channel_surfacing_keeps_partial_for_an_unrestored_open_tab(
        self, state
    ) -> None:
        state.unrestored_slot_keys = {"chat-2-unreadable"}
        state.begin_slot_list_rebuild()

        await srv._restore_dashboard_sessions(state, _restore_cfg(surface_channel_sessions=False))

        assert state.slot_list_rebuilding is True
        assert state._channel_slot_reconciler is None

    @pytest.mark.asyncio
    async def test_without_channel_surfacing_retries_finish_when_dropped_tab_is_listed(
        self, state
    ) -> None:
        state.unrestored_slot_keys = {"chat-2-unreadable"}
        state.begin_slot_list_rebuild()

        await srv._restore_dashboard_sessions(state, _restore_cfg(surface_channel_sessions=False))
        assert state.slot_list_rebuilding is True

        state.get_or_create_slot("chat-2-unreadable")

        assert state.slot_list_rebuilding is False
        assert state._channel_slot_reconciler is None


class TestMetadataReadState:
    @staticmethod
    def _channel_log(state, line_state: str) -> SimpleNamespace:
        log = SimpleNamespace()
        state.conversation_log = log
        log.list_sessions_status = lambda: (
            [{"key": "slack_1.1", "title": "", "modified": 10**12}],
            True,
        )
        log.get_metadata_status = lambda _key: ({}, False)
        log.metadata_line_state = lambda _key: line_state
        log.mtime_of = lambda _key: None
        log.read_messages = MagicMock(return_value=[{"role": "user", "content": "hello"}])
        state.sessions.channel_key_for_stem = lambda stem: (
            "slack:1.1" if stem == "slack_1.1" else ""
        )
        return log

    @pytest.mark.asyncio
    async def test_corrupt_metadata_counts_complete_and_can_surface(
        self, state, _floor_monkeypatch
    ) -> None:
        log = self._channel_log(state, METADATA_LINE_CORRUPT)

        async def _no_folder(_state, _namespace) -> str:
            return ""

        _floor_monkeypatch.setattr(channel_slots, "lookup_channel_folder", _no_folder)

        _surfaced, complete = await channel_slots._reconcile_with_status(state, 30)

        assert complete is True
        assert "slack_1.1" in state._slots
        log.read_messages.assert_called_once_with("slack_1.1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("line_state", [METADATA_LINE_TRANSIENT, METADATA_LINE_READABLE])
    async def test_indeterminate_metadata_keeps_partial_and_is_not_surfaced(
        self, state, line_state
    ) -> None:
        log = self._channel_log(state, line_state)

        _surfaced, complete = await channel_slots._reconcile_with_status(state, 30)

        assert complete is False
        assert "slack_1.1" not in state._slots
        log.read_messages.assert_not_called()


class TestPartialDeadline:
    """A hold that never clears on its own ends at the deadline, logged."""

    @staticmethod
    def _overdue(state) -> None:
        state._slot_list_rebuild_began -= srv_state._SLOT_LIST_PARTIAL_DEADLINE_S + 1

    @staticmethod
    def _unknowable(state) -> None:
        state.unrestored_slot_notice = {"count": 0, "keys": [], "unknowable": True}
        state.finish_slot_list_rebuild()

    @staticmethod
    def _dropped_tab_never_reopened(state) -> None:
        state.unrestored_slot_notice = {"count": 1, "keys": ["chat-9-gone"], "unknowable": False}
        state.finish_slot_list_rebuild()

    @staticmethod
    def _channel_pass_never_completes(state) -> None:
        """No finish request: the reconciler only asks after a complete pass."""

    _CAUSES = [
        (_unknowable, []),
        (_dropped_tab_never_reopened, ["chat-9-gone"]),
        (_channel_pass_never_completes, []),
    ]

    @pytest.mark.parametrize("hold,expected_held", _CAUSES)
    def test_a_persistent_hold_keeps_the_label_before_the_deadline(
        self, state, hold, expected_held
    ) -> None:
        state.begin_slot_list_rebuild()
        hold.__func__(state)

        assert state._complete_slot_list_rebuild_if_ready() is False
        assert state.slot_list_rebuilding is True

    @pytest.mark.parametrize("hold,expected_held", _CAUSES)
    def test_an_unrelated_push_cannot_clear_the_label_while_a_cause_remains(
        self, state, hold, expected_held, _floor_monkeypatch
    ) -> None:
        """push_slots_update retries the clear; before the deadline a remaining cause wins."""
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        _floor_monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        state.begin_slot_list_rebuild()
        state.replace_channel_slot_holds((), hold_channels=False)
        hold.__func__(state)

        state.push_slots_update()
        state.push_slots_update()

        frames = _slots_broadcasts(broadcast)
        assert state.slot_list_rebuilding is True
        assert [frame["slotsPartial"] for frame in frames] == [True, True]
        assert frames[-1].get("slotsHeldKeys", []) == expected_held

    @pytest.mark.parametrize("hold,expected_held", _CAUSES)
    def test_a_persistent_hold_clears_with_a_warning_once_overdue(
        self, state, hold, expected_held, _floor_monkeypatch, caplog
    ) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        state.begin_slot_list_rebuild()
        hold.__func__(state)
        self._overdue(state)

        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.state"):
            state.push_slots_update()

        frame = _slots_broadcasts(broadcast)[-1]
        assert state.slot_list_rebuilding is False
        assert frame["slotsPartial"] is False
        assert frame.get("slotsHeldKeys", []) == expected_held
        assert "known key(s)" in caplog.text

    def test_deadline_keeps_every_channel_held_before_the_first_pass(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        state.begin_slot_list_rebuild()
        self._overdue(state)

        state.push_slots_update()

        frame = _slots_broadcasts(broadcast)[-1]
        assert state.slot_list_rebuilding is False
        assert state.slot_list_hold_channels() is True
        assert frame["slotsPartial"] is False
        assert frame["slotsHoldChannels"] is True

    def test_a_dropped_absent_tab_stays_held_after_deadline_until_listed(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        dropped_key = "chat-9-gone"
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": [dropped_key],
            "unknowable": False,
        }
        state.begin_slot_list_rebuild()
        state.finish_slot_list_rebuild()
        self._overdue(state)

        state.push_slots_update()

        expired = _slots_broadcasts(broadcast)[-1]
        assert expired["slotsPartial"] is False
        assert expired["slotsHeldKeys"] == [dropped_key]

        state.get_or_create_slot(dropped_key)
        state.push_slots_update()

        assert _slots_broadcasts(broadcast)[-1].get("slotsHeldKeys", []) == []

    def test_a_dropped_tab_deleted_from_history_stops_being_held(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        deleted_key = "chat-9-gone"
        kept_key = "chat-8-gone"
        state.unrestored_slot_notice = {
            "count": 2,
            "keys": [kept_key, deleted_key],
            "unknowable": False,
        }
        state.begin_slot_list_rebuild()
        state.finish_slot_list_rebuild()
        self._overdue(state)
        state.push_slots_update()
        assert _slots_broadcasts(broadcast)[-1]["slotsHeldKeys"] == [kept_key, deleted_key]

        state.release_deleted_slot_keys(["dashboard_" + deleted_key, deleted_key])
        state.push_slots_update()

        # The restore report itself is untouched; only the drain hold is lifted.
        assert state.unrestored_slot_notice["keys"] == [kept_key, deleted_key]
        assert _slots_broadcasts(broadcast)[-1]["slotsHeldKeys"] == [kept_key]

    def test_a_released_key_recycled_by_a_new_tab_is_held_while_it_builds(self, state) -> None:
        recycled_key = "chat-9-gone"
        state.unrestored_slot_notice = {"count": 1, "keys": [recycled_key], "unknowable": False}
        state.release_deleted_slot_keys([recycled_key])
        assert state.slot_list_held_keys() == []

        slot = state.get_or_create_slot(recycled_key)
        state.begin_slot_construction(slot.key)

        assert state.slot_list_held_keys() == [recycled_key]
        state.end_slot_construction(slot.key)
        assert state.slot_list_held_keys() == []

    def test_only_reported_keys_are_recorded_as_released(self, state) -> None:
        state.unrestored_slot_notice = {"count": 1, "keys": ["chat-9-gone"], "unknowable": False}

        state.release_deleted_slot_keys(["dashboard_chat-1-other", "chat-1-other", "chat-9-gone"])

        assert state._slot_list_released_keys == {"chat-9-gone"}

    def test_an_unknowable_notice_still_holds_the_keys_it_reported(
        self, state, _floor_monkeypatch
    ) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        dropped_key = "chat-9-gone"
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": [dropped_key],
            "unknowable": True,
        }
        state.begin_slot_list_rebuild()
        state.finish_slot_list_rebuild()
        self._overdue(state)

        state.push_slots_update()

        expired = _slots_broadcasts(broadcast)[-1]
        assert expired["slotsPartial"] is False
        assert state.slot_list_held_keys() == [dropped_key]
        assert expired["slotsHeldKeys"] == [dropped_key]

    def test_a_dropped_tab_under_construction_holds_past_the_deadline(self, state) -> None:
        dropped_key = "chat-2-rebuilding"
        state.unrestored_slot_notice = {"count": 1, "keys": [dropped_key], "unknowable": False}
        state.begin_slot_list_rebuild()
        state.replace_channel_slot_holds((), hold_channels=False)
        state.finish_slot_list_rebuild()
        slot = state.get_or_create_slot(dropped_key)
        state.begin_slot_construction(slot.key)
        self._overdue(state)

        assert state._complete_slot_list_rebuild_if_ready() is False

        state.end_slot_construction(slot.key)
        state.push_slots_update()
        assert state.slot_list_rebuilding is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hold,expected_held", _CAUSES)
    async def test_the_deadline_timer_clears_even_when_it_fires_early(
        self, state, _floor_monkeypatch, hold, expected_held
    ) -> None:
        # asyncio runs a timer up to its clock resolution early (about 16 ms on
        # Windows), so the timer must not re-measure a deadline it already marks.
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        _floor_monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        state.begin_slot_list_rebuild()
        hold(state)
        assert state.slot_list_rebuilding is True

        state._expire_slot_list_rebuild()

        assert state.slot_list_rebuilding is False
        assert state.slot_list_held_keys() == expected_held
        assert state._slot_list_rebuild_deadline is None
        assert [frame["slotsPartial"] for frame in _slots_broadcasts(broadcast)] == [False]

    def test_a_suspended_push_holds_past_the_deadline(self, state, _floor_monkeypatch) -> None:
        broadcast = MagicMock()
        _floor_monkeypatch.setattr(state, "_broadcast", broadcast)
        _floor_monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        state.begin_slot_list_rebuild()
        self._overdue(state)

        with state.suspend_slots_push():
            assert state._complete_slot_list_rebuild_if_ready() is False
            state.push_slots_update()

        assert state.slot_list_rebuilding is False
        assert [frame["slotsPartial"] for frame in _slots_broadcasts(broadcast)] == [False]

    @pytest.mark.asyncio
    async def test_the_armed_deadline_clears_and_pushes_on_an_idle_gateway(
        self, state, _floor_monkeypatch
    ) -> None:
        cleared = asyncio.Event()

        def _broadcast(frame) -> None:
            if frame.get("_type") == "slots" and frame.get("slotsPartial") is False:
                cleared.set()

        _floor_monkeypatch.setattr(state, "_broadcast", _broadcast)
        _floor_monkeypatch.setattr(srv_state, "_SLOT_LIST_PARTIAL_DEADLINE_S", 0.0)
        state.begin_slot_list_rebuild()
        state.arm_slot_list_rebuild_deadline()

        await await_until(cleared, what="deadline push")

        assert state.slot_list_rebuilding is False
        assert state._slot_list_rebuild_deadline is None

    @pytest.mark.asyncio
    async def test_deadline_retries_after_a_suspended_push_releases(
        self, state, _floor_monkeypatch
    ) -> None:
        deadline_fired = asyncio.Event()
        cleared = asyncio.Event()
        expire = state._expire_slot_list_rebuild

        def _observe_expiry() -> None:
            expire()
            deadline_fired.set()

        def _broadcast(frame) -> None:
            if frame.get("_type") == "slots" and frame.get("slotsPartial") is False:
                cleared.set()

        _floor_monkeypatch.setattr(state, "_expire_slot_list_rebuild", _observe_expiry)
        _floor_monkeypatch.setattr(state, "_broadcast", _broadcast)
        _floor_monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        _floor_monkeypatch.setattr(srv_state, "_SLOT_LIST_PARTIAL_DEADLINE_S", 0.01)
        state.begin_slot_list_rebuild()

        with state.suspend_slots_push():
            state.arm_slot_list_rebuild_deadline()
            await await_until(deadline_fired, what="deadline during suspended push")
            assert state.slot_list_rebuilding is True

        await await_until(cleared, what="deadline retry after suspended push")
        assert state._slot_list_rebuild_deadline is None

    @pytest.mark.asyncio
    async def test_deadline_retries_after_dropped_tab_construction_ends(
        self, state, _floor_monkeypatch
    ) -> None:
        dropped_key = "chat-2-rebuilding"
        deadline_fired = asyncio.Event()
        cleared = asyncio.Event()
        expire = state._expire_slot_list_rebuild

        def _observe_expiry() -> None:
            expire()
            deadline_fired.set()

        def _broadcast(frame) -> None:
            if frame.get("_type") == "slots" and frame.get("slotsPartial") is False:
                cleared.set()

        _floor_monkeypatch.setattr(state, "_expire_slot_list_rebuild", _observe_expiry)
        _floor_monkeypatch.setattr(state, "_broadcast", _broadcast)
        _floor_monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        state.unrestored_slot_notice = {
            "count": 1,
            "keys": [dropped_key],
            "unknowable": False,
        }
        state.begin_slot_list_rebuild()
        state.replace_channel_slot_holds((), hold_channels=False)
        state.finish_slot_list_rebuild()
        with state.suspend_slots_push():
            slot = state.get_or_create_slot(dropped_key)
            state.begin_slot_construction(slot.key)
        # Shorten the deadline only now: a slow host can spend more than 10 ms in the
        # setup above, and an already-expired deadline would clear the label inside
        # finish_slot_list_rebuild(), leaving arm_slot_list_rebuild_deadline() nothing to arm.
        _floor_monkeypatch.setattr(srv_state, "_SLOT_LIST_PARTIAL_DEADLINE_S", 0.01)
        state.arm_slot_list_rebuild_deadline()

        await await_until(deadline_fired, what="deadline during slot construction")
        assert state.slot_list_rebuilding is False
        assert state.slot_list_held_keys() == [dropped_key]

        state.end_slot_construction(slot.key)
        state.push_slots_update()

        assert state.slot_list_held_keys() == []
        assert cleared.is_set()
        assert state._slot_list_rebuild_deadline is None

    @pytest.mark.asyncio
    async def test_a_normal_clear_cancels_the_armed_deadline(self, state) -> None:
        state.begin_slot_list_rebuild()
        state.replace_channel_slot_holds((), hold_channels=False)
        state.arm_slot_list_rebuild_deadline()
        timer = state._slot_list_rebuild_deadline

        state.finish_slot_list_rebuild()

        assert state.slot_list_rebuilding is False
        assert timer is not None and timer.cancelled()
        assert state._slot_list_rebuild_deadline is None

    @pytest.mark.asyncio
    async def test_boot_restore_arms_the_deadline(self, state, _floor_monkeypatch) -> None:
        _floor_monkeypatch.setattr(srv.cautious_boot, "pause_before", AsyncMock(return_value=None))
        _floor_monkeypatch.setattr(srv.chat, "restore_open_slots_async", AsyncMock(return_value=0))
        _floor_monkeypatch.setattr(
            srv.chat, "restore_recent_sessions_async", AsyncMock(return_value=0)
        )
        state.unrestored_slot_notice = {"count": 0, "keys": [], "unknowable": True}
        state.begin_slot_list_rebuild()

        await srv._restore_dashboard_sessions(state, _restore_cfg(surface_channel_sessions=False))

        assert state.slot_list_rebuilding is True
        assert state._slot_list_rebuild_deadline is not None
        state._slot_list_rebuild_deadline.cancel()
