"""``scripts/run_sharded_suite.py`` sizes its parts from memory and reports every red part."""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

import pytest
from defusedxml.ElementTree import parse

from scripts import run_sharded_suite as runner
from scripts.ci_file_shards import file_shard, hash_shard, read_targets

REPO_ROOT = Path(__file__).resolve().parents[1]

#: POSIX: a child reports through a FIFO it keeps open, so the test's read blocks first
#: for the child's pid and then for its exit, with no clock.
_HAS_FIFO = hasattr(os, "mkfifo") and hasattr(os, "killpg")


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


def _junit(out: Path, index: int, tests: int = 1) -> None:
    """The JUnit file a part that ran *tests* tests leaves."""
    (out / f"junit-{index}.xml").write_text(
        f'<testsuites><testsuite tests="{tests}" failures="0" errors="0"/></testsuites>',
        encoding="utf-8",
    )


def _run_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, codes: dict[int, int]) -> int:
    def part(index, *_args):
        if codes.get(index, 0) == 0:
            _junit(tmp_path, index)
        return index, codes.get(index, 0), 0.0

    monkeypatch.setattr(runner, "run_part", part)
    # The real tree's groups are not what these tests are about; keep them off disk.
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    return runner.main(["--jobs", "2", "--shards", str(len(codes)), "--out", str(tmp_path)])


def test_a_relative_out_is_resolved_before_the_parts_change_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every part runs from the repository root, so a relative pin path would miss."""
    seen: list[tuple[Path, Path]] = []

    def part(index, _shards, _workers, out, _extra, pins, *_rest):
        seen.append((out, pins))
        _junit(out, index)
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


def test_a_part_that_exits_zero_without_a_result_is_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Exit 0 alone is not a verdict: the part must have counted a test."""
    monkeypatch.setattr(runner, "run_part", lambda index, *_args: (index, 0, 0.0))
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    _junit(tmp_path, 1)

    assert runner.main(["--jobs", "2", "--shards", "2", "--out", str(tmp_path)]) == 1

    out = capsys.readouterr().out
    assert f"red: part 2 (exit {runner.PART_NO_RESULT}" in out
    assert "red: part 1" not in out


#: A stand-in part: ``argv[1]`` is what it does, ``argv[2]`` the JUnit file it may write.
_PART = """\
import os, signal, sys, threading
action, junit = sys.argv[1], sys.argv[2]
if action == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
if action == "hang":
    print("stuck in part", flush=True)
    if len(sys.argv) > 3:
        os.write(os.open(sys.argv[3], os.O_WRONLY), str(os.getpid()).encode())
    threading.Event().wait()
if action in ("pass", "pass-with-coverage"):
    with open(junit, "w", encoding="utf-8") as stream:
        stream.write('<testsuites><testsuite tests="2" failures="0" errors="0"/></testsuites>')
if action == "pass-with-coverage":
    from coverage import CoverageData
    data = CoverageData(basename=os.environ["COVERAGE_FILE"])
    data.add_lines({sys.argv[3]: [int(sys.argv[4])]})
    data.write()
sys.exit(5 if action == "empty" else 0)
"""


def _stand_in_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, actions: dict[int, list[str]]
) -> None:
    """Make part ``i`` a real child process that does ``actions[i]``."""
    script = tmp_path / "part.py"
    script.write_text(_PART, encoding="utf-8")

    def command(index, _shards, _workers, out, *_rest):
        action, *more = actions[index]
        return [sys.executable, str(script), action, str(out / f"junit-{index}.xml"), *more]

    monkeypatch.setattr(runner, "part_command", command)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="a part killed by a signal is POSIX")
@pytest.mark.timeout(60)
def test_a_part_that_dies_makes_the_run_red_and_the_other_parts_still_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _stand_in_parts(monkeypatch, tmp_path, {1: ["crash"], 2: ["pass"], 3: ["empty"]})

    rc = runner.main(["--jobs", "1", "--shards", "3", "--out", str(tmp_path / "o")])

    out = capsys.readouterr().out
    assert rc == 1
    assert f"red: part 1 (exit {-signal.SIGKILL})" in out
    assert "part 2/3: exit 0, 2 tests" in out
    assert f"part 3/3: exit {runner.NO_TESTS_COLLECTED}" in out and "red: part 3" not in out


class _Clock:
    """The script's clock: it moves only when a test moves it, never with the wall."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    held = _Clock()
    monkeypatch.setattr(runner, "_now", held)
    return held


def _once_started(fifo: Path, then, fds: list[int]) -> threading.Thread:
    """Call *then* once the part reporting through *fifo* is running."""

    def run() -> None:
        pid, fd = _read_pid(fifo)
        fds.append(fd)
        if pid is not None:
            then()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _main_until_started(argv: list[str], fifo: Path, then) -> int:
    """``main(argv)``, calling *then* once the hanging part behind *fifo* is running."""
    fds: list[int] = []
    waiter = _once_started(fifo, then, fds)
    try:
        return _run_main(argv, [fifo])
    finally:
        waiter.join()
        for fd in fds:
            _wait_exit(fd)


@pytest.mark.skipif(not _HAS_FIFO, reason="needs process groups and FIFOs (POSIX)")
@pytest.mark.timeout(60)
def test_a_part_that_outlives_its_bound_is_killed_and_reported_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys, clock: _Clock
) -> None:
    """One job: the passing part has ended before the hanging one starts. The script
    reads the hanging part's start before it launches it, so its bound is already set
    when the part reports that it runs, which is when the clock moves past it."""
    hung = _fifo(tmp_path / "hung")
    _stand_in_parts(monkeypatch, tmp_path, {1: ["pass"], 2: ["hang", str(hung)]})
    argv = ["--jobs", "1", "--shards", "2", "--part-timeout", "600", "--out", str(tmp_path / "o")]
    moved = threading.Event()
    launch = runner.Parts.launch

    def launch_then_hold(self, command, log, env=None):
        """The worst schedule: nothing after the hanging part's launch runs until the
        clock has moved, so a bound set after the launch would never be reached."""
        child = launch(self, command, log, env)
        if "hang" in command:
            moved.wait()
        return child

    monkeypatch.setattr(runner.Parts, "launch", launch_then_hold)

    def move_the_clock() -> None:
        clock.advance(601)
        moved.set()

    try:
        rc = _main_until_started(argv, hung, move_the_clock)
    finally:
        moved.set()

    out = capsys.readouterr().out
    assert rc == 1
    assert f"red: part 2 (exit {runner.PART_TIMED_OUT}: still running at --part-timeout" in out
    assert "part 1/2: exit 0, 2 tests" in out and "red: part 1" not in out


@pytest.mark.skipif(not _HAS_FIFO, reason="needs process groups and FIFOs (POSIX)")
@pytest.mark.timeout(60)
def test_the_runs_own_deadline_ends_a_hung_part_prints_its_log_and_starts_no_more(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd, clock: _Clock
) -> None:
    """A part that starts late still ends at the run's deadline, not a part-timeout later."""
    hung = _fifo(tmp_path / "hung")
    _stand_in_parts(monkeypatch, tmp_path, {1: ["pass"], 2: ["hang", str(hung)], 3: ["pass"]})
    out_dir = tmp_path / "o"
    argv = ["--jobs", "1", "--shards", "3", "--part-timeout", "3000", "--run-timeout", "600"]
    argv += ["--print-logs", "--out", str(out_dir)]

    def late_start() -> None:
        # The part has run 500 of its own 3000 seconds when the run's 600 are up.
        clock.advance(601)

    rc = _main_until_started(argv, hung, late_start)

    out = capfd.readouterr().out
    assert rc == 1
    assert "part 1/3: exit 0, 2 tests" in out
    assert f"red: part 2 (exit {runner.PART_TIMED_OUT}" in out
    assert "--- part 2/3 log\nstuck in part\n--- end of part 2/3 log" in out
    assert f"red: part 3 (exit {runner.PART_NOT_STARTED}: not started" in out
    assert not (out_dir / "junit-3.xml").exists()


@pytest.mark.skipif(not _HAS_FIFO, reason="needs process groups and FIFOs (POSIX)")
@pytest.mark.timeout(60)
def test_a_part_that_goes_quiet_has_its_log_printed_while_it_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd, clock: _Clock
) -> None:
    """A hung part's output must not wait for the part to end. The part writes its line
    before it reports that it runs. From then on, each look that has a size on record for
    the part moves the clock by one stall period: that look or the one before recorded the
    log's final size, the next prints it, and a later one passes the run's deadline."""
    hung = _fifo(tmp_path / "hung")
    _stand_in_parts(monkeypatch, tmp_path, {1: ["hang", str(hung)]})
    # How often the run looks, not a bound: the verdict reads the test's clock.
    monkeypatch.setattr(runner, "WATCH_POLL_SECS", 0.01)  # flake-ok: poll interval
    started = threading.Event()
    look = runner.Watch.look

    def look_then_move_the_clock(self) -> None:
        up = started.is_set()
        look(self)
        if up and 1 in self.quiet:
            clock.advance(runner.STALL_SECS + 1)

    monkeypatch.setattr(runner.Watch, "look", look_then_move_the_clock)
    argv = ["--jobs", "1", "--shards", "1", "--print-logs", "--out", str(tmp_path / "o")]
    argv += ["--run-timeout", str(10 * runner.STALL_SECS)]

    rc = _main_until_started(argv, hung, started.set)

    out = capfd.readouterr().out
    assert rc == 1
    early = out.index("--- part 1/1 log so far (nothing written for")
    assert "still running)\nstuck in part\n" in out[early:]
    assert early < out.index("part 1/1: exit")
    assert out.index("running: part 1 for") < out.index("part 1/1: exit")


@pytest.mark.skipif(not _HAS_FIFO, reason="needs process groups and FIFOs (POSIX)")
@pytest.mark.timeout(60)
def test_a_stop_prints_the_log_so_far_of_the_parts_it_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd
) -> None:
    """A cancelled CI job signals this script; what its parts wrote must reach stdout."""
    hung = _fifo(tmp_path / "hung")
    _stand_in_parts(monkeypatch, tmp_path, {1: ["hang", str(hung)], 2: ["pass"]})
    argv = ["--jobs", "1", "--shards", "2", "--print-logs", "--out", str(tmp_path / "o")]

    rc = _main_until_started(argv, hung, lambda: os.kill(os.getpid(), signal.SIGTERM))

    out = capfd.readouterr().out
    assert rc == 128 + signal.SIGTERM
    assert "--- part 1/2 log so far (still running at the stop)\nstuck in part\n" in out
    assert not (tmp_path / "o" / "junit-2.xml").exists()


@pytest.mark.parametrize("waitid", [True, False], ids=["waitid", "no-waitid"])
@pytest.mark.timeout(60)
def test_a_wait_ends_at_the_earlier_of_the_parts_bound_and_the_runs_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, waitid: bool, clock: _Clock
) -> None:
    if not waitid:
        monkeypatch.delattr(runner.os, "waitid", raising=False)
    bounded = runner.Parts(timeout=600, deadline=clock() + 60)  # flake-ok: the test's clock
    with open(tmp_path / "log", "wb") as log:
        part = bounded.launch(
            [sys.executable, "-c", "import threading; threading.Event().wait()"], log
        )
    clock.advance(61)

    assert bounded.wait(part) != 0
    assert part in bounded.overran and not bounded.live


@pytest.mark.timeout(60)
def test_a_parts_bound_counts_from_the_reading_taken_before_its_launch(
    tmp_path: Path, clock: _Clock
) -> None:
    """Whatever happens between the launch and the wait, the bound is the one armed first."""
    bounded = runner.Parts(timeout=600)  # flake-ok: the test's clock
    armed = clock()
    with open(tmp_path / "log", "wb") as log:
        part = bounded.launch(
            [sys.executable, "-c", "import threading; threading.Event().wait()"], log
        )
    clock.advance(601)

    assert bounded.wait(part, armed) != 0
    assert part in bounded.overran


def test_a_part_whose_turn_comes_after_the_runs_deadline_is_not_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: _Clock
) -> None:
    monkeypatch.setattr(runner, "part_command", lambda *_args: pytest.fail("must not start"))
    late = runner.Parts(deadline=clock())  # flake-ok: the test's clock

    assert runner.run_part(1, 1, 0, tmp_path, [], tmp_path / "p.txt", late) == (
        1,
        runner.PART_NOT_STARTED,
        0.0,
    )


def test_an_unbounded_wait_records_no_overrun(tmp_path: Path, parts: runner.Parts) -> None:
    with open(tmp_path / "log", "wb") as log:
        part = parts.launch([sys.executable, "-c", "pass"], log)

    assert parts.wait(part) == 0
    assert not parts.overran


@pytest.mark.timeout(120)
def test_each_part_writes_its_own_coverage_and_the_run_combines_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coverage import CoverageData

    source = tmp_path / "measured.py"
    source.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    actions = {
        1: ["pass-with-coverage", str(source), "1"],
        2: ["pass-with-coverage", str(source), "3"],
        3: ["empty"],
    }
    _stand_in_parts(monkeypatch, tmp_path, actions)
    target = tmp_path / "combined" / ".coverage"
    target.parent.mkdir()
    out = tmp_path / "o"

    rc = runner.main(
        ["--jobs", "3", "--shards", "3", "--out", str(out), "--coverage-file", str(target)]
    )

    assert rc == 0
    combined = CoverageData(basename=str(target))
    combined.read()
    (measured,) = combined.measured_files()
    assert sorted(combined.lines(measured)) == [1, 3]
    assert [path.name for path in target.parent.iterdir()] == [".coverage"]


@pytest.mark.timeout(120)
def test_a_part_that_ran_tests_without_coverage_data_is_red_and_nothing_is_combined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    source = tmp_path / "measured.py"
    source.write_text("a = 1\n", encoding="utf-8")
    _stand_in_parts(
        monkeypatch, tmp_path, {1: ["pass-with-coverage", str(source), "1"], 2: ["pass"]}
    )
    target = tmp_path / ".coverage"
    target.write_text("left by an earlier run", encoding="utf-8")
    stale = runner.coverage_dir(tmp_path / "o", 2)
    stale.mkdir(parents=True)
    (stale / ".coverage").write_text("left by an earlier run", encoding="utf-8")

    rc = runner.main(
        ["--jobs", "2", "--shards", "2", "--out", str(tmp_path / "o")]
        + ["--coverage-file", str(target)]
    )

    assert rc == 1
    assert "red: part 2 ran tests but left no coverage data" in capsys.readouterr().out
    assert not target.exists()


@pytest.mark.timeout(120)
def test_a_red_part_leaves_no_combined_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "measured.py"
    source.write_text("a = 1\n", encoding="utf-8")
    _stand_in_parts(
        monkeypatch, tmp_path, {1: ["pass-with-coverage", str(source), "1"], 2: ["fail"]}
    )
    target = tmp_path / ".coverage"

    rc = runner.main(
        ["--jobs", "2", "--shards", "2", "--out", str(tmp_path / "o")]
        + ["--coverage-file", str(target)]
    )

    assert rc == 1
    assert not target.exists()


def test_print_logs_copies_every_parts_log_to_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfdbinary
) -> None:
    def part(index, _shards, _workers, out, *_rest):
        (out / f"shard-{index}.log").write_bytes(f"output of part {index}\n".encode())
        _junit(out, index)
        return index, 0, 0.0

    monkeypatch.setattr(runner, "run_part", part)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    rc = runner.main(["--jobs", "1", "--shards", "2", "--print-logs", "--out", str(tmp_path)])
    shown = capfdbinary.readouterr().out.decode()

    assert rc == 0
    for index in (1, 2):
        assert f"::group::part {index}/2 log\noutput of part {index}\n::endgroup::" in shown


def test_the_outer_shards_own_every_file_exactly_once_and_honour_the_pins(
    tmp_path: Path,
) -> None:
    names = [f"test/test_{i}.py" for i in range(40)] + ["pkg/x/tests/data.yaml", "test/conftest.py"]
    root = _tree(tmp_path, dict.fromkeys(names, ""))
    moved = next(name for name in names if hash_shard(name, 4) != 2)
    pins = tmp_path / "pins.txt"
    pins.write_text(f"# shards: 4\n2 {moved}\n", encoding="utf-8")

    owned = {index: runner.outer_shard_files(root, 4, index, pins) for index in range(1, 5)}

    assert sorted(name for files in owned.values() for name in files) == sorted(names)
    assert moved in owned[2]
    for index, files in owned.items():
        for name in files:
            assert file_shard(root / name, root, 4, {moved: 2}) == index


def test_the_targets_file_is_what_ci_file_shards_reads(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"test/test_a.py": "", "test/sub/test_b.py": ""})

    path = runner.write_targets(["test/test_a.py", "test/sub/test_b.py"], tmp_path / "t.txt")

    assert read_targets(path, root) == {root / "test/test_a.py", root / "test/sub/test_b.py"}


def test_a_part_of_an_outer_shard_is_narrowed_to_that_shards_files(tmp_path: Path) -> None:
    targets = tmp_path / "t.txt"

    command = runner.part_command(1, 4, 0, tmp_path, ["-q"], tmp_path / "p.txt", targets)

    assert f"--file-shard-targets={targets}" in command
    assert command[-1] == "-q"
    assert not any(
        word.startswith("--file-shard-targets")
        for word in runner.part_command(1, 4, 0, tmp_path, ["-q"], tmp_path / "p.txt")
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--outer-shards", "8"],
        ["--outer-shard", "1"],
        ["--outer-shards", "8", "--outer-shard", "9"],
        ["--outer-pins", "pins.txt"],
        ["--part-timeout", "0"],
        ["--run-timeout", "0"],
    ],
    ids=["no-index", "no-count", "index-out-of-range", "pins-alone", "zero-bound", "zero-run"],
)
def test_a_half_named_outer_shard_or_a_zero_bound_is_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        runner.main(["--jobs", "1", "--shards", "1", *argv])

    assert refused.value.code == 2


def _ran(out: Path) -> set[str]:
    """``file::test`` of every test case in the parts' JUnit files."""
    cases = set()
    for path in out.glob("junit-*.xml"):
        for case in parse(path).getroot().iter("testcase"):
            cases.add(f"{case.get('classname')}::{case.get('name')}")
    return cases


@pytest.mark.timeout(300)
def test_an_outer_shard_runs_as_parts_that_cover_exactly_its_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Real pytest parts: the outer shard's tests run once each, and their coverage is one file."""
    from coverage import CoverageData

    first, second = (
        next(
            f"test/test_part_{i}.py"
            for i in range(200)
            if hash_shard(f"test/test_part_{i}.py", 2) == part
        )
        for part in (1, 2)
    )
    root = _tree(
        tmp_path / "tree",
        {
            "logic.py": "def choose(value):\n    if value:\n        return 'yes'\n    return 'no'\n",
            first: "from logic import choose\ndef test_yes(): assert choose(1) == 'yes'\n",
            second: "from logic import choose\ndef test_no(): assert choose(0) == 'no'\n",
            "test/test_elsewhere.py": "raise RuntimeError('owned by the other outer shard')\n",
        },
    )
    (root / "setup.cfg").write_text(
        "[tool:pytest]\ntestpaths = test\npythonpath = .\n", encoding="utf-8"
    )
    pins = tmp_path / "outer-pins.txt"
    pins.write_text(
        f"# shards: 2\n1 {first}\n1 {second}\n2 test/test_elsewhere.py\n", encoding="utf-8"
    )
    monkeypatch.setattr(runner, "ROOT", root)
    # The parts are nested pytest runs: they own their plugins and coverage data.
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "COV_CORE_SOURCE", "COV_CORE_DATAFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("COVERAGE_PROCESS_START", raising=False)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    target = tmp_path / "combined" / ".coverage"
    target.parent.mkdir()
    argv = ["--jobs", "2", "--shards", "2"]
    argv += ["--outer-shards", "2", "--outer-shard", "1", "--outer-pins", str(pins)]
    argv += ["--coverage-file", str(target), "--out"]
    plugins = ["-p", "xdist.plugin", "-p", "pytest_cov.plugin", "-p", "no:cacheprovider"]
    pytest_args = ["--", *plugins, "--cov=logic", "--cov-report="]

    rc = runner.main([*argv, str(tmp_path / "green"), *pytest_args])

    assert rc == 0, capsys.readouterr().out
    assert {case.rsplit(".", 1)[-1] for case in _ran(tmp_path / "green")} == {
        f"{Path(first).stem}::test_yes",
        f"{Path(second).stem}::test_no",
    }
    for index in (1, 2):
        assert runner.junit_counts(tmp_path / "green" / f"junit-{index}.xml") == (1, 0)
    combined = CoverageData(basename=str(target))
    combined.read()
    (measured,) = combined.measured_files()
    assert sorted(combined.lines(measured)) == [1, 2, 3, 4]

    (root / second).write_text("def test_no(): assert False\n", encoding="utf-8")

    assert runner.main([*argv, str(tmp_path / "red"), *pytest_args]) == 1
    assert "red: part 2 (exit 1)" in capsys.readouterr().out
    assert not target.exists()


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
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
