"""The loop-stall dump names the event-loop thread even past faulthandler's thread limit.

faulthandler lists at most 100 threads, newest first, so in a gateway with more
threads the main thread, where the event loop runs, is the one it leaves out.
The watchdog writes the loop thread's own block into the dump file before the
alarm fires, and the readers take that block; with neither, they say the loop
thread is missing instead of naming the 100th thread.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard import crash_dump_store
from kiro_crew.dashboard.crash_dump_store import (
    LOOP_THREAD_HEADER,
    LOOP_THREAD_MISSING_NOTE,
    DumpFile,
    dump_first_stack_lines,
    dump_replay_lines,
    dump_wedged_frames,
)
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_HEADER = (
    "# KiroCrew loop-stall crash dump — opened 20261107T000000Z\n"  # brand-ok: mirrors production dump header
    "# PID: 12345\n"
    "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
    "\n"
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def dump_file(tmp_path: Path) -> Iterator[DumpFile]:
    path = tmp_path / "loopstall-20261107T000000Z.txt"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    os.write(fd, _HEADER.encode("utf-8"))
    try:
        yield DumpFile(fd, path)
    finally:
        os.close(fd)


def _armed_watchdog(dump_file: DumpFile, clock: _Clock) -> LoopStallWatchdog:
    """Alarm armed through recording hooks (no real timer), daemon thread idle."""
    wd = LoopStallWatchdog(
        stall_after=30.0,
        exit_after=25.0,
        poll_interval=3600.0,
        now=clock,
        dump=lambda: None,
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
        dump_file=dump_file,
        enrich_after=15.0,
        enrich=lambda _s: [],
        log=logging.getLogger("test.loop_stall_dump_loop_thread"),
    )
    wd.start()
    return wd


def _content(dump_file: DumpFile) -> str:
    return dump_file.path.read_bytes().decode("utf-8")


def test_a_stall_writes_the_loop_threads_stack_and_recovery_removes_it(
    dump_file: DumpFile,
) -> None:
    clock = _Clock()
    wd = _armed_watchdog(dump_file, clock)
    try:
        wd.beat()  # this test's thread is the loop thread
        clock.t += 16.0
        wd.check()
        written = _content(dump_file)
        assert f"{LOOP_THREAD_HEADER} 0x" in written
        assert "test_a_stall_writes_the_loop_threads_stack_and_recovery_removes_it" in written
        frames = dump_wedged_frames(dump_file.path)
        assert frames[0].startswith(LOOP_THREAD_HEADER)

        wd.beat()
        wd.check()
        assert _content(dump_file) == _HEADER
        assert crash_dump_store._is_header_only(dump_file.path)
        # The next write lands right after the header, with no gap.
        os.write(dump_file.fileno(), b"Thread 0x1 (most recent call first):\n")
        assert _content(dump_file) == _HEADER + "Thread 0x1 (most recent call first):\n"
    finally:
        wd.stop()


def test_an_orderly_stop_mid_stall_leaves_the_file_header_only(dump_file: DumpFile) -> None:
    clock = _Clock()
    wd = _armed_watchdog(dump_file, clock)
    wd.beat()
    clock.t += 16.0
    wd.check()
    assert LOOP_THREAD_HEADER in _content(dump_file)
    wd.stop()
    assert _content(dump_file) == _HEADER


def test_an_in_place_restart_right_after_a_recovered_stall_leaves_the_file_header_only(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    from kiro_crew import pre_exec_stops
    from kiro_crew.dashboard import crash_dump_store as store
    from kiro_crew.dashboard import server

    # Owner functions run on the server module's globals: patch there.
    monkeypatch.setattr(server, "sweep_stale_dumps", lambda: None)
    monkeypatch.setattr(server, "rotate_dumps", lambda: None)
    monkeypatch.setattr(server, "open_dump_file", lambda: store.open_dump_file(tmp_path))
    monkeypatch.setattr(server, "LoopStallWatchdog", _NoAlarmWatchdog)
    monkeypatch.setattr(server, "load_loop_stall_exit_after", lambda _e: 25)

    wd = asyncio.run(server._open_loop_watchdog({}))
    try:
        wd.start()
        wd.beat()
        wd._now.t += 16.0
        wd.check()
        assert LOOP_THREAD_HEADER in wd._dump_file.path.read_text(encoding="utf-8")
        wd.beat()  # recovered; the daemon thread has not polled yet
        asyncio.run(pre_exec_stops.run_pre_exec_stops())
        assert store._is_header_only(wd._dump_file.path)
    finally:
        wd.stop()
        os.close(wd._dump_file.fileno())


class _NoAlarmWatchdog(LoopStallWatchdog):
    """The production watchdog with recording alarm hooks and a fake clock."""

    def __init__(self, **kwargs) -> None:
        super().__init__(
            stall_after=30.0,
            poll_interval=3600.0,
            now=_Clock(),
            dump=lambda: None,
            arm_later=lambda _t: None,
            cancel_later=lambda: None,
            enrich_after=15.0,
            enrich=lambda _s: [],
            **kwargs,
        )


def _faulthandler_dump(threads: int, *, cut: bool) -> str:
    """A dump shaped like faulthandler's: newest thread first, ``...`` past the limit."""
    out = []
    for n in range(threads):
        out.append(f"Thread 0x{n + 1:016x} (most recent call first):\n")
        out.append(f'  File "/usr/lib/python3.12/threading.py", line {n + 10} in idle_{n}\n')
        out.append("\n")
    if cut:
        out.append("...\n")
    return "".join(out)


def test_a_dump_cut_at_the_thread_limit_does_not_name_the_100th_thread(tmp_path: Path) -> None:
    path = tmp_path / "loopstall-20261107T000001Z.txt"
    path.write_text(_HEADER + _faulthandler_dump(100, cut=True), encoding="utf-8")

    assert dump_first_stack_lines(path, max_lines=8) == [LOOP_THREAD_MISSING_NOTE]
    assert dump_wedged_frames(path) == []
    replay, _truncated = dump_replay_lines(path)
    assert replay[0] == LOOP_THREAD_MISSING_NOTE


def test_a_dump_that_lists_every_thread_still_names_the_last_one(tmp_path: Path) -> None:
    path = tmp_path / "loopstall-20261107T000002Z.txt"
    path.write_text(_HEADER + _faulthandler_dump(3, cut=False), encoding="utf-8")

    assert "idle_2" in "\n".join(dump_first_stack_lines(path, max_lines=4))


def test_a_main_thread_past_the_frame_limit_is_not_read_as_a_cut_dump(tmp_path: Path) -> None:
    path = tmp_path / "loopstall-20261107T000004Z.txt"
    deep_main = (
        "Thread 0x0000000000000001 (most recent call first):\n"
        + "".join(f'  File "/srv/kiro_crew/deep.py", line {n} in deep_{n}\n' for n in range(100))
        + "  ...\n"
    )
    path.write_text(_HEADER + _faulthandler_dump(1, cut=False) + deep_main, encoding="utf-8")

    assert "deep_0" in "\n".join(dump_first_stack_lines(path, max_lines=4))
    assert dump_wedged_frames(path)


def test_a_beat_leaves_the_file_alone_and_the_next_poll_drops_the_block(
    dump_file: DumpFile,
) -> None:
    clock = _Clock()
    wd = _armed_watchdog(dump_file, clock)
    try:
        wd.beat()
        clock.t += 16.0
        wd.check()
        written = _content(dump_file)
        wd.beat()  # on the loop thread: no file I/O
        assert _content(dump_file) == written
        clock.t += 1.0
        wd.check()  # a new silence's first poll is below enrich_after
        assert _content(dump_file) == _HEADER
    finally:
        wd.stop()


def test_faulthandlers_stack_of_the_loop_thread_beats_an_older_loop_block(tmp_path: Path) -> None:
    path = tmp_path / "loopstall-20261107T000005Z.txt"
    stale = (
        f"{LOOP_THREAD_HEADER} 0x0000000000000abc (most recent call first):\n"
        '  File "/srv/kiro_crew/a.py", line 1 in recovered_earlier\n'
    )
    fatal = (
        "Thread 0x0000000000000def (most recent call first):\n"
        '  File "/usr/lib/python3.12/threading.py", line 1 in idle\n'
        "Current thread 0x0000000000000abc (most recent call first):\n"
        '  File "/srv/kiro_crew/b.py", line 2 in fatal_now\n'
    )
    path.write_text(_HEADER + stale + fatal, encoding="utf-8")
    assert "fatal_now" in "\n".join(dump_wedged_frames(path))

    # faulthandler's timer path labels the same thread ``Thread``.
    timer = fatal.replace("Current thread 0x0000000000000abc", "Thread 0x0000000000000abc")
    path.write_text(_HEADER + stale + timer, encoding="utf-8")
    assert "fatal_now" in "\n".join(dump_wedged_frames(path))

    other = fatal.replace("Current thread 0x0000000000000abc", "Current thread 0x0000000000000fed")
    path.write_text(_HEADER + stale + other, encoding="utf-8")
    assert "recovered_earlier" in "\n".join(dump_wedged_frames(path))


def test_a_block_written_after_a_beat_drops_itself(dump_file: DumpFile, monkeypatch) -> None:
    clock = _Clock()
    wd = _armed_watchdog(dump_file, clock)
    real_write = os.write

    def write_then_beat(fd: int, data: bytes) -> int:
        written = real_write(fd, data)
        wd.beat()  # the loop recovers while the block is being written
        return written

    try:
        wd.beat()
        clock.t += 16.0
        monkeypatch.setattr(os, "write", write_then_beat)
        wd.check()
        monkeypatch.setattr(os, "write", real_write)
        assert _content(dump_file) == _HEADER
    finally:
        wd.stop()


def test_a_block_written_after_stop_began_drops_itself(dump_file: DumpFile, monkeypatch) -> None:
    clock = _Clock()
    wd = _armed_watchdog(dump_file, clock)
    real_write = os.write

    def write_then_stop(fd: int, data: bytes) -> int:
        written = real_write(fd, data)
        wd.stop()  # shutdown runs its cleanup before the writer records its offset
        return written

    wd.beat()
    clock.t += 16.0
    monkeypatch.setattr(os, "write", write_then_stop)
    wd.check()
    monkeypatch.setattr(os, "write", real_write)
    assert _content(dump_file) == _HEADER


def test_the_watchdogs_loop_block_wins_over_a_cut_dump(tmp_path: Path) -> None:
    path = tmp_path / "loopstall-20261107T000003Z.txt"
    loop_block = (
        "# Event-loop thread after 15s of silence, written by the loop watchdog: "
        "120 threads were alive; only the newest 100 are listed after this block.\n"
        f"{LOOP_THREAD_HEADER} 0x00007f0000000abc (most recent call first):\n"
        '  File "/srv/kiro_crew/history_search.py", line 505 in _list_sessions_status\n'
        "\n"
    )
    path.write_text(_HEADER + loop_block + _faulthandler_dump(100, cut=True), encoding="utf-8")

    lines = dump_first_stack_lines(path, max_lines=8)
    assert lines[0].startswith("# Event-loop thread after 15s")
    assert lines[1].startswith(LOOP_THREAD_HEADER)
    assert "_list_sessions_status" in lines[2]
    assert "_list_sessions_status" in dump_wedged_frames(path)[1]


_CHILD = textwrap.dedent("""
    import logging, os, sys, threading, time
    from kiro_crew.dashboard.crash_dump_store import DumpFile
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
    path = sys.argv[1]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    os.write(fd, b"# h1\\n# h2\\n# h3\\n\\n")
    gate = threading.Event()
    for _ in range(120):
        threading.Thread(target=gate.wait, daemon=True).start()
    wd = LoopStallWatchdog(
        stall_after=60.0, exit_after=50.0, poll_interval=0.1, enrich_after=0.2,
        dump_file=DumpFile(fd, path), enrich=lambda _s: [], log=logging.getLogger("t"),
    )
    wd.start()
    wd.beat()

    def wedged_loop_frame():
        # Wait for the signal (the block is on file), not a timer race.
        deadline = time.monotonic() + 20.0
        while wd._loop_stack_offset is None:
            if time.monotonic() > deadline:
                sys.exit(4)
            time.sleep(0.05)
        # Park the daemon thread in a long wait (stopping it would drop the
        # block), so the alarm's dump never walks a thread running Python;
        # wait until it is in that wait, then arm the short exit.
        wd._poll_interval = 3600.0

        def parked():
            f = sys._current_frames().get(wd._thread.ident)
            while f is not None:
                if f.f_code.co_name == "wait" and f.f_locals.get("timeout") == 3600.0:
                    return True
                f = f.f_back
            return False

        while not parked():
            if time.monotonic() > deadline:
                sys.exit(5)
            time.sleep(0.05)
        wd._rearm_alarm(0.3)
        time.sleep(20.0)

    wedged_loop_frame()
    print("still alive", file=sys.stderr)
    sys.exit(3)
    """)


def test_a_real_alarm_dump_with_more_than_100_threads_holds_the_loop_thread(
    tmp_path: Path,
) -> None:
    dump = tmp_path / "loopstall.txt"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(dump)],
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        cwd=str(cwd),
        **UTF8_TEXT,
    )
    expected = -signal.SIGALRM if platform_compat.process_alarm_available() else 1
    text = dump.read_text(encoding="utf-8", errors="replace")
    assert proc.returncode == expected, (proc.returncode, proc.stderr, text[-3000:])
    assert text.rstrip().endswith("..."), text[-500:]  # faulthandler cut at 100
    lines = dump_first_stack_lines(dump, max_lines=4)
    assert lines[1].startswith(LOOP_THREAD_HEADER), lines
    assert "wedged_loop_frame" in "\n".join(dump_wedged_frames(dump))
