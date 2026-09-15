"""Opt-in, payload-free pytest timing records; never changes test selection or outcomes."""

from __future__ import annotations

import ast
import json
import re
import time
import uuid
from pathlib import Path

import pytest

_KEY = pytest.StashKey()
_PROPERTY = "kirocrew_ci_progress"
_CONSOLE_INTERVAL = 30.0
_COMPONENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,99}\Z")


def pytest_addoption(parser):
    parser.addoption("--ci-progress-dir", default=None, help="Opt in to CI timing JSONL files")


def pytest_configure(config):
    directory = config.getoption("ci_progress_dir")
    if directory:
        recorder = Progress(config, Path(directory))
        config.stash[_KEY] = recorder
        config.pluginmanager.register(recorder, "ci-progress-recorder")


def pytest_unconfigure(config):
    recorder = config.stash.get(_KEY, None)
    if recorder is not None:
        recorder.close()


class Progress:
    def __init__(self, config, directory):
        import os

        self.config = config
        self.root = Path(config.rootpath).resolve()
        self.worker = getattr(config, "workerinput", {}).get("workerid", "controller")
        if not re.fullmatch(r"gw[0-9]+|controller", self.worker):
            self.worker = "worker"
        self.controller = not hasattr(config, "workerinput") and bool(
            getattr(config.option, "numprocesses", 0)
        )
        self.sources = {}
        self.identities = {}
        self.started = time.perf_counter()
        self.console_at = self.started - _CONSOLE_INTERVAL
        self.slowest = None
        self.last_completed = None
        self.collection_started = self.started
        self.stream = None
        self.console = config.pluginmanager.getplugin("terminalreporter")
        # A UUID per recorder also separates repeated pytest.main calls in one PID.
        self.run = f"{self.worker}-{os.getpid()}-{uuid.uuid4().hex}"
        if not self.controller:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self.stream = (directory / f"{self.run}.jsonl").open("x", encoding="utf-8")
            except OSError:
                self.notice({"event": "unavailable"})

    def notice(self, record):
        # TerminalReporter is not registered yet during pytest_configure.
        reporter = self.console or self.config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            try:
                reporter.write_line("CI_PROGRESS " + json.dumps(record, separators=(",", ":")))
                reporter._tw.flush()
            except OSError:
                pass

    def emit(self, event, **fields):
        record = {"event": event, "time": round(time.time(), 6), **fields}
        if self.stream is not None:
            try:
                self.stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                self.stream.flush()  # Survives process cancellation, not a machine/power loss.
            except OSError:
                self.close()
        return record

    def close(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def source_identity(self, item, ordinal):
        """Use source declarations, not nodeid/name/originalname or parameter values."""
        module = item.getparent(pytest.Module)
        identity = {"case": ordinal, "test": "dynamic"}
        if module is None:
            return identity
        path = module.path
        if path not in self.sources:
            declarations = {}
            relative = None
            try:
                resolved = path.resolve()
                parts = resolved.relative_to(self.root).parts
                if len(parts) <= 16 and all(_COMPONENT.fullmatch(p) for p in parts[:-1]):
                    if resolved.suffix == ".py" and _COMPONENT.fullmatch(resolved.stem):
                        relative = "/".join(parts)
                        tree = ast.parse(resolved.read_text(encoding="utf-8-sig"))

                        def visit(body, classes=()):
                            for node in body:
                                if isinstance(node, ast.ClassDef):
                                    visit(node.body, (*classes, node.name))
                                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    names = (*classes, node.name)
                                    if all(_COMPONENT.fullmatch(n) for n in names):
                                        line = min(
                                            [node.lineno] + [d.lineno for d in node.decorator_list]
                                        )
                                        declarations[line - 1] = "::".join(names)[:300]

                        visit(tree.body)
            except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError):
                pass
            self.sources[path] = relative, declarations
        relative, declarations = self.sources[path]
        if relative is not None:
            identity["module"] = relative
            # Only the numeric source line is read from pytest's dynamic location.
            line = item.location[1]
            identity["line"] = line + 1
            identity["test"] = declarations.get(line, "dynamic")
        return identity

    def pytest_collection(self, session):
        self.collection_started = time.perf_counter()
        self.emit("collection_start", run=self.run)

    def pytest_collection_finish(self, session):
        elapsed = time.perf_counter() - self.collection_started
        for ordinal, item in enumerate(session.items):
            self.identities[item.nodeid] = self.source_identity(item, ordinal)
        self.emit("collection_end", selected=len(session.items), elapsed=elapsed, run=self.run)

    def pytest_runtest_logstart(self, nodeid, location):
        if not self.controller:
            self.emit("test_start", **self.identities.get(nodeid, {"test": "dynamic"}))

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        identity = self.identities.get(item.nodeid, {"test": "dynamic"})
        # xdist transports this safe metadata; never forward captured output or longrepr.
        setattr(report, _PROPERTY, {"run": self.run, **identity})

    def pytest_runtest_logreport(self, report):
        if report.when not in ("setup", "call", "teardown"):
            return
        identity = getattr(report, _PROPERTY, None)
        if identity is None:
            return
        record = dict(
            identity,
            phase=report.when,
            elapsed=round(max(0.0, report.duration), 6),
            outcome=(
                report.outcome if report.outcome in ("passed", "failed", "skipped") else "other"
            ),
        )
        if not self.controller:
            self.emit("phase_end", **record)
        if report.when == "teardown":
            self.last_completed = identity
        if self.slowest is None or record["elapsed"] > self.slowest["elapsed"]:
            self.slowest = record
        now = time.perf_counter()
        if not hasattr(self.config, "workerinput") and now - self.console_at >= _CONSOLE_INTERVAL:
            self.notice(
                {
                    "event": "progress",
                    "last_phase": record,
                    "last_completed": self.last_completed,
                    "slowest_phase": self.slowest,
                }
            )
            self.console_at, self.slowest = now, None

    def pytest_runtest_logfinish(self, nodeid, location):
        if not self.controller:
            self.emit("test_end", **self.identities.get(nodeid, {"test": "dynamic"}))

    @staticmethod
    def worker_label(node):
        value = node.gateway.id
        return value if re.fullmatch(r"gw[0-9]+", value) else "worker"

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodeready(self, node):
        self.notice({"event": "worker_ready", "worker": self.worker_label(node)})

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.notice(
            {"event": "worker_collected", "worker": self.worker_label(node), "selected": len(ids)}
        )

    def pytest_sessionfinish(self, session, exitstatus):
        self.emit("session_end", exitstatus=int(exitstatus))
        if not hasattr(self.config, "workerinput"):
            self.notice({"event": "session_end", "exitstatus": int(exitstatus)})
        self.close()
