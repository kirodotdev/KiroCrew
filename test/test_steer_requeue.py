"""Tests for the steer-loss fix: unconsumed mid-turn steers are requeued.

A steer handed to kiro-cli lives inside the running turn; if the turn dies
before kiro-cli echoes ``steering_consumed`` (stall-cancel, soft STOP, error,
or a steer racing the turn's natural end) the message used to vanish silently
(2026-07-17 incident). The fix tracks pending steers on the slot:

  * the steer handler registers in ``slot._pending_steers`` BEFORE the steer
    RPC's await (unwound on failure), so a turn dying mid-write still sees it;
  * ``EVENT_STEER_CONSUMED`` settles pending steers matched against the echo's
    ``<user_message>``-wrapped snapshot (late arrivals stay pending; an empty
    echo falls back to settling all);
  * ``_run_chat``'s finally requeues leftovers at the HEAD of the slot queue
    as ordinary, individually-cancellable queue cards (``queue_push``);
  * a hard kill (force stop) discards pending steers alongside the queue —
    mirroring the existing "second press = discard everything" semantics.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.fixture
def _patch_sel():
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


def _running_slot(state, key="test"):
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


class TestSteerPendingTracking:
    """The steer handler records successful steers on the slot."""

    @pytest.mark.asyncio
    async def test_successful_steer_is_tracked_pending(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "fix sw.js", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        assert slot._pending_steers == ["fix sw.js"]

    @pytest.mark.asyncio
    async def test_failed_steer_not_tracked(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=RuntimeError("boom"))
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        # fell through to the queue path — must NOT also be pending as a steer
        # (that would double-deliver it after the turn ends)
        assert slot._pending_steers == []

    @pytest.mark.asyncio
    async def test_multiple_steers_tracked_in_order(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            for msg in ("first", "second"):
                resp = await client.post(
                    "/api/chat", json={"slot": "test", "message": msg, "steer": True}
                )
                assert resp.status == 200

        assert slot._pending_steers == ["first", "second"]


class TestSteerConsumedClears:
    """_settle_consumed_steers: snapshot-matched settling via the real helper."""

    def _slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        return state.get_or_create_slot("test")

    def test_snapshot_settles_only_contained_steers(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["fix the bug", "late arrival"]
        # kiro-cli echo: <user_message>-wrapped concatenated snapshot that was
        # taken BEFORE "late arrival" was registered.
        _settle_consumed_steers(slot, "<user_message>\nfix the bug\n</user_message>")
        assert slot._pending_steers == ["late arrival"]

    def test_snapshot_with_all_steers_settles_all(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["a", "b"]
        _settle_consumed_steers(
            slot, "<user_message>\na\n</user_message><user_message>\nb\n</user_message>"
        )
        assert slot._pending_steers == []

    def test_empty_snapshot_falls_back_to_settling_all(self, tmp_path, monkeypatch):
        # Older backend / redacted echo: no usable text -> pre-review behavior
        # (settle all; duplicate is visible+cancellable, loss is not).
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["a", "b"]
        _settle_consumed_steers(slot, "   ")
        assert slot._pending_steers == []

    def test_substring_steer_not_falsely_settled(self, tmp_path, monkeypatch):
        # review-bot regression: "fix" is a SUBSTRING of the consumed block
        # "fix the bug" but was never itself consumed — equality matching on
        # parsed blocks must keep it pending (substring matching would settle
        # it and silently lose it when the turn dies).
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["fix", "fix the bug"]
        _settle_consumed_steers(slot, "<user_message>\nfix the bug\n</user_message>")
        assert slot._pending_steers == ["fix"]

    def test_wrapper_text_not_falsely_settled(self, tmp_path, monkeypatch):
        # A steer like "user" must not match the <user_message> wrapper itself.
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["user", "e"]
        _settle_consumed_steers(slot, "<user_message>\nsomething else\n</user_message>")
        assert slot._pending_steers == ["user", "e"]

    def test_whitespace_parity_with_rpc_strip(self, tmp_path, monkeypatch):
        # The steer RPC wraps message.strip(); pending stores the raw message.
        # A trailing-newline pending entry must still settle against its block.
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["do the thing\n"]
        _settle_consumed_steers(slot, "<user_message>\ndo the thing\n</user_message>")
        assert slot._pending_steers == []

    def test_duplicate_steers_only_settle_consumed_count(self, tmp_path, monkeypatch):
        # review-bot regression: two identical pending steers, snapshot consumed
        # only ONE of them (the duplicate was registered after kiro-cli
        # snapshotted). Set-membership settling would sweep both and silently
        # lose the second — settling must be count-aware.
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["fix", "fix"]
        _settle_consumed_steers(slot, "<user_message>\nfix\n</user_message>")
        assert slot._pending_steers == ["fix"]

    def test_duplicate_steers_settle_all_when_snapshot_has_both(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["fix", "fix"]
        _settle_consumed_steers(
            slot,
            "<user_message>\nfix\n</user_message><user_message>\nfix\n</user_message>",
        )
        assert slot._pending_steers == []

    def test_noop_without_pending(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        _settle_consumed_steers(slot, "<user_message>x</user_message>")
        assert slot._pending_steers == []


class TestSteerRegisteredBeforeAwait:
    """The pending registration must happen BEFORE the steer RPC's await, so a
    turn dying during the stdin.drain() suspension still sees (and requeues)
    the steer — the append-after-await race."""

    @pytest.mark.asyncio
    async def test_pending_visible_during_steer_await(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        observed: list[list[str]] = []

        async def _steer(message):
            # Snapshot what the turn's finally would see mid-await.
            observed.append(list(slot._pending_steers))
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = _steer
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "mid-write", "steer": True}
            )
            assert resp.status == 200

        assert observed == [["mid-write"]]  # registered BEFORE the await completed
        assert slot._pending_steers == ["mid-write"]

    @pytest.mark.asyncio
    async def test_failed_steer_unwinds_registration(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=RuntimeError("boom"))
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        # unwound — queue fallback owns delivery, no double-delivery via requeue
        assert slot._pending_steers == []
        assert [i["content"] for i in slot._queue] == ["later"]

    @pytest.mark.asyncio
    async def test_failed_steer_already_requeued_by_finally_skips_fallback(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        # The turn's finally ran DURING the await and requeued the steer; the
        # failure path must detect the missing entry and NOT queue it again.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _steer(message):
            # Simulate _requeue_unconsumed_steers running mid-await.
            from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

            _requeue_unconsumed_steers(state, slot)
            raise RuntimeError("backend died")

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = _steer
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "racy", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        # exactly ONE copy in the queue (from the finally's requeue), not two
        assert [i["content"] for i in slot._queue] == ["racy"]
        assert slot._pending_steers == []


class TestProductionWiring:
    """Source-level guards (pattern: test_chat_turn_timeout_consistency.py):
    deleting either production wiring point must fail a test, closing the
    'all tests still green with the wiring removed' review gap."""

    def _runner_source(self) -> str:
        from pathlib import Path

        import kiro_crew.dashboard.chat_runner as cr

        return Path(cr.__file__).read_text(encoding="utf-8")

    def test_finally_calls_requeue_before_queue_drain(self):
        src = self._runner_source()
        requeue_at = src.index("_requeue_unconsumed_steers(state, slot)")
        drain_at = src.index("# Process queued messages (FIFO)")
        assert requeue_at < drain_at, (
            "_run_chat's finally must call _requeue_unconsumed_steers BEFORE "
            "the queue drain so a requeued steer is delivered on the very next turn"
        )

    def test_event_loop_wires_steer_consumed_to_settle(self):
        src = self._runner_source()
        assert "elif event.kind == EVENT_STEER_CONSUMED:" in src
        branch_at = src.index("elif event.kind == EVENT_STEER_CONSUMED:")
        settle_at = src.index("_settle_consumed_steers(slot, event.text", branch_at)
        # the settle call must be the branch body (within a few lines)
        assert settle_at - branch_at < 200

    def test_steer_handler_registers_before_await(self):
        from pathlib import Path

        import kiro_crew.dashboard.chat_handlers as ch

        src = Path(ch.__file__).read_text(encoding="utf-8")
        register_at = src.index("slot._pending_steers.append(message)")
        await_at = src.index("await _client.steer(message)")
        assert register_at < await_at, (
            "pending registration must precede the steer RPC await so a turn "
            "dying mid-write still requeues the steer"
        )


class TestSteerRequeueOnTurnDeath:
    """_run_chat's finally requeues unconsumed steers as queue cards."""

    @pytest.mark.asyncio
    async def test_unconsumed_steers_requeued_at_queue_head(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        # a message the user queued during the turn
        slot.queue_append("queued-later")
        # two steers the dying turn never consumed
        slot._pending_steers = ["steer-1", "steer-2"]

        # Execute the requeue block exactly as _run_chat's finally does.
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        _requeue_unconsumed_steers(state, slot)

        # steers land at the HEAD, preserving their relative order,
        # ahead of the previously queued message
        contents = [item["content"] for item in slot._queue]
        assert contents == ["steer-1", "steer-2", "queued-later"]
        assert slot._pending_steers == []
        # each requeued steer broadcast a queue_push card
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert events.count("queue_push") == 2
        payloads = [c.args[1] for c in state.broadcast_ws.call_args_list]
        assert all(p["slot"] == "test" and p["queue_id"] for p in payloads)

    @pytest.mark.asyncio
    async def test_no_pending_steers_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot.queue_append("existing")

        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        _requeue_unconsumed_steers(state, slot)

        assert [i["content"] for i in slot._queue] == ["existing"]
        state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_requeue_survives_broadcast_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock(side_effect=RuntimeError("ws down"))
        slot = state.get_or_create_slot("test")
        slot._pending_steers = ["important"]

        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        _requeue_unconsumed_steers(state, slot)  # must not raise

        # message is in the queue even though the broadcast failed
        assert [i["content"] for i in slot._queue] == ["important"]
        assert slot._pending_steers == []


class TestHardKillDiscardsSteers:
    """Force stop (second press) discards pending steers with the queue."""

    @pytest.mark.asyncio
    async def test_force_stop_clears_pending_steers(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.sessions.stop_turn = AsyncMock()
        slot = _running_slot(state)
        slot._stop_state = "soft_pending"  # first press already happened
        slot.queue_append("queued")
        slot._pending_steers = ["steered"]

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/stop?force=true")
            assert resp.status == 200

        assert slot._queue == []
        assert slot._pending_steers == []
