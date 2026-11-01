"""Tests for the steer-loss fix: unconsumed mid-turn steers are requeued.

A steer handed to kiro-cli lives inside the running turn; if the turn dies
before kiro-cli echoes ``steering_consumed`` (stall-cancel, soft STOP, error,
or a steer racing the turn's natural end) the message would vanish silently.
Pending steers are tracked on the slot:

  * the steer handler registers in ``slot._pending_steers`` BEFORE the steer
    RPC's await (unwound on failure), so a turn dying mid-write still sees it;
  * ``EVENT_STEER_CONSUMED`` settles pending steers matched against the echo's
    ``<user_message>``-wrapped snapshot (late arrivals stay pending; an empty
    echo falls back to settling all);
  * ``_run_chat``'s finally requeues leftovers at the HEAD of the slot queue
    as ordinary, individually-cancellable queue cards (``queue_push``);
  * a hard kill (force stop) discards pending steers alongside the queue —
    mirroring the existing "second press = discard everything" semantics.

What a requeued steer carries onto its queue entry and its row -- the delivery id,
the client's send id and the decision receipt -- and how a requeued, cancelled or
hard-killed steer's bookkeeping ends are pinned in
``test_steer_requeue_entry_meta.py``; the SEL patch and the mid-turn slot both
modules use are in ``steer_requeue_helpers.py``.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
import steer_requeue_helpers
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from steer_requeue_helpers import _running_slot

# The shared fixture, bound here by name so pytest registers it for this module.
_patch_sel = steer_requeue_helpers._patch_sel


class TestDeliveryIdLifecycle:
    """The delivery-id map must not outlive the delivery it identifies.

    It is keyed by the message TEXT, so an entry left behind holds a full
    message string for the slot's whole lifetime. The requeue paths keep theirs
    on purpose -- the drain in `chat_runner` still has to match the id, and that
    entry is bounded by the queue -- but a delivery that persists its own row is
    terminal here and nothing downstream will read it again.

    `_steer_send_ids` is the same shape with the same failure mode, and is
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
                    # removed.
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
        # nothing will read this entry either.
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
                        # a leak, so each send must contribute its own key.
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

    def test_no_site_writes_a_steer_entry(self, tmp_path, monkeypatch):
        """The session vocabulary carries no steer type, and that is deliberate.

        The fact is knowable only from this echo, while the assistant text the steer
        INTERRUPTED reaches the log from the handler's segment cut, which runs when
        `client.steer()` returns. Under stdin backpressure that RPC is still in
        `drain()` when the echo arrives, so an entry written here takes a lower seq
        than the text it cut and a fold reads that text as the reply TO the steer.
        Cutting the segment from here instead flushes post-steer text above the steer
        row in the transcript, which is worse.

        This guards the decision rather than the mechanism, on both halves: the
        emitter exposes no steer entry point to call, and settling still works
        without one. Adding an emitter back at either site, without a resolver that
        owns both facts, reddens this test.

        `subagent/steered` is a different type and is exempt: its site accepts the
        steer and holds both the child's id and the mode at that moment, so there is
        no second observer to wait for and no seq it could contradict. It is named
        here as an exact set rather than skipped by a substring, so a second
        steer-named entry point -- for either family -- still reddens this.
        """
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        assert {name for name in dir(crew_log_emit) if "steer" in name.lower()} == {
            "on_subagent_steered"
        }, (
            "the emitter's steer-named entry points changed; a message steer has no "
            "site that can order one correctly"
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        slot._pending_steers = ["fix the bug", "late arrival"]
        slot._steer_delivery_ids = {"fix the bug": "d-settled", "late arrival": "d-pending"}
        slot._acp_client = MagicMock()

        appended: list[tuple] = []
        monkeypatch.setattr(crew_log_emit, "session_id_of", lambda _c: "acp-1")
        monkeypatch.setattr(crew_log_emit, "_write", lambda *a, **kw: appended.append((a, kw)))

        _settle_consumed_steers(slot, "<user_message>\nfix the bug\n</user_message>", state)

        assert slot._pending_steers == ["late arrival"], "settling itself still works"
        assert appended == [], "the echo wrote a crew log entry it cannot order"

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

    def test_empty_snapshot_settles_nothing(self, tmp_path, monkeypatch):
        # Older backend / redacted echo: no usable text is no evidence of
        # consumption, so everything stays pending for the turn-end requeue.
        # A duplicate card is visible and cancellable; a silent loss is not.
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["a", "b"]
        _settle_consumed_steers(slot, "   ")
        assert slot._pending_steers == ["a", "b"]

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
        slot._steer_possibly_delivered.add("later")
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
        assert "later" not in slot._steer_possibly_delivered
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

        return "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [
                *sorted(Path(cr.__file__).with_name("chat_turn").glob("*.py")),
                Path(cr.__file__),
            ]
            if turn_file.name != "__init__.py"
        )

    def test_finally_calls_requeue_before_queue_drain(self):
        import inspect

        import kiro_crew.dashboard.chat_runner as cr

        # Scoped to the one hand-off every turn exit runs, so a requeue call
        # anywhere else in the module cannot satisfy the order.
        src = inspect.getsource(cr._hand_off_queue)
        requeue_at = src.index("_requeue_unconsumed_steers(state, slot)")
        drain_at = src.index("await _start_next_queued_turn(", requeue_at)
        assert requeue_at < drain_at, (
            "_hand_off_queue must call _requeue_unconsumed_steers BEFORE "
            "the queue drain so a requeued steer is delivered on the very next turn"
        )
        # The hand-off is the tail's last step, and the turn's finally ends in it.
        assert "await _hand_off_queue(" in inspect.getsource(cr._end_turn_tail)
        assert (
            "await run_to_completion(_end_turn_tail(state, slot, turn_exit))"
            in inspect.getsource(cr._run_chat)
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
        assert src.index("await _on_steer_consumed(turn, event)", branch_at) - branch_at < 200
        phase_at = src.index("async def _on_steer_consumed(")
        # The phase's entry reads (``x = turn.x``) are plumbing, not body: measure past them.
        body = re.sub(r"(?m)^[ \t]*(\w+) = turn\.\1\n", "", src[phase_at:])
        settle_at = body.index("_settle_consumed_steers(slot, event.text")
        # the settle call must be the branch body (within a few lines of its phase's head)
        assert settle_at < 400

    def test_steer_handler_registers_before_await(self):
        from pathlib import Path

        import kiro_crew.dashboard.chat_delivery as cd

        src = Path(cd.__file__).read_text(encoding="utf-8")
        register_at = src.index("slot._pending_steers.append(message)")
        await_at = src.index("await client.steer(wire_message)")
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
        # ahead of the message already queued
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
    as its own turn. Claiming a successful mid-turn injection from write-ack
    alone would render "steered into the running turn" for a turn that was never
    redirected.

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
    async def test_a_consumed_steer_retires_a_late_stateless_question(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """Provider consumption closes the card-registration ordering gap.

        The dashboard persists the user's steer row as soon as the steer RPC
        accepts it. The agent can then post an ``ask_question`` card before
        kiro-cli emits ``steering_consumed`` for that same user message. The
        earlier row append cannot retire a card that did not exist yet, so the
        consumption event must finish that lifecycle without disturbing a
        legacy blocking ask.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.broadcast_ws_owners = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        # The accepted steer writes its row first. The card is registered only
        # afterwards, reproducing the ordering from the dashboard report.
        await steer_into_running_turn(state, slot, "build the recommended option")
        state.mark_question_pending(
            "test",
            blocking=False,
            card_id="card-late",
            questions=[{"question": "Which option?", "options": [{"label": "B"}]}],
        )
        state.mark_question_pending("test", blocking=True, card_id="ask-parked")

        _settle_consumed_steers(
            slot,
            "<user_message>\nbuild the recommended option\n</user_message>",
            state,
        )

        assert list(slot._question_pending) == ["ask-parked"]
        state.broadcast_ws_owners.assert_any_call(
            "question_card_resolved", {"card_id": "card-late", "slot": "test"}
        )

    def test_unmatched_or_empty_echo_does_not_retire_a_question(self, tmp_path, monkeypatch):
        """Only positive consumption evidence closes the answer channel."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws_owners = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._pending_steers = ["still pending"]
        state.mark_question_pending(
            "test",
            blocking=False,
            card_id="card-live",
            questions=[{"question": "Which option?", "options": [{"label": "B"}]}],
        )

        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        _settle_consumed_steers(slot, "<user_message>\nsomething else\n</user_message>", state)
        assert list(slot._question_pending) == ["card-live"]

        # Legacy empty echoes settle the steer list to avoid message loss, but
        # they prove no user message was consumed and must not retire the card.
        _settle_consumed_steers(slot, "", state)
        assert list(slot._question_pending) == ["card-live"]
        state.broadcast_ws_owners.assert_not_called()

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
        This is the mirror case: overstating and understating are
        both the row disagreeing with the backend.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _consume_during_rpc(_msg):
            # Drive the REAL settle with a REAL echo rather than hand-clearing the
            # list. A bare `_pending_steers.clear()` would reproduce an
            # EVIDENCE-FREE sweep, not a matched echo -- an injection
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

        An empty frame settles nothing, so the entry stays registered -- the
        still-registered path, which yields `written`. If `chat_delivery` inferred
        `consumed` from the entry being gone, a frame that proved nothing becomes
        a success badge -- and a row persisted as `consumed` is terminal, so
        nothing would correct it. That is the defect this change removes, reached
        by a different route.
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
        # The empty echo settled nothing, so the entry is still registered when
        # the RPC resumes -- the still-registered path, which yields `written`.
        assert slot._pending_steers == ["go north"]
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
        for a pending steer is a separate refactor, out of scope here.
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
        the row resolver must not disagree with it and leave two CONFIRMED
        injections reading `written` for the slot's life. That understates the
        state -- the mirror case -- and needs no real steer identity, which is a
        separate refactor.
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
        other way would confirm a steer whose attribution is unknown -- the
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
        """An empty echo leaves the pending list untouched and promotes no row.

        An empty EVENT_STEER_CONSUMED echo must not clear the whole pending list
        with no evidence: doing so suppresses `_requeue_unconsumed_steers` and
        the user's correction is silently lost while its row claims delivery. An
        empty echo is no evidence of consumption (`steer_settle` says exactly that),
        so the entry must stay pending, nothing may be promoted, and the
        turn-end requeue must render a visible, cancellable queue card.
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
        from kiro_crew.dashboard.chat_runner import (
            _requeue_unconsumed_steers,
            _settle_consumed_steers,
        )

        await steer_into_running_turn(state, slot, "go north")
        row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        state.broadcast_ws.reset_mock()

        _settle_consumed_steers(slot, "   ", state)

        # the empty echo settled NOTHING: the entry is still pending
        assert slot._pending_steers == ["go north"]
        # and nothing was CLAIMED about the row
        assert row["meta"].get("steerState") == "written"
        assert not any(
            c.args[0] == "chat_message_update" for c in state.broadcast_ws.call_args_list
        )

        # the turn ends with the entry still pending, so the teardown requeue
        # (wired into _run_chat's outer finally) renders a cancellable card
        # instead of the correction being silently dropped
        _requeue_unconsumed_steers(state, slot)

        assert [entry["content"] for entry in slot._queue] == ["go north"]
        assert slot._pending_steers == []
        assert row["meta"].get("steerState") == "requeued"

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


class TestPeerSteerOnCodex:
    """codex steer is accepted only from the session's own composer.

    A codex steer can be lost to a later denied approval, and only the human
    watching the composer can notice and resend, so a peer's steer
    (``session_send``, ``user_origin=False``) is refused before any RPC and the
    caller falls back to its queue path.
    """

    @pytest.mark.asyncio
    async def test_a_peer_steer_into_codex_is_refused_before_the_rpc(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        from kiro_crew.dashboard.chat_delivery import STEER_UNAVAILABLE, steer_into_running_turn

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client = MagicMock()
        client.supports_steer = True
        client.steer_needs_loss_recovery = True
        client.steer = AsyncMock(return_value=True)
        slot._acp_client = client

        outcome = await steer_into_running_turn(state, slot, "from a peer", user_origin=False)
        assert outcome == STEER_UNAVAILABLE
        client.steer.assert_not_awaited()
        assert slot._pending_steers == []

    @pytest.mark.asyncio
    async def test_the_composer_still_steers_codex(self, tmp_path, monkeypatch, _patch_sel):
        from kiro_crew.dashboard.chat_delivery import STEER_UNAVAILABLE, steer_into_running_turn

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client = MagicMock()
        client.supports_steer = True
        client.steer_needs_loss_recovery = True
        client.steer = AsyncMock(return_value=True)
        slot._acp_client = client

        outcome = await steer_into_running_turn(state, slot, "typed here", user_origin=True)
        assert outcome != STEER_UNAVAILABLE
        client.steer.assert_awaited_once()
