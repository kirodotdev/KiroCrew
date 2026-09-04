"""A red pytest job must annotate the TESTS that failed (issue #7296).

Annotations are what a check run shows: the PR page renders them, a fork
contributor who cannot re-run a job has nothing else, and a triage report copies
them. Nothing in this repository wrote one, so they came from the ``python``
problem matcher ``actions/setup-python`` registers by default -- a two-line
pattern (a traceback frame, then ``raise SomeError('msg')``) applied to the whole
log, including the part where pytest prints its WARNINGS summary.

MEASURED on the six Backend Tests jobs cited in #7296: that pattern matched a
warning traceback every time and a pytest failure not once. All six reds carried
only ``Event loop is closed`` at line 545 -- ``asyncio/base_events.py`` inside
``_check_closed``, reached from a ``PytestUnraisableExceptionWarning`` about a
garbage-collected coroutine -- while the tests that actually failed (a ``git
add`` timeout, a missing diag.jsonl, sandbox-dependent project tests) were named
nowhere. Four unrelated PRs were filed and triaged as an event-loop teardown
flake on that evidence.

So these tests pin the two halves of the answer: the rootdir conftest turns each
failing report into an annotation that names the test, and every workflow job
that runs pytest has the matcher that used to lie turned off.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
from types import SimpleNamespace

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ROOT_CONFTEST = _REPO_ROOT / "conftest.py"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"


def _load_root_conftest():
    """Import the rootdir conftest under its own module name.

    ``from conftest import ...`` inside ``test/`` binds to ``test/conftest.py``
    (prepend import mode puts this directory on ``sys.path`` first), so the
    rootdir file has to be loaded by path. Same idiom as
    ``test_host_isolation_floor.py``; the fixtures it defines are inert here
    because nothing in this namespace collects them.
    """
    spec = importlib.util.spec_from_file_location("_kirocrew_annotations_conftest", _ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()


class _Reporter:
    """Minimal stand-in for pytest's terminal reporter."""

    def __init__(self, stats: dict[str, list[object]]) -> None:
        self.stats = stats
        self.lines: list[str] = []

    def write_line(self, line: str) -> None:
        self.lines.append(line)


def _report(
    nodeid: str,
    *,
    path: str = "test/test_thing.py",
    lineno: int | None = 41,
    message: str = "AssertionError: assert False",
) -> SimpleNamespace:
    """A stand-in carrying the attributes the emitter reads off a real report."""
    location = (path, lineno, nodeid) if path else ()
    return SimpleNamespace(
        nodeid=nodeid,
        location=location,
        longrepr=SimpleNamespace(reprcrash=SimpleNamespace(message=message)),
        longreprtext=message,
    )


def _emit(stats: dict[str, list[object]], monkeypatch: pytest.MonkeyPatch) -> _Reporter:
    """Run the hook as GitHub Actions would and return what it wrote."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    reporter = _Reporter(stats)
    _root.pytest_terminal_summary(reporter, 1, SimpleNamespace())
    return reporter


class TestTheAnnotationNamesTheFailingTest:
    """The whole point: a reader of the check run learns which test failed."""

    def test_a_failure_is_annotated_with_its_node_id_file_and_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """File and line come from ``report.location``, which is 0-based."""
        nodeid = "test/test_md_notebook.py::test_sync_refuses_rather_than_pushing_a_subset"
        reporter = _emit(
            {"failed": [_report(nodeid, path="test/test_md_notebook.py", lineno=41)]},
            monkeypatch,
        )

        assert len(reporter.lines) == 1, reporter.lines
        line = reporter.lines[0]
        assert line.startswith("::error "), line
        assert "file=test/test_md_notebook.py" in line
        # 41 is 0-based in the report; the annotation is 1-based.
        assert "line=42" in line
        assert "test_sync_refuses_rather_than_pushing_a_subset" in line
        assert line.endswith("AssertionError: assert False")

    def test_setup_and_teardown_errors_are_annotated_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``error`` is a separate stats bucket, and it reds the job the same way."""
        reporter = _emit(
            {"error": [_report("test/test_x.py::test_y", message="ImportError: no module")]},
            monkeypatch,
        )

        assert len(reporter.lines) == 1, reporter.lines
        assert "test_y" in reporter.lines[0]
        assert reporter.lines[0].endswith("ImportError: no module")

    def test_a_collection_error_without_a_line_omits_the_line_property(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A guessed line points the reader at an unrelated statement."""
        reporter = _emit(
            {"error": [_report("test/test_x.py", path="test/test_x.py", lineno=None)]},
            monkeypatch,
        )

        assert "line=" not in reporter.lines[0], reporter.lines[0]
        assert "file=test/test_x.py" in reporter.lines[0]

    def test_a_report_with_no_failure_detail_still_names_the_test(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Losing the reason is survivable; losing the test id is the defect."""
        report = _report("test/test_x.py::test_y", message="")
        report.longreprtext = ""
        reporter = _emit({"failed": [report]}, monkeypatch)

        assert "test/test_x.py::test_y" in reporter.lines[0].replace("%3A", ":")
        assert "no failure detail" in reporter.lines[0]

    def test_a_fixture_error_reports_the_error_not_the_preamble(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``reprcrash`` is the preamble for this shape, MEASURED on a real run.

        An annotation built from it reads ``file <path>, line 5`` and spends its
        whole width saying nothing. The verdict is the ``E`` line below it.
        """
        report = _report("test/test_x.py::test_y", message="file /repo/test/test_x.py, line 5")
        report.longreprtext = (
            "file /repo/test/test_x.py, line 5\n"
            "      def test_y(nonexistent_fixture):\n"
            "E       fixture 'nonexistent_fixture' not found\n"
            ">       available fixtures: anyio_backend, cache\n"
        )
        reporter = _emit({"failed": [report]}, monkeypatch)

        assert reporter.lines[0].endswith("fixture 'nonexistent_fixture' not found")

    def test_an_indented_source_line_is_not_mistaken_for_the_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pytest puts its marker in column 0; a statement is indented."""
        report = _report("test/test_x.py::test_y", message="AssertionError: boom")
        report.longreprtext = "      E = compute()\nE       AssertionError: boom\n"
        reporter = _emit({"failed": [report]}, monkeypatch)

        assert reporter.lines[0].endswith("AssertionError: boom")


class TestTheAnnotationSurvivesTheRunnersParser:
    """The runner splits a workflow command on ``,`` and ``::``.

    Every pytest node id contains ``::``, so an unescaped one truncates the
    annotation at the first colon pair -- which would name the file and drop the
    test, most of the defect this exists to fix.
    """

    def test_the_node_id_colons_are_escaped_in_the_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reporter = _emit({"failed": [_report("test/test_x.py::TestC::test_y")]}, monkeypatch)
        line = reporter.lines[0]

        title = line.split("title=", 1)[1].split("::", 1)[0]
        assert title == "test/test_x.py%3A%3ATestC%3A%3Atest_y", title

    def test_a_parametrized_id_with_a_comma_is_escaped_in_the_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real id from the #7296 logs carried spaces, quotes and separators."""
        nodeid = 'test/test_data_home_not_relocatable.py::test_alias[cd ~; mv "a,b" /tmp/x]'
        reporter = _emit({"failed": [_report(nodeid)]}, monkeypatch)
        # The command's own leading "::" is not a separator, so drop the prefix
        # before splitting the property list off the message.
        properties = reporter.lines[0][len("::error ") :].split("::", 1)[0]

        assert "%2C" in properties, properties
        # The property list must not gain a bare comma, which would read as the
        # start of another property name.
        assert properties.count(",") == 2, properties

    def test_percent_signs_in_the_message_are_escaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``%`` is the escape introducer, so it has to go first or it eats the rest."""
        report = _report("test/test_x.py::test_y", message="")
        report.longrepr = None
        report.longreprtext = "AssertionError: 50% off"
        reporter = _emit({"failed": [report]}, monkeypatch)

        assert "%25" in reporter.lines[0]
        assert "50% off" not in reporter.lines[0]

    def test_line_breaks_are_escaped_rather_than_ending_the_command(self) -> None:
        """A raw newline terminates the workflow command and drops the rest.

        Exercised on the escaper directly: the emitter only ever passes it one
        line, so a regression that stopped escaping breaks would be invisible
        end-to-end until some report shape carried an embedded break.
        """
        escaped = _root._gha_escape("first\r\nsecond", is_property=False)

        assert escaped == "first%0D%0Asecond"

    def test_a_very_long_reason_is_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A full assertion diff does not fit the check-run UI and is in the log."""
        reporter = _emit(
            {"failed": [_report("test/test_x.py::test_y", message="E" * 5000)]},
            monkeypatch,
        )

        assert len(reporter.lines[0]) < 1000, len(reporter.lines[0])
        assert reporter.lines[0].endswith("...")


class TestTheAnnotationsStayBounded:
    """GitHub keeps a bounded number per check run and drops the excess silently."""

    def test_past_the_cap_the_rest_are_counted_not_annotated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = _root._MAX_ANNOTATED_FAILURES
        reports = [_report(f"test/test_x.py::test_{i}") for i in range(cap + 7)]
        reporter = _emit({"failed": reports}, monkeypatch)

        errors = [line for line in reporter.lines if line.startswith("::error ")]
        notices = [line for line in reporter.lines if line.startswith("::notice::")]
        assert len(errors) == cap, len(errors)
        assert len(notices) == 1, reporter.lines
        assert f"{cap + 7} tests failed" in notices[0]
        assert "remaining 7" in notices[0]

    def test_exactly_the_cap_gets_no_notice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An off-by-one here claims failures the job does not have."""
        cap = _root._MAX_ANNOTATED_FAILURES
        reports = [_report(f"test/test_x.py::test_{i}") for i in range(cap)]
        reporter = _emit({"failed": reports}, monkeypatch)

        assert all(line.startswith("::error ") for line in reporter.lines), reporter.lines


class TestNothingIsWrittenWhenThereIsNothingToSay:
    """These lines are noise anywhere they are not interpreted."""

    def test_a_green_run_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter = _emit({"passed": [_report("test/test_x.py::test_y")]}, monkeypatch)

        assert reporter.lines == []

    def test_outside_github_actions_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A developer's local red must not grow workflow-command lines."""
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        reporter = _Reporter({"failed": [_report("test/test_x.py::test_y")]})
        _root.pytest_terminal_summary(reporter, 1, SimpleNamespace())

        assert reporter.lines == []

    def test_an_xdist_worker_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The worker's reports are counted again on the controller.

        Annotating from both would double every line, once per worker that saw the
        test -- which is how a 2-failure shard reports 20 annotations.
        """
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        reporter = _Reporter({"failed": [_report("test/test_x.py::test_y")]})
        _root.pytest_terminal_summary(reporter, 1, SimpleNamespace(workerinput={}))

        assert reporter.lines == []


class TestTheMatcherThatLiedIsOffWhereverPytestRuns:
    """The other half: nothing else may write a pytest job's annotations.

    Asserted from the workflow SOURCE across EVERY workflow, keyed to whether the
    job runs pytest, so a new pytest job cannot inherit the default matcher
    unnoticed -- in ci.yml or anywhere else. Scoping this to ci.yml was the first
    version and it left `release.yml` and `test-durations.yml` matched, which is
    exactly the gap the guard exists to close (found in review).
    """

    _DIRECTIVE = "::remove-matcher owner=python::"

    @staticmethod
    def _runs_pytest(step: dict) -> bool:
        return re.search(r"(?m)^\s*pytest\s", str(step.get("run", ""))) is not None

    @classmethod
    def _pytest_jobs(cls) -> dict[str, list[dict]]:
        """``<workflow>:<job>`` -> its steps, for every job that runs pytest."""
        found: dict[str, list[dict]] = {}
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for name, job in (document.get("jobs") or {}).items():
                steps = job.get("steps") or []
                if any(cls._runs_pytest(step) for step in steps):
                    found[f"{path.name}:{name}"] = steps
        return found

    def test_the_pytest_jobs_are_the_ones_this_covers(self) -> None:
        """A guard whose subject list silently empties protects nothing."""
        assert set(self._pytest_jobs()) >= {
            "ci.yml:backend-test",
            "ci.yml:backend-test-windows",
            "ci.yml:backend-test-macos",
            "ci.yml:backend-test-sandbox",
            "release.yml:release-candidate-tests",
            "test-durations.yml:refresh",
        }, set(self._pytest_jobs())

    def test_every_pytest_job_removes_the_python_matcher_before_pytest_runs(self) -> None:
        """Its pattern matches a traceback, and a warning can print one (#7296).

        Ordering matters and is asserted, not assumed: ``::remove-matcher`` takes
        effect for the rest of the job from the point it is echoed, so a
        directive placed after the pytest step would leave the run it was meant
        to protect fully matched.
        """
        offenders = []
        for name, steps in self._pytest_jobs().items():
            first_pytest = next(i for i, step in enumerate(steps) if self._runs_pytest(step))
            removed_at = [
                i for i, step in enumerate(steps) if self._DIRECTIVE in str(step.get("run", ""))
            ]
            if not removed_at or min(removed_at) > first_pytest:
                offenders.append(name)

        assert not offenders, (
            f"{offenders} run pytest with setup-python's `python` problem matcher still in "
            "scope. It annotates any `File ...` + `raise X('msg')` pair in the log, "
            "including one printed inside pytest's warnings summary, and it never matches "
            f"a pytest failure -- so the check run names a warning and hides the red. Echo "
            f"`{self._DIRECTIVE}` at or before the first step that runs pytest."
        )

    def test_no_workflow_passes_the_input_that_does_not_exist(self) -> None:
        """``add-problem-matchers`` is not an input on this action pin.

        It was the obvious first attempt and it is a trap: the action ignores it
        and the runner answers with an ``Unexpected input(s)`` warning, which
        becomes one more annotation saying nothing about the tests. MEASURED on
        this PR's own first run, against ``actions/setup-python`` v7.0.0.

        Checked against the PARSED step inputs, not the file text, so prose that
        names the trap (this docstring's counterpart in ci.yml) does not trip it.
        """
        offenders = []
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for name, job in (document.get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    if "add-problem-matchers" in (step.get("with") or {}):
                        offenders.append(f"{path.name}:{name}")

        assert not offenders, (
            f"{offenders} pass `add-problem-matchers` to an action that has no such input; "
            "the runner warns and the matcher stays on. Echo "
            f"`{self._DIRECTIVE}` in the job instead."
        )

    def test_the_emitter_lives_in_the_rootdir_conftest(self) -> None:
        """``test/conftest.py`` is not loaded for the in-package app suites.

        Those testpaths red the same jobs, so an emitter registered there would
        leave exactly the runs that fail hardest with no annotation at all.
        """
        root = _ROOT_CONFTEST.read_text(encoding="utf-8")
        suite = (_REPO_ROOT / "test" / "conftest.py").read_text(encoding="utf-8")
        hook = "def pytest_terminal_summary"

        assert hook in root, "the annotation emitter must be registered from the rootdir"
        assert hook not in suite, (
            "a second definition in test/conftest.py would shadow nothing but would "
            "annotate twice for `test/` and not at all for the in-package suites"
        )
