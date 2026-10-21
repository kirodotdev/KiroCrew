"""The monitor-loop counters, driven through their production call sites.

Four counters, each with its own way of going wrong, and each test here holds
the property that keeps it honest rather than merely proving an emit happens:

* ``loops.started`` must count ARMS. A gateway restart restores every persisted
  loop, so the test that matters is that a restore adds nothing -- otherwise the
  number reports how often the host reboots.
* ``ticks`` must count every tick that reached a verdict about a turn, exactly
  once, and keep ``quiet`` apart from ``delivered``: that ratio is the only way
  to tell a loop doing work from a loop idling.
* ``loops.stopped`` must count a stop ONCE however it was reached, and must map
  a stored reason to a closed value -- a reason is composed at some call sites
  and model-authored at others.
* ``monitor.decisions`` must carry every member of the controller's decision
  enum and no monitored kind the registry does not know.

Nothing here asserts on a loop id, a slot key or a target: the counters do not
carry them, and the tests that pass one in assert it never appears.
"""

from __future__ import annotations

import json
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew import autonudge_stop_log
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.autonudge_service import model as loop_model
from kiro_crew.metrics import monitor_loops as ml
from kiro_crew.metrics.recorder import MetricsRecorder
from kiro_crew.monitoring import models as monitor_models
from kiro_crew.monitoring.decision import decide_monitor
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorProbeResult,
    MonitorState,
    ProviderErrorKind,
)
from kiro_crew.monitoring.pull_request import PullRequestFacts, classify_pull_request_facts

_HOUR = 3600
_DAY = 86400


class _CapturingRecorder:
    """Stands in for the OTEL-backed recorder and keeps every add."""

    def __init__(self) -> None:
        self.counters: list[dict] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "value": value, "attrs": dict(attrs or {})})

    def histogram(self, name, value, **kwargs) -> None:  # pragma: no cover - unused
        pass


@pytest.fixture
def rec():
    r = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


@pytest.fixture(autouse=True)
def _autonudge_enabled(_floor_monkeypatch):
    """The service refuses to arm a timer without this flag.

    Requests ``_floor_monkeypatch`` rather than the shared ``monkeypatch``: an
    autouse pin on the test-owned stack is lifted by any test that calls
    ``monkeypatch.undo()``, so the floor fixture is what gives it an independent
    lifetime.
    """
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


def _named(rec, name):
    return [c for c in rec.counters if c["name"] == name]


def _state(**changes: object) -> MonitorState:
    values: dict[str, object] = {
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
    }
    values.update(changes)
    return MonitorState(**values)


def _row(**changes: object) -> dict:
    row: dict = {
        "id": "abc123",
        "slot_key": "chat-1-123",
        "active": True,
        "cycle_count": 1,
        "max_cycles": 0,
        "created_ts": 1_000,
        "max_runtime_secs": 0,
    }
    row.update(changes)
    return row


def _stop_record(*, created_ts: int = 1_000, now: float = 1_000.0, **row_changes) -> dict:
    """One real stop record, produced by the production differ.

    Drives ``stop_records`` rather than hand-writing the dict, so the record the
    counter reads is the record the store's commit point would hand it -- which
    is where ``ran_secs`` and the stored reason actually come from.
    """
    before = _row(created_ts=created_ts, **row_changes)
    after = dict(before, active=False)
    records = autonudge_stop_log.stop_records(
        autonudge_stop_log.active_summaries([before]),
        [after],
        {},
        now=now,
    )
    assert len(records) == 1
    return records[0]


# ---------------------------------------------------------------------------
# loops.started
# ---------------------------------------------------------------------------


class TestLoopsStarted:
    @pytest.mark.asyncio
    async def test_one_arm_counts_once(self, rec, tmp_path):
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)

        calls = _named(rec, ml.LOOPS_STARTED)
        assert len(calls) == 1
        assert calls[0]["value"] == 1
        assert calls[0]["attrs"] == {"kind": ml.KIND_LOOP}

    @pytest.mark.asyncio
    async def test_a_restore_after_restart_does_not_re_count(self, rec, tmp_path):
        """The population is arms, not arms plus however often the host reboots."""
        first = AutoNudgeService(base_dir=tmp_path)
        await first.start()
        await first.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        await first.add(slot_key="chat-2-456", message="go", idle_secs=3600)
        assert len(_named(rec, ml.LOOPS_STARTED)) == 2

        rec.counters.clear()
        restored = AutoNudgeService(base_dir=tmp_path)
        await restored.start()

        assert len(restored._loops) == 2, "the restore must have loaded them"
        assert _named(rec, ml.LOOPS_STARTED) == []

    @pytest.mark.asyncio
    async def test_a_refused_arm_counts_nothing(self, rec, tmp_path):
        """Counted after the write commits, so a refusal leaves no increment."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        with pytest.raises(Exception):
            await svc.add(
                slot_key="chat-1-123",
                message="go",
                admission_check=lambda: False,
            )
        assert _named(rec, ml.LOOPS_STARTED) == []

    @pytest.mark.asyncio
    async def test_a_structured_monitor_arm_counts_too(self, rec, tmp_path):
        """Its own mutation, not the prompt one, and its own snapshot writer.

        Its stop IS counted, so an uncounted arm would let
        ``loops.stopped{kind=monitor}`` outnumber
        ``loops.started{kind=monitor}``.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )

        calls = _named(rec, ml.LOOPS_STARTED)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"kind": ml.KIND_MONITOR}

    @pytest.mark.asyncio
    async def test_a_structured_arm_and_its_stop_balance(self, rec, tmp_path):
        """The property the pairing exists for, asserted on both counters."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        await svc.stop_monitor(loop.id, user_reason="done with it")

        started = _named(rec, ml.LOOPS_STARTED)
        stopped = _named(rec, ml.LOOPS_STOPPED)
        assert len(started) == 1
        assert len(stopped) == 1
        assert started[0]["attrs"]["kind"] == stopped[0]["attrs"]["kind"] == ml.KIND_MONITOR
        assert stopped[0]["attrs"]["reason"] == "user_stop"
        assert "done with it" not in "".join(stopped[0]["attrs"].values())

    @pytest.mark.asyncio
    async def test_a_pause_resume_and_cap_stop_balance(self, rec, tmp_path):
        """A resume is an inactive-to-active transition and counts as a start.

        Without it, the pause and the later cycle-cap stop record two stops
        against one start, and the reconcilers that revive inactive loops on
        every pass widen that gap for as long as a campaign runs.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600, max_cycles=1)
        await svc.update(loop.id, active=False)
        await svc.update(loop.id, active=True)
        assert loop.active, "the resume must have revived the loop"
        loop.cycle_count = 1
        svc._on_fire = _unreachable_fire
        await svc._timer(loop, delay=0)
        assert not loop.active

        started = _named(rec, ml.LOOPS_STARTED)
        stopped = _named(rec, ml.LOOPS_STOPPED)
        assert len(started) == 2
        assert len(stopped) == 2
        assert [c["attrs"]["reason"] for c in stopped] == ["manual", "cycle_cap"]
        assert {c["attrs"]["kind"] for c in started + stopped} == {ml.KIND_LOOP}

    @pytest.mark.asyncio
    async def test_an_update_on_an_active_loop_is_not_a_start(self, rec, tmp_path):
        """A settings save sends ``active=True`` on a live loop; nothing revived."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)

        await svc.update(loop.id, active=True, message="go on")

        assert len(_named(rec, ml.LOOPS_STARTED)) == 1
        assert _named(rec, ml.LOOPS_STOPPED) == []

    @pytest.mark.asyncio
    async def test_a_failed_close_restore_and_its_later_stop_balance(self, rec, tmp_path):
        """The close retirement commits a stop, so its undo is a start."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        await svc.retire_monitor_for_session_close(loop.id, now=110.0)
        assert not loop.active
        restored = await svc.restore_monitor_after_failed_session_close(loop.id, now=120.0)
        assert restored is loop and loop.active
        await svc.stop_monitor(loop.id, now=130.0)

        started = _named(rec, ml.LOOPS_STARTED)
        stopped = _named(rec, ml.LOOPS_STOPPED)
        assert len(started) == 2
        assert len(stopped) == 2
        assert [c["attrs"]["reason"] for c in stopped] == ["session_close", "user_stop"]
        assert {c["attrs"]["kind"] for c in started + stopped} == {ml.KIND_MONITOR}

    @pytest.mark.asyncio
    async def test_a_replacement_rollback_of_an_active_row_balances(self, rec, tmp_path):
        """The replacement commit removes the prior id, which counts a stop."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        prior = await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        assert prior.monitor is not None
        await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            expected_existing_monitor_id=prior.id,
            expected_existing_config_generation=prior.monitor.config_generation,
            loop_id="replaced",
            defer_replaced_trust_revocation=True,
        )
        assert await svc.rollback_monitor_replacement("replaced") is True
        assert svc._loops[prior.id].active
        await svc.stop_monitor(prior.id, now=130.0)

        assert len(_named(rec, ml.LOOPS_STARTED)) == 3
        assert len(_named(rec, ml.LOOPS_STOPPED)) == 3

    @pytest.mark.asyncio
    async def test_a_declined_revival_of_a_finished_row_is_not_a_start(self, rec, tmp_path):
        """A finished row refuses ``active=True`` and stays inactive: no start.

        The reconcilers send that call on every pass, so a start counted here
        would grow without a matching stop for as long as a campaign runs.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        await svc.update(loop.id, active=False, stopped_reason=loop_model.MONITOR_TERMINAL_REASON)
        rec.counters.clear()

        await svc.update(loop.id, active=True)

        assert not loop.active, "the finished row must have declined the revival"
        assert _named(rec, ml.LOOPS_STARTED) == []
        assert _named(rec, ml.LOOPS_STOPPED) == []

    @pytest.mark.asyncio
    async def test_a_boot_resume_of_a_torn_row_and_its_stop_balance(self, rec, tmp_path):
        """The load repair resumes the row after the stop baseline was taken.

        The row is inactive on disk, so the baseline does not hold it, and the
        commit that writes it active counts its start. Without that, its later
        stop is counted against no start. A second restart loads the row active
        and adds nothing.
        """
        _write_store(tmp_path, _torn_row())
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        try:
            loop = svc._loops[_TORN_ID]
            assert loop.active, "the load must have resumed the torn row"
            assert len(_named(rec, ml.LOOPS_STARTED)) == 1

            await svc.update(loop.id, active=False)
        finally:
            svc.stop()

        started = _named(rec, ml.LOOPS_STARTED)
        stopped = _named(rec, ml.LOOPS_STOPPED)
        assert len(started) == 1
        assert len(stopped) == 1
        assert started[0]["attrs"] == {"kind": ml.KIND_LOOP}
        assert stopped[0]["attrs"]["reason"] == "manual"

    @pytest.mark.asyncio
    async def test_a_second_restart_does_not_re_count_a_boot_resume(self, rec, tmp_path):
        _write_store(tmp_path, _torn_row())
        first = AutoNudgeService(base_dir=tmp_path)
        await first.start()
        first.stop()
        assert len(_named(rec, ml.LOOPS_STARTED)) == 1

        rec.counters.clear()
        second = AutoNudgeService(base_dir=tmp_path)
        await second.start()
        second.stop()

        assert second._loops[_TORN_ID].active
        assert _named(rec, ml.LOOPS_STARTED) == []

    @pytest.mark.asyncio
    async def test_an_activation_path_that_never_names_the_counter_is_counted(self, rec, tmp_path):
        """A future mutation that activates a loop must not need to know this counter.

        The activation here is a bare field write and a plain store save -- no
        mutation, no emit -- standing in for a path nobody has written yet. Starts
        are counted at the commit point from the same diff as stops, so it is
        still counted, and the pause, the bare resume and the final stop balance.
        A start emitted per mutation would miss it and leave two stops against
        one start.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        try:
            loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
            await svc.update(loop.id, active=False)

            loop.active = True
            loop.stopped_reason = ""
            svc._save()
            assert len(_named(rec, ml.LOOPS_STARTED)) == 2

            svc._save()
            assert len(_named(rec, ml.LOOPS_STARTED)) == 2, "a re-save is not a start"

            await svc.update(loop.id, active=False)
        finally:
            svc.stop()

        started = _named(rec, ml.LOOPS_STARTED)
        stopped = _named(rec, ml.LOOPS_STOPPED)
        assert len(started) == len(stopped) == 2
        assert {c["attrs"]["kind"] for c in started + stopped} == {ml.KIND_LOOP}

    @pytest.mark.asyncio
    async def test_an_unparsed_active_row_is_not_a_start(self, rec, tmp_path):
        """A row the load could not parse is rewritten on every commit, never served.

        It is outside the loaded baseline, so a commit-point diff that did not
        skip it would count one start per boot that no stop can ever match.
        """
        unparsed = _row(id="unparsed1", idle_secs="not-a-number", created_ts="later")
        _write_store(tmp_path, unparsed)
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        try:
            assert "unparsed1" not in svc._loops
            assert svc._store.unparsed_rows, "the row must have been held as unparsed"
            await svc.add(slot_key="chat-2-456", message="go", idle_secs=3600)
        finally:
            svc.stop()

        on_disk = json.loads((tmp_path / "autonudge.json").read_text())["loops"]
        assert any(r.get("id") == "unparsed1" and r.get("active") for r in on_disk)
        assert len(_named(rec, ml.LOOPS_STARTED)) == 1
        assert _named(rec, ml.LOOPS_STOPPED) == []

    @pytest.mark.asyncio
    async def test_no_loop_id_or_slot_key_reaches_the_attributes(self, rec, tmp_path):
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-secret-slot", message="go", idle_secs=3600)

        attrs = _named(rec, ml.LOOPS_STARTED)[-1]["attrs"]
        assert set(attrs) == {"kind"}
        assert loop.id not in attrs.values()
        assert "chat-secret-slot" not in attrs.values()


# ---------------------------------------------------------------------------
# ticks
# ---------------------------------------------------------------------------


class TestTicks:
    @pytest.mark.asyncio
    async def test_a_quiet_gated_tick_is_counted_quiet(self, rec, tmp_path):
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        loop.monitor = _state()
        loop.gate = True
        svc._on_fire = _unreachable_fire
        rec.counters.clear()

        with patch.object(AutoNudgeService, "_monitor_tick_is_quiet", _coroutine_returning(True)):
            await svc._timer(loop, delay=0)

        calls = _named(rec, ml.TICKS)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"kind": ml.KIND_MONITOR, "outcome": ml.OUTCOME_QUIET}

    @pytest.mark.asyncio
    async def test_a_delivered_tick_is_counted_delivered(self, rec, tmp_path):
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        svc._on_fire = _coroutine_returning(True)
        rec.counters.clear()

        await svc._timer(loop, delay=0)

        calls = _named(rec, ml.TICKS)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"kind": ml.KIND_LOOP, "outcome": ml.OUTCOME_DELIVERED}

    @pytest.mark.asyncio
    async def test_a_refused_delivery_is_not_a_failure(self, rec, tmp_path):
        """A slot mid-turn declines on purpose; that is not a defect."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        svc._on_fire = _coroutine_returning(False)
        rec.counters.clear()

        await svc._timer(loop, delay=0)

        calls = _named(rec, ml.TICKS)
        assert len(calls) == 1
        assert calls[0]["attrs"]["outcome"] == ml.OUTCOME_REFUSED

    @pytest.mark.asyncio
    async def test_a_callback_that_removes_the_loop_is_a_stop_and_not_a_refusal(
        self, rec, tmp_path
    ):
        """The dashboard fire removes a loop whose session is gone, then returns False.

        The store counts that removal on `loops.stopped`; counting it as a
        `refused` tick too would report one event twice.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)

        async def _remove_then_refuse(fired):
            await svc.remove(fired.id, stop_reason="session_unreachable")
            return False

        svc._on_fire = _remove_then_refuse
        rec.counters.clear()

        await svc._timer(loop, delay=0)

        assert _named(rec, ml.TICKS) == []
        assert len(_named(rec, ml.LOOPS_STOPPED)) == 1
        assert loop.id not in svc._loops

    @pytest.mark.asyncio
    async def test_a_raising_fire_callback_is_counted_failed(self, rec, tmp_path):
        """Kept apart from ``refused``: otherwise a wedged callback is invisible."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        svc._on_fire = _coroutine_raising()
        rec.counters.clear()

        await svc._timer(loop, delay=0)

        calls = _named(rec, ml.TICKS)
        assert len(calls) == 1
        assert calls[0]["attrs"]["outcome"] == ml.OUTCOME_FAILED

    @pytest.mark.asyncio
    async def test_a_tick_that_stopped_the_loop_is_not_also_a_tick(self, rec, tmp_path):
        """A spent cycle cap decided about the LOOP, not about a turn.

        ``loops.stopped`` records it. Counting it here too would put events that
        were never candidates for a turn into the denominator the
        quiet-to-delivered ratio is read against.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600, max_cycles=1)
        loop.cycle_count = 1
        svc._on_fire = _unreachable_fire
        rec.counters.clear()

        await svc._timer(loop, delay=0)

        assert _named(rec, ml.TICKS) == []
        assert not loop.active

    @pytest.mark.asyncio
    async def test_a_terminal_quiet_verdict_is_a_stop_and_not_a_tick(self, rec, tmp_path):
        """The gate answers quiet for a merged subject having just retired it.

        That tick ended the loop, so `loops.stopped` owns it; counting it here
        as well would report one event twice.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        loop.monitor = _state()
        loop.gate = True
        svc._on_fire = _unreachable_fire
        rec.counters.clear()

        async def _terminal_quiet(_self, watched):
            watched.active = False
            watched.stopped_reason = "monitor_terminal"
            return True

        with patch.object(AutoNudgeService, "_monitor_tick_is_quiet", _terminal_quiet):
            await svc._timer(loop, delay=0)

        assert _named(rec, ml.TICKS) == []
        assert not loop.active

    @pytest.mark.asyncio
    async def test_a_structured_monitor_record_reaches_no_tick_site(self, rec, tmp_path):
        """Pins the documented scope of this counter.

        A structured record is driven by its typed controller, which decides
        nothing `ticks` can describe; `monitor.decisions` is that path's verdict.
        So within `ticks`, `kind=monitor` means a probe-gated PROMPT loop, and a
        per-loop rate must not be taken as ticks over started for that kind.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        ticked: list[str] = []

        async def _monitor_tick(watched):
            ticked.append(watched.id)

        svc._on_monitor_tick = _monitor_tick
        await svc.start()
        loop = await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        svc._on_fire = _unreachable_fire
        rec.counters.clear()

        await svc._timer(loop, delay=0)

        assert ticked == [loop.id], "the typed controller must have run"
        assert _named(rec, ml.TICKS) == []

    @pytest.mark.asyncio
    async def test_an_unknown_outcome_never_travels_as_itself(self, rec, tmp_path):
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        rec.counters.clear()

        ml.record_tick(loop, "something-new")

        assert _named(rec, ml.TICKS)[-1]["attrs"]["outcome"] == ml.OTHER


# ---------------------------------------------------------------------------
# loops.stopped
# ---------------------------------------------------------------------------


class TestLoopsStopped:
    @pytest.mark.asyncio
    async def test_a_stop_through_the_store_counts_once(self, rec, tmp_path):
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        rec.counters.clear()

        await svc.update(loop.id, active=False, stopped_reason="cycle_cap")

        calls = _named(rec, ml.LOOPS_STOPPED)
        assert len(calls) == 1
        assert calls[0]["attrs"]["kind"] == ml.KIND_LOOP
        assert calls[0]["attrs"]["reason"] == "cycle_cap"

    @pytest.mark.asyncio
    async def test_stopping_an_already_stopped_loop_counts_nothing_more(self, rec, tmp_path):
        """The differ only sees a loop cross from active to not-active once."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        await svc.update(loop.id, active=False, stopped_reason="cycle_cap")
        assert len(_named(rec, ml.LOOPS_STOPPED)) == 1

        await svc.update(loop.id, active=False, stopped_reason="cycle_cap")
        await svc.remove(loop.id)

        assert len(_named(rec, ml.LOOPS_STOPPED)) == 1

    @pytest.mark.asyncio
    async def test_a_restart_does_not_re_report_a_stop(self, rec, tmp_path):
        """A stopped row stays on disk for inspection; its stop is already counted."""
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
        await svc.update(loop.id, active=False, stopped_reason="cycle_cap")
        rec.counters.clear()

        restored = AutoNudgeService(base_dir=tmp_path)
        await restored.start()
        await restored.add(slot_key="chat-2-456", message="go", idle_secs=3600)

        assert _named(rec, ml.LOOPS_STOPPED) == []

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("cycle_cap", "cycle_cap"),
            ("runtime_budget", "runtime_budget"),
            ("approval_stalled", "approval_stall"),
            ("approval_stall", "approval_stall"),
            ("stop_sentinel", "stop_sentinel"),
            ("monitor_terminal", "monitor_terminal"),
            ("session_start_failures", "session_start_failures"),
            ("consecutive_failures", "consecutive_failures"),
            ("manual", "manual"),
            ("user_stop", "user_stop"),
            ("session_close", "session_close"),
            ("removed", "removed"),
            ("unspecified", "unspecified"),
        ],
    )
    def test_every_stored_reason_maps_to_its_closed_value(self, stored, expected):
        assert ml.stop_reason(stored) == expected
        assert expected in ml.STOP_REASON_VALUES

    def test_every_stop_reason_constant_is_covered(self):
        """The table is built from these, so none of them can go missing.

        A retyped table had already lost one member, and the cost is silent: the
        stop is reported as `other` while the table reads as complete.
        """
        constants = [
            loop_model.CYCLE_CAP_REASON,
            loop_model.RUNTIME_BUDGET_REASON,
            loop_model.STOP_SENTINEL_REASON,
            loop_model.MONITOR_TERMINAL_REASON,
            loop_model.AUTONUDGE_STOP_REASON,
            loop_model.APPROVAL_STALL_REASON,
            loop_model.SESSION_START_FAILURE_REASON,
            loop_model.CONSECUTIVE_FAILURE_REASON,
            loop_model.STRUCTURAL_TERMINAL_REASON,
            loop_model.MODEL_REFUSED_REASON,
            loop_model.SENTINEL_DROPPED_REASON,
            loop_model.MANUAL_STOP_REASON,
            autonudge_stop_log.REMOVED_REASON,
            autonudge_stop_log.UNSPECIFIED_REASON,
        ]
        constants += [
            getattr(monitor_models, name)
            for name in dir(monitor_models)
            if name.startswith("MONITOR_STOP_")
        ]
        unmapped = [reason for reason in constants if ml.stop_reason(reason) == ml.OTHER]
        assert unmapped == []

    def test_every_terminal_outcome_is_covered(self):
        """A monitor row with no stop reason falls back to its outcome."""
        unmapped = [
            outcome.value for outcome in MonitorOutcome if ml.stop_reason(outcome.value) == ml.OTHER
        ]
        assert unmapped == []

    @pytest.mark.parametrize(
        "stored",
        [
            "campaign_completed",
            "orphaned_worker",
            "the model decided to stop: /home/someone/notes.md",
            "",
            None,
            123,
        ],
    )
    def test_an_unknown_reason_becomes_other(self, stored):
        """Some reasons are composed at the call site and are unbounded."""
        assert ml.stop_reason(stored) == ml.OTHER

    def test_the_stop_detail_never_reaches_the_attributes(self, rec):
        record = _stop_record(stopped_reason="autonudge_stop")
        record["detail"] = "deleting /home/someone/.aws/credentials now"

        ml.record_loop_stopped(record)

        attrs = _named(rec, ml.LOOPS_STOPPED)[-1]["attrs"]
        assert set(attrs) == {"kind", "reason", "ran"}
        assert "credentials" not in "".join(attrs.values())

    @pytest.mark.parametrize(
        ("ran_secs", "expected"),
        [
            (0, ml.RAN_LT_1H),
            (_HOUR - 1, ml.RAN_LT_1H),
            (_HOUR, ml.RAN_1H_24H),
            (_HOUR + 1, ml.RAN_1H_24H),
            (_DAY - 1, ml.RAN_1H_24H),
            (_DAY, ml.RAN_GT_24H),
            (_DAY + 1, ml.RAN_GT_24H),
        ],
    )
    def test_each_ran_bucket_edge(self, rec, ran_secs, expected):
        """Driven through the real differ, so the derivation is the product's."""
        record = _stop_record(created_ts=1_000, now=1_000.0 + ran_secs)
        assert record["ran_secs"] == ran_secs

        ml.record_loop_stopped(record)

        assert _named(rec, ml.LOOPS_STOPPED)[-1]["attrs"]["ran"] == expected

    def test_a_row_with_no_creation_time_reports_an_unknown_runtime(self, rec):
        """Reported as unknown rather than folded into the shortest bucket."""
        record = _stop_record(created_ts=0)
        assert record["ran_secs"] is None

        ml.record_loop_stopped(record)

        assert _named(rec, ml.LOOPS_STOPPED)[-1]["attrs"]["ran"] == ml.OTHER

    def test_a_monitor_row_is_labelled_monitor(self, rec):
        record = _stop_record(monitor={"stopped_reason": "runtime_budget"})
        assert record["kind"] == ml.KIND_MONITOR

        ml.record_loop_stopped(record)

        attrs = _named(rec, ml.LOOPS_STOPPED)[-1]["attrs"]
        assert attrs["kind"] == ml.KIND_MONITOR
        assert attrs["reason"] == "runtime_budget"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "status", "expected"),
        [
            ("merged", MonitorObservationStatus.SUCCESS, "success"),
            ("closed", MonitorObservationStatus.BLOCKED, "blocked"),
        ],
    )
    async def test_a_pull_request_monitor_stop_reports_its_outcome(
        self, rec, tmp_path, state, status, expected
    ):
        """A merged or closed PR stores the provider's reason code as its stop reason.

        `pull_request_merged` / `pull_request_closed` are not in STOP_REASONS, so
        without the outcome fallback the most common monitor stop reads `other`.
        Driven through `apply_monitor_probe` and the store's commit point, with
        the reason code produced by the real PR classifier.
        """
        svc = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
        await svc.start()
        loop = await svc.add_monitor(
            slot_key="chat-1-123",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        assert loop.monitor is not None
        facts = PullRequestFacts(
            kind="github_pull_request",
            target="owner/repo#123",
            state=state,
            draft=False,
            head_revision="a" * 40,
            mergeability="mergeable",
            review_decision="none",
            checks=(),
            unresolved_review_threads=0,
            review_threads_complete=True,
        )
        classified, reason_code = classify_pull_request_facts(facts)
        assert classified is status
        rec.counters.clear()

        verdict = await svc.apply_monitor_probe(
            loop.id,
            MonitorProbeResult(
                canonical={},
                observation=MonitorObservation(f"{state}-1", classified, reason_code=reason_code),
            ),
            now=loop.monitor.created_ts + 10.0,
            config_generation=loop.monitor.config_generation,
        )

        assert not loop.active
        assert loop.monitor.stopped_reason == reason_code
        assert ml.stop_reason(reason_code) == ml.OTHER
        assert verdict.decision in (MonitorDecision.STOP_SUCCESS, MonitorDecision.STOP_BLOCKED)
        calls = _named(rec, ml.LOOPS_STOPPED)
        assert len(calls) == 1
        assert calls[0]["attrs"]["kind"] == ml.KIND_MONITOR
        assert calls[0]["attrs"]["reason"] == expected
        svc.stop()

    def test_a_known_stored_reason_wins_over_the_outcome(self, rec):
        record = _stop_record(monitor={"stopped_reason": "runtime_budget"})

        ml.record_loop_stopped(record, MonitorOutcome.BUDGET.value)

        assert _named(rec, ml.LOOPS_STOPPED)[-1]["attrs"]["reason"] == "runtime_budget"

    @pytest.mark.parametrize("outcome", [None, "", "not-an-outcome", 7])
    def test_an_unknown_reason_with_no_valid_outcome_stays_other(self, rec, outcome):
        record = _stop_record(monitor={"stopped_reason": "pull_request_merged"})

        ml.record_loop_stopped(record, outcome)

        assert _named(rec, ml.LOOPS_STOPPED)[-1]["attrs"]["reason"] == ml.OTHER

    def test_monitor_outcomes_keeps_only_enum_values(self):
        rows = [
            {"id": "a", "monitor": {"outcome": "success"}},
            {"id": "b", "monitor": {"outcome": "made-up"}},
            {"id": "c", "monitor": {"outcome": None}},
            {"id": "d"},
            {"monitor": {"outcome": "blocked"}},
            "not-a-row",
        ]

        assert ml.monitor_outcomes(rows) == {"a": "success"}

    def test_an_unknown_kind_becomes_other(self, rec):
        ml.record_loop_stopped({"kind": "something-new", "reason": "manual", "ran_secs": 1})

        assert _named(rec, ml.LOOPS_STOPPED)[-1]["attrs"]["kind"] == ml.OTHER


# ---------------------------------------------------------------------------
# monitor.decisions
# ---------------------------------------------------------------------------

#: One real ``(state, observation)`` pair per member of ``MonitorDecision``,
#: so the counter is exercised by the policy rather than by a patched return.
_DECISION_CASES = [
    (
        MonitorDecision.NO_CHANGE,
        _state(last_fingerprint="pending-a"),
        MonitorObservation("pending-a", MonitorObservationStatus.PENDING),
    ),
    (
        MonitorDecision.RECORD_ONLY,
        _state(last_fingerprint="pending-a"),
        MonitorObservation("pending-b", MonitorObservationStatus.PENDING),
    ),
    (
        MonitorDecision.WAKE_ACTIONABLE,
        _state(last_fingerprint="pending-a"),
        MonitorObservation("failure-b", MonitorObservationStatus.ACTIONABLE),
    ),
    (
        MonitorDecision.STOP_SUCCESS,
        _state(last_fingerprint="pending-a"),
        MonitorObservation("ready-c", MonitorObservationStatus.SUCCESS),
    ),
    (
        MonitorDecision.STOP_BLOCKED,
        _state(last_fingerprint="pending-a"),
        MonitorObservation(
            "closed-c",
            MonitorObservationStatus.BLOCKED,
            reason_code="pull_request_closed",
        ),
    ),
    (
        MonitorDecision.RETRY_PROVIDER,
        _state(consecutive_provider_errors=0, budgets=MonitorBudgets(max_provider_errors=3)),
        MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.TRANSIENT,
        ),
    ),
    (
        MonitorDecision.STOP_BUDGET,
        _state(provider_error_count=3, budgets=MonitorBudgets(max_provider_errors=3)),
        MonitorObservation("pending-a", MonitorObservationStatus.PENDING),
    ),
]

#: Every decision the enum declares has exactly one case above.
assert {case[0] for case in _DECISION_CASES} == set(MonitorDecision)


class TestMonitorDecisions:
    @pytest.mark.parametrize("decision", sorted(d.value for d in MonitorDecision))
    def test_every_decision_value_is_counted_under_its_own_name(self, rec, decision):
        state, observation = _case_for(decision)

        verdict = decide_monitor(state, observation, now=1_100.0)

        assert verdict.decision.value == decision, "the policy must reach this decision"
        calls = _named(rec, ml.MONITOR_DECISIONS)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"decision": decision, "kind": "github_pull_request"}

    def test_one_call_counts_once(self, rec):
        state, observation = _case_for(MonitorDecision.NO_CHANGE.value)

        decide_monitor(state, observation, now=1_100.0)
        decide_monitor(state, observation, now=1_101.0)

        assert len(_named(rec, ml.MONITOR_DECISIONS)) == 2

    def test_the_vocabulary_is_derived_from_the_enum(self):
        """Not a second hand-written copy of the enum, which would drift."""
        assert {d.value for d in MonitorDecision} == ml.MONITOR_DECISION_VALUES

    def test_an_unregistered_kind_becomes_other(self, rec):
        ml.record_monitor_decision(MonitorDecision.NO_CHANGE, "kind_from_a_newer_gateway")

        assert _named(rec, ml.MONITOR_DECISIONS)[-1]["attrs"]["kind"] == ml.OTHER

    def test_the_target_never_reaches_the_attributes(self, rec):
        state = _state(target="owner/private-repo#99", last_fingerprint="pending-a")

        decide_monitor(
            state,
            MonitorObservation("pending-a", MonitorObservationStatus.PENDING),
            now=1_100.0,
        )

        attrs = _named(rec, ml.MONITOR_DECISIONS)[-1]["attrs"]
        assert set(attrs) == {"decision", "kind"}
        assert "private-repo" not in "".join(attrs.values())

    @pytest.mark.parametrize("kind", sorted(ml.LOOP_KINDS))
    def test_the_loop_kinds_are_the_two_the_stop_log_writes(self, kind):
        """The three ``autonudge.*`` counters share one derivation of ``kind``."""
        assert kind in {"loop", "monitor"}


# ---------------------------------------------------------------------------
# telemetry off
# ---------------------------------------------------------------------------


class TestTelemetryDisabled:
    @pytest.mark.asyncio
    async def test_a_whole_loop_lifetime_records_nothing_with_telemetry_off(self, tmp_path):
        """A no-op recorder is what ``telemetry.enabled`` false installs.

        The observable property is that no instrument is ever created: the
        recorder's own caches stay empty, so nothing is exported and nothing is
        held per metric name.
        """
        noop = MetricsRecorder(None)
        assert not noop.enabled

        with patch("kiro_crew.metrics.provider.get_recorder", return_value=noop):
            svc = AutoNudgeService(base_dir=tmp_path)
            await svc.start()
            loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
            svc._on_fire = _coroutine_returning(True)
            await svc._timer(loop, delay=0)
            await svc.update(loop.id, active=False, stopped_reason="cycle_cap")
            decide_monitor(
                _state(last_fingerprint="pending-a"),
                MonitorObservation("pending-a", MonitorObservationStatus.PENDING),
                now=1_100.0,
            )

        assert noop._counters == {}
        assert noop._histograms == {}

    def test_a_raising_recorder_never_breaks_the_decision(self):
        """Telemetry must never break the path it sits on."""
        with patch("kiro_crew.metrics.events.emit_counter", side_effect=RuntimeError("boom")):
            verdict = decide_monitor(
                _state(last_fingerprint="pending-a"),
                MonitorObservation("pending-a", MonitorObservationStatus.PENDING),
                now=1_100.0,
            )

        assert verdict.decision is MonitorDecision.NO_CHANGE

    @pytest.mark.asyncio
    async def test_a_raising_recorder_never_breaks_an_arm(self, tmp_path):
        with patch("kiro_crew.metrics.events.emit_counter", side_effect=RuntimeError("boom")):
            svc = AutoNudgeService(base_dir=tmp_path)
            await svc.start()
            loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)

        assert loop.id in svc._loops

    @pytest.mark.asyncio
    async def test_a_raising_recorder_never_breaks_a_tick(self, tmp_path):
        delivered: list[str] = []

        async def _fire(watched, *args, **kwargs):
            delivered.append(watched.id)
            return True

        with patch("kiro_crew.metrics.events.emit_counter", side_effect=RuntimeError("boom")):
            svc = AutoNudgeService(base_dir=tmp_path)
            await svc.start()
            loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)
            svc._on_fire = _fire
            await svc._timer(loop, delay=0)

        assert delivered == [loop.id], "the turn must still have been delivered"
        assert loop.cycle_count == 1
        assert loop.active

    @pytest.mark.asyncio
    async def test_a_raising_recorder_never_breaks_a_stop(self, tmp_path, caplog):
        """The stop is counted after the commit; a failed count must not undo it.

        Only the stop recorder raises, and only around the stop, so the arm
        commits normally and the failure reaches the store's own guard around
        ``record_loop_stopped`` -- the guard this test pins. ``_emit`` swallows
        its own failures, so raising below it would never reach that guard.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        await svc.start()
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=3600)

        with (
            patch.object(ml, "record_loop_stopped", side_effect=RuntimeError("boom")) as raising,
            caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"),
        ):
            await svc.update(loop.id, active=False, stopped_reason="cycle_cap")

        assert raising.call_count == 1, "the stop must have reached the recorder"
        assert any("could not record a loop stop" in r.getMessage() for r in caplog.records)
        assert not loop.active
        reloaded = AutoNudgeService(base_dir=tmp_path)
        await reloaded.start()
        assert not reloaded._loops[loop.id].active, "the stop must have committed"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _coroutine_returning(value):
    async def _call(*args, **kwargs):
        return value

    return _call


def _coroutine_raising():
    async def _call(*args, **kwargs):
        raise RuntimeError("fire callback is wedged")

    return _call


_TORN_ID = "2f8efda1"


def _torn_row() -> dict:
    """A persisted row in the torn-deactivation shape ``_load`` resumes.

    Inactive, no stop reason, a live deadline and a stop-file path: the shape no
    stop path of the service writes (see ``store_loader._is_torn_deactivation``).
    """
    now = time.time()
    return {
        "id": _TORN_ID,
        "slot_key": "chat-746-1788942058",
        "message": "patrol",
        "idle_secs": 1200,
        "max_cycles": 0,
        "cycle_count": 3,
        "active": False,
        "last_fire_ts": now - 1500,
        "created_ts": now - 600,
        "stop_sentinel_path": "/nonexistent/workspace/.stop-chat-746-1788942058",
        "max_runtime_secs": 0,
        "gate": False,
        "stopped_reason": "",
        "approval_stalled": False,
        "next_due_ts": now - 300,
        "banner": "",
    }


def _write_store(tmp_path, *rows: dict) -> None:
    (tmp_path / "autonudge.json").write_text(json.dumps({"version": 1, "loops": list(rows)}))


async def _unreachable_fire(*args, **kwargs):  # pragma: no cover - must not run
    raise AssertionError("this tick must not reach the fire callback")


def _case_for(decision: str) -> tuple[MonitorState, MonitorObservation]:
    """The ``(state, observation)`` pair that reaches *decision*."""
    for want, state, observation in _DECISION_CASES:
        if want.value == decision:
            return state, observation
    raise AssertionError(f"no case reaches {decision!r}")
