"""Verdicts of the auto-improvement bug track's RED/GREEN gate (:mod:`spine.bug_gate`).

``BugGate.run`` decides whether a proposed bug fix is filed. A wrong ACCEPT files a fix
that does not reproduce, does not fix, or regresses the suite; a wrong REJECT throws a
correct fix away. Each test drives the gate through a scripted fake ``BugRunner`` and
pins one verdict, plus the cheapest-first order (a failed early check never pays for a
later one). No subprocess runs and nothing is written to disk.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine.bug_gate import BugGate
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    BUG_ERROR,
    BUG_FAILED_BUILD,
    BUG_FAILED_LINT,
    BUG_FILED,
    BUG_NOT_GREEN,
    BUG_NOT_RED,
    BUG_REGRESSED,
    BUG_TEST_FLAKY,
    BUG_TEST_INVALID,
    BugReproducingTest,
    Candidate,
)

BASE = Path("/base")
CAND = Path("/cand")
TEST_ID = "test/test_bug_x.py::test_reproduces"


class _Runner:
    """A scripted BugRunner that records every call it receives, in order."""

    def __init__(
        self,
        *,
        build: bool = True,
        lint: bool = True,
        collects: bool = True,
        base: Iterable[bool | None] = (False, False),
        fix: bool | None = True,
        suite: tuple[bool, list[str]] = (True, []),
        base_failures: list[str] | Exception | None = None,
    ) -> None:
        self.build, self.lint, self.collects = build, lint, collects
        self.base = list(base)
        self.fix, self.suite = fix, suite
        self.base_failures = base_failures
        self.calls: list[tuple] = []

    def build_imports_ok(self, *, src: Path) -> bool:
        self.calls.append(("build", src))
        return self.build

    def lint_clean(self, *, base_src: Path, cand_src: Path) -> bool:
        self.calls.append(("lint", base_src, cand_src))
        return self.lint

    def test_collects(self, *, src: Path, test_path: str) -> bool:
        self.calls.append(("collect", src, test_path))
        return self.collects

    def run_reproducing_test(self, *, src: Path, test_id: str, test_only: bool) -> bool | None:
        self.calls.append(("repro", src, test_id, test_only))
        if test_only:
            return self.base.pop(0)
        return self.fix

    def run_suite(self, *, src: Path) -> tuple[bool, list[str]]:
        self.calls.append(("suite", src))
        return self.suite

    def run_named_tests(self, *, src: Path, test_ids: list[str]) -> list[str]:
        self.calls.append(("named", src, list(test_ids)))
        if isinstance(self.base_failures, Exception):
            raise self.base_failures
        return list(self.base_failures or [])

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def _candidate(test_id: str = TEST_ID, test_path: str = "") -> Candidate:
    return Candidate(
        kind="bug",
        target="src/pkg/mod.py::fn",
        reproducing_test=BugReproducingTest(test_id=test_id, test_path=test_path),
    )


def _run(runner: _Runner, candidate: Candidate | None = None, **gate_kwargs):
    return BugGate(**gate_kwargs).run(
        runner=runner, candidate=candidate or _candidate(), base_src=BASE, cand_src=CAND
    )


class TestAccept:
    def test_red_twice_then_green_then_green_suite_files_the_bug(self) -> None:
        runner = _Runner()
        res = _run(runner)
        assert res.passed is True
        assert res.reason == BUG_FILED
        assert (res.red, res.green, res.staygreen) == (True, True, True)
        assert (res.build_ok, res.lint_ok, res.collected) == (True, True, True)
        # RED runs the test-only diff on BASE twice; GREEN runs the full fix on CAND.
        assert runner.calls == [
            ("build", CAND),
            ("lint", BASE, CAND),
            ("collect", CAND, TEST_ID),
            ("repro", BASE, TEST_ID, True),
            ("repro", BASE, TEST_ID, True),
            ("repro", CAND, TEST_ID, False),
            ("suite", CAND),
        ]

    def test_suite_failures_that_also_fail_on_base_are_not_regressions(self) -> None:
        runner = _Runner(suite=(False, ["t::a", "t::b"]), base_failures=["t::a", "t::b"])
        res = _run(runner)
        assert res.passed is True
        assert res.reason == BUG_FILED
        assert runner.calls[-1] == ("named", BASE, ["t::a", "t::b"])


class TestMalformedCandidate:
    @pytest.mark.parametrize(
        "reproducing_test", [None, BugReproducingTest(test_id="")], ids=["none", "blank-id"]
    )
    def test_no_reproducing_test_is_invalid_and_runs_nothing(self, reproducing_test) -> None:
        runner = _Runner()
        candidate = Candidate(
            kind="bug", target="src/pkg/mod.py::fn", reproducing_test=reproducing_test
        )
        res = _run(runner, candidate)
        assert (res.passed, res.reason) == (False, BUG_TEST_INVALID)
        assert runner.calls == []

    def test_collection_checks_the_test_path_when_one_is_given(self) -> None:
        runner = _Runner()
        _run(runner, _candidate(test_path="test/test_bug_x.py"))
        assert ("collect", CAND, "test/test_bug_x.py") in runner.calls


class TestTriageLadder:
    def test_a_build_failure_stops_before_lint(self) -> None:
        runner = _Runner(build=False)
        res = _run(runner)
        assert (res.passed, res.reason, res.build_ok) == (False, BUG_FAILED_BUILD, False)
        assert runner.kinds() == ["build"]

    def test_a_lint_failure_stops_before_collection(self) -> None:
        runner = _Runner(lint=False)
        res = _run(runner)
        assert (res.reason, res.build_ok, res.lint_ok) == (BUG_FAILED_LINT, True, False)
        assert runner.kinds() == ["build", "lint"]

    def test_a_test_that_does_not_collect_never_runs(self) -> None:
        runner = _Runner(collects=False)
        res = _run(runner)
        assert (res.reason, res.lint_ok, res.collected) == (BUG_TEST_INVALID, True, False)
        assert runner.kinds() == ["build", "lint", "collect"]


class TestRed:
    def test_a_test_that_passes_on_base_is_vacuous(self) -> None:
        runner = _Runner(base=(True, True))
        res = _run(runner)
        assert (res.passed, res.reason, res.red) == (False, BUG_NOT_RED, False)
        assert "suite" not in runner.kinds()

    @pytest.mark.parametrize("base", [(False, True), (True, False)], ids=["fail-pass", "pass-fail"])
    def test_mixed_base_results_are_flaky(self, base) -> None:
        runner = _Runner(base=base)
        res = _run(runner)
        assert res.reason == BUG_TEST_FLAKY
        assert runner.kinds().count("repro") == 2  # GREEN never runs

    @pytest.mark.parametrize("base", [(False, None), (None, None)], ids=["one", "both"])
    def test_an_errored_base_run_is_not_a_clean_red(self, base) -> None:
        runner = _Runner(base=base)
        res = _run(runner)
        assert (res.passed, res.reason, res.red) == (False, BUG_TEST_INVALID, False)
        assert runner.kinds().count("repro") == 2

    @pytest.mark.parametrize(("requested", "expected"), [(0, 2), (1, 2), (3, 3)])
    def test_red_runs_at_least_twice(self, requested: int, expected: int) -> None:
        runner = _Runner(base=[False] * expected)
        res = _run(runner, red_reps=requested)
        assert res.passed is True
        assert runner.calls.count(("repro", BASE, TEST_ID, True)) == expected


class TestGreen:
    @pytest.mark.parametrize("fix", [False, None], ids=["fails", "errors"])
    def test_a_fix_that_does_not_turn_the_test_green_is_rejected(self, fix) -> None:
        runner = _Runner(fix=fix)
        res = _run(runner)
        assert (res.passed, res.reason) == (False, BUG_NOT_GREEN)
        assert (res.red, res.green) == (True, False)
        assert "suite" not in runner.kinds()


class TestStayGreen:
    def test_a_suite_failure_new_under_the_fix_is_a_regression(self) -> None:
        runner = _Runner(suite=(False, ["t::old", "t::new"]), base_failures=["t::old"])
        res = _run(runner)
        assert (res.passed, res.reason) == (False, BUG_REGRESSED)
        assert (res.red, res.green, res.staygreen) == (True, True, False)
        assert res.failing_tests == ["t::new"]

    @pytest.mark.parametrize(
        "failing", [[], ["<unparsed-suite-failure>"]], ids=["empty", "unparsed-sentinel"]
    )
    def test_a_red_suite_with_no_named_failure_is_a_regression(self, failing) -> None:
        # Nothing to re-run on base, so the failure can never be shown pre-existing.
        runner = _Runner(suite=(False, failing))
        res = _run(runner)
        assert (res.passed, res.reason, res.staygreen) == (False, BUG_REGRESSED, False)
        assert res.failing_tests == failing
        assert "named" not in runner.kinds()

    def test_an_erroring_base_recheck_counts_every_failure(self) -> None:
        runner = _Runner(suite=(False, ["t::a", "t::b"]), base_failures=OSError("boom"))
        res = _run(runner)
        assert res.reason == BUG_REGRESSED
        assert res.failing_tests == ["t::a", "t::b"]

    def test_without_a_base_recheck_every_failure_counts(self) -> None:
        runner = _Runner(suite=(False, ["t::a"]))
        runner.run_named_tests = None  # type: ignore[assignment,method-assign]
        res = _run(runner)
        assert (res.reason, res.failing_tests) == (BUG_REGRESSED, ["t::a"])


class TestRunnerErrors:
    @pytest.mark.parametrize("stage", ["build_imports_ok", "run_reproducing_test", "run_suite"])
    def test_a_runner_exception_is_a_gate_error_not_a_verdict(
        self, stage: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _Runner()

        def _raise(**_kwargs):
            raise RuntimeError("toolchain crashed")

        monkeypatch.setattr(runner, stage, _raise)
        res = _run(runner)
        assert (res.passed, res.reason) == (False, BUG_ERROR)
        assert res.detail == "bug gate error: RuntimeError: toolchain crashed"


def test_every_verdict_is_logged_with_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="auto_improvement.spine.bug_gate")
    _run(_Runner(build=False))
    _run(_Runner())
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"bug_gate: FAIL reason={BUG_FAILED_BUILD} ") for m in messages)
    assert any(m.startswith(f"bug_gate: PASS reason={BUG_FILED} ") for m in messages)
    assert all("target=src/pkg/mod.py::fn" in m for m in messages)
