"""Doubles and builders shared by the runtime reconciler's test modules.

The reconciler factory and its refusal reader, the ``SessionCleanup`` harness the sweep
tests drive, a lease holder, the ``agent_root`` fixture (declared
``@pytest.fixture(name=...)`` and imported under its function name), and the
fabricated cgroup member pids.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import runtime_ownership as ro
from kiro_crew import runtime_reconcile as rr

#: Cgroup members the scope-reaper tests fabricate, above 2**32 so no platform can
#: allocate one. ``session_scope_reap._reclaim_scope`` drops ``os.getpid()`` from a
#: scope's members and its signal ladder skips it again, so a literal the OS can
#: issue leaves the scope whenever it is this xdist worker's own pid: Windows
#: runners issued 7272 and 8484 to workers that ran these tests. One pair per test,
#: so a barrier or claim one test strands cannot reach another.
_MEMBER_A, _MEMBER_B = 99_999_999_971, 99_999_999_972
_UNCLAIMED, _TENANTED = 99_999_999_973, 99_999_999_974
_EARLY, _LATE = 99_999_999_975, 99_999_999_976
_CLEARED_A, _CLEARED_B = 99_999_999_977, 99_999_999_978
_PLAIN_A, _PLAIN_B = 99_999_999_979, 99_999_999_980


def _reconciler(
    *,
    kernel: set[int],
    recorded: set[int],
    alive: set[int] | None = None,
    recycled: set[int] | None = None,
    ours: set[int] | None = None,
    managed: set[int] | None = None,
    leased: set[int] | None = None,
    authorize: bool = True,
    age: float = 10_000.0,
    # The shipped budget, which is what these tests exercise: the conditions under
    # test only mean something when the arm is allowed to signal. The observe-only
    # behaviour an operator selects by setting 0 has its own tests, which pass 0
    # explicitly.
    max_kills: int = rr.DEFAULT_MAX_KILLS,
    killed: list[int] | None = None,
    forgotten: list[int] | None = None,
    notified: list[int] | None = None,
    audited: list[tuple[int, str, str]] | None = None,
) -> rr.RuntimeReconciler:
    """A reconciler over a fake kernel: no real processes, no real signals.

    The identity seam is faked too, and must be. Left at its default it reads the
    HOST for these invented pid numbers, so whether a test passes depends on
    whether the host happens to have a process at pid 100 -- true on a developer
    box, where those numbers are kernel threads, and false on a CI container,
    where the identity reads as unreadable and the kill arm fail-closes. Tests
    about identity build their own reconciler and pass their own seam.
    """
    live = kernel if alive is None else alive
    return rr.RuntimeReconciler(
        slice_pids=lambda: set(kernel),
        recorded_pids=lambda: set(recorded),
        is_alive=lambda pid: pid in live,
        was_recycled=lambda pid: pid in (recycled or set()),
        is_ours=lambda pid: pid in (kernel if ours is None else ours),
        # Faked for the reason the identity seam is: left at its default it reads the
        # HOST's argv for these invented numbers, so every kill test would depend on
        # whether the box happens to run a harness at pid 100. Tests about the argv
        # condition pass their own set.
        is_managed=lambda pid: pid in (kernel if managed is None else managed),
        leases_on=lambda pid: 1 if pid in (leased or set()) else 0,
        authorize=lambda pid, reason: authorize,
        identity_of=lambda pid: f"id-{pid}",
        kill_tree=lambda pid, expected=None: (killed if killed is not None else []).append(pid)
        or 1,
        # "retracted" is the seam's real answer for a row it removed; the tri-state is
        # what lets a record nobody can retract stop being reported as a failure.
        forget=lambda pid: ((forgotten if forgotten is not None else []).append(pid), "retracted")[
            1
        ],
        notify_dead=lambda pid: (notified if notified is not None else []).append(pid),
        age_secs=lambda pid: age,
        max_kills=max_kills,
        audit=lambda pid, outcome, why: (audited if audited is not None else []).append(
            (pid, outcome, why)
        ),
    )


def _refusals(audited: list[tuple[int, str, str]]) -> tuple[str, ...]:
    """The reasons a pass held pids back, read off the audit rows it wrote.

    The hold reason has one consumer, and it is the operator: a SEL row and a log
    line. Asserting it here rather than through a field on the reading means a
    mutation that corrupts the reason also fails the test, and the reading carries
    only the five counts the SLI publishes.
    """
    return tuple(why for _pid, outcome, why in audited if outcome == "refused")


class _Session:
    def __init__(self, pid: int | None = None) -> None:
        self.provider = object()
        self.semaphore = asyncio.BoundedSemaphore(1)
        self.last_used = 0.0
        self._pid = pid


class _Owner:
    """The slice of ``SessionManager`` the cleanup service reaches through."""

    def __init__(self) -> None:
        self._cfg = _Cfg()
        self._sessions: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._draining_bg_runtimes: list[Any] = []
        self._bg_runtime_lock = asyncio.Lock()
        self.on_session_expire = None
        self.on_stuck_turn = None
        self.recycled: list[tuple[str, str]] = []

    def get_pid(self, key: str) -> int | None:
        return getattr(self._sessions.get(key), "_pid", None)

    def _pool_pids(self) -> set[int]:
        return set()

    def _in_flight_pids(self) -> set[int]:
        return set()

    def _companion_runtime_pids(self) -> set[int]:
        return set()

    async def _fire_recycle_callback(self, key: str, *, reason: str) -> None:
        self.recycled.append((key, reason))

    async def _cleanup_loop(self) -> None:  # pragma: no cover - not driven here
        return None

    async def _expire_idle(self, timeout_secs: int) -> None:  # pragma: no cover
        return None

    async def _reap_drained_bg_runtimes_locked(self) -> None:  # pragma: no cover
        return None

    async def _reap_idle_stale_bg_runtime(self) -> bool:  # pragma: no cover
        return False

    async def reset(self, key: str, **kwargs: Any) -> bool:  # pragma: no cover
        return False


class _SessionCfg:
    timeout_secs = 0
    watchdog_rss_max_mb = 0
    #: 0, not the shipped default: these fixtures assert what the HOOK hands over,
    #: so the value has to be one no default could produce by accident.
    reconcile_max_kills = 0


class _Cfg:
    def __init__(self) -> None:
        # Per instance, not a shared class attribute: the fixture writes
        # ``reconcile_max_kills`` on it, and one shared object would carry a test's
        # kill budget into every test that ran after it.
        self.session = _SessionCfg()


def _cleanup(
    *,
    candidates: list[int],
    mcp_candidates: list[int] | None = None,
    active: set[int] | None = None,
    union_complete: bool = True,
    union_threads: list[int] | None = None,
    reconcile_max_kills: object = 0,
) -> tuple[Any, dict[str, list[int]]]:
    """A ``SessionCleanup`` whose sweeps report *candidates* and record kills.

    *union_threads* records the thread each active-pid read ran on, which is how
    the reconcile hook's snapshot-on-the-loop rule is checked.

    *reconcile_max_kills* is what ``session.reconcile_max_kills`` holds for this
    service, as a raw object rather than an int: the hook reads it off a config file
    an agent can write, so the tests need to hand it the values a hand edit produces.
    """
    from kiro_crew.session_cleanup import CleanupDeps, CleanupState, SessionCleanup
    from kiro_crew.watchdog import SessionWatchdog

    recorded: dict[str, list[int]] = {"pid_kills": [], "mcp_kills": []}
    executor = ThreadPoolExecutor(max_workers=1)

    class _Shutdown:
        def is_set(self) -> bool:
            return True

        def wait(self) -> Any:
            fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
            return fut

    def kill_confirmed(gateway_pid: int, confirmed: list[int], dead: set[str]) -> int:
        recorded["pid_kills"].extend(confirmed)
        return len(confirmed)

    def kill_mcps(pids: list[int]) -> int:
        recorded["mcp_kills"].extend(pids)
        return len(pids)

    def collect_active(sessions: Any) -> tuple[set[int], bool]:
        if union_threads is not None:
            union_threads.append(threading.get_ident())
        return set(active or set()), union_complete

    deps = CleanupDeps(
        logger=logging.getLogger("test.w4"),
        get_shutdown_signal=_Shutdown,
        get_maintenance_executor=lambda: executor,
        get_subprocess_executor=lambda: executor,
        cleanup_orphaned_mcp_servers=lambda: 0,
        cleanup_orphaned_session_roots=lambda: 0,
        cleanup_stale_sandbox_profiles=lambda: 0,
        prune_session_pid_mappings=lambda: 0,
        prune_member_pid_bindings=lambda: 0,
        sweep_session_staging_temps=lambda: 0,
        rotate_shell_audit_log=lambda: False,
        prune_pycache=lambda: (0, 0),
        collect_active_pids=collect_active,
        periodic_pid_sweep=lambda gw, pids: (set(), list(candidates)),
        kill_confirmed_and_writeback=kill_confirmed,
        find_orphan_mcp_candidates=lambda pids: list(
            candidates if mcp_candidates is None else mcp_candidates
        ),
        kill_orphan_mcps=kill_mcps,
        reap_agent_scopes=lambda pids: None,
        build_child_map=dict,
        rss_mb_from_tree=lambda pid, child_map: 0,
        get_session_rss_mb=lambda pid: 0,
        is_windows=lambda: False,
        getpid=lambda: 1,
        monotonic=lambda: 0.0,
        stats_factory=lambda: _Stats(),
        sel_factory=lambda: _Sel(),
        provider_has_active_turn=lambda provider: False,
        emit_counter=lambda event, dims: None,
        get_persistent_keys=frozenset,
        get_channel_prefix=lambda: "channel:",
        get_stuck_turn_report_secs=lambda: 1e9,
        get_pycache_gc_interval_secs=lambda: 1e9,
        get_session_idle_expired_event=lambda: "idle",
    )
    state = CleanupState(watchdog=SessionWatchdog([]))
    owner = _Owner()
    owner._cfg.session.reconcile_max_kills = reconcile_max_kills
    return SessionCleanup(owner, deps, state=state), recorded


class _Stats:
    def inc_session_cleaned(self) -> None:
        return None


class _Sel:
    def log_api_access(self, **kwargs: Any) -> None:
        return None


class _LeaseHolder:
    """One session's claim on a runtime with a given pid."""

    def __init__(self, pid: int) -> None:
        self._runtime = _PidRuntime(pid)
        self._lease: str | None = None

    async def take(self) -> None:
        acquisition = await ro.RUNTIME_OWNERSHIP.acquire(
            ("test", self._runtime.pid),
            f"session:{self._runtime.pid}",
            self._spawn,
        )
        self._lease = acquisition.lease

    @property
    def lease(self) -> str:
        """The lease id, for a test that must hand it to a provider stand-in."""
        assert self._lease is not None, "take() first"
        return self._lease

    async def give_back(self) -> None:
        if self._lease is not None:
            await ro.RUNTIME_OWNERSHIP.release(self._lease)
            self._lease = None

    async def _spawn(self) -> Any:
        return self._runtime


class _PidRuntime:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def is_alive(self) -> bool:
        return True


@pytest.fixture(name="agent_root")
def agent_root_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point subagent persistence at a registry below this test's temp directory."""
    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    return root
