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


class TestDeliveryIdLifecycle:
    """The delivery-id map must not outlive the delivery it identifies.

    It is keyed by the message TEXT, so an entry left behind holds a full
    message string for the slot's whole lifetime. The requeue paths keep theirs
    on purpose -- the drain in `chat_runner` still has to match the id, and that
    entry is bounded by the queue -- but a delivery that persists its own row is
    terminal here and nothing downstream will read it again.

    `_steer_send_ids` (#6751) is the same shape with the same failure mode, and is
    removed in LOCKSTEP with the delivery id at every site, so these pins assert
    BOTH maps rather than growing a parallel test class. Each POST below carries a
    `meta.sendId`, without which the second assertion would be vacuous.
    """

    @pytest.mark.asyncio
    async def test_a_successful_steer_leaves_no_entry(self, tmp_path, monkeypatch, _patch_sel):
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
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "fix sw.js",
                    "steer": True,
                    # Carries a send id so the `_steer_send_ids` assertion below is a
                    # real pin rather than a vacuous one: without it that map is
                    # never populated and the assertion holds even with the pop
                    # removed (#6751).
                    "meta": {"sendId": "s-m4k2p1-9x7"},
                },
            )
            assert resp.status == 200

        assert slot._steer_delivery_ids == {}, (
            "a delivered steer that persisted its own row is terminal; its id has "
            "no later reader, so keeping it holds the message text for the slot's life"
        )
        assert slot._steer_send_ids == {}, (
            "same for the send id (#6751): this delivery stamped it onto its own "
            "row, so nothing downstream reads the map entry again"
        )

    @pytest.mark.asyncio
    async def test_a_refused_steer_leaves_no_entry(self, tmp_path, monkeypatch, _patch_sel):
        """The unwind path must clear it too, or a queue fallback leaks instead."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=False)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "fix sw.js",
                    "steer": True,
                    "meta": {"sendId": "s-m4k2p1-9x7"},
                },
            )

        assert slot._steer_delivery_ids == {}
        # The unwind hands delivery to the queue fallback, which mints no steer, so
        # nothing will read this entry either (#6751).
        assert slot._steer_send_ids == {}

    @pytest.mark.asyncio
    async def test_many_successful_steers_do_not_accumulate(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """The growth shape is what makes this a leak rather than one stale key."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            for n in range(5):
                await client.post(
                    "/api/chat",
                    json={
                        "slot": "test",
                        "message": f"unique message {n}",
                        "steer": True,
                        # A distinct id per send: the growth shape is what makes this
                        # a leak, so each send must contribute its own key (#6751).
                        "meta": {"sendId": f"s-m4k2p1-{n}"},
                    },
                )

        assert slot._steer_delivery_ids == {}
        assert slot._steer_send_ids == {}


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
        drain_at = src.index(
            "next_turn_started = await _start_next_queued_turn(state, slot)",
            requeue_at,
        )
        assert requeue_at < drain_at, (
            "_run_chat's finally must call _requeue_unconsumed_steers BEFORE "
            "the queue drain so a requeued steer is delivered on the very next turn"
        )

    def test_inject_provenance_folds_into_the_mapping_the_row_write_reads(self):
        """One mapping carries BOTH provenance kinds to the row.

        `_start_next_queued_turn` builds row meta from two independent producers:
        the drain's union over every consumed entry (which is what carries a merged
        row's steer delivery ids) and the `inject` block's `injectKind`/`cronLabel`.
        They must fold into the SAME mapping, because only one of them is passed to
        `slot.append`. A second local would silently drop whichever producer the row
        write does not read -- and no drain-level test covers `injectKind`, so that
        loss would not otherwise surface.
        """
        src = self._runner_source()
        fold_at = src.index("_drained_meta.update(_inject_meta)")
        write_at = src.index("meta=_drained_meta or None", fold_at)
        assert fold_at < write_at, (
            "the inject provenance fold must target _drained_meta -- the same "
            "mapping slot.append receives -- and must precede the row write"
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

        import kiro_crew.dashboard.chat_delivery as cd

        src = Path(cd.__file__).read_text(encoding="utf-8")
        register_at = src.index("slot._pending_steers.append(message)")
        await_at = src.index("await client.steer(message)")
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


class TestSteerLifecycleState:
    """The row must report which of the three states the steer is actually in.

    `steer()` returning proves only that the backend has the bytes. A steer is
    injected at a model-inference boundary, so a turn streaming text without
    dispatching a tool can end without ever reaching one -- the backend then
    echoes no `steering_consumed`, the teardown requeues the message, and it runs
    as its own turn. The row used to claim a successful mid-turn injection from
    write-ack alone, so that path rendered "steered into the running turn" for a
    turn that was never redirected (#7246).

    These assert the wire values as LITERALS on purpose. Importing the state
    constants would make every test here fail on an unfixed tree with an
    ImportError, which proves only that the names are new -- not that the row used
    to carry the wrong claim. With literals the failure is the behaviour: the row
    has no state at all, or still reads as an injection after the requeue.
    """

    @pytest.mark.asyncio
    async def test_a_written_steer_row_is_not_marked_consumed(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

        outcome = await steer_into_running_turn(state, slot, "go north")

        assert outcome == STEER_STEERED
        row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert row["meta"].get("steerState") == "written"
        # and the live echo carries the same state, so a client that never
        # reloads renders the same fact the row holds
        push = next(
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"
        )
        assert push.get("steerState") == "written"

    @pytest.mark.asyncio
    async def test_a_consumed_echo_promotes_the_row(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        await steer_into_running_turn(state, slot, "go north")
        row_ts = next(m for m in slot.messages if m.get("meta", {}).get("steer"))["ts"]
        state.broadcast_ws.reset_mock()

        _settle_consumed_steers(slot, "<user_message>\ngo north\n</user_message>", state)

        row = next(m for m in slot.messages if m["ts"] == row_ts)
        assert row["meta"].get("steerState") == "consumed"
        patch_payload = next(
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "chat_message_update"
        )
        assert patch_payload["ts"] == row_ts
        assert patch_payload["meta"]["steerState"] == "consumed"

    @pytest.mark.asyncio
    async def test_a_requeued_steer_row_stops_claiming_injection(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """The state the bug report is about: acked, never consumed, requeued."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        await steer_into_running_turn(state, slot, "go north")
        row_ts = next(m for m in slot.messages if m.get("meta", {}).get("steer"))["ts"]
        # the turn ends with no `steering_consumed` echo, so the entry is still
        # pending when the teardown runs
        assert slot._pending_steers == ["go north"]
        state.broadcast_ws.reset_mock()

        _requeue_unconsumed_steers(state, slot)

        row = next(m for m in slot.messages if m["ts"] == row_ts)
        # the row must NOT still read as a successful mid-turn injection
        assert row["meta"].get("steerState") == "requeued"
        # the message still runs -- correcting the claim must not drop it
        assert [i["content"] for i in slot._queue] == ["go north"]
        patch_payload = next(
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "chat_message_update"
        )
        assert patch_payload["meta"]["steerState"] == "requeued"

    @pytest.mark.asyncio
    async def test_a_steer_consumed_during_the_rpc_persists_as_consumed(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """The echo can land while `steer()` is still suspended.

        The settle then removes the pending entry BEFORE any row exists, so it has
        nothing to promote. If the row that follows claimed `written`, a CONFIRMED
        injection would be understated forever -- nothing runs the promotion twice.
        This is the mirror of the #7246 defect: overstating and understating are
        both the row disagreeing with the backend.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _consume_during_rpc(_msg):
            # Drive the REAL settle with a REAL echo rather than hand-clearing the
            # list. A bare `_pending_steers.clear()` reproduces the EVIDENCE-FREE
            # `settle_all_on_empty` sweep, not a matched echo -- an injection
            # narrower than the fault this test names, which let it pass while
            # `chat_delivery` was inferring `consumed` from absence alone.
            from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

            _settle_consumed_steers(slot, "<user_message>\ngo north\n</user_message>", state)
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=_consume_during_rpc)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

        outcome = await steer_into_running_turn(state, slot, "go north")

        assert outcome == STEER_STEERED
        row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert row["meta"].get("steerState") == "consumed"
        push = next(
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"
        )
        assert push.get("steerState") == "consumed"

    @pytest.mark.asyncio
    async def test_an_empty_echo_during_the_rpc_persists_as_written(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """An EMPTY echo is no evidence, so the row must not claim consumption.

        `_settle_consumed_steers` passes `settle_all_on_empty=True`, so an empty
        frame clears the pending list without matching anything. `chat_delivery`
        used to infer `consumed` from the entry being gone, which turned a frame
        that proved nothing into a success badge -- and a row persisted as
        `consumed` is terminal, so nothing ever corrected it. That is the #7246
        defect this change exists to remove, reached by a different route.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _empty_echo_during_rpc(_msg):
            from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

            # A real empty frame, not a synthesized clear: this is the exact input
            # whose handling the finding is about.
            _settle_consumed_steers(slot, "", state)
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=_empty_echo_during_rpc)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

        outcome = await steer_into_running_turn(state, slot, "go north")

        assert outcome == STEER_STEERED
        # The sweep really did clear it, so this is the absence-inferring path.
        assert slot._pending_steers == []
        row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert (
            row["meta"].get("steerState") == "written"
        ), "an empty echo carries no evidence, so the row must stay `written`"
        push = next(
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"
        )
        assert push.get("steerState") == "written"

    @pytest.mark.asyncio
    async def test_an_unreadable_evidence_marker_fails_closed_to_written(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """ "No marker" and "no evidence" must be the SAME branch.

        The marker is new per-slot state, so a future refactor or a slot rebuilt
        without it must not be read as confirmation. A marker whose ABSENCE yielded
        `consumed` would reintroduce the defect through a different door, and
        invisibly, because a row persisted as consumed is terminal. Driven with a
        non-set value rather than a missing attribute because `__slots__` guarantees
        the attribute exists -- so the reachable failure is a WRONG TYPE, where an
        unguarded `in` raises TypeError and would crash the steer path instead of
        degrading to the honest state.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _consume_with_broken_marker(_msg):
            from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

            _settle_consumed_steers(slot, "<user_message>\ngo north\n</user_message>", state)
            # Real evidence WAS recorded and is then made unreadable, so this pins
            # the fallback rather than merely the absence of a match.
            slot._steer_confirmed = 0  # type: ignore[assignment]
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=_consume_with_broken_marker)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

        outcome = await steer_into_running_turn(state, slot, "go north")

        assert outcome == STEER_STEERED, "an unreadable marker must not break the steer"
        row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert (
            row["meta"].get("steerState") == "written"
        ), "an unreadable marker must fall back to `written`, never to `consumed`"

    @pytest.mark.asyncio
    async def test_two_steers_with_identical_sanitized_content_are_left_alone(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """An ambiguous row match patches NOTHING.

        The in-flight guard admits one steer per RAW text, and the row stores the
        SANITIZED text -- so two steers differing only in credential material are
        both admitted and their rows carry byte-identical content. Which row
        belongs to which steer is then unknowable from the row, so neither is
        patched: understating a state is recoverable, while patching the wrong row
        would claim the wrong message was the one the turn consumed. Real identity
        for a pending steer is the refactor tracked in #4333.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import sanitize_outbound, steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _mark_steer_row_state

        first = "deploy with AKIAIOSFODNN7EXAMPLE"
        second = "deploy with AKIAI44QH8DHBEXAMPLE"
        # precondition: different raw texts, identical persisted content
        assert first != second
        assert sanitize_outbound(first) == sanitize_outbound(second)

        await steer_into_running_turn(state, slot, first)
        await steer_into_running_turn(state, slot, second)
        rows = [m for m in slot.messages if m.get("meta", {}).get("steer")]
        assert len(rows) == 2
        assert all(r["meta"].get("steerState") == "written" for r in rows)

        # settling the FIRST steer must not relabel the second's row
        _mark_steer_row_state(state, slot, first, "consumed")

        states = [r["meta"].get("steerState") for r in rows]
        assert states == ["written", "written"], (
            "which row is which is unknowable from the sanitized content, so "
            "neither may be patched -- understating is recoverable, mislabelling "
            "which message the turn consumed is not"
        )
        assert not any(
            c.args[0] == "chat_message_update" for c in state.broadcast_ws.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_collision_the_echo_fully_accounted_for_transitions_both_rows(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """An echo covering EVERY member of a redaction collision confirms both, so
        both rows must leave `written`.

        The sibling test above is the PARTIAL case: one member settles while the
        other is still pending, and refusing to patch is right because which row
        belongs to which steer is unknowable. This is the FULL case, and it is a
        different question -- when the echo accounts for the whole group there is
        nothing left to attribute, and both rows take the SAME new state, so which
        row is which cannot be observed. ``steer_settle`` already settles this
        group (`test_a_redaction_collision_settles_when_every_member_was_echoed`);
        the row resolver used to disagree with it and leave two CONFIRMED
        injections reading `written` for the slot's life. That understates the
        state -- the mirror of #7246 -- and needs no real steer identity, which
        remains #4333's job.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import (
            sanitize_outbound,
            steer_into_running_turn,
        )
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        first = "deploy with AKIAIOSFODNN7EXAMPLE"
        second = "deploy with AKIAI44QH8DHBEXAMPLE"
        # precondition: distinct raw texts, one persisted content -- the collision
        assert first != second
        target = sanitize_outbound(first)
        assert sanitize_outbound(second) == target

        await steer_into_running_turn(state, slot, first)
        await steer_into_running_turn(state, slot, second)
        rows = [m for m in slot.messages if m.get("meta", {}).get("steer")]
        assert len(rows) == 2
        assert all(r["meta"].get("steerState") == "written" for r in rows)
        assert len(slot._pending_steers) == 2

        # The echo carries the redacted text ONCE PER MEMBER, which is what makes
        # the group fully accounted for rather than ambiguous.
        echo = f"<user_message>\n{target}\n</user_message>" * 2
        _settle_consumed_steers(slot, echo, state)

        assert slot._pending_steers == [], (
            "the settlement layer settles a fully-echoed collision; if this fails "
            "the test is no longer exercising the full case"
        )
        states = [r["meta"].get("steerState") for r in rows]
        assert states == ["consumed", "consumed"], (
            "the echo confirmed both steers, so leaving either row `written` "
            "understates a state the backend positively reported"
        )

    @pytest.mark.asyncio
    async def test_a_twin_left_pending_by_the_echo_keeps_the_settled_row_written(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """A PARTIALLY accounted-for group must fail toward refusing the patch.

        The sibling test above promotes rows when the echo covered every member.
        This is the other side of that discrimination and it must not err
        permissive: if the echo left a twin PENDING, which row belongs to the
        settled steer is unknowable again, so the row keeps `written`. Erring the
        other way would confirm a steer whose attribution is unknown -- the #7246
        defect this whole change exists to prevent -- so "cannot prove the group
        fully settled" and "partial" have to be the same branch.

        MEASURED, not hypothesised: `settle_consumed_steers` is all-or-nothing for
        a collision of DISTINCT raw texts (echoed once, both stay pending; echoed
        twice, both settle), so the only shape that partially settles is a group of
        IDENTICAL raw texts -- two pending, echo accounts for one, one returned as
        still pending. That is the state built here.

        `steer_into_running_turn`'s in-flight guard
        (`_pending_steers.count(message) or message in _steer_delivery_ids`) bars two
        identical texts from being pending together, so this is not reachable from
        the public steer path TODAY. It is pinned at this level because the settle
        function provably produces the state, the promote loop has to survive it,
        and `side_state` shares the same helper with its own pending list.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _running_slot(state)

        from kiro_crew.dashboard.chat_delivery import sanitize_outbound
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers
        from kiro_crew.dashboard.steer_settle import settle_consumed_steers

        msg = "deploy with AKIAIOSFODNN7EXAMPLE"
        target = sanitize_outbound(msg)
        echo = f"<user_message>\n{target}\n</user_message>"

        # precondition: this echo settles exactly ONE of the two identical entries,
        # which is what makes the group partial rather than fully accounted for.
        assert settle_consumed_steers([msg, msg], echo) == [msg]

        slot.messages.append(
            {
                "role": "user",
                "ts": "1",
                "content": target,
                "meta": {"steer": True, "steerState": "written", "mid": "m1"},
            }
        )
        slot._pending_steers[:] = [msg, msg]
        state.broadcast_ws = MagicMock()

        _settle_consumed_steers(slot, echo, state)

        assert slot._pending_steers == [msg], (
            "one identical entry stays pending, so the group was only partially "
            "accounted for -- if this fails the test is not exercising the partial case"
        )
        assert slot.messages[-1]["meta"]["steerState"] == "written", (
            "a twin is still pending, so which row is the settled steer's is "
            "unknowable and the row must not be promoted"
        )
        assert not any(
            c.args[0] == "chat_message_update" for c in state.broadcast_ws.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_settle_during_the_rpc_does_not_relabel_an_earlier_stale_row(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """A hard-killed steer's row must not be claimed by a later identical steer.

        A hard kill clears the pending list without reaching either transition, so
        its row truthfully keeps `written` forever. Send the same text again: while
        `steer()` is suspended the new steer has NO row yet, so a settle arriving in
        that window must not resolve to the older row and mark a steer consumed
        that never was.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _mark_steer_row_state

        # first steer persists a row, then a hard kill wipes the bookkeeping
        first_client = MagicMock()
        first_client.supports_steer = True
        first_client.steer = AsyncMock(return_value=True)
        slot._acp_client = first_client
        await steer_into_running_turn(state, slot, "go north")
        stale_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert stale_row["meta"]["steerState"] == "written"
        slot._pending_steers.clear()
        slot._steer_delivery_ids.clear()

        # same text again; a settle lands while the RPC is still suspended
        async def _settle_mid_rpc(_msg):
            _mark_steer_row_state(state, slot, "go north", "consumed")
            slot._pending_steers.clear()
            return True

        second_client = MagicMock()
        second_client.supports_steer = True
        second_client.steer = AsyncMock(side_effect=_settle_mid_rpc)
        slot._acp_client = second_client
        await steer_into_running_turn(state, slot, "go north")

        assert stale_row["meta"]["steerState"] == "written", (
            "the hard-killed steer's row was never consumed and must not be "
            "relabelled by a later steer that happens to carry the same text"
        )

    @pytest.mark.asyncio
    async def test_the_steer_push_carries_the_row_id_the_state_patch_uses(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """`steer_push` must carry the row's `mid`, because the patch is keyed on it.

        The client stores the row from `steer_push` and resolves the later
        `chat_message_update` by `mid`. If the push omits it, the stored row has no
        `mid`, the patch matches nothing, and a consumed steer's badge stays hidden
        until the page is reloaded.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        await steer_into_running_turn(state, slot, "go north")
        row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        push = next(
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"
        )
        assert push.get("mid") == row["meta"]["mid"]

        # and the patch that follows names the same row
        state.broadcast_ws.reset_mock()
        _settle_consumed_steers(slot, "<user_message>\ngo north\n</user_message>", state)
        patch_payload = next(
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "chat_message_update"
        )
        assert patch_payload["mid"] == push["mid"]

    @pytest.mark.asyncio
    async def test_a_dead_row_does_not_block_a_later_steer_from_settling(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """A hard-killed row must not make the NEXT identical steer unsettleable.

        The stale row keeps `written` forever, so a later identical steer finds two
        matching rows. Declining on that would leave a genuinely consumed steer
        reading `written` for good. Only ONE live steer sanitizes to this content,
        so the newest match is unambiguously the live one and the dead row is left
        exactly as it was.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        # a steer whose turn was hard-killed: row persists, bookkeeping wiped
        await steer_into_running_turn(state, slot, "go north")
        dead_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        slot._pending_steers.clear()
        slot._steer_delivery_ids.clear()

        # the same text again, this time genuinely consumed
        await steer_into_running_turn(state, slot, "go north")
        live_row = [m for m in slot.messages if m.get("meta", {}).get("steer")][-1]
        assert live_row is not dead_row

        _settle_consumed_steers(slot, "<user_message>\ngo north\n</user_message>", state)

        assert live_row["meta"]["steerState"] == "consumed"
        assert dead_row["meta"]["steerState"] == "written"

    @pytest.mark.asyncio
    async def test_an_empty_echo_never_claims_consumption(self, tmp_path, monkeypatch, _patch_sel):
        """An empty echo clears the pending list but must not promote any row.

        `settle_all_on_empty=True` is this path's long-standing behaviour and it
        stays, so an empty echo still suppresses the requeue. But an empty echo is
        no evidence of consumption -- `steer_settle` says exactly that -- so writing
        `consumed` off it would reinstate the defect this change exists to remove:
        the row, the transcript and the history all asserting an injection nothing
        confirmed.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        await steer_into_running_turn(state, slot, "go north")
        row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        state.broadcast_ws.reset_mock()

        _settle_consumed_steers(slot, "   ", state)

        # pending-list behaviour unchanged: the empty echo still settles it
        assert slot._pending_steers == []
        # but nothing was CLAIMED about the row
        assert row["meta"].get("steerState") == "written"
        assert not any(
            c.args[0] == "chat_message_update" for c in state.broadcast_ws.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_duplicate_pending_steer_settles_one_entry_only(self, tmp_path, monkeypatch):
        """One echo block settles one pending entry, and neither row is patched.

        The accounting and the row patch are separate guarantees. The multiset
        difference must settle exactly one of two identical pending entries, so its
        twin is still requeued rather than swept. The rows are a different matter:
        two of them carry the same content here, so which is which is unknowable
        and the patch correctly declines (see the sanitized-collision test).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")

        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot.append(
            "user", "same", "msg msg-u", ts="1", meta={"steer": True, "steerState": "written"}
        )
        slot.append(
            "user", "same", "msg msg-u", ts="2", meta={"steer": True, "steerState": "written"}
        )
        slot._pending_steers = ["same", "same"]

        _settle_consumed_steers(slot, "<user_message>\nsame\n</user_message>", state)

        # one entry settled, one still pending -- so the twin is still requeued
        assert slot._pending_steers == ["same"]
        states = [
            m["meta"].get("steerState") for m in slot.messages if m.get("meta", {}).get("steer")
        ]
        assert states == ["written", "written"]


class TestRequeuedThenCancelledSteer:
    """A requeued steer whose card the user cancels never ran, so no row.

    The teardown requeue MOVES the delivery id out of `_steer_delivery_ids` and
    into the new queue entry's meta. If the user then cancels that card before the
    steer RPC resumes, the id is in neither place and no row was ever written --
    which looks exactly like the running turn having consumed the steer.

    A natural stage end requeues without touching `_stop_generation`, so this
    arrives with `stopped` false. Before the fix the not-stopped path never
    consulted the delivery-id map and fell through to the persisting tail,
    writing a transcript row for text the user had explicitly cancelled.
    """

    @pytest.mark.asyncio
    async def test_cancelled_requeue_is_not_persisted_as_delivered(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        text = "fix sw.js"

        async def _requeue_then_cancel(*_a, **_k):
            # Mirror `_requeue_unconsumed_steers`: it pops BOTH the pending entry
            # and the delivery id, carrying the id into the queue entry's meta.
            did = slot._steer_delivery_ids.get(text, "")
            slot._pending_steers.clear()
            slot._steer_delivery_ids.clear()
            qid = slot.queue_insert(0, text, meta={"steer_delivery_id": did})
            # The user dismisses that card before this RPC returns.
            slot.queue_remove_by_id(qid)
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=_requeue_then_cancel)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat", json={"slot": "test", "message": text, "steer": True})

        persisted = [m for m in slot.messages if text in str(m.get("content", ""))]
        assert persisted == [], (
            "the steer was requeued and its card cancelled, so the text never ran; "
            "persisting a row claims a delivery the user explicitly discarded"
        )
        # Not lost either: STEER_UNAVAILABLE means "did not land, safe to resend",
        # so `/api/chat` falls back to `queue_for_next_turn` and the message comes
        # back as its own cancellable card. That fallback is the pre-existing
        # contract of this return value (the hard-kill path shares it) -- what the
        # fix changes is only that no row claims the steer was delivered.
        assert [q["content"] for q in slot._queue] == [
            text
        ], "an undeliverable steer must fall back to the queue rather than vanish"


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


class TestRequeuedSteerCarriesTheClientSendId:
    """A steer the turn never confirmed must reach its ROW with the client id.

    An ACCEPTED steer persists its own row and stamps `meta.sendId` there
    (#6075). A REQUEUED steer does not persist anything: the teardown degrades it
    into a queue card and the DRAIN writes the row. So the id has to travel one
    step further -- registration, queue entry meta, drained row -- or the row is
    id-less and `mergePreservedThinking` has nothing to resolve the tab's
    optimistic bubble against, leaving the pre-steer thinking chip stranded at the
    tail until a reload (#6751).

    The three `STEER_REQUEUED` returns are deliberately NOT the write site, which
    is why no test here asserts against them: one of them returns BEFORE the
    teardown has requeued anything and another AFTER the drain already wrote the
    row, so neither has an entry to stamp at the moment it runs. The requeue is
    the only writer common to all three, so that is what these tests drive.
    """

    _TEXT = "use the cached build"
    #: Same shape the client mints (`s-<base36>-<base36>`), so the value under
    #: test passes the real `normalize_send_id` gates rather than a stand-in.
    _SEND_ID = "s-m4k2p1-9x7"

    def _steer_client(self, on_steer):
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = on_steer
        return client_mock

    @pytest.mark.asyncio
    async def test_requeued_entry_meta_carries_the_send_id(self, tmp_path, monkeypatch, _patch_sel):
        """End to end from the POST: register, requeue, read the entry meta.

        The requeue runs INSIDE the steer RPC's await, driving the real
        `_requeue_unconsumed_steers` rather than a hand-rolled stand-in, so the
        entry meta asserted here is the one production writes.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _steer(message):
            from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

            _requeue_unconsumed_steers(state, slot)
            return True

        slot._acp_client = self._steer_client(_steer)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": self._TEXT,
                    "steer": True,
                    "meta": {"sendId": self._SEND_ID},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        assert [i["content"] for i in slot._queue] == [self._TEXT]
        assert slot._queue[0]["meta"].get("sendId") == self._SEND_ID, (
            "the requeued entry must carry the client's sendId -- the drain unions "
            "entry meta onto the row it writes, so this is the only place the id "
            "can be put for a steer that never persists its own row"
        )
        # The delivery id still rides along: this fix ADDS a key, it does not
        # displace the one the drain already matches on.
        assert slot._queue[0]["meta"].get("steer_delivery_id")

    @pytest.mark.asyncio
    async def test_queue_push_echoes_the_send_id(self, tmp_path, monkeypatch, _patch_sel):
        """A plain (non-steer) enqueue must name the send on its broadcast.

        The POST receipt also reports ``queued``, but a 2xx whose body will not
        parse leaves that unreadable, and the tab then has nothing to retire the
        optimistic row it already rendered -- so every refetch re-attaches it for
        the tab's lifetime. This event is the independent release, so the id has
        to be on it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = _running_slot(state)
        slot._acp_client = None

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": self._TEXT,
                    "meta": {"sendId": self._SEND_ID},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        pushes = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "queue_push"
        ]
        assert pushes, "the enqueue must broadcast queue_push, or nothing can release the row"
        assert pushes[0].args[1].get("sendId") == self._SEND_ID, (
            "queue_push must echo the client's sendId: it is the only release for a "
            "queued send whose POST receipt did not parse"
        )

    @pytest.mark.asyncio
    async def test_queue_push_omits_an_absent_send_id(self, tmp_path, monkeypatch, _patch_sel):
        """Negative control: absence must stay absent, not become a wildcard.

        A payload that always carried the key would hand the client an empty id
        to match on, and a match-anything release deletes a pending send the
        queue never took -- the loss the retention exists to prevent.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = _running_slot(state)
        slot._acp_client = None

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"slot": "test", "message": self._TEXT},
            )
            assert resp.status == 200

        pushes = [
            c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "queue_push"
        ]
        assert pushes, "the enqueue must still broadcast queue_push"
        assert "sendId" not in pushes[0].args[1]

    @pytest.mark.asyncio
    async def test_plain_enqueue_persists_the_send_id_on_the_queue_entry(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """The broadcast is live-only; the ENTRY is what survives a lost receipt.

        A tab that misses `queue_push` (reload, dropped socket) has only the drained
        row to learn the send landed. If the id never reaches the entry, the drain
        cannot put it on the row, so the executed send reads as never-confirmed and
        stays staged for resending -- the send is delivered twice.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = _running_slot(state)
        slot._acp_client = None

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": self._TEXT,
                    "meta": {"sendId": self._SEND_ID},
                },
            )
            assert resp.status == 200

        assert slot._queue, "the message must be queued for the test to mean anything"
        assert (slot._queue[0].get("meta") or {}).get("sendId") == self._SEND_ID, (
            "the queue entry must carry the send id durably: the live broadcast is "
            "not available to a tab that missed it"
        )

    @pytest.mark.asyncio
    async def test_a_held_send_persists_the_send_id_on_the_queue_entry(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """The hold branch owes the same durable release as the busy branch.

        A main slot that is IDLE while sub-agents run HOLDS the message instead of
        starting a turn, and the tab appended a retained bubble because the composer
        was not busy. If the id never reaches the entry, the drained row cannot carry
        it, so that bubble stays unconfirmed for the tab's life and invites a resend.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._acp_client = None
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=["agent-1"])

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": self._TEXT,
                    "meta": {"sendId": self._SEND_ID},
                },
            )
            assert resp.status == 200

        assert slot._queue, "the hold branch must have queued the message"
        assert (slot._queue[0].get("meta") or {}).get("sendId") == self._SEND_ID, (
            "the HELD entry must carry the send id durably, exactly as the busy "
            "branch does: a tab that missed queue_push has only the drained row"
        )

    def test_a_merged_drain_names_every_send_it_stands_for(self, tmp_path, monkeypatch):
        """Two queued sends fold into one row, so one scalar id cannot speak for both.

        The union is a plain dict update, so a scalar `sendId` is last-wins and the
        earlier send's bubble is never resolved -- it stays staged for resending.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot.queue_append("first", meta={"sendId": "send-A"})
        slot.queue_append("second", meta={"sendId": "send-B"})

        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        _, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert len(consumed) == 2, "the premise is a MERGE; a single pop tests nothing"

        from kiro_crew.dashboard.chat_runner import _merged_send_ids

        assert _merged_send_ids(consumed) == ["send-A", "send-B"], (
            "a merged row must name every send it stands for, or the ids it drops "
            "leave those sends falsely unconfirmed"
        )

    @pytest.mark.asyncio
    async def test_drained_row_carries_the_send_id(self, tmp_path, monkeypatch):
        """The leg the fix RELIES on rather than changes: entry meta -> row meta.

        Asserted end to end because "the id is on the queue entry" is worth
        nothing on its own -- the row is what the frontend reads. The drain's
        union already carries arbitrary entry meta (it is how a merged row names
        its steer delivery ids), and this pins that `sendId` is not filtered out
        of it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = state.get_or_create_slot("test")
        slot._pending_steers = [self._TEXT]
        slot._steer_delivery_ids = {self._TEXT: "did-1"}
        slot._steer_send_ids = {self._TEXT: self._SEND_ID}

        from kiro_crew.dashboard import chat_runner

        chat_runner._requeue_unconsumed_steers(state, slot)

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        rows = [m for m in slot.messages if m.get("role") == "user"]
        assert rows, "the drain must have written a user row for the requeued steer"
        assert (rows[-1].get("meta") or {}).get("sendId") == self._SEND_ID, (
            "the drained row is what mergePreservedThinking reads; without the id "
            "on it the optimistic bubble cannot be resolved by identity"
        )

    @pytest.mark.asyncio
    async def test_a_steer_without_a_send_id_keeps_the_prior_entry_shape(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """Additive, not mandatory: an old client's POST carries no id.

        Pinned as an ABSENT KEY rather than a falsy value -- an empty string would
        travel to the row and give the frontend an id that matches nothing.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _steer(message):
            from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

            _requeue_unconsumed_steers(state, slot)
            return True

        slot._acp_client = self._steer_client(_steer)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": self._TEXT, "steer": True}
            )
            assert resp.status == 200

        assert [i["content"] for i in slot._queue] == [self._TEXT]
        assert "sendId" not in slot._queue[0]["meta"]

    @pytest.mark.asyncio
    async def test_an_unusable_send_id_is_treated_as_absent(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """The requeue must inherit `normalize_send_id`, not the raw POST value.

        The entry meta is persisted with the queue and reaches the row, so a value
        that fails the id gates must not get there by the requeue door after being
        refused at the row door.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _steer(message):
            from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

            _requeue_unconsumed_steers(state, slot)
            return True

        slot._acp_client = self._steer_client(_steer)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": self._TEXT,
                    "steer": True,
                    # A JWT dot and base64 padding: outside the id alphabet, which
                    # is exactly what that alphabet exists to exclude.
                    "meta": {"sendId": "a.b/c+d="},
                },
            )
            assert resp.status == 200

        assert "sendId" not in slot._queue[0]["meta"]


class TestSendIdMapLifecycle:
    """The pop sites the extended `TestDeliveryIdLifecycle` pins do not reach.

    `_steer_send_ids` is keyed by message TEXT, so a leaked entry holds a full
    message for the slot's lifetime. It is removed in LOCKSTEP with
    `_steer_delivery_ids` at FIVE sites, and a property enforced at five sites
    needs five proofs. Two of them -- the terminal persisting tail and the unwind
    -- are already pinned above by the delivery-id lifecycle tests now that their
    POSTs carry a send id. The remaining three are here: the requeue, the hard
    kill, and the already-drained return.
    """

    _TEXT = "fix sw.js"
    _SEND_ID = "s-m4k2p1-9x7"

    @pytest.mark.asyncio
    async def test_the_requeue_moves_the_entry_out(self, tmp_path, monkeypatch):
        """Moved onto the queue entry, not copied -- the map must not keep it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._pending_steers = [self._TEXT]
        slot._steer_delivery_ids = {self._TEXT: "did-1"}
        slot._steer_send_ids = {self._TEXT: self._SEND_ID}

        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        _requeue_unconsumed_steers(state, slot)

        assert slot._queue[0]["meta"].get("sendId") == self._SEND_ID
        assert slot._steer_send_ids == {}
        assert slot._steer_delivery_ids == {}

    @pytest.mark.asyncio
    async def test_many_requeued_steers_neither_accumulate_nor_cross_attribute(
        self, tmp_path, monkeypatch
    ):
        """The growth shape for the REQUEUE loop, which one steer cannot show.

        Every other test here requeues a SINGLE pending steer, and with one entry
        the loop variable and any fixed index into the batch are the same value. So
        a classic loop-variable slip -- popping `requeued[0]` rather than
        `steer_msg` -- is invisible to all of them: measured, the whole file stays
        green under exactly that mutation.

        It has two consequences and this pins both. The map keeps an entry per
        extra steer, which is the leak the accumulation pin exists for. Worse, the
        later entries get the FIRST steer's id stamped on them, so the drained row
        for steer B would name steer A's send and the client would reconcile the
        wrong bubble -- a correctness fault, not just memory. Asserting each entry
        against its OWN id catches that direction; asserting only that the map
        emptied would not.

        The existing accumulation pin drives five ACCEPTED steers, which take the
        terminal tail and enter the requeue loop zero times, so it cannot cover
        this even though it is the same failure mode one layer up.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        texts = ["steer alpha", "steer beta", "steer gamma"]
        slot._pending_steers = list(texts)
        slot._steer_delivery_ids = {t: f"did-{n}" for n, t in enumerate(texts)}
        slot._steer_send_ids = {t: f"s-m4k2p1-{n}" for n, t in enumerate(texts)}

        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        _requeue_unconsumed_steers(state, slot)

        # Requeued at the HEAD in reversed order, so the queue preserves the
        # original pending order (pinned by the ordering test above).
        assert [item["content"] for item in slot._queue] == texts
        paired = {item["content"]: item["meta"].get("sendId") for item in slot._queue}
        assert paired == {t: f"s-m4k2p1-{n}" for n, t in enumerate(texts)}, (
            "each requeued entry must carry ITS OWN send id; a shared or shifted id "
            "makes the drained row name a different send and the client reconcile "
            "the wrong optimistic bubble"
        )
        assert slot._steer_send_ids == {}, (
            "one leaked entry per requeued steer is the growth shape a single-steer "
            "test cannot see"
        )
        assert slot._steer_delivery_ids == {}

    @pytest.mark.asyncio
    async def test_a_hard_kill_drops_the_entry(self, tmp_path, monkeypatch, _patch_sel):
        """A force stop discards the text, so no requeued entry will carry it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.sessions.stop_turn = AsyncMock()
        slot = _running_slot(state)
        slot._stop_state = "soft_pending"  # first press already happened
        slot._pending_steers = [self._TEXT]
        slot._steer_delivery_ids = {self._TEXT: "did-1"}
        slot._steer_send_ids = {self._TEXT: self._SEND_ID}

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/stop?force=true")
            assert resp.status == 200

        assert slot._steer_send_ids == {}, (
            "the hard kill discarded the text, so nothing downstream will ever "
            "read this id -- keeping it holds the message for the slot's lifetime"
        )

    @pytest.mark.asyncio
    async def test_the_real_already_drained_path_finds_the_maps_already_clean(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """Why the fifth pop needs a CONSTRUCTED state to observe: it is defensive.

        The `_row_has_delivery_id` return is reached only when the whole
        requeue-then-drain sequence completed during the steer RPC -- and the
        requeue is what pops both maps, so by the time that return runs they are
        already empty. This drives the REAL sequence (real requeue, real drain) and
        records that precondition, so the constructed pin below is honestly
        labelled a defensive-invariant pin rather than a production-path one.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = _running_slot(state)
        observed: dict[str, object] = {}

        async def _requeue_and_drain(message):
            from kiro_crew.dashboard import chat_runner

            chat_runner._requeue_unconsumed_steers(state, slot)
            with (
                patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
                patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
            ):
                await chat_runner._start_next_queued_turn(state, slot)
            # Snapshot BEFORE the steer path resumes and runs its own pop.
            observed["delivery"] = dict(slot._steer_delivery_ids)
            observed["send"] = dict(slot._steer_send_ids)
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = _requeue_and_drain
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": self._TEXT,
                    "steer": True,
                    "meta": {"sendId": self._SEND_ID},
                },
            )
            assert resp.status == 200
            # `queued` is the STEER_REQUEUED receipt: the row was already written
            # by the drain, so this is the return under discussion.
            assert (await resp.json()).get("queued") is True

        assert observed["send"] == {}, (
            "the requeue already emptied the map, which is why removing the pop at "
            "this return cannot redden a production-path test"
        )
        assert observed["delivery"] == {}, "same precondition for the delivery id"
        # The row the drain wrote carries the id, which is the whole point of the
        # threading -- this return is not a path where the id is lost.
        rows = [m for m in slot.messages if m.get("role") == "user"]
        assert rows and (rows[-1].get("meta") or {}).get("sendId") == self._SEND_ID

    @pytest.mark.asyncio
    async def test_the_already_drained_return_clears_a_populated_map(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """Defensive-invariant pin for the fifth pop site.

        The state is CONSTRUCTED, not produced: the test above shows the real path
        reaches this return with both maps already empty. The pop is kept anyway
        because the lockstep rule -- an entry in one map implies an entry in the
        other -- is what every reader of these two maps relies on, and the existing
        `_steer_delivery_ids` pop at this same return is defensive for exactly the
        same reason. This pin is what makes removing either of them fail.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _write_row_only(message):
            # The drain's effect WITHOUT the requeue's bookkeeping: a durable row
            # carrying this steer's delivery id, both maps left populated. That is
            # what forces `_row_has_delivery_id` true with entries still present.
            did = slot._steer_delivery_ids[message]
            slot.append("user", message, "msg msg-u", meta={"steer_delivery_id": did})
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = _write_row_only
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": self._TEXT,
                    "steer": True,
                    "meta": {"sendId": self._SEND_ID},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        assert slot._steer_send_ids == {}, (
            "the already-drained return must clear the send id in lockstep with the "
            "delivery id, or a reader cannot assume the two maps agree"
        )
        assert slot._steer_delivery_ids == {}
