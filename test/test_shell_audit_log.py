"""The gateway bounds the bundled shell-audit hook's ``audit.log``.

The default ``postToolUse`` hook in ``config/defaults.json`` appends every
shell call to ``<data home>/audit.log`` and bounds nothing. The bound lives
on the gateway side instead: ``shell_audit_log.rotate_shell_audit_log``
reuses ``jsonl_util.rotate_jsonl_at`` (one ``.1`` generation, try-locked,
never raises) and the session cleanup loop calls it on every tick. These
tests pin the module's contract, the tick call site, and the real wiring
from ``SessionManager``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew import shell_audit_log
from kiro_crew.shell_audit_log import (
    SHELL_AUDIT_LOG_MAX_BYTES,
    SHELL_AUDIT_LOG_NAME,
    SHELL_AUDIT_LOG_WARN_INTERVAL_SECS,
    rotate_shell_audit_log,
    shell_audit_log_path,
)

# One record in the shape the bundled hook writes: a single line -- a UTC stamp,
# then " BASH: ", then the hook-event payload kiro-cli hands the hook on stdin
# with every CR and LF stripped. The payload bytes stand in for that JSON; only
# the size matters to the sweep.
_RECORD = b"1970-01-01T00:00:00Z BASH: ls -la\n"


def _fill(path: Path, size: int) -> bytes:
    """Write hook-shaped records to *path* totalling EXACTLY *size* bytes.

    Exact, so a test at the cap pins the boundary itself: a file of precisely
    the cap must rotate, which a file a few bytes over cannot prove.
    """
    body = (_RECORD * (size // len(_RECORD) + 1))[:size]
    path.write_bytes(body)
    assert len(body) == size
    return body


class TestRotateShellAuditLog:
    def test_the_path_is_the_hooks_own(self, tmp_path: Path) -> None:
        """Same file the shipped command names: ``<data home>/audit.log``."""
        assert shell_audit_log_path(tmp_path) == tmp_path / "audit.log"
        assert SHELL_AUDIT_LOG_NAME == "audit.log"

    def test_a_log_at_the_shipped_cap_is_rotated_to_one_generation(self, tmp_path: Path) -> None:
        """Exactly at the cap rotates: the boundary is ``>=`` the cap, not ``>``."""
        live = tmp_path / SHELL_AUDIT_LOG_NAME
        body = _fill(live, SHELL_AUDIT_LOG_MAX_BYTES)
        assert live.stat().st_size == SHELL_AUDIT_LOG_MAX_BYTES

        assert rotate_shell_audit_log(tmp_path) is True

        rotated = tmp_path / (SHELL_AUDIT_LOG_NAME + ".1")
        assert rotated.read_bytes() == body, "the oversized file is kept as the .1 generation"
        assert not live.exists(), "the live file is renamed aside, not truncated or copied"
        assert not (tmp_path / (SHELL_AUDIT_LOG_NAME + ".2")).exists()

    def test_one_byte_under_the_shipped_cap_is_not_rotated(self, tmp_path: Path) -> None:
        live = tmp_path / SHELL_AUDIT_LOG_NAME
        body = _fill(live, SHELL_AUDIT_LOG_MAX_BYTES - 1)

        assert rotate_shell_audit_log(tmp_path) is False

        assert live.read_bytes() == body
        assert not (tmp_path / (SHELL_AUDIT_LOG_NAME + ".1")).exists()

    def test_a_log_under_the_cap_is_left_untouched(self, tmp_path: Path) -> None:
        live = tmp_path / SHELL_AUDIT_LOG_NAME
        body = _fill(live, 4096)
        assert len(body) < SHELL_AUDIT_LOG_MAX_BYTES

        assert rotate_shell_audit_log(tmp_path) is False

        assert live.read_bytes() == body
        # Nothing else appears beside it: no ``.1``, and no lock file either,
        # since a file under the cap never reaches the rotation primitive.
        assert sorted(p.name for p in tmp_path.iterdir()) == [SHELL_AUDIT_LOG_NAME]

    def test_a_second_rotation_replaces_the_generation_rather_than_adding_one(
        self, tmp_path: Path
    ) -> None:
        """Disk use stays at two generations: the live file and one ``.1``."""
        live = tmp_path / SHELL_AUDIT_LOG_NAME
        rotated = tmp_path / (SHELL_AUDIT_LOG_NAME + ".1")

        live.write_bytes(b"1" * SHELL_AUDIT_LOG_MAX_BYTES)
        assert rotate_shell_audit_log(tmp_path) is True
        assert rotated.read_bytes() == b"1" * SHELL_AUDIT_LOG_MAX_BYTES

        live.write_bytes(b"2" * SHELL_AUDIT_LOG_MAX_BYTES)
        assert rotate_shell_audit_log(tmp_path) is True
        assert rotated.read_bytes() == b"2" * SHELL_AUDIT_LOG_MAX_BYTES
        assert not live.exists()
        names = {p.name for p in tmp_path.iterdir() if not p.name.endswith(".lock")}
        assert names == {SHELL_AUDIT_LOG_NAME + ".1"}

    def test_a_missing_log_is_not_an_error_and_creates_nothing(self, tmp_path: Path) -> None:
        """A fresh install has no audit.log yet; the sweep must leave no trace."""
        assert rotate_shell_audit_log(tmp_path) is False
        assert list(tmp_path.iterdir()) == []

    def test_an_unusable_data_home_is_not_an_error(self, tmp_path: Path) -> None:
        """The home resolving to a FILE makes every stat fail; the sweep still returns."""
        not_a_dir = tmp_path / "home"
        not_a_dir.write_bytes(b"not a directory")
        assert rotate_shell_audit_log(not_a_dir) is False
        assert not_a_dir.read_bytes() == b"not a directory"

    def test_a_blocked_rotation_slot_never_raises_and_keeps_the_live_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A directory planted at ``audit.log.1`` makes the rename fail; the record survives.

        The failure is not silent either: the primitive swallows it by contract,
        so this is the one place a rotation that stopped working can surface,
        and an over-cap file that cannot be moved aside must not read like a
        file under the cap.
        """
        live = tmp_path / SHELL_AUDIT_LOG_NAME
        body = _fill(live, SHELL_AUDIT_LOG_MAX_BYTES)
        (tmp_path / (SHELL_AUDIT_LOG_NAME + ".1")).mkdir()

        with caplog.at_level(logging.WARNING, logger="kiro_crew.shell_audit_log"):
            assert rotate_shell_audit_log(tmp_path) is False

        assert live.read_bytes() == body
        assert (tmp_path / (SHELL_AUDIT_LOG_NAME + ".1")).is_dir()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "a rotation that could not complete must warn once"
        assert "could not be rotated" in warnings[0].getMessage()
        assert f"{SHELL_AUDIT_LOG_MAX_BYTES} bytes" in warnings[0].getMessage()

    def test_a_rotation_that_stays_blocked_warns_once_per_interval_not_once_per_attempt(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller retries every tick for the gateway's life; a blocked slot does not clear itself.

        One line per attempt would be the same WARNING hundreds of times a day.
        The throttle is per data home, so a second home warns on its own clock.
        """
        clock = [1_000.0]
        monkeypatch.setattr(
            shell_audit_log, "time", SimpleNamespace(monotonic=lambda: clock[0]), raising=False
        )
        live = tmp_path / SHELL_AUDIT_LOG_NAME
        _fill(live, SHELL_AUDIT_LOG_MAX_BYTES)
        (tmp_path / (SHELL_AUDIT_LOG_NAME + ".1")).mkdir()
        other = tmp_path / "other-home"
        other.mkdir()
        _fill(other / SHELL_AUDIT_LOG_NAME, SHELL_AUDIT_LOG_MAX_BYTES)
        (other / (SHELL_AUDIT_LOG_NAME + ".1")).mkdir()

        def _warnings() -> list[logging.LogRecord]:
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.WARNING, logger="kiro_crew.shell_audit_log"):
            assert rotate_shell_audit_log(tmp_path) is False
            clock[0] += 300.0  # the next tick, well inside the interval
            assert rotate_shell_audit_log(tmp_path) is False
            assert len(_warnings()) == 1, "a retry inside the interval repeats nothing"

            assert rotate_shell_audit_log(other) is False
            assert len(_warnings()) == 2, "another data home is another throttle window"

            clock[0] += SHELL_AUDIT_LOG_WARN_INTERVAL_SECS
            assert rotate_shell_audit_log(tmp_path) is False
            assert len(_warnings()) == 3, "the interval elapsed: the stuck bound is reported again"
        assert "next warning in 3600 s" in _warnings()[0].getMessage()

    def test_a_file_under_the_cap_warns_about_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _fill(tmp_path / SHELL_AUDIT_LOG_NAME, 256)
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.shell_audit_log"):
            assert rotate_shell_audit_log(tmp_path) is False
        assert not caplog.records, "a file under the cap is the quiet, ordinary case"

    def test_the_cap_is_bounded_by_its_stated_measurement(self) -> None:
        """8 MiB: about 9.5 weeks of the measured 4.4 MB / 5 weeks before the first rotation."""
        assert SHELL_AUDIT_LOG_MAX_BYTES == 8 * 1024 * 1024


# ── The two call sites in the session cleanup loop ──────────────────────────


class _Shutdown:
    """A shutdown signal whose ``wait`` resolves when *done* is set."""

    def __init__(self, done: asyncio.Event) -> None:
        self._done = done

    def is_set(self) -> bool:
        return self._done.is_set()

    def wait(self) -> Any:
        return self._done.wait()


class _Owner:
    """The slice of ``SessionManager`` the cleanup service reaches through."""

    def __init__(self) -> None:
        self._cfg = SimpleNamespace(
            session=SimpleNamespace(timeout_secs=0, watchdog_rss_max_mb=0, reconcile_max_kills=0)
        )
        self._sessions: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._watchdog = SimpleNamespace(tick=AsyncMock())

    def get_pid(self, key: str) -> int | None:  # pragma: no cover - not driven here
        return None

    def _pool_pids(self) -> set[int]:  # pragma: no cover - not driven here
        return set()

    def _in_flight_pids(self) -> set[int]:  # pragma: no cover - not driven here
        return set()

    def _companion_runtime_pids(self) -> set[int]:  # pragma: no cover - not driven here
        return set()


_OTHER_TICK_STEPS = (
    "_sweep_session_roots",
    "_sweep_sandbox_artifacts",
    "_sweep_session_pid_mappings",
    "_sweep_member_pid_bindings",
    "_maybe_prune_pycache",
    "_sweep_periodic_pids",
    "_sweep_untracked_mcps",
    "_reconcile_runtimes_hook",
)


def _service(shutdown: _Shutdown, rotate: Any, pool: ThreadPoolExecutor) -> Any:
    from kiro_crew.session_cleanup import CleanupDeps, CleanupState, SessionCleanup
    from kiro_crew.watchdog import SessionWatchdog

    deps = CleanupDeps(
        logger=logging.getLogger("test.shell_audit_log"),
        get_shutdown_signal=lambda: shutdown,
        get_maintenance_executor=lambda: pool,
        get_subprocess_executor=lambda: pool,
        cleanup_orphaned_mcp_servers=lambda: 0,
        cleanup_orphaned_session_roots=lambda: 0,
        cleanup_stale_sandbox_profiles=lambda: 0,
        prune_session_pid_mappings=lambda: 0,
        prune_member_pid_bindings=lambda: 0,
        rotate_shell_audit_log=rotate,
        prune_pycache=lambda: (0, 0),
        collect_active_pids=lambda sessions: (set(), True),
        periodic_pid_sweep=lambda gw, pids: (set(), []),
        kill_confirmed_and_writeback=lambda gw, confirmed, dead: 0,
        find_orphan_mcp_candidates=lambda pids: [],
        kill_orphan_mcps=lambda pids: 0,
        reap_agent_scopes=lambda pids: None,
        build_child_map=dict,
        rss_mb_from_tree=lambda pid, child_map: 0,
        get_session_rss_mb=lambda pid: 0,
        is_windows=lambda: False,
        getpid=lambda: 1,
        monotonic=lambda: 0.0,
        stats_factory=lambda: SimpleNamespace(inc_session_cleaned=lambda: None),
        sel_factory=lambda: SimpleNamespace(log_api_access=lambda **kw: None),
        provider_has_active_turn=lambda provider: False,
        emit_counter=lambda event, dims: None,
        get_persistent_keys=frozenset,
        get_channel_prefix=lambda: "channel:",
        get_stuck_turn_report_secs=lambda: 1e9,
        get_pycache_gc_interval_secs=lambda: 1e9,
        get_session_idle_expired_event=lambda: "idle",
    )
    state = CleanupState(watchdog=SessionWatchdog([]))
    return SessionCleanup(cast(Any, _Owner()), deps, state=state)


@pytest.mark.asyncio
async def test_every_cleanup_tick_rotates_the_audit_log_off_the_loop() -> None:
    """The periodic bound rides the existing tick; no timer of its own.

    The first tick comes within ``MAX_TICK_INTERVAL_SECS`` of the loop
    starting, which is also what bounds an install that already carries an
    oversized file. The rotation is dispatched to the maintenance executor:
    the data home is agent-writable, so its stat and rename never run on the
    event loop.
    """
    from kiro_crew.session_cleanup import SessionCleanup

    done = asyncio.Event()
    ran_on: list[int] = []
    ticks = 0

    def rotate() -> bool:
        ran_on.append(threading.get_ident())
        return False

    def adopt(self: Any) -> float:
        nonlocal ticks
        ticks += 1
        if ticks >= 3:
            done.set()
        return 0.01

    pool = ThreadPoolExecutor(max_workers=1)
    service = _service(_Shutdown(done), rotate, pool)
    try:
        with (
            patch.object(SessionCleanup, "_adopt_idle_policy", adopt),
            patch.multiple(SessionCleanup, **{name: AsyncMock() for name in _OTHER_TICK_STEPS}),
        ):
            await asyncio.wait_for(service._run_cleanup_ticks(0.01), timeout=10)
    finally:
        pool.shutdown(wait=True)

    assert ran_on, "the cleanup tick never rotated the shell audit log"
    assert threading.get_ident() not in ran_on, "the rotation ran on the event-loop thread"


@pytest.mark.asyncio
async def test_a_rotation_that_raises_does_not_end_the_tick() -> None:
    """The step is fail-open like its siblings: a raise is logged, the tick goes on."""
    from kiro_crew.session_cleanup import SessionCleanup

    done = asyncio.Event()
    calls = 0

    def rotate() -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("planted")

    def adopt(self: Any) -> float:
        if calls >= 2:
            done.set()
        return 0.01

    pool = ThreadPoolExecutor(max_workers=1)
    service = _service(_Shutdown(done), rotate, pool)
    try:
        with (
            patch.object(SessionCleanup, "_adopt_idle_policy", adopt),
            patch.multiple(SessionCleanup, **{name: AsyncMock() for name in _OTHER_TICK_STEPS}),
        ):
            await asyncio.wait_for(service._run_cleanup_ticks(0.01), timeout=10)
    finally:
        pool.shutdown(wait=True)

    assert calls >= 2, "a raising rotation must not stop the loop from ticking again"


def test_the_real_wiring_rotates_the_data_homes_audit_log() -> None:
    """``SessionManager._cleanup_deps`` points the sweep at the resolved data home.

    Drives the forwarding callable the real deps carry against the isolated
    ``KIROCREW_HOME`` the suite pins, so the path and the cap are the shipped
    ones end to end rather than a fake's.
    """
    from kiro_crew.config.paths import config_dir
    from kiro_crew.session import SessionManager

    home = config_dir()
    live = home / SHELL_AUDIT_LOG_NAME
    body = _fill(live, SHELL_AUDIT_LOG_MAX_BYTES)
    try:
        deps = SessionManager._cleanup_deps(cast(Any, SimpleNamespace()))
        assert deps.rotate_shell_audit_log() is True
        assert (home / (SHELL_AUDIT_LOG_NAME + ".1")).read_bytes() == body
        assert not live.exists()
    finally:
        for name in (
            SHELL_AUDIT_LOG_NAME,
            SHELL_AUDIT_LOG_NAME + ".1",
            SHELL_AUDIT_LOG_NAME + ".lock",
        ):
            (home / name).unlink(missing_ok=True)
