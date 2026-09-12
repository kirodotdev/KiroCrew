"""A ``Failed`` that escapes the runtest protocol fails one test, not the session.

pytest-timeout's SIGALRM handler calls ``pytest.fail`` from wherever the timer
fires. Inside setup, call or teardown that is an ordinary failure; outside them
-- while pytest renders a report, between phases -- it propagates out of
``pytest_runtest_protocol`` with no report logged, and under xdist the controller
turns that into an INTERNALERROR that erases the whole shard's results. The root
``conftest.py`` wraps the protocol and converts such an escape into a failure
report for the item that owned the timer.

The test drives the exact escape path deterministically with a tiny plugin that
raises ``pytest.fail`` from a hook that runs outside every ``CallInfo``, in a real
xdist session that loads the root conftest as a plugin. Three escape sites prove the
one-report-per-phase rule: ``makereport`` before the call report exists, and
``logreport`` after xdist has sent the call or teardown report. The skipped bystander
proves every collected item still produces a pytest-split durations key, which the CI
count gate requires.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import textwrap

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_PLUGIN = textwrap.dedent("""
    import json
    import os
    import pathlib
    import signal

    import pytest

    _FIRED = []


    _SITE = os.environ["ESCAPE_SITE"]


    def _escape_once(nodeid):
        # Worker side only: the controller replays reports through these hooks
        # too, and a raise there is a different failure than the one under test.
        # Once, like a timer that has been cancelled: the guard's own synthesized
        # report for the victim passes through these hooks as well.
        if not os.environ.get("PYTEST_XDIST_WORKER"):
            return
        if "::test_victim" in nodeid and not _FIRED:
            _FIRED.append(nodeid)
            pytest.fail("Timeout >120.0s (synthetic escape)")


    def pytest_runtest_makereport(item, call):
        # Where pytest-timeout's SIGALRM lands when it fires while pytest renders
        # the call report: the call phase has finished, its report does not exist
        # yet, so nothing for this phase reaches the controller unless the guard
        # synthesizes it.
        if _SITE == "makereport" and call.when == "call":
            _escape_once(item.nodeid)


    def pytest_runtest_logreport(report):
        # Record worker-side reports so the outer test can assert that no protocol
        # phase was reported twice. The controller also loads this plugin.
        if os.environ.get("PYTEST_XDIST_WORKER"):
            entry = {"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome}
            if report.when == "teardown":
                entry["timer_armed"] = (
                    signal.getitimer(signal.ITIMER_REAL)[0] > 0
                    if hasattr(signal, "setitimer")
                    else None
                )
            with pathlib.Path(os.environ["REPORTS_PATH"]).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry) + "\\n")
        # Where pytest-timeout's SIGALRM lands when it fires while the call report
        # is being logged. xdist's own implementation registered later and ran
        # first, so the real call report is already on its way to the controller.
        if _SITE == "logreport" and report.when == "call":
            _escape_once(report.nodeid)
        # A synthetic escape after xdist sent teardown pins the no-duplicate branch.
        # Real timeout alarms cannot reach this point because makereport disarms them.
        if _SITE == "teardown_logreport" and report.when == "teardown":
            _escape_once(report.nodeid)
    """)

_TESTS = textwrap.dedent("""
    import time
    import pytest

    # One worker for the whole module: the bystander must run AFTER the victim
    # on the same worker, where an item left un-torn-down would make its setup
    # fail with "previous item was not torn down properly".
    pytestmark = pytest.mark.xdist_group("escape_guard")


    @pytest.fixture
    def tracked():
        yield "value"


    @pytest.mark.timeout(120)
    def test_victim(tracked):
        time.sleep(3.0)
        assert tracked == "value"


    def test_bystander(tracked):
        assert tracked == "value"


    @pytest.mark.skip(reason="collected but never run")
    def test_skipped_bystander():
        raise AssertionError("skip marker did not apply")
    """)


def _run_inner_pytest(tmp_path, env, *args):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=200,
    )


@pytest.mark.timeout(240)
@pytest.mark.parametrize("escape_site", ["makereport", "logreport", "teardown_logreport"])
def test_escaped_failed_is_reported_against_its_test_not_as_internalerror(tmp_path, escape_site):
    (tmp_path / "escape_plugin.py").write_text(_PLUGIN, encoding="utf-8")
    (tmp_path / "test_escape.py").write_text(_TESTS, encoding="utf-8")
    durations_path = tmp_path / "durations.json"
    reports_path = tmp_path / "reports.jsonl"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_REPO_ROOT), str(tmp_path), env.get("PYTHONPATH", "")) if p
    )
    env.pop("PYTEST_XDIST_WORKER", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    # The inner session installs its own import-time data-home floor. Passing the
    # outer per-test home down would be refused whenever the outer TMPDIR sits
    # under the live ~/.kiro/crew tree, which is the containment that floor exists
    # to enforce.
    env.pop("KIROCREW_HOME", None)
    env.pop("KIROCREW_WORKSPACE", None)
    env["ESCAPE_SITE"] = escape_site
    env["REPORTS_PATH"] = str(reports_path)

    proc = _run_inner_pytest(
        tmp_path,
        env,
        "-p",
        "conftest",
        "-p",
        "escape_plugin",
        "-p",
        "pytest_split",
        "-n",
        "2",
        "--dist",
        "loadgroup",
        "-q",
        "--store-durations",
        "--clean-durations",
        "--durations-path",
        str(durations_path),
        "test_escape.py",
    )
    out = proc.stdout + proc.stderr

    assert "INTERNALERROR" not in out, out
    assert proc.returncode == (0 if escape_site == "teardown_logreport" else 1), out
    assert "not torn down" not in out, out

    reports = [json.loads(line) for line in reports_path.read_text(encoding="utf-8").splitlines()]
    victim_reports = [
        (report["when"], report["outcome"])
        for report in reports
        if "::test_victim" in report["nodeid"]
    ]
    if escape_site == "makereport":
        assert (
            "FAILED test_escape.py::test_victim@escape_guard - Failed: Timeout >120.0s" in out
        ), out
        assert "1 failed" in out and "error" not in out.split("short test summary")[-1], out
        assert victim_reports == [("setup", "passed"), ("call", "failed"), ("teardown", "passed")]
    elif escape_site == "logreport":
        assert (
            "ERROR test_escape.py::test_victim@escape_guard - Failed: Timeout >120.0s" in out
        ), out
        assert "FAILED test_escape.py::test_victim@escape_guard" not in out, out
        assert "2 passed" in out and "1 error" in out, out
        assert victim_reports == [("setup", "passed"), ("call", "passed"), ("teardown", "failed")]
    else:
        assert "FAILED test_escape.py::test_victim@escape_guard" not in out, out
        assert "ERROR test_escape.py::test_victim@escape_guard" not in out, out
        assert "2 passed" in out, out
        assert victim_reports == [
            ("setup", "passed"),
            ("call", "passed"),
            ("teardown", "passed"),
        ]

    if hasattr(signal, "setitimer"):
        for test_name in ("test_victim", "test_bystander"):
            teardown = [
                report
                for report in reports
                if f"::{test_name}" in report["nodeid"] and report["when"] == "teardown"
            ]
            assert len(teardown) == 1, (escape_site, test_name, teardown)
            assert teardown[0]["timer_armed"] is False, (escape_site, test_name, teardown)

    durations = json.loads(durations_path.read_text(encoding="utf-8"))
    victim_duration = durations["test_escape.py::test_victim@escape_guard"]
    bystander_duration = durations["test_escape.py::test_bystander@escape_guard"]
    skipped_nodeid = "test_escape.py::test_skipped_bystander@escape_guard"
    assert skipped_nodeid in durations
    assert victim_duration >= 3.0, (escape_site, durations)
    assert victim_duration - bystander_duration < 4.5, (escape_site, durations)

    collect_proc = _run_inner_pytest(
        tmp_path,
        env,
        "-n",
        "0",
        "--collect-only",
        "-q",
        "--no-cov",
        "test_escape.py",
    )
    collect_out = collect_proc.stdout + collect_proc.stderr
    assert collect_proc.returncode == 0, collect_out
    collected = re.search(r"(\d+) tests? collected", collect_out)
    assert collected is not None, collect_out
    assert len(durations) == int(collected.group(1)), (durations, collect_out)
