"""Shared fixtures and store helpers for the ``taskq.adapters.runner`` tests.

Holds the ``clock`` and ``store`` fixtures (a fake clock and a real SQLite store in
``tmp_path``), ``_Sleeps`` (records requested sleeps and advances the fake clock instead
of waiting), ``_admission`` (a ``RunnerAdmission`` over one lane with that fake sleep),
``_RUNNER_LOGGER`` (the adapter's logger name) and ``_locked_store`` with its
``_LockedAfterClaim`` store (a real store whose file another connection locks the instant
a claim commits). ``test_taskq_runner_adapter.py`` and
``test_taskq_runner_adapter_durability.py`` import what they use from here by name.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from overload_fakes import Clock, open_task_store

from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.store import TaskStore

_RUNNER_LOGGER = "kiro_crew.taskq.adapters.runner"


class _Sleeps:
    """Records requested sleeps; advances the fake clock instead of waiting."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, secs: float) -> None:
        self.calls.append(secs)
        self.clock.advance(secs)
        # flake-ok: the fake sleep yields one loop iteration instead of waiting
        await asyncio.sleep(0)


@pytest.fixture(name="clock")
def clock_fixture() -> Clock:
    return Clock()


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(tmp_path, clock)


def _admission(store: TaskStore | None, clock: Clock, **kw) -> tuple[r.RunnerAdmission, _Sleeps]:
    sleeps = _Sleeps(clock)
    lane = kw.pop("lane", None) or r.RunnerLane(kw.pop("cap", 1), mode=kw.pop("mode", r.MODE_AIMD))
    adm = r.RunnerAdmission(store, lane=lane, clock=clock, sleep=sleeps, **kw)
    return adm, sleeps


# ── the claim/start boundary: nothing executes under an ``admitted`` row ──────


class _LockedAfterClaim(TaskStore):
    """A REAL store whose FILE another connection locks the instant a claim
    commits, so the very next write gets SQLite's own ``database is locked``.

    That is the shape of a lock the store's ``busy_timeout`` gives up on: a
    competing writer arrives between the claim and the start write.
    ``clears_after_one_refusal`` is the transient case (the writer is gone again
    by the next statement); False keeps the file locked for the whole test, which
    is the outage case.
    """

    def __init__(self, *args, clears_after_one_refusal: bool = True, **kw) -> None:
        super().__init__(*args, **kw)
        self._blocker = None
        self._clears = bool(clears_after_one_refusal)

    def claim(self, task_id, *, owner=None, lease_secs=None):
        claimed = super().claim(task_id, owner=owner, lease_secs=lease_secs)
        if claimed is not None and self._blocker is None:
            # ``claim`` runs on the store's writer thread and ``close`` on the
            # test's, so the blocker is not pinned to either.
            self._blocker = sqlite3.connect(str(self.path), timeout=0, check_same_thread=False)
            self._blocker.execute("BEGIN EXCLUSIVE")
        return claimed

    def transition(self, *args, **kw):
        try:
            return super().transition(*args, **kw)
        finally:
            if self._clears:
                self.unlock()

    def unlock(self) -> None:
        blocker, self._blocker = self._blocker, None
        if blocker is not None:
            blocker.execute("ROLLBACK")
            blocker.close()

    def close(self) -> None:
        self.unlock()
        super().close()


def _locked_store(tmp_path: Path, clock: Clock, **kw) -> "_LockedAfterClaim":
    return _LockedAfterClaim(
        tmp_path / "tasks.db",
        window=8,
        clock=clock,
        network_fs=False,
        busy_timeout_secs=0.05,
        **kw,
    ).open()
