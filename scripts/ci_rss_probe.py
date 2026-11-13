"""Opt-in per-test memory probe for xdist workers; never changes selection or outcomes.

Each worker appends one JSON line per test whose own RSS, peak RSS or
children's peak RSS grew by at least ``--ci-rss-min-mib`` while it ran, plus
one line at session end with the worker's final RSS and peak. The backend-test
job prints the largest records after the run.
"""

from __future__ import annotations

import json
import os
import resource
import time
from pathlib import Path

import pytest

_KEY = pytest.StashKey()


def pytest_addoption(parser):
    parser.addoption("--ci-rss-dir", default=None, help="Opt in to per-test RSS JSONL files")
    parser.addoption("--ci-rss-min-mib", type=int, default=32)


def pytest_configure(config):
    directory = config.getoption("ci_rss_dir")
    if directory and hasattr(config, "workerinput"):
        probe = Probe(config, Path(directory))
        config.stash[_KEY] = probe
        config.pluginmanager.register(probe, "ci-rss-probe")


def pytest_unconfigure(config):
    probe = config.stash.get(_KEY, None)
    if probe is not None:
        probe.close()


def _status_mib() -> tuple[int, int]:
    rss = hwm = 0
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) // 1024
                elif line.startswith("VmHWM:"):
                    hwm = int(line.split()[1]) // 1024
    except OSError:
        pass
    return rss, hwm


def _children_mib() -> int:
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024


class Probe:
    def __init__(self, config, directory):
        self.worker = config.workerinput.get("workerid", "worker")
        self.min_mib = config.getoption("ci_rss_min_mib")
        self.before = (0, 0, 0)
        self.started = 0.0
        self.per_file: dict[str, int] = {}
        self.stream = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.stream = (directory / f"{self.worker}-{os.getpid()}.jsonl").open(
                "a", encoding="utf-8"
            )
        except OSError:
            self.stream = None

    def _write(self, record):
        if self.stream is None:
            return
        self.stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.stream.flush()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logstart(self, nodeid, location):
        rss, hwm = _status_mib()
        self.before = (rss, hwm, _children_mib())
        self.started = time.time()

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logfinish(self, nodeid, location):
        rss, hwm = _status_mib()
        kids = _children_mib()
        d_rss = rss - self.before[0]
        d_hwm = hwm - self.before[1]
        d_kids = kids - self.before[2]
        module = nodeid.split("::", 1)[0]
        self.per_file[module] = self.per_file.get(module, 0) + d_rss
        if max(d_rss, d_hwm, d_kids) >= self.min_mib:
            self._write(
                {
                    "w": self.worker,
                    "t": round(self.started, 1),
                    "dur": round(time.time() - self.started, 2),
                    "test": nodeid,
                    "rss": rss,
                    "d_rss": d_rss,
                    "hwm": hwm,
                    "d_hwm": d_hwm,
                    "kids": kids,
                    "d_kids": d_kids,
                }
            )

    def close(self):
        rss, hwm = _status_mib()
        top = sorted(self.per_file.items(), key=lambda kv: -kv[1])[:15]
        self._write(
            {
                "w": self.worker,
                "end": True,
                "rss": rss,
                "hwm": hwm,
                "kids": _children_mib(),
                "files": top,
            }
        )
        if self.stream is not None:
            self.stream.close()
            self.stream = None
