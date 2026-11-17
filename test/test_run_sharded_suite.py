"""``scripts/run_sharded_suite.py`` sizes its parts from memory and reports every red part."""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

import pytest

from scripts import run_sharded_suite as runner


def test_the_plan_fits_the_parts_in_the_memory_budget() -> None:
    shards = runner.plan(jobs=8, available=16.0)

    per_part = runner.PROCESS_FIXED_GIB + runner.SUITE_GIB / shards
    assert 8 * per_part <= 16.0 * 0.8


def test_the_plan_never_has_fewer_parts_than_jobs() -> None:
    assert runner.plan(jobs=32, available=10_000.0) == 32


def test_an_unknown_memory_reading_gives_four_parts_per_job() -> None:
    assert runner.plan(jobs=3, available=None) == 12


def test_jobs_that_cannot_fit_at_all_are_refused() -> None:
    with pytest.raises(SystemExit, match="smaller --jobs"):
        runner.plan(jobs=64, available=10.0)


def test_each_part_collects_only_its_own_files(tmp_path: Path) -> None:
    command = runner.part_command(3, 16, 0, tmp_path, ["-m", "not slow"], tmp_path / "p.txt")

    assert command[1:5] == ["-m", "pytest", "-p", "scripts.ci_file_shards"]
    assert command[5:9] == ["--file-shards", "16", "--file-shard", "3"]
    assert command[9] == f"--file-shard-pins={tmp_path / 'p.txt'}"
    assert command[10:12] == ["-n", "0"]
    assert f"--junitxml={tmp_path / 'junit-3.xml'}" in command
    assert command[-2:] == ["-m", "not slow"]


def test_junit_counts_read_failures_and_errors(tmp_path: Path) -> None:
    (tmp_path / "junit-1.xml").write_text(
        '<testsuites><testsuite tests="5" failures="1" errors="2"/></testsuites>',
        encoding="utf-8",
    )

    assert runner.junit_counts(tmp_path / "junit-1.xml") == (5, 3)
    assert runner.junit_counts(tmp_path / "missing.xml") == (0, 0)


def _run_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, codes: dict[int, int]) -> int:
    monkeypatch.setattr(runner, "run_part", lambda index, *_args: (index, codes.get(index, 0), 0.0))
    # The real tree's groups are not what these tests are about; keep them off disk.
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    return runner.main(["--jobs", "2", "--shards", str(len(codes)), "--out", str(tmp_path)])


def test_a_relative_out_is_resolved_before_the_parts_change_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every part runs from the repository root, so a relative pin path would miss."""
    seen: list[tuple[Path, Path]] = []

    def part(index, _shards, _workers, out, _extra, pins, _parts=None):
        seen.append((out, pins))
        return index, 0, 0.0

    monkeypatch.setattr(runner, "run_part", part)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    monkeypatch.chdir(tmp_path)

    assert runner.main(["--jobs", "1", "--shards", "1", "--out", "results"]) == 0

    ((out, pins),) = seen
    assert out == tmp_path / "results" and out.is_absolute()
    assert pins == out / "group-pins.txt" and pins.is_file()


def test_a_part_that_collected_nothing_is_not_red(monkeypatch, tmp_path: Path) -> None:
    assert _run_with(monkeypatch, tmp_path, {1: 0, 2: runner.NO_TESTS_COLLECTED}) == 0


def test_a_run_where_no_part_collected_anything_is_red(monkeypatch, tmp_path: Path) -> None:
    codes = {1: runner.NO_TESTS_COLLECTED, 2: runner.NO_TESTS_COLLECTED}

    assert _run_with(monkeypatch, tmp_path, codes) == runner.NO_TESTS_COLLECTED


def test_one_red_part_makes_the_run_red(monkeypatch, tmp_path: Path, capsys) -> None:
    assert _run_with(monkeypatch, tmp_path, {1: 0, 2: 1, 3: 0}) == 1
    assert "red: part 2 (exit 1)" in capsys.readouterr().out


def _tree(root: Path, files: dict[str, str]) -> Path:
    (root / "setup.cfg").write_text("[tool:pytest]\ntestpaths = test pkg\n", encoding="utf-8")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_files_joined_by_a_group_share_one_part_and_a_lone_group_file_is_not_pinned(
    tmp_path: Path,
) -> None:
    root = _tree(
        tmp_path,
        {
            "test/test_a.py": 'pytestmark = pytest.mark.xdist_group(name="serial")\n',
            "test/test_b.py": "@pytest.mark.xdist_group('serial')\n"
            '@pytest.mark.xdist_group("spawn")\n',
            "pkg/x/tests/test_c.py": '@pytest.mark.xdist_group( name = "spawn" )\n',
            "test/test_alone.py": 'pytestmark = pytest.mark.xdist_group("only_here")\n',
            "test/test_plain.py": "def test_x(): pass\n",
        },
    )

    pins = runner.group_pins(root, 32)

    # a and c share no group, but b joins them, so all three run in one part
    assert set(pins) == {"test/test_a.py", "test/test_b.py", "pkg/x/tests/test_c.py"}
    assert len(set(pins.values())) == 1
    assert runner.group_pins(root, 32) == pins


def test_the_pin_file_is_what_ci_file_shards_reads(tmp_path: Path) -> None:
    from scripts import ci_file_shards

    path = runner.write_pins({"test/test_a.py": 3, "test/test_b.py": 3}, 4, tmp_path / "p.txt")

    assert ci_file_shards.read_pins(path, 4) == {"test/test_a.py": 3, "test/test_b.py": 3}


def test_every_part_reads_the_group_pins(tmp_path: Path) -> None:
    command = runner.part_command(1, 4, 0, tmp_path, [], tmp_path / "p.txt")

    assert f"--file-shard-pins={tmp_path / 'p.txt'}" in command


def test_a_stopped_run_starts_no_further_part(tmp_path: Path) -> None:
    parts = runner.Parts()
    parts.stop(grace=0)

    with open(tmp_path / "log", "wb") as log:
        assert parts.launch([sys.executable, "-c", "pass"], log) is None


#: POSIX: a child reports through a FIFO it keeps open, so the test's read blocks first
#: for the child's pid and then for its exit, with no clock.
_HAS_FIFO = hasattr(os, "mkfifo") and hasattr(os, "killpg")

#: A child that writes its pid to ``argv[1]`` (a FIFO the test reads, or a marker file),
#: keeps it open and blocks until it is killed.
_REPORTER = (
    "import os, sys, threading\n"
    "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT)\n"
    "os.write(fd, str(os.getpid()).encode())\n"
    "threading.Event().wait()\n"
)


def _fifo(path: Path) -> Path:
    os.mkfifo(path)
    return path


def _read_pid(fifo: Path) -> tuple[int | None, int]:
    """``(pid, fd)``: blocks until a child writes its pid; pid ``None`` if unblocked empty."""
    fd = os.open(fifo, os.O_RDONLY)
    data = os.read(fd, 64)
    return (int(data) if data else None), fd


def _unblock(fifo: Path) -> None:
    """End a ``_read_pid`` still waiting on *fifo*: open and close its write end."""
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass  # no reader left waiting


def _wait_exit(fd: int) -> None:
    """Block until the child holding *fd*'s write end has exited (EOF); the timeout bounds it."""
    try:
        while os.read(fd, 64):
            pass
    finally:
        os.close(fd)


def _sigterm_after(fifos: list[Path], fds: list[int]) -> threading.Thread:
    """Read a pid from each FIFO in order, then SIGTERM this process."""

    def run() -> None:
        for fifo in fifos:
            pid, fd = _read_pid(fifo)
            fds.append(fd)
            if pid is None:
                return
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _run_main(argv: list[str], fifos: list[Path]) -> int:
    try:
        return runner.main(argv)
    finally:
        for fifo in fifos:
            _unblock(fifo)


@pytest.mark.skipif(not _HAS_FIFO, reason="needs process groups and FIFOs (POSIX)")
@pytest.mark.timeout(60)
def test_sigterm_stops_the_running_part_and_starts_none_of_the_queued(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One job, three parts: a stop during part 1 must not let parts 2 and 3 start."""
    (tmp_path / "reporter.py").write_text(_REPORTER, encoding="utf-8")
    first = _fifo(tmp_path / "started-1")

    def command(index, *_args):
        return [sys.executable, str(tmp_path / "reporter.py"), str(tmp_path / f"started-{index}")]

    monkeypatch.setattr(runner, "part_command", command)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    fds: list[int] = []

    stopper = _sigterm_after([first], fds)
    rc = _run_main(["--jobs", "1", "--shards", "3", "--out", str(tmp_path / "o")], [first])
    stopper.join()

    assert rc == 128 + signal.SIGTERM
    assert not (tmp_path / "started-2").exists() and not (tmp_path / "started-3").exists()
    (part,) = fds
    _wait_exit(part)


@pytest.mark.skipif(not _HAS_FIFO or not hasattr(os, "waitid"), reason="needs waitid and FIFOs")
@pytest.mark.timeout(60)
def test_a_child_that_ignores_sigterm_is_killed_after_its_part_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The part dies on SIGTERM at once; what it started must not outlive the stop."""
    (tmp_path / "stubborn.py").write_text(
        "import signal\n" "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + _REPORTER,
        encoding="utf-8",
    )
    (tmp_path / "part.py").write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "sys.argv[1:] = sys.argv[3:]\n" + _REPORTER,
        encoding="utf-8",
    )
    child, part = _fifo(tmp_path / "child"), _fifo(tmp_path / "part")

    def command(index, *_args):
        return [
            sys.executable,
            str(tmp_path / "part.py"),
            str(tmp_path / "stubborn.py"),
            str(child),
            str(part),
        ]

    monkeypatch.setattr(runner, "part_command", command)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    monkeypatch.setattr(runner, "STOP_GRACE_SECS", 0)
    fds: list[int] = []

    # The child reports only after it ignores SIGTERM, and the part only after that.
    stopper = _sigterm_after([child, part], fds)
    rc = _run_main(["--jobs", "1", "--shards", "1", "--out", str(tmp_path / "o")], [child, part])
    stopper.join()

    assert rc == 128 + signal.SIGTERM
    stubborn, _part = fds
    _wait_exit(stubborn)
    _wait_exit(_part)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
@pytest.mark.timeout(60)
def test_a_part_that_cannot_start_stops_the_queued_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "reporter.py").write_text(_REPORTER, encoding="utf-8")

    def command(index, *_args):
        if index == 1:
            return [str(tmp_path / "no-such-interpreter")]
        return [sys.executable, str(tmp_path / "reporter.py"), str(tmp_path / f"started-{index}")]

    monkeypatch.setattr(runner, "part_command", command)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})

    with pytest.raises(FileNotFoundError):
        runner.main(["--jobs", "1", "--shards", "3", "--out", str(tmp_path / "o")])

    assert not (tmp_path / "started-2").exists() and not (tmp_path / "started-3").exists()


@pytest.fixture
def parts():
    """A ``Parts`` whose launched groups are killed and reaped however the test ends."""
    held = runner.Parts()
    yield held
    held.stop(grace=0)
    for child in list(held.live):
        child.wait()


@pytest.mark.skipif(not _HAS_FIFO or not hasattr(os, "waitid"), reason="needs waitid and FIFOs")
@pytest.mark.timeout(60)
def test_what_a_finished_part_left_running_is_killed_before_its_reap(
    tmp_path: Path, parts: runner.Parts
) -> None:
    """A part's id is its group's; once reaped it could name someone else's group."""
    (tmp_path / "reporter.py").write_text(_REPORTER, encoding="utf-8")
    child, go = _fifo(tmp_path / "child"), _fifo(tmp_path / "go")
    # The part starts the child, then exits once the test has the child's pid.
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "open(sys.argv[3]).read()\n"
    )
    with open(tmp_path / "log", "wb") as log:
        part = parts.launch(
            [sys.executable, "-c", script, str(tmp_path / "reporter.py"), str(child), str(go)],
            log,
        )
    report = None
    try:
        _pid, report = _read_pid(child)
        with open(go, "w", encoding="ascii"):
            pass

        assert parts.wait(part) == 0
        assert not parts.live
        fd, report = report, None
        _wait_exit(fd)
    finally:
        # An interrupted handshake: the fixture kills the group; free the FIFO ends here.
        _unblock(child)
        if report is not None:
            os.close(report)


def test_the_first_signal_ignores_the_next_before_the_stop_begins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A double Ctrl-C must not raise again inside the stop and leave parts running."""
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(runner.signal, "signal", lambda sig, handler: calls.append((sig, handler)))

    with pytest.raises(runner.Interrupted):
        runner._raise_interrupted(signal.SIGINT, None)

    assert sorted(calls) == sorted(
        [(signal.SIGINT, signal.SIG_IGN), (signal.SIGTERM, signal.SIG_IGN)]
    )


@pytest.mark.parametrize("waitid", [True, False], ids=["waitid", "no-waitid"])
def test_a_part_is_waited_for_and_forgotten_on_every_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, waitid: bool, parts: runner.Parts
) -> None:
    """Windows and macOS before 3.13 have no ``os.waitid``; the part must still be reaped."""
    if not waitid:
        monkeypatch.delattr(runner.os, "waitid", raising=False)
    with open(tmp_path / "log", "wb") as log:
        part = parts.launch([sys.executable, "-c", "import sys; sys.exit(3)"], log)

    assert parts.wait(part) == 3
    assert not parts.live
