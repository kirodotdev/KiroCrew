"""What a work-ledger wake actually DELIVERS, and what happens when it lands mid-turn.

Two defects, one delivery path.

A conductor arms ``monitor_start`` with ``watch="work-ledger"``. A bound worker then
reports ``done``, ``blocked`` or ``question``, the crew-log bus pulls the conductor's
tick forward, and the kernel answers WAKE.

* If the conductor happened to be mid-turn, the fire was declined as BUSY while the
  kernel had already committed its dedupe and the probe had already charged the item's
  wake budget -- so nothing re-raised the observation, and the turn-complete hook armed
  toward the loop's own deadline. Measured 14 to 30 minutes late on real boards.
* And when a wake DID land, the delivered turn carried the cycle header and the loop's
  own patrol message alone: the probe's briefs lived only in the verdict the gate read
  and threw away, so the conductor was not told which item had moved and spent a
  ``work_ledger_read`` finding out.

The fix is one seam -- the wake's text becomes a value the claim carries from the gate
to delivery -- and these cases pin both ends of it: the text reaches the turn (with the
board built at delivery, not at observation), and a wake that lands mid-turn is steered
into the running turn rather than held, with the turn-end retry behind it when a steer
is not available.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import irq, ledger_wake, work_ledger
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import work_ledger as wl_routes
from kiro_crew.monitoring.models import MONITOR_STATE_VERSION, MonitorState
from kiro_crew.slack import gateway as gw

CONDUCTOR = "chat-conductor"
WORKER = "chat-worker"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home per test, so no ledger or rate file outlives its own test.

    ``_floor_monkeypatch``, not the test-owned ``monkeypatch``: an autouse fixture
    patching through the shared one is lifted by any test that calls
    ``monkeypatch.undo()``, which would point the rest of that test at the real
    data home.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


# ── the text policy ───────────────────────────────────────────────────────


class TestWakeTurnText:
    """``ledger_wake.wake_turn_text``: the news first, then the board."""

    def test_the_briefs_come_before_the_board(self):
        text = ledger_wake.wake_turn_text("[work-ledger wake] item=it_1 status=done", '{"a": 1}')
        assert text.index("item=it_1") < text.index(ledger_wake.SNAPSHOT_HEADER)
        assert text.index(ledger_wake.SNAPSHOT_HEADER) < text.index('{"a": 1}')

    def test_a_snapshot_the_store_could_not_build_still_delivers_the_briefs(self):
        """The shape that shipped before the snapshot existed must still work."""
        assert ledger_wake.wake_turn_text("[work-ledger wake] item=it_1 status=done", "") == (
            "[work-ledger wake] item=it_1 status=done"
        )

    def test_briefs_the_kernel_did_not_hand_over_still_deliver_the_board(self):
        text = ledger_wake.wake_turn_text("", '{"a": 1}')
        assert text.startswith(ledger_wake.SNAPSHOT_HEADER)
        assert '{"a": 1}' in text

    def test_neither_half_is_no_text_at_all(self):
        """So the caller delivers the plain nudge rather than an empty header."""
        assert ledger_wake.wake_turn_text("", "") == ""

    def test_an_over_long_brief_list_loses_its_tail_and_not_the_board(self):
        """A kernel or probe defect must not push the board out of the turn."""
        text = ledger_wake.wake_turn_text("x" * (ledger_wake.MAX_WAKE_BRIEF_CHARS * 3), "BOARD")
        assert "(brief list truncated)" in text
        assert "BOARD" in text, "the snapshot must survive an over-long brief list"
        assert len(text) < ledger_wake.MAX_WAKE_BRIEF_CHARS * 2


# ── the snapshot, built at delivery ───────────────────────────────────────


def _seed_board() -> str:
    """One conductor, one bound worker that reported ``done``. The item id."""
    work_ledger.ensure_conductor(CONDUCTOR, goal="ship the thing")
    created = work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title="do the thing", acceptance={"kind": "human_approval"}
    )
    item_id = str(created["item"].item_id)
    work_ledger.apply_conductor_action(
        CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER
    )
    work_ledger.apply_worker_report(CONDUCTOR, item_id, status="done", summary="opened the PR")
    return item_id


def _state() -> SimpleNamespace:
    """A dashboard state whose slot table answers "nothing is open"."""
    return SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        slot_exists=MagicMock(return_value=False),
        # The two the restricted-session predicate reads, both empty: this
        # conductor is an ordinary persistent dashboard session.
        _slots={},
        _restricted_keys=set(),
        sessions=None,
    )


def _wake(
    text: str, *, briefs: str | None = None, slot: object = None, admission: dict | None = None
):
    """A ``WakeSnapshot`` for the cases that stand in for the board builder.

    *briefs* defaults to *text* so a case that does not care which half a path
    takes still reads the same string from either.
    """
    return wl_routes.WakeSnapshot(text, text if briefs is None else briefs, slot, admission)


def _admitted():
    """Patch the containment gate to its ADMITTED answer, recording nothing.

    The gate itself is exercised by the refusal cases below and by the one that
    asserts the admitted audience is recorded. A test about the board's SHAPE
    should not also have to stand up a slot table, a verified channel surface and
    an owner roster for ``judge_owner_dm`` to read, and with none of those the
    real predicate fails closed -- correctly -- and there is no board to inspect.
    """
    return patch.object(wl_routes, "caller_admission", return_value=("", None, None))


class TestCompactWakeSnapshot:
    @pytest.mark.asyncio
    async def test_it_carries_the_compact_columns_and_the_accept_batch(self):
        item_id = _seed_board()
        doc = json.loads((await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text)
        assert doc["compact"] is True
        assert [row["item_id"] for row in doc["items"]] == [item_id]
        row = doc["items"][0]
        assert row["status"] == "done", "the report that woke the conductor must be in the board"
        assert row["summary"] == "opened the PR"
        assert set(row) == set(wl_routes._COMPACT_ROW_FIELDS), (
            "the snapshot must publish exactly the columns the compact read does, or a "
            "conductor is shown one board by its wake and another by its read"
        )
        assert "accept_batch" in doc, "a done report's next move is to evaluate its bar"

    @pytest.mark.asyncio
    async def test_it_carries_no_acceptance_bars_and_no_event_tails(self):
        """The compact contract: nothing that is a document in its own right."""
        _seed_board()
        doc = json.loads((await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text)
        assert "acceptance" not in doc["items"][0]
        assert "events" not in doc["items"][0]

    @pytest.mark.asyncio
    async def test_a_session_with_no_ledger_gets_no_snapshot(self):
        """And the wake then delivers its briefs alone, as it did before."""
        assert (await wl_routes.compact_wake_snapshot(_state(), "chat-nobody")).text == ""

    @pytest.mark.asyncio
    async def test_a_ledger_with_no_items_yet_gets_no_snapshot(self):
        """An empty board is a header over nothing; the briefs say more."""
        work_ledger.ensure_conductor(CONDUCTOR, goal="ship the thing")
        with _admitted():
            assert (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text == ""

    @pytest.mark.asyncio
    async def test_an_unnameable_slot_gets_no_snapshot(self):
        assert (await wl_routes.compact_wake_snapshot(_state(), "")).text == ""

    @pytest.mark.asyncio
    async def test_a_restricted_session_gets_no_snapshot(self):
        """A ledger is durable state those modes promise not to leave behind."""
        _seed_board()
        with patch.object(wl_routes, "_is_restricted_session_key", return_value=True):
            assert (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text == ""

    @pytest.mark.asyncio
    async def test_a_session_reaching_a_channel_gets_no_snapshot(self):
        """The gate ``work_ledger_read`` applies, applied here through the same call.

        A conductor tab mirrored to a channel that is not the owner's own DM would
        otherwise have the board, worker prose included, republished to that channel
        by the very turn this text enters.
        """
        _seed_board()
        with patch.object(
            wl_routes,
            "caller_admission",
            return_value=("the mirror is not the owner's DM", None, None),
        ):
            assert (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text == ""

    @pytest.mark.asyncio
    async def test_an_audience_that_widens_during_the_read_gets_no_snapshot(self):
        """The post-read re-check, for the reason the route takes one."""
        _seed_board()
        with (
            patch.object(wl_routes, "caller_admission", return_value=("", None, None)),
            patch.object(
                wl_routes, "contained_channel_caller", return_value="a mirror was retargeted"
            ),
        ):
            assert (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text == ""

    @pytest.mark.asyncio
    async def test_an_admitted_read_hands_its_audience_out_rather_than_recording_it(self):
        """The recorder writes only inside a runner turn, and a wake to an IDLE
        conductor is built before its turn starts.

        Recording here would be a no-op on that path, and the publisher, finding no
        entry, would republish board-derived text to a mirror linked during the very
        turn this board enters. So the admission travels WITH the board and whoever
        owns the receiving turn fences it.
        """
        _seed_board()
        slot = object()
        admission = {"linked": False}
        with (
            patch.object(wl_routes, "caller_admission", return_value=("", slot, admission)),
            patch.object(wl_routes.session_control, "record_audience_admission") as recorded,
        ):
            built = await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)
        assert built.text
        assert built.slot is slot
        assert built.admission is admission
        recorded.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_successful_read_is_audited_and_not_only_its_refusals(self):
        """A trail of refusals alone says nothing about what was disclosed."""
        _seed_board()
        with _admitted(), patch.object(wl_routes, "_audit") as audited:
            assert (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text
        assert any(
            call.args[2] == "ok" and call.args[1] == wl_routes._WAKE_OPERATION
            for call in audited.call_args_list
        ), f"no ok row was written: {audited.call_args_list}"

    @pytest.mark.asyncio
    async def test_a_dirty_cache_is_never_served_as_the_board(self):
        """It may hold a mutation the record never saw, and this text tells the
        conductor not to re-read."""
        _seed_board()
        with patch.object(wl_routes.work_ledger, "cache_dirty", return_value="an undo failed"):
            assert (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text == ""

    @pytest.mark.asyncio
    async def test_a_store_failure_costs_the_snapshot_and_never_the_wake(self):
        _seed_board()
        with patch.object(wl_routes.work_ledger, "list_work_items", side_effect=OSError("gone")):
            assert (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text == ""

    @pytest.mark.asyncio
    async def test_it_is_bounded_and_stays_valid_json(self):
        """Trimmed by the tool layer's own fitter, so a cut never tears the document."""
        _seed_board()
        text = (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR, budget=400)).text
        assert len(text) <= 400
        json.loads(text)

    @pytest.mark.asyncio
    async def test_the_conductors_prefixed_slot_spelling_reads_its_own_board(self):
        """One session is legitimately spelled both ways; the fold is why it works."""
        item_id = _seed_board()
        with _admitted():
            doc = json.loads(
                (await wl_routes.compact_wake_snapshot(_state(), f"dashboard_{CONDUCTOR}")).text
            )
        assert [row["item_id"] for row in doc["items"]] == [item_id]

    @pytest.mark.asyncio
    async def test_nothing_from_the_snapshot_reaches_the_watch_state_directory(self):
        """The boundary ``wake_brief`` exists for, asserted on the files themselves.

        ``irq`` persists an observation's brief beside the watch state, which is not
        under the store's identity-gated read path. So the snapshot is built at
        delivery and must leave no trace there -- checked by waking the kernel over
        the same board and grepping every file it wrote for the worker's prose.
        """
        _seed_board()
        from kiro_crew.probes import work_ledger as probe_mod

        config = json.dumps({"conductor": CONDUCTOR})
        verdict = await asyncio.to_thread(
            irq.poll, "loop-snapshot", config, probe_mod.WorkLedgerProbe()
        )
        assert verdict.outcome is irq.Outcome.WAKE
        with _admitted():
            snapshot = (await wl_routes.compact_wake_snapshot(_state(), CONDUCTOR)).text
        assert "opened the PR" in snapshot, "the control: the prose IS in the snapshot"
        root = irq.state_path("work-ledger", CONDUCTOR, "probe").parent
        written = [path for path in root.rglob("*") if path.is_file()]
        assert written, "the kernel wrote no state at all, so this proves nothing"
        for path in written:
            body = path.read_text(encoding="utf-8", errors="replace")
            assert "opened the PR" not in body, f"worker prose reached {path.name}"


# ── the claim carries the kernel's text ───────────────────────────────────


def _service(tmp_path) -> AutoNudgeService:
    return AutoNudgeService(base_dir=tmp_path / "svc")


def _monitor() -> MonitorState:
    """The record ``add(..., watch="work-ledger")`` builds, in its fields that matter.

    ``objective`` is ``review_ready`` because the arming path sets it for every
    monitor kind; nothing on this path reads it, and spelling it here keeps the
    fixture a record the service would accept.
    """
    return MonitorState(
        kind="work-ledger",
        target=CONDUCTOR,
        objective="review_ready",
        created_ts=1_000.0,
        version=MONITOR_STATE_VERSION,
    )


def _ledger_loop(slot_key: str = CONDUCTOR) -> NudgeLoop:
    return NudgeLoop(
        id="loop-wl",
        slot_key=slot_key,
        message="patrol the work ledger",
        idle_secs=1200,
        monitor=_monitor(),
        gate=True,
    )


class TestTheClaimCarriesTheWakeText:
    @pytest.mark.asyncio
    async def test_a_refused_fire_re_owes_the_text_with_the_claim(self, tmp_path):
        """The headline accounting rule: a refusal must leave the news deliverable.

        The retry takes the gate's observation-free bypass, so it never re-reads the
        probe and the briefs cannot be rebuilt. An empty re-owe would deliver the
        wake as a bare cycle header, which is the defect the text exists to close.
        """
        service = _service(tmp_path)
        loop = _ledger_loop()
        service._loops[loop.id] = loop
        service._pending_monitor_wake[loop.id] = "[work-ledger wake] item=it_1 status=done"
        service._on_fire = AsyncMock(return_value=False)
        try:
            await service._run_fire_cycle(loop)
            assert loop.id in service._pending_monitor_wake, "the claim stays owed"
            assert service.pending_wake_briefs(loop.id) == (
                "[work-ledger wake] item=it_1 status=done"
            ), "and so does the text it was carrying"
        finally:
            service.stop()

    @pytest.mark.asyncio
    async def test_a_delivered_fire_spends_the_text(self, tmp_path):
        """So a later floor tick cannot re-announce news a turn already carried."""
        service = _service(tmp_path)
        loop = _ledger_loop()
        service._loops[loop.id] = loop
        service._pending_monitor_wake[loop.id] = "[work-ledger wake] item=it_1 status=done"
        service._on_fire = AsyncMock(return_value=True)
        try:
            await service._run_fire_cycle(loop)
            assert service.pending_wake_briefs(loop.id) == ""
        finally:
            service.stop()

    def test_the_gate_stores_the_kernels_body_on_the_wake_arm(self, tmp_path):
        """``verdict.body`` is the only holder of the probe's briefs."""
        service = _service(tmp_path)
        loop = _ledger_loop()
        service._loops[loop.id] = loop
        try:
            with patch.object(
                irq,
                "poll",
                return_value=irq.Verdict(irq.Outcome.WAKE, "[work-ledger wake] item=it_1 done"),
            ):
                quiet = asyncio.run(service._monitor_tick_is_quiet(loop))
            assert quiet is False, "a wake must spend a turn"
            assert service.pending_wake_briefs(loop.id) == "[work-ledger wake] item=it_1 done"
        finally:
            service.stop()


# ── a report that lands while the conductor is mid-turn ──────────────────


class TestAWakeOwedAfterTheTurnEnds:
    """The guaranteed fallback: the retry runs now, not at the loop's deadline."""

    def test_the_turn_end_retries_an_owed_wake_at_delay_zero(self, tmp_path):
        service = _service(tmp_path)
        loop = _ledger_loop()
        loop.next_due_ts = 0.0
        service._loops[loop.id] = loop
        armed: list[float | None] = []
        try:
            with (
                patch.object(service, "_arm_timer", lambda _loop, delay=None: armed.append(delay)),
                patch.object(service, "_arm_from_deadline", lambda _loop: armed.append("deadline")),
            ):
                service._pending_monitor_wake[loop.id] = "[work-ledger wake] item=it_1 status=done"
                service.notify_turn_complete(loop.slot_key)
            assert armed == [0.0], (
                "a wake the busy slot refused must be retried as soon as that turn ends, "
                "not one whole patrol interval later"
            )
        finally:
            service.stop()

    def test_a_turn_end_with_nothing_owed_still_arms_toward_the_deadline(self, tmp_path):
        """The unchanged path: a user turn defers a pending fire, it does not pull it in."""
        service = _service(tmp_path)
        loop = _ledger_loop()
        service._loops[loop.id] = loop
        armed: list[object] = []
        try:
            with (
                patch.object(service, "_arm_timer", lambda _loop, delay=None: armed.append(delay)),
                patch.object(service, "_arm_from_deadline", lambda _loop: armed.append("deadline")),
            ):
                service.notify_turn_complete(loop.slot_key)
            assert armed == ["deadline"]
        finally:
            service.stop()

    def test_a_turn_ending_mid_fire_still_defers_rather_than_cancelling(self, tmp_path):
        """The owed-wake branch must not reach past the fire window's own guard.

        ``_arm_timer`` cancels the running timer task, which during the fire window
        may be parked writing the delivered cycle.
        """
        service = _service(tmp_path)
        loop = _ledger_loop()
        service._loops[loop.id] = loop
        armed: list[object] = []
        try:
            service._firing.add(loop.id)
            service._pending_monitor_wake[loop.id] = "[work-ledger wake] item=it_1 status=done"
            with (
                patch.object(service, "_arm_timer", lambda _loop, delay=None: armed.append(delay)),
                patch.object(service, "_arm_from_deadline", lambda _loop: armed.append("deadline")),
            ):
                service.notify_turn_complete(loop.slot_key)
            assert armed == [], "nothing may arm during the fire window"
            assert loop.id in service._rearm_pending
        finally:
            service.stop()


# ── steering the wake into the running turn ───────────────────────────────


def _orchestrator() -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
        # Read by the snapshot's restricted-session gate, as in ``_state()``.
        _slots={},
        _restricted_keys=set(),
        sessions=None,
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch.autonudge_svc.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
    orch.autonudge_svc.pending_wake_briefs = MagicMock(
        return_value="[work-ledger wake] item=it_1 status=done"
    )
    orch._session_tasks = {}
    return orch


def _slot(*, running: bool = False, mode: str = "") -> MagicMock:
    slot = MagicMock()
    slot.key = CONDUCTOR
    slot.running = running
    slot.is_closing = False
    slot.mode = mode
    slot.memory_mode = "persistent"
    slot._last_turn_structural_terminal = False
    slot._last_turn_structural_terminal_loop_id = ""
    slot._last_turn_structural_terminal_loop_gen = 0
    return slot


def _fake_spawn():
    """Stand-in for ``spawn_guarded_turn`` that does not run the turn."""
    calls: list[object] = []

    def _spawn(state, slot, coro, **kwargs):
        coro.close()
        calls.append(slot)
        return MagicMock(name="turn-task")

    _spawn.calls = calls  # type: ignore[attr-defined]
    return _spawn


def _running_spawn():
    """Stand-in for ``spawn_guarded_turn`` that RUNS the turn coro to completion.

    The audience fence is recorded inside the turn coro, right before ``_run_chat``,
    so only a spawn that actually runs the coro records it -- which is the whole
    point of recording it there rather than before the spawn. ``tasks`` exposes the
    scheduled task so the test can await it.
    """
    tasks: list[object] = []

    def _spawn(state, slot, coro, **kwargs):
        task = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    _spawn.tasks = tasks  # type: ignore[attr-defined]
    return _spawn


class TestSteeringAWakeIntoARunningTurn:
    @pytest.mark.asyncio
    async def test_a_busy_conductor_is_steered_rather_than_skipped(self):
        """The bar: the report reaches the conductor WITHIN the turn it lands in."""
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        steer = AsyncMock(return_value="steered")
        with (
            patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer),
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch.object(
                orch, "_work_ledger_wake_text", AsyncMock(return_value=_wake("WAKE TEXT"))
            ) as text,
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        assert (
            text.await_count == 1
        ), "the board costs a ledger read, so both delivery routes must share one build"
        assert steer.await_args.args[2] == "WAKE TEXT"  # the briefs half
        assert steer.await_args.kwargs["user_origin"] is False, (
            "nobody typed this into the session's own surface, so it must not inherit "
            "the composer's exemption from the drain's linked drop"
        )

    @pytest.mark.asyncio
    async def test_the_steer_carries_the_briefs_and_never_the_board(self):
        """THE BOUNDARY: ledger prose may enter only a turn fenced for its whole life.

        A steer's text can be requeued -- the teardown clears the running turn's
        fence and the drain starts a successor nothing re-fences -- so the board
        cannot ride it. The briefs can: they are item ids and statuses, the text
        ``irq`` is already allowed to persist outside the store's gated read path.
        """
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        steer = AsyncMock(return_value="steered")
        with (
            patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer),
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch("kiro_crew.dashboard.session_control.record_audience_admission") as fenced,
            patch.object(
                orch,
                "_work_ledger_wake_text",
                AsyncMock(
                    return_value=_wake(
                        "[work-ledger wake] item=it_1 status=done\n\nTHE BOARD",
                        briefs="[work-ledger wake] item=it_1 status=done",
                        slot=object(),
                        admission={"linked": False},
                    )
                ),
            ),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        injected = steer.await_args.args[2]
        assert injected == "[work-ledger wake] item=it_1 status=done"
        assert "THE BOARD" not in injected, "ledger prose must not ride a steer"
        fenced.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_idle_turn_is_fenced_as_a_pending_turn(self):
        """The turn that runs records the fence right before ``_run_chat``.

        The fence lives inside the turn coro, after admission, so a spawn that runs
        the coro records it as a pending turn -- the one ``_run_chat`` is guaranteed
        to clear in its teardown.
        """
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        target, admission = object(), {"linked": False}
        spawn = _running_spawn()
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch.object(gw, "_run_chat", new=AsyncMock()),
            patch("kiro_crew.dashboard.session_control.record_audience_admission") as fenced,
            patch.object(
                orch,
                "_work_ledger_wake_text",
                AsyncMock(return_value=_wake("WAKE", slot=target, admission=admission)),
            ),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
            # Run the turn coro the spawn scheduled through to its _run_chat.
            await asyncio.gather(*spawn.tasks, return_exceptions=True)
        assert fenced.call_args.args[:2] == (target, admission)
        assert fenced.call_args.kwargs["for_pending_turn"] is True

    @pytest.mark.asyncio
    async def test_a_cancelled_queued_wake_leaves_no_fence(self):
        """A queued wake cancelled before ``_run_chat`` must record NO fence.

        The regression this guards: recording the fence BEFORE ``spawn_guarded_turn``
        meant a wake cancelled or timed out waiting for background capacity -- so
        ``_run_chat`` never ran and its teardown never fired -- left a fence nothing
        cleared, and the publisher then withheld the next unrelated mirror reply
        (fail-closed). With the fence inside the turn coro, right before
        ``_run_chat``, a turn that never reaches that statement records nothing.
        """
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        target, admission = object(), {"linked": False}
        # ``_fake_spawn`` closes the coro without running it -- exactly a wake that
        # never reaches ``_run_chat`` because its admission was cancelled/timed out.
        with (
            patch.object(gw, "spawn_guarded_turn", _fake_spawn()),
            patch.object(gw, "_run_chat", new=AsyncMock()),
            patch("kiro_crew.dashboard.session_control.record_audience_admission") as fenced,
            patch.object(
                orch,
                "_work_ledger_wake_text",
                AsyncMock(return_value=_wake("WAKE", slot=target, admission=admission)),
            ),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        fenced.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_requeued_steer_counts_as_delivered(self):
        """The teardown put the text at the head of the queue, so it RUNS.

        Reporting a refusal would re-owe the briefs, and the turn-end retry would
        then deliver the same news a second time with a fresh board and another
        charged turn.
        """
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        with (
            patch(
                "kiro_crew.dashboard.chat_delivery.steer_into_running_turn",
                AsyncMock(return_value="requeued"),
            ),
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch.object(
                orch, "_work_ledger_wake_text", AsyncMock(return_value=_wake("WAKE TEXT"))
            ),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True

    @pytest.mark.asyncio
    async def test_a_steer_the_backend_did_not_take_leaves_the_wake_owed(self):
        """Then the loop declines the fire exactly as it did, and the retry delivers."""
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        with (
            patch(
                "kiro_crew.dashboard.chat_delivery.steer_into_running_turn",
                AsyncMock(return_value="unavailable"),
            ),
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch.object(
                orch, "_work_ledger_wake_text", AsyncMock(return_value=_wake("WAKE TEXT"))
            ),
        ):
            assert await orch._fire_dashboard_nudge(loop) is False

    @pytest.mark.asyncio
    async def test_a_plain_prompt_loop_on_a_busy_slot_is_still_skipped(self):
        """Queueing a multi-kilobyte instruction payload behind a turn is the thing
        the running-slot refusal exists to prevent, and that is unchanged."""
        orch = _orchestrator()
        loop = NudgeLoop(id="loop-plain", slot_key=CONDUCTOR, message="check the PR", idle_secs=300)
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        steer = AsyncMock(return_value="steered")
        with patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer):
            assert await orch._fire_dashboard_nudge(loop) is False
        steer.assert_not_awaited()

    def test_a_structured_monitor_wake_is_not_reshaped(self):
        """It composes its own envelope through its controller and owns that text."""
        orch = _orchestrator()
        loop = _ledger_loop()
        assert orch._work_ledger_wake_loop(loop, None) is True
        assert orch._work_ledger_wake_loop(loop, "an envelope the controller built") is False

    @pytest.mark.asyncio
    async def test_a_gated_watch_on_another_subject_is_not_steered(self):
        """Its text comes from that subject's probe and is not this path's to reshape."""
        orch = _orchestrator()
        loop = _ledger_loop()
        assert loop.monitor is not None
        loop.monitor.kind = "gh-pr"
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        steer = AsyncMock(return_value="steered")
        with patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer):
            assert await orch._fire_dashboard_nudge(loop) is False
        steer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_crew_slot_that_did_not_arm_the_loop_refuses_the_steer(self):
        """The steer must not become the way around the crew/member boundary."""
        orch = _orchestrator()
        loop = _ledger_loop()
        loop.self_armed = False
        slot = _slot(running=True, mode="crew")
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        steer = AsyncMock(return_value="steered")
        with (
            patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer),
            patch.object(orch, "_audit_fire_refused", AsyncMock()) as audited,
        ):
            assert await orch._fire_dashboard_nudge(loop) is False
        steer.assert_not_awaited()
        audited.assert_awaited()

    @pytest.mark.asyncio
    async def test_nothing_owed_and_no_board_means_no_steer(self):
        """An empty wake must not interrupt a turn to say nothing."""
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        steer = AsyncMock(return_value="steered")
        with (
            patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer),
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value=_wake(""))),
        ):
            assert await orch._fire_dashboard_nudge(loop) is False
        steer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_helper_refuses_an_empty_text_and_a_closing_slot(self):
        """Its own floor, so a future caller cannot steer nothing into a dying slot."""
        orch = _orchestrator()
        loop = _ledger_loop()
        steer = AsyncMock(return_value="steered")
        with patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer):
            assert (
                await orch._steer_work_ledger_wake(loop, _slot(running=True), _wake(""))
            ) is False
            closing = _slot(running=True)
            closing.is_closing = True
            assert await orch._steer_work_ledger_wake(loop, closing, _wake("WAKE")) is False
        steer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_containment_it_cannot_read_is_not_one_to_steer_past(self):
        orch = _orchestrator()
        loop = _ledger_loop()
        steer = AsyncMock(return_value="steered")
        with (
            patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer),
            patch(
                "kiro_crew.dashboard.session_control.containment_meta",
                side_effect=RuntimeError("no probe"),
            ),
        ):
            assert (
                await orch._steer_work_ledger_wake(loop, _slot(running=True), _wake("WAKE"))
            ) is False
        steer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_steer_that_raises_leaves_the_wake_owed_rather_than_failing_the_fire(self):
        orch = _orchestrator()
        loop = _ledger_loop()
        with (
            patch(
                "kiro_crew.dashboard.chat_delivery.steer_into_running_turn",
                AsyncMock(side_effect=RuntimeError("rpc died")),
            ),
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
        ):
            assert (
                await orch._steer_work_ledger_wake(loop, _slot(running=True), _wake("WAKE"))
            ) is False


# ── the wake turn carries the board ───────────────────────────────────────


class TestTheNudgeTurnCarriesTheWake:
    @pytest.mark.asyncio
    async def test_an_idle_conductors_turn_is_prefixed_with_the_wake(self):
        """The bar: the briefs plus the compact board, in that order."""
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        spawn = _fake_spawn()
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
            patch.object(
                orch,
                "_work_ledger_wake_text",
                AsyncMock(return_value=_wake("[work-ledger wake] item=it_1 status=done\n\nBOARD")),
            ),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        appended = [call for call in slot.append.call_args_list if call.args[0] == "nudge"]
        assert appended, "the nudge row was never written"
        body = appended[0].args[1]
        assert body.index("item=it_1") < body.index(
            "patrol the work ledger"
        ), "the news has to come before the standing patrol instruction"
        assert "BOARD" in body

    @pytest.mark.asyncio
    async def test_a_cycle_with_nothing_owed_delivers_the_body_it_always_did(self):
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        spawn = _fake_spawn()
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value=_wake(""))),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        appended = [call for call in slot.append.call_args_list if call.args[0] == "nudge"]
        body = appended[0].args[1]
        assert body.startswith("[auto-nudge cycle")
        assert "work-ledger wake" not in body

    @pytest.mark.asyncio
    async def test_a_cycle_with_no_owed_wake_reads_no_board_at_all(self):
        """A floor tick or a fallback observed no news, so there is nothing for a
        board to be the state FOR, and nothing for the steer to inject.

        A quiet-streak floor tick on a busy slot must stay declined: steering one
        would interrupt the turn the user is driving to hand it a board nobody
        asked for, and charge a cycle for doing so.
        """
        _seed_board()
        orch = _orchestrator()
        orch.autonudge_svc.pending_wake_briefs = MagicMock(return_value="")
        with patch.object(wl_routes, "compact_wake_snapshot", AsyncMock()) as board:
            assert (await orch._work_ledger_wake_text(_ledger_loop())).text == ""
        board.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_floor_tick_on_a_busy_slot_is_still_declined(self):
        """The same rule observed through the fire path, which is what regressed."""
        orch = _orchestrator()
        orch.autonudge_svc.pending_wake_briefs = MagicMock(return_value="")
        loop = _ledger_loop()
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        steer = AsyncMock(return_value="steered")
        with patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer):
            assert await orch._fire_dashboard_nudge(loop) is False
        steer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_gateway_with_no_dashboard_yet_builds_no_wake_text(self):
        """``_init_autonudge`` can run before ``_init_dashboard``, and --no-dashboard
        skips it entirely, so the absence has to answer rather than raise."""
        orch = _orchestrator()
        orch.dashboard_state = None
        assert (await orch._work_ledger_wake_text(_ledger_loop())).text == ""

    @pytest.mark.asyncio
    async def test_the_text_joins_the_owed_briefs_to_a_board_read_at_delivery(self):
        """End to end through the real composer: the claim's text plus a live read."""
        item_id = _seed_board()
        orch = _orchestrator()
        loop = _ledger_loop()
        orch.autonudge_svc.pending_wake_briefs = MagicMock(
            return_value=ledger_wake.wake_brief(item_id=item_id, status="done")
        )
        with _admitted():
            text = (await orch._work_ledger_wake_text(loop)).text
        assert f"item={item_id} status=done" in text
        assert ledger_wake.SNAPSHOT_HEADER in text
        assert "opened the PR" in text, "the board must carry the report that woke the conductor"
