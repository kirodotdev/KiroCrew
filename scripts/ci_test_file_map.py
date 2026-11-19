"""pytest plugin: record which repository `.py` files each test executed.

SHADOW ONLY. Loaded by `.github/workflows/test-map.yml` alone, never by ci.yml:

    pytest -p scripts.ci_test_file_map --test-map-dir=DIR ...

Each process (xdist worker or a plain session) writes one
`DIR/part-<worker>.json`: `{test file: {"nodes": N, "files": [path, ...]}}`.
`scripts/ci_test_map.py build` merges the parts into the map.

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

shortcut: a module body that ran at collection time, before any test, is not
linked to the tests that use its values (a constant, a decorator). Upgrade when
the shadow report shows misses of that kind.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# 5 is the interpreter's OPTIMIZER_ID, which no tool in this suite claims; 3 and 4
# stay free for test/denied_commands_security_helpers.py, which takes either.
TOOL_ID = 5


class _Recorder:
    def __init__(self, root: Path, out_dir: Path) -> None:
        self.root = str(root) + os.sep
        self.out_dir = out_dir
        self.current: set[str] | None = None
        self.by_test_file: dict[str, dict[str, Any]] = {}
        self._seen_code: dict[str, str | None] = {}

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
        if self.current is not None:
            rel = self._rel(code.co_filename)
            if rel is not None:
                self.current.add(rel)
        return sys.monitoring.DISABLE

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

    def record(self, nodeid: str, files: set[str]) -> None:
        test_file = nodeid.split("::", 1)[0]
        entry = self.by_test_file.setdefault(test_file, {"nodes": 0, "files": set()})
        entry["nodes"] += 1
        entry["files"].update(files)

    def write(self) -> Path:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"part-{worker}.json"
        doc = {
            name: {"nodes": entry["nodes"], "files": sorted(entry["files"])}
            for name, entry in sorted(self.by_test_file.items())
        }
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
    _RECORDER = _Recorder(Path(str(config.rootpath)), Path(out))
    _RECORDER.start()


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
