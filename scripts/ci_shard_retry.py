"""Retry a backend shard's few failed tests once, and record each flaky pass.

One flaky test turns a whole shard job red. This plugin reruns the failed tests
once, in a fresh pytest pass in the same job, and writes every test that passed
only on that retry to a record file, so the flaky test is named, not forgotten.

    python -m pytest -p scripts.ci_shard_retry ...
        Loaded by a shard. Does nothing unless $CI_SHARD_RETRY is ``1`` and
        $CI_SHARD_RETRY_EVENT is ``pull_request``.

    python3 scripts/ci_shard_retry.py summary
        A later step, as the runner identity: one job summary line per flaky
        test in the record (Linux shards run pytest as a user that cannot write
        $GITHUB_STEP_SUMMARY).

Rules (each is pinned by `test/test_ci_shard_retry.py`):

1. Off unless opted in, and never on a run whose event is not `pull_request`:
   main keeps showing every flaky failure.
2. Only a session that ended in plain test failures retries, and only when 1 to
   ``MAX_RETRY`` tests failed in setup, call or teardown. More than that is
   likely a real break: nothing is retried and one line says so. A crashed
   xdist worker (a report with no setup/call/teardown phase), a collection
   error, or a session `-x` / `--maxfail` stopped early, retries nothing.
3. The retry is one fresh pytest pass with the session's own arguments and
   ``-n 0``: it collects what the session collected, wherever the selection
   came from (command line, ini ``addopts`` or $PYTEST_ADDOPTS), and
   deselects every test but the failed ones. A failed id with the xdist
   ``@group`` suffix matches the plain id the serial pass collects, whatever
   the group's name. Report-writing options are dropped from all three
   sources, so the first session's reports stay whole. It never retries
   again.
4. The job turns green only when every retried test passed, the retry pass
   exited 0 and the record was written. A test that fails again keeps the job
   red.
5. Each flaky pass is one JSON line in $CI_SHARD_RETRY_RECORD (default
   ``ci-shard-retry.jsonl`` in $RUNNER_TEMP, else the system temp dir):
   ``nodeid`` (the plain id), ``first_failure``, ``shard`` and ``head_sha``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any

ENV_ON = "CI_SHARD_RETRY"
ENV_EVENT = "CI_SHARD_RETRY_EVENT"
ENV_RECORD = "CI_SHARD_RETRY_RECORD"
ENV_SHARD = "CI_SHARD_RETRY_SHARD"
ENV_HEAD_SHA = "CI_SHARD_RETRY_HEAD_SHA"
# Set only on the retry pass: where it writes each node id's outcome.
ENV_RESULTS = "CI_SHARD_RETRY_RESULTS"
# Set only on the retry pass: the JSON list of failed node ids it runs.
ENV_TARGETS = "CI_SHARD_RETRY_TARGETS"
PLUGIN = "scripts.ci_shard_retry"
MAX_RETRY = 5
SUMMARY_CHARS = 200
# Bounds the retry pass; far above five tests' worth of pytest-timeout.
RETRY_TIMEOUT_SECONDS = 1800
_PHASES = ("setup", "call", "teardown")
# Options that write a report file, each with a required value.
_REPORT_OPTIONS = frozenset(
    {"--junitxml", "--junit-xml", "--report-log", "--result-log", "--json-report-file"}
)
_COV_VALUE_OPTIONS = frozenset(
    {"--cov-report", "--cov-config", "--cov-fail-under", "--cov-context"}
)
_UNPRINTABLE = re.compile(r"[^\x20-\x7e]+")


def match_targets(nodeids: list[str], targets: list[str]) -> dict[str, str]:
    """``{collected id: failed id}`` for each failed id among ``nodeids``.

    `--dist loadgroup` reports ``<id>@<group>`` for any group name, while a
    serial pass collects ``<id>``: an exact match wins, then ``<id>@`` as a
    prefix.
    """
    known = set(nodeids)
    matched = {target: target for target in targets if target in known}
    left = [target for target in targets if target not in known]
    for nodeid in nodeids:
        if nodeid in matched:
            continue
        for target in left:
            if target.startswith(nodeid + "@"):
                matched[nodeid] = target
                left.remove(target)
                break
    return matched


def printable(text: str, limit: int = SUMMARY_CHARS) -> str:
    """``text`` as printable ASCII on one line, cut to ``limit`` characters."""
    return " ".join(_UNPRINTABLE.sub(" ", text).split())[:limit]


def enabled(env: dict[str, str]) -> bool:
    return env.get(ENV_ON) == "1" and env.get(ENV_EVENT) == "pull_request"


def record_path(env: dict[str, str]) -> Path:
    if env.get(ENV_RECORD):
        return Path(env[ENV_RECORD])
    return Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / "ci-shard-retry.jsonl"


def failure_summary(report: Any) -> str:
    """Exception type and the first line of its message, from a failed report."""
    crash = getattr(report.longrepr, "reprcrash", None)
    message = getattr(crash, "message", None)
    if not isinstance(message, str):
        message = str(report.longrepr or "")
    lines = [line for line in message.splitlines() if line.strip()]
    return printable(lines[0] if lines else f"failed in {report.when}")


def _report_option(arg: str) -> tuple[bool, bool]:
    """``(is a report-writing option, its value is the next token)`` for one token."""
    name, has_value = arg.split("=", 1)[0], "=" in arg
    if name in _REPORT_OPTIONS or name in _COV_VALUE_OPTIONS:
        return True, not has_value
    if name == "--cov" or name.startswith("--cov-"):
        return True, False
    if name == "--json-report":
        # The switch alone writes `.report.json`; without it nothing is written.
        return True, False
    return False, False


def drop_report_options(args: list[str]) -> list[str]:
    """``args`` without the options that write a report file (`--junitxml`, `--cov*`, ...).

    The retry pass would rewrite the first session's report with only the
    retried tests.
    """
    kept = []
    skip_value = False
    for index, arg in enumerate(args):
        if skip_value:
            skip_value = False
            continue
        report, takes_next = _report_option(arg)
        if report:
            following = args[index + 1] if index + 1 < len(args) else ""
            # `--cov` takes an optional value: argparse binds a following
            # non-option token to it, never to the file arguments.
            skip_value = takes_next or (
                arg == "--cov" and following != "" and not following.startswith("-")
            )
            continue
        kept.append(arg)
    return kept


def names_plugin(args: list[str]) -> bool:
    """Whether ``args`` already load this plugin with ``-p``."""
    pairs = zip(args, args[1:] + [""])
    return any(arg == "-p" + PLUGIN or (arg, after) == ("-p", PLUGIN) for arg, after in pairs)


def retry_argv(
    args: list[str],
    xdist: bool,
    addopts: list[str] | None = None,
    env_addopts: list[str] | None = None,
) -> list[str]:
    """The session's own arguments, serial, for a pass that deselects all but the failed tests.

    Report-writing options are dropped from the command line and from the ini
    ``addopts`` (passed back as the last ``-o addopts=``, which wins over the
    ini file and any earlier override).
    """
    options = drop_report_options(args)
    ini = drop_report_options(list(addopts or []))
    loaded = names_plugin(options) or names_plugin(ini) or names_plugin(list(env_addopts or []))
    extra = ([] if loaded else ["-p", PLUGIN]) + (["-n", "0"] if xdist else [])
    return [sys.executable, "-m", "pytest", *options, *extra, "-o", "addopts=" + shlex.join(ini)]


def retry_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` with report-writing options dropped from $PYTEST_ADDOPTS."""
    env = dict(env)
    if env.get("PYTEST_ADDOPTS"):
        env["PYTEST_ADDOPTS"] = shlex.join(drop_report_options(shlex.split(env["PYTEST_ADDOPTS"])))
    return env


class _Retry:
    """The first session's half: collect failures, retry them, record flaky passes."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.failures: dict[str, str] = {}
        self.crashed = False
        self.collection_failed = False

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.collection_failed = True

    def pytest_runtest_logreport(self, report: Any) -> None:
        if not report.failed:
            return
        if report.when not in _PHASES:
            self.crashed = True
            return
        self.failures.setdefault(report.nodeid, failure_summary(report))

    @staticmethod
    def _say(session: Any, line: str) -> None:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(line)
        else:
            print(line)

    def pytest_sessionfinish(self, session: Any, exitstatus: Any) -> None:
        if int(exitstatus) != 1 or not self.failures:
            return
        if self.crashed:
            self._say(session, "shard-retry: a worker crashed; no retry.")
            return
        if self.collection_failed:
            # A retry cannot clear a collection error, so a green one would hide it.
            self._say(session, "shard-retry: a collection error; no retry.")
            return
        if session.shouldfail or session.shouldstop:
            # `-x` / `--maxfail` left later tests unrun: a green retry would hide them.
            self._say(session, "shard-retry: the session stopped early; no retry.")
            return
        if len(self.failures) > MAX_RETRY:
            self._say(
                session,
                f"shard-retry: {len(self.failures)} tests failed, over the cap of "
                f"{MAX_RETRY}; no retry (many failures point at a real break).",
            )
            return
        nodeids = list(self.failures)
        passed, retry_ok = self._run_retry(session, nodeids)
        flaky = [nodeid for nodeid in nodeids if nodeid in passed]
        env = dict(os.environ)
        path = record_path(env)
        try:
            if flaky:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    for nodeid in flaky:
                        row = {
                            "nodeid": passed[nodeid],
                            "first_failure": self.failures[nodeid],
                            "shard": env.get(ENV_SHARD) or None,
                            "head_sha": env.get(ENV_HEAD_SHA) or None,
                        }
                        handle.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError as exc:
            self._say(
                session, f"::warning::shard-retry: cannot write {path} ({exc}); job stays red."
            )
            return
        self._say(
            session, f"shard-retry: {len(flaky)} flaky test(s) passed on retry, recorded in {path}"
        )
        if retry_ok and len(flaky) == len(nodeids):
            session.exitstatus = 0
        elif not retry_ok:
            self._say(session, "shard-retry: the retry pass did not exit 0; job stays red.")

    def _run_retry(self, session: Any, nodeids: list[str]) -> tuple[dict[str, str], bool]:
        """``({failed id: its plain id} for each that passed, whether the pass exited 0)``."""
        config = self.config
        env = dict(os.environ)
        argv = retry_argv(
            list(config.invocation_params.args),
            config.pluginmanager.hasplugin("xdist"),
            [str(arg) for arg in config.getini("addopts")],
            shlex.split(env.get("PYTEST_ADDOPTS") or ""),
        )
        self._say(session, f"shard-retry: retrying {len(nodeids)} failed test(s) once.")
        with tempfile.TemporaryDirectory(prefix="ci-shard-retry-") as scratch:
            results = Path(scratch) / "results.json"
            env = retry_env(env)
            env[ENV_RESULTS] = str(results)
            env[ENV_TARGETS] = json.dumps(nodeids)
            capture = config.pluginmanager.get_plugin("capturemanager")
            # The retry pass writes to the job log, not into pytest's capture.
            uncaptured = (
                capture.global_and_fixture_disabled() if capture is not None else nullcontext()
            )
            try:
                with uncaptured:
                    done = subprocess.run(
                        argv,
                        cwd=config.invocation_params.dir,
                        env=env,
                        timeout=RETRY_TIMEOUT_SECONDS,
                        check=False,
                    )
                outcome = json.loads(results.read_text(encoding="utf-8"))
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                self._say(
                    session, f"::warning::shard-retry: the retry pass failed to report ({exc})."
                )
                return {}, False
        passed = outcome.get("passed") if isinstance(outcome, dict) else None
        if not isinstance(passed, dict):
            return {}, False
        return {str(k): str(v) for k, v in passed.items()}, done.returncode == 0


class _Results:
    """The retry pass's half: run only the failed tests, write each one's outcome."""

    def __init__(self, path: str, targets: list[str]) -> None:
        self.path = Path(path)
        self.targets = targets
        # collected id -> the failed id it stands for
        self.matched: dict[str, str] = {}
        self.passed: set[str] = set()
        self.failed: set[str] = set()

    def pytest_collection_modifyitems(self, config: Any, items: list[Any]) -> None:
        self.matched = match_targets([item.nodeid for item in items], self.targets)
        kept = [item for item in items if item.nodeid in self.matched]
        dropped = [item for item in items if item.nodeid not in self.matched]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = kept

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_sessionfinish(self, session: Any, exitstatus: Any) -> None:
        passed = {
            self.matched[nodeid]: nodeid
            for nodeid in sorted(self.passed - self.failed)
            if nodeid in self.matched
        }
        self.path.write_text(json.dumps({"passed": passed}), encoding="utf-8")


def _targets(env: dict[str, str]) -> list[str]:
    try:
        targets = json.loads(env.get(ENV_TARGETS) or "[]")
    except ValueError:
        return []
    return [str(target) for target in targets] if isinstance(targets, list) else []


def pytest_configure(config: Any) -> None:
    if hasattr(config, "workerinput"):
        return
    env = dict(os.environ)
    if env.get(ENV_RESULTS):
        plugin = _Results(env[ENV_RESULTS], _targets(env))
        config.pluginmanager.register(plugin, "ci-shard-retry-results")
    elif enabled(env):
        config.pluginmanager.register(_Retry(config), "ci-shard-retry")


# -- summary: the job summary step ------------------------------------------


def read_record(path: Path) -> list[dict[str, Any]]:
    """The record's rows; a missing file or a malformed line gives no row."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("nodeid"), str):
            rows.append(row)
    return rows


def summary_line(row: dict[str, Any]) -> str:
    nodeid = printable(row["nodeid"], 400).replace("`", "'")
    failure = printable(str(row.get("first_failure") or "")).replace("`", "'")
    return f"- Flaky, passed on retry: `{nodeid}` (first failure: {failure})"


def cmd_summary(env: dict[str, str]) -> int:
    """Write one job summary line per flaky test in the record; never fails the step."""
    rows = read_record(record_path(env))
    if not rows:
        print("Shard retry: no flaky retries.")
        return 0
    text = "".join(summary_line(row) + "\n" for row in rows)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text, end="")
    return 0


COMMANDS = {"summary": cmd_summary}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print("usage: ci_shard_retry.py summary", file=sys.stderr)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]](dict(os.environ)))
