"""Tests for posture-gated admission control.

Covers :func:`kiro_crew.resource_status.admission_check` (critical refuses;
ample/tight/unknown admit; off-switch; fail-open), the cron scheduler's
critical-posture deferral in ``_on_timer`` (deferred jobs are not marked
failed, fire on recovery, one INFO per episode; manual triggers are never
deferred), the subagent spawn refusal (typed SEL outcome + retry-later error),
and the ``agent.admission_gate`` config key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import resource_status as rs
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.cron import CronService


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
    return rs.AdmissionDecision(
        admitted=True, posture=rs.POSTURE_AMPLE, available_gb=16.0
    )


async def _wait_for(predicate, timeout=5.0, interval=0.05):
    """Poll until predicate is true or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("Timed out waiting for predicate")
        await asyncio.sleep(interval)


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

    def test_off_switch_admits_even_when_critical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
        decision = rs.admission_check(_cfg(gate=False))
        assert decision.admitted is True
        # The posture is still reported truthfully — only enforcement is off.
        assert decision.posture == rs.POSTURE_CRITICAL

    def test_fail_open_on_probe_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(cfg: object | None = None) -> rs.ResourceStatus:
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(rs, "probe", _boom)
        decision = rs.admission_check(_cfg())
        assert decision.admitted is True
        assert decision.posture == rs.POSTURE_UNKNOWN

    def test_fail_open_on_config_load_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_non_bool_gate_value_defaults_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        assert job.id not in svc._running_tasks
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "deferring" in r.getMessage()
        ]
        assert len(infos) == 1  # one INFO per episode, not per tick

        # Recovery: the same job fires on the next admitted tick.
        with patch("kiro_crew.cron.admission_check", return_value=_admitted()):
            await svc._on_timer()
        await _wait_for(lambda: "gated" in executed)

        # A NEW critical episode logs its own INFO line.
        job.last_run_ts = time.time() - 120
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="kiro_crew.cron"):
            with patch("kiro_crew.cron.admission_check", return_value=_refused()):
                await svc._on_timer()
        assert any(
            "deferring" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO
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


class TestSpawnAdmissionGate:
    def _mgr(self):
        from kiro_crew.subagent import SubagentManager

        return SubagentManager(
            sessions=MagicMock(),
            ctx_builder=MagicMock(),
            on_done=MagicMock(),
            max_concurrent=3,
        )

    def test_spawn_refused_when_critical(self) -> None:
        """spawn() returns a done SubagentInfo with a retry-later error."""
        mgr = self._mgr()
        with patch(
            "kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)
        ), patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg, patch(
            "kiro_crew.subagent.cached_admission_check", return_value=_refused()
        ), patch(
            "kiro_crew.subagent.sel"
        ) as mock_sel:
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_sel.return_value.log_tool_invocation = MagicMock()

            info = mgr.spawn(task="test task", parent_session_key="sess-1")

        assert info is not None
        assert info.done is True
        assert "critical" in info.error
        assert "retry" in info.error
        call_kwargs = mock_sel.return_value.log_tool_invocation.call_args[1]
        assert call_kwargs["outcome"] == "refused_memory_critical"
        assert call_kwargs["metadata"]["posture"] == rs.POSTURE_CRITICAL

    def test_spawn_proceeds_past_gate_when_admitted(self) -> None:
        """An admitted decision falls through to the next guard (cwd here)."""
        mgr = self._mgr()
        with patch(
            "kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)
        ), patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg, patch(
            "kiro_crew.subagent.cached_admission_check", return_value=_admitted()
        ), patch(
            "kiro_crew.subagent.validate_cwd", return_value=("", "not allowed")
        ), patch(
            "kiro_crew.subagent.sel"
        ) as mock_sel:
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cwd_allowed_roots = []
            mock_sel.return_value.log_tool_invocation = MagicMock()

            info = mgr.spawn(task="test task", parent_session_key="sess-1", cwd="/x")

        assert info is not None
        assert info.done is True
        call_kwargs = mock_sel.return_value.log_tool_invocation.call_args[1]
        assert call_kwargs["outcome"] == "rejected_invalid_cwd"


# ── config key ───────────────────────────────────────────────────────────────


def _load_from_dict(data: dict, tmp_path: Path) -> KiroCrewConfig:
    """Write *data* to a config file under *tmp_path* and load it."""
    tmp = tmp_path / "config.json"
    tmp.write_text(json.dumps(data))
    with unittest.mock.patch(
        "kiro_crew.config.loader.config_path", return_value=tmp
    ):
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
            for _ in range(200):  # refresh thread publishes shortly after
                if rs._cached_decision is not None:
                    break
                time.sleep(0.01)
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
        try:
            assert rs.cached_admission_check() is verdict
            time.sleep(0.05)
            assert probes == []  # fresh cache => no background refresh either
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

        def claiming_check(cfg: object | None = None):
            svc._executing.add(job.id)  # simulate a manual run claiming it
            return _admitted()

        with patch("kiro_crew.cron.admission_check", side_effect=claiming_check):
            await svc._on_timer()
        assert executed == []  # revalidated away, no duplicate
        svc._executing.discard(job.id)
        await svc.stop()

    @pytest.mark.asyncio
    async def test_manual_run_completed_during_await_is_not_double_fired(
        self, tmp_path: Path
    ) -> None:
        # Harder variant: the manual run starts AND FINISHES during the
        # admission await, so the job is no longer in _executing. An id-only
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
        await asyncio.sleep(0.05)
        assert executed == []  # not re-fired against the stale snapshot
        await svc.stop()

    @pytest.mark.asyncio
    async def test_job_edited_during_await_dispatches_live_object(
        self, tmp_path: Path
    ) -> None:
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
    async def test_cron_expr_job_runs_normally_under_critical(
        self, tmp_path: Path
    ) -> None:
        # A cron-expression job whose minute matches during a critical
        # episode fires anyway — the occurrence is neither dropped nor
        # remembered in state that a restart would lose.
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("expr-job", "msg", cron_expr="* * * * *")

        with (
            patch("kiro_crew.cron.admission_check", return_value=_refused()),
            patch("kiro_crew.cron.cron_expr_matches", return_value=True),
        ):
            await svc._on_timer()
        await _wait_for(lambda: "expr-job" in executed)
        await svc.stop()

    @pytest.mark.asyncio
    async def test_mixed_due_defers_interval_but_fires_expr(
        self, tmp_path: Path
    ) -> None:
        # One tick, both kinds due, critical posture: the interval job is
        # deferred (stays due, untouched), the cron-expression job fires.
        executed: list[str] = []

        async def callback(job) -> None:
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("interval", "msg", every_secs=60)
        svc.add_job("expr", "msg", cron_expr="* * * * *")
        interval_job = next(j for j in svc._jobs if j.name == "interval")
        interval_job.last_run_ts = time.time() - 120

        with (
            patch("kiro_crew.cron.admission_check", return_value=_refused()),
            patch("kiro_crew.cron.cron_expr_matches", return_value=True),
        ):
            await svc._on_timer()
        await _wait_for(lambda: "expr" in executed)
        assert executed == ["expr"]  # interval deferred, not fired
        assert interval_job.last_status is None  # untouched: still due

        # Recovery: the deferred interval job fires on its own.
        with patch("kiro_crew.cron.admission_check", return_value=_admitted()):
            await svc._on_timer()
        await _wait_for(lambda: "interval" in executed)
        await svc.stop()

    @pytest.mark.asyncio
    async def test_deferral_episode_floors_timer_delay(
        self, tmp_path: Path
    ) -> None:
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
    async def test_interval_edited_to_cron_during_await_still_fires(
        self, tmp_path: Path
    ) -> None:
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
        # A queued single spawn rejected at drain time (here: by the admission
        # gate) must produce EXACTLY ONE completion announcement. The drain
        # loop announces it off the returned info; spawn's own
        # _announce_rejection must stay batch-only, or the requester gets a
        # duplicate completion injection and wave/orchestration counters
        # double-count the failure. Exercises the REAL spawn path (no stubs)
        # so both potential announce sites are live.
        from kiro_crew.subagent import SubagentManager

        announced: list = []

        async def _on_done(info) -> None:
            announced.append(info)

        mgr = SubagentManager(
            sessions=MagicMock(),
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

        with patch(
            "kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)
        ), patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg, patch(
            "kiro_crew.subagent.cached_admission_check", return_value=_refused()
        ), patch(
            "kiro_crew.subagent.sel"
        ) as mock_sel:
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mgr._drain_queue()
            # Flush every announce coroutine scheduled via ensure_future —
            # a duplicate would surface as a second on_done call here.
            for _ in range(5):
                await asyncio.sleep(0)

        assert [i.id for i in announced] == ["q1"], (
            f"expected exactly one announcement, got {len(announced)}"
        )
        assert "critical" in announced[0].error

    @pytest.mark.asyncio
    async def test_interval_job_not_replayed_after_manual_run(
        self, tmp_path: Path
    ) -> None:
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
        await asyncio.sleep(0.1)
        assert executed == ["interval"]  # no replay
        await svc.stop()

    def test_refresh_thread_start_failure_fails_open(self, monkeypatch) -> None:
        rs._cached_decision = None
        rs._cached_at = 0.0
        monkeypatch.setattr(
            rs.threading,
            "Thread",
            MagicMock(side_effect=RuntimeError("can't start new thread")),
        )
        verdict = rs.cached_admission_check()  # must not raise
        assert verdict.admitted  # fail-open
        # The refresh lock was released, not leaked:
        assert rs._cache_refresh_inflight.acquire(blocking=False)
        rs._cache_refresh_inflight.release()
