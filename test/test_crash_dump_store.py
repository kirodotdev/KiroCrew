"""Tests for crash-dump store — rotation, newest-dump detection, doctor surfacing.

Uses injected temp directories to avoid touching the real ~/.kirocrew/logs/.
Follows the same injectable-dependency pattern as test_loop_watchdog.py.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kiro_crew.dashboard import crash_dump_store
from kiro_crew.dashboard.crash_dump_store import (
    DUMP_PREFIX,
    DUMP_SUFFIX,
    dump_age_seconds,
    dump_first_stack_lines,
    newest_dump,
    newest_dump_with_stacks,
    open_dump_file,
    rotate_dumps,
    sweep_stale_dumps,
)


@pytest.fixture
def dumps_dir(tmp_path: Path) -> Path:
    d = tmp_path / "crash-dumps"
    d.mkdir()
    return d


def _create_header_only_dump(
    dumps_dir: Path, name: str, *, pid_domain: str | None = None
) -> Path:
    """Create a dump file with only the header (no stacks = clean exit).

    ``pid_domain`` defaults to THIS process's domain so ownership is
    attributable and the injected liveness check is consulted; pass a foreign
    domain (or empty string for a legacy domain-less header) to exercise the
    unattributable paths.
    """
    domain = crash_dump_store._pid_domain() if pid_domain is None else pid_domain
    pid_line = f"# PID: 12345 @ {domain}\n" if domain else "# PID: 12345\n"
    p = dumps_dir / name
    p.write_text(
        "# KiroCrew loop-stall crash dump — opened 20260717T010000Z\n"
        + pid_line
        + "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n"
    )
    return p


def _create_stacked_dump(dumps_dir: Path, name: str) -> Path:
    """Create a dump file with real stack content (simulating a wedge)."""
    p = dumps_dir / name
    p.write_text(
        "# KiroCrew loop-stall crash dump — opened 20260717T020000Z\n"
        f"# PID: 12345 @ {crash_dump_store._pid_domain()}\n"
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


# ── Stale header-only dump sweep ──


def _dead_pid(pid: int) -> bool:
    return False


def _live_pid(pid: int) -> bool:
    return True


def test_sweep_removes_header_only_dump_of_dead_pid(dumps_dir: Path) -> None:
    p = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 1
    assert not p.exists()


def test_sweep_keeps_header_only_dump_of_live_pid(dumps_dir: Path) -> None:
    # A live PID means another gateway on this data home still owns the file
    # (concurrent pod / overlapping restart) — must not be touched.
    p = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_live_pid)
    assert removed == 0
    assert p.exists()


def test_sweep_never_touches_dumps_with_stacks(dumps_dir: Path) -> None:
    p = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T020000Z{DUMP_SUFFIX}")
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0
    assert p.exists()


def test_sweep_keeps_own_pid_file(dumps_dir: Path) -> None:
    # The current process's own pre-created file must survive even if the
    # injected liveness check lies about it.
    p = dumps_dir / f"{DUMP_PREFIX}20260717T030000Z{DUMP_SUFFIX}"
    p.write_text(
        "# KiroCrew loop-stall crash dump — opened 20260717T030000Z\n"  # brand-ok: mirrors production dump header
        f"# PID: {os.getpid()} @ {crash_dump_store._pid_domain()}\n"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n"
    )
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0
    assert p.exists()


def test_sweep_keeps_dump_from_foreign_pid_domain(dumps_dir: Path) -> None:
    # A PID recorded by a gateway on another host sharing the data home (or in
    # another PID namespace) is not checkable with a local liveness probe — a
    # locally-dead verdict says nothing about the remote owner, whose
    # faulthandler still holds this file's fd. Must be left alone.
    p = _create_header_only_dump(
        dumps_dir,
        f"{DUMP_PREFIX}20260717T090000Z{DUMP_SUFFIX}",
        pid_domain="other-host/pid:[4026531836]",
    )
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0
    assert p.exists()


def test_sweep_keeps_legacy_dump_without_pid_domain(dumps_dir: Path) -> None:
    # Headers written before the PID domain was recorded carry a bare PID.
    # Ownership cannot be scoped to a PID table, so the sweep leaves the file
    # for rotation to reap under pressure.
    p = _create_header_only_dump(
        dumps_dir, f"{DUMP_PREFIX}20260717T100000Z{DUMP_SUFFIX}", pid_domain=""
    )
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0
    assert p.exists()


def test_open_dump_file_header_records_pid_domain(dumps_dir: Path) -> None:
    # The header must qualify the PID with this process's PID domain so a
    # later sweep on a different host/namespace treats it as unattributable,
    # and (where procfs exists) with the start ID so a recycled PID cannot
    # masquerade as the owner.
    f = open_dump_file(dumps_dir)
    content = f.path.read_text(encoding="utf-8")
    start_id = crash_dump_store._pid_start_id(os.getpid())
    start_tok = f" start={start_id}" if start_id is not None else ""
    assert f"# PID: {os.getpid()} @ {crash_dump_store._pid_domain()}{start_tok}\n" in content
    # And a same-process sweep must classify it as alive-owned, not sweep it.
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0
    assert f.path.exists()


def test_sweep_keeps_header_only_dump_without_pid_line(dumps_dir: Path) -> None:
    # No parseable PID — cannot attribute the file, so leave it alone.
    p = dumps_dir / f"{DUMP_PREFIX}20260717T040000Z{DUMP_SUFFIX}"
    p.write_text("# KiroCrew loop-stall crash dump — opened 20260717T040000Z\n\n")  # brand-ok: mirrors production dump header
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0
    assert p.exists()


def test_sweep_treats_oversized_pid_as_unparseable(dumps_dir: Path) -> None:
    # A corrupt header whose digit run exceeds any real pid_t must not raise
    # out of the startup sweep (int() digit-limit ValueError, os.kill
    # OverflowError) — the file is unattributable and left alone.
    for name, digits in (("20260717T050000Z", "9" * 5000), ("20260717T060000Z", str(2**31))):
        p = dumps_dir / f"{DUMP_PREFIX}{name}{DUMP_SUFFIX}"
        p.write_text(
            "# KiroCrew loop-stall crash dump — opened 20260717T050000Z\n"  # brand-ok: mirrors production dump header
            f"# PID: {digits}\n"
            "\n"
        )
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_sweep_refuses_symlinked_dump(dumps_dir: Path) -> None:
    # A dump-named symlink (e.g. pointed at /dev/zero) must not be followed:
    # the header inspection opens O_NOFOLLOW, so the sweep leaves the entry
    # alone instead of pulling an unbounded read.
    target = dumps_dir / "target.txt"
    target.write_text("# not a dump\n")
    link = dumps_dir / f"{DUMP_PREFIX}20260717T070000Z{DUMP_SUFFIX}"
    link.symlink_to(target)
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0
    assert link.is_symlink()


def test_list_dumps_skips_files_vanishing_mid_listing(
    dumps_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A concurrent gateway's sweep can unlink a dump between ``iterdir()`` and
    # ``stat()``; the listing must skip the vanished entry, not raise out of
    # the startup sweep.
    keep = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    ghost = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T020000Z{DUMP_SUFFIX}")

    real_stat = Path.stat

    def racing_stat(self: Path, **kwargs: object) -> object:
        if self.name == ghost.name:
            raise FileNotFoundError(str(self))
        return real_stat(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", racing_stat)
    assert crash_dump_store._list_dumps(dumps_dir) == [keep]


def test_sweep_reads_only_a_bounded_prefix(dumps_dir: Path) -> None:
    # A huge file is by definition not header-only; the sweep must classify it
    # from its leading bytes alone and never load the whole thing.
    p = dumps_dir / f"{DUMP_PREFIX}20260717T080000Z{DUMP_SUFFIX}"
    with p.open("w") as f:
        f.write("# KiroCrew loop-stall crash dump — opened 20260717T080000Z\n")  # brand-ok: mirrors production dump header
        f.write("# PID: 1\n\n")
        f.write("x" * (1024 * 1024))  # single long line, no newlines
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 0
    assert p.exists()


def test_sweep_empty_dir(dumps_dir: Path) -> None:
    assert sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid) == 0


def test_sweep_mixed_directory(dumps_dir: Path) -> None:
    stale1 = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T010000Z{DUMP_SUFFIX}")
    real = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260717T020000Z{DUMP_SUFFIX}")
    stale2 = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T030000Z{DUMP_SUFFIX}")
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 2
    assert not stale1.exists()
    assert not stale2.exists()
    assert real.exists()


# ── Stack-aware rotation ──


def test_rotate_sacrifices_header_only_before_stacked(dumps_dir: Path) -> None:
    # Oldest file has REAL stacks; three newer header-only files follow.
    # With max_dumps=3 the rotation must delete header-only files (oldest
    # first) and keep the stall evidence, even though it is the oldest file.
    real = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260710T000000Z{DUMP_SUFFIX}")
    os.utime(real, (1000, 1000))
    empties = []
    for i in range(1, 4):
        p = _create_header_only_dump(
            dumps_dir, f"{DUMP_PREFIX}2026071{i}T000000Z{DUMP_SUFFIX}"
        )
        os.utime(p, (1000 + i, 1000 + i))
        empties.append(p)

    removed = rotate_dumps(max_dumps=3, dumps_dir=dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 2
    assert real.exists()
    # The two OLDEST header-only files are gone; the newest survives.
    assert not empties[0].exists()
    assert not empties[1].exists()
    assert empties[2].exists()


def test_rotate_removes_stacked_when_no_header_only_left(dumps_dir: Path) -> None:
    paths = []
    for i in range(4):
        p = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}2026071{i}T000000Z{DUMP_SUFFIX}")
        os.utime(p, (1000 + i, 1000 + i))
        paths.append(p)

    removed = rotate_dumps(max_dumps=3, dumps_dir=dumps_dir, is_pid_alive=_dead_pid)
    assert removed == 2
    # Oldest two stacked dumps removed, newest two kept.
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert paths[2].exists()
    assert paths[3].exists()


def test_rotate_never_victimizes_a_live_owners_dump(dumps_dir: Path) -> None:
    # A concurrently running gateway's pre-created dump is header-only until
    # it wedges — exactly the class sacrificed first. faulthandler holds its
    # fd for the owner's lifetime, so unlinking it would send later stall
    # evidence to an unreachable inode. Live-owner dumps are never victims,
    # even when that means staying over the cap.
    live = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260710T000000Z{DUMP_SUFFIX}")
    os.utime(live, (1000, 1000))  # oldest — would be first victim otherwise
    dead = []
    for i in range(1, 4):
        p = _create_header_only_dump(
            dumps_dir, f"{DUMP_PREFIX}2026071{i}T000000Z{DUMP_SUFFIX}"
        )
        os.utime(p, (1000 + i, 1000 + i))
        dead.append(p)

    removed = rotate_dumps(max_dumps=3, dumps_dir=dumps_dir, is_pid_alive=_live_pid)
    # All four dumps carry PID 12345; with every owner "alive" nothing is
    # sacrificed regardless of rotation pressure.
    assert removed == 0
    assert live.exists() and all(p.exists() for p in dead)


def test_rotate_never_victimizes_foreign_domain_dumps(dumps_dir: Path) -> None:
    # GPT round: a foreign-domain owner (another host/namespace sharing the
    # data home) may be a LIVE gateway whose faulthandler holds this file's
    # fd — and that cannot be checked from here. Rotation must never unlink
    # its path (evidence would land on an unreachable inode); the owner's own
    # domain rotates it. Legacy domain-less files carry no such live-fd claim
    # and stay reapable, so unattributable litter is still bounded.
    foreign = _create_header_only_dump(
        dumps_dir,
        f"{DUMP_PREFIX}20260711T000000Z{DUMP_SUFFIX}",
        pid_domain="other-host/pid:[4026531836]",
    )
    os.utime(foreign, (1001, 1001))
    legacy = _create_header_only_dump(
        dumps_dir, f"{DUMP_PREFIX}20260712T000000Z{DUMP_SUFFIX}", pid_domain=""
    )
    os.utime(legacy, (1002, 1002))
    real = _create_stacked_dump(dumps_dir, f"{DUMP_PREFIX}20260710T000000Z{DUMP_SUFFIX}")
    os.utime(real, (1000, 1000))  # oldest — kept anyway: stacked evidence

    removed = rotate_dumps(max_dumps=2, dumps_dir=dumps_dir, is_pid_alive=_live_pid)
    # 3 files, cap 2 => excess computed as 2, but the foreign dump is excluded
    # from the victim list: only the legacy header-only file is sacrificed.
    assert removed == 1
    assert foreign.exists()
    assert not legacy.exists()
    assert real.exists()


def test_owner_alive_detects_pid_reuse_via_start_id(dumps_dir: Path) -> None:
    # GPT round: a live PID is not proof of a live OWNER — the kernel can
    # recycle the recorded PID for an unrelated process. A header that
    # recorded a start ID differing from the live process's start ID means
    # the owner is dead; its file must be protectable no longer.
    if crash_dump_store._pid_start_id(os.getpid()) is None:
        pytest.skip("no procfs start-id probe on this platform")
    # Use a REAL live process (the parent) so the start-id probe returns a
    # value; record a fabricated start id that cannot match it.
    reused_pid = os.getppid()
    p = dumps_dir / f"{DUMP_PREFIX}20260717T110000Z{DUMP_SUFFIX}"
    p.write_text(
        "# KiroCrew loop-stall crash dump — opened 20260717T110000Z\n"  # brand-ok: mirrors production dump header
        f"# PID: {reused_pid} @ {crash_dump_store._pid_domain()} start=fabricated-mismatch\n"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n"
    )
    assert crash_dump_store._owner_alive(p, _live_pid) is False
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_live_pid)
    assert removed == 1
    assert not p.exists()


def test_owner_alive_without_recorded_start_id_trusts_liveness(dumps_dir: Path) -> None:
    # Legacy headers (no start= token) fall back to plain PID liveness —
    # conservative: a live PID protects the file.
    p = _create_header_only_dump(dumps_dir, f"{DUMP_PREFIX}20260717T120000Z{DUMP_SUFFIX}")
    assert crash_dump_store._owner_alive(p, _live_pid) is True
    removed = sweep_stale_dumps(dumps_dir, is_pid_alive=_live_pid)
    assert removed == 0
    assert p.exists()
