"""AutoNudge rules the suite left unpinned before the service was split into owners.

The first five cases were found by mutating the pre-split ``autonudge.py`` and running
the owned AutoNudge tests: the mutation survived, so nothing held the rule. The last two
pin the maintenance transaction's explicit lock checks and the view's release of a loop it
could not quiesce. Each rule now lives in an owner module under
``kiro_crew/autonudge_service/``, and each test below fails when its rule is broken there.
"""

from __future__ import annotations

import json
import time

import pytest

import kiro_crew.autonudge as autonudge
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.monitoring.models import (
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorProbeResult,
    ProviderErrorKind,
)

_NOW = 1_000_000.0


def _loop(loop_id: str, **changes: object) -> NudgeLoop:
    values: dict[str, object] = {
        "id": loop_id,
        "slot_key": f"chat-1-{loop_id}",
        "message": "watch",
        "idle_secs": 600,
        "created_ts": _NOW,
    }
    values.update(changes)
    return NudgeLoop(**values)  # type: ignore[arg-type]


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: _NOW)


class TestTheTimerKeepsItsSchedule:
    @pytest.mark.asyncio
    async def test_a_quiet_tick_re_arms_on_a_fresh_full_interval(
        self, tmp_path, monkeypatch, frozen
    ) -> None:
        """A quiet tick spends no turn, so nothing else re-arms it: it has to set its own
        next deadline one full interval out, not fire again on the overdue beat."""

        async def _never(loop):
            raise AssertionError("a quiet tick must not fire")

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_never)
        loop = _loop("quiet001", next_due_ts=_NOW - 30)
        svc._loops[loop.id] = loop

        async def _quiet(target):
            return True

        armed: list[float | None] = []
        monkeypatch.setattr(svc, "_monitor_tick_is_quiet", _quiet)
        monkeypatch.setattr(svc, "_arm_timer", lambda target, delay=None: armed.append(delay))
        monkeypatch.setattr(svc, "_persist_soon", lambda: None)
        await svc._timer(loop, 0)
        assert loop.next_due_ts == _NOW + 600
        assert armed == [600.0]

    def test_an_arm_never_waits_longer_than_one_interval(
        self, tmp_path, monkeypatch, frozen
    ) -> None:
        """A deadline further out than the interval (a clock stepped back) is capped, so a
        loop cannot be parked past one full cycle."""
        svc = AutoNudgeService(base_dir=tmp_path)
        armed: list[float | None] = []
        monkeypatch.setattr(svc, "_arm_timer", lambda target, delay=None: armed.append(delay))
        svc._arm_from_deadline(_loop("far00001", next_due_ts=_NOW + 5_000))
        assert armed == [600.0]


class TestStructuredMonitorAccounting:
    @pytest.mark.asyncio
    async def test_a_provider_retry_never_waits_longer_than_the_cadence(self, tmp_path) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add_monitor(
            slot_key="chat-1",
            kind="github_pull_request",
            target="octo/repo#7",
            objective="review_ready",
            cadence_secs=20,
            budgets=MonitorBudgets(max_provider_errors=10),
            now=100.0,
        )
        assert loop.monitor is not None
        error = MonitorProbeResult(
            canonical={},
            observation=MonitorObservation(
                "",
                MonitorObservationStatus.PROVIDER_ERROR,
                provider_error=ProviderErrorKind.TRANSIENT,
                reason_code="provider_transient",
            ),
        )
        deadlines = []
        for tick in range(4):
            verdict = await svc.apply_monitor_probe(
                loop.id,
                error,
                now=200.0 + tick,
                config_generation=loop.monitor.config_generation,
            )
            assert verdict.decision is MonitorDecision.RETRY_PROVIDER
            deadlines.append(loop.next_due_ts - (200.0 + tick))
        # 15s, then doubling -- but never past the monitor's own 20s cadence.
        assert deadlines == [15.0, 20.0, 20.0, 20.0]
        assert loop.monitor.next_probe_at == loop.next_due_ts

    @pytest.mark.asyncio
    async def test_a_dispatched_wake_is_counted_once_when_its_turn_completes(
        self, tmp_path
    ) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add_monitor(
            slot_key="chat-1",
            kind="github_pull_request",
            target="octo/repo#7",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            now=100.0,
        )
        assert loop.monitor is not None
        try:
            assert await svc.mark_monitor_action_in_flight(loop.id, "fp-1", now=110.0)
            await svc.record_monitor_dispatched(loop.id, "fp-1", now=111.0)
            assert loop.monitor.wake_count == 1
            await svc.record_monitor_turn_completion(
                MonitorActionCompletion(
                    monitor_id=loop.id,
                    fingerprint="fp-1",
                    disposition=MonitorActionDisposition.SUCCESS,
                    completed_ts=120.0,
                    input_tokens=10,
                    output_tokens=5,
                )
            )
            assert loop.monitor.wake_count == 1
            assert loop.monitor.agent_turns == 1
            assert not loop.monitor.wake_in_flight
        finally:
            svc.stop()


class TestTheQuarantineSidecarKeepsAPeersRows:
    def test_compaction_keeps_a_row_this_instance_never_enumerated(self, tmp_path) -> None:
        """Only a row this instance enumerated at load may be compacted away; a row a peer
        holds aside after that load is its only durable copy."""
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()
        peer_row = {"id": "AKIAIOSFODNN7EXAMPLE", "slot_key": "chat-9-9", "message": "held"}
        sidecar = tmp_path / autonudge._QUARANTINE_FILE
        sidecar.write_text(json.dumps({"version": 1, "quarantined": [peer_row]}), encoding="utf-8")
        svc._save()
        assert json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"] == [peer_row]


class TestTheMaintenanceTransaction:
    @pytest.mark.asyncio
    async def test_a_mutation_lock_is_claimed_only_while_held_and_only_once(self, tmp_path) -> None:
        """Explicit checks rather than asserts, so ``python -O`` cannot drop them."""
        lock = autonudge._maintenance_lock(tmp_path)
        with pytest.raises(RuntimeError, match="must be held by the caller"):
            autonudge._claim_mutation_lock(lock)
        async with lock:
            autonudge._claim_mutation_lock(lock)
            try:
                with pytest.raises(RuntimeError, match="already has an owner"):
                    autonudge._claim_mutation_lock(lock)
            finally:
                autonudge._unclaim_mutation_lock(lock)
        with pytest.raises(RuntimeError, match="must be held by the caller"):
            autonudge._assert_mutation_lock_owned(lock)

    @pytest.mark.asyncio
    async def test_the_view_releases_a_loop_it_could_not_quiesce(self, tmp_path) -> None:
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            loop = await svc.add("chat-1-1", "watch", loop_id="view0001")
            async with AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
                # Unpublished, so the view is an offline load of the store: same row, not
                # the same object.
                stored = view.get_by_slot("chat-1-1")
                assert stored is not None and stored is not loop and stored.id == loop.id
                assert await view.deactivate_and_wait("missing01") is False
                assert "missing01" not in view._service._maintenance_quiescing
            assert await svc.deactivate_and_wait("missing01") is False
            assert await svc.deactivate_and_wait(loop.id) is True
            assert loop.active is False and loop.stopped_reason == "manual"
        finally:
            svc.stop()
