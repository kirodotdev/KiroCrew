"""Unit tests for ``slack.auto_link_sessions``: the first-message Slack thread.

Covers the eligibility predicate, the send-time hook, the helper the manual
Connect to Slack button shares with it, and the config fields on both the
loader and the Slack settings endpoints.
"""

from __future__ import annotations

import asyncio
import gc
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_slack
from kiro_crew.dashboard.chat_slack import (
    SlackLinkError,
    auto_link_eligible,
    link_slot_to_slack,
    maybe_auto_link_slack,
)
from kiro_crew.dashboard.state import SlotOrigin


def _slack_state(tmp_path, monkeypatch, *, owner: str | None = "U123"):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.slack_client = MagicMock()
    state.slack_client.open_dm = AsyncMock(return_value="D_OWNER")
    state.slack_client.post_message = AsyncMock(return_value="ts_anchor")
    state.owner_id = owner
    state.push_slots_update = MagicMock()
    return state


def _user_slot(state, name: str = "s1", **kwargs):
    slot = state.get_or_create_slot(name, origin=SlotOrigin.USER, **kwargs)
    slot.append("user", "first prompt: rotate the api key")
    slot.drain()
    return slot


def _assert_no_link_lock(state) -> None:
    """The weak registry holds a lock only while something still references it.

    A traceback the test still holds (``pytest.raises`` keeps it) references the
    frame that held the lock, so drop cycles first.
    """
    gc.collect()
    assert list(state._slack_link_locks.items()) == []


def _settings(monkeypatch, enabled: bool, channel: str = "") -> None:
    monkeypatch.setattr(chat_slack, "_read_auto_link_settings", lambda: (enabled, channel, False))


class TestEligibility:
    def test_fresh_user_slot_with_one_prompt_qualifies(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        assert auto_link_eligible(state, slot) is True

    @pytest.mark.parametrize("origin", [SlotOrigin.CRON, SlotOrigin.APP, SlotOrigin.SYSTEM, ""])
    def test_non_dashboard_origins_never_qualify(self, tmp_path, monkeypatch, origin):
        state = _slack_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("bg", origin=origin or None)
        slot.append("user", "hello")
        slot.drain()
        assert auto_link_eligible(state, slot) is False

    def test_channel_born_slot_never_qualifies(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state, channel_origin=True)
        assert auto_link_eligible(state, slot) is False

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_restricted_memory_modes_never_qualify(self, tmp_path, monkeypatch, mode):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state, memory_mode=mode)
        assert auto_link_eligible(state, slot) is False

    def test_second_message_is_not_first(self, tmp_path, monkeypatch):
        """A session that existed before the setting was turned on is left alone,
        and a failed first attempt is not retried on the next send."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        slot.append("assistant", "done")
        slot.append("user", "second")
        slot.drain()
        assert auto_link_eligible(state, slot) is False

    def test_disk_history_is_not_first(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        slot._disk_older_count = 3
        assert auto_link_eligible(state, slot) is False

    def test_agent_created_session_never_qualifies(self, tmp_path, monkeypatch):
        """Session control stamps USER on slots an agent opens for its own work
        and records the creator; the marker, not the origin, decides."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        slot._created_by = "dashboard:parent-slot"
        assert auto_link_eligible(state, slot) is False

    def test_already_linked_slot_does_not_qualify(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        state.link_slack(slot.key, "ts_manual", "C_MANUAL")
        assert auto_link_eligible(state, slot) is False


class TestHook:
    @pytest.mark.asyncio
    async def test_off_by_default_opens_nothing(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=False)
        assert await maybe_auto_link_slack(state, slot) is False
        state.slack_client.post_message.assert_not_awaited()
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == (None, None)

    @pytest.mark.asyncio
    async def test_on_opens_owner_dm_thread_without_backfill(self, tmp_path, monkeypatch):
        """The default target is the owner DM, and the thread is NOT backfilled:
        the turn that follows echoes the one message the slot holds."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        spawned: list = []
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: spawned.append(a))

        assert await maybe_auto_link_slack(state, slot) is True

        state.slack_client.open_dm.assert_awaited_once_with("U123")
        state.slack_client.post_message.assert_awaited_once()
        channel, text = state.slack_client.post_message.await_args.args[:2]
        assert channel == "D_OWNER"
        assert "first prompt: rotate the api key" in text
        assert chat_slack._AUTO_LINK_ANCHOR_SUFFIX in text
        assert spawned == []
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == ("ts_anchor", "D_OWNER")
        assert slot._slack_linked is True

    @pytest.mark.asyncio
    async def test_configured_channel_is_used_verbatim(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True, channel="C0TEAM")
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)

        assert await maybe_auto_link_slack(state, slot) is True

        state.slack_client.open_dm.assert_not_awaited()
        assert state.slack_client.post_message.await_args.args[0] == "C0TEAM"
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == ("ts_anchor", "C0TEAM")

    @pytest.mark.asyncio
    async def test_ineligible_slot_never_reads_settings(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("cron", origin=SlotOrigin.CRON)
        slot.append("user", "tick")
        slot.drain()
        reads: list = []

        def _record_settings_read():
            reads.append(1)
            return (True, "", False)

        monkeypatch.setattr(chat_slack, "_read_auto_link_settings", _record_settings_read)
        assert await maybe_auto_link_slack(state, slot) is False
        assert reads == []

    @pytest.mark.asyncio
    async def test_slack_failure_is_swallowed_and_leaves_no_link(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        state.slack_client.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
        assert await maybe_auto_link_slack(state, slot) is False
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == (None, None)

    @pytest.mark.asyncio
    async def test_link_is_flushed_before_the_send_proceeds(self, tmp_path, monkeypatch):
        """The map's writer is debounced; the link must be durable before the
        turn mirrors into the thread, or a crash strands the thread."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)
        flushed: list = []

        async def _aflush():
            flushed.append(state.sessions.get_slack_link(f"dashboard:{slot.key}"))

        state.sessions.aflush = _aflush
        assert await maybe_auto_link_slack(state, slot) is True
        # Flushed exactly once, and the link was already in the map when it ran.
        assert flushed == [("ts_anchor", "D_OWNER")]

    @pytest.mark.asyncio
    async def test_governance_denial_posts_nothing(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(
            chat_slack, "vet_and_audit", lambda *a, **k: SimpleNamespace(permitted=False)
        )
        assert await maybe_auto_link_slack(state, slot) is False
        state.slack_client.open_dm.assert_not_awaited()
        state.slack_client.post_message.assert_not_awaited()
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == (None, None)

    @pytest.mark.asyncio
    async def test_governance_check_names_the_operation(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)
        calls: list = []

        def _vet(scope, item, **kw):
            calls.append((scope, item, kw.get("tool_name"), kw.get("fail_closed")))
            return SimpleNamespace(permitted=True)

        monkeypatch.setattr(chat_slack, "vet_and_audit", _vet)
        assert await maybe_auto_link_slack(state, slot) is True
        assert calls == [("channels", "slack", "chat.slack_auto_link", True)]

    @pytest.mark.asyncio
    async def test_no_slack_client_is_a_quiet_no(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        state.slack_client = None
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        assert await maybe_auto_link_slack(state, slot) is False

    @pytest.mark.asyncio
    async def test_manual_connect_during_config_hop_wins(self, tmp_path, monkeypatch):
        """The settings read yields to the loop; a link made meanwhile stands."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)

        def _read_and_link():
            state.link_slack(slot.key, "ts_manual", "C_MANUAL")
            return (True, "", False)

        monkeypatch.setattr(chat_slack, "_read_auto_link_settings", _read_and_link)
        assert await maybe_auto_link_slack(state, slot) is False
        state.slack_client.post_message.assert_not_awaited()
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == ("ts_manual", "C_MANUAL")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("first_message", "cc_provider"),
        [("/model", False), ("/compact now", False), ("/help", True)],
    )
    async def test_first_harness_slash_command_does_not_auto_link(
        self, tmp_path, monkeypatch, first_message, cc_provider
    ):
        """A slash command never reaches the mirror, so no thread is opened for it."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.append("user", first_message)
        slot.drain()
        monkeypatch.setattr(chat_slack, "_read_auto_link_settings", lambda: (True, "", cc_provider))

        assert await maybe_auto_link_slack(state, slot) is False
        state.slack_client.post_message.assert_not_awaited()
        assert state.sessions.get_slack_link("dashboard:s1") == (None, None)

    @pytest.mark.asyncio
    async def test_cancelled_wait_keeps_link_tracked_and_backfills_once(
        self, tmp_path, monkeypatch
    ):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        posting = asyncio.Event()
        release = asyncio.Event()
        spawned: list = []
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: spawned.append(a))

        async def _blocked_post(*_a, **_k):
            posting.set()
            await release.wait()
            return "ts_late"

        state.slack_client.post_message = AsyncMock(side_effect=_blocked_post)
        awaiting = asyncio.create_task(maybe_auto_link_slack(state, slot))
        await posting.wait()
        link_task = next(task for task in state._background_tasks if task is not awaiting)

        awaiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await awaiting
        assert link_task in state._background_tasks

        release.set()
        await link_task
        await asyncio.sleep(0)

        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == ("ts_late", "D_OWNER")
        assert spawned == [(state, slot, "D_OWNER", "ts_late")]

    @pytest.mark.asyncio
    async def test_late_link_waits_for_unmirrored_turn_before_backfill(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "AUTO_LINK_HOLD_SECS", 0.01)
        spawned: list = []
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: spawned.append(a))
        finish_turn = asyncio.Event()

        async def _running_turn():
            await finish_turn.wait()

        turn_task = asyncio.create_task(_running_turn())
        slot.task = turn_task

        async def _slow_post(*_a, **_k):
            await asyncio.sleep(0.05)
            return "ts_late"

        state.slack_client.post_message = AsyncMock(side_effect=_slow_post)

        assert await maybe_auto_link_slack(state, slot) is False
        link_task = next(task for task in state._background_tasks if task is not turn_task)
        await link_task
        await asyncio.sleep(0)
        assert spawned == []

        finish_turn.set()
        await turn_task
        await asyncio.sleep(0)
        slot.task = None
        assert spawned == [(state, slot, "D_OWNER", "ts_late")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("withdraw", ["pause", "unlink"])
    async def test_late_backfill_is_withdrawn_by_a_pause_or_unlink(
        self, tmp_path, monkeypatch, withdraw
    ):
        """The replay waits for the turn; a pause or unlink in that wait is the
        person's latest word on the thread, so nothing is posted."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "AUTO_LINK_HOLD_SECS", 0.01)
        spawned: list = []
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: spawned.append(a))
        finish_turn = asyncio.Event()
        turn_task = asyncio.create_task(finish_turn.wait())
        slot.task = turn_task

        async def _slow_post(*_a, **_k):
            await asyncio.sleep(0.05)
            return "ts_late"

        state.slack_client.post_message = AsyncMock(side_effect=_slow_post)
        assert await maybe_auto_link_slack(state, slot) is False
        link_task = next(task for task in state._background_tasks if task is not turn_task)
        await link_task
        await asyncio.sleep(0)
        key = f"dashboard:{slot.key}"
        assert state.sessions.get_slack_link(key) == ("ts_late", "D_OWNER")
        if withdraw == "pause":
            state.sessions.is_slack_paused = lambda k: k == key
        else:
            state.sessions.clear_slack_link(key)

        finish_turn.set()
        await turn_task
        await asyncio.sleep(0)
        assert spawned == []

    @pytest.mark.asyncio
    async def test_late_link_skips_backfill_for_live_mirrored_turn(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "AUTO_LINK_HOLD_SECS", 0.01)
        spawned: list = []
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: spawned.append(a))
        finish_turn = asyncio.Event()

        async def _running_turn():
            await finish_turn.wait()

        turn_task = asyncio.create_task(_running_turn())
        slot.task = turn_task
        slot.note_slack_live_mirror("ts_late", echoed=True)

        async def _slow_post(*_a, **_k):
            await asyncio.sleep(0.05)
            return "ts_late"

        state.slack_client.post_message = AsyncMock(side_effect=_slow_post)

        assert await maybe_auto_link_slack(state, slot) is False
        link_task = next(task for task in state._background_tasks if task is not turn_task)
        await link_task
        await asyncio.sleep(0)
        assert spawned == []

        finish_turn.set()
        await turn_task
        await asyncio.sleep(0)
        slot.task = None
        assert spawned == []

    @pytest.mark.asyncio
    async def test_late_link_replays_the_turn_a_live_successor_follows(self, tmp_path, monkeypatch):
        """The first turn ends and a queued second turn starts before the slow link
        lands. The second turn finds the link and is mirrored live; the replay
        still owes the thread the first turn, and only that."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "AUTO_LINK_HOLD_SECS", 0.01)
        windows: list = []
        monkeypatch.setattr(
            chat_slack, "_spawn_slack_backfill", lambda *a, window=None: windows.append(window)
        )
        release_post = asyncio.Event()

        async def _slow_post(*_a, **_k):
            await release_post.wait()
            return "ts_late"

        state.slack_client.post_message = AsyncMock(side_effect=_slow_post)
        first_done = asyncio.Event()
        first_task = asyncio.create_task(first_done.wait())
        slot.task = first_task

        assert await maybe_auto_link_slack(state, slot) is False
        link_task = next(t for t in state._background_tasks if t is not first_task)

        slot.append("assistant", "first reply")
        slot.drain()
        first_done.set()
        await first_task
        slot.append("user", "second prompt")
        slot.drain()
        second_done = asyncio.Event()
        second_task = asyncio.create_task(second_done.wait())
        slot.task = second_task

        release_post.set()
        await link_task
        await asyncio.sleep(0)
        # The runner's start-of-turn read for the second turn finds the link.
        slot.note_slack_live_mirror("ts_late", echoed=True)
        assert windows == []

        second_done.set()
        await second_task
        await asyncio.sleep(0)
        assert [[row["content"] for row in w] for w in windows] == [
            ["first prompt: rotate the api key", "first reply"]
        ]

    @pytest.mark.asyncio
    async def test_late_link_waits_for_a_successor_dispatched_before_its_callback(
        self, tmp_path, monkeypatch
    ):
        """The link lands mid-turn and the queued turn is dispatched before the first
        turn's done callback runs: the replay waits for that successor's own link
        read and leaves the live-mirrored successor out."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "AUTO_LINK_HOLD_SECS", 0.01)
        windows: list = []
        monkeypatch.setattr(
            chat_slack, "_spawn_slack_backfill", lambda *a, window=None: windows.append(window)
        )
        second_done = asyncio.Event()
        first_done = asyncio.Event()

        async def _first_turn():
            await first_done.wait()
            slot.append("assistant", "first reply")
            slot.append("user", "second prompt")
            slot.drain()
            # The queue drain dispatches the next turn from inside the ending one.
            slot.task = asyncio.create_task(second_done.wait())

        first_task = asyncio.create_task(_first_turn())
        slot.task = first_task

        async def _slow_post(*_a, **_k):
            await asyncio.sleep(0.05)
            return "ts_late"

        state.slack_client.post_message = AsyncMock(side_effect=_slow_post)
        assert await maybe_auto_link_slack(state, slot) is False
        link_task = next(t for t in state._background_tasks if t is not first_task)
        await link_task
        await asyncio.sleep(0)

        first_done.set()
        await first_task
        await asyncio.sleep(0)
        second_task = slot.task
        assert second_task is not first_task
        assert windows == []
        slot.note_slack_live_mirror("ts_late", echoed=True)

        second_done.set()
        await second_task
        await asyncio.sleep(0)
        assert [[row["content"] for row in w] for w in windows] == [
            ["first prompt: rotate the api key", "first reply"]
        ]

    def test_a_live_synthetic_turn_leaves_the_rows_before_it_owed(self, tmp_path, monkeypatch):
        """A synthetic turn echoes no user row of its own, so the newest user row
        belongs to an earlier, unmirrored turn and is still replayed; a turn
        mirrored into ANOTHER thread does not cut this one's replay."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        slot.append("assistant", "first reply")
        slot.drain()
        slot.note_slack_live_mirror("ts_other", echoed=True)
        assert [r["content"] for r in chat_slack._rows_owed_to_thread(slot, "ts_late")] == [
            "first prompt: rotate the api key",
            "first reply",
        ]
        slot._slack_live_since = None
        slot.note_slack_live_mirror("ts_late", echoed=False)
        slot.append("assistant", "live reply")
        slot.drain()
        assert [r["content"] for r in chat_slack._rows_owed_to_thread(slot, "ts_late")] == [
            "first prompt: rotate the api key",
            "first reply",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("echoed", [True, False])
    async def test_a_fast_link_replays_only_a_prompt_whose_echo_failed(
        self, tmp_path, monkeypatch, echoed
    ):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        state.link_slack(slot.key, "ts_fast", "D_OWNER")
        windows: list = []
        monkeypatch.setattr(
            chat_slack, "_spawn_slack_backfill", lambda *a, window=None: windows.append(window)
        )
        finish_turn = asyncio.Event()
        turn_task = asyncio.create_task(finish_turn.wait())
        slot.task = turn_task

        chat_slack.replay_unechoed_first_turn(state, slot)
        # The runner's start-of-turn read finds the link and reports its echo.
        slot.note_slack_live_mirror("ts_fast", echoed=echoed)
        finish_turn.set()
        await turn_task
        await asyncio.sleep(0)

        owed = [] if echoed else [["first prompt: rotate the api key"]]
        assert [[row["content"] for row in w] for w in windows] == owed

    @pytest.mark.asyncio
    async def test_slow_link_finishes_in_background_and_backfills(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "AUTO_LINK_HOLD_SECS", 0.01)
        spawned: list = []
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: spawned.append(a))

        async def _slow_post(*_a, **_k):
            await asyncio.sleep(0.05)
            return "ts_late"

        state.slack_client.post_message = AsyncMock(side_effect=_slow_post)

        assert await maybe_auto_link_slack(state, slot) is False
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == (None, None)
        late_task = next(iter(state._background_tasks))
        await late_task
        await asyncio.sleep(0)

        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == ("ts_late", "D_OWNER")
        assert spawned == [(state, slot, "D_OWNER", "ts_late")]


class TestSharedHelper:
    @pytest.mark.asyncio
    async def test_manual_path_keeps_backfill_and_its_anchor(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        spawned: list = []
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: spawned.append(a))
        result = await link_slot_to_slack(state, slot, governed=False)
        assert result == {"ok": True, "thread_ts": "ts_anchor", "channel": "D_OWNER"}
        assert len(spawned) == 1
        text = state.slack_client.post_message.await_args.args[1]
        assert chat_slack._MANUAL_LINK_ANCHOR_SUFFIX in text

    @pytest.mark.asyncio
    async def test_a_late_post_still_records_its_thread(self, tmp_path, monkeypatch):
        """A late Slack response is awaited and its anchor is linked."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)

        async def _slow_post(*_a, **_k):
            await asyncio.sleep(0.05)
            return "ts_late"

        state.slack_client.post_message = AsyncMock(side_effect=_slow_post)
        result = await link_slot_to_slack(state, slot, governed=False)
        assert result["thread_ts"] == "ts_late"
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == ("ts_late", "D_OWNER")

    @pytest.mark.asyncio
    async def test_governance_denial_is_a_403(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        monkeypatch.setattr(
            chat_slack, "vet_and_audit", lambda *a, **k: SimpleNamespace(permitted=False)
        )
        with pytest.raises(SlackLinkError) as exc:
            await link_slot_to_slack(state, slot, governed=True)
        assert exc.value.status == 403
        state.slack_client.open_dm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_governance_denial_on_existing_link_posts_nothing(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        state.link_slack(slot.key, "ts_existing", "C_EXISTING")
        monkeypatch.setattr(
            chat_slack, "vet_and_audit", lambda *a, **k: SimpleNamespace(permitted=False)
        )

        with pytest.raises(SlackLinkError) as exc:
            await link_slot_to_slack(state, slot, governed=True)

        assert exc.value.code == "channel_not_permitted"
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_anchor_escapes_slack_mentions_after_redaction(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.append("user", "notify <!channel> about this")
        slot.drain()
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)

        await link_slot_to_slack(state, slot, governed=False)

        anchor = state.slack_client.post_message.await_args.args[1]
        assert "<!channel>" not in anchor
        assert "&lt;!channel&gt;" in anchor

    @pytest.mark.asyncio
    async def test_manual_path_skips_governance(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        vet = MagicMock(return_value=SimpleNamespace(permitted=False))
        monkeypatch.setattr(chat_slack, "vet_and_audit", vet)
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)

        result = await link_slot_to_slack(state, slot, governed=False)

        assert result["ok"] is True
        vet.assert_not_called()
        state.slack_client.post_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_links_post_one_anchor(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)

        async def _slow_post(*_a, **_k):
            await asyncio.sleep(0.02)
            return "ts_once"

        state.slack_client.post_message = AsyncMock(side_effect=_slow_post)
        first, second = await asyncio.gather(
            link_slot_to_slack(state, slot, governed=False),
            link_slot_to_slack(state, slot, governed=False),
        )

        assert state.slack_client.post_message.await_count == 2
        assert state.slack_client.post_message.await_args_list[1].args == (
            "D_OWNER",
            "🔗 Session linked from dashboard — continuing here.",
            "ts_once",
        )
        assert "already_linked" not in first
        assert second["already_linked"] is True
        _assert_no_link_lock(state)

    @pytest.mark.asyncio
    async def test_link_entry_exists_only_while_attempt_is_live(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)
        posting = asyncio.Event()
        release = asyncio.Event()

        async def _blocked_post(*_a, **_k):
            posting.set()
            await release.wait()
            return "ts_blocked"

        state.slack_client.post_message = AsyncMock(side_effect=_blocked_post)
        task = asyncio.create_task(link_slot_to_slack(state, slot, governed=False))
        await posting.wait()

        lock = state._slack_link_locks[f"dashboard:{slot.key}"]
        assert lock.locked()
        del lock

        release.set()
        await task
        _assert_no_link_lock(state)

    @pytest.mark.asyncio
    async def test_slot_closed_during_anchor_is_not_linked(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        anchor_posting = asyncio.Event()
        release_anchor = asyncio.Event()

        async def _post(_channel, _text, thread_ts=None):
            if thread_ts:
                return "ts_note"
            anchor_posting.set()
            await release_anchor.wait()
            return "ts_closed"

        state.slack_client.post_message = AsyncMock(side_effect=_post)
        task = asyncio.create_task(link_slot_to_slack(state, slot, governed=False))
        await anchor_posting.wait()
        state._slots.pop(slot.key, None)
        release_anchor.set()

        with pytest.raises(SlackLinkError) as exc:
            await task

        assert exc.value.status == 409
        assert exc.value.code == "slot_closed"
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == (None, None)
        assert state.slack_client.post_message.await_args_list[-1].args == (
            "D_OWNER",
            "\U0001f50c _Unlinked from dashboard — the session was closed._",
            "ts_closed",
        )
        # The finished task's traceback still pins the frame that held the lock,
        # so the registry entry outlives this test; what must hold is release.
        lingering = state._slack_link_locks.get(f"dashboard:{slot.key}")
        assert lingering is None or not lingering.locked()

    @pytest.mark.asyncio
    async def test_closed_slot_does_not_post_into_existing_thread(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        state._slots.pop(slot.key, None)

        with pytest.raises(SlackLinkError) as exc:
            await link_slot_to_slack(
                state, slot, channel="C_EXISTING", existing_thread="ts_existing", governed=False
            )

        assert exc.value.code == "slot_closed"
        state.slack_client.post_message.assert_not_awaited()
        del exc
        _assert_no_link_lock(state)

    @pytest.mark.asyncio
    async def test_refusals_carry_the_api_status(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch, owner=None)
        slot = _user_slot(state)
        with pytest.raises(SlackLinkError) as exc:
            await link_slot_to_slack(state, slot, governed=False)
        assert exc.value.status == 500
        state.slack_client = None
        with pytest.raises(SlackLinkError) as exc:
            await link_slot_to_slack(state, slot, governed=False)
        assert exc.value.status == 503


class TestConfig:
    def test_defaults_are_off_and_dm(self):
        from kiro_crew.config.section_builders import _build_slack_config

        cfg = _build_slack_config({})
        assert cfg.auto_link_sessions is False
        assert cfg.auto_link_channel == ""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("C0TEAM", "C0TEAM"),
            ("G0GROUP", "G0GROUP"),
            (" C0TEAM ", "C0TEAM"),
            ("dm", ""),
            ("", ""),
            ("#general", ""),
            ("D0DIRECT", ""),
            ("c0lower", ""),
            (123, ""),
            (None, ""),
            ("C" + "A" * 59, ""),
        ],
    )
    def test_channel_coercion_fails_closed_to_dm(self, raw, expected):
        from kiro_crew.config.section_builders import _build_slack_config

        cfg = _build_slack_config({"auto_link_sessions": True, "auto_link_channel": raw})
        assert cfg.auto_link_channel == expected

    def test_enabled_must_be_a_real_bool(self):
        from kiro_crew.config.section_builders import _build_slack_config

        assert _build_slack_config({"auto_link_sessions": "true"}).auto_link_sessions is False
        assert _build_slack_config({"auto_link_sessions": 1}).auto_link_sessions is False
        assert _build_slack_config({"auto_link_sessions": True}).auto_link_sessions is True


def _config_app(tmp_path, monkeypatch):
    import kiro_crew.dashboard.handlers.messaging as mod
    from kiro_crew.config import loader

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"slack": {"command": "kirocrew"}}', encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.delenv("KIROCREW_OWNER_ID", raising=False)
    return mod, cfg


class TestSettingsEndpoint:
    def test_save_round_trips_both_fields(self, tmp_path, monkeypatch):
        mod, cfg = _config_app(tmp_path, monkeypatch)
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def _put(payload):
            app = web.Application()
            app.router.add_put("/api/slack/config", mod.api_slack_config_save)
            async with TestClient(TestServer(app)) as client:
                resp = await client.put("/api/slack/config", json=payload)
                return resp.status, await resp.json()

        status, body = asyncio.run(
            _put({"auto_link_sessions": True, "auto_link_channel": "C0TEAM"})
        )
        assert status == 200
        # Both apply live: neither field is boot-read.
        assert body["restart_required"] is False
        saved = json.loads(cfg.read_text(encoding="utf-8"))["slack"]
        assert saved["auto_link_sessions"] is True
        assert saved["auto_link_channel"] == "C0TEAM"

        status, _ = asyncio.run(_put({"auto_link_channel": "dm"}))
        assert status == 200
        assert json.loads(cfg.read_text(encoding="utf-8"))["slack"]["auto_link_channel"] == ""

    @pytest.mark.parametrize(
        "payload",
        [
            {"auto_link_sessions": "yes"},
            {"auto_link_channel": "#general"},
            {"auto_link_channel": "D0DIRECT"},
            {"auto_link_channel": 42},
        ],
    )
    def test_save_refuses_bad_values(self, tmp_path, monkeypatch, payload):
        mod, cfg = _config_app(tmp_path, monkeypatch)
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def _put():
            app = web.Application()
            app.router.add_put("/api/slack/config", mod.api_slack_config_save)
            async with TestClient(TestServer(app)) as client:
                resp = await client.put("/api/slack/config", json=payload)
                return resp.status

        assert asyncio.run(_put()) == 400
        assert "auto_link" not in cfg.read_text(encoding="utf-8")

    def test_get_exposes_both_fields(self, tmp_path, monkeypatch):
        mod, cfg = _config_app(tmp_path, monkeypatch)
        cfg.write_text(
            '{"slack": {"auto_link_sessions": true, "auto_link_channel": "C0TEAM"}}',
            encoding="utf-8",
        )
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def _get():
            app = web.Application()
            app["state"] = MagicMock()
            app.router.add_get("/api/slack/config", mod.api_slack_config_get)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/slack/config")
                return await resp.json()

        body = asyncio.run(_get())
        assert body["auto_link_sessions"] is True
        assert body["auto_link_channel"] == "C0TEAM"


class TestSendHandlerHook:
    """The send handler links BEFORE dispatching the turn, and only for a person's own send."""

    @pytest.mark.asyncio
    async def test_link_exists_when_the_turn_starts(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        state = _slack_state(tmp_path, monkeypatch)
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
        seen: dict = {}

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            # The runner's own echo reads the link at turn start; it must already be there.
            seen["link"] = st.sessions.get_slack_link(f"dashboard:{sl.key}")
            sl.append("assistant", "reply")
            sl.append("done", "", "done", broadcast=False)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"message": "hello", "slot": "s1"})
            await resp.text()
        assert seen["link"] == ("ts_anchor", "D_OWNER")
        state.slack_client.post_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_first_send_queues_behind_link_admission(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        state = _slack_state(tmp_path, monkeypatch)
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(
            chat_slack, "vet_and_audit", lambda *a, **k: SimpleNamespace(permitted=True)
        )
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
        posting = asyncio.Event()
        release = asyncio.Event()
        dispatches: list[str] = []

        async def _blocked_post(*_a, **_k):
            posting.set()
            await release.wait()
            return "ts_anchor"

        async def _run(st, sl, msg, **_kw):
            dispatches.append(msg)
            sl.append("assistant", "reply")
            sl.append("done", "", "done", broadcast=False)

        state.slack_client.post_message = AsyncMock(side_effect=_blocked_post)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", _run)
        async with TestClient(TestServer(_make_app(state))) as client:
            first_request = asyncio.create_task(
                client.post("/api/chat", json={"message": "first", "slot": "s1"})
            )
            await posting.wait()

            second = await client.post("/api/chat", json={"message": "second", "slot": "s1"})
            second_body = await second.json()
            assert second_body["queued"] is True
            assert second_body["queue_id"]

            release.set()
            first = await first_request
            await first.text()

        assert dispatches == ["first"]
        assert [entry["content"] for entry in state.get_slot("s1")._queue] == ["second"]
        assert state.get_slot("s1")._turn_admission_reserved is False

    @pytest.mark.asyncio
    async def test_slot_closed_during_link_is_not_dispatched(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        state = _slack_state(tmp_path, monkeypatch)
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
        run_chat = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat)

        async def _close_slot(st, sl):
            st._slots.pop(sl.key, None)
            return False

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.maybe_auto_link_slack", _close_slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"message": "hello", "slot": "s1"})
            body = await resp.json()

        assert resp.status == 409
        assert body == {"error": "session closed", "code": "slot_closed"}
        run_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_during_link_hold_keeps_the_turn_from_starting(self, tmp_path, monkeypatch):
        """A Stop pressed while the send waits for the link finds no task to
        cancel; the send sees it after the hold and does not dispatch."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        state = _slack_state(tmp_path, monkeypatch)
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        run_chat = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat)
        holding = asyncio.Event()
        release = asyncio.Event()

        async def _hold(_st, _sl):
            holding.set()
            await release.wait()
            return False

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.maybe_auto_link_slack", _hold)
        async with TestClient(TestServer(_make_app(state))) as client:
            send = asyncio.create_task(
                client.post("/api/chat?ws=1", json={"message": "hello", "slot": "s1"})
            )
            await holding.wait()
            stop = await client.post("/api/chat/slots/s1/stop")
            assert stop.status == 200
            release.set()
            resp = await send
            body = await resp.json()

        assert resp.status == 200
        assert body["stopped"] is True
        run_chat.assert_not_awaited()
        slot = state.get_slot("s1")
        assert slot.task is None
        assert slot._turn_admission_reserved is False

    @pytest.mark.asyncio
    async def test_stop_during_link_hold_still_starts_a_send_queued_behind_it(
        self, tmp_path, monkeypatch
    ):
        """As at the end of a stopped turn, a follow-up queued during the hold runs."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        state = _slack_state(tmp_path, monkeypatch)
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        started: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._start_next_queued_turn",
            AsyncMock(side_effect=lambda st, sl, **_k: started.append(sl._queue[0]["content"])),
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
        holding = asyncio.Event()
        release = asyncio.Event()

        async def _hold(_st, _sl):
            holding.set()
            await release.wait()
            return False

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.maybe_auto_link_slack", _hold)
        async with TestClient(TestServer(_make_app(state))) as client:
            send = asyncio.create_task(
                client.post("/api/chat?ws=1", json={"message": "first", "slot": "s1"})
            )
            await holding.wait()
            queued = await client.post("/api/chat?ws=1", json={"message": "second", "slot": "s1"})
            assert (await queued.json()).get("queued") is True
            await client.post("/api/chat/slots/s1/stop")
            release.set()
            body = await (await send).json()

        assert body["stopped"] is True
        assert started == ["second"]

    @pytest.mark.asyncio
    async def test_a_fast_link_arms_the_echo_replay_after_dispatch(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        state = _slack_state(tmp_path, monkeypatch)
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.maybe_auto_link_slack", AsyncMock(return_value=True)
        )
        armed: list = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.replay_unechoed_first_turn",
            lambda _st, sl: armed.append(sl.task),
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "hello", "slot": "s1"})
            await resp.json()

        assert resp.status == 200
        assert len(armed) == 1 and armed[0] is not None

    @pytest.mark.asyncio
    async def test_app_token_send_does_not_link(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        state = _slack_state(tmp_path, monkeypatch)
        state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())

        async def fake_run_chat(st, sl, msg, **_kw):
            sl.append("assistant", "reply")
            sl.append("done", "", "done", broadcast=False)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._app_may_send_to_slot",
            AsyncMock(return_value=True),
        )
        app = _make_app(state)

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "observer-app"
            return await handler(request)

        app.middlewares.insert(0, _as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat", json={"message": "hello", "slot": "s1"})
            await resp.text()
        # The send itself went through; only the link was withheld.
        assert resp.status == 200
        assert any(m.get("role") == "assistant" for m in state.get_slot("s1").messages)
        state.slack_client.post_message.assert_not_awaited()
        assert state.sessions.get_slack_link("dashboard:s1") == (None, None)


class TestLiveMirrorBoundaryFromTheRunner:
    """The runner records the live-mirror boundary from what its echo did."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("echo_ts", "owed"), [("echo-ts", []), (None, ["first message"])])
    async def test_a_failed_echo_leaves_the_prompt_owed(self, tmp_path, monkeypatch, echo_ts, owed):
        from test_slack_mirror_unlink import _fake_provider, _make_slack_client
        from test_slack_mirror_unlink import _make_state as _mirror_state

        from kiro_crew.dashboard.chat import _history_key_for, _run_chat
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        session_map = SessionMap()
        state = _mirror_state(tmp_path, session_map)
        state.slack_client = _make_slack_client()
        # The echo is the first post of the turn; Slack answers a refused post with no ts.
        answers = iter([echo_ts])
        state.slack_client.post_message = AsyncMock(
            side_effect=lambda *_a, **_k: next(answers, "reply-ts")
        )
        state.sessions.get_or_create = AsyncMock(return_value=(_fake_provider(), False, False))
        slot = state.get_or_create_slot("s1")
        slot.append("user", "first message")
        slot.drain()
        state.sessions.set_slack_link(_history_key_for(slot.key), "thread-1", "C-1")

        await _run_chat(state, slot, "first message")

        assert slot._slack_live_since is not None
        assert [r["content"] for r in chat_slack._rows_owed_to_thread(slot, "thread-1")] == owed


class TestUnsavedLink:
    @pytest.mark.asyncio
    async def test_a_link_the_map_cannot_write_is_taken_back_down(self, tmp_path, monkeypatch):
        """A failed map write leaves no in-memory binding to mirror into."""
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        monkeypatch.setattr(chat_slack, "_spawn_slack_backfill", lambda *a, **_k: None)
        monkeypatch.setattr(state.sessions, "aflush", AsyncMock(side_effect=OSError("disk full")))

        with pytest.raises(SlackLinkError) as exc:
            await link_slot_to_slack(state, slot, governed=False)

        assert (exc.value.status, exc.value.code) == (500, "link_not_saved")
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == (None, None)
        assert (slot._slack_linked, slot._slack_thread_ts, slot._slack_channel) == (False, "", "")
        assert "ts_anchor" not in state._slack_to_slot

    @pytest.mark.asyncio
    async def test_a_failed_handoff_gives_the_thread_back_to_its_owner(self, tmp_path, monkeypatch):
        """Linking to a thread another session holds, then failing to save it,
        leaves that session holding the thread as before."""
        state = _slack_state(tmp_path, monkeypatch)
        owner = _user_slot(state, "s0")
        state.link_slack(owner.key, "ts_existing", "C_EXISTING")
        slot = _user_slot(state, "s1")
        monkeypatch.setattr(state.sessions, "aflush", AsyncMock(side_effect=OSError("disk full")))

        with pytest.raises(SlackLinkError) as exc:
            await link_slot_to_slack(
                state, slot, channel="C_EXISTING", existing_thread="ts_existing", governed=False
            )

        assert exc.value.code == "link_not_saved"
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == (None, None)
        assert state.sessions.get_slack_link(f"dashboard:{owner.key}") == (
            "ts_existing",
            "C_EXISTING",
        )
        assert (owner._slack_linked, owner._slack_thread_ts, owner._slack_channel) == (
            True,
            "ts_existing",
            "C_EXISTING",
        )
        assert state._slack_to_slot["ts_existing"] == owner.key
        assert slot._slack_linked is False

    @pytest.mark.asyncio
    async def test_the_automatic_path_proceeds_unlinked(self, tmp_path, monkeypatch):
        state = _slack_state(tmp_path, monkeypatch)
        slot = _user_slot(state)
        _settings(monkeypatch, enabled=True)
        monkeypatch.setattr(state.sessions, "aflush", AsyncMock(side_effect=OSError("disk full")))

        assert await maybe_auto_link_slack(state, slot) is False
        assert state.sessions.get_slack_link(f"dashboard:{slot.key}") == (None, None)
        assert slot._slack_linked is False
