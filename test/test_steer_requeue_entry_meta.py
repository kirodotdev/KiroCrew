"""What a requeued steer carries onto its queue entry and row, and how it ends.

The companion of ``test_steer_requeue.py``, which pins how an unconsumed mid-turn
steer is tracked on the slot, requeued when its turn dies, and labelled on its row.
A requeued steer persists nothing itself: the teardown degrades it into a queue card
and the drain writes the row, so everything the row must say travels one step
further, on the entry's meta. This module pins that journey -- the delivery id a
cancelled card takes with it, the client's send id and its map's every pop site, the
``message.steer`` decision receipt -- and the hard kill that discards pending steers
with the queue.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import steer_requeue_helpers
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from steer_requeue_helpers import _running_slot

# The shared fixture, bound here by name so pytest registers it for this module.
_patch_sel = steer_requeue_helpers._patch_sel


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

    An ACCEPTED steer persists its own row and stamps `meta.sendId` there.
    A REQUEUED steer does not persist anything: the teardown degrades it
    into a queue card and the DRAIN writes the row. So the id has to travel one
    step further -- registration, queue entry meta, drained row -- or the row is
    id-less and `mergePreservedThinking` has nothing to resolve the tab's
    optimistic bubble against, leaving the pre-steer thinking chip stranded at the
    tail until a reload.

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
    -- are already pinned in test_steer_requeue.py by the delivery-id lifecycle tests now that their
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
        # original pending order (pinned by the ordering test in test_steer_requeue.py).
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


class TestRequeuedSteerKeepsItsDecisionReceipt:
    """A steer a `message.steer` decision CHOSE must reach its row with the receipt.

    Both outcomes the decision can choose already stamp one: the steer path puts it
    on the row it persists, `queue_for_next_turn` puts it on the entry it appends.
    The third path is neither -- the turn ended while the steer RPC was suspended,
    so the teardown degrades the steer into a queue card and the DRAIN writes the
    row. The receipt therefore has to travel registration -> entry meta -> row, the
    same one step further the client's `sendId` travels, or the one outcome a
    decision did choose lands as a row that reads as undecided.
    """

    _TEXT = "use the cached build"
    _STRIP = {"point": "message.steer", "decision": "steer", "id": "d-1"}

    @pytest.mark.asyncio
    async def test_requeued_entry_meta_carries_the_receipt(self, tmp_path, monkeypatch):
        """Registration -> entry meta, driving the real requeue from inside the RPC."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _steer(message):
            from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

            _requeue_unconsumed_steers(state, slot)
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = _steer
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import STEER_REQUEUED, steer_into_running_turn

        outcome = await steer_into_running_turn(state, slot, self._TEXT, decision_strip=self._STRIP)
        assert outcome == STEER_REQUEUED
        assert slot._queue[0]["meta"].get("decisions_strip") == self._STRIP, (
            "the requeued entry must carry the decision receipt -- the drain unions "
            "entry meta onto the row it writes, so this is the only writer a steer "
            "that never persists its own row has"
        )
        assert slot._steer_decision_strips == {}, "popped in lockstep with the other maps"

    @pytest.mark.asyncio
    async def test_drained_row_carries_the_receipt(self, tmp_path, monkeypatch):
        """Entry meta -> row meta: the row is what the transcript reader reads."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = state.get_or_create_slot("test")
        slot._pending_steers = [self._TEXT]
        slot._steer_delivery_ids = {self._TEXT: "did-1"}
        slot._steer_decision_strips = {self._TEXT: self._STRIP}

        from kiro_crew.dashboard import chat_runner

        chat_runner._requeue_unconsumed_steers(state, slot)
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        rows = [m for m in slot.messages if m.get("role") == "user"]
        assert rows, "the drain must have written a user row for the requeued steer"
        assert (rows[-1].get("meta") or {}).get("decisions_strip") == self._STRIP

    @pytest.mark.asyncio
    async def test_a_row_that_already_carries_the_receipt_does_not_requeue_it(
        self, tmp_path, monkeypatch
    ):
        """The live-then-died path must not put ONE decision's receipt on two rows.

        A delivery still registered when it reaches the persisting tail stamps the
        receipt on the row it writes there, and the turn can still die afterwards and
        requeue the text. Carrying the receipt into that requeue too would stamp it on
        the queue entry, the drain would union it onto a second row, and
        `_mark_steer_row_state` rewrites only `steerState` -- so both the corrected
        REQUEUED row and the drained row would claim the decision and neither copy is
        ever removed. `sendId` is dropped at that same site for the same reason.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

        assert (
            await steer_into_running_turn(state, slot, self._TEXT, decision_strip=self._STRIP)
            == STEER_STEERED
        )
        # The row this path persisted is where the receipt lives.
        _rows = [m for m in slot.messages if m.get("role") == "user"]
        assert (_rows[-1].get("meta") or {}).get("decisions_strip") == self._STRIP

        # The turn ends without the echo, so its teardown degrades the live steer.
        chat_runner._requeue_unconsumed_steers(state, slot)
        assert "decisions_strip" not in (slot._queue[0].get("meta") or {}), (
            "the persisted row already carries this decision; a second copy on the "
            "drained row would render one decision as two receipts"
        )

    @pytest.mark.asyncio
    async def test_a_settled_steer_releases_its_receipt(self, tmp_path, monkeypatch):
        """The other side of that hold: an echo-confirmed steer keeps nothing.

        The map is keyed by message TEXT, so an entry left behind holds a whole
        message for the slot's lifetime -- and a steer the echo accounted for is
        never requeued, so nothing will read it again.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._pending_steers = [self._TEXT]
        slot._steer_decision_strips = {self._TEXT: self._STRIP}

        from kiro_crew.dashboard import chat_runner

        chat_runner._settle_consumed_steers(
            slot, f"<user_message>\n{self._TEXT}\n</user_message>", state
        )

        assert slot._pending_steers == []
        assert slot._steer_decision_strips == {}

    @pytest.mark.asyncio
    async def test_a_manual_steer_keeps_the_prior_entry_shape(self, tmp_path, monkeypatch):
        """Additive: an undecided steer has no receipt, and its entry must not gain one.

        Pinned as an ABSENT KEY rather than a falsy value: the entry meta is
        persisted with the queue and reaches the row, where an empty receipt would
        render as a decision that never happened.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _steer(message):
            from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

            _requeue_unconsumed_steers(state, slot)
            return True

        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = _steer
        slot._acp_client = client_mock

        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

        await steer_into_running_turn(state, slot, self._TEXT)
        assert "decisions_strip" not in slot._queue[0]["meta"]
