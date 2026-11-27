"""pytest plugin: record which repository `.py` files each test executed.

SHADOW ONLY. Loaded by `.github/workflows/test-map.yml` alone, never by ci.yml:

    pytest -p scripts.ci_test_file_map --test-map-dir=DIR ...

Each process (xdist worker or a plain session) writes one
`DIR/part-<worker>.json`:

    {test file: {"nodes": N, "files": [path, ...],
                 "functions": [[path, qualname, first line, last line], ...]},
     "<import time>": [[path, qualname], ...]}

`scripts/ci_test_map.py build` merges the parts into the map.

`functions` covers only the shared-core files listed in
`scripts/ci_test_map_core.txt` under the root (none when it is absent): for
those, a file-level link reaches most of the suite, so the recorder also keeps
which function ran, named by its code object's `co_qualname` and spanning
`co_firstlineno` to its last line. A module or class body, a lambda or a
generator expression is not recorded. `<import time>` names the functions that
ran while a repository module was being imported: what they built is module
state every later test reads without running them, so the map keeps them
file-level.

Why not coverage contexts: with `--cov-context=test` coverage writes its SQLite
data at every context switch, which happens INSIDE a test's protocol. Tests that
patch `sqlite3.connect` to refuse opens then break coverage itself and crash the
xdist worker (a local full run: 133 failed, 49,844 errors, INTERNALERROR), and
the per-test arc data reached 35 GB. This plugin writes nothing until the session
ends and keeps only file names.

How: Python 3.12 `sys.monitoring` PY_START. The first time a function (or a
module body) starts during a test, its file is recorded and the event is turned
off for that code object (`DISABLE`); `restart_events()` turns them all back on
at the next test. So each code object costs one callback per test, and every
thread the test starts is seen. A child process is not.

Collection: the same switch is on while pytest collects each test module, so
every module body its import starts (a constant, a decorator, a table built at
import) is linked to that test file. A module already imported by an earlier
test file is cached in `sys.modules`, so its body is linked to the first one only.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# 5 is the interpreter's OPTIMIZER_ID, which no tool in this suite claims; 3 and 4
# stay free for test/denied_commands_security_helpers.py, which takes either.
TOOL_ID = 5

# Written by `scripts/ci_test_map.py core`, which names the same path.
CORE_LIST = "scripts/ci_test_map_core.txt"
# Read by `scripts/ci_test_map.py build`, which names the same key.
IMPORT_TIME = "<import time>"


# What a test ran: file paths, and `(path, qualname, first line)` for each
# function of a listed file.
Ran = set[str | tuple[str, str, int]]


class _Recorder:
    def __init__(self, root: Path, out_dir: Path, core: frozenset[str]) -> None:
        self.root = str(root) + os.sep
        self.out_dir = out_dir
        self.core = core
        self.current: Ran | None = None
        self.by_test_file: dict[str, dict[str, Any]] = {}
        self.collected: dict[str, Ran] = {}
        self._seen_code: dict[str, str | None] = {}
        self._last_lines: dict[tuple[str, str, int], int] = {}
        self.import_time: set[tuple[str, str]] = set()

    def _rel(self, filename: str) -> str | None:
        """Repository-relative POSIX path of a `.py` under the root, else None (cached)."""
        cached = self._seen_code.get(filename, "")
        if cached != "":
            return cached
        rel = None
        if filename.startswith(self.root) and filename.endswith(".py"):
            rel = filename[len(self.root) :].replace(os.sep, "/")
            if rel.startswith((".venv/", "build/")):
                rel = None
        self._seen_code[filename] = rel
        return rel

    def on_start(self, code: Any, _offset: int) -> Any:
        rel = self._rel(code.co_filename)
        if rel is None:
            return sys.monitoring.DISABLE
        if self.current is not None:
            self.current.add(rel)
        if rel in self.core and code.co_flags & inspect.CO_OPTIMIZED and code.co_name[:1] != "<":
            key = (rel, code.co_qualname, code.co_firstlineno)
            if key not in self._last_lines:
                lines = [line for _, _, line in code.co_lines() if line is not None]
                self._last_lines[key] = max(lines, default=key[2])
            if self.current is not None:
                self.current.add(key)
            # Also outside a test: a conftest's imports run while pytest collects a directory.
            # Frame 1 is the function starting; its callers are above it.
            if key[:2] not in self.import_time and self._importing(sys._getframe(1)):
                self.import_time.add(key[:2])
        return sys.monitoring.DISABLE

    def _importing(self, frame: Any) -> bool:
        """Whether a repository module's body is on the stack, that is, being imported."""
        while frame is not None:
            code = frame.f_code
            if code.co_name == "<module>" and self._rel(code.co_filename) is not None:
                return True
            frame = frame.f_back
        return False

    def start(self) -> None:
        monitoring = sys.monitoring
        if monitoring.get_tool(TOOL_ID) is not None:
            raise pytest.UsageError(f"sys.monitoring tool id {TOOL_ID} is already in use")
        monitoring.use_tool_id(TOOL_ID, "ci_test_map")
        monitoring.register_callback(TOOL_ID, monitoring.events.PY_START, self.on_start)
        monitoring.set_events(TOOL_ID, monitoring.events.PY_START)

    def stop(self) -> None:
        monitoring = sys.monitoring
        monitoring.set_events(TOOL_ID, monitoring.events.NO_EVENTS)
        monitoring.register_callback(TOOL_ID, monitoring.events.PY_START, None)
        monitoring.free_tool_id(TOOL_ID)

    def record(self, nodeid: str, files: Ran) -> None:
        test_file = nodeid.split("::", 1)[0]
        entry = self.by_test_file.setdefault(test_file, {"nodes": 0, "files": set()})
        entry["nodes"] += 1
        entry["files"].update(files)

    def record_collection(self, nodeid: str, files: Ran) -> None:
        self.collected.setdefault(nodeid.split("::", 1)[0], set()).update(files)

    def write(self) -> Path:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"part-{worker}.json"
        # A test file that collected but ran no test here (deselected, or another
        # xdist worker ran it) stays out: its collection links travel with its tests.
        doc = {}
        for name, entry in sorted(self.by_test_file.items()):
            ran = entry["files"] | self.collected.get(name, set())
            doc[name] = {
                "nodes": entry["nodes"],
                "files": sorted(p for p in ran if isinstance(p, str)),
                "functions": [
                    [*key, self._last_lines[key]]
                    for key in sorted(k for k in ran if isinstance(k, tuple))
                ],
            }
        doc[IMPORT_TIME] = [list(key) for key in sorted(self.import_time)]
        path.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
        return path


_RECORDER: _Recorder | None = None


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--test-map-dir",
        default=None,
        help="directory for per-process test -> executed-files parts (shadow test map)",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _RECORDER
    out = config.getoption("--test-map-dir")
    # No `sys.monitoring` before Python 3.12: record nothing rather than fail the run.
    if not out or _RECORDER is not None or not hasattr(sys, "monitoring"):
        return
    # The xdist controller runs no test; only the processes that do record.
    if os.environ.get("PYTEST_XDIST_WORKER") is None and _uses_xdist(config):
        return
    root = Path(str(config.rootpath))
    _RECORDER = _Recorder(root, Path(out), _core_files(root / CORE_LIST))
    _RECORDER.start()


def _core_files(listed: Path) -> frozenset[str]:
    if not listed.is_file():
        return frozenset()
    lines = listed.read_text(encoding="utf-8").splitlines()
    return frozenset(line for line in lines if line and not line.startswith("#"))


def _uses_xdist(config: pytest.Config) -> bool:
    numprocesses = getattr(config.option, "numprocesses", None)
    return bool(numprocesses) and numprocesses != 0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    recorder = _RECORDER
    if recorder is None:
        yield
        return
    recorder.current = set()
    sys.monitoring.restart_events()
    try:
        yield
    finally:
        files, recorder.current = recorder.current, None
        recorder.record(item.nodeid, files)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    recorder = _RECORDER
    if recorder is None or not isinstance(collector, pytest.Module):
        yield
        return
    outer, recorder.current = recorder.current, set()
    sys.monitoring.restart_events()
    try:
        yield
    finally:
        files, recorder.current = recorder.current, outer
        recorder.record_collection(collector.nodeid, files)
        # A collection inside a test (an in-process pytester run) is that test's work too.
        if outer is not None:
            outer.update(files)


def pytest_unconfigure(config: pytest.Config) -> None:
    global _RECORDER
    recorder, _RECORDER = _RECORDER, None
    if recorder is not None:
        recorder.stop()
        recorder.write()


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    """On the xdist controller: mark a worker that died, since its part is never written.

    A crashed worker loses every record it held, so the shard's map would be
    missing tests. The workflow refuses a shard with a `crashed-*` marker.
    """
    out = node.config.getoption("--test-map-dir")
    if error is None or not out:
        return
    worker = getattr(getattr(node, "gateway", None), "id", "unknown")
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / f"crashed-{worker}").write_text(str(error)[:500], encoding="utf-8")
