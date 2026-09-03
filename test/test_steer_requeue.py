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
