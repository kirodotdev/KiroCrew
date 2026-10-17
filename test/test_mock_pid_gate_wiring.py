"""Pins the wiring the mock-pid rule rests on but cannot itself observe.

``semgrep --test`` already proves the rule's own behaviour: the fixture file
asserts both directions, and removing any pattern alternative reddens it. What
that harness cannot see is the machinery around the rule, and each piece of it
fails silently:

* The scan is diff-scoped by ``--baseline-commit``. The rule is deliberately
  unconditional, so the tree it runs against already carries findings on lines
  nobody is touching. Drop that flag and the rule stops being a gate on new code
  and becomes a red on every open pull request at once.
* ``.semgrepignore`` keeps the normal scan out of ``semgrep-tests/``. The
  fixtures are deliberate violations, so without that entry the rule fails the
  job on its own test data.
* The rule's ceiling has to agree with the one the update-provider suite already
  asserts. Two numbers for one convention is the drift the convention exists to
  stop.

None of the above raises when it breaks, which is why they are asserted here
rather than trusted.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULE = ROOT / "semgrep" / "mock-pid-allocatable.yaml"
FIXTURE = ROOT / "semgrep-tests" / "mock-pid-allocatable.py"
SEMGREPIGNORE = ROOT / ".semgrepignore"
CODE_REVIEW = ROOT / ".github" / "workflows" / "code-review.yml"
UNALLOCATABLE_PID_SUITE = ROOT / "test" / "test_update_provider.py"

RULE_ID = "kirocrew.mock-pid-allocatable"
# Every supported platform allocates pids below this, so a literal at or above
# it cannot name a live process.
PID_FLOOR = 2**32


def _rule_body() -> dict:
    rules = yaml.safe_load(RULE.read_text(encoding="utf-8"))["rules"]
    matching = [r for r in rules if r.get("id") == RULE_ID]
    assert len(matching) == 1, f"expected exactly one {RULE_ID}, found {len(matching)}"
    return matching[0]


class TestTheRuleAndItsFixturesStayPaired:
    def test_the_rule_declares_the_id_the_fixture_annotates(self) -> None:
        """A rule renamed without its fixture leaves the fixture asserting nothing."""
        body = _rule_body()
        assert body["severity"] == "ERROR"
        assert body["languages"] == ["python"]

    def test_the_fixture_asserts_both_directions(self) -> None:
        """A fixture carrying only positives passes an over-matching rule."""
        text = FIXTURE.read_text(encoding="utf-8")
        assert f"# ruleid: {RULE_ID}" in text, "the fixture lost its must-match cases"
        assert f"# ok: {RULE_ID}" in text, "the fixture lost its must-not-match cases"

    def test_the_fixture_pins_both_sides_of_the_ceiling(self) -> None:
        """The ceiling is the whole claim, so a pair either side of it must stay.

        Without the pair, a rule that flagged every pid, or none, still passes.
        """
        text = FIXTURE.read_text(encoding="utf-8")
        assert str(PID_FLOOR - 1) in text, "the last allocatable value is no longer covered"
        assert str(PID_FLOOR) in text, "the first unallocatable value is no longer covered"

    def test_the_rule_is_scoped_to_the_test_trees(self) -> None:
        """In production a pid is one the operating system issued, not a fabrication.

        An unscoped rule would flag those too, and would be removed within a
        week, taking the gate with it.

        The built-in include is recursive because those suites are not all one
        level down: aws_control/crew/packaging/tests is collected like any other
        and a single-star include reads past it, which is the same
        partial-coverage gap this rule exists to close.
        """
        include = _rule_body()["paths"]["include"]
        assert "test/**" in include
        assert "src/kiro_crew/apps/builtins/**/tests/**" in include, include


class TestTheCeilingHasOneValueRepoWide:
    def test_the_rule_compares_against_the_documented_floor(self) -> None:
        comparisons = [
            clause["metavariable-comparison"]["comparison"]
            for clause in _rule_body()["patterns"]
            if "metavariable-comparison" in clause
        ]
        assert len(comparisons) == 1, comparisons
        numbers = [int(n) for n in re.findall(r"\d+", comparisons[0])]
        assert numbers == [PID_FLOOR], comparisons[0]

    def test_the_update_provider_suite_asserts_the_same_floor(self) -> None:
        """Two numbers for one convention is the drift this gate exists to stop."""
        text = UNALLOCATABLE_PID_SUITE.read_text(encoding="utf-8")
        assert (
            "_UNALLOCATABLE_PID > 2**32" in text
        ), "the shared spelling no longer pins the ceiling this rule compares against"


def _sast_run_steps() -> list[str]:
    """The shell of every step in the SAST job, and nothing from its siblings.

    A substring search over the whole workflow cannot tell the scan step from
    the rule-test step, nor from the two other jobs that resolve a diff base of
    their own, so every assertion below is scoped to this job's own steps.
    """
    jobs = yaml.safe_load(CODE_REVIEW.read_text(encoding="utf-8"))["jobs"]
    steps = jobs["sast"]["steps"]
    return [step["run"] for step in steps if "run" in step]


class TestTheScanStaysDiffScoped:
    def test_the_sast_job_loads_the_custom_rules(self) -> None:
        scan = [run for run in _sast_run_steps() if "semgrep scan" in run]
        assert len(scan) == 1, f"expected one scan step, found {len(scan)}"
        assert "--config semgrep/" in scan[0], "the custom rule directory left the scan"

    def test_the_sast_job_scans_only_what_the_diff_adds(self) -> None:
        """The flag that makes an unconditional rule a gate on new code only.

        Without it the rule reports every pre-existing binding in the tree, on
        every open pull request, none of which the author touched.
        """
        scan = [run for run in _sast_run_steps() if "semgrep scan" in run]
        assert len(scan) == 1, f"expected one scan step, found {len(scan)}"
        assert "--baseline-commit" in scan[0], "the scan is no longer diff-scoped"
        assert "SEMGREP_BASELINE_REF" in scan[0]

    def test_the_diff_base_is_the_merge_base(self) -> None:
        """A base-branch tip would read sibling commits as this branch's own work."""
        resolvers = [run for run in _sast_run_steps() if "MERGE_BASE" in run]
        assert len(resolvers) == 1, f"expected one diff-base step, found {len(resolvers)}"
        assert "git merge-base" in resolvers[0]

    def test_the_rule_tests_run_on_the_fixtures(self) -> None:
        harness = [run for run in _sast_run_steps() if "--test" in run]
        assert len(harness) == 1, f"expected one rule-test step, found {len(harness)}"
        assert "--config semgrep/" in harness[0]
        assert "semgrep-tests/" in harness[0]

    def test_the_normal_scan_cannot_read_the_fixtures(self) -> None:
        """The fixtures are deliberate violations; the scan must not see them."""
        entries = [
            line.strip()
            for line in SEMGREPIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert "semgrep-tests/" in entries, entries
