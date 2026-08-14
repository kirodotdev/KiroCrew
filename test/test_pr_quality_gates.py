"""Regression tests for the PR quality gates.

These pin the behaviours that are easy to break silently by editing YAML:
the triggers a gate needs to be fixable without a code push, the
added-lines-only scoping that keeps a gate from blaming a PR for
pre-existing code, the advisory-vs-blocking contract of each lane, and --
most importantly -- that `pr-readiness.yml` no longer force-passes a failing
Design Review.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class TestScreenshotEvidence:
    """The gate must be satisfiable by editing the PR body alone."""

    def test_reruns_on_body_edit_and_label_change(self):
        # Without `edited`, a contributor who adds the screenshots to the
        # description cannot turn the check green without a no-op push.
        # Without `labeled`, the escape hatch has the same problem.
        wf = _read("screenshot-evidence.yml")
        types_line = next(ln for ln in wf.splitlines() if "types:" in ln)
        for needed in ("edited", "labeled", "unlabeled"):
            assert needed in types_line, f"missing '{needed}' trigger"

    def test_has_escape_hatch_label(self):
        # A gate with no exemption path forces contributors to paste a
        # meaningless screenshot to get green, which defeats the purpose.
        assert "no-screenshots" in _read("screenshot-evidence.yml")

    def test_excludes_non_visual_frontend_paths(self):
        # Tests, type declarations and locale catalogues change constantly
        # with no visual delta; gating on them trains bad habits.
        wf = _read("screenshot-evidence.yml")
        for excluded in (
            ":(exclude)website/src/**/*.test.tsx",
            ":(exclude)website/src/test/**",
            ":(exclude)website/src/**/*.d.ts",
        ):
            assert excluded in wf, f"should exclude {excluded}"

    def test_body_is_only_pattern_matched(self):
        # The PR body is untrusted author input. It must never be eval'd or
        # interpolated into a shell command.
        wf = _read("screenshot-evidence.yml")
        assert 'body="$(gh api' in wf
        assert "eval" not in wf

    def test_has_fork_friendly_body_marker(self):
        # Fork contributors cannot add labels, so the body marker must exist
        # as a self-service waiver alongside the label.
        wf = _read("screenshot-evidence.yml")
        assert "<!-- no-visual-delta -->" in wf
        # The marker is attacker-controlled text: fixed-string match only.
        assert "grep -qF -- '<!-- no-visual-delta -->'" in wf

    def test_marker_requires_justification(self):
        # A bare marker is a silent bypass; the waiver must carry a reviewable
        # claim and fail loudly without one.
        wf = _read("screenshot-evidence.yml")
        assert "why no screenshots?" in wf.lower()
        assert "marker without a justification" in wf

    def test_marker_waiver_warns_instead_of_passing_silently(self):
        # A reviewer scanning the run log must see that evidence was waived.
        wf = _read("screenshot-evidence.yml")
        assert (
            "::warning::'<!-- no-visual-delta -->' marker present" in wf
        ), "waiver must emit a warning annotation naming the marker"


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the evidence step runs under bash on ubuntu-latest",
)
class TestScreenshotEvidenceBodyLogic:
    """Execute the real evidence step against fixture PR bodies.

    Textual pins cannot prove the branch logic; this extracts the actual
    ``run:`` script from the YAML and runs it with ``gh`` stubbed to return a
    fixture body, so the waiver semantics are locked by behavior.
    """

    def _run_step(self, tmp_path: Path, body: str, exempt: str = "false"):
        wf = yaml.safe_load(_read("screenshot-evidence.yml"))
        steps = wf["jobs"]["screenshot-evidence"]["steps"]
        step = next(
            (s for s in steps if s.get("name") == "Require visual evidence in the PR body"),
            None,
        )
        assert step is not None, "step 'Require visual evidence in the PR body' not found"
        body_file = tmp_path / "body.txt"
        body_file.write_text(body, encoding="utf-8")
        # `gh api ... --jq '.body // ""'` prints the raw body: stub it with cat.
        # The sentinel proves the stub (not a real gh on PATH) served the call.
        sentinel = tmp_path / "gh-stub-invoked"
        gh = tmp_path / "gh"
        gh.write_text(
            f'#!/bin/sh\ntouch "{sentinel}"\ncat "{body_file}"\n',
            encoding="utf-8",
            newline="\n",
        )
        gh.chmod(0o755)
        summary = tmp_path / "summary.md"
        summary.touch()
        env = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
            # Starve any real gh of credentials so a stub-resolution failure
            # can never turn into a live API call.
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
            "EXEMPT": exempt,
            "REPO": "example/repo",
            "PR": "1",
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        result = subprocess.run(
            ["bash", "-c", step["run"]],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if exempt != "true":
            # The label path exits before reading the body; every other path
            # must have gone through the stub.
            assert sentinel.exists(), "gh stub was never invoked"
        return result

    def test_marker_with_justification_passes_with_warning(self, tmp_path):
        body = (
            "<!-- no-visual-delta -->\n"
            "**Why no screenshot:** internal string builder change, rendered\n"
            "output is byte-identical.\n"
        )
        result = self._run_step(tmp_path, body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning::" in result.stdout
        assert "<!-- no-visual-delta -->" in result.stdout

    def test_marker_alone_fails_with_explanation(self, tmp_path):
        result = self._run_step(tmp_path, "<!-- no-visual-delta -->\njust trust me\n")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "marker without a justification" in result.stdout

    def test_empty_justification_does_not_waive(self, tmp_path):
        # A justification label with nothing after the colon is still a bare
        # marker: the claim must carry content.
        body = "<!-- no-visual-delta -->\n**Why no screenshot:**\n"
        result = self._run_step(tmp_path, body)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_emphasis_opening_justification_waives(self, tmp_path):
        # A reason that opens with markdown emphasis is still a reason.
        body = "<!-- no-visual-delta -->\n**Why no screenshot:** *pure rename*, no delta.\n"
        result = self._run_step(tmp_path, body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning::" in result.stdout

    def test_image_beats_marker(self, tmp_path):
        # Real evidence satisfies the gate outright: a body carrying both a
        # screenshot and an unjustified marker passes on the screenshot.
        body = "<!-- no-visual-delta -->\n![shot](https://example.test/x.png)\n"
        result = self._run_step(tmp_path, body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Visual evidence found" in result.stdout

    def test_no_marker_no_image_still_fails(self, tmp_path):
        result = self._run_step(tmp_path, "A visual change with no evidence.\n")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "::error::" in result.stdout

    def test_image_in_body_still_passes(self, tmp_path):
        result = self._run_step(tmp_path, "![shot](https://example.test/x.png)\n")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_label_waiver_unchanged(self, tmp_path):
        # The marker is an additional path; the label path must keep working.
        result = self._run_step(tmp_path, "no evidence at all", exempt="true")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "'no-screenshots' label present" in result.stdout


class TestCrossPlatform:
    """Findings must be confined to lines the PR actually adds."""

    def test_scans_added_lines_only(self):
        wf = _read("cross-platform.yml")
        assert "grep -E '^\\+'" in wf
        assert "grep -vE '^\\+\\+\\+'" in wf

    def test_filters_prose_before_matching(self):
        # Verified against commit 1d78b24e3: a docstring quoting ``shell=True``
        # to explain why it is avoided must not fail the gate.
        wf = _read("cross-platform.yml")
        assert "grep -vE '^\\+[[:space:]]*#'" in wf
        assert "grep -vF '``'" in wf

    def test_no_encoding_rule(self):
        # A line regex cannot decide this: nested calls truncate the lookahead
        # and multi-line calls split `encoding=` onto another line. Both give
        # FALSE failures on correct code (verified against commit 1d78b24e3),
        # so the rule is deliberately absent and its absence is documented.
        wf = _read("cross-platform.yml")
        assert "deliberately NO" in wf, "the absence must stay documented"
        # No rule may actually grep for the encoding kwarg.
        rule_lines = [
            ln for ln in wf.splitlines()
            if ln.lstrip().startswith("hits=")
        ]
        assert rule_lines, "expected at least one scan rule"
        for ln in rule_lines:
            assert "encoding" not in ln, f"encoding rule reintroduced: {ln.strip()[:80]}"

    def test_excludes_vendor_and_compat_module(self):
        wf = _read("cross-platform.yml")
        assert ":(exclude)src/kiro_crew/_vendor/**" in wf
        assert ":(exclude)src/kiro_crew/platform_compat.py" in wf

    def test_has_escape_hatch_label(self):
        assert "posix-only-approved" in _read("cross-platform.yml")


class TestPrScope:
    """Scope breadth is advisory: it must never fail the build."""

    def test_never_exits_nonzero(self):
        wf = _read("pr-scope.yml")
        assert "exit 1" not in wf, "PR Scope must stay advisory"

    def test_requires_both_thresholds(self):
        # Breadth alone or size alone is legitimately self-contained; only the
        # combination reviews badly.
        wf = _read("pr-scope.yml")
        assert '-gt "$MAX_AREAS" ] && [' in wf
        assert 'MAX_LINES' in wf

    def test_excludes_vendor_and_screenshots(self):
        wf = _read("pr-scope.yml")
        assert ":(exclude)src/kiro_crew/_vendor/**" in wf
        assert ":(exclude)temp-screenshots/**" in wf


class TestDesignReviewBlocks:
    """A BLOCK verdict must reach the required `PR Readiness` status."""

    def test_readiness_no_longer_force_passes_design_review(self):
        # This is the whole point of the change: previously BOTH lanes did
        # `passed+=("$label (advisory)")` for Design Review, so a red Design
        # Review check still produced a green PR Readiness.
        wf = _read("pr-readiness.yml")
        assert '"$label" = "Design Review" ] || [ "$label" = "UX Review"' not in wf, (
            "Design Review must no longer share the UX advisory branch"
        )
        assert 'failed+=("$label (BLOCK)")' in wf

    def test_ux_review_stays_advisory(self):
        # Only Design Review was promoted; UX keeps its advisory contract.
        wf = _read("pr-readiness.yml")
        assert 'passed+=("$label (advisory)")' in wf
        assert '[ "$label" = "UX Review" ]' in wf

    @pytest.mark.parametrize("name", ["design-review.yml", "fork-design-review.yml"])
    def test_prompt_no_longer_claims_block_is_advisory(self, name):
        # The prompt used to tell the model "BLOCK does NOT block the merge",
        # which taught it to under-use the verdict that now actually gates.
        wf = _read(name)
        assert "does NOT block the merge" not in wf
        assert "BLOCK (advisory)" not in wf
        assert "blocks PR readiness" in wf

    @pytest.mark.parametrize("name", ["design-review.yml", "fork-design-review.yml"])
    def test_falsification_step_and_block_budget(self, name):
        # Raising the stakes of BLOCK requires a matching precision bar.
        wf = _read(name)
        assert "FALSIFY BEFORE YOU BLOCK" in wf
        assert "at most 1 BLOCK per" in wf

    def test_same_repo_gate_fails_only_on_block(self):
        # The readiness wiring is only safe because every non-BLOCK outcome --
        # including an errored or throttled run -- exits 0.
        wf = _read("design-review.yml")
        gate = wf.split("Design review status (gates on BLOCK)")[1]
        assert "PASS|CONCERNS)" in gate
        assert "exit 1 ;;" in gate
        # The wildcard (errored / no verdict) branch must not fail.
        tail = gate.split("BLOCK)")[1]
        assert "exit 0" in tail, "an incomplete review must never block"
