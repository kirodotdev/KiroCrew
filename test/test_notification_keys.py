"""Keyed notification notes: a key replaces its source's earlier note, quietly
unless more urgent, and an agent can dismiss a note by key.

The frontend half is pinned by ``website/src/test/notificationKeys.test.ts``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.constants import MAX_SHORT_STRING
from kiro_crew.dashboard.handlers.messaging import (
    api_notification_agent_dismiss,
    api_notification_agent_push,
)
from kiro_crew.dashboard.state import DashboardState, _load_notifications
from kiro_crew.notifications.bus import NotificationPayload, NotificationValidationError
from kiro_crew.testing.wait import await_until


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _agent_payload(title: str, *, key: str | None = None, priority: str | None = None):
    return NotificationPayload(
        source="system",
        channel="system.agent",
        title=title,
        body="",
        priority=priority,
        key=key,
    )


async def _push(state: DashboardState, payload: NotificationPayload) -> dict:
    note = state.notification_bus.push(payload)
    persist = state.last_notification_persist
    assert persist is not None and await persist
    return note


def _titles(state: DashboardState) -> list[str]:
    return [note["title"] for note in state._notification_log]


#: How long a staged executor job waits to be released before failing. A lost release
#: fails the test at that wait instead of hanging the worker until the suite timeout.
_SIGNAL_TIMEOUT_SECS = 5


async def _signalled(event: asyncio.Event) -> None:
    await await_until(event, what="the staged executor callback")


def _stored_titles() -> list[str]:
    return [note["title"] for note in _load_notifications()]


def _capped_state_with_clock(
    monkeypatch, tmp_path, *, clock_minutes: tuple[int, ...]
) -> DashboardState:
    state = _make_state(monkeypatch, tmp_path)
    state._notification_coordinator._max_persisted = 3
    monkeypatch.setattr("kiro_crew.dashboard.state._MAX_PERSISTED_NOTIFICATIONS", 3)
    clock = MagicMock()
    clock.now.side_effect = [
        datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc) for minute in clock_minutes
    ]
    monkeypatch.setattr("kiro_crew.notifications.bus.datetime", clock)
    return state


async def _state_at_real_cap_with_key(
    monkeypatch, tmp_path, *, keyed_title: str
) -> tuple[DashboardState, dict]:
    state = _make_state(monkeypatch, tmp_path)
    max_persisted = state._notification_coordinator._max_persisted
    assert max_persisted == 200
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = MagicMock()
    clock.now.side_effect = [start + timedelta(seconds=index) for index in range(max_persisted + 1)]
    monkeypatch.setattr("kiro_crew.notifications.bus.datetime", clock)

    for index in range(max_persisted - 1):
        await _push(state, _agent_payload(f"retained-{index}"))
    keyed = await _push(state, _agent_payload(keyed_title, key="k"))
    assert len(state._notification_log) == max_persisted
    return state, keyed


async def _overlap_keyed_rewrite_with_unkeyed_arrival(
    monkeypatch, tmp_path, *, clock_minutes: tuple[int, int, int, int, int]
) -> DashboardState:
    from kiro_crew.dashboard import state as state_module

    state = _make_state(monkeypatch, tmp_path)
    state._notification_coordinator._max_persisted = 3
    monkeypatch.setattr("kiro_crew.dashboard.state._MAX_PERSISTED_NOTIFICATIONS", 3)
    clock = MagicMock()
    clock.now.side_effect = [
        datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc) for minute in clock_minutes
    ]
    monkeypatch.setattr("kiro_crew.notifications.bus.datetime", clock)

    for index in range(3):
        await _push(state, _agent_payload(f"retained-{index}"))

    real_rewrite = state_module._rewrite_notifications
    rewrite_started = asyncio.Event()
    release_rewrite = threading.Event()
    loop = asyncio.get_running_loop()

    def hold_rewrite(notes):
        loop.call_soon_threadsafe(rewrite_started.set)
        assert release_rewrite.wait(timeout=5)
        return real_rewrite(notes)

    monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_rewrite)

    state.notification_bus.push(_agent_payload("keyed", key="clock-direction"))
    replacing = state.last_notification_persist
    await _signalled(rewrite_started)
    state.notification_bus.push(_agent_payload("unkeyed"))
    appending = state.last_notification_persist
    release_rewrite.set()

    assert replacing is not None and await replacing is True
    assert appending is not None and await appending is True
    return state


class TestKeyValidation:
    @pytest.mark.parametrize("key", ["", "   ", 7, "k" * 501])
    def test_a_malformed_key_is_rejected(self, key):
        with pytest.raises(NotificationValidationError, match="key"):
            _agent_payload("t", key=key).validate()  # type: ignore[arg-type]

    def test_meta_cannot_set_a_key_or_mark_a_note_quiet(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notify("cron", "t", "b", meta={"key": "forged", "quiet": True, "job_id": "j1"})
        note = state._notification_log[-1]
        assert note["job_id"] == "j1"
        assert "key" not in note and "quiet" not in note


class TestReplacement:
    @pytest.mark.asyncio
    async def test_a_keyed_note_replaces_its_earlier_note_in_memory_and_on_disk(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("Unrelated"))
        await _push(state, _agent_payload("Pushed", key="cr:1"))
        await _push(state, _agent_payload("Merged", key="cr:1"))
        assert _titles(state) == ["Unrelated", "Merged"]
        # An append would leave "Pushed" on disk to come back on the next load.
        assert _stored_titles() == ["Unrelated", "Merged"]

    @pytest.mark.asyncio
    async def test_an_earlier_keyed_note_survives_a_full_retention_cap(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state._notification_coordinator._max_persisted = 3
        clock = MagicMock()
        clock.now.side_effect = [
            datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc) for minute in (1, 2, 3, 0)
        ]
        monkeypatch.setattr("kiro_crew.notifications.bus.datetime", clock)

        for index in range(3):
            await _push(state, _agent_payload(f"retained-{index}"))
        await _push(state, _agent_payload("incoming", key="clock-correction"))

        expected = ["retained-1", "retained-2", "incoming"]
        assert _titles(state) == expected
        assert _stored_titles() == expected
        assert len(state._notification_log) == 3
        assert state._unread_count == 3
        assert await state.dismiss_notification(source="system", key="clock-correction") == (
            True,
            True,
        )

    @pytest.mark.asyncio
    async def test_a_backward_clock_overlap_keeps_memory_equal_to_reloaded_file(
        self, monkeypatch, tmp_path
    ):
        state = await _overlap_keyed_rewrite_with_unkeyed_arrival(
            monkeypatch, tmp_path, clock_minutes=(1, 2, 3, 0, 4)
        )

        expected = ["retained-2", "keyed", "unkeyed"]
        assert _titles(state) == _stored_titles() == expected
        assert await state.dismiss_notification(source="system", key="clock-direction") == (
            True,
            True,
        )

    @pytest.mark.asyncio
    async def test_a_forward_clock_overlap_keeps_keyed_arrival_before_unkeyed_arrival(
        self, monkeypatch, tmp_path
    ):
        state = await _overlap_keyed_rewrite_with_unkeyed_arrival(
            monkeypatch, tmp_path, clock_minutes=(0, 1, 2, 3, 4)
        )

        expected = ["retained-2", "keyed", "unkeyed"]
        assert _titles(state) == _stored_titles() == expected
        assert await state.dismiss_notification(source="system", key="clock-direction") == (
            True,
            True,
        )

    @pytest.mark.asyncio
    async def test_other_keys_other_sources_and_unkeyed_notes_are_kept(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_bus.register_channel("mochi.alerts")
        await _push(state, _agent_payload("cr 1", key="cr:1"))
        await _push(state, _agent_payload("cr 2", key="cr:2"))
        await _push(
            state,
            NotificationPayload(
                source="app:mochi", channel="mochi.alerts", title="app cr 1", body="", key="cr:1"
            ),
        )
        await _push(state, _agent_payload("plain"))
        await _push(state, _agent_payload("plain"))
        assert _titles(state) == ["cr 1", "cr 2", "app cr 1", "plain", "plain"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("first", "second", "quiet"),
        [
            ("default", "default", True),
            ("critical", "default", True),
            ("default", "passive", True),
            ("default", "critical", False),
            ("passive", "default", False),
        ],
    )
    async def test_a_replacement_is_quiet_unless_more_urgent(
        self, monkeypatch, tmp_path, first, second, quiet
    ):
        state = _make_state(monkeypatch, tmp_path)
        first_note = await _push(state, _agent_payload("one", key="k", priority=first))
        assert "quiet" not in first_note
        second_note = await _push(state, _agent_payload("two", key="k", priority=second))
        assert second_note.get("quiet", False) is quiet

    @pytest.mark.asyncio
    async def test_the_replacement_is_unread_even_when_the_replaced_note_was_read(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        first = await _push(state, _agent_payload("one", key="k"))
        assert await state.ack_notification(first["ts"])
        await _push(state, _agent_payload("two", key="k"))
        assert [note.get("acked") for note in state._notification_log] == [None]

    @pytest.mark.asyncio
    async def test_a_first_keyed_publish_whose_write_fails_shows_nothing(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        frames: list[dict] = []
        monkeypatch.setattr(state, "_broadcast", frames.append)
        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", lambda notes: False)
        state.notification_bus.push(_agent_payload("first", key="k"))
        assert await state.last_notification_persist is False
        assert state._notification_log == [] and frames == []
        assert state._unread_count == 0

    @pytest.mark.asyncio
    async def test_a_failed_rewrite_fails_the_publish_and_changes_nothing(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("one", key="k"))
        frames: list[dict] = []
        monkeypatch.setattr(state, "_broadcast", frames.append)
        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", lambda notes: False)
        state.notification_bus.push(_agent_payload("two", key="k"))
        assert await state.last_notification_persist is False
        # Memory, the open views and disk all still show the note it was to replace.
        assert _titles(state) == ["one"]
        assert state._unread_count == 1
        assert frames == []
        assert _stored_titles() == ["one"]

    @pytest.mark.asyncio
    async def test_a_replacement_reaches_the_views_only_after_it_is_written(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("one", key="k"))
        frames: list[str] = []
        monkeypatch.setattr(state, "_broadcast", lambda note: frames.append(note["title"]))
        state.notification_bus.push(_agent_payload("two", key="k"))
        assert frames == []
        assert await state.last_notification_persist is True
        assert frames == ["two"] and _titles(state) == ["two"]

    @pytest.mark.asyncio
    async def test_first_of_two_overlapping_replacements_survives_when_second_fails(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("A", key="k"))
        frames: list[tuple[str, object]] = []
        monkeypatch.setattr(
            state, "_broadcast", lambda note: frames.append(("notification", note["title"]))
        )
        monkeypatch.setattr(
            state, "broadcast_ws", lambda kind, data: frames.append((kind, data.get("ts")))
        )
        real_rewrite = state_module._rewrite_notifications
        first_started = asyncio.Event()
        release_first = threading.Event()
        loop = asyncio.get_running_loop()
        calls = 0

        def first_succeeds_second_fails(notes):
            nonlocal calls
            calls += 1
            if calls == 1:
                loop.call_soon_threadsafe(first_started.set)
                assert release_first.wait(timeout=5)
                return real_rewrite(notes)
            return False

        monkeypatch.setattr(
            "kiro_crew.dashboard.state._rewrite_notifications", first_succeeds_second_fails
        )
        state.notification_bus.push(_agent_payload("B", key="k"))
        first = state.last_notification_persist
        await _signalled(first_started)
        assert _titles(state) == ["A"]
        state.notification_bus.push(_agent_payload("C", key="k"))
        second = state.last_notification_persist
        release_first.set()

        assert first is not None and await first is True
        assert second is not None and await second is False
        assert _titles(state) == ["B"]
        assert _stored_titles() == ["B"]
        assert frames == [("notification", "B")]

    @pytest.mark.asyncio
    async def test_a_dismiss_queued_behind_a_replacement_leaves_memory_and_disk_empty(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("one", key="k"))
        frames: list[tuple[str, object]] = []
        monkeypatch.setattr(
            state, "_broadcast", lambda note: frames.append(("notification", note["title"]))
        )
        monkeypatch.setattr(
            state, "broadcast_ws", lambda kind, data: frames.append((kind, data.get("ts")))
        )
        real_rewrite = state_module._rewrite_notifications
        replacement_started = asyncio.Event()
        release_replacement = threading.Event()
        loop = asyncio.get_running_loop()

        def hold_replacement(notes):
            loop.call_soon_threadsafe(replacement_started.set)
            assert release_replacement.wait(timeout=5)
            return real_rewrite(notes)

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_replacement)
        replacement = state.notification_bus.push(_agent_payload("two", key="k"))
        replacing = state.last_notification_persist
        await _signalled(replacement_started)
        dismissing = asyncio.create_task(state.dismiss_notification(source="system", key="k"))
        release_replacement.set()

        assert replacing is not None and await replacing is True
        assert await dismissing == (True, True)
        assert _titles(state) == []
        assert _stored_titles() == []
        assert frames == [
            ("notification", "two"),
            ("notification_dismiss", replacement["ts"]),
        ]

    @pytest.mark.asyncio
    async def test_a_dismiss_broadcasts_a_note_deleted_while_its_rewrite_is_held(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state = _make_state(monkeypatch, tmp_path)
        clock = MagicMock()
        clock.now.side_effect = [
            datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc) for minute in (0, 1)
        ]
        monkeypatch.setattr("kiro_crew.notifications.bus.datetime", clock)
        keyed = await _push(state, _agent_payload("keyed", key="k", priority="passive"))
        await _push(state, _agent_payload("unkeyed"))
        frames: list[tuple[str, dict]] = []
        original_broadcast = state.broadcast_ws

        def capture_broadcast(kind, data):
            frames.append((kind, data))
            original_broadcast(kind, data)

        monkeypatch.setattr(state, "broadcast_ws", capture_broadcast)
        real_rewrite = state_module._rewrite_notifications
        rewrite_started = asyncio.Event()
        delete_queued = asyncio.Event()
        release_rewrite = threading.Event()
        loop = asyncio.get_running_loop()

        def hold_rewrite(notes):
            loop.call_soon_threadsafe(rewrite_started.set)
            assert release_rewrite.wait(timeout=_SIGNAL_TIMEOUT_SECS)
            return real_rewrite(notes)

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_rewrite)
        original_rewrite = state._rewrite_notifications_async

        async def observe_queued_delete() -> bool:
            delete_queued.set()
            return await original_rewrite()

        monkeypatch.setattr(state, "_rewrite_notifications_async", observe_queued_delete)
        dismissing = asyncio.create_task(state.dismiss_notification(source="system", key="k"))
        await _signalled(rewrite_started)
        deleting = asyncio.create_task(state.delete_notification(keyed["ts"]))
        await _signalled(delete_queued)
        assert _titles(state) == ["unkeyed"]
        release_rewrite.set()

        assert await await_until(dismissing, what="the keyed dismissal") == (True, True)
        assert await await_until(deleting, what="the concurrent deletion") is True
        assert frames == [("notification_dismiss", {"ts": keyed["ts"]})]
        assert _titles(state) == _stored_titles() == ["unkeyed"]
        assert state._unread_count == sum(
            note.get("priority") != "passive" for note in state._notification_log
        )

    @pytest.mark.asyncio
    async def test_an_ack_during_a_replacement_write_is_not_undone_on_disk(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("other"))
        await _push(state, _agent_payload("one", key="k"))
        other_ts = state._notification_log[0]["ts"]
        state.notification_bus.push(_agent_payload("two", key="k"))
        replacing = state.last_notification_persist
        assert await state.ack_notification(other_ts)
        assert await replacing is True
        stored = {note["title"]: note for note in _load_notifications()}
        assert set(stored) == {"other", "two"}
        assert stored["other"].get("acked") is True

    @pytest.mark.asyncio
    async def test_a_queued_ack_persists_the_note_after_a_keyed_dismiss_fails(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state = _make_state(monkeypatch, tmp_path)
        note = await _push(state, _agent_payload("held", key="k"))
        frames: list[tuple[str, dict]] = []
        monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: frames.append((kind, data)))
        real_rewrite = state_module._rewrite_notifications
        dismiss_started = asyncio.Event()
        ack_queued = asyncio.Event()
        release_dismiss = threading.Event()
        loop = asyncio.get_running_loop()
        calls = 0

        def dismiss_fails_then_ack_succeeds(notes):
            nonlocal calls
            calls += 1
            if calls == 1:
                loop.call_soon_threadsafe(dismiss_started.set)
                assert release_dismiss.wait(timeout=5)
                return False
            return real_rewrite(notes)

        monkeypatch.setattr(
            "kiro_crew.dashboard.state._rewrite_notifications", dismiss_fails_then_ack_succeeds
        )
        original_rewrite = state._rewrite_notifications_async

        async def observe_queued_ack() -> bool:
            ack_queued.set()
            return await original_rewrite()

        monkeypatch.setattr(state, "_rewrite_notifications_async", observe_queued_ack)
        dismissing = asyncio.create_task(state.dismiss_notification(source="system", key="k"))
        await _signalled(dismiss_started)
        assert _titles(state) == ["held"]
        acknowledging = asyncio.create_task(state.ack_notification(note["ts"]))
        await _signalled(ack_queued)
        release_dismiss.set()

        assert await dismissing == (True, False)
        assert await acknowledging is True
        assert _titles(state) == ["held"]
        stored = _load_notifications()
        assert [item["title"] for item in stored] == ["held"]
        assert stored[0].get("acked") is True
        assert not any(kind == "notification_dismiss" for kind, _ in frames)

    @pytest.mark.asyncio
    async def test_a_clear_racing_a_replacement_leaves_memory_matching_disk(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("one", key="k"))
        frames: list[tuple[str, object]] = []
        clear_started = asyncio.Event()

        def broadcast(kind, data):
            frames.append((kind, data.get("ts")))
            if kind == "notifications_clear":
                clear_started.set()

        monkeypatch.setattr(state, "broadcast_ws", broadcast)
        monkeypatch.setattr(
            state, "_broadcast", lambda note: frames.append(("notification", note["title"]))
        )
        real_rewrite = state_module._rewrite_notifications
        replacement_started = asyncio.Event()
        release_replacement = threading.Event()
        loop = asyncio.get_running_loop()
        calls = 0

        def hold_first_rewrite(notes):
            nonlocal calls
            calls += 1
            if calls == 1:
                loop.call_soon_threadsafe(replacement_started.set)
                assert release_replacement.wait(timeout=5)
            return real_rewrite(notes)

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_first_rewrite)
        state.notification_bus.push(_agent_payload("two", key="k"))
        replacing = state.last_notification_persist
        await _signalled(replacement_started)
        clearing = asyncio.create_task(state.clear_notifications())
        await _signalled(clear_started)
        release_replacement.set()

        assert replacing is not None and await replacing is True
        await clearing
        assert _titles(state) == ["two"]
        assert _stored_titles() == ["two"]
        assert frames == [("notifications_clear", None), ("notification", "two")]

    @pytest.mark.asyncio
    async def test_two_replacements_in_flight_leave_only_the_last(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("one", key="k"))
        state.notification_bus.push(_agent_payload("two", key="k"))
        first = state.last_notification_persist
        state.notification_bus.push(_agent_payload("three", key="k"))
        second = state.last_notification_persist
        assert await first is True and await second is True
        assert _titles(state) == ["three"]
        assert _stored_titles() == ["three"]

    @pytest.mark.asyncio
    async def test_cancelling_a_replacement_waiter_does_not_cancel_the_write_then_commit(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state, replaced = await _state_at_real_cap_with_key(
            monkeypatch, tmp_path, keyed_title="replaced"
        )
        frames: list[dict] = []
        replacement_landed = asyncio.Event()

        def broadcast(note):
            frames.append(dict(note))
            replacement_landed.set()

        monkeypatch.setattr(state, "_broadcast", broadcast)
        real_rewrite = state_module._rewrite_notifications
        rewrite_started = asyncio.Event()
        release_rewrite = threading.Event()
        loop = asyncio.get_running_loop()

        def hold_rewrite(notes):
            loop.call_soon_threadsafe(rewrite_started.set)
            assert release_rewrite.wait(timeout=_SIGNAL_TIMEOUT_SECS)
            return real_rewrite(notes)

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_rewrite)
        replacement = state.notification_bus.push(_agent_payload("replacement", key="k"))
        replacing = state.last_notification_persist
        assert replacing is not None
        await _signalled(rewrite_started)
        replacing.cancel()
        release_rewrite.set()

        with pytest.raises(asyncio.CancelledError):
            await replacing
        try:
            await _signalled(replacement_landed)
        except asyncio.TimeoutError:
            pass

        stored = _load_notifications()
        assert state._notification_log == stored
        assert replacement in state._notification_log
        assert replaced not in state._notification_log
        assert frames == [replacement]

    @pytest.mark.asyncio
    async def test_cancelling_a_dismiss_waiter_does_not_cancel_the_write_then_commit(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state, dismissed = await _state_at_real_cap_with_key(
            monkeypatch, tmp_path, keyed_title="dismissed"
        )
        frames: list[tuple[str, dict]] = []
        dismissal_landed = asyncio.Event()

        def broadcast(kind, data):
            frames.append((kind, data))
            if kind == "notification_dismiss":
                dismissal_landed.set()

        monkeypatch.setattr(state, "broadcast_ws", broadcast)
        real_rewrite = state_module._rewrite_notifications
        rewrite_started = asyncio.Event()
        release_rewrite = threading.Event()
        loop = asyncio.get_running_loop()

        def hold_rewrite(notes):
            loop.call_soon_threadsafe(rewrite_started.set)
            assert release_rewrite.wait(timeout=_SIGNAL_TIMEOUT_SECS)
            return real_rewrite(notes)

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_rewrite)
        dismissing = asyncio.create_task(state.dismiss_notification(source="system", key="k"))
        await _signalled(rewrite_started)
        dismissing.cancel()
        release_rewrite.set()

        with pytest.raises(asyncio.CancelledError):
            await dismissing
        try:
            await _signalled(dismissal_landed)
        except asyncio.TimeoutError:
            pass

        stored = _load_notifications()
        assert state._notification_log == stored
        assert dismissed not in state._notification_log
        assert frames == [("notification_dismiss", {"ts": dismissed["ts"]})]

    @pytest.mark.asyncio
    async def test_dismiss_at_cap_keeps_the_slot_freed_by_the_dismissal(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state = _capped_state_with_clock(monkeypatch, tmp_path, clock_minutes=(0, 1, 2, 3))
        await _push(state, _agent_payload("oldest"))
        dismissed = await _push(state, _agent_payload("dismissed", key="k"))
        await _push(state, _agent_payload("newest"))
        frames: list[tuple[str, dict]] = []
        monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: frames.append((kind, data)))
        real_rewrite = state_module._rewrite_notifications
        rewrite_started = asyncio.Event()
        release_rewrite = threading.Event()
        loop = asyncio.get_running_loop()

        def hold_rewrite(notes):
            loop.call_soon_threadsafe(rewrite_started.set)
            assert release_rewrite.wait(timeout=5)
            return real_rewrite(notes)

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_rewrite)
        dismissing = asyncio.create_task(state.dismiss_notification(source="system", key="k"))
        await _signalled(rewrite_started)
        state.notification_bus.push(_agent_payload("arrival"))
        appending = state.last_notification_persist
        release_rewrite.set()

        assert await dismissing == (True, True)
        assert appending is not None and await appending is True
        expected = ["oldest", "newest", "arrival"]
        assert _titles(state) == _stored_titles() == expected
        assert len(state._notification_log) == 3
        assert state._unread_count == 3
        assert frames == [("notification_dismiss", {"ts": dismissed["ts"]})]

    @pytest.mark.asyncio
    async def test_replacement_broadcast_arrival_is_trimmed_to_the_retention_cap(
        self, monkeypatch, tmp_path
    ):
        state = _capped_state_with_clock(monkeypatch, tmp_path, clock_minutes=(0, 1, 2, 3, 4))
        await _push(state, _agent_payload("oldest"))
        await _push(state, _agent_payload("replaced", key="k"))
        await _push(state, _agent_payload("newest"))
        original_broadcast = state._broadcast
        late_persist = None
        delivered = False

        def broadcast(note):
            nonlocal delivered, late_persist
            if note["title"] == "replacement" and not delivered:
                delivered = True
                state.notification_bus.push(_agent_payload("late"))
                late_persist = state.last_notification_persist
            original_broadcast(note)

        monkeypatch.setattr(state, "_broadcast", broadcast)
        state.notification_bus.push(_agent_payload("replacement", key="k"))
        replacing = state.last_notification_persist

        assert replacing is not None
        assert await await_until(replacing, what="the keyed replacement") is True
        assert late_persist is not None
        assert await await_until(late_persist, what="the late note's persist") is True
        expected = ["newest", "replacement", "late"]
        assert len(state._notification_log) == 3
        assert _titles(state) == _stored_titles() == expected

    @pytest.mark.asyncio
    async def test_dismiss_broadcast_arrival_is_trimmed_to_the_retention_cap(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state = _capped_state_with_clock(monkeypatch, tmp_path, clock_minutes=(0, 1, 2, 3, 4))
        await _push(state, _agent_payload("oldest"))
        await _push(state, _agent_payload("dismissed", key="k"))
        await _push(state, _agent_payload("newest"))
        original_broadcast = state.broadcast_ws
        late_persist = None
        delivered = False

        def broadcast(kind, data):
            nonlocal delivered, late_persist
            if kind == "notification_dismiss" and not delivered:
                delivered = True
                state.notification_bus.push(_agent_payload("late"))
                late_persist = state.last_notification_persist
            original_broadcast(kind, data)

        monkeypatch.setattr(state, "broadcast_ws", broadcast)
        real_rewrite = state_module._rewrite_notifications
        rewrite_started = asyncio.Event()
        release_rewrite = threading.Event()
        loop = asyncio.get_running_loop()

        def hold_rewrite(notes):
            loop.call_soon_threadsafe(rewrite_started.set)
            assert release_rewrite.wait(timeout=_SIGNAL_TIMEOUT_SECS)
            return real_rewrite(notes)

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_rewrite)
        dismissing = asyncio.create_task(state.dismiss_notification(source="system", key="k"))
        await _signalled(rewrite_started)
        state.notification_bus.push(_agent_payload("arrival"))
        appending = state.last_notification_persist
        release_rewrite.set()

        assert await await_until(dismissing, what="the keyed dismissal") == (True, True)
        assert appending is not None
        assert await await_until(appending, what="the arrival's persist") is True
        assert late_persist is not None
        assert await await_until(late_persist, what="the late note's persist") is True
        expected = ["newest", "arrival", "late"]
        assert len(state._notification_log) == 3
        assert _titles(state) == _stored_titles() == expected

    @pytest.mark.asyncio
    async def test_replacing_oldest_at_cap_keeps_the_written_quiet_decision(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.dashboard import state as state_module

        state = _capped_state_with_clock(monkeypatch, tmp_path, clock_minutes=(0, 1, 2, 3, 4))
        await _push(state, _agent_payload("oldest", key="k"))
        await _push(state, _agent_payload("middle"))
        await _push(state, _agent_payload("newest"))
        frames: list[dict] = []
        monkeypatch.setattr(state, "_broadcast", lambda note: frames.append(dict(note)))
        real_rewrite = state_module._rewrite_notifications
        rewrite_started = asyncio.Event()
        release_rewrite = threading.Event()
        loop = asyncio.get_running_loop()

        def hold_rewrite(notes):
            loop.call_soon_threadsafe(rewrite_started.set)
            assert release_rewrite.wait(timeout=5)
            return real_rewrite(notes)

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", hold_rewrite)
        state.notification_bus.push(_agent_payload("replacement", key="k"))
        replacing = state.last_notification_persist
        await _signalled(rewrite_started)
        state.notification_bus.push(_agent_payload("arrival"))
        appending = state.last_notification_persist
        release_rewrite.set()

        assert replacing is not None and await replacing is True
        assert appending is not None and await appending is True
        expected = ["newest", "replacement", "arrival"]
        assert _titles(state) == _stored_titles() == expected
        replacement_frames = [note for note in frames if note["title"] == "replacement"]
        assert len(replacement_frames) == 1
        assert replacement_frames[0].get("quiet") is True
        stored = {note["title"]: note for note in _load_notifications()}
        assert stored["replacement"].get("quiet") is True
        assert state._unread_count == 3

    @pytest.mark.asyncio
    async def test_failed_keyed_write_at_cap_trims_the_overlapping_arrival(
        self, monkeypatch, tmp_path
    ):
        state = _capped_state_with_clock(monkeypatch, tmp_path, clock_minutes=(0, 1, 2, 3, 4))
        await _push(state, _agent_payload("oldest", key="k"))
        await _push(state, _agent_payload("retained-1"))
        await _push(state, _agent_payload("retained-2"))
        frames: list[dict] = []
        monkeypatch.setattr(state, "_broadcast", lambda note: frames.append(dict(note)))
        rewrite_started = asyncio.Event()
        release_rewrite = threading.Event()
        loop = asyncio.get_running_loop()

        def fail_rewrite(notes):
            loop.call_soon_threadsafe(rewrite_started.set)
            assert release_rewrite.wait(timeout=5)
            return False

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", fail_rewrite)
        state.notification_bus.push(_agent_payload("replacement", key="k"))
        replacing = state.last_notification_persist
        await _signalled(rewrite_started)
        state.notification_bus.push(_agent_payload("arrival"))
        appending = state.last_notification_persist
        release_rewrite.set()

        assert replacing is not None and await replacing is False
        assert appending is not None and await appending is True
        expected = ["retained-1", "retained-2", "arrival"]
        assert _titles(state) == _stored_titles() == expected
        assert len(state._notification_log) == 3
        assert state._unread_count == 3
        assert [note["title"] for note in frames] == ["arrival"]

    @pytest.mark.asyncio
    async def test_failed_dismiss_at_cap_trims_the_overlapping_arrival(self, monkeypatch, tmp_path):
        state = _capped_state_with_clock(monkeypatch, tmp_path, clock_minutes=(0, 1, 2, 3))
        await _push(state, _agent_payload("oldest"))
        await _push(state, _agent_payload("dismissed", key="k"))
        await _push(state, _agent_payload("newest"))
        frames: list[tuple[str, dict]] = []
        monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: frames.append((kind, data)))
        rewrite_started = asyncio.Event()
        release_rewrite = threading.Event()
        loop = asyncio.get_running_loop()

        def fail_rewrite(notes):
            loop.call_soon_threadsafe(rewrite_started.set)
            assert release_rewrite.wait(timeout=5)
            return False

        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", fail_rewrite)
        dismissing = asyncio.create_task(state.dismiss_notification(source="system", key="k"))
        await _signalled(rewrite_started)
        state.notification_bus.push(_agent_payload("arrival"))
        appending = state.last_notification_persist
        release_rewrite.set()

        assert await dismissing == (True, False)
        assert appending is not None and await appending is True
        expected = ["dismissed", "newest", "arrival"]
        assert _titles(state) == _stored_titles() == expected
        assert len(state._notification_log) == 3
        assert state._unread_count == 3
        assert frames == []


class TestAgentRoutes:
    def _app(self, state, *, internal_auth: bool = True, app_name: str = "") -> web.Application:
        @web.middleware
        async def _auth_marker(request, handler):
            if internal_auth:
                request["internal_auth"] = True
            if app_name:
                request["app"] = app_name
            return await handler(request)

        app = web.Application(middlewares=[_auth_marker])
        app["state"] = state
        app.router.add_post("/api/notifications/agent", api_notification_agent_push)
        app.router.add_post("/api/notifications/agent/dismiss", api_notification_agent_dismiss)
        return app

    @pytest.mark.asyncio
    async def test_publish_with_a_key_replaces_and_dismiss_removes(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        frames: list[tuple[str, dict]] = []
        monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: frames.append((kind, data)))
        async with TestClient(TestServer(self._app(state))) as client:
            for title in ("Poller down", "Poller still down"):
                resp = await client.post(
                    "/api/notifications/agent", json={"title": title, "key": "poller:ci"}
                )
                assert resp.status == 200
            assert _titles(state) == ["Poller still down"]
            held_ts = state._notification_log[0]["ts"]

            resp = await client.post("/api/notifications/agent/dismiss", json={"key": "poller:ci"})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "dismissed": True}
        assert state._notification_log == []
        assert _stored_titles() == []
        assert frames == [("notification_dismiss", {"ts": held_ts})]

    @pytest.mark.asyncio
    async def test_dismissing_an_unknown_key_reports_nothing_dismissed(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("kept", key="other"))
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/notifications/agent/dismiss", json={"key": "absent"})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "dismissed": False}
        assert _titles(state) == ["kept"]

    @pytest.mark.asyncio
    async def test_dismiss_never_reaches_an_app_note(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_bus.register_channel("mochi.alerts")
        await _push(
            state,
            NotificationPayload(
                source="app:mochi", channel="mochi.alerts", title="app note", body="", key="k"
            ),
        )
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/notifications/agent/dismiss", json={"key": "k"})
            assert (await resp.json())["dismissed"] is False
        assert _titles(state) == ["app note"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{}, {"key": ""}, {"key": "  "}, {"key": 3}])
    async def test_a_dismiss_without_a_usable_key_is_a_400(self, monkeypatch, tmp_path, body):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/notifications/agent/dismiss", json=body)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_dismiss_takes_the_same_longest_key_publish_takes(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        longest_key = ("watch-" * MAX_SHORT_STRING)[:MAX_SHORT_STRING]
        too_long_key = longest_key + "x"
        async with TestClient(TestServer(self._app(state))) as client:
            published = await client.post(
                "/api/notifications/agent", json={"title": "Poller down", "key": longest_key}
            )
            assert published.status == 200
            dismissed = await client.post(
                "/api/notifications/agent/dismiss", json={"key": longest_key}
            )
            assert await dismissed.json() == {"ok": True, "dismissed": True}

            refused_publish = await client.post(
                "/api/notifications/agent", json={"title": "Poller down", "key": too_long_key}
            )
            assert refused_publish.status == 400
            refused_dismiss = await client.post(
                "/api/notifications/agent/dismiss", json={"key": too_long_key}
            )
            assert refused_dismiss.status == 400
            assert (await refused_dismiss.json())["code"] == "invalid_key"
        assert _titles(state) == []

    @pytest.mark.asyncio
    async def test_a_non_string_key_on_publish_is_a_400(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/notifications/agent", json={"title": "t", "key": 3})
            assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("internal_auth", "app_name"), [(False, ""), (True, "some-app")])
    async def test_cookie_and_app_callers_cannot_dismiss(
        self, monkeypatch, tmp_path, internal_auth, app_name
    ):
        state = _make_state(monkeypatch, tmp_path)
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.messaging._sel", lambda: sel_mock)
        await _push(state, _agent_payload("kept", key="k"))
        app = self._app(state, internal_auth=internal_auth, app_name=app_name)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/notifications/agent/dismiss", json={"key": "k"})
            assert resp.status == 403
        assert _titles(state) == ["kept"]
        denied = [
            kw for _, kw in sel_mock.log_api_access.call_args_list if kw.get("outcome") == "denied"
        ]
        assert denied and denied[0]["operation"] == "notification_agent_dismiss"

    @pytest.mark.asyncio
    async def test_a_caller_whose_slot_is_gone_cannot_dismiss(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("kept", key="k"))
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent/dismiss",
                json={"key": "k"},
                headers={"X-Session-Key": "dashboard:chat-gone"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "caller_session_missing"
        assert _titles(state) == ["kept"]

    @pytest.mark.asyncio
    async def test_a_failed_rewrite_on_dismiss_changes_nothing_and_is_a_500(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        await _push(state, _agent_payload("before"))
        await _push(state, _agent_payload("kept", key="k"))
        await _push(state, _agent_payload("after"))
        frames: list[tuple[str, dict]] = []
        monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: frames.append((kind, data)))
        monkeypatch.setattr("kiro_crew.dashboard.state._rewrite_notifications", lambda notes: False)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/notifications/agent/dismiss", json={"key": "k"})
            assert resp.status == 500
        # Memory, the open views and disk still agree, so a retry finds the note.
        assert _titles(state) == ["before", "kept", "after"]
        assert frames == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "route", ["/api/notifications/agent", "/api/notifications/agent/dismiss"]
    )
    async def test_a_key_that_redaction_would_rewrite_is_a_400(self, monkeypatch, tmp_path, route):
        state = _make_state(monkeypatch, tmp_path)
        secret_like = "ghp_" + "a" * 36
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(route, json={"title": "t", "key": secret_like})
            assert resp.status == 400
            assert "credential" in (await resp.json())["error"]
        assert state._notification_log == []

    @pytest.mark.asyncio
    async def test_the_dismiss_route_is_internal_secret_only(self):
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS
        from kiro_crew.dashboard.token_auth import token_auth_middleware

        async def ok(_request):
            return web.Response(text="ok")

        secret = "test-secret-123"
        middleware = token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS, internal_secret=secret
        )

        def request(headers: dict) -> MagicMock:
            req = MagicMock(spec=web.Request)
            req.path = "/api/notifications/agent/dismiss"
            req.query = {}
            req.cookies = {}
            req.remote = "127.0.0.1"
            req.headers = headers
            req.method = "POST"
            return req

        assert (await middleware(request({"X-Internal-Secret": secret}), ok)).status == 200
        refused = await middleware(request({}), ok)
        assert refused.status in (401, 403)

    def test_the_gateway_serves_the_dismiss_route(self):
        from kiro_crew.dashboard import server

        app = web.Application()
        server._register_mcp_routes(app)
        paths = {route.resource.canonical for route in app.router.routes() if route.resource}
        assert "/api/notifications/agent/dismiss" in paths


class TestTools:
    @pytest.fixture
    def identified(self, monkeypatch):
        from kiro_crew import mcp_core

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-9")
        monkeypatch.setattr(mcp_core, "_vet_messaging_governance", lambda *a, **k: None)
        posted: list[tuple[str, dict]] = []
        replies: dict = {"value": {"ok": True, "dismissed": True}}

        def fake_post(path, payload):
            posted.append((path, payload))
            return replies["value"]

        monkeypatch.setattr(mcp_core, "_post", fake_post)
        return posted, replies

    def test_send_notification_forwards_the_key(self, identified):
        from kiro_crew import mcp_core

        posted, replies = identified
        replies["value"] = {"ok": True, "note": {}}
        mcp_core._call_tool_inner("send_notification", {"title": "t", "key": "cr:1"})
        assert posted == [("/api/notifications/agent", {"title": "t", "key": "cr:1"})]

    def test_dismiss_notification_posts_the_key(self, identified):
        from kiro_crew import mcp_core

        posted, _ = identified
        result = mcp_core._call_tool_inner("dismiss_notification", {"key": "poller:ci"})
        assert posted == [("/api/notifications/agent/dismiss", {"key": "poller:ci"})]
        assert "dismissed" in result and not result.startswith("Error")

    def test_dismiss_notification_says_when_nothing_held_the_key(self, identified):
        from kiro_crew import mcp_core

        _, replies = identified
        replies["value"] = {"ok": True, "dismissed": False}
        result = mcp_core._call_tool_inner("dismiss_notification", {"key": "absent"})
        assert "nothing to dismiss" in result

    def test_dismiss_notification_reports_a_gateway_failure_as_an_error(self, identified):
        from kiro_crew import mcp_core

        _, replies = identified
        replies["value"] = {"ok": False, "error": "boom"}
        assert mcp_core._call_tool_inner("dismiss_notification", {"key": "k"}).startswith("Error:")

    def test_dismiss_notification_fails_closed_without_a_verified_identity(self, monkeypatch):
        from kiro_crew import mcp_core

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        posted: list = []
        monkeypatch.setattr(mcp_core, "_post", lambda *a, **k: posted.append(a) or {"ok": True})
        result = mcp_core._call_tool_inner("dismiss_notification", {"key": "k"})
        assert "cannot verify caller identity" in result
        assert not posted

    def test_dismiss_notification_honours_a_messaging_denial(self, monkeypatch):
        from kiro_crew import mcp_core

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-9")
        seen: list = []
        monkeypatch.setattr(
            mcp_core,
            "_vet_messaging_governance",
            lambda *a, **k: seen.append((a, k)) or "messaging disabled",
        )
        posted: list = []
        monkeypatch.setattr(mcp_core, "_post", lambda *a, **k: posted.append(a) or {"ok": True})
        result = mcp_core._call_tool_inner("dismiss_notification", {"key": "k"})
        assert result == "Error: messaging disabled"
        assert not posted
        assert seen[0][1] == {"tool_name": "dismiss_notification", "fail_closed": True}

    def test_a_channel_agent_cannot_dismiss(self, monkeypatch):
        from kiro_crew import mcp_core

        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "channel:C1:agent-1")
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "channel:C1:agent-1")
        monkeypatch.setattr(mcp_core, "_vet_messaging_governance", lambda *a, **k: None)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
        posted: list = []
        monkeypatch.setattr(mcp_core, "_post", lambda *a, **k: posted.append(a) or {"ok": True})
        result = mcp_core._call_tool_inner("dismiss_notification", {"key": "k"})
        assert result.startswith("Error:") and "channel agents" in result
        assert not posted

    def test_a_dismiss_without_a_key_is_refused_before_the_tool_runs(self, monkeypatch):
        from kiro_crew import mcp_core

        posted: list = []
        monkeypatch.setattr(mcp_core, "_post", lambda *a, **k: posted.append(a) or {"ok": True})
        assert mcp_core._call_tool("dismiss_notification", {}).startswith("Error:")
        assert not posted

    def test_the_tool_is_advertised_with_a_required_key(self):
        from kiro_crew.mcp_tools import build_tool_list

        tools = {tool["name"]: tool for tool in build_tool_list()}
        assert tools["dismiss_notification"]["inputSchema"]["required"] == ["key"]
        assert "key" in tools["send_notification"]["inputSchema"]["properties"]


def test_a_rewrite_that_fails_part_way_leaves_the_previous_file(monkeypatch, tmp_path):
    """A full disk mid-write must not truncate history the caller still holds."""
    from kiro_crew import atomic_write as atomic_write_module
    from kiro_crew.dashboard.state_parts.notification_log import _rewrite_notifications

    state = _make_state(monkeypatch, tmp_path)
    state.notification_bus.push(_agent_payload("kept"))
    before = (tmp_path / "notifications.jsonl").read_bytes()

    def disk_full(fd, data, path):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(atomic_write_module, "_write_all", disk_full)
    assert _rewrite_notifications([{"ts": "1", "title": "other", "body": ""}]) is False
    assert (tmp_path / "notifications.jsonl").read_bytes() == before


def test_a_rewrite_keeps_the_unservable_lines_the_file_preserves(monkeypatch, tmp_path):
    """The rewrite a replacement or a dismiss makes keeps damaged lines, bytes intact."""
    state = _make_state(monkeypatch, tmp_path)
    state.notification_bus.push(_agent_payload("one", key="k"))
    path = tmp_path / "notifications.jsonl"
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write("{torn fragment\r\n")
        handle.write("[1, 2]")  # no terminator: must not glue onto the next row
    state.notification_bus.push(_agent_payload("two", key="k"))
    raw = path.read_bytes().decode("utf-8")
    assert raw.startswith("{torn fragment\r\n[1, 2]\n")
    assert [note["title"] for note in _load_notifications()] == ["two"]


def test_the_stored_file_never_holds_two_notes_for_one_key(monkeypatch, tmp_path):
    """A reload after replacements serves exactly what memory held."""
    state = _make_state(monkeypatch, tmp_path)
    for title in ("a", "b", "c"):
        state.notification_bus.push(_agent_payload(title, key="k"))
    rows = [
        json.loads(line) for line in (tmp_path / "notifications.jsonl").read_text().splitlines()
    ]
    assert [row["title"] for row in rows] == ["c"]
