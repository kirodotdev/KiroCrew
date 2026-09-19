"""PR Hygiene tells a contributor how to fix a stale branch, and its green log stays quiet.

Two properties of that job need pinning, and they are independent of each other.

The commit-count remedy has to name the trap, not just hand over a recipe.
GitHub's **Update branch** button is the only one-click stale-branch remedy the UI
offers, and it adds a merge commit, which counts toward the limit and re-trips the
check. A message that says "rebase" without saying that points the contributor at
the one control that breaks the thing being fixed.

Every step has to assemble its `::error::` prefix at runtime. Actions echoes each
`run:` block's source into the log whether that branch executes or not, so a
literal annotation string is printed by passing runs too, where it reads as a real
failure.

Three surfaces carry this guidance and none can see the others: the hygiene step,
the PR template, and CONTRIBUTING.md. This file pins the load-bearing content of
each. It is indifferent to how the prose is worded, except where the wording IS
the fix -- a remedy that omits the button is the defect.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"


def _can_run_posix_shell() -> bool:
    """PROBE, never a platform guess: does ``bash`` on PATH run a POSIX script?

    A Windows runner has a ``bash`` on PATH, but it is the WSL launcher stub. It
    prints a UTF-16 notice about installed distributions, exits non-zero, and
    never runs the script -- so a test that shells out reads that notice as the
    step's output. Probing keeps these assertions wherever a real shell exists,
    Git Bash or a provisioned distro included, instead of dropping them for a
    whole platform.

    Reserve this for tests that EXECUTE a workflow step's script. A test that
    only reads the step's text needs no shell at all.
    """
    try:
        probe = subprocess.run(
            ["bash", "-c", "printf ok"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "ok"


_HAS_POSIX_SHELL = _can_run_posix_shell()

requires_posix_shell = pytest.mark.skipif(
    not _HAS_POSIX_SHELL,
    reason="the step under test is a POSIX `run:` block and needs a real bash to execute",
)


def _hygiene_job() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document["jobs"]["pr-hygiene"]


def _step(name_fragment: str) -> dict[str, Any]:
    for step in _hygiene_job()["steps"]:
        if name_fragment.lower() in str(step.get("name", "")).lower():
            return step
    raise AssertionError(f"no pr-hygiene step whose name contains {name_fragment!r}")


class TestTheGreenLogDoesNotImpersonateAFailure:
    def test_no_step_writes_an_annotation_literally(self) -> None:
        # The whole defect: a literal prefix is printed by a passing run too,
        # because Actions renders the script source regardless of what executes.
        offenders = [
            step.get("name")
            for step in _hygiene_job()["steps"]
            if "::error::" in (step.get("run") or "")
        ]
        assert offenders == []

    def test_every_failing_step_still_assembles_a_real_annotation(self) -> None:
        # Removing the literal must not mute the annotation. A step that can fail
        # and has no way to say so is the opposite defect.
        failing = [step for step in _hygiene_job()["steps"] if "exit 1" in (step.get("run") or "")]
        assert failing, "expected pr-hygiene steps that can fail"
        for step in failing:
            assert "err()" in step["run"], f"{step.get('name')} cannot emit an annotation"


class TestTheCommitCountRemedy:
    """The message is the fix: it must name the trap, not just the recipe."""

    def test_it_names_the_update_branch_button_as_the_trap(self) -> None:
        run = _step("Enforce commit count")["run"]
        assert "Update branch" in run
        assert "merge commit" in run
        # A message that says "rebase" without naming the button is the shape a
        # contributor can follow all the way into the failure it warns about.
        assert "re-trip" in run

    def test_it_offers_rebase_rather_than_merge(self) -> None:
        run = _step("Enforce commit count")["run"]
        assert "git rebase" in run
        assert "git push --force-with-lease" in run

    def test_the_recipe_uses_the_prs_own_base_branch(self) -> None:
        # Spelling `origin/main` literally sends a contributor whose PR targets
        # another base to rebase onto a branch they are not merging into.
        step = _step("Enforce commit count")
        assert step["env"]["BASE_REF"] == "${{ github.event.pull_request.base.ref }}"
        assert "origin/$BASE_REF" in step["run"]
        assert "origin/main" not in step["run"]

    def test_the_stated_limit_comes_from_the_enforced_value(self) -> None:
        # A hardcoded number in the prose drifts from MAX_COMMITS silently, and the
        # contributor is then told a limit the gate does not apply.
        step = _step("Enforce commit count")
        assert "$MAX_COMMITS" in step["run"]
        assert int(step["env"]["MAX_COMMITS"]) >= 1


@requires_posix_shell
class TestTheStepActuallyEmitsWhatItClaims:
    """Executed, not read: the annotation has to survive the refactor."""

    def _run_commit_count_step(self) -> str:
        script = _step("Enforce commit count")["run"]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        env = {
            # An empty range trips the `-lt 1` arm, so the failure text is
            # reachable without fabricating commits.
            "BASE": head,
            "HEAD": head,
            "BASE_REF": "main",
            "MAX_COMMITS": "2",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        }
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        # Naming the expected text in the guard, not just the exit code: a shell
        # that never ran the script can exit 1 on its own, and a bare returncode
        # check lets that through to fail later on a confusing assertion.
        assert result.returncode == 1, (
            f"expected the failure arm, got {result.returncode}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert (
            "commits (has" in result.stdout
        ), f"the script did not run; stdout={result.stdout!r} stderr={result.stderr!r}"
        return result.stdout

    def test_the_emitted_line_is_a_byte_exact_annotation(self) -> None:
        out = self._run_commit_count_step()
        # Actions only honours a workflow command on a line that STARTS with `::`.
        annotations = [line for line in out.splitlines() if line.startswith("::error::")]
        assert len(annotations) == 1, f"expected one annotation, got {annotations}"
        assert "commits (has 0)" in annotations[0]

    def test_the_operator_sees_the_trap_and_the_remedy(self) -> None:
        out = self._run_commit_count_step()
        assert "Update branch" in out
        assert "git rebase origin/main" in out


class TestTheContributorFacingSurfaces:
    @pytest.mark.parametrize("path", [TEMPLATE, CONTRIBUTING], ids=["pr-template", "contributing"])
    def test_each_warns_against_merging_to_update_a_branch(self, path: Path) -> None:
        # A contributor who reads either one should not need the failure message.
        body = path.read_text(encoding="utf-8")
        assert "Update branch" in body, f"{path.name} does not name the button"
        assert "rebase" in body.lower(), f"{path.name} does not give the rebase route"
