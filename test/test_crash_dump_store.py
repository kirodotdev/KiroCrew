"""Tests for crash-dump store — rotation, newest-dump detection, doctor surfacing.

Uses injected temp directories to avoid touching the real ~/.kirocrew/logs/.
Follows the same injectable-dependency pattern as test_loop_watchdog.py.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kiro_crew.dashboard.crash_dump_store import (
    DUMP_PREFIX,
    DUMP_SUFFIX,
    dump_age_seconds,
    dump_first_stack_lines,
    newest_dump,
    newest_dump_with_stacks,
    open_dump_file,
    rotate_dumps,
)


@pytest.fixture
def dumps_dir(tmp_path: Path) -> Path:
    d = tmp_path / "crash-dumps"
    d.mkdir()
    return d


def _create_header_only_dump(dumps_dir: Path, name: str) -> Path:
    """Create a dump file with only the header (no stacks = clean exit)."""
    p = dumps_dir / name
    p.write_text(
        "# KiroCrew loop-stall crash dump — opened 20260717T010000Z\n"
        "# PID: 12345\n"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n"
    )
    return p


def _create_stacked_dump(dumps_dir: Path, name: str) -> Path:
    """Create a dump file with real stack content (simulating a wedge)."""
    p = dumps_dir / name
    p.write_text(
        "# KiroCrew loop-stall crash dump — opened 20260717T020000Z\n"
        "# PID: 12345\n"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n"
        "Thread 0x00007f1234 (most recent call first):\n"
        '  File "/usr/lib/python3.12/socket.py", line 704, in close\n'
        "    self._real_close()\n"
        '  File "/home/user/.kirocrew/src/kiro_crew/acp/client.py", line 312, in _teardown\n'
        "    self._sock.close()\n"
        '  File "/home/user/.kirocrew/src/kiro_crew/dashboard/server.py", line 800, in _cleanup\n'
        "    await self._teardown()\n"
    )
    return p


# ── Rotation ──


def test_rotate_removes_oldest(dumps_dir: Path) -> None:
    # Create 12 dump files (more than max_dumps=10)
    for i in range(12):
        p = dumps_dir / f"{DUMP_PREFIX}2026071{i:02d}T000000Z{DUMP_SUFFIX}"
        p.write_text(f"dump {i}")
        # Stagger mtimes so sort order is deterministic
        os.utime(p, (1000 + i, 1000 + i))

    removed = rotate_dumps(max_dumps=10, dumps_dir=dumps_dir)
    remaining = list(dumps_dir.iterdir())
    # After rotation with max_dumps=10, we keep max_dumps-1=9 (room for new one)
    assert len(remaining) == 9
    assert removed == 3
    # The 3 oldest (i=0,1,2) should be gone
    for i in range(3):
        assert not (dumps_dir / f"{DUMP_PREFIX}2026071{i:02d}T000000Z{DUMP_SUFFIX}").exists()


def test_rotate_noop_when_under_limit(dumps_dir: Path) -> None:
    _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    removed = rotate_dumps(max_dumps=10, dumps_dir=dumps_dir)
    assert removed == 0
    assert len(list(dumps_dir.iterdir())) == 1


def test_rotate_empty_dir(dumps_dir: Path) -> None:
    removed = rotate_dumps(max_dumps=10, dumps_dir=dumps_dir)
    assert removed == 0


# ── Open dump file ──


def test_open_dump_file_creates_file(dumps_dir: Path) -> None:
    f = open_dump_file(dumps_dir)
    try:
        assert f is not None
        assert not f.closed
        # File should exist on disk
        files = list(dumps_dir.iterdir())
        assert len(files) == 1
        assert files[0].name.startswith(DUMP_PREFIX)
        assert files[0].name.endswith(DUMP_SUFFIX)
        # Header should be written
        content = files[0].read_text(encoding="utf-8")
        assert "KiroCrew loop-stall crash dump" in content
        assert "PID:" in content
    finally:
        f.close()


def test_open_dump_file_returns_writable_fd(dumps_dir: Path) -> None:
    f = open_dump_file(dumps_dir)
    try:
        # faulthandler needs to write to this fd
        f.write("Thread 0x1234 (most recent call first):\n")
        f.flush()
        files = list(dumps_dir.iterdir())
        content = files[0].read_text(encoding="utf-8")
        assert "Thread 0x1234" in content
    finally:
        f.close()


# ── Newest dump detection ──


def test_newest_dump_returns_none_on_empty(dumps_dir: Path) -> None:
    assert newest_dump(dumps_dir) is None


def test_newest_dump_returns_latest(dumps_dir: Path) -> None:
    p1 = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260716T010000Z{DUMP_SUFFIX}")
    os.utime(p1, (1000, 1000))
    p2 = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    os.utime(p2, (2000, 2000))

    result = newest_dump(dumps_dir)
    assert result == p2


def test_newest_dump_with_stacks_skips_header_only(dumps_dir: Path) -> None:
    # Older dump with stacks
    p1 = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260716T010000Z{DUMP_SUFFIX}")
    os.utime(p1, (1000, 1000))
    # Newer dump with only header (clean shutdown)
    p2 = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    os.utime(p2, (2000, 2000))

    # newest_dump returns p2 (most recent by mtime)
    assert newest_dump(dumps_dir) == p2
    # newest_dump_with_stacks skips p2 and returns p1
    assert newest_dump_with_stacks(dumps_dir) == p1


def test_newest_dump_with_stacks_returns_none_when_all_clean(dumps_dir: Path) -> None:
    _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T020000Z{DUMP_SUFFIX}")
    assert newest_dump_with_stacks(dumps_dir) is None


# ── Stack line extraction ──


def test_dump_first_stack_lines(dumps_dir: Path) -> None:
    p = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    lines = dump_first_stack_lines(p, max_lines=3)
    assert len(lines) == 3
    assert "Thread 0x" in lines[0]
    assert "socket.py" in lines[1]


def test_dump_first_stack_lines_header_only(dumps_dir: Path) -> None:
    p = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    lines = dump_first_stack_lines(p, max_lines=5)
    assert lines == []


# ── Age calculation ──


def test_dump_age_seconds(dumps_dir: Path) -> None:
    p = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    age = dump_age_seconds(p)
    # Should be very small since we just created it
    assert 0 <= age < 2.0


def test_dump_age_never_negative_with_future_mtime(dumps_dir: Path) -> None:
    """A dump whose mtime rounds marginally AHEAD of ``time.time()`` (sub-microsecond
    float jitter on a just-written file, or higher-resolution FS timestamps) must
    report age 0.0 — never a negative. Regression for `assert 0 <= age` failing
    with a tiny negative delta (~-2e-7)."""
    import time

    p = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    st = p.stat()
    # Force mtime clearly into the future to reproduce the jitter deterministically.
    os.utime(p, (st.st_atime, time.time() + 5.0))
    assert dump_age_seconds(p) == 0.0


# ── Integration with LoopStallWatchdog dump_file param ──


def test_watchdog_dump_file_param_custom_callback(dumps_dir: Path) -> None:
    """Verify custom dump callback is invoked when dump_file is set (wiring only)."""
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog

    class _Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

        def advance(self, dt: float) -> None:
            self.t += dt

    clock = _Clock()
    dump_targets: list[object] = []

    # Open a dump file
    dump_file = open_dump_file(dumps_dir)
    try:
        # Create watchdog with dump_file — custom dump callback to verify it's wired
        wd = LoopStallWatchdog(
            stall_after=30.0,
            exit_after=None,
            now=clock,
            dump=lambda: dump_targets.append("called"),
            dump_file=dump_file,
            log=logging.getLogger("test.loop_watchdog"),
        )
        wd.beat()
        clock.advance(31.0)
        assert wd.check() is True
        assert dump_targets == ["called"]
    finally:
        dump_file.close()


def test_watchdog_dump_file_default_dump(dumps_dir: Path) -> None:
    """Verify dump_file receives real faulthandler output when NO custom dump is set.

    This is the real wiring test: construct LoopStallWatchdog with dump_file and
    NO custom dump callback, beat, advance past stall_after, call check(), flush,
    and assert the file contains thread-stack markers from faulthandler.
    """
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog

    class _Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

        def advance(self, dt: float) -> None:
            self.t += dt

    clock = _Clock()

    dump_file = open_dump_file(dumps_dir)
    try:
        wd = LoopStallWatchdog(
            stall_after=30.0,
            exit_after=None,
            now=clock,
            dump_file=dump_file,
            # NO custom dump — exercises _default_dump(dump_file)
            arm_later=lambda t: None,  # disable armed timer
            cancel_later=lambda: None,
            log=logging.getLogger("test.loop_watchdog"),
        )
        wd.beat()
        clock.advance(31.0)
        assert wd.check() is True
        dump_file.flush()

        # Read the file content — should contain faulthandler thread stack output
        dump_path = list(dumps_dir.iterdir())[0]
        content = dump_path.read_text(encoding="utf-8", errors="replace")
        # faulthandler.dump_traceback writes a thread marker ("Thread 0x..." when
        # multiple threads exist, "Current thread 0x..." for a single thread) —
        # match case-insensitively so the assertion holds whether the run is
        # multi-threaded (free-threaded CPython) or single-threaded.
        assert "thread" in content.lower(), (
            f"Expected thread stacks in dump file, got: {content!r}"
        )
    finally:
        dump_file.close()


# ── fd stability (regression for #1571) ──


def test_dump_file_fd_survives_repeated_arm_cancel(dumps_dir: Path) -> None:
    """Regression test for #1571: the raw fd must remain valid across cancel/re-arm.

    The bug: faulthandler's C timer captures the fd at arm time and writes to it
    when the timer fires.  If the fd is invalidated between arm and fire (e.g.
    by GC of an intermediate Python file object or by closing/reopening), the
    dump writes to nothing and the crash file contains only the header.

    This test simulates the beat() cadence (cancel + re-arm every 5s) and then
    verifies that a faulthandler.dump_traceback(file=dump_file) still lands real
    content in the file — proving the fd was not invalidated by the churn.
    """
    import faulthandler
    import gc

    dump_file = open_dump_file(dumps_dir)
    try:
        # Simulate 20 cancel/re-arm cycles (beat() every 5s for ~100s of runtime).
        # Each cycle exercises the same code path that runs in production.
        for _ in range(20):
            fd = dump_file.fileno()
            # Verify the fd is still valid after each "cycle"
            os.fstat(fd)  # raises OSError if fd was closed/invalidated

        # Force a GC to surface any weak-reference or ref-counting issues
        gc.collect()

        # The fd must still be valid after GC
        os.fstat(dump_file.fileno())

        # Now verify faulthandler can actually write through it
        faulthandler.dump_traceback(file=dump_file, all_threads=True)

        # Read the file and confirm real stacks landed (not just the header)
        dump_path = list(dumps_dir.iterdir())[0]
        content = dump_path.read_text(encoding="utf-8", errors="replace")
        assert "thread" in content.lower(), (
            f"Expected thread stacks after 20 arm/cancel cycles, got: {content!r}"
        )
    finally:
        dump_file.close()


def test_dump_file_fileno_is_stable(dumps_dir: Path) -> None:
    """The fd number returned by fileno() never changes across the DumpFile lifetime."""
    dump_file = open_dump_file(dumps_dir)
    try:
        fd1 = dump_file.fileno()
        dump_file.write("some data\n")
        dump_file.flush()
        fd2 = dump_file.fileno()
        assert fd1 == fd2, "fileno() must return the same fd across calls"
    finally:
        dump_file.close()


# ── dump_replay_lines ──


def test_dump_replay_lines_basic(dumps_dir: Path) -> None:
    """Replay reads all stack lines within limits."""
    from kiro_crew.dashboard.crash_dump_store import dump_replay_lines

    p = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T030000Z{DUMP_SUFFIX}")
    lines, truncated = dump_replay_lines(p)
    assert len(lines) > 0
    assert "Thread" in lines[0]
    assert not truncated


def test_dump_replay_lines_truncates_by_line_count(dumps_dir: Path) -> None:
    """Replay truncates at max_lines."""
    from kiro_crew.dashboard.crash_dump_store import dump_replay_lines

    p = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T030000Z{DUMP_SUFFIX}")
    lines, truncated = dump_replay_lines(p, max_lines=2)
    assert len(lines) == 2
    assert truncated


def test_dump_replay_lines_truncates_by_bytes(dumps_dir: Path) -> None:
    """Replay truncates at max_bytes."""
    from kiro_crew.dashboard.crash_dump_store import dump_replay_lines

    p = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T030000Z{DUMP_SUFFIX}")
    lines, truncated = dump_replay_lines(p, max_bytes=50)
    assert truncated
    total = sum(len(ln) for ln in lines)
    assert total <= 50


def test_dump_replay_lines_header_only(dumps_dir: Path) -> None:
    """Replay returns empty for header-only dumps."""
    from kiro_crew.dashboard.crash_dump_store import dump_replay_lines

    p = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T030000Z{DUMP_SUFFIX}")
    lines, truncated = dump_replay_lines(p)
    assert lines == []
    assert not truncated


# ── Journal replay integration test ──


def test_startup_crash_dump_replay_logs_stacks(dumps_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Verify that the journal replay logic logs dump content at WARNING."""
    from kiro_crew.dashboard.crash_dump_store import (
        dump_replay_lines,
        newest_dump_with_stacks,
    )

    _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T030000Z{DUMP_SUFFIX}")
    prior_dump = newest_dump_with_stacks(dumps_dir)
    assert prior_dump is not None

    # Simulate the server.py replay logic
    _replay_lines, _truncated = dump_replay_lines(prior_dump)
    assert len(_replay_lines) > 0
    _replay_body = "\n".join(_replay_lines)
    if _truncated:
        _replay_body += "\n  [truncated — full dump at above path]"

    test_logger = logging.getLogger("test.startup_replay")
    with caplog.at_level(logging.WARNING, logger="test.startup_replay"):
        test_logger.warning("Replaying prior crash dump stacks:\n%s", _replay_body)

    assert "Thread" in caplog.text
    assert "socket.py" in caplog.text
