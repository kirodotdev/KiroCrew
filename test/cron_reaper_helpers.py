"""Shared fixtures and fakes for the cron reaper suites.

``test_cron_reaper.py`` and the four files split from it (``test_cron_reaper_failed_kill.py``,
``_kill_set.py``, ``_ending_fence.py`` and ``_process_identity.py``) use these by name:
the autouse pin of the kill seam (every platform kill primitive refuses unless a
test patches it), the session, client and handle fakes, and the reap fixtures the kill
path tests share. ``test_cron_cancel.py`` and ``test_cron_run_claim.py`` import the
kill-seam names from ``test_cron_reaper``, which binds them from here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronJob, CronSchedule, CronService, _RunClaim
from kiro_crew.cron_history import CronHistoryStore
from kiro_crew.process_identity import ProcessHandle, process_handle_of
from kiro_crew.session_lifecycle import TornDown, _TeardownScope
from kiro_crew.testing.wait import finished

#: The platform kill primitives the kill path can reach. Each is pinned to a
#: refusal for the whole module (``_kill_seam``): a test that drives one patches
#: it inside its own block, and an unpinned call surfaces as a loud test failure
#: instead of a real signal to whatever process owns a fabricated pid on the host.
_KILL_PRIMITIVES = (
    "kill_process_group",
    "kill_pid",
    "kill_pid_async",
    "kill_process_tree",
    "kill_process_tree_async",
    "kill_process_tree_pinned",
)


def _refuse_unpinned_signal(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        f"unpinned kill primitive called with {args!r}: patch the seam this test drives"
    )


async def _refuse_unpinned_signal_async(*args: Any, **kwargs: Any) -> Any:
    _refuse_unpinned_signal(*args, **kwargs)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the cron reaper suites' autouse kill-seam pin; none of them calls monkeypatch.undo()
def _kill_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill path is POSIX-shaped here unless a test pins ``IS_WINDOWS`` itself, and no primitive signals for real.

    ``kill_verified_process`` reads ``platform_compat.IS_WINDOWS`` at call time:
    on POSIX the live root's captured group is signalled through
    ``kill_process_group`` (which the POSIX-shaped tests here patch to land or
    raise), on Windows through ``kill_process_tree_pinned``, which a fabricated
    pid can never pin -- so an unpinned test read differently on a Windows runner
    than on Linux. A test of the Windows shape sets ``IS_WINDOWS`` True in its
    own body, after this fixture, and wins. The other platform reads these tests
    make (the pid liveness, the start id, the group capture, the group probe) are
    stubbed per test; every kill primitive is pinned to a refusal here so a
    seam a test forgot to patch cannot reach the host (see ``_KILL_PRIMITIVES``).
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    for name in _KILL_PRIMITIVES:
        guard = (
            _refuse_unpinned_signal_async if name.endswith("_async") else _refuse_unpinned_signal
        )
        monkeypatch.setattr(platform_compat, name, guard)


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    sessions._sessions = {}
    # No teardown in flight: the manager's torn-down table is empty, so a live-map
    # miss is a key with no process (``SessionManager.tearing_down``).
    sessions.tearing_down = MagicMock(return_value=[])
    # A real scope, so a fake reset can report which session it pops the way the
    # real one does (``scope.note_pop``); a fake reset that ignores it pops nothing.
    sessions.teardown_scope = lambda on_pop=None: _TeardownScope({}, on_pop)
    return sessions


def _live_task() -> MagicMock:
    """A tracked task that has not finished (the reaper's deadline path)."""
    return MagicMock(done=MagicMock(return_value=False))


def _make_job(job_id: str = "job1", name: str = "test job") -> CronJob:
    return CronJob(
        id=job_id,
        name=name,
        message="do something",
        schedule=CronSchedule(kind="every", every_secs=300),
        created_ts=time.time(),
    )


# The start id the fake client records at spawn (``platform_compat.get_process_start_id``
# reads ``/proc/<pid>/stat`` field 22 on Linux); the kill re-reads it before signalling.
_START_ID = "4821903"


def _session_with_pid(svc: CronService, session_key: str, pid: int | None) -> MagicMock:
    """Register a session under ``session_key`` whose ACP client reports ``pid``.

    The client carries the start id the reaper's recycled-pid check compares, so
    a test whose start-id read answers the same value drives ``_sigkill_session``
    all the way to the group kill.
    """
    client = MagicMock()
    client._pid = pid
    client._child_pids = {}
    client._start_time = _START_ID
    session = MagicMock()
    session.provider._client = client
    svc._sessions._sessions[session_key] = session
    return client


def _handle_of(svc: CronService, session_key: str) -> ProcessHandle:
    """The kill handle ``_force_reap`` / ``cancel`` take before the reset and hand to the kill."""
    handles = svc._session_process_handles(session_key)
    assert handles, f"no session registered under {session_key}"
    return handles[0]


def _torn_down_by_the_run(svc: CronService, session_key: str) -> MagicMock:
    """Move the session under ``session_key`` from the live map into the torn-down table.

    The shape of a run whose OWN teardown reset ran first: the session is out of
    the live map (the pop happens under the registry lock, before the awaits that
    can hang), and the session manager retains it -- with the process handle read
    at the pop, as the real table does -- for exactly the life of that teardown,
    where ``_session_process_handles`` reads it on a live-map miss.
    """
    session = svc._sessions._sessions.pop(session_key)
    _retain_torn_down(svc, session_key, session)
    return session


def _retain_torn_down(svc: CronService, session_key: str, *sessions: Any) -> None:
    """Have the manager double's ``tearing_down`` answer *sessions* under ``session_key``, in order.

    Each entry is the real :class:`TornDown` record -- the session and the handle
    read off it NOW, at the moment the table would have captured it -- so a test
    that clears the session's pid afterwards models exactly the hung teardown
    whose own awaits cleared it. Captured under :func:`_root_reads_back`, as the
    pop would have read it with the leader alive and leading its own group.
    """
    with _root_reads_back():
        entries = [TornDown(session, process_handle_of(session)) for session in sessions]
    svc._sessions.tearing_down = MagicMock(
        side_effect=lambda key: list(entries) if key == session_key else []
    )


def _group_gone() -> Any:
    """The leader's process group is empty: the signal-0 probe of it finds no member.

    Pinned wherever a test walks a gone or recycled leader: the probe is real
    ``os.killpg(number, 0)`` otherwise, and a made-up pid may name a live group
    on the host running the suite.
    """
    return patch("kiro_crew.platform_compat.pgroup_exists", return_value=False)


@contextmanager
def _isolated_leader() -> Iterator[None]:
    """The fabricated root leads its own POSIX group: ``getpgid(pid) == pid``, and our own group is 1.

    Both names are CREATED on a runner that lacks them (Windows has neither), so
    the capture site ``isolated_group_of`` -- which reads them through ``getattr``
    -- captures the group on every platform the POSIX-shaped tests run on.
    """
    with (
        patch("os.getpgid", side_effect=lambda pid: pid, create=True),
        patch("os.getpgrp", return_value=1, create=True),
    ):
        yield


@contextmanager
def _root_reads_back(start_id: str = _START_ID) -> Iterator[None]:
    """The root's start id reads back as ``start_id``, the platform reports the pid alive, it leads its own group, its group probe is empty.

    All four reads are pinned: ``process_survived`` asks the platform for
    liveness after the identity check (Windows reads a creation time back for an
    exited child whose handle is still held) and asks a gone leader's group
    before calling it gone, and an unpinned ``pid_exists`` / ``pgroup_exists`` on
    a made-up pid answers whatever the host happens to run -- not the same on
    every runner. ``os.getpgid`` answers the pid itself (:func:`_isolated_leader`),
    so the snapshot captures the isolated group a ``start_new_session`` root leads
    (``ProcessHandle.pgid == pid``): the kill then addresses THAT captured id
    through ``kill_process_group``, which the test pins, and never a group
    resolved from the pid at signal time. Restored on exit, so a test wanting no
    captured group patches ``os.getpgid`` itself inside the block.
    """
    with (
        patch("kiro_crew.platform_compat.get_process_start_id", return_value=start_id),
        patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        _isolated_leader(),
        _group_gone(),
    ):
        yield


def _kill_path_stubs() -> Any:
    """The child-tree probe, the root reads and the sweep stubbed so only the kill decides."""
    return (
        patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        _root_reads_back(),
        patch("kiro_crew.acp.client._kill_escaped_children"),
    )


def _overdue_reap_fixture(tmp_path: object, job_id: str) -> tuple[CronService, CronJob, _RunClaim]:
    """A service whose session reset fails, so ``_force_reap`` reaches the SIGKILL."""
    svc = CronService(base_dir=None, on_job=AsyncMock())
    svc._history = CronHistoryStore(base_dir=tmp_path)
    svc._sessions = _mock_sessions()
    svc._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
    job = _make_job(job_id)
    svc._jobs = [job]
    claim = svc._claims[job_id] = _RunClaim(
        trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
    )
    return svc, job, claim


#: Lost-run ceiling on a state a test has just released ON THE LOOP: a refused
#: start's reservation cleanup, a held claim reaching the fence door, a claim
#: reaching a busy parent's turn. Each is a few loop steps and at most one worker
#: hop, milliseconds; only a wedged allocation path reaches the ceiling, which is
#: a sixth of the suite's 120 s ``--timeout``, so it fails by name.
_FENCE_SETTLE_CEILING_SECS = 20.0


async def _within_the_ceiling(aw: Any, what: str) -> Any:
    """Await *aw* under ``_FENCE_SETTLE_CEILING_SECS``; past it raise naming ``what``."""
    return await finished(aw, what, timeout=_FENCE_SETTLE_CEILING_SECS)


def _parks_at_the_fence_door(mgr: Any) -> tuple[asyncio.Event, Any]:
    """An event set when a caller parks at ``mgr``'s ending-fence door, and the patch that sets it."""
    boundary = mgr._allocation_boundary()
    real_wait = boundary.wait_for_ending_fence
    parked = asyncio.Event()

    async def _wait(key: str, deadline: float | None) -> float | None:
        if key in boundary.state.ending_keys:
            parked.set()
        return await real_wait(key, deadline)

    return parked, patch.object(boundary, "wait_for_ending_fence", _wait)


async def _held_or_landed(parked: asyncio.Event, claim: asyncio.Task[Any]) -> str:
    """Wait until the claim parks at the fence door or lands, and name which.

    Returns rather than raises: the reset and history-append seams it runs in
    swallow exceptions, so the caller's own assertion names the outcome.
    """
    parking = asyncio.ensure_future(parked.wait())
    try:
        done, _ = await asyncio.wait(
            {parking, claim},
            timeout=_FENCE_SETTLE_CEILING_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        parking.cancel()
    if not done:
        return f"neither held nor landed after {_FENCE_SETTLE_CEILING_SECS:.0f}s"
    return "held" if parked.is_set() and not claim.done() else "landed before the record"


def _audited_outcome(mock_sel: MagicMock) -> str:
    return mock_sel().log_tool_invocation.call_args.kwargs["outcome"]


def _start_id_reads(table: dict[int, Any]) -> Any:
    """A per-pid ``get_process_start_id`` side effect.

    A list is consumed in order and its last value then repeats; anything else
    is that pid's constant answer; an unlisted pid reads ``None`` (gone).
    """
    state = {
        pid: (list(value) if isinstance(value, list) else value) for pid, value in table.items()
    }

    def _read(pid: int) -> str | None:
        value = state.get(pid)
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    return _read
