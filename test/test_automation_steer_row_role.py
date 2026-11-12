"""An automation steer keeps its origin on its row, steered or requeued.

A work-ledger wake that lands while the conductor's turn is running is steered
into that turn (``nudge_dashboard_fire._steer_work_ledger_wake``). The idle fire
writes the same text as a ``nudge`` row, drawn as the nudge chip; the steer path
wrote a ``user`` row, drawn as a bubble the person typed. And when the turn ended
before taking it, the requeued entry merged with the person's own queued message
into one ``user`` row, which is how the wake got glued onto their words.

These tests pin the row role to the origin: the steered row, and the row the drain
writes for a requeued one, are ``nudge`` rows, and the requeued entry drains alone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state
from chat_test_helpers import _make_state
from steer_requeue_helpers import _running_slot

from kiro_crew.context_assembly.replay import build_interrupted_turn_preamble
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers
from kiro_crew.dashboard.chat_utils import _dequeue_next_message
from kiro_crew.dashboard.chat_utils_injected_rows import AUTOMATION_STEER_META
from kiro_crew.history import HUMAN_TURN_META_KEY

WAKE = "[work-ledger wake] item=it_1 status=question\n[work-ledger] read the ledger"
NUDGE = {"cycle": 4, "loop_id": "loop-1"}


def _steer_client(on_steer=None):
    client = MagicMock()
    client.supports_steer = True
    client.steer = on_steer or AsyncMock(return_value=True)
    return client


class TestSteeredRow:
    @pytest.mark.asyncio
    async def test_an_automation_steer_is_a_nudge_row_not_a_user_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = _steer_client()

        outcome = await steer_into_running_turn(
            state, slot, WAKE, user_origin=False, admission={}, nudge_meta=NUDGE
        )

        assert outcome == STEER_STEERED
        rows = [m for m in slot.messages if "work-ledger wake" in str(m.get("content"))]
        assert [r["role"] for r in rows] == ["nudge"], (
            "automation text must draw as the nudge chip the idle fire writes, "
            "never as a bubble the person typed"
        )
        meta = rows[0]["meta"]
        assert meta["nudge"] == NUDGE
        assert meta["steer"] is True, "still a steer: it opens no turn"
        assert HUMAN_TURN_META_KEY not in meta
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" not in events, "the client draws a steer_push as a user bubble"
        assert "chat_segment" in events, "open tabs freeze the streaming text at the cut"
        # No consumption echo yet, so the turn can still end without taking it:
        # the origin stays held for that requeue, like the steer itself.
        assert slot._pending_steers == [WAKE]
        assert slot._steer_automation == {WAKE: NUDGE}

    @pytest.mark.asyncio
    async def test_other_tabs_get_the_row_while_a_stream_reader_holds_the_turn(
        self, tmp_path, monkeypatch
    ):
        """An SSE reader suppresses the append's own broadcast; the row still goes out."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = _steer_client()
        slot._has_reader = True

        await steer_into_running_turn(
            state, slot, WAKE, user_origin=False, admission={}, nudge_meta=NUDGE
        )

        frames = [
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_message"
        ]
        assert [f.get("role") for f in frames] == ["nudge"]

    @pytest.mark.asyncio
    async def test_the_steered_row_comes_from_the_wake_origin(self, tmp_path, monkeypatch):
        """One origin-to-row mapping: the steer and the idle fire share it."""
        from kiro_crew.dashboard import session_deliver

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = _steer_client()
        real = session_deliver.origin_row
        seen: list[tuple[str, dict]] = []

        def _recording(origin):
            seen.append((origin.kind, dict(origin.nudge or {})))
            return real(origin)

        monkeypatch.setattr(session_deliver, "origin_row", _recording)
        await steer_into_running_turn(
            state, slot, WAKE, user_origin=False, admission={}, nudge_meta=NUDGE
        )
        assert seen == [(session_deliver.ORIGIN_AUTOMATION, NUDGE)]

    @pytest.mark.asyncio
    async def test_a_peer_steer_still_writes_a_user_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = _steer_client()

        await steer_into_running_turn(state, slot, "peer text", user_origin=False, admission={})

        rows = [m for m in slot.messages if m.get("content") == "peer text"]
        assert [r["role"] for r in rows] == ["user"]
        assert "steer_push" in [c.args[0] for c in state.broadcast_ws.call_args_list]


class TestRequeuedAutomationSteer:
    def _requeue_with_human_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot.queue_append("please also fix the login bug", directive_user_origin=True)
        # The wake was registered as an automation steer the turn never took.
        slot._pending_steers = [WAKE]
        slot._steer_automation = {WAKE: dict(NUDGE)}
        _requeue_unconsumed_steers(state, slot)
        return state, slot

    def test_the_requeued_entry_keeps_its_origin(self, tmp_path, monkeypatch):
        _state_, slot = self._requeue_with_human_message(tmp_path, monkeypatch)

        head = slot._queue[0]
        assert head["content"] == WAKE
        assert head["meta"][AUTOMATION_STEER_META] == NUDGE
        assert slot._steer_automation == {}

    def test_the_drain_never_merges_it_into_the_persons_text(self, tmp_path, monkeypatch):
        _state_, slot = self._requeue_with_human_message(tmp_path, monkeypatch)

        first, first_items = _dequeue_next_message(slot, merge_enabled=True)
        second, second_items = _dequeue_next_message(slot, merge_enabled=True)

        assert (first, len(first_items)) == (WAKE, 1)
        assert (second, len(second_items)) == ("please also fix the login bug", 1)

    def test_a_merge_run_stops_before_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        slot = _make_state(tmp_path).get_or_create_slot("test")
        slot.queue_append("first")
        slot.queue_append(WAKE, meta={AUTOMATION_STEER_META: dict(NUDGE)})

        text, items = _dequeue_next_message(slot, merge_enabled=True)

        assert (text, len(items)) == ("first", 1)

    @pytest.mark.asyncio
    async def test_a_human_edit_clears_automation_origin_and_drains_as_user(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        qid = slot.queue_append(WAKE, meta={AUTOMATION_STEER_META: dict(NUDGE), "sendId": "s-1"})

        assert slot.queue_edit_by_id(qid, "my own words", directive_user_origin=True) is True

        entry = slot._queue[0]
        assert entry["id"] == qid
        assert entry["content"] == "my own words"
        assert entry["meta"] == {"sendId": "s-1"}
        assert entry["_directive_user_origin"] is True
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        rows = [m for m in slot.messages if m.get("content") == "my own words"]
        assert [r["role"] for r in rows] == ["user"]
        assert "nudge" not in rows[0]["meta"]
        assert AUTOMATION_STEER_META not in rows[0]["meta"]

    @pytest.mark.asyncio
    async def test_the_drained_row_is_a_nudge_row(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.broadcast_ws = MagicMock()
        slot.queue_append(WAKE, meta={AUTOMATION_STEER_META: dict(NUDGE)})

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        rows = [m for m in slot.messages if m.get("content") == WAKE]
        assert [r["role"] for r in rows] == ["nudge"]
        assert rows[0]["meta"]["nudge"] == NUDGE
        assert AUTOMATION_STEER_META not in rows[0]["meta"]
        assert HUMAN_TURN_META_KEY not in rows[0]["meta"]
        pops = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_pop"]
        assert pops and all(
            p["drain_writes_row"] is True for p in pops
        ), "the client must not rebuild the popped entry as a user row"

    @pytest.mark.asyncio
    async def test_other_tabs_get_the_drained_nudge_while_a_stream_reader_holds_the_slot(
        self, tmp_path
    ):
        """An SSE reader suppresses append's broadcast; the drained row still goes out."""
        state, slot = _state(tmp_path), _slot()
        slot._has_reader = True
        slot.queue_append(WAKE, meta={AUTOMATION_STEER_META: dict(NUDGE)})

        with (
            patch.object(state, "broadcast_ws") as broadcast,
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        frames = [c.args[1] for c in broadcast.call_args_list if c.args[0] == "chat_message"]
        assert [f.get("role") for f in frames] == ["nudge"]
        assert frames[0]["content"] == WAKE
        assert frames[0]["meta"]["nudge"] == NUDGE
        rows = [m for m in slot.messages if m.get("content") == WAKE]
        assert frames[0]["meta"]["mid"] == rows[0]["meta"]["mid"]


class TestRequeuedAutomationSteerShowsOnce:
    """A requeued automation steer's nudge row is superseded by its drained nudge row.

    The RPC accepted the wake, so its ``nudge`` row was written at once. The turn
    then ended before ``steering_consumed``, so the teardown marked that row
    ``requeued`` and queued the text. The drain writes the turn's own ``nudge``
    row and retires the old one as ``superseded``, so the client draws the wake
    once, the same as a person's requeued steer.
    """

    async def _steer_then_requeue(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = _running_slot(state)
        slot._acp_client = _steer_client()

        await steer_into_running_turn(
            state, slot, WAKE, user_origin=False, admission={}, nudge_meta=NUDGE
        )
        # No consumption echo: the turn ends with the steer still pending.
        assert slot._pending_steers == [WAKE]
        _requeue_unconsumed_steers(state, slot)
        # The soft stop that ended the turn leaves its card after the steer row.
        slot.append("stop_event", "Stopped", "msg msg-stop", meta={"kind": "stop_event"})
        slot.task = None
        return state, slot

    @staticmethod
    async def _drain(state, slot):
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

    @staticmethod
    def _rows(slot, role):
        return [m for m in slot.messages if m.get("role") == role and m.get("content") == WAKE]

    @pytest.mark.asyncio
    async def test_the_drain_supersedes_the_requeued_nudge_row(self, tmp_path, monkeypatch):
        state, slot = await self._steer_then_requeue(tmp_path, monkeypatch)
        (old,) = self._rows(slot, "nudge")
        old_mid = old["meta"]["mid"]
        assert old["meta"]["steerState"] == "requeued"
        assert slot._queue[0]["meta"]["requeued_steer_row_mid"] == old_mid
        state.broadcast_ws.reset_mock()

        await self._drain(state, slot)

        old_row, fresh = self._rows(slot, "nudge")
        assert self._rows(slot, "user") == []
        assert old_row is old
        assert old_row["meta"]["steerState"] == "superseded"
        assert fresh["meta"]["mid"] and fresh["meta"]["mid"] != old_mid
        assert old_row["meta"]["supersededBy"] == fresh["meta"]["mid"]
        assert fresh["meta"]["nudge"] == NUDGE
        assert not fresh["meta"].get("steer")
        assert "requeued_steer_row_mid" not in fresh["meta"]
        assert slot.messages.index(fresh) > slot.messages.index(
            next(m for m in slot.messages if m.get("role") == "stop_event")
        )
        updates = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "chat_message_update"
        ]
        assert any(
            u.get("mid") == old_mid
            and u["meta"]["steerState"] == "superseded"
            and u["meta"]["supersededBy"] == fresh["meta"]["mid"]
            for u in updates
        ), "open clients must be told to stop drawing the old row"

    @pytest.mark.asyncio
    async def test_a_user_drain_never_retires_a_nudge_steer_row(self, tmp_path, monkeypatch):
        state, slot = await self._steer_then_requeue(tmp_path, monkeypatch)
        (old,) = self._rows(slot, "nudge")
        # The entry still names the nudge row and holds its text, but it no
        # longer carries the automation origin, so it drains as a `user` row.
        slot._queue[0]["meta"].pop(AUTOMATION_STEER_META)

        await self._drain(state, slot)

        assert [r["role"] for r in self._rows(slot, "user")] == ["user"]
        assert self._rows(slot, "nudge") == [old]
        assert old["meta"]["steerState"] == "requeued"
        assert "supersededBy" not in old["meta"]

    @pytest.mark.asyncio
    async def test_an_automation_drain_never_retires_a_user_steer_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = _running_slot(state)
        slot._acp_client = _steer_client()
        # A peer's steer with the wake's text: a `user` row.
        await steer_into_running_turn(state, slot, WAKE, user_origin=False, admission={})
        _requeue_unconsumed_steers(state, slot)
        slot.task = None
        (old,) = self._rows(slot, "user")
        assert old["meta"]["steerState"] == "requeued"
        assert slot._queue[0]["meta"]["requeued_steer_row_mid"] == old["meta"]["mid"]
        # The same entry, now with an automation origin, drains as a `nudge` row.
        slot._queue[0]["meta"][AUTOMATION_STEER_META] = dict(NUDGE)

        await self._drain(state, slot)

        assert [r["role"] for r in self._rows(slot, "nudge")] == ["nudge"]
        assert self._rows(slot, "user") == [old]
        assert old["meta"]["steerState"] == "requeued"
        assert "supersededBy" not in old["meta"]

    @pytest.mark.parametrize(
        ("row_role", "drain_role", "matched"),
        [
            ("nudge", "nudge", True),
            ("user", "user", True),
            ("user", "nudge", False),
            ("nudge", "user", False),
        ],
    )
    def test_the_match_requires_the_role_the_drain_writes(self, row_role, drain_role, matched):
        from kiro_crew.dashboard.chat_turn.superseded_steer import _requeued_steer_row
        from kiro_crew.dashboard.chat_utils_injected_rows import REQUEUED_STEER_ROW_MID_META

        row = {
            "role": row_role,
            "content": WAKE,
            "meta": {"mid": "m-old", "steer": True, "steerState": "requeued"},
        }
        slot = MagicMock()
        slot.messages = [row]
        consumed = [{"content": WAKE, "meta": {REQUEUED_STEER_ROW_MID_META: "m-old"}}]

        found = _requeued_steer_row(slot, consumed, WAKE, drain_role)

        assert (found is row) is matched


def test_a_steered_nudge_row_does_not_open_the_interrupted_turn():
    """Resume restores the PERSON's request, not the wake steered into it."""
    messages = [
        {"role": "user", "content": "build the report", "meta": {}},
        {"role": "assistant", "content": "working on it", "meta": {}},
        {"role": "nudge", "content": WAKE, "meta": {"steer": True, "nudge": NUDGE}},
    ]

    preamble = build_interrupted_turn_preamble(messages)

    assert "build the report" in preamble
    assert "[Steered mid-turn]" in preamble
