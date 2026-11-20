#!/usr/bin/env python3
"""Run the whole backend suite as many small pytest processes, each collecting only its part.

One ``pytest -n N`` run makes every xdist worker collect every test (~280k items) before it
runs one, so its memory grows with N and a single controller handles every report. Here
``scripts/ci_file_shards.py`` assigns whole files to ``--shards`` parts before import, and
at most ``--jobs`` parts run at once. Peak memory is about ``jobs`` processes of
``1/shards`` of the suite; more shards at the same jobs means less memory and better
balance. Every file still runs exactly once.

Parts run side by side, so the files of one ``xdist_group`` are pinned to one part: under
``--dist loadgroup`` a group runs on one worker, which is how ``serial`` and
``subprocess_spawn`` keep their tests from contending on the host, and two of its files in
two parts would run at once.

    python3 scripts/run_sharded_suite.py                    # sized from this machine
    python3 scripts/run_sharded_suite.py --jobs 4 --shards 32
    python3 scripts/run_sharded_suite.py -- -m "not ipv6_required and not real_venv"

``--outer-shards N --outer-shard K`` (with ``--outer-pins``, the pin file of that split)
runs only the files that shard K of N owns, split again into this run's parts. That is
how a CI job that already is one file shard runs its share as small processes. The outer
shard's files reach every part as a ``--file-shard-targets`` list, so discovery, ignores
and the part split stay pytest's and ``scripts/ci_file_shards.py``'s own.

Arguments after ``--`` go to every pytest process. Each part writes ``junit-<i>.xml`` and
``shard-<i>.log`` under ``--out``; ``--print-logs`` also copies each log to stdout when
its part ends. Exit status is 0 only when every part passed. A part that collected
nothing (pytest's exit 5) counts as passed, because its files belong to other parts; a
run in which no part collected anything exits 5, as pytest does. A part is red when it
exits with any other status, when it exits 0 without a JUnit file that counts a test, or
when it runs longer than ``--part-timeout`` seconds (its process group is killed and it
is reported as exit 124).

``--coverage-file PATH`` gives every part its own coverage data directory under
``--out`` (through ``COVERAGE_FILE``, so two parts never write one file) and, once every
part passed, combines them into PATH. A part that ran tests and left no data makes the
run red, and a red run writes no PATH.

Ctrl-C or SIGTERM starts no further part, stops the running ones (each part runs in its own
process group: SIGTERM, then SIGKILL to whatever is left after ``STOP_GRACE_SECS``) and
exits 128 + the signal. A part that cannot be started ends the run the same way, then
raises its error. Where ``os.waitid`` exists (Linux; macOS from Python 3.13), whatever a
part's tests left running in its group is killed when the part exits. Without it, a
stop's SIGTERM still reaches the whole group on macOS (only the part's own interpreter on
Windows), but a part that exits on it is reaped before the SIGKILL pass, so a child that
ignores SIGTERM survives the stop.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from defusedxml.ElementTree import ParseError, parse  # noqa: E402

from kiro_crew import platform_compat  # noqa: E402
from scripts.ci_file_shards import file_shard, hash_shard, read_pins  # noqa: E402

#: A literal ``xdist_group`` name. shortcut: a name built at runtime is not seen, so its
#: files keep their hash owners; read the marks from a collection if one ever spans files.
_GROUP_MARK = re.compile(r"""xdist_group\(\s*(?:name\s*=\s*)?["']([^"']+)["']""")

#: Per-process peak memory, GiB, measured on the full Linux suite at ``-n0`` (r7i.8xlarge):
#: ``FIXED + SUITE / shards``. 8 parts peaked at 3.5 GiB each, 32 parts at 1.06 GiB; all 32
#: parts at once peaked at 33.6 GiB on the whole machine, where ``pytest -n 32`` took 120 GiB.
#: Re-measure when the suite grows by half again.
PROCESS_FIXED_GIB = 0.25
SUITE_GIB = 27.0
#: pytest's own "no tests collected" exit status.
NO_TESTS_COLLECTED = 5
#: Reported for a part killed at ``--part-timeout``; the status ``timeout(1)`` uses.
PART_TIMED_OUT = 124
#: Reported for a part that exited 0 but left no JUnit file counting a test.
PART_NO_RESULT = 125


def available_gib() -> float | None:
    """Memory free for new processes, in GiB; ``None`` when the platform cannot say."""
    mib = platform_compat.host_available_mib()
    return mib / 1024 if mib else None


def plan(jobs: int, available: float | None, headroom: float = 0.8) -> int:
    """The fewest shards whose ``jobs`` concurrent parts fit in ``headroom`` of memory.

    Never fewer shards than jobs (an idle job slot buys nothing). With no memory reading,
    four parts per job.
    """
    if available is None:
        return jobs * 4
    budget = available * headroom - jobs * PROCESS_FIXED_GIB
    if budget <= 0:
        raise SystemExit(
            f"{jobs} parallel parts do not fit in {available:.1f} GiB available; pass a "
            "smaller --jobs"
        )
    return max(jobs, math.ceil(SUITE_GIB * jobs / budget))


def testpaths(root: Path) -> list[Path]:
    """``[tool:pytest] testpaths`` from ``setup.cfg``."""
    config = configparser.RawConfigParser()
    config.read(root / "setup.cfg", encoding="utf-8")
    return [root / name for name in config.get("tool:pytest", "testpaths").split()]


def group_pins(root: Path, shards: int) -> dict[str, int]:
    """One part for every file of each ``xdist_group`` that spans files.

    Files joined through any shared group form one set, owned by the hash owner of its
    first path, so the answer depends only on the tree and the part count.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent.setdefault(node, node) != node:
            node = parent[node]
        return node

    for top in testpaths(root):
        for path in sorted({*top.rglob("test_*.py"), *top.rglob("*_test.py")}):
            relative = path.relative_to(root).as_posix()
            for name in set(_GROUP_MARK.findall(path.read_text(encoding="utf-8"))):
                parent[find(relative)] = find("group:" + name)
    members: dict[str, list[str]] = {}
    for node in parent:
        if not node.startswith("group:"):
            members.setdefault(find(node), []).append(node)
    pins = {}
    for files in members.values():
        if len(files) > 1:
            owner = hash_shard(min(files), shards)
            pins.update(dict.fromkeys(files, owner))
    return pins


def write_pins(pins: dict[str, int], shards: int, path: Path) -> Path:
    """``pins`` in ``scripts/ci_file_shards.py``'s ``--file-shard-pins`` format."""
    lines = [f"# shards: {shards}", *(f"{owner} {name}" for name, owner in sorted(pins.items()))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def outer_shard_files(root: Path, count: int, index: int, pins: Path | None) -> list[str]:
    """Every file under ``testpaths`` that shard *index* of *count* owns, root-relative.

    Every file, not only test files: the list narrows pytest's discovery and never widens
    it, so a name pytest would not collect costs nothing here, and a file some collector
    does take is never left out.
    """
    owners = read_pins(pins, count) if pins is not None else {}
    owned = []
    for top in testpaths(root):
        for path in sorted(top.rglob("*")):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            if file_shard(path, root, count, owners) == index:
                owned.append(path.relative_to(root).as_posix())
    return owned


def write_targets(files: list[str], path: Path) -> Path:
    """*files* in ``scripts/ci_file_shards.py``'s ``--file-shard-targets`` format."""
    path.write_text("".join(f"{name}\n" for name in files), encoding="utf-8")
    return path


def coverage_dir(out: Path, index: int) -> Path:
    """Where part *index* keeps its coverage data, apart from every other part's."""
    return out / f"coverage-{index}"


def part_env(out: Path, index: int, coverage: bool) -> dict[str, str] | None:
    """The part's environment: its own ``COVERAGE_FILE`` when coverage is combined."""
    if not coverage:
        return None
    directory = coverage_dir(out, index)
    directory.mkdir(parents=True, exist_ok=True)
    # Data left by an earlier run in the same --out must not count for this one.
    for stale in coverage_files(out, index):
        stale.unlink()
    return {**os.environ, "COVERAGE_FILE": str(directory / ".coverage")}


def coverage_files(out: Path, index: int) -> list[Path]:
    """Part *index*'s coverage data: one file, or several suffixed ones (``parallel``)."""
    return sorted(coverage_dir(out, index).glob(".coverage*"))


def combine_coverage(out: Path, indexes: list[int], target: Path) -> int:
    """Combine the parts' coverage data into *target*; the ``coverage combine`` status.

    Files, not directories: in a directory ``coverage combine`` takes only names with a
    suffix, and a part that wrote one file leaves it unsuffixed.
    """
    command = [sys.executable, "-m", "coverage", "combine", f"--data-file={target}"]
    command += [str(path) for index in sorted(indexes) for path in coverage_files(out, index)]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def part_command(
    index: int,
    shards: int,
    workers: int,
    out: Path,
    extra: list[str],
    pins: Path,
    targets: Path | None = None,
) -> list[str]:
    # ``=`` on the targets path: see scripts/ci_file_shards.py on a path outside the root.
    narrowed = [f"--file-shard-targets={targets}"] if targets is not None else []
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "scripts.ci_file_shards",
        "--file-shards",
        str(shards),
        "--file-shard",
        str(index),
        f"--file-shard-pins={pins}",
        *narrowed,
        "-n",
        str(workers),
        f"--junitxml={out / f'junit-{index}.xml'}",
        *extra,
    ]


def junit_counts(path: Path) -> tuple[int, int]:
    """``(tests, failures + errors)`` in one part's JUnit file; zeros when it is absent."""
    try:
        root = parse(path).getroot()
    except (OSError, ParseError):
        return 0, 0
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(s.get("tests", 0)) for s in suites)
    bad = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    return tests, bad


#: Seconds a stopped part gets to exit after SIGTERM before it is killed.
STOP_GRACE_SECS = 10.0
#: Seconds between two looks at a part that is waited for with a bound or without waitid.
PART_POLL_SECS = 0.05


class Interrupted(Exception):
    """SIGTERM, raised in the main thread the way SIGINT raises ``KeyboardInterrupt``."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class Parts:
    """The running parts, so a stop can end them and start no more.

    One lock covers starting a part, reaping it and signalling its group, so a group id
    is only ever signalled while its leader is alive or an unreaped zombie: the kernel
    cannot hand that id to anyone else until the reap.
    """

    def __init__(self, timeout: float | None = None) -> None:
        self.lock = threading.Lock()
        self.stopped = False
        self.live: set[subprocess.Popen] = set()
        #: Seconds one part may run, or ``None`` for no bound.
        self.timeout = timeout
        #: Parts killed at the bound.
        self.overran: set[subprocess.Popen] = set()

    def launch(
        self, command: list[str], log, env: dict[str, str] | None = None
    ) -> subprocess.Popen | None:
        with self.lock:
            if self.stopped:
                return None
            # Its own process group, so a stop reaches the part's own children too.
            child = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.live.add(child)
            return child

    def _kill_overrun(self, child: subprocess.Popen) -> None:
        """Kill a part at its bound. Not yet reaped, so the group id is still its own."""
        with self.lock:
            self.overran.add(child)
            _signal_group(child, getattr(signal, "SIGKILL", signal.SIGTERM))

    def wait(self, child: subprocess.Popen) -> int:
        """The part's exit status; on Linux, once nothing it started is left running.

        A part still running ``timeout`` seconds after this call has its group killed and
        is recorded in ``overran``.
        """
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        if platform_compat.IS_POSIX and hasattr(os, "waitid"):
            # Exited but not reaped: the zombie keeps the group id ours while a test's
            # leftover server or worker is killed.
            flags = os.WEXITED | os.WNOWAIT
            if deadline is not None:
                while os.waitid(os.P_PID, child.pid, flags | os.WNOHANG) is None:
                    if time.monotonic() >= deadline:
                        self._kill_overrun(child)
                        break
                    time.sleep(PART_POLL_SECS)
            os.waitid(os.P_PID, child.pid, flags)
            with self.lock:
                _signal_group(child, signal.SIGKILL)
                rc = child.wait()
                self.live.discard(child)
            return rc
        # shortcut: no waitid (Windows; macOS before Python 3.13), so leftovers are not
        # killed here or by a stop. The reap still happens under the lock, so a stop never
        # signals an id the kernel may have reused. Upgrade: on macOS, hold the reap while
        # ``stopped`` until the SIGKILL pass, so the zombie keeps the group id.
        while True:
            with self.lock:
                rc = child.poll()
                if rc is not None:
                    self.live.discard(child)
                    return rc
            if deadline is not None and time.monotonic() >= deadline:
                self._kill_overrun(child)
                deadline = None
            time.sleep(PART_POLL_SECS)

    def halt(self) -> None:
        """Start no further part."""
        with self.lock:
            self.stopped = True

    def stop(self, grace: float | None = None) -> None:
        """Start no further part; SIGTERM every running part's group, SIGKILL what is left.

        Groups, not leaders: a part's pytest dies at once on SIGTERM, but a worker or a
        server one of its tests started may not. Where ``os.waitid`` exists, ``wait``
        kills those as the part exits. Without it the part is reaped first, so its group
        is no longer safe to signal and such a child survives the stop.
        """
        grace = STOP_GRACE_SECS if grace is None else grace
        with self.lock:
            self.stopped = True
            for child in self.live:
                _signal_group(child, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self.lock:
                if not self.live:
                    return
            time.sleep(0.05)
        with self.lock:
            for child in self.live:
                _signal_group(child, getattr(signal, "SIGKILL", signal.SIGTERM))


def _signal_group(child: subprocess.Popen, sig: int) -> None:
    """*sig* to a part's process group (its pid is the group id); an emptied one is skipped.

    Windows has no process groups here, so the part's own interpreter gets it instead.
    """
    if not platform_compat.IS_POSIX:
        # shortcut: only the part's own interpreter is stopped on Windows; use a job
        # object if parts ever leave children behind there.
        if child.poll() is None:
            child.terminate() if sig == signal.SIGTERM else child.kill()
        return
    try:
        platform_compat.kill_process_group(child.pid, sig)
    except (ProcessLookupError, PermissionError, ValueError):
        pass


def run_part(
    index: int,
    shards: int,
    workers: int,
    out: Path,
    extra: list[str],
    pins: Path,
    parts: Parts | None = None,
    targets: Path | None = None,
    coverage: bool = False,
) -> tuple:
    """``(index, exit status, seconds)``; status ``None`` when a stop came first.

    The status is ``PART_TIMED_OUT`` for a part killed at the run's ``--part-timeout``.

    A part that cannot be started stops the run before this thread takes the next
    queued part, so a broken launch (a missing interpreter, no file descriptors left)
    fails once, not once per part.
    """
    parts = parts or Parts()
    started = time.monotonic()
    try:
        # A result left by an earlier run in the same --out must not count for this one.
        (out / f"junit-{index}.xml").unlink(missing_ok=True)
        with open(out / f"shard-{index}.log", "wb") as log:
            command = part_command(index, shards, workers, out, extra, pins, targets)
            child = parts.launch(command, log, part_env(out, index, coverage))
    except BaseException:
        # Before this thread takes the next queued part; main stops the running ones.
        parts.halt()
        raise
    if child is None:
        return index, None, 0.0
    rc = parts.wait(child)
    if child in parts.overran:
        rc = PART_TIMED_OUT
    return index, rc, time.monotonic() - started


def print_log(index: int, shards: int, path: Path) -> None:
    """Copy one part's log to stdout, folded into a group on GitHub Actions."""
    grouped = os.environ.get("GITHUB_ACTIONS") == "true"
    print(f"::group::part {index}/{shards} log" if grouped else f"--- part {index}/{shards} log")
    sys.stdout.flush()
    try:
        sys.stdout.buffer.write(path.read_bytes())
    except OSError as exc:
        print(f"cannot read {path}: {exc}")
    sys.stdout.buffer.flush()
    print("::endgroup::" if grouped else f"--- end of part {index}/{shards} log", flush=True)


def _raise_interrupted(signum: int, _frame) -> None:
    # Ignore both first: a second Ctrl-C must not cut the stop that this one starts.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)
    raise Interrupted(signum)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    extra: list[str] = []
    if "--" in args:
        cut = args.index("--")
        args, extra = args[:cut], args[cut + 1 :]
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="parts at once")
    parser.add_argument("--shards", type=int, default=None, help="parts in total")
    parser.add_argument("--workers", type=int, default=0, help="xdist workers per part")
    parser.add_argument("--out", type=Path, default=None, help="directory for logs and JUnit")
    parser.add_argument("--outer-shards", type=int, default=None, help="file shards outside")
    parser.add_argument("--outer-shard", type=int, default=None, help="the one to run here")
    parser.add_argument("--outer-pins", type=Path, default=None, help="pin file of that split")
    parser.add_argument(
        "--coverage-file", type=Path, default=None, help="combine the parts' coverage here"
    )
    parser.add_argument("--part-timeout", type=float, default=None, help="seconds one part may run")
    parser.add_argument("--print-logs", action="store_true", help="copy each log to stdout")
    opts = parser.parse_args(args)
    outer = (opts.outer_shards, opts.outer_shard)
    if outer == (None, None):
        if opts.outer_pins is not None:
            parser.error("--outer-pins requires --outer-shards and --outer-shard")
    elif None in outer or not 1 <= opts.outer_shard <= opts.outer_shards:
        parser.error("an outer shard requires 1 <= --outer-shard <= --outer-shards")
    if opts.part_timeout is not None and opts.part_timeout <= 0:
        parser.error("--part-timeout must be positive")
    jobs = max(1, opts.jobs)
    shards = opts.shards or plan(jobs, available_gib())
    # Absolute: every part runs from the repository root, not from where this was started.
    out = (opts.out or Path(tempfile.mkdtemp(prefix="kc-sharded-suite-"))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pins = group_pins(ROOT, shards)
    pin_file = write_pins(pins, shards, out / "group-pins.txt")
    targets = None
    scope = ""
    if opts.outer_shards is not None:
        # Resolved here: a relative pin path is the caller's, and the parts run from ROOT.
        outer_pins = opts.outer_pins.resolve() if opts.outer_pins is not None else None
        owned = outer_shard_files(ROOT, opts.outer_shards, opts.outer_shard, outer_pins)
        targets = write_targets(owned, out / "outer-shard-files.txt")
        scope = f" of outer shard {opts.outer_shard}/{opts.outer_shards} ({len(owned)} files)"
    coverage = opts.coverage_file.resolve() if opts.coverage_file is not None else None
    if coverage is not None:
        # A red run leaves no combined file, not even an earlier run's.
        coverage.unlink(missing_ok=True)
    print(
        f"{shards} parts{scope}, {jobs} at a time, -n {opts.workers} each, {len(pins)} grouped "
        f"files pinned; logs in {out}",
        flush=True,
    )

    results = []
    parts = Parts(opts.part_timeout)
    previous = {
        sig: signal.signal(sig, _raise_interrupted) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    finished = False
    try:
        futures = [
            pool.submit(
                run_part,
                i,
                shards,
                opts.workers,
                out,
                extra,
                pin_file,
                parts,
                targets,
                coverage is not None,
            )
            for i in range(1, shards + 1)
        ]
        for future in concurrent.futures.as_completed(futures):
            index, rc, secs = future.result()
            tests, bad = junit_counts(out / f"junit-{index}.xml")
            if rc == 0 and not tests:
                # pytest exits 0 only after it ran a test, and then it wrote the file.
                rc = PART_NO_RESULT
            results.append((index, rc, secs, tests, bad))
            if opts.print_logs:
                print_log(index, shards, out / f"shard-{index}.log")
            print(
                f"part {index}/{shards}: exit {rc}, {tests} tests, {bad} failed, {secs:.0f}s",
                flush=True,
            )
        finished = True
    except Interrupted as stop:
        print(f"stopped by signal {stop.signum}: {len(results)} of {shards} parts finished")
        return 128 + stop.signum
    finally:
        if not finished:
            # Whatever ended the loop (a signal, a part that could not start), no queued
            # part may start and no running one is left behind.
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            pool.shutdown(wait=False, cancel_futures=True)
            parts.stop()
        pool.shutdown(wait=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)

    failed = [r for r in results if r[1] not in (0, NO_TESTS_COLLECTED)]
    total = sum(r[3] for r in results)
    bad = sum(r[4] for r in results)
    print(f"{total} tests in {shards} parts, {bad} failed or errored; {len(failed)} part(s) red")
    for index, rc, _secs, _tests, _bad in sorted(failed):
        why = _RED_REASONS.get(rc, "")
        print(f"  red: part {index} (exit {rc}{why}), see {out / f'shard-{index}.log'}")
    if all(r[1] == NO_TESTS_COLLECTED for r in results):
        # A selection that matches nothing (a misspelt -m) must not read as green.
        print("  red: no part collected a test")
        return NO_TESTS_COLLECTED
    if failed:
        return 1
    if coverage is not None:
        return _combine(out, [r[0] for r in results if r[1] == 0], coverage)
    return 0


#: What the two statuses this script assigns itself mean, for the red-part line.
_RED_REASONS = {
    PART_TIMED_OUT: ": ran past --part-timeout and was killed",
    PART_NO_RESULT: ": exited 0 without a JUnit file that counts a test",
}


def _combine(out: Path, ran: list[int], target: Path) -> int:
    """Combine the coverage of the parts that ran tests; 1 when any of it is missing."""
    missing = [index for index in sorted(ran) if not coverage_files(out, index)]
    for index in missing:
        print(f"  red: part {index} ran tests but left no coverage data")
    if missing:
        return 1
    if combine_coverage(out, ran, target) != 0 or not target.is_file():
        print(f"  red: the parts' coverage data could not be combined into {target}")
        return 1
    print(f"coverage of {len(ran)} parts combined into {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
