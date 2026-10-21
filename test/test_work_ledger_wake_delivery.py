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
  and threw away, so the conductor was not told which item had moved.

The fix is one seam -- the wake's text becomes a value the claim carries from the gate
to delivery -- and these cases pin both ends of it: the text reaches the turn, and a
wake that lands mid-turn is steered into the running turn rather than held, with the
turn-end retry behind it when a steer is not available.

WHAT THE TEXT IS: the kernel's structural briefs plus ONE line pointing the conductor
at ``work_ledger_read``. No board. An inline board costs a payload proportional to the
whole goal for news that means one item moved, and it is ledger prose, which may enter
only a turn whose cross-surface publication is fenced for that turn's whole life -- a
steer can be requeued, which drops that fence, so prose cannot ride one and the two
delivery routes would carry different text. The pointer discloses nothing, so both
routes carry one string.
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
    """``ledger_wake.wake_turn_text``: the news first, then where to read it."""

    def test_the_briefs_come_before_the_pointer(self):
        text = ledger_wake.wake_turn_text("[work-ledger wake] item=it_1 status=done")
        assert text.index("item=it_1") < text.index(ledger_wake.WAKE_READ_POINTER)

    def test_the_pointer_names_the_read_and_both_of_its_shapes(self):
        """A conductor told only "read the ledger" has two tools and one wake."""
        text = ledger_wake.wake_turn_text("[work-ledger wake] item=it_1 status=done")
        assert "work_ledger_read(item_id=<id>)" in text
        assert "work_ledger_read(compact=true)" in text

    def test_no_board_rides_the_wake(self):
        """THE POINT: a wake is a notification, not a copy of the board.

        Pinned on the shape rather than on a byte count, because a board is what a
        regression puts back: the compact document's own keys. On a twenty-item goal
        an inline board is tens of kilobytes of mostly-closed rows per wake, and a
        wake only ever means that ONE item moved.
        """
        text = ledger_wake.wake_turn_text("[work-ledger wake] item=it_1 status=done")
        assert '"items"' not in text and '"compact"' not in text
        assert "accept_batch" not in text
        assert len(text.splitlines()) == 2, f"a wake is two lines, not a document: {text!r}"

    def test_briefs_the_kernel_did_not_hand_over_are_no_text_at_all(self):
        """So the caller delivers the plain nudge rather than a pointer to nothing."""
        assert ledger_wake.wake_turn_text("") == ""
        assert ledger_wake.wake_turn_text("   \n ") == ""

    def test_an_over_long_brief_list_loses_its_tail_and_not_the_pointer(self):
        """A kernel or probe defect must not push the pointer out of the turn."""
        text = ledger_wake.wake_turn_text("x" * (ledger_wake.MAX_WAKE_BRIEF_CHARS * 3))
        assert "(brief list truncated)" in text
        assert ledger_wake.WAKE_READ_POINTER in text, (
            "the pointer must survive an over-long brief list, or the conductor is "
            "woken with no instruction to read the ledger"
        )
        assert len(text) < ledger_wake.MAX_WAKE_BRIEF_CHARS * 2


# ── the board is the conductor's own read, never the wake's ───────────────


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


def _no_prose_outside_the_store(item_id: str, text: str) -> None:
    """Assert the worker's own summary is in NEITHER the wake nor the watch state.

    The boundary ``wake_brief`` exists for, now covering the delivered text too.
    ``irq`` persists an observation's brief beside the watch state, which is not
    under the store's identity-gated read path -- so the briefs carry item ids and
    statuses and nothing else. The DELIVERED text is those same briefs plus a read
    pointer, so the same assertion holds at both ends: the only way to the worker's
    prose is the conductor's own ``work_ledger_read``.
    """
    assert f"item={item_id}" in text, "the control: the wake must name the item"
    assert "opened the PR" not in text, "worker prose reached the delivered wake"
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
                patch.object(
                    service,
                    "_arm_from_deadline",
                    lambda _loop: armed.append("deadline"),
                ),
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
                patch.object(
                    service,
                    "_arm_from_deadline",
                    lambda _loop: armed.append("deadline"),
                ),
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
                patch.object(
                    service,
                    "_arm_from_deadline",
                    lambda _loop: armed.append("deadline"),
                ),
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
                orch, "_work_ledger_wake_text", AsyncMock(return_value="WAKE TEXT")
            ) as text,
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        assert (
            text.await_count == 1
        ), "one text, built once, whichever of the two routes below delivers it"
        assert steer.await_args.args[2] == "WAKE TEXT"
        assert steer.await_args.kwargs["user_origin"] is False, (
            "nobody typed this into the session's own surface, so it must not inherit "
            "the composer's exemption from the drain's linked drop"
        )
        assert steer.await_args.kwargs["nudge_meta"] == {
            "cycle": loop.cycle_count + 1,
            "loop_id": loop.id,
        }, "the wake is automation, so its row is the loop's nudge chip (#22747)"

    @pytest.mark.asyncio
    async def test_the_steer_carries_the_whole_wake_and_fences_nothing(self):
        """Both routes carry the SAME text, and the pointer is why.

        Ledger prose may enter only a turn whose cross-surface publication is fenced
        for that turn's whole life, and a steer's text can be requeued -- the
        teardown clears the running turn's fence and the drain starts a successor
        nothing re-fences. So a board cannot ride a steer. A wake that is briefs plus
        a ``work_ledger_read`` pointer discloses nothing, so there is no half to hold
        back and no fence to record.
        """
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        steer = AsyncMock(return_value="steered")
        wake = ledger_wake.wake_turn_text("[work-ledger wake] item=it_1 status=done")
        with (
            patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer),
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch("kiro_crew.dashboard.session_control.record_audience_admission") as fenced,
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value=wake)),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        injected = steer.await_args.args[2]
        assert injected == wake
        assert ledger_wake.WAKE_READ_POINTER in injected, (
            "the steered conductor must be told to read the ledger, or it acts on a "
            "one-line notification as if it were the board"
        )
        fenced.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_fired_wake_turn_records_no_audience_fence(self):
        """THE DELETION, pinned: there is nothing in a wake for a fence to withhold.

        A fence is the publisher's fail-closed withholding of the cross-surface
        legs, and it is owed by a turn carrying worker-authored ledger prose. This
        turn carries briefs and a read pointer, and the one read of the board is the
        conductor's own ``work_ledger_read``, which judges containment and records
        its own admission inside this very turn. So a fence here would be a promise
        about text that is not here.
        """
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        spawn = _running_spawn()
        wake = ledger_wake.wake_turn_text("[work-ledger wake] item=it_1 status=done")
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()) as ran,
            patch("kiro_crew.dashboard.session_control.record_audience_admission") as fenced,
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value=wake)),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
            # Run the turn coro the spawn scheduled through to its _run_chat, which
            # is the statement a fence would have to be recorded before.
            await asyncio.gather(*spawn.tasks, return_exceptions=True)
        fenced.assert_not_called()
        assert ran.await_count == 1, "the turn must still run; only the fence is gone"
        assert ledger_wake.WAKE_READ_POINTER in ran.await_args.args[2]

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
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value="WAKE TEXT")),
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
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value="WAKE TEXT")),
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
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value="")),
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
            assert (await orch._steer_work_ledger_wake(loop, _slot(running=True), "")) is False
            closing = _slot(running=True)
            closing.is_closing = True
            assert await orch._steer_work_ledger_wake(loop, closing, "WAKE") is False
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
            assert (await orch._steer_work_ledger_wake(loop, _slot(running=True), "WAKE")) is False
        steer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_steer_that_raises_leaves_the_wake_owed_rather_than_failing_the_fire(
        self,
    ):
        orch = _orchestrator()
        loop = _ledger_loop()
        with (
            patch(
                "kiro_crew.dashboard.chat_delivery.steer_into_running_turn",
                AsyncMock(side_effect=RuntimeError("rpc died")),
            ),
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
        ):
            assert (await orch._steer_work_ledger_wake(loop, _slot(running=True), "WAKE")) is False


# ── the wake turn carries the news and the pointer ──────────────────


class TestTheNudgeTurnCarriesTheWake:
    @pytest.mark.asyncio
    async def test_an_idle_conductors_turn_is_prefixed_with_the_wake(self):
        """The bar: the news, then the read pointer, then the patrol instruction."""
        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        spawn = _fake_spawn()
        wake = ledger_wake.wake_turn_text("[work-ledger wake] item=it_1 status=done")
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value=wake)),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        appended = [call for call in slot.append.call_args_list if call.args[0] == "nudge"]
        assert appended, "the nudge row was never written"
        body = appended[0].args[1]
        assert body.index("item=it_1") < body.index(
            ledger_wake.WAKE_READ_POINTER
        ), "the news has to come before the instruction that acts on it"
        assert body.index(ledger_wake.WAKE_READ_POINTER) < body.index(
            "patrol the work ledger"
        ), "both have to come before the standing patrol instruction"

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
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value="")),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        appended = [call for call in slot.append.call_args_list if call.args[0] == "nudge"]
        body = appended[0].args[1]
        assert body.startswith("[auto-nudge cycle")
        assert "work-ledger wake" not in body

    @pytest.mark.asyncio
    async def test_a_cycle_with_no_owed_wake_delivers_no_wake_text(self):
        """A floor tick or a fallback observed no news, so there is no item to point
        AT, and nothing for the steer to inject.

        A quiet-streak floor tick on a busy slot must stay declined: steering one
        would interrupt the turn the user is driving to say nothing, and charge a
        cycle for doing so.
        """
        orch = _orchestrator()
        orch.autonudge_svc.pending_wake_briefs = MagicMock(return_value="")
        assert await orch._work_ledger_wake_text(_ledger_loop()) == ""

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
    async def test_a_gateway_with_no_autonudge_service_builds_no_wake_text(self):
        """``_init_autonudge`` can be skipped, so the absence has to answer rather
        than raise: the service is the only holder of the owed claim, which is now
        this method's only input."""
        orch = _orchestrator()
        orch.autonudge_svc = None
        assert await orch._work_ledger_wake_text(_ledger_loop()) == ""

    @pytest.mark.asyncio
    async def test_the_text_is_the_owed_briefs_and_the_pointer_and_no_ledger_prose(
        self,
    ):
        """End to end through the real composer, over a REAL board with a real report.

        The bar: the conductor is told which item moved and to go read it, and the
        worker's own summary -- which an inline board carries in full on every wake
        -- is not in the turn at all. ``_no_prose_outside_the_store`` also checks the
        watch-state files, so the one text and the one persisted brief are held to
        the same boundary.
        """
        item_id = _seed_board()
        orch = _orchestrator()
        loop = _ledger_loop()
        orch.autonudge_svc.pending_wake_briefs = MagicMock(
            return_value=ledger_wake.wake_brief(item_id=item_id, status="done")
        )
        from kiro_crew.probes import work_ledger as probe_mod

        verdict = await asyncio.to_thread(
            irq.poll,
            "loop-pointer",
            json.dumps({"conductor": CONDUCTOR}),
            probe_mod.WorkLedgerProbe(),
        )
        assert verdict.outcome is irq.Outcome.WAKE, "the kernel wrote no state to check"
        text = await orch._work_ledger_wake_text(loop)
        assert f"item={item_id} status=done" in text
        assert ledger_wake.WAKE_READ_POINTER in text
        _no_prose_outside_the_store(item_id, text)

    @pytest.mark.asyncio
    async def test_the_text_does_not_grow_with_the_size_of_the_ledger(self):
        """THE DEFECT, measured rather than described.

        An inline board carries a row per item, bounded only at tens of kilobytes,
        so one item moving costs a wake proportional to the WHOLE goal -- on a
        twenty-item ledger, mostly-closed rows re-sent on every wake. The bar is
        therefore not "smaller" but "constant": the same single report, delivered
        over a one-item ledger and then over a twenty-item one, has to produce the
        SAME BYTES.

        Measured against the composer the fire path calls, not against
        ``wake_turn_text`` alone, because the read that grows the text belongs to the
        delivery site and that is where a regression puts it.
        """
        item_id = _seed_board()
        orch = _orchestrator()
        loop = _ledger_loop()
        orch.autonudge_svc.pending_wake_briefs = MagicMock(
            return_value=ledger_wake.wake_brief(item_id=item_id, status="done")
        )
        small = await orch._work_ledger_wake_text(loop)
        for n in range(19):
            work_ledger.apply_conductor_action(
                CONDUCTOR,
                "create",
                title=f"filler {n} with a title long enough to show up in a board row",
                acceptance={"kind": "human_approval"},
            )
        assert len(work_ledger.list_work_items(CONDUCTOR)) == 20, "the board did not grow"
        large = await orch._work_ledger_wake_text(loop)
        assert large == small, (
            "the wake grew with the ledger: one item moved, so one brief and one "
            "pointer are the whole payload however many items the board holds"
        )
        assert len(large) < 400, f"a wake is two short lines, not a document: {large!r}"
