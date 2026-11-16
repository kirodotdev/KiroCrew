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

Arguments after ``--`` go to every pytest process. Each part writes ``junit-<i>.xml`` and
``shard-<i>.log`` under ``--out``. Exit status is 0 only when every part passed (a part
that collected nothing counts as passed: its files belong to other parts; a run in which no
part collected anything exits 5, as pytest does).

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
from scripts.ci_file_shards import hash_shard  # noqa: E402

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


def part_command(
    index: int, shards: int, workers: int, out: Path, extra: list[str], pins: Path
) -> list[str]:
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

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stopped = False
        self.live: set[subprocess.Popen] = set()

    def launch(self, command: list[str], log) -> subprocess.Popen | None:
        with self.lock:
            if self.stopped:
                return None
            # Its own process group, so a stop reaches the part's own children too.
            child = subprocess.Popen(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            self.live.add(child)
            return child

    def wait(self, child: subprocess.Popen) -> int:
        """The part's exit status; on Linux, once nothing it started is left running."""
        if platform_compat.IS_POSIX and hasattr(os, "waitid"):
            # Exited but not reaped: the zombie keeps the group id ours while a test's
            # leftover server or worker is killed.
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
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
            time.sleep(0.05)

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
) -> tuple:
    """``(index, exit status, seconds)``; status ``None`` when a stop came first.

    A part that cannot be started stops the run before this thread takes the next
    queued part, so a broken launch (a missing interpreter, no file descriptors left)
    fails once, not once per part.
    """
    parts = parts or Parts()
    started = time.monotonic()
    try:
        with open(out / f"shard-{index}.log", "wb") as log:
            child = parts.launch(part_command(index, shards, workers, out, extra, pins), log)
    except BaseException:
        # Before this thread takes the next queued part; main stops the running ones.
        parts.halt()
        raise
    if child is None:
        return index, None, 0.0
    return index, parts.wait(child), time.monotonic() - started


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
    opts = parser.parse_args(args)
    jobs = max(1, opts.jobs)
    shards = opts.shards or plan(jobs, available_gib())
    # Absolute: every part runs from the repository root, not from where this was started.
    out = (opts.out or Path(tempfile.mkdtemp(prefix="kc-sharded-suite-"))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pins = group_pins(ROOT, shards)
    pin_file = write_pins(pins, shards, out / "group-pins.txt")
    print(
        f"{shards} parts, {jobs} at a time, -n {opts.workers} each, {len(pins)} grouped files "
        f"pinned; logs in {out}",
        flush=True,
    )

    results = []
    parts = Parts()
    previous = {
        sig: signal.signal(sig, _raise_interrupted) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    finished = False
    try:
        futures = [
            pool.submit(run_part, i, shards, opts.workers, out, extra, pin_file, parts)
            for i in range(1, shards + 1)
        ]
        for future in concurrent.futures.as_completed(futures):
            index, rc, secs = future.result()
            tests, bad = junit_counts(out / f"junit-{index}.xml")
            results.append((index, rc, secs, tests, bad))
            print(f"part {index}/{shards}: exit {rc}, {tests} tests, {bad} failed, {secs:.0f}s")
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
        print(f"  red: part {index} (exit {rc}), see {out / f'shard-{index}.log'}")
    if all(r[1] == NO_TESTS_COLLECTED for r in results):
        # A selection that matches nothing (a misspelt -m) must not read as green.
        print("  red: no part collected a test")
        return NO_TESTS_COLLECTED
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
