"""A tombstone whose write fails (a full disk) is written once space returns.

A run's tombstone is what keeps its folder out of the next boot's orphan
reconciliation, which announces every untombstoned run to its parent. When the
write fails, the run is recorded for retry; the reaper retries off the event loop
each tick and graceful shutdown once more. Every test keeps its state under
tmp_path; the reconciler's notifiers are recorders and its PID probe says dead, so
nothing is signalled or sent, and no prune runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import shutil
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.subagent as sa
from kiro_crew import subagent_persistence as sp
from kiro_crew.subagent import SubagentManager

PARENT = "dashboard:chat-7"


@pytest.fixture(autouse=True)
def _pinned_paths(tmp_path, _floor_monkeypatch):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        path = tmp_path / sub
        path.mkdir()
        _floor_monkeypatch.setenv(var, str(path))


@pytest.fixture(autouse=True)
def _managers(_no_owed_tombstones, close_subagent_managers):
    """Managers built here are closed at teardown (``conftest``), then owed entries clear."""


@pytest.fixture(autouse=True)
def _no_owed_tombstones():
    owed = getattr(sp, "_owed_tombstones", None)
    if owed is not None:
        owed.clear()
    yield
    if owed is not None:
        owed.clear()


class _FullDisk:
    """Fail every ``tombstone.json`` write with ENOSPC while installed."""

    def __init__(self, monkeypatch) -> None:
        self._monkeypatch = monkeypatch
        self._real = sp._atomic_write
        self.failed: list[str] = []
        monkeypatch.setattr(sp, "_atomic_write", self._write)
        if hasattr(sp, "_create_exclusive"):
            self._real_exclusive = sp._create_exclusive
            monkeypatch.setattr(sp, "_create_exclusive", self._exclusive)

    def _write(self, path, data):  # type: ignore[no-untyped-def]
        if path.name == "tombstone.json":
            self.failed.append(path.parent.name)
            raise OSError(errno.ENOSPC, "No space left on device")
        return self._real(path, data)

    def _exclusive(self, path, data):  # type: ignore[no-untyped-def]
        if path.name == "tombstone.json":
            self.failed.append(path.parent.name)
            raise OSError(errno.ENOSPC, "No space left on device")
        return self._real_exclusive(path, data)

    def space_returns(self) -> None:
        self._monkeypatch.setattr(sp, "_atomic_write", self._real)
        if hasattr(self, "_real_exclusive"):
            self._monkeypatch.setattr(sp, "_create_exclusive", self._real_exclusive)


def _finished_run(agent_id: str) -> None:
    sp.create_agent_folder(agent_id, task="summarise the logs", parent_session=PARENT)
    assert sp.write_finished_result(agent_id, "the whole answer", sp.update_state)


def _manager() -> SubagentManager:
    return SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), on_done=AsyncMock())


async def _boot_announcements(monkeypatch) -> list[tuple[str, str]]:
    """Run a fresh gateway's orphan reconciliation; return what it announced."""
    manager = _manager()
    injected: list[tuple[str, str]] = []

    async def _inject(parent_session, msg, meta=None):  # type: ignore[no-untyped-def]
        injected.append((parent_session, msg))
        return True

    monkeypatch.setattr(manager, "_try_inject_orphan_notification", _inject)
    monkeypatch.setattr(manager, "_send_orphan_slack_dm", AsyncMock())
    monkeypatch.setattr(manager, "_is_pid_alive", lambda _pid: False)
    await manager._reconcile_orphans()
    return injected


@contextlib.contextmanager
def _quiet_reaper(manager: SubagentManager):  # type: ignore[no-untyped-def]
    """Record everything the reaper loop does but the owed-tombstone retry; yield its pump."""
    pump = MagicMock()
    with (
        patch.object(sa, "_REAPER_INTERVAL", 0),
        patch.object(sa, "compact_cost_log"),
        patch.object(sa, "prune_stale_tombstones", MagicMock(return_value=0)),
        patch.object(manager, "_sample_live_costs"),
        patch.object(manager, "_rebuild_conversation_registry", new=AsyncMock()),
        patch.object(manager, "_sweep_stuck_waves_async", new=AsyncMock()),
        patch.object(manager, "_sweep_digest_holds_async", new=AsyncMock()),
        patch.object(manager, "_sweep_conversations_async", new=AsyncMock()),
        patch.object(manager, "retry_owed_teardown_sweeps", new=AsyncMock()),
        patch.object(manager, "_taskq_pump", pump),
        patch.object(type(manager._admission), "taskq_schedule_owed_replay", MagicMock()),
        patch.object(manager, "_force_reap", AsyncMock()),
    ):
        yield pump


async def _one_reaper_tick(manager: SubagentManager) -> None:
    """Run the real reaper loop for one whole tick with everything else recorded."""
    with _quiet_reaper(manager) as pump:
        task = asyncio.ensure_future(manager._reaper_loop())
        for _ in range(3000):  # a whole tick has run once the pump runs twice
            if pump.call_count >= 2 or task.done():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert pump.call_count >= 2, "the reaper loop did not complete a tick"


@pytest.mark.asyncio
async def test_a_delivered_run_is_not_reported_again_once_the_reaper_writes_its_tombstone(
    monkeypatch,
):
    _finished_run("del0001")
    disk = _FullDisk(monkeypatch)
    sp.mark_delivered("del0001", elapsed=1.0, credits=0.0)  # the result reached its parent
    assert disk.failed == ["del0001"]
    disk.space_returns()
    await _one_reaper_tick(_manager())
    assert await _boot_announcements(monkeypatch) == [], (
        "a run whose result already reached its parent was reported again at the next "
        "boot, although space came back before the restart"
    )


@pytest.mark.asyncio
async def test_a_reaper_retry_running_at_shutdown_stops_before_its_next_write(monkeypatch):
    """A retry the reaper began before shutdown writes nothing after the write it is in.

    The drain clears the tombstone of a run it re-admits to orphan recovery, after
    the shutdown retry. A later write by the reaper's retry would mark that run
    ended, and the next start would not recover it.
    """
    for agent_id in ("slow001", "strag01"):
        _finished_run(agent_id)
    disk = _FullDisk(monkeypatch)
    for agent_id in ("slow001", "strag01"):
        sp.mark_delivered(agent_id, elapsed=1.0, credits=0.0)
    assert disk.failed == ["slow001", "strag01"]
    disk.space_returns()
    entered, gate = threading.Event(), threading.Event()
    real_exclusive, stalled = sp._create_exclusive, []

    def _stall_first_write(path, data):  # type: ignore[no-untyped-def]
        if path.parent.name == "slow001" and not stalled:
            stalled.append(path)
            entered.set()
            gate.wait(10)
        return real_exclusive(path, data)

    monkeypatch.setattr(sp, "_create_exclusive", _stall_first_write)
    real_retry, returned = sp.retry_owed_tombstones, []

    def _tracked_retry(*args, **kwargs):  # type: ignore[no-untyped-def]
        done = threading.Event()
        returned.append(done)
        try:
            return real_retry(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(sp, "retry_owed_tombstones", _tracked_retry)
    monkeypatch.setattr(sa, "_STATE_DRAIN_TIMEOUT", 0.2)
    manager = _manager()
    with _quiet_reaper(manager):
        reaper = asyncio.ensure_future(manager._reaper_loop())
        try:
            assert await asyncio.to_thread(entered.wait, 5), "the reaper never began its retry"
            await asyncio.wait_for(manager.cancel_all(), 10)
            sp.clear_tombstone("strag01")  # the drain re-admits strag01 to orphan recovery
        finally:
            gate.set()
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
    assert await asyncio.to_thread(returned[0].wait, 10), "the reaper's retry never returned"
    assert not (
        sp._agent_dir("strag01") / "tombstone.json"
    ).exists(), "a retry that began before shutdown wrote a tombstone after the shutdown cleared it"


@pytest.mark.asyncio
async def test_graceful_shutdown_writes_an_owed_delivered_tombstone(monkeypatch):
    _finished_run("del0002")
    disk = _FullDisk(monkeypatch)
    sp.mark_delivered("del0002", elapsed=1.0, credits=0.0)
    disk.space_returns()
    await _manager().cancel_all()
    assert (
        await _boot_announcements(monkeypatch) == []
    ), "the shutdown did not write the delivered tombstone it owed"


@pytest.mark.asyncio
async def test_a_boot_tombstone_that_failed_is_retried_so_the_next_boot_is_quiet(monkeypatch):
    _finished_run("orph001")  # never delivered: the first boot must announce it
    disk = _FullDisk(monkeypatch)
    first = await _boot_announcements(monkeypatch)
    assert [p for p, _ in first] == [PARENT]
    disk.space_returns()
    await _one_reaper_tick(_manager())
    assert await _boot_announcements(monkeypatch) == [], (
        "a run announced at one boot was announced again at the next, because the "
        "boot's own tombstone write had failed"
    )


@pytest.mark.asyncio
async def test_an_undelivered_orphan_is_announced_exactly_once(monkeypatch):
    _finished_run("orph002")
    first = await _boot_announcements(monkeypatch)
    second = await _boot_announcements(monkeypatch)
    assert [p for p, _ in first] == [PARENT]
    assert second == []


def test_a_retry_past_its_budget_leaves_its_entries_owed(monkeypatch):
    _finished_run("late001")
    disk = _FullDisk(monkeypatch)
    sp.mark_delivered("late001", elapsed=1.0, credits=0.0)
    disk.space_returns()
    assert sp.retry_owed_tombstones(budget=0) == 0
    assert sp.has_owed_tombstones(), "a retry past its deadline dropped an entry it never wrote"
    assert not (sp._agent_dir("late001") / "tombstone.json").exists()


def test_a_write_that_lands_owes_nothing():
    _finished_run("ok00001")
    sp.mark_delivered("ok00001", elapsed=1.0, credits=0.0)
    assert sp.read_tombstone("ok00001")["cause"] == "delivered"
    assert not sp.has_owed_tombstones()


def test_a_retry_never_recreates_a_removed_run_folder(monkeypatch):
    _finished_run("gone001")
    disk = _FullDisk(monkeypatch)
    sp.mark_delivered("gone001", elapsed=1.0, credits=0.0)
    disk.space_returns()
    folder = sp._agent_dir("gone001")
    shutil.rmtree(folder)
    assert sp.retry_owed_tombstones() == 0
    assert not folder.exists()
    assert not sp.has_owed_tombstones()


def test_a_retry_never_replaces_a_tombstone_written_since(monkeypatch):
    _finished_run("newr001")
    disk = _FullDisk(monkeypatch)
    sp.mark_delivered("newr001", elapsed=1.0, credits=0.0)
    disk.space_returns()
    sp.write_tombstone("newr001", cause="reaped", recovery_action="none")
    assert sp.retry_owed_tombstones() == 0
    assert sp.read_tombstone("newr001")["cause"] == "reaped"
    assert not sp.has_owed_tombstones()


def test_the_retry_keeps_the_original_fields_and_moment(monkeypatch):
    _finished_run("keep001")
    disk = _FullDisk(monkeypatch)
    sp.mark_delivered("keep001", elapsed=3.5, credits=0.25)
    died = sp._owed_tombstones["keep001"]["extra"]["died"]
    disk.space_returns()
    assert sp.retry_owed_tombstones() == 1
    tomb = json.loads((sp._agent_dir("keep001") / "tombstone.json").read_text(encoding="utf-8"))
    assert (tomb["cause"], tomb["elapsed"], tomb["credits"], tomb["died"]) == (
        "delivered",
        3.5,
        0.25,
        died,
    )
    assert not sp.has_owed_tombstones()


def test_a_retry_that_fails_again_keeps_the_entry_without_a_warning(monkeypatch, caplog):
    _finished_run("full001")
    _FullDisk(monkeypatch)
    sp.mark_delivered("full001", elapsed=1.0, credits=0.0)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=sp.logger.name):
        assert sp.retry_owed_tombstones() == 0
        assert sp.retry_owed_tombstones() == 0
    assert sp.has_owed_tombstones()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_past_the_cap_a_failed_write_is_not_retried_and_stays_an_orphan(monkeypatch):
    monkeypatch.setattr(sp, "_OWED_TOMBSTONES_MAX", 1)
    _finished_run("cap0001")
    _finished_run("cap0002")
    disk = _FullDisk(monkeypatch)
    sp.mark_delivered("cap0001", elapsed=1.0, credits=0.0)
    sp.mark_delivered("cap0002", elapsed=1.0, credits=0.0)
    assert list(sp._owed_tombstones) == ["cap0001"]
    disk.space_returns()
    assert sp.retry_owed_tombstones() == 1
    assert [o.get("id") for o in sp.list_orphans()] == ["cap0002"]


def test_an_oversized_write_is_not_stored_for_a_run_already_owed(monkeypatch):
    # The size bound applies on every write, including one that replaces the
    # record of a run that already failed once. The replaced record is not
    # retried either: the folder stays an orphan, reported again at the next
    # start, never lost.
    _finished_run("big0001")
    disk = _FullDisk(monkeypatch)
    sp.mark_delivered("big0001", elapsed=1.0, credits=0.0)
    assert "big0001" in sp._owed_tombstones
    sp.write_tombstone(
        "big0001",
        cause="reaped",
        recovery_action="none",
        note="x" * (sp._OWED_TOMBSTONE_MAX_BYTES + 1),
    )
    stored = sp._owed_tombstones.get("big0001")
    assert stored is None or (
        len(json.dumps(stored, ensure_ascii=False, default=str)) <= sp._OWED_TOMBSTONE_MAX_BYTES
    ), "a record larger than the per-entry bound was stored for a run already owed"
    disk.space_returns()
    sp.retry_owed_tombstones()
    assert [o.get("id") for o in sp.list_orphans()] == ["big0001"]


class _Killed(BaseException):
    """The process dies here: nothing after this point runs, cleanup included."""


class _OsKilledAtLink:
    """The module's ``os``, dying at the link that publishes the tombstone."""

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    @staticmethod
    def link(_src: object, _dst: object) -> None:
        raise _Killed

    @staticmethod
    def unlink(_path: object) -> None:
        """A process that died removes nothing."""


def test_a_retry_killed_mid_write_leaves_a_temp_file_the_next_retry_does_not_trip_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next retry writes the tombstone instead of reading the leftover temp file
    as a tombstone written since, which would drop the entry with nothing on disk."""
    agent_id = "killed01"
    _finished_run(agent_id)
    disk = _FullDisk(monkeypatch)
    sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
    assert disk.failed == [agent_id] and sp.has_owed_tombstones()
    disk.space_returns()
    folder = sp._agent_dir(agent_id)
    with monkeypatch.context() as m:
        m.setattr(sp, "os", _OsKilledAtLink())
        with pytest.raises(_Killed):
            sp.retry_owed_tombstones()
    assert [p for p in folder.iterdir() if p.name.endswith(".owed")], "no temp file was left"
    assert not (folder / "tombstone.json").exists()

    assert sp.retry_owed_tombstones() == 1
    assert (folder / "tombstone.json").exists()
    assert not sp.has_owed_tombstones()
