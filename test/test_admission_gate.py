"""Tests for posture-gated admission control.

Covers :func:`kiro_crew.resource_status.admission_check` (critical refuses;
ample/tight/unknown admit; off-switch; fail-open), the cron scheduler's
critical-posture deferral in ``_on_timer`` (deferred jobs are not marked
failed, fire on recovery, one INFO per episode; manual triggers are never
deferred), the subagent spawn refusal (typed SEL outcome + retry-later error),
and the ``agent.admission_gate`` config key.

The ``TestSpawnAdmissionGatePressureHold`` cases on the macOS kernel memory-pressure
hold are in ``test_admission_gate_pressure_hold.py``; it and ``TestSpawnAdmissionGate``
inherit the helper methods they share from ``_TestSpawnAdmissionGateHarness`` in
``admission_gate_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from admission_gate_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_managers,
    _TestSpawnAdmissionGateHarness,
    _wait_for,
)
from overload_fakes import wait_taskq_open

from kiro_crew import resource_status as rs
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS
from kiro_crew.cron import CronService
from kiro_crew.subagent import _UNLEARNED_DEDICATED_START_GB
from kiro_crew.testing.wait import await_until, wait_until


def _cfg(pressure: float = 4.0, critical: float = 2.0, gate: bool = True) -> SimpleNamespace:
    """Minimal stand-in for KiroCrewConfig exposing the gate's config surface."""
    return SimpleNamespace(
        agent=SimpleNamespace(
            resource_pressure_gb=pressure,
            resource_critical_gb=critical,
            admission_gate=gate,
        )
    )


def _refused() -> rs.AdmissionDecision:
    return rs.AdmissionDecision(
        admitted=False,
        posture=rs.POSTURE_CRITICAL,
        available_gb=1.2,
        reason=(
            "host memory is critical (~1.2 GB free, critical \u2264 2 GB) — "
            "retry when memory frees"
        ),
    )


def _admitted() -> rs.AdmissionDecision:
    return rs.AdmissionDecision(admitted=True, posture=rs.POSTURE_AMPLE, available_gb=16.0)


async def _backdate_persisted(svc: CronService, job_id: str, secs: float) -> None:
    """Make *job_id* overdue by *secs*, in memory AND in ``crons.json``.

    Any ``_sync()`` that finds the store's digest unlike the recorded one
    (a store write whose fingerprint read failed, as a briefly held file on
    Windows can make it) reloads ``svc._jobs`` as NEW objects. A job object a
    test captured earlier is then detached, so writing ``last_run_ts`` on it
    changes nothing the next tick scans. Rewriting the live record and saving
    it under the store lock keeps the backdate whether or not the tick reloads.
    """

    def _write() -> None:
        with svc._file_lock():
            svc._sync()
            live = next(j for j in svc._jobs if j.id == job_id)
            live.last_run_ts = time.time() - secs
            svc._save()

    await asyncio.to_thread(_write)


async def _retire_real_timer(svc: CronService) -> None:
    """Make the test's own ``await svc._on_timer()`` calls the only ticks.

    ``start()``, every ``add_job`` and every run completion arm a REAL ``_tick``.
    With a cron-expression job its delay is the next minute boundary (capped at the
    30 s poll), so a test that starts just before a boundary gets a real tick after
    its ``patch(...)`` window has closed, and that tick dispatches on the host's real
    admission verdict. Call it before the first ``add_job``.
    """
    svc._arm_timer = lambda: None  # type: ignore[method-assign]
    task, svc._timer_task = svc._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ── admission_check ──────────────────────────────────────────────────────────


class TestAdmissionCheck:
    def test_critical_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
        decision = rs.admission_check(_cfg())
        assert decision.admitted is False
        assert decision.posture == rs.POSTURE_CRITICAL
        assert "critical" in decision.reason
        assert "retry" in decision.reason

    @pytest.mark.parametrize(
        "avail,posture",
        [
            (3.0, rs.POSTURE_TIGHT),
            (32.0, rs.POSTURE_AMPLE),
            (-1.0, rs.POSTURE_UNKNOWN),  # unreadable probe → fail open
        ],
    )
    def test_non_critical_admits(
        self, monkeypatch: pytest.MonkeyPatch, avail: float, posture: str
    ) -> None:
        monkeypatch.setattr(rs, "_read_available_gb", lambda: avail)
        decision = rs.admission_check(_cfg())
        assert decision.admitted is True
        assert decision.posture == posture
        assert decision.reason == ""

    def test_off_switch_admits_even_when_critical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
        decision = rs.admission_check(_cfg(gate=False))
        assert decision.admitted is True
        # The posture is still reported truthfully — only enforcement is off.
        assert decision.posture == rs.POSTURE_CRITICAL

    def test_fail_open_on_probe_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(cfg: object | None = None) -> rs.ResourceStatus:
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(rs, "probe", _boom)
        decision = rs.admission_check(_cfg())
        assert decision.admitted is True
        assert decision.posture == rs.POSTURE_UNKNOWN

    def test_fail_open_on_config_load_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unreadable config must ADMIT (fail-open), never gate work on
        # default thresholds it could not actually read.
        monkeypatch.setattr(
            rs.KiroCrewConfig,
            "load",
            MagicMock(side_effect=RuntimeError("config unreadable")),
        )
        probe_mock = MagicMock()
        monkeypatch.setattr(rs, "probe", probe_mock)
        decision = rs.admission_check(None)
        assert decision.admitted is True
        assert decision.posture == rs.POSTURE_UNKNOWN
        probe_mock.assert_not_called()  # returned before probing

    def test_gate_defaults_on_when_config_lacks_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
        cfg = SimpleNamespace(
            agent=SimpleNamespace(resource_pressure_gb=4.0, resource_critical_gb=2.0)
        )
        assert rs.admission_check(cfg).admitted is False

    def test_non_bool_gate_value_defaults_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
        cfg = _cfg()
        cfg.agent.admission_gate = "yes"  # malformed → treated as enabled
        assert rs.admission_check(cfg).admitted is False


# ── cron deferral ────────────────────────────────────────────────────────────


class TestCronAdmissionDeferral:
    @pytest.mark.asyncio
    async def test_critical_defers_then_runs_on_recovery(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("gated", "msg", every_secs=60)
        job = svc._jobs[0]
        job.last_run_ts = time.time() - 120

        with caplog.at_level(logging.INFO, logger="kiro_crew.cron"):
            with patch("kiro_crew.cron.admission_check", return_value=_refused()):
                await svc._on_timer()
                await svc._on_timer()

        # Deferred: never fired, not marked failed, still due next tick.
        assert executed == []
        assert job.last_status is None
        assert job.id not in svc._claims
        infos = [
            r for r in caplog.records if r.levelno == logging.INFO and "deferring" in r.getMessage()
        ]
        assert len(infos) == 1  # one INFO per episode, not per tick

        # Recovery: the same job fires on the next admitted tick.
        with patch("kiro_crew.cron.admission_check", return_value=_admitted()):
            await svc._on_timer()
            run_task = svc._claims[job.id].task
        assert run_task is not None
        # The callback precedes finalization, and claim release precedes the
        # result merge. Wait for the whole run before changing its timestamp.
        await await_until(run_task, timeout=5.0)
        await _wait_for(lambda: "gated" in executed)

        # A NEW critical episode logs its own INFO line. The run's result merge
        # wrote the store from a worker thread, so backdate the live record.
        await _backdate_persisted(svc, job.id, 120)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="kiro_crew.cron"):
            with patch("kiro_crew.cron.admission_check", return_value=_refused()):
                await svc._on_timer()
        assert any(
            "deferring" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        )
        await svc.stop()

    @pytest.mark.asyncio
    async def test_a_new_episode_is_logged_when_the_tick_reloads_the_store(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        svc = CronService(base_dir=tmp_path, on_job=AsyncMock())
        await svc.start()
        await _retire_real_timer(svc)
        svc.add_job("gated", "msg", every_secs=60)
        job_id = svc._jobs[0].id
        await _backdate_persisted(svc, job_id, 120)
        with patch("kiro_crew.cron.admission_check", return_value=_admitted()):
            await svc._on_timer()
            run_task = svc._claims[job_id].task
        assert run_task is not None
        await await_until(run_task, timeout=5.0)

        # Force the next _sync() to reload, the way a failed fingerprint read
        # after a store write does: every job object is replaced.
        svc._reset_fingerprint()
        await _backdate_persisted(svc, job_id, 120)
        svc._reset_fingerprint()
        before = svc._jobs[0]

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="kiro_crew.cron"):
            with patch("kiro_crew.cron.admission_check", return_value=_refused()):
                await svc._on_timer()
        assert svc._jobs[0] is not before  # the tick really reloaded
        assert job_id not in svc._claims
        assert any(
            "deferring" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        )
        await svc.stop()

    @pytest.mark.asyncio
    async def test_manual_trigger_runs_despite_critical(self, tmp_path: Path) -> None:
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("manual", "msg", every_secs=3600)
        job_id = svc._jobs[0].id

        with patch("kiro_crew.cron.admission_check", return_value=_refused()):
            ran = await svc.run_job(job_id)

        assert ran is True
        assert executed == ["manual"]
        await svc.stop()

    @pytest.mark.asyncio
    async def test_admitted_tick_fires_normally(self, tmp_path: Path) -> None:
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("open", "msg", every_secs=60)
        svc._jobs[0].last_run_ts = time.time() - 120

        with patch("kiro_crew.cron.admission_check", return_value=_admitted()):
            await svc._on_timer()
        await _wait_for(lambda: "open" in executed)
        await svc.stop()


# ── spawn refusal ────────────────────────────────────────────────────────────


class TestSpawnAdmissionGate(_TestSpawnAdmissionGateHarness):
    @pytest.mark.parametrize("durable", [True, False], ids=["durable", "no-store"])
    def test_a_critical_posture_does_not_gate_a_spawn(self, durable: bool) -> None:
        """Spawns admit on the memory floor alone; the posture tier is cron's.

        At defaults the floor equals ``resource_critical_gb``, so a host
        admission has filled to the floor reads ``critical``: consulting the
        posture as well deferred every start at the very line the floor
        guarantees. The posture verdict is not read at all, durable or not:
        the start goes on to the stagger, which queues it behind the start
        that just went out -- a capacity wait, not a memory one.
        """
        mgr = self._mgr()
        if not durable:
            mgr._taskq = None
        mgr._last_spawn_ts = time.monotonic()
        mgr._spawn_stagger_secs = 3600.0
        posture = MagicMock(return_value=_refused())
        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.resource_status.cached_admission_check", posture),
            patch("kiro_crew.resource_status.admission_check", posture),
            # A posture read through the subagent module counts too; create=True
            # because the module does not define the name.
            patch("kiro_crew.subagent.cached_admission_check", posture, create=True),
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            mock_sel.return_value.log_tool_invocation = MagicMock()

            info = mgr.spawn(task="test task", parent_session_key="sess-1")

        assert info is not None and info.done is False and info.error == ""
        assert info.queued is True and info.queued_reason == "concurrency_limit"
        posture.assert_not_called()
        outcomes = [
            c[1]["outcome"] for c in mock_sel.return_value.log_tool_invocation.call_args_list
        ]
        assert not [o for o in outcomes if "critical" in o], outcomes

    def test_spawn_proceeds_past_gate_when_admitted(self) -> None:
        """An admitted decision falls through to the next guard (cwd here)."""
        mgr = self._mgr()
        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.validate_cwd", return_value=("", "not allowed")),
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cwd_allowed_roots = []
            mock_sel.return_value.log_tool_invocation = MagicMock()

            info = mgr.spawn(task="test task", parent_session_key="sess-1", cwd="/x")

        assert info is not None
        assert info.done is True
        call_kwargs = mock_sel.return_value.log_tool_invocation.call_args[1]
        assert call_kwargs["outcome"] == "rejected_invalid_cwd"

    def test_unmeasurable_memory_proceeds_but_is_logged(self) -> None:
        """(True, -1.0) means the guard did not run: spawn proceeds, SEL logs it.

        The cwd gate runs BEFORE the memory guard here (a bad path is refused
        before a row is persisted), so the guard's fall-through is observed at
        the next gate after it: the stagger, which queues the row behind the
        start that just went out.
        """
        mgr = self._mgr()
        mgr._last_spawn_ts = time.monotonic()
        mgr._spawn_stagger_secs = 3600.0
        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(True, -1.0)),
            patch("kiro_crew.platform_compat.IS_LINUX", True),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            mock_sel.return_value.log_tool_invocation = MagicMock()

            info = mgr.spawn(task="test task", parent_session_key="sess-1")

        # The spawn proceeded past the memory guard (it reached the stagger,
        # which queued the row), so the fail-open contract held...
        assert info is not None
        assert info.done is False and info.queued is True
        assert info.queued_reason == "concurrency_limit"
        outcomes = [
            c[1]["outcome"] for c in mock_sel.return_value.log_tool_invocation.call_args_list
        ]
        # ...and the guard-did-not-run case was made observable.
        assert outcomes == ["memory_check_unavailable"]
        unavailable = mock_sel.return_value.log_tool_invocation.call_args_list[0][1]
        assert unavailable["tool_name"] == "spawn_run"
        assert unavailable["metadata"]["min_gb"] == 5.0  # floor plus the pending process
        assert unavailable["metadata"]["task"] == "test task"

    @pytest.mark.parametrize(
        ("configured", "expected_min_gb"),
        [
            # floor plus one start at the measured unlearned dedicated price,
            # which the configured cost cannot lower
            (0.5, 4.0 + _UNLEARNED_DEDICATED_START_GB),
            (2.0, 6.0),  # an operator's higher pin still prices the start
        ],
    )
    def test_the_pending_start_is_priced_at_its_dedicated_projection(
        self, configured, expected_min_gb
    ) -> None:
        """A start costs what a runtime settles at, not what a run grew to.

        A run's peak RSS is its whole subtree -- test suites and builds it
        launched included -- so neither a learned whole-tree p90 nor a live
        worker's peak may price the next start: that held ordinary spawns at
        10 GB+ on a laptop. With no learned settled figure, the dedicated price
        is the larger of the configured cost and the measured unlearned start.
        A settled worker already sits inside the free-memory reading and owes
        nothing.
        """
        from kiro_crew.subagent import SubagentInfo

        mgr = self._mgr()
        mgr._agents["heavy"] = SubagentInfo(
            id="heavy", task="w", peak_rss_gb=7.5, last_rss_gb=1.0, _rss_samples=2, _pid=4242
        )
        seen: list[float] = []

        def memory_check(*, min_gb, **_kw):
            seen.append(min_gb)
            return True, 32.0

        with (
            patch("kiro_crew.subagent.check_memory_available", side_effect=memory_check),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = configured
            mock_sel.return_value.log_tool_invocation = MagicMock()

            mgr.spawn(task="test task", parent_session_key="sess-1")

        assert seen == [pytest.approx(expected_min_gb)]

    def test_low_memory_deferral_names_what_it_needs(self) -> None:
        """A deferral says how much memory it saw and how much the start needs."""
        mgr = self._mgr()
        assert mgr._taskq is not None
        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(False, 3.0)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            mock_sel.return_value.log_tool_invocation = MagicMock()

            info = mgr.spawn(task="test task", parent_session_key="sess-1")

        assert info is not None and info.queued is True
        call_kwargs = mock_sel.return_value.log_tool_invocation.call_args[1]
        assert call_kwargs["outcome"] == "deferred_low_memory"
        assert call_kwargs["metadata"]["startup_cost_gb"] == pytest.approx(0.5)
        assert call_kwargs["metadata"]["min_gb"] == pytest.approx(5.0)
        deferred = [e for e in mgr._taskq.events(info.id) if e.kind == "deferred"]
        reason = str(deferred[-1].data.get("reason")) if deferred else ""
        assert "3.0 GB available" in reason
        assert "(1.00 GB for this start)" in reason

    # ── the deferral reason reaches the UI event and the caller ──────────────
    #
    # ``subagent_queued`` carried only a count, so every UI reading it rendered
    # "queued behind the concurrency limit" for a row the MEMORY guard parked,
    # and ``POST /api/spawn`` answered ``spawned`` for it. The gate's verdict is
    # unchanged here; only what it tells the caller is.

    def test_low_memory_deferral_names_its_reason_on_the_queued_event(self) -> None:
        mgr = self._mgr()
        assert mgr._taskq is not None
        info, events, _sel = self._spawn_capturing_queued(mgr, memory=(False, 3.2))
        assert info is not None and info.queued is True and info.done is False
        assert info.queued_reason == "low_memory"
        assert "3.2 GB available" in info.queued_reason_detail
        assert events, "the deferral must still emit the advisory queued count"
        last = events[-1]
        assert last["queued"] == 1
        assert last["reason"] == "low_memory"
        assert last["available_gb"] == pytest.approx(3.2)
        # spawn_min_memory_gb 4.0 + one warming start at the configured 0.5.
        assert last["required_gb"] == pytest.approx(5.0)

    def test_a_non_durable_low_memory_wait_names_its_reason_on_the_queued_event(
        self,
    ) -> None:
        """No store row to defer: the in-memory wait carries the same label."""
        mgr = self._mgr()
        mgr._taskq = None
        info, events, _sel = self._spawn_capturing_queued(mgr, memory=(False, 3.2))
        assert info is not None and info.queued is True and info.done is False
        assert info.queued_reason == "low_memory"
        assert "3.2 GB available" in info.queued_reason_detail
        assert events and events[-1]["reason"] == "low_memory"
        assert events[-1]["available_gb"] == pytest.approx(3.2)
        assert events[-1]["required_gb"] == pytest.approx(5.0)


# ── config key ───────────────────────────────────────────────────────────────


def _load_from_dict(data: dict, tmp_path: Path) -> KiroCrewConfig:
    """Write *data* to a config file under *tmp_path* and load it."""
    tmp = tmp_path / "config.json"
    tmp.write_text(json.dumps(data))
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
        return KiroCrewConfig.load()


class TestAdmissionGateConfig:
    def test_defaults_on(self, tmp_path: Path) -> None:
        cfg = _load_from_dict({}, tmp_path)
        assert cfg.agent.admission_gate is True

    def test_off_switch(self, tmp_path: Path) -> None:
        cfg = _load_from_dict({"agent": {"admission_gate": False}}, tmp_path)
        assert cfg.agent.admission_gate is False

    def test_non_bool_value_falls_back_to_default(self, tmp_path: Path) -> None:
        cfg = _load_from_dict({"agent": {"admission_gate": "nope"}}, tmp_path)
        assert cfg.agent.admission_gate is True


class TestCachedAdmissionCheck:
    """cached_admission_check() — the non-blocking verdict for event-loop
    callers: no inline I/O, background refresh, bounded staleness."""

    def _reset(self) -> None:
        rs._cached_decision = None
        rs._cached_at = 0.0

    def test_first_call_fails_open_and_kicks_refresh(self, monkeypatch) -> None:
        self._reset()
        gate = threading.Event()
        verdict = _refused()

        def fake_check(cfg: object | None = None) -> rs.AdmissionDecision:
            gate.wait(5.0)  # hold the refresh until fail-open is asserted
            return verdict

        monkeypatch.setattr(rs, "admission_check", fake_check)
        try:
            first = rs.cached_admission_check()
            assert first.admitted  # fail-open before the first refresh lands
            gate.set()
            wait_until(
                lambda: rs._cached_decision is not None
            )  # refresh thread publishes shortly after
            assert rs.cached_admission_check() is verdict  # fresh cache served
        finally:
            # A refused verdict left in the module-global cache would poison
            # every spawn-exercising test in this worker for the TTL window.
            self._reset()

    def test_fresh_cache_is_served_without_probing(self, monkeypatch) -> None:
        self._reset()
        verdict = _refused()
        rs._cached_decision = verdict
        rs._cached_at = time.monotonic()
        probes: list[int] = []
        monkeypatch.setattr(rs, "admission_check", lambda cfg=None: probes.append(1))
        # The module's own binding: a refresh is a thread the call would construct
        # here, so "none was started" is read off the call, not off a sleep.
        threads = MagicMock()
        monkeypatch.setattr(rs, "threading", SimpleNamespace(Thread=threads))
        try:
            assert rs.cached_admission_check() is verdict
            threads.assert_not_called()  # fresh cache => no background refresh either
            assert probes == []
        finally:
            self._reset()


class TestCronExprPassthrough:
    """Cron-expression jobs run normally even under critical posture: they
    cannot be deferred statelessly (in-memory markers lose the occurrence on
    restart; dropping loses it outright), so only ``every``/``at`` jobs —
    which stay due on their own — are deferred."""

    @pytest.mark.asyncio
    async def test_job_claimed_during_admission_await_is_not_double_fired(
        self, tmp_path: Path
    ) -> None:
        # The admission await yields the loop; a manual run can claim the job
        # meanwhile. The timer must revalidate and skip it, never start a
        # duplicate execution over the in-flight run.
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("claimed", "msg", every_secs=60)
        job = svc._jobs[0]
        job.last_run_ts = time.time() - 120

        manual: list[Any] = []

        def claiming_check(cfg: object | None = None):
            manual.append(svc._claim_run(job.id, "manual"))  # a manual run claims it
            return _admitted()

        with patch("kiro_crew.cron.admission_check", side_effect=claiming_check):
            await svc._on_timer()
        # The scan claims what it dispatches before it returns, so a duplicate
        # would already have replaced the manual claim.
        assert svc._claims.get(job.id) is manual[0], "a duplicate run claimed the job"
        assert executed == []  # revalidated away, no duplicate
        svc._claims.pop(job.id, None)
        await svc.stop()

    @pytest.mark.asyncio
    async def test_manual_run_completed_during_await_is_not_double_fired(
        self, tmp_path: Path
    ) -> None:
        # Harder variant: the manual run starts AND FINISHES during the
        # admission await, so the job holds no claim. An id-only
        # revalidation would double-fire; the live-object _is_due re-check
        # (advanced last_run_ts) must catch it.
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("finished", "msg", every_secs=60)
        job = svc._jobs[0]
        job.last_run_ts = time.time() - 120

        def completing_check(cfg: object | None = None):
            job.last_run_ts = time.time()  # manual run ran to completion
            return _admitted()

        with patch("kiro_crew.cron.admission_check", side_effect=completing_check):
            await svc._on_timer()
        # The scan claims what it dispatches before it returns, and a run releases
        # its claim only after its callback ran: one of the two shows a re-fire.
        assert job.id not in svc._claims, "the tick dispatched the completed job"
        assert executed == []  # not re-fired against the stale snapshot
        await svc.stop()

    @pytest.mark.asyncio
    async def test_job_edited_during_await_dispatches_live_object(self, tmp_path: Path) -> None:
        # A job replaced during the await must execute its LIVE definition,
        # not the stale snapshot's.
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.message)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("edited", "old-message", every_secs=60)
        job = svc._jobs[0]
        job.last_run_ts = time.time() - 120

        def editing_check(cfg: object | None = None):
            job.message = "new-message"
            return _admitted()

        with patch("kiro_crew.cron.admission_check", side_effect=editing_check):
            await svc._on_timer()
        await _wait_for(lambda: len(executed) == 1)
        assert executed == ["new-message"]
        await svc.stop()

    @pytest.mark.asyncio
    async def test_cron_expr_job_runs_normally_under_critical(self, tmp_path: Path) -> None:
        # A cron-expression job whose minute matches during a critical
        # episode fires anyway — the occurrence is neither dropped nor
        # remembered in state that a restart would lose.
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        await _retire_real_timer(svc)
        svc.add_job("expr-job", "msg", cron_expr="* * * * *")

        with (
            patch("kiro_crew.cron.admission_check", return_value=_refused()),
            patch("kiro_crew.cron.cron_expr_matches", return_value=True),
        ):
            await svc._on_timer()
        await _wait_for(lambda: "expr-job" in executed)
        await svc.stop()

    @pytest.mark.asyncio
    async def test_mixed_due_defers_interval_but_fires_expr(self, tmp_path: Path) -> None:
        # One tick, both kinds due, critical posture: the interval job is
        # deferred (stays due, untouched), the cron-expression job fires.
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        # add_job("expr") arms a real tick for the next minute boundary; one that
        # lands after the patch window below runs on the host's real verdict.
        await _retire_real_timer(svc)
        svc.add_job("interval", "msg", every_secs=60)
        svc.add_job("expr", "msg", cron_expr="* * * * *")
        interval_job = next(j for j in svc._jobs if j.name == "interval")
        interval_job.last_run_ts = time.time() - 120

        with (
            patch("kiro_crew.cron.admission_check", return_value=_refused()),
            patch("kiro_crew.cron.cron_expr_matches", return_value=True),
        ):
            await svc._on_timer()
        # The tick claims what it dispatches before it returns.
        assert interval_job.id not in svc._claims, "the interval job was dispatched"
        await _wait_for(lambda: "expr" in executed)
        assert executed == ["expr"]  # interval deferred, not fired
        assert interval_job.last_status is None  # untouched: still due

        # Recovery: the deferred interval job fires on its own.
        with patch("kiro_crew.cron.admission_check", return_value=_admitted()):
            await svc._on_timer()
        await _wait_for(lambda: "interval" in executed)
        await svc.stop()

    @pytest.mark.asyncio
    async def test_deferral_episode_floors_timer_delay(self, tmp_path: Path) -> None:
        # A deferred (overdue) interval job would otherwise re-arm the timer
        # at zero delay — a busy loop of scans and admission probes on a host
        # already under memory pressure. During an episode the re-arm delay
        # is floored at the poll cadence.
        from kiro_crew.cron import _TIMER_POLL_SECS

        svc = CronService(base_dir=tmp_path, on_job=AsyncMock())
        await svc.start()
        svc.add_job("overdue", "msg", every_secs=60)
        svc._jobs[0].last_run_ts = time.time() - 120

        assert svc._effective_delay() < 1.0  # overdue: due immediately

        with patch("kiro_crew.cron.admission_check", return_value=_refused()):
            await svc._on_timer()  # opens the episode, defers the job
        assert svc._admission_deferring is True
        assert svc._effective_delay() == _TIMER_POLL_SECS  # floored

        with patch("kiro_crew.cron.admission_check", return_value=_admitted()):
            await svc._on_timer()  # recovery closes the episode
        assert svc._admission_deferring is False
        await svc.stop()

    @pytest.mark.asyncio
    async def test_interval_edited_to_cron_during_await_still_fires(self, tmp_path: Path) -> None:
        # An interval job edited into a matching cron expression during the
        # admission await must be classified by its LIVE kind: partitioning
        # the stale snapshot would defer-and-drop the occurrence.
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("morph", "msg", every_secs=60)
        job = svc._jobs[0]
        job.last_run_ts = time.time() - 120

        from kiro_crew.cron import CronSchedule

        def editing_check(cfg: object | None = None):
            job.schedule = CronSchedule(kind="cron", cron_expr="* * * * *")
            job.last_run_ts = None  # cron kind: same-minute guard off
            return _refused()

        with (
            patch("kiro_crew.cron.admission_check", side_effect=editing_check),
            patch("kiro_crew.cron.cron_expr_matches", return_value=True),
        ):
            await svc._on_timer()
        await _wait_for(lambda: "morph" in executed)  # fired, not deferred
        await svc.stop()

    @pytest.mark.asyncio
    async def test_queued_nonbatch_rejection_announced_exactly_once(self) -> None:
        # A queued single spawn rejected at drain time (here: by governance,
        # re-checked before dispatch) must produce EXACTLY ONE completion announcement. The drain
        # loop announces it off the returned info; spawn's own
        # _announce_rejection must stay batch-only, or the requester gets a
        # duplicate completion injection and wave/orchestration counters
        # double-count the failure. Exercises the REAL spawn path (no stubs)
        # so both potential announce sites are live.
        from kiro_crew.subagent import SubagentManager

        announced: list = []

        async def _on_done(info) -> None:
            announced.append(info)

        sessions = MagicMock()
        sessions.get_agent_selection.return_value = ("template", "")
        mgr = SubagentManager(
            sessions=sessions,
            ctx_builder=MagicMock(),
            on_done=_on_done,
            max_concurrent=3,
        )
        mgr._queue = [
            {
                "task": "queued then refused",
                "parent_session_key": "sess-1",
                "_preassigned_id": "q1",
            }
        ]
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._emit_queue_depth = MagicMock()

        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent._vet_spawn_governance", return_value="spawning is disabled"),
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            mock_sel.return_value.log_tool_invocation = MagicMock()
            # On a running loop the pump is a coroutine (its store reads run
            # off-loop); await one pass directly.
            await mgr._drain_queue_async()
            # Flush every announce coroutine scheduled via ensure_future —
            # a duplicate would surface as a second on_done call here.
            for _ in range(5):
                await asyncio.sleep(0)

        assert [i.id for i in announced] == [
            "q1"
        ], f"expected exactly one announcement, got {len(announced)}"
        assert "spawning is disabled" in announced[0].error

    @pytest.mark.asyncio
    async def test_interval_job_not_replayed_after_manual_run(self, tmp_path: Path) -> None:
        # An ``every`` job stays due on its own during a critical episode;
        # a manual trigger that completes the work must not be replayed on
        # recovery (deferral keeps no per-job state that could replay it).
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("interval", "msg", every_secs=3600)
        job = svc._jobs[0]
        job.last_run_ts = time.time() - 7200

        with patch("kiro_crew.cron.admission_check", return_value=_refused()):
            await svc._on_timer()
        assert executed == []  # deferred

        # Manual run during the episode completes the work.
        with patch("kiro_crew.cron.admission_check", return_value=_refused()):
            assert await svc.run_job(job.id) is True
        await _wait_for(lambda: executed == ["interval"])
        job.last_run_ts = time.time()  # manual run marked it

        with patch("kiro_crew.cron.admission_check", return_value=_admitted()):
            await svc._on_timer()
        # The recovery tick claims what it dispatches before it returns.
        assert job.id not in svc._claims, "the recovery tick replayed the job"
        assert executed == ["interval"]  # no replay
        await svc.stop()

    def test_refresh_thread_start_failure_fails_open(self, monkeypatch) -> None:
        rs._cached_decision = None
        rs._cached_at = 0.0
        # The module's own binding, not the stdlib class every thread in the
        # worker constructs through.
        monkeypatch.setattr(
            rs,
            "threading",
            SimpleNamespace(Thread=MagicMock(side_effect=RuntimeError("can't start new thread"))),
        )
        verdict = rs.cached_admission_check()  # must not raise
        assert verdict.admitted  # fail-open
        # The refresh lock was released, not leaked:
        assert rs._cache_refresh_inflight.acquire(blocking=False)
        rs._cache_refresh_inflight.release()


class TestALearnedWholeTreePeakNeverPricesAStart:
    """A cost log whose p90 is a whole-tree peak does not raise the start bar."""

    def test_a_132_gb_learned_p90_leaves_the_bar_at_floor_plus_the_start_price(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.subagent import SubagentManager

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        log = tmp_path / "subagents" / "cost_samples.jsonl"
        log.parent.mkdir(parents=True)
        now = time.time()
        log.write_text(
            "".join(
                json.dumps({"agent": "kirocrew", "mem_gb": v, "cpu_cores": 1.0, "ts": now - i})
                + "\n"
                for i, v in enumerate([1.2] * 5 + [132.3] * 5)
            )
        )
        sessions = MagicMock()
        sessions.get_agent_selection.return_value = ("template", "")
        mgr = SubagentManager(
            sessions=sessions,
            ctx_builder=MagicMock(),
            on_done=MagicMock(),
            max_concurrent=3,
        )
        # A learned p90 published on the manager, where a gate that priced
        # starts from learned costs would read it. The start bar ignores it.
        mgr._learned_costs_gb = {"kirocrew": 132.3}  # type: ignore[attr-defined]
        asked: list[float] = []

        def _check(min_gb: float) -> tuple[bool, float]:
            asked.append(min_gb)
            return (85.9 >= min_gb, 85.9)

        with (
            patch("kiro_crew.subagent.check_memory_available", side_effect=_check),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel"),
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            info = mgr.spawn(task="t", parent_session_key="s")

        assert asked == [pytest.approx(4.0 + _UNLEARNED_DEDICATED_START_GB)]
        assert info is not None and info.queued is False


# ── who counts as "a dedicated runtime of ours" for the pressure hold ────────


def _row(**fields: Any) -> Any:
    from kiro_crew.subagent import SubagentInfo

    return SubagentInfo(id=str(fields.pop("id", "r")), task="t", **fields)


@pytest.mark.parametrize(
    ("rows", "claims", "owns"),
    [
        pytest.param([], {}, False, id="empty"),
        pytest.param([{}], {}, True, id="a-dedicated-live-row"),
        pytest.param([{"_session_sharing": True}], {}, False, id="confirmed-shared"),
        pytest.param([{"_start_priced_shared": True}], {}, False, id="in-flight-priced-shared"),
        pytest.param(
            [{"_session_sharing": True}, {"id": "b", "_session_sharing": True}],
            {},
            False,
            id="two-shared",
        ),
        pytest.param([{"_awaiting_approval": True}], {}, False, id="parked-at-spawn-approval"),
        pytest.param(
            [{"_awaiting_approval": True, "_exec_started": 1.0}],
            {},
            True,
            id="waiting-on-a-tool-prompt",
        ),
        pytest.param([{"_start_release": object()}], {}, False, id="approved-awaiting-release"),
        pytest.param([{"done": True}], {}, False, id="done"),
        pytest.param([{"queued": True}], {}, False, id="queued"),
        # A finished dedicated row keeps its slot until its teardown completes;
        # it still owns no runtime the hold could wait on.
        pytest.param(
            [{"done": True, "_slot_released": False}], {}, False, id="finished-row-tearing-down"
        ),
        # A yielded dedicated parent mid-resume (its slot re-reserved before the
        # flag clears) still has its process, so it counts.
        pytest.param(
            [{"_slot_released": True, "_resume_pending": True}],
            {},
            True,
            id="yielded-parent-mid-resume",
        ),
        pytest.param([], {"c1": (1.0, False)}, True, id="unregistered-dedicated-claim"),
        pytest.param([], {"c1": (0.65, True)}, False, id="unregistered-shared-claim"),
    ],
)
def test_owns_dedicated_runtime(
    rows: list[dict[str, Any]], claims: dict[str, tuple[float, bool]], owns: bool
) -> None:
    from kiro_crew.subagent import _owns_dedicated_runtime

    assert _owns_dedicated_runtime([_row(**r) for r in rows], claim_prices=claims) is owns


def test_a_defer_write_outage_answers_still_queued_never_refused() -> None:
    """A ``TaskStoreUnavailable`` leaves the row QUEUED and due: announcing a
    refusal would give one id a refusal now and a completion when the pump starts
    it later."""
    from kiro_crew import taskq
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    sessions = MagicMock()
    sessions.get_agent_selection.return_value = ("template", "")
    mgr = SubagentManager(
        sessions=sessions, ctx_builder=MagicMock(), on_done=MagicMock(), max_concurrent=3
    )
    store = mgr._taskq
    assert store is not None
    queued = SubagentInfo(id="row1", task="t", parent_session_key="sess-1", queued=True)
    refused = SubagentInfo(
        id="row1", task="t", parent_session_key="sess-1", done=True, error="refused"
    )
    mgr._admission.park_defer(
        "row1",
        reason="low memory",
        parent_session_key="sess-1",
        batch_id="",
        queued=queued,
        refused=refused,
        wait={"reason": "low_memory"},
    )
    announce = MagicMock(side_effect=lambda info: info)

    async def _outage(*_args: Any, **_kwargs: Any) -> Any:
        raise taskq.TaskStoreUnavailable("disk gone")

    async def run() -> Any:
        with (
            patch.object(store, "run", _outage),
            patch.object(mgr, "_announce_rejection", announce),
        ):
            return await mgr._admission.finish_parked_defer(queued)

    answer = asyncio.run(run())
    assert answer is queued and answer.done is False
    announce.assert_not_called()


def test_a_queued_stop_drops_what_the_process_kept_for_the_start() -> None:
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_agent_selection.return_value = ("template", "")
    mgr = SubagentManager(
        sessions=sessions, ctx_builder=MagicMock(), on_done=MagicMock(), max_concurrent=3
    )
    mgr._held_approval_modes["x"] = "auto"
    mgr._pressure_holds["x"] = time.monotonic()
    mgr._pressure_hold_expired.add("x")

    async def run() -> None:
        mgr._report_queued_stop({"_preassigned_id": "x", "parent_session_key": "sess-1"})
        await asyncio.sleep(0)

    asyncio.run(run())
    assert "x" not in mgr._held_approval_modes
    assert "x" not in mgr._pressure_holds and "x" not in mgr._pressure_hold_expired


def test_the_user_docs_state_the_hold_bounds_the_code_uses() -> None:
    """``subagents.md`` names the recheck interval and the per-start bound in
    words; they must be what the gate runs on. The bound is
    ``agent.subagent_queue_max_wait_secs``, so the docs name that key and its
    default, and the config field and its configuration.md row carry the same
    default."""
    from kiro_crew.config.sections import AgentConfig
    from kiro_crew.subagent_wait_reasons import MEMORY_PRESSURE_RECHECK_SECS

    root = Path(__file__).resolve().parents[1]
    minutes = DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS // 60
    assert AgentConfig().subagent_queue_max_wait_secs == DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS
    doc = (root / "src/kiro_crew/docs/subagents.md").read_text(encoding="utf-8")
    assert f"every {MEMORY_PRESSURE_RECHECK_SECS} seconds" in doc
    hold = next(line for line in doc.splitlines() if line.startswith("- **macOS memory pressure**"))
    assert f"held for `agent.subagent_queue_max_wait_secs` ({minutes} minutes by default)" in hold
    # The bound counts time with no start in the row's lane (its chat's queue,
    # any batch), not time since submission, and the wave's results name what
    # never started and when to resubmit it.
    assert "with no other top-level start from the same chat beginning" in hold
    assert "from any `spawn_run` batch" in hold
    assert "to resubmit once memory eases" in hold
    reference = (root / "src/kiro_crew/docs/configuration.md").read_text(encoding="utf-8")
    row = next(
        line
        for line in reference.splitlines()
        if line.startswith("| `agent.subagent_queue_max_wait_secs` |")
    )
    assert "macOS memory-pressure hold" in row
    assert row.endswith(f"| `{DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS}` ({minutes} min) |")
    # The key moves the bound live and the frame carries it, so no catalog may
    # name a number: the bound reaches each give-up clause as ``{{limit}}``, and
    # the sentence without a bound (``0``) has no give-up clause at all.
    catalogs = sorted((root / "website/src/i18n/locales").glob("*.json"))
    # en.manual.json holds only hand-authored keys with no source literal.
    for path in (p for p in catalogs if p.name != "en.manual.json"):
        chip = json.loads(path.read_text(encoding="utf-8"))["pages"]["chat"]["subagentQueued"]
        for key in ("low_memory", "low_memory_no_figures", "memory_pressure"):
            assert "{{limit}}" not in chip[key], (path.name, key)
            assert "{{limit}}" not in chip[f"{key}_no_limit"], (path.name, key)
            assert "{{limit}}" in chip[f"{key}_limit"], (path.name, key)
            if path.name != "en-XA.json":  # the pseudo-locale pads and brackets each string
                assert chip[f"{key}_limit"].startswith(chip[key]), (path.name, key)
                assert chip[f"{key}_no_limit"].startswith(chip[key]), (path.name, key)
        for key in ("memory_pressure", "memory_pressure_limit", "memory_pressure_no_limit"):
            assert not re.search(r"\d", chip[key]), (path.name, key)
    english = json.loads((root / "website/src/i18n/locales/en.json").read_text(encoding="utf-8"))
    chip_en = english["pages"]["chat"]["subagentQueued"]
    # A duration the memory must stay short for, not a deadline from now.
    # The chip names the same outcome the terminal report does: they never start.
    for key in ("low_memory_limit", "low_memory_no_figures_limit", "memory_pressure_limit"):
        assert chip_en[key].endswith("they never start if memory stays short for {{limit}}"), key
    assert chip_en["memory_pressure_no_limit"].endswith("they wait with no time limit")


def test_the_card_and_the_gate_agree_on_the_never_started_prefix() -> None:
    """The run card headlines a wholly never-started wave by matching the gate's
    terminal error, so the two spellings must not drift."""
    from kiro_crew.subagent_wait_reasons import (
        MEMORY_PRESSURE_NEVER_STARTED,
        NEVER_STARTED_MEMORY_PREFIX,
        QUEUED_WAIT_EXPIRED_TEXT,
    )

    phrases = json.loads(
        (Path(__file__).resolve().parents[1] / "website/src/lib/backendPhrases.json").read_text(
            encoding="utf-8"
        )
    )
    assert phrases["neverStartedPrefix"] == NEVER_STARTED_MEMORY_PREFIX
    # Both memory terminals open with it, and each ends with its next step.
    for terminal in (MEMORY_PRESSURE_NEVER_STARTED, QUEUED_WAIT_EXPIRED_TEXT):
        assert terminal.startswith(NEVER_STARTED_MEMORY_PREFIX)
        assert "; retry once " in terminal
    # The card names the reason each run ended with, so the pressure prefix
    # matches the pressure terminal alone.
    pressure = phrases["neverStartedPressurePrefix"]
    assert pressure.startswith(NEVER_STARTED_MEMORY_PREFIX)
    assert MEMORY_PRESSURE_NEVER_STARTED.startswith(pressure)
    assert not QUEUED_WAIT_EXPIRED_TEXT.startswith(pressure)


def test_event_loop_spawns_validate_the_agent_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``spawn_async`` and the coroutine pump take the agent-directory scan off
    the loop and hand the gate the answer; the gate never walks it on the loop."""
    import kiro_crew.subagent as subagent_mod
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    threads: list[bool] = []

    def _validate(agent: str, cwd: str = "") -> tuple[str, str, str]:
        threads.append(threading.current_thread() is threading.main_thread())
        return agent, "", ""

    monkeypatch.setattr(subagent_mod, "_validate_agent", _validate)

    async def run() -> Any:
        sessions = MagicMock()
        sessions.get_agent_selection.return_value = ("template", "")
        mgr = SubagentManager(
            sessions=sessions, ctx_builder=MagicMock(), on_done=MagicMock(), max_concurrent=3
        )
        await wait_taskq_open(mgr)
        mgr._spawn_stagger_secs = 60.0  # the second spawn queues for the stagger
        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel"),
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            await mgr.spawn_async("one", parent_session_key="sess-1", agent="reviewer")
            queued = await mgr.spawn_async("two", parent_session_key="sess-1", agent="reviewer")
            assert queued is not None and queued.queued is True
            mgr._spawn_stagger_secs = 0.0
            mgr._last_spawn_ts = 0.0
            calls_before_drain = len(threads)
            mgr._drain_queue()
            await _wait_for(
                lambda: len(threads) > calls_before_drain,
                message="the pump's agent re-validation",
            )
            await _wait_for(
                lambda: mgr._drain_task is None or mgr._drain_task.done(),
                message="the pump pass to finish",
            )
        await mgr.cancel_all()

    asyncio.run(run())
    assert threads and not any(threads), threads


def test_an_app_spawn_proves_ownership_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """App ownership walks the agents directory too, so the gate must read the
    off-loop answer instead of calling it on the loop."""
    import kiro_crew.subagent as subagent_mod
    from kiro_crew.subagent import SubagentManager

    threads: list[bool] = []

    def _owner(agent: str, app: str) -> str:
        threads.append(threading.current_thread() is threading.main_thread())
        return ""

    monkeypatch.setattr(subagent_mod, "_validate_app_agent_ownership", _owner)
    monkeypatch.setattr(subagent_mod, "_validate_agent", lambda a, cwd="": (a, "", ""))

    async def run() -> Any:
        sessions = MagicMock()
        sessions.get_agent_selection.return_value = ("template", "")
        mgr = SubagentManager(
            sessions=sessions, ctx_builder=MagicMock(), on_done=MagicMock(), max_concurrent=3
        )
        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel"),
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            await mgr.spawn_async(
                "one", parent_session_key="sess-1", agent="app__helper", app="app"
            )
        await mgr.cancel_all()

    asyncio.run(run())
    assert threads and not any(threads), threads


def test_the_off_loop_check_keys_on_the_app_the_gate_settles_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn inside an app session passes no ``app`` of its own; the gate takes
    the captured execution's app (``resolve_spawn_execution``), so the off-loop
    answer must be computed and keyed for that app or the gate walks the agents
    directory on the loop after all."""
    import kiro_crew.subagent as subagent_mod
    from kiro_crew.subagent import SubagentManager

    asked: list[str] = []
    monkeypatch.setattr(
        subagent_mod,
        "_validate_app_agent_ownership",
        lambda agent, app: asked.append(app) or "",
    )
    monkeypatch.setattr(subagent_mod, "_validate_agent", lambda a, cwd="": (a, "", ""))
    sessions = MagicMock()
    sessions._pool_cwd = "/pool"
    mgr = SubagentManager(
        sessions=sessions, ctx_builder=MagicMock(), on_done=MagicMock(), max_concurrent=3
    )
    check = asyncio.run(
        mgr._check_agent_off_loop("app__helper", "", app="", execution_context={"app": "the-app"})
    )
    assert check is not None and check[2] == "the-app"
    assert asked == ["the-app"]


def test_the_off_loop_agent_check_keys_on_the_canonical_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A symlinked or ``~``-relative cwd is canonicalized in the worker exactly as
    the gate resolves it, so the gate's key matches and it never re-walks the
    agents directory on the loop."""
    import os

    import kiro_crew.subagent as subagent_mod
    from kiro_crew.subagent import SubagentManager

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    threads: list[bool] = []

    def _validate(agent: str, cwd: str = "") -> tuple[str, str, str]:
        threads.append(threading.current_thread() is threading.main_thread())
        return agent, "", ""

    monkeypatch.setattr(subagent_mod, "_validate_agent", _validate)

    async def run() -> Any:
        sessions = MagicMock()
        sessions.get_agent_selection.return_value = ("template", "")
        mgr = SubagentManager(
            sessions=sessions, ctx_builder=MagicMock(), on_done=MagicMock(), max_concurrent=3
        )
        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel"),
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            mock_cfg.load.return_value.agent.subagent_cwd_allowed_roots = [str(tmp_path)]
            check = await mgr._check_agent_off_loop("reviewer", str(link), prevalidated=False)
            info = await mgr.spawn_async(
                "one", parent_session_key="sess-1", agent="reviewer", cwd=str(link)
            )
        await mgr.cancel_all()
        return check, info

    check, info = asyncio.run(run())
    assert check is not None and check[:2] == ("reviewer", os.path.realpath(real))
    assert info is not None and "cwd" not in (info.error or "")
    assert threads and not any(threads), threads
