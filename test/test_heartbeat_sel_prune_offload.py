"""Regression guards for the heartbeat's SEL-prune tick.

Two things are pinned here.

1. Which thread pool prune runs on.  ``sel().prune()`` does blocking file IO
   (it streams the log to a temp file and ``os.replace``s it), so it must not
   run on the event loop.  It runs on ``maintenance_executor`` -- deliberately,
   even though that pool's charter is "fast periodic sweeps" -- because the
   three pools are split on what makes a worker un-reclaimable rather than on
   duration.  See the comment at the call site in ``heartbeat.py``.  These
   tests assert on the NAME OF THE THREAD prune actually ran on, so an inline
   call (``MainThread``) and a move to another pool (``mc-subproc``,
   ``mc-discovery``) both fail here.

2. That a failed prune is loud.  A silently-dead prune means retention stops,
   which is the unbounded-growth failure prune exists to prevent.  It must log
   at WARNING and bump an alarmable counter, without killing the heartbeat.
"""

from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.heartbeat import HeartbeatService

_PRUNE_COUNTER = "kirocrew.sel.prune_failed.count"


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prune_error: BaseException | None = None,
    consolidator: MagicMock | None = None,
) -> tuple[HeartbeatService, MagicMock, list[str]]:
    """A HeartbeatService parked on a tick where the prune branch fires.

    Returns the service, the fake ``sel()`` singleton, and a list that receives
    the name of the thread ``prune`` ran on.  Only the prune tick
    is exercised: ``_processing`` short-circuits the HEARTBEAT.md branch, and
    memory/config are stubbed so the surviving IO is prune's alone.
    """
    memory = MagicMock()
    memory.rebuild_index.return_value = 0
    svc = HeartbeatService(memory=memory, on_task=None, consolidator=consolidator)
    svc._processing = True
    svc._tick = 0  # 0 % _PRUNE_TICKS == 0 -> prune branch runs

    monkeypatch.setattr(
        hb_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
    )
    ran_on: list[str] = []

    def _prune() -> None:
        # Record where the work landed BEFORE raising, so the failure cases
        # still report their thread.  This list is the positive field the pool
        # assertions read: the real executor names its threads "mc-maint".
        ran_on.append(threading.current_thread().name)
        if prune_error is not None:
            raise prune_error

    fake_sel = MagicMock()
    fake_sel.prune.side_effect = _prune
    monkeypatch.setattr(hb_mod, "sel", lambda: fake_sel)
    return svc, fake_sel, ran_on


class TestSelPrunePool:
    """The pool choice is an exemption; keep it pinned so it is re-decided
    on purpose rather than drifting."""

    @pytest.mark.asyncio
    async def test_prune_runs_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, _, ran_on = _service(monkeypatch)
        loop_thread = threading.current_thread().name

        await svc._beat()

        # Indexing is deliberate: if prune never ran at all this raises rather
        # than passing vacuously.
        assert ran_on[0] != loop_thread

    @pytest.mark.asyncio
    async def test_prune_runs_on_the_maintenance_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, _, ran_on = _service(monkeypatch)

        await svc._beat()

        # "mc-maint" is maintenance_executor's thread_name_prefix; mc-subproc /
        # mc-discovery / mc-cron would each fail this.
        assert ran_on[0].startswith("mc-maint")


class TestSelPruneFailureIsLoud:
    @pytest.mark.asyncio
    async def test_failure_logs_at_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        svc, _, _ran = _service(monkeypatch, prune_error=OSError("no space left on device"))
        monkeypatch.setattr(hb_mod, "get_recorder", lambda: MagicMock())

        with caplog.at_level(logging.WARNING, logger="kiro_crew.heartbeat"):
            await svc._beat()

        assert any(
            rec.levelno == logging.WARNING and "SEL prune failed" in rec.getMessage()
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_failure_increments_alarmable_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc, _, _ran = _service(monkeypatch, prune_error=OSError("no space left on device"))
        recorder = MagicMock()
        monkeypatch.setattr(hb_mod, "get_recorder", lambda: recorder)

        await svc._beat()

        # Core metric names must live under the kirocrew.* namespace -- an
        # off-namespace name is rejected inside counter(), which swallows the
        # error, so the counter would never fire and nothing would say so.
        recorder.counter.assert_called_once_with(_PRUNE_COUNTER)

    @pytest.mark.asyncio
    async def test_failure_does_not_stop_the_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        consolidator = MagicMock()
        svc, _, _ran = _service(
            monkeypatch,
            prune_error=OSError("no space left on device"),
            consolidator=consolidator,
        )
        monkeypatch.setattr(hb_mod, "get_recorder", lambda: MagicMock())

        await svc._beat()

        # Work after the prune branch still ran, so the failure was swallowed
        # rather than propagated out of the cycle.
        consolidator.check_idle_sessions.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_telemetry_fault_does_not_stop_the_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counter is guarded: a broken recorder must not escalate a
        logged prune failure into a dead heartbeat loop."""
        consolidator = MagicMock()
        svc, _, _ran = _service(
            monkeypatch,
            prune_error=OSError("no space left on device"),
            consolidator=consolidator,
        )
        recorder = MagicMock()
        recorder.counter.side_effect = RuntimeError("metrics backend down")
        monkeypatch.setattr(hb_mod, "get_recorder", lambda: recorder)

        await svc._beat()

        consolidator.check_idle_sessions.assert_called_once_with()
