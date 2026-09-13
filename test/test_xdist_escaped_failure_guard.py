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
xdist session that loads the root conftest as a plugin. Two escape sites, because
pytest-split sums every report's duration per node id and the guard must get the
total right at both: ``makereport`` for the call phase (the test body has run, no
call report exists yet -- the synthesized report is the only one, so it must carry
the elapsed time) and ``logreport`` of the call report (xdist has already sent the
real call report -- the synthesized report must NOT charge that time again).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_PLUGIN = textwrap.dedent("""
    import os

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
        # Where it lands when it fires while the call report is being logged.
        # xdist's own logreport impl registered later and so ran first: the real
        # call report is already on its way to the controller when this raises.
        if _SITE == "logreport" and report.when == "call":
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


    def test_victim(tracked):
        time.sleep(3.0)
        assert tracked == "value"


    def test_bystander(tracked):
        assert tracked == "value"
    """)


@pytest.mark.timeout(240)
@pytest.mark.parametrize("escape_site", ["makereport", "logreport"])
def test_escaped_failed_is_reported_against_its_test_not_as_internalerror(tmp_path, escape_site):
    (tmp_path / "escape_plugin.py").write_text(_PLUGIN, encoding="utf-8")
    (tmp_path / "test_escape.py").write_text(_TESTS, encoding="utf-8")
    durations_path = tmp_path / "durations.json"

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

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
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
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=200,
    )
    out = proc.stdout + proc.stderr

    # Without the guard the worker dies with INTERNALERROR and the session
    # reports whatever it had, which for this two-test file is "2 passed" and
    # exit 0: the escaped failure is swallowed, not surfaced.
    assert "INTERNALERROR" not in out, out
    assert proc.returncode == 1, out
    assert "FAILED test_escape.py::test_victim@escape_guard - Failed: Timeout >120.0s" in out, out
    # The victim's fixtures were torn down, so the bystander that follows it on
    # the same worker sets up cleanly instead of erroring on the leftover stack.
    assert "not torn down" not in out, out
    assert "1 failed" in out and "error" not in out.split("short test summary")[-1], out
    # pytest-split stores the sum of the victim's report durations. At the
    # makereport site the synthesized call report is the only call report and
    # must carry the 3 s the body slept: a ~0 s entry is what would make
    # pytest-split schedule a 120 s timeout as a free test. At the logreport site
    # the real call report already carries those 3 s, and the synthesized one
    # must not charge them a second time: a doubled entry skews the split the
    # other way. The bystander runs the same fixtures with no sleep and no
    # escape, so it measures this host's setup+teardown cost; the victim may
    # exceed it by the one sleep, never by two.
    durations = json.loads(durations_path.read_text(encoding="utf-8"))
    victim_duration = durations["test_escape.py::test_victim@escape_guard"]
    bystander_duration = durations["test_escape.py::test_bystander@escape_guard"]
    assert victim_duration >= 3.0, (escape_site, durations)
    assert victim_duration - bystander_duration < 4.5, (escape_site, durations)
