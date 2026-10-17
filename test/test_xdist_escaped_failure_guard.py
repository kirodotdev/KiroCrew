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

The subprocess test proves reporting end to end. Deterministic unit tests drive the
real root ``pytest_runtest_protocol`` generator with an injected clock and ``CallInfo``
values to pin duration accounting without depending on machine speed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import _pytest.outcomes
import _pytest.runner
import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_PLUGIN = textwrap.dedent("""
    import json
    import os
    import pathlib
    import signal
    import time
    from types import SimpleNamespace

    from _pytest import timing
    import conftest
    import pytest

    _FIRED = []
    _CLOCK = 1000.0


    def advance(seconds):
        global _CLOCK
        _CLOCK += seconds


    @pytest.hookimpl(tryfirst=True, wrapper=True)
    def pytest_runtest_protocol(item, nextitem):
        # Registered after the root plugin, so this brackets its tryfirst wrapper
        # too. Change clock inputs only: real CallInfo, reports, teardown and xdist
        # still run. A module-local proxy leaves stdlib time and timeout timers real.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(timing, "perf_counter", lambda: _CLOCK)
            patch.setattr(
                conftest, "time",
                SimpleNamespace(perf_counter=lambda: _CLOCK, time=time.time),
            )
            return (yield)



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
            entry = {
                "nodeid": report.nodeid, "when": report.when,
                "outcome": report.outcome, "duration": report.duration,
            }
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
    def tracked(request):
        from escape_plugin import advance

        # First-item setup cost need not match the following bystander's cost.
        if request.node.name == "test_victim":
            advance(2.0)
        yield "value"
        advance(0.5)


    @pytest.mark.timeout(120)
    def test_victim(tracked):
        from escape_plugin import advance

        time.sleep(3.0)
        advance(3.0)
        assert tracked == "value"


    def test_bystander(tracked):
        assert tracked == "value"


    @pytest.mark.skip(reason="collected but never run")
    def test_skipped_bystander():
        raise AssertionError("skip marker did not apply")
    """)


def _run_inner_pytest(tmp_path, env, *args):
    # The inner session loads the root conftest, whose ``pytest_configure`` points
    # the platform temp dir at ``/tmp`` on Darwin. Left to its default, the inner
    # basetemp would then be the SHARED ``/tmp/pytest-of-<user>`` -- the same tree
    # the outer run and every concurrent xdist worker prune at startup -- and any
    # ``garbage-*`` left there by an unrelated run surfaces in THIS process's output
    # as an ``(rm_rf) error removing`` warning. An explicit basetemp under the outer
    # test's tmp_path is used verbatim (no ``gettempdir()`` lookup, no sibling
    # pruning), so the inner output only ever describes the inner run.
    #
    # The inner session also gets its OWN, empty config file. Without one pytest
    # walks up from ``tmp_path`` looking for an ini, and whenever the outer TMPDIR
    # sits inside the checkout (a pinned ``TMPDIR=<repo>/hygiene/tmp``, a worktree
    # under the source tree) it finds the repository's ``setup.cfg``: the rootdir
    # becomes the repo, every nodeid and pytest-split durations key grows a
    # ``<relative tmp_path>/`` prefix, and the repo ``addopts`` (``--color=yes``,
    # ``--verbose``, ``--timeout``) reshape the very summary lines asserted below.
    # ``-c`` pins the inifile and therefore the rootdir to ``tmp_path`` regardless
    # of where the host put it; ``--color=no`` keeps the captured output plain even
    # when the host exports ``PY_COLORS``/``FORCE_COLOR`` into the inherited env.
    inifile = tmp_path / "pytest.ini"
    if not inifile.exists():
        inifile.write_text("[pytest]\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(inifile),
            f"--rootdir={tmp_path}",
            "--color=no",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'inner-basetemp'}",
            *args,
        ],
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
    victim_nodeid = "test_escape.py::test_victim@escape_guard"
    bystander_nodeid = "test_escape.py::test_bystander@escape_guard"
    skipped_nodeid = "test_escape.py::test_skipped_bystander@escape_guard"
    for nodeid in (victim_nodeid, bystander_nodeid, skipped_nodeid):
        assert nodeid in durations, (escape_site, durations)
        assert durations[nodeid] >= 0.0, (escape_site, durations)
    victim_duration = durations[victim_nodeid]
    bystander_duration = durations[bystander_nodeid]
    # The fixture owns the clock inputs: victim setup=2, call=3, teardown=0.5;
    # bystander setup/call=0, teardown=0.5. No expected value comes from a report
    # or the guard's accumulator. Exact phase budgets catch both recharging setup
    # on a synthesized call and recharging the call on a replacement teardown.
    assert victim_duration == 5.5, (escape_site, durations)
    assert bystander_duration == 0.5, (escape_site, durations)
    victim_phase_durations = [
        report["duration"] for report in reports if "::test_victim" in report["nodeid"]
    ]
    assert victim_phase_durations == [2.0, 3.0, 0.5], (escape_site, reports)

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


# ── Deterministic unit tests of the real pytest_runtest_protocol accounting ──
#
# The subprocess test above exercises the guard through a real xdist+split session
# with fixture-owned phase budgets. These additional tests drive the SAME guard
# code -- the root conftest's ``pytest_runtest_protocol`` generator -- directly,
# with an injected clock and injected ``CallInfo`` values, so individual branches
# and exact duration attribution are also checked independently.


def _load_root_conftest():
    """The ROOTDIR conftest, loaded by path as its own module instance.

    A bare ``import conftest`` from ``test/`` resolves ``test/conftest.py``, which does
    not own this guard. Loading a fresh instance also gives it its own ``time`` binding
    and its own ``_escape_logged_reports`` dict, so patching the clock and seeding logged
    phases here cannot perturb the live session running this test.
    """
    spec = importlib.util.spec_from_file_location(
        "_root_conftest_escape_guard", _REPO_ROOT / "conftest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FrozenClock:
    """Deterministic stand-in for the ``time`` module the guard reads.

    ``pytest_runtest_protocol`` consumes exactly two ``perf_counter`` reads (protocol
    start, then stop) and one ``time`` read; everything else forwards to the real module
    so no other attribute the guard might touch changes behaviour.
    """

    def __init__(self, perf_values, wall):
        self._perf = list(perf_values)
        self._wall = wall

    def perf_counter(self):
        assert self._perf, "perf_counter called more times than the guard should"
        return self._perf.pop(0)

    def time(self):
        return self._wall

    def __getattr__(self, name):
        import time as _stdlib_time

        return getattr(_stdlib_time, name)


def _failed_escape_excinfo():
    """A real ``(Failed, value, tb)`` triple, as pytest-timeout's ``pytest.fail`` leaves."""
    try:
        pytest.fail("Timeout >120.0s (synthetic escape)")
    except BaseException:  # noqa: BLE001 - capturing the escape is the whole point
        return sys.exc_info()


def _drive_protocol(root, *, logged_phases, protocol_duration, teardown_duration):
    """Run the real guard generator once and capture what it emitted.

    ``logged_phases`` is a list of ``(when, duration)`` seeded through the guard's REAL
    ``pytest_runtest_logreport`` tracker, so the accounting under test does the summing.
    ``CallInfo.from_call`` is replaced with a deterministic version that still runs the
    real func (so ``excinfo``/result are genuine) but stamps a fixed duration, letting the
    teardown branch's ``CallInfo.duration`` be pinned exactly.
    """
    nodeid = "deterministic::escape_guard_case"
    makereport_calls = []
    logged_reports = []
    finishes = []
    interacts = []
    force_results = []

    def _makereport(item, call):
        makereport_calls.append(call)
        return SimpleNamespace(
            nodeid=item.nodeid,
            when=call.when,
            duration=call.duration,
            outcome="failed" if call.excinfo is not None else "passed",
        )

    ihook = SimpleNamespace(
        pytest_runtest_makereport=_makereport,
        pytest_runtest_logreport=lambda report: logged_reports.append(report),
        pytest_runtest_teardown=lambda item, nextitem: None,
        pytest_runtest_logfinish=lambda nodeid, location: finishes.append(nodeid),
        pytest_exception_interact=lambda node, call, report: interacts.append(report),
    )
    item = SimpleNamespace(
        nodeid=nodeid,
        ihook=ihook,
        location=("test_escape.py", 0, "test_victim"),
        config=SimpleNamespace(getoption=lambda name, default=None: default),
    )
    outcome = SimpleNamespace(
        excinfo=_failed_escape_excinfo(),
        force_result=lambda value: force_results.append(value),
    )

    def _fake_from_call(func, when, reraise=None):
        excinfo = None
        try:
            result = func()
        except BaseException:  # noqa: BLE001 - mirror CallInfo.from_call's own capture
            excinfo = pytest.ExceptionInfo.from_current()
            if reraise is not None and isinstance(excinfo.value, reraise):
                raise
            result = None
        stamp = teardown_duration if when == "teardown" else 0.0
        return _pytest.runner.CallInfo(
            result=result,
            excinfo=excinfo,
            start=500.0,
            stop=500.0 + stamp,
            duration=stamp,
            when=when,
            _ispytest=True,
        )

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(
            root, "time", _FrozenClock([1000.0, 1000.0 + protocol_duration], wall=500.0)
        )
        patched.setattr(_pytest.runner.CallInfo, "from_call", staticmethod(_fake_from_call))
        generator = root.pytest_runtest_protocol(item=item, nextitem=None)
        next(generator)
        for when, phase_duration in logged_phases:
            root.pytest_runtest_logreport(
                report=SimpleNamespace(nodeid=nodeid, when=when, duration=phase_duration)
            )
        with pytest.raises(StopIteration):
            generator.send(outcome)

    return SimpleNamespace(
        nodeid=nodeid,
        makereport_calls=makereport_calls,
        logged_reports=logged_reports,
        finishes=finishes,
        interacts=interacts,
        force_results=force_results,
    )


def test_missing_call_report_synthesizes_the_earliest_missing_phase_with_untracked_time():
    """No call report: synthesize the earliest missing phase and charge only untracked time.

    Logged setup=2s inside a protocol that ran 10s means the synthesized call report owns
    ``max(10 - 2, 0) = 8`` -- the protocol time no logged phase has already accounted for --
    never the full 10s. Teardown still runs so the worker is left clean.
    """
    root = _load_root_conftest()
    result = _drive_protocol(
        root,
        logged_phases=[("setup", 2.0)],
        protocol_duration=10.0,
        teardown_duration=0.0,
    )

    assert [call.when for call in result.makereport_calls] == ["call", "teardown"]
    synth_call = result.makereport_calls[0]
    assert synth_call.excinfo is not None
    assert isinstance(synth_call.excinfo.value, _pytest.outcomes.Failed)
    assert synth_call.duration == pytest.approx(8.0)
    assert synth_call.duration >= 0.0
    assert synth_call.duration != pytest.approx(10.0)
    assert result.force_results == [True]
    assert result.finishes == [result.nodeid]


def test_logged_call_without_teardown_charges_the_escape_only_the_real_teardown_time():
    """Call logged, teardown missing: escape rides a teardown report owning only teardown time.

    Setup+call already logged 5s of a 10s protocol, and the replacement teardown's own
    ``CallInfo`` measured 1.5s. The synthesized teardown report must own exactly that 1.5s
    -- never the whole protocol (10s) nor the untracked remainder (10 - 5 = 5s).
    """
    root = _load_root_conftest()
    result = _drive_protocol(
        root,
        logged_phases=[("setup", 3.0), ("call", 2.0)],
        protocol_duration=10.0,
        teardown_duration=1.5,
    )

    assert [call.when for call in result.makereport_calls] == ["teardown"]
    synth_teardown = result.makereport_calls[0]
    assert isinstance(synth_teardown.excinfo.value, _pytest.outcomes.Failed)
    assert synth_teardown.duration == pytest.approx(1.5)
    assert synth_teardown.duration != pytest.approx(10.0)
    assert synth_teardown.duration != pytest.approx(5.0)
    # A Failed teardown is an interactive exception, reported once against the item.
    assert len(result.interacts) == 1
    assert result.force_results == [True]
    assert result.finishes == [result.nodeid]


def test_all_phases_already_logged_emits_no_extra_report():
    """Every phase already reached the controller: the guard adds nothing, just closes out."""
    root = _load_root_conftest()
    result = _drive_protocol(
        root,
        logged_phases=[("setup", 1.0), ("call", 1.0), ("teardown", 1.0)],
        protocol_duration=10.0,
        teardown_duration=0.0,
    )

    assert result.makereport_calls == []
    assert result.interacts == []
    assert result.force_results == [True]
    assert result.finishes == [result.nodeid]


def test_a_non_failed_outcome_is_left_untouched():
    """The guard only repairs a ``Failed`` escape: a normal outcome passes straight through."""
    root = _load_root_conftest()
    nodeid = "deterministic::escape_guard_passthrough"
    finishes = []
    force_results = []
    makereport_calls = []
    ihook = SimpleNamespace(
        pytest_runtest_makereport=lambda item, call: makereport_calls.append(call),
        pytest_runtest_logreport=lambda report: None,
        pytest_runtest_logfinish=lambda nodeid, location: finishes.append(nodeid),
    )
    item = SimpleNamespace(nodeid=nodeid, ihook=ihook, location=("test_escape.py", 0, "ok"))
    outcome = SimpleNamespace(excinfo=None, force_result=lambda value: force_results.append(value))

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(root, "time", _FrozenClock([1000.0, 1010.0], wall=500.0))
        generator = root.pytest_runtest_protocol(item=item, nextitem=None)
        next(generator)
        with pytest.raises(StopIteration):
            generator.send(outcome)

    assert makereport_calls == []
    assert finishes == []
    assert force_results == []
