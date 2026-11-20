"""``scripts/run_sharded_suite.py`` sizes its parts from memory and reports every red part."""

from __future__ import annotations

import contextlib
import io
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

# Several tests here start a real interpreter. One group keeps the module on one xdist
# worker, so those children start one after another and never on every worker at once.
pytestmark = pytest.mark.xdist_group(name="run_sharded_suite_children")


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


def test_a_part_killed_by_a_signal_is_red_and_the_other_parts_still_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    codes = {1: -9, 2: 0, 3: runner.NO_TESTS_COLLECTED}

    assert _run_with(monkeypatch, tmp_path, codes) == 1

    out = capsys.readouterr().out
    assert "red: part 1 (exit -9)" in out
    assert "part 2/3: exit 0, 1 tests" in out
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
    """The script reads this clock, and its pause between two looks moves it.

    Log titles are the plain ones: on GitHub Actions the script folds a log into a
    ``::group::``, and these tests run there too.
    """
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    held = _Clock()
    monkeypatch.setattr(runner, "_now", held)
    monkeypatch.setattr(runner, "_pause", held.advance)
    return held


class _Part:
    """Stands in for a part's process: it has ended once the clock reads ``ends_at``."""

    def __init__(self, ends_at: float | None = None) -> None:
        self.ends_at = ends_at


class _SimulatedParts(runner.Parts):
    """``Parts`` over ``_Part`` stand-ins: nothing is started, signalled or reaped, so a
    wait runs in the calling thread against the test's clock alone."""

    def __init__(self, clock: _Clock, timeout: float | None, deadline: float | None) -> None:
        super().__init__(timeout, deadline)
        self.clock = clock
        self.killed_at: list[float] = []

    def launch(self, command, log, env=None):
        # A launch takes time; the part's bound must not start after it.
        self.clock.advance(5)
        return _Part()

    def _ended(self, child) -> bool:
        return child.ends_at is not None and self.clock() >= child.ends_at

    def _kill_overrun(self, child) -> None:
        self.overran.add(child)
        self.killed_at.append(self.clock())

    def _reap(self, child) -> int:
        return -9 if child in self.overran else 0


def _within_one_look(reading: float, bound: float) -> bool:
    """Whether *reading* is the first look at or after *bound*."""
    return bound <= reading < bound + 2 * runner.PART_POLL_SECS


def test_a_part_still_running_at_its_own_bound_is_killed_then(clock: _Clock) -> None:
    parts = _SimulatedParts(clock, 600, None)
    armed = clock()

    assert parts.wait(_Part(), armed) == -9

    (killed,) = parts.killed_at
    assert _within_one_look(killed, armed + 600)


def test_a_part_that_ends_before_its_bound_is_not_killed(clock: _Clock) -> None:
    parts = _SimulatedParts(clock, 600, None)
    armed = clock()

    assert parts.wait(_Part(ends_at=armed + 599), armed) == 0

    assert parts.killed_at == [] and not parts.overran
    assert _within_one_look(clock(), armed + 599)


def test_a_part_that_starts_late_still_ends_at_the_runs_deadline(clock: _Clock) -> None:
    """Its own 3000 seconds would end long after the run's 600."""
    deadline = clock() + 600
    parts = _SimulatedParts(clock, 3000, deadline)
    clock.advance(500)

    assert parts.bound(clock()) == deadline
    assert parts.wait(_Part(), clock()) == -9

    (killed,) = parts.killed_at
    assert _within_one_look(killed, deadline)


@pytest.mark.parametrize(
    ("timeout", "run_left", "expected"),
    [(600, None, 600), (None, 60, 60), (600, 60, 60), (60, 600, 60), (None, None, None)],
    ids=["part-only", "run-only", "run-first", "part-first", "unbounded"],
)
def test_the_bound_is_the_earlier_of_the_parts_and_the_runs(
    clock: _Clock, timeout: float | None, run_left: float | None, expected: float | None
) -> None:
    start = clock()
    parts = _SimulatedParts(clock, timeout, None if run_left is None else start + run_left)

    assert parts.bound(start) == (None if expected is None else start + expected)


def test_a_wait_without_a_bound_only_reaps(clock: _Clock) -> None:
    parts = _SimulatedParts(clock, None, None)
    start = clock()

    assert parts.wait(_Part()) == 0

    assert clock() == start and parts.killed_at == []


def test_a_parts_bound_counts_from_before_its_launch(tmp_path: Path, clock: _Clock) -> None:
    """The stand-in launch moves the clock by 5 s; the part still gets 600 s, not 605."""
    parts = _SimulatedParts(clock, 600, None)
    before = clock()

    index, rc, _secs = runner.run_part(1, 1, 0, tmp_path, [], tmp_path / "p.txt", parts)

    assert (index, rc) == (1, runner.PART_TIMED_OUT)
    (killed,) = parts.killed_at
    assert _within_one_look(killed, before + 600)
    assert parts.running == {}


def test_a_part_whose_turn_comes_after_the_runs_deadline_is_not_started(
    tmp_path: Path, clock: _Clock
) -> None:
    late = _SimulatedParts(clock, None, clock())
    late.launch = lambda *_args: pytest.fail("must not start")  # type: ignore[method-assign]

    assert runner.run_part(1, 1, 0, tmp_path, [], tmp_path / "p.txt", late) == (
        1,
        runner.PART_NOT_STARTED,
        0.0,
    )


def _running(parts: runner.Parts, clock: _Clock, tmp_path: Path, index: int, text: str) -> Path:
    """Record part *index* as running since now, with *text* in its log."""
    log = tmp_path / f"shard-{index}.log"
    # Bytes: the script copies a log byte for byte, and text mode would write CRLF on Windows.
    log.write_bytes(text.encode())
    parts.running[index] = (clock(), log)
    return log


def test_the_running_parts_are_named_once_a_heartbeat(
    tmp_path: Path, clock: _Clock, capsys
) -> None:
    parts = _SimulatedParts(clock, None, None)
    watch = runner.Watch(parts, 4, False)
    _running(parts, clock, tmp_path, 2, "")
    _running(parts, clock, tmp_path, 3, "")

    watch.look()
    assert capsys.readouterr().out == ""

    clock.advance(runner.HEARTBEAT_SECS)
    watch.look()
    watch.look()
    line = f"running: part 2 for {runner.HEARTBEAT_SECS:.0f}s, part 3 for {runner.HEARTBEAT_SECS:.0f}s\n"
    assert capsys.readouterr().out == line

    del parts.running[2]
    clock.advance(runner.HEARTBEAT_SECS)
    watch.look()
    assert capsys.readouterr().out == f"running: part 3 for {2 * runner.HEARTBEAT_SECS:.0f}s\n"


def test_a_part_gone_quiet_has_its_log_printed_once_while_it_still_runs(
    tmp_path: Path, clock: _Clock, capfd
) -> None:
    parts = _SimulatedParts(clock, None, None)
    watch = runner.Watch(parts, 1, True)
    log = _running(parts, clock, tmp_path, 1, "stuck in part\n")
    quiet = "--- part 1/1 log so far (nothing written for"

    watch.look()
    clock.advance(runner.STALL_SECS - 1)
    watch.look()
    assert quiet not in capfd.readouterr().out

    clock.advance(1)
    watch.look()
    shown = capfd.readouterr().out
    assert f"{quiet} {runner.STALL_SECS:.0f}s, still running)\nstuck in part\n" in shown

    clock.advance(runner.STALL_SECS)
    watch.look()
    assert quiet not in capfd.readouterr().out

    # The log grows, so the part is not quiet until it stops again for a whole period.
    log.write_bytes(b"stuck in part\nand again\n")
    watch.look()
    clock.advance(runner.STALL_SECS)
    watch.look()
    assert "stuck in part\nand again\n" in capfd.readouterr().out


def test_a_quiet_parts_log_is_not_printed_without_print_logs(
    tmp_path: Path, clock: _Clock, capfd
) -> None:
    parts = _SimulatedParts(clock, None, None)
    watch = runner.Watch(parts, 1, False)
    _running(parts, clock, tmp_path, 1, "stuck in part\n")

    watch.look()
    clock.advance(runner.STALL_SECS)
    watch.look()
    watch.stopping()

    assert "stuck in part" not in capfd.readouterr().out


def test_a_stop_prints_the_log_so_far_of_every_running_part(
    tmp_path: Path, clock: _Clock, capfd
) -> None:
    parts = _SimulatedParts(clock, None, None)
    watch = runner.Watch(parts, 2, True)
    _running(parts, clock, tmp_path, 1, "first so far\n")
    _running(parts, clock, tmp_path, 2, "second so far\n")

    watch.stopping()

    shown = capfd.readouterr().out
    for index, text in ((1, "first"), (2, "second")):
        title = f"--- part {index}/2 log so far (still running at the stop)"
        assert f"{title}\n{text} so far\n--- end of part {index}/2 log" in shown


def test_the_running_logs_are_printed_before_the_parts_are_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Whatever stops the run may kill this script a few seconds later."""
    order: list[str] = []

    def part(_index, *_args):
        raise RuntimeError("cannot start")

    monkeypatch.setattr(runner, "run_part", part)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    monkeypatch.setattr(runner.Watch, "stopping", lambda self: order.append("logs"))
    monkeypatch.setattr(runner.Parts, "stop", lambda self, grace=None: order.append("stop"))

    with pytest.raises(RuntimeError, match="cannot start"):
        runner.main(["--jobs", "1", "--shards", "1", "--print-logs", "--out", str(tmp_path)])

    assert order == ["logs", "stop"]


def test_each_part_gets_its_own_coverage_file_and_an_earlier_runs_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KEPT_FROM_THE_CALLER", "yes")
    stale = runner.coverage_dir(tmp_path, 2)
    stale.mkdir()
    (stale / ".coverage.host.1.x").write_text("left by an earlier run", encoding="utf-8")

    first, second = (runner.part_env(tmp_path, index, True) for index in (1, 2))

    assert first is not None and second is not None
    assert first["COVERAGE_FILE"] == str(tmp_path / "coverage-1" / ".coverage")
    assert second["COVERAGE_FILE"] == str(tmp_path / "coverage-2" / ".coverage")
    assert first["KEPT_FROM_THE_CALLER"] == "yes"
    assert runner.coverage_files(tmp_path, 2) == []
    assert runner.part_env(tmp_path, 3, False) is None
    assert not runner.coverage_dir(tmp_path, 3).exists()


def _parts_that(monkeypatch: pytest.MonkeyPatch, leave: dict[int, tuple[int, int | None]]) -> None:
    """Make part ``i`` end with status ``leave[i][0]`` and, when ``leave[i][1]`` is a line
    number, coverage data that says ``measured.py`` ran that line."""
    from coverage import CoverageData

    def part(index, _shards, _workers, out, *_rest):
        rc, line = leave[index]
        if rc == 0:
            _junit(out, index)
        if line is not None:
            directory = runner.coverage_dir(out, index)
            directory.mkdir(parents=True, exist_ok=True)
            data = CoverageData(basename=str(directory / ".coverage"))
            data.add_lines({str(out / "measured.py"): [line]})
            data.write()
        return index, rc, 0.0

    monkeypatch.setattr(runner, "run_part", part)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})


def _coverage_run(tmp_path: Path, shards: int) -> tuple[int, Path]:
    target = tmp_path / "combined" / ".coverage"
    target.parent.mkdir()
    target.write_text("left by an earlier run", encoding="utf-8")
    argv = ["--jobs", "1", "--shards", str(shards), "--out", str(tmp_path / "o")]
    return runner.main([*argv, "--coverage-file", str(target)]), target


def test_the_coverage_of_the_parts_that_ran_tests_is_combined_into_the_one_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Which parts are combined, and where; the combine itself runs in the nested test."""
    combined: list[tuple[list[int], Path]] = []

    def combine(_out, indexes, target):
        combined.append((sorted(indexes), target))
        target.write_text("combined", encoding="utf-8")
        return 0

    _parts_that(monkeypatch, {1: (0, 1), 2: (0, 3), 3: (runner.NO_TESTS_COLLECTED, None)})
    monkeypatch.setattr(runner, "combine_coverage", combine)

    rc, target = _coverage_run(tmp_path, 3)

    assert rc == 0
    assert combined == [([1, 2], target)]
    assert f"coverage of 2 parts combined into {target}" in capsys.readouterr().out


def test_a_part_that_ran_tests_without_coverage_data_is_red_and_nothing_is_combined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _parts_that(monkeypatch, {1: (0, 1), 2: (0, None)})

    rc, target = _coverage_run(tmp_path, 2)

    assert rc == 1
    assert "red: part 2 ran tests but left no coverage data" in capsys.readouterr().out
    assert not target.exists()


def test_a_combine_that_does_not_end_is_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    seen: list[float] = []

    def never_ends(command, **kwargs):
        seen.append(kwargs["timeout"])
        raise runner.subprocess.TimeoutExpired(command, kwargs["timeout"])

    _parts_that(monkeypatch, {1: (0, 1)})
    monkeypatch.setattr(runner, "_run", never_ends)

    rc, target = _coverage_run(tmp_path, 1)

    assert rc == 1
    assert seen == [runner.COMBINE_TIMEOUT_SECS]
    assert "coverage combine was still running" in capsys.readouterr().out
    assert not target.exists()


def test_a_red_part_leaves_no_combined_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _parts_that(monkeypatch, {1: (0, 1), 2: (1, 2)})

    rc, target = _coverage_run(tmp_path, 2)

    assert rc == 1
    assert not target.exists()


def test_print_logs_copies_every_started_parts_log_to_stdout_and_names_the_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfdbinary
) -> None:
    codes = {1: 0, 2: runner.PART_TIMED_OUT, 3: runner.PART_NOT_STARTED}

    def part(index, _shards, _workers, out, *_rest):
        if codes[index] != runner.PART_NOT_STARTED:
            (out / f"shard-{index}.log").write_bytes(f"output of part {index}\n".encode())
        if codes[index] == 0:
            _junit(out, index)
        return index, codes[index], 0.0

    monkeypatch.setattr(runner, "run_part", part)
    monkeypatch.setattr(runner, "group_pins", lambda _root, _shards: {})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    rc = runner.main(["--jobs", "1", "--shards", "3", "--print-logs", "--out", str(tmp_path)])
    shown = capfdbinary.readouterr().out.decode()

    assert rc == 1
    for index in (1, 2):
        assert f"::group::part {index}/3 log\noutput of part {index}\n::endgroup::" in shown
    assert "part 3/3 log" not in shown
    assert f"red: part 2 (exit {runner.PART_TIMED_OUT}: still running at --part-timeout" in shown
    assert f"red: part 3 (exit {runner.PART_NOT_STARTED}: not started" in shown
    assert "red: part 1" not in shown


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


#: Lost-run ceilings for the nested run: two pytest parts over three one-line test files,
#: then one ``coverage combine``. The whole run took under 1 s on a 32-core host at load
#: 40. Both together are under half of the 300 s its consumers allow, so a part or a
#: combine that does not end is killed and reported by the script, by name, before
#: pytest-timeout would end the worker.
_NESTED_RUN_CEILING_SECS = 60
_NESTED_COMBINE_CEILING_SECS = 60
_NESTED_TEST_TIMEOUT_SECS = 300


class _NestedRun:
    """What the module's one run of real parts left behind."""

    def __init__(self, rc: int, said: str, out: Path, coverage: Path, tests: tuple[str, str]):
        self.rc = rc
        self.said = said
        self.out = out
        self.coverage = coverage
        self.tests = tests


@pytest.fixture(scope="module")
def nested_run(tmp_path_factory: pytest.TempPathFactory) -> _NestedRun:
    """Outer shard 1 of 2 of a three-file tree, run once as two real pytest parts.

    The only real processes the tests of this change start: two pytest parts and the
    script's own ``coverage combine``, once for the module, each under a ceiling above.
    Every test that needs what real parts leave reads it from here.
    """
    base = tmp_path_factory.mktemp("nested-run")
    first, second = (
        next(
            f"test/test_part_{i}.py"
            for i in range(200)
            if hash_shard(f"test/test_part_{i}.py", 2) == part
        )
        for part in (1, 2)
    )
    root = _tree(
        base / "tree",
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
    pins = base / "outer-pins.txt"
    pins.write_text(
        f"# shards: 2\n1 {first}\n1 {second}\n2 test/test_elsewhere.py\n", encoding="utf-8"
    )
    target = base / "combined" / ".coverage"
    target.parent.mkdir()
    argv = ["--jobs", "2", "--shards", "2", "--run-timeout", str(_NESTED_RUN_CEILING_SECS)]
    argv += ["--outer-shards", "2", "--outer-shard", "1", "--outer-pins", str(pins)]
    argv += ["--coverage-file", str(target), "--out", str(base / "out")]
    plugins = ["-p", "xdist.plugin", "-p", "pytest_cov.plugin", "-p", "no:cacheprovider"]
    argv += ["--", *plugins, "--cov=logic", "--cov-report="]
    said = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "ROOT", root)
        # The parts are nested pytest runs: they own their plugins and coverage data.
        for name in _OUTER_RUN_ENV:
            patch.delenv(name, raising=False)
        patch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        patch.setenv("PYTHONDONTWRITEBYTECODE", "1")
        patch.setenv("PYTHONPATH", str(REPO_ROOT))
        ceiling = _NESTED_COMBINE_CEILING_SECS
        patch.setattr(runner, "COMBINE_TIMEOUT_SECS", ceiling)  # flake-ok: lost-run ceiling
        with contextlib.redirect_stdout(said):
            rc = runner.main(argv)
    tests = (f"{Path(first).stem}::test_yes", f"{Path(second).stem}::test_no")
    return _NestedRun(rc, said.getvalue(), base / "out", target, tests)


#: What the outer pytest run may have exported that a nested one must not inherit.
_OUTER_RUN_ENV = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "COV_CORE_SOURCE",
    "COV_CORE_DATAFILE",
    "COVERAGE_PROCESS_START",
    "COVERAGE_RCFILE",
)


@pytest.mark.timeout(_NESTED_TEST_TIMEOUT_SECS)
def test_an_outer_shard_runs_as_parts_that_cover_exactly_its_files(nested_run: _NestedRun) -> None:
    """The outer shard's tests run once each, one per part; the other outer shard's file,
    which raises on import, is never collected."""
    assert nested_run.rc == 0, nested_run.said
    assert {case.rsplit(".", 1)[-1] for case in _ran(nested_run.out)} == set(nested_run.tests)
    for index in (1, 2):
        assert runner.junit_counts(nested_run.out / f"junit-{index}.xml") == (1, 0)


@pytest.mark.timeout(_NESTED_TEST_TIMEOUT_SECS)
def test_real_parts_leave_one_coverage_file_with_every_parts_lines(nested_run: _NestedRun) -> None:
    from coverage import CoverageData

    assert nested_run.rc == 0, nested_run.said
    combined = CoverageData(basename=str(nested_run.coverage))
    combined.read()
    (measured,) = combined.measured_files()
    assert sorted(combined.lines(measured)) == [1, 2, 3, 4]
    assert sorted(path.name for path in nested_run.coverage.parent.iterdir()) == [".coverage"]


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
