"""The fix-loop discipline stays wired: pattern harvest in, untracked deferrals out.

Two mechanisms exist because this repository measured its own fix loop: 75% of
escaped defects were preventable, and the largest traceable class was findings a
reviewer raised and a disposition deferred without follow-through. The pattern-
harvest half turns each fix into a rule candidate; the deferral half makes every
deferred finding a tracked promise (label + owner + due date).

Each half is spread across surfaces that cannot see each other — a PR template,
a hygiene step, a semgrep rule, a review-prompt rule, two workflows, and a
skill. Losing any one piece silently reopens the gap the others assume closed,
and nothing else fails when that happens. So this file pins the wiring: it does
not care how the guidance is worded, only that every load-bearing piece is
still there and still pointed at the same names.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
CODE_REVIEW = ROOT / ".github" / "workflows" / "code-review.yml"
CONVERGENCE = ROOT / ".github" / "review-prompts" / "gpt-round-convergence.md"
DEFERRAL_CHECK = ROOT / ".github" / "workflows" / "disposition-deferral-check.yml"
AUDIT = ROOT / ".github" / "workflows" / "deferred-findings-audit.yml"
SEMGREP_RULE = ROOT / "semgrep" / "find-sentinel-truthiness.yaml"
SEMGREP_FIXTURE = ROOT / "semgrep-tests" / "find-sentinel-truthiness.py"
AUTOSDE = ROOT / "AUTOSDE.yaml"
PREPARE_PR = (
    ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "SKILL.md"
)

LABEL = "deferred-finding"

ANALYSIS = ROOT / ".github" / "workflows" / "fix-loop-analysis.yml"
METRICS_SCRIPT = ROOT / ".github" / "scripts" / "fix_loop_metrics.py"

# Names the runner defines for every step, plus shell specials. A reference to
# one of these is defined even though nothing in the workflow assigns it.
_RUNNER_PROVIDED = frozenset(
    {
        "CI",
        "HOME",
        "PATH",
        "PWD",
        "SHELL",
        "TMPDIR",
        "IFS",
        "RANDOM",
        "LINENO",
        "BASH_ENV",
        "BASH_SOURCE",
        "FUNCNAME",
        "OSTYPE",
        "HOSTNAME",
        "USER",
        "LANG",
    }
)


def _shell_steps(workflow: Path) -> Iterator[tuple[str, str, set[str], str]]:
    """Yield (job, step name, names defined for it, the shell body) per run step."""
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    workflow_env = set((doc.get("env") or {}).keys())
    for job_id, job in (doc.get("jobs") or {}).items():
        job_env = workflow_env | set((job.get("env") or {}).keys())
        for step in job.get("steps") or []:
            body = step.get("run")
            if not body:
                continue
            defined = job_env | set((step.get("env") or {}).keys())
            yield job_id, str(step.get("name") or "<unnamed>"), defined, str(body)


def _assigned_in_block(body: str) -> set[str]:
    """Names the shell body itself creates, so a local variable is not a miss."""
    names: set[str] = set()
    names |= set(re.findall(r"^\s*(?:local\s+|export\s+)?([A-Za-z_][A-Za-z0-9_]*)\+?=", body, re.M))
    names |= set(re.findall(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", body))
    names |= set(re.findall(r"\bread\s+(?:-r\s+)?([A-Za-z_][A-Za-z0-9_]*)", body))
    return names


def undefined_expansions(body: str, defined: set[str]) -> set[str]:
    """Variable names the block expands but nothing defines.

    GitHub `${{ ... }}` expressions are removed first: they are substituted
    before the shell ever sees them, so their contents are not shell names.
    Whole-line comments are removed too -- a line whose first non-space
    character is `#` is unconditionally a comment in bash, so prose describing
    an expansion must not read as one. A TRAILING `#` is deliberately left
    alone: it is ambiguous inside a string (`grep -oE '#[0-9]+'`), and cutting
    at it would truncate real code.
    """
    shell = re.sub(r"\$\{\{.*?\}\}", "", body, flags=re.S)
    shell = "\n".join(line for line in shell.splitlines() if not line.lstrip().startswith("#"))
    known = defined | _assigned_in_block(shell) | _RUNNER_PROVIDED
    referenced = {
        m.group(1)
        for m in re.finditer(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", shell)
        if not m.group(1).startswith(("GITHUB_", "RUNNER_", "INPUT_", "ACTIONS_"))
    }
    return referenced - known


class TestNoSilentlyBrokenShell:
    """A reference to an undefined name is invisible until the unhappy path runs.

    `set -euo pipefail` makes an unset expansion fatal, and these two lanes are
    non-blocking by design: the deferral checker replies instead of failing a
    required check, and the analysis step is `continue-on-error`. So a typo in a
    branch that only executes when something is WRONG takes the whole reply or
    the whole narrative down while the run still reports success.

    That is not hypothetical. `Checked disposition: $COMMENT_URL_` -- a markdown
    italic terminator absorbed into the variable name -- killed the refusal reply
    on every one of the first 18 incomplete deferrals, and nothing went red.
    """

    WORKFLOWS = (DEFERRAL_CHECK, AUDIT, ANALYSIS)

    def test_extractor_catches_the_defect_it_was_written_for(self) -> None:
        """Pin the ratchet itself against the exact shape that escaped.

        A linter for a class nobody can reproduce is a linter nobody can trust,
        so the historical form is asserted directly rather than assumed covered.
        """
        assert undefined_expansions('echo "at: $COMMENT_URL_"', {"COMMENT_URL"}) == {"COMMENT_URL_"}
        assert undefined_expansions('echo "at: ${COMMENT_URL}_"', {"COMMENT_URL"}) == set()

    def test_every_expansion_resolves(self) -> None:
        misses: list[str] = []
        for workflow in self.WORKFLOWS:
            for job, step, defined, body in _shell_steps(workflow):
                unknown = undefined_expansions(body, defined)
                if unknown:
                    misses.append(f"{workflow.name} :: {job} :: {step} -> {sorted(unknown)}")
        assert not misses, "shell steps expand names nothing defines:\n" + "\n".join(misses)


class TestFailedHalvesAreVisible:
    """Fail-soft must not mean fail-silent.

    Letting the model step fail is the right product call -- a broken narrative
    must never cost the deterministic numbers. But `continue-on-error` also made
    the run report SUCCESS, so a weekly job nobody opens became the perfect place
    for a permanently broken half to hide: the first scheduled run published a
    metrics-only issue and the Actions list stayed green.
    """

    def test_the_analysis_failure_is_reraised_after_the_issue_is_filed(self) -> None:
        doc = yaml.safe_load(ANALYSIS.read_text(encoding="utf-8"))
        steps = doc["jobs"]["analyze"]["steps"]
        filed = next(i for i, s in enumerate(steps) if str(s.get("id") or "") == "file_issue")
        reraise = [i for i, s in enumerate(steps) if "analysis_ok" in str(s.get("if") or "")]
        assert reraise, "a missing analysis must re-raise so the run goes red"
        assert reraise[-1] > filed, (
            "the re-raise must come AFTER the issue is filed, or the deterministic "
            "numbers are lost with the narrative"
        )
        assert "exit 1" in str(steps[reraise[-1]].get("run") or "")

    def test_an_empty_report_counts_as_no_analysis(self) -> None:
        """The action can exit 0 having written nothing.

        Keying the title, the body and the re-raise off the model step's outcome
        alone would then publish a normal-looking issue over metrics-only output
        and leave the run green -- the failure mode this workflow exists to end.
        One computed flag drives all three.
        """
        doc = yaml.safe_load(ANALYSIS.read_text(encoding="utf-8"))
        filing = next(
            s for s in doc["jobs"]["analyze"]["steps"] if str(s.get("id") or "") == "file_issue"
        )
        body = str(filing.get("run") or "")
        assert "-s fix-loop-report.md" in body, "emptiness must be part of the verdict"
        assert 'echo "analysis_ok=' in body, "the verdict must be published as a step output"

    def test_the_issue_title_says_when_only_numbers_landed(self) -> None:
        """The issue list is the surface a reader scans without opening anything."""
        text = ANALYSIS.read_text(encoding="utf-8")
        assert "metrics only" in text, "a degraded report must be labelled in its title"


class TestHarvestRecognitionMatchesTheGate:
    """What the metric counts as a harvest must be what the gate accepts.

    The gate is case-insensitive and space-tolerant (`grep -qiE`), so a body
    reading `### pattern harvest` passes it. A stricter reading in the metric
    publishes that PR as an escape -- corrupting the single number the metric
    exists to make trustworthy, in the direction that manufactures alarm.

    Two halves are pinned: the patterns are literally the gate's own, and the
    behaviour they produce is asserted on the shapes that differ between a
    substring test and the gate.
    """

    def _module(self):
        spec = importlib.util.spec_from_file_location("fix_loop_metrics", METRICS_SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_patterns_are_transcribed_from_the_gate(self) -> None:
        """Drift is silent: both sides keep working, and only the count is wrong."""
        gate_patterns = set(re.findall(r"grep -qiE '([^']+)'", CODE_REVIEW.read_text("utf-8")))
        mod = self._module()
        for attr in ("HARVEST_HEADING_RE", "HARVEST_ANSWER_RE"):
            pattern = getattr(mod, attr).pattern
            assert pattern in gate_patterns, (
                f"{attr} is {pattern!r}, which the hygiene gate does not use; "
                f"the gate's patterns are {sorted(gate_patterns)}"
            )

    def test_the_shapes_the_gate_accepts_are_counted(self) -> None:
        """Each body here passes the gate but fails a case-sensitive substring test."""
        mod = self._module()
        accepted = (
            "## Pattern harvest\nRule candidate: semgrep",
            "### pattern harvest\nRule candidate: semgrep",
            "##   Pattern harvest\nRule candidate: semgrep",
            "## PATTERN HARVEST\n  not generalizable : one-off",
        )
        for body in accepted:
            assert mod.has_harvest_section(body), f"the gate accepts this body: {body!r}"

    def test_a_heading_with_no_answer_is_not_a_harvest(self) -> None:
        """The gate demands both, so a PR that merged with only a heading escaped."""
        mod = self._module()
        assert not mod.has_harvest_section("## Pattern harvest\n<!-- comment only -->")
        assert not mod.has_harvest_section("Rule candidate: semgrep")


class TestTheMetricsTokenCanReachWhatItReads:
    """An explicit `permissions:` block zeroes every scope it does not list.

    So a new API surface in the metrics script is a silent 403 waiting for the
    next scheduled run, and because `gh_json` raises rather than publish a false
    zero, that 403 costs the entire weekly report -- the total break the fail-soft
    split exists to prevent. The scopes are asserted from the endpoints the script
    actually names, so adding a call without its grant fails here rather than on a
    Monday.
    """

    # API path fragment -> the GITHUB_TOKEN scope line it requires.
    REQUIRED_SCOPES = {
        "/actions/": "actions: read",
        "/check-runs": "checks: read",
    }

    def test_every_api_surface_the_script_reads_is_granted(self) -> None:
        script = METRICS_SCRIPT.read_text(encoding="utf-8")
        doc = yaml.safe_load(ANALYSIS.read_text(encoding="utf-8"))
        granted = doc.get("permissions") or {}
        for fragment, scope in self.REQUIRED_SCOPES.items():
            if fragment not in script:
                continue
            key, _, value = scope.partition(": ")
            assert granted.get(key) == value, (
                f"the metrics script reads {fragment}, which needs `{scope}`; an "
                f"explicit permissions block grants nothing it does not list, so "
                f"the scheduled run 403s and files no report at all"
            )


class TestHarvestDenominator:
    """`escapes` must mean "the gate ran and this merged anyway" -- nothing else.

    Two ways to corrupt it, in opposite directions, and both have already
    happened. Counting a PR the gate never applied to manufactures a hole: the
    first audit of this mechanism reported two release backports as escapes. And
    excluding by the PR's CREATION time hides a real one: a PR opened before the
    rollout but pushed to afterwards gets a fresh hygiene run carrying the gate,
    so if it merged with no section it escaped -- and a creation-time filter
    publishes that as zero.

    Applicability is therefore read off the PR's own hygiene run.
    """

    def _module(self):
        spec = importlib.util.spec_from_file_location("fix_loop_metrics", METRICS_SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @staticmethod
    def _fake_gh(prs: list[dict], run_created: dict[str, str] | None = None):
        """Stand in for `gh`: the default-branch probe, the gate-run probe, the PR list."""
        created = run_created or {}

        def fake_gh_json(*args, **kwargs):
            if args and args[0] == "repo":
                return {"defaultBranchRef": {"name": "main"}}
            if args and args[0] == "api":
                sha = str(args[1]).split("head_sha=")[1].split("&")[0]
                when = created.get(sha)
                return {"workflow_runs": [{"created_at": when}] if when else []}
            return prs

        return fake_gh_json

    @staticmethod
    def _pr(title: str, body: str, sha: str, base: str = "main") -> dict:
        return {
            "title": title,
            "body": body,
            "mergedAt": "2026-09-01T00:00:00Z",
            "baseRefName": base,
            "headRefOid": sha,
        }

    def test_other_base_prs_are_excluded_not_counted_as_escapes(self) -> None:
        mod = self._module()
        prs = [
            self._pr("fix: a", "## Pattern harvest\nRule candidate: semgrep", "sha-a"),
            self._pr("fix: b", "no section", "sha-b", base="release/0.5.0"),
            self._pr("feat: c", "", "sha-c"),
        ]
        fake = self._fake_gh(prs, {"sha-a": "2026-09-01T00:00:00Z"})
        mod.gh_json = fake
        out = mod.harvest_metrics("o/r", "2026-08-31T00:00:00Z")
        assert out["merged_fix_prs"] == 1, "only default-branch fix PRs are in scope"
        assert out["escapes"] == 0, "a release backport is not an escape"
        assert out["excluded_other_base"] == 1
        assert out["adoption_pct"] == 100.0

    def test_a_pr_whose_hygiene_never_ran_under_the_gate_is_excluded(self) -> None:
        """Two ways to have never been asked: an older run, or no run at all.

        The older-run case covers a re-run too: re-running a pre-gate run
        re-executes the ORIGINAL workflow version, and the run's `created_at`
        stays put, so it must not be read as gate-bearing.
        """
        mod = self._module()
        prs = [
            self._pr("fix: pre-gate run", "no section", "sha-old"),
            self._pr("fix: no run at all", "no section", "sha-none"),
        ]
        mod.gh_json = self._fake_gh(prs, {"sha-old": "2026-08-30T00:00:00Z"})
        out = mod.harvest_metrics("o/r", "2026-08-31T00:00:00Z")
        assert out["merged_fix_prs"] == 0
        assert out["escapes"] == 0
        assert out["excluded_gate_never_ran"] == 2
        assert out["adoption_pct"] is None, "an empty denominator must not render a percentage"

    def test_a_pre_gate_pr_re_run_under_the_gate_is_a_real_escape(self) -> None:
        """The case a creation-time filter hides.

        This PR predates the rollout, so it would have been excluded by its own
        creation date -- but it was pushed to afterwards, the gate ran on it, and
        it merged with no section. That is an escape and must be reported.
        """
        mod = self._module()
        prs = [self._pr("fix: opened early, pushed late", "no section", "sha-late")]
        mod.gh_json = self._fake_gh(prs, {"sha-late": "2026-09-01T00:00:00Z"})
        out = mod.harvest_metrics("o/r", "2026-08-31T00:00:00Z")
        assert out["merged_fix_prs"] == 1
        assert out["escapes"] == 1
        assert out["excluded_gate_never_ran"] == 0

    def test_a_real_escape_is_still_reported(self) -> None:
        mod = self._module()
        prs = [self._pr("fix: a", "no section", "sha-a")]
        mod.gh_json = self._fake_gh(prs, {"sha-a": "2026-09-01T00:00:00Z"})
        out = mod.harvest_metrics("o/r", "2026-08-31T00:00:00Z")
        assert out["escapes"] == 1
        assert out["adoption_pct"] == 0.0


class TestPatternHarvest:
    def test_pr_template_carries_the_section_and_both_branches(self) -> None:
        """Authors can only fill in a section the template still offers."""
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "## Pattern harvest" in text
        assert "Rule candidate:" in text, "the generalizable branch vanished from the template"
        assert "Not generalizable:" in text, "the one-off branch vanished from the template"

    def test_hygiene_gate_requires_the_section_on_fix_prs(self) -> None:
        """The template alone is a suggestion; the hygiene step is what makes it land."""
        text = CODE_REVIEW.read_text(encoding="utf-8")
        assert "Require Pattern harvest section on fix PRs" in text
        assert "Pattern harvest" in text
        # The gate is scoped to fix/revert titles — a gate that fired on every
        # PR would be removed within a week, taking the mechanism with it.
        assert "fix|revert" in text

    def test_hygiene_gate_reads_the_body_through_env(self) -> None:
        """PR bodies are attacker-controlled; they must reach the shell via env only."""
        text = CODE_REVIEW.read_text(encoding="utf-8")
        assert "PR_BODY: ${{ github.event.pull_request.body }}" in text

    def test_semgrep_owns_the_sentinel_rule_with_fixtures(self) -> None:
        """The first harvested pattern stays enforced, and its fixtures keep both directions."""
        rule = SEMGREP_RULE.read_text(encoding="utf-8")
        assert "kirocrew.find-sentinel-in-boolean-context" in rule
        fixture = SEMGREP_FIXTURE.read_text(encoding="utf-8")
        assert "# ruleid: kirocrew.find-sentinel-in-boolean-context" in fixture
        assert "# ok: kirocrew.find-sentinel-in-boolean-context" in fixture

    def test_autosde_carries_the_harvested_pattern_rule(self) -> None:
        """The review lanes read AUTOSDE, not prose promises — the harvest rule must live there."""
        text = AUTOSDE.read_text(encoding="utf-8")
        assert "recurring-defect-patterns" in text


class TestDeferralDiscipline:
    def test_convergence_refuses_deferral_of_security_findings(self) -> None:
        """A deferral must never adjudicate away a security/data-loss finding."""
        text = CONVERGENCE.read_text(encoding="utf-8")
        assert "DEFERRAL is not an adjudication" in text
        assert "security" in text and "data-loss" in text

    def test_deferral_check_validates_the_full_promise_shape(self) -> None:
        """Label, owner, and due date are the promise; dropping any one unmakes it."""
        text = DEFERRAL_CHECK.read_text(encoding="utf-8")
        assert "issue_comment" in text
        assert "accepted-and-deferred" in text
        assert LABEL in text
        assert "assignee" in text
        assert "Due: YYYY-MM-DD" in text

    def test_deferral_check_reads_the_comment_through_env(self) -> None:
        """Comment bodies are attacker-controlled; they must reach the shell via env only."""
        text = DEFERRAL_CHECK.read_text(encoding="utf-8")
        assert "COMMENT_BODY: ${{ github.event.comment.body }}" in text
        # The body expression must appear ONLY in env stanzas — an occurrence
        # inside a run: block would be shell injection on a public repo.
        assert text.count("${{ github.event.comment.body }}") == 1

    def test_audit_sweeps_the_label_on_a_schedule(self) -> None:
        """An untracked overdue deferral is invisible; the weekly sweep is its visibility."""
        text = AUDIT.read_text(encoding="utf-8")
        assert "schedule" in text
        assert LABEL in text
        assert "overdue" in text

    def test_prepare_pr_skill_teaches_the_tracked_deferral_shape(self) -> None:
        """The agent filing the follow-up issue must know the shape CI will check."""
        text = PREPARE_PR.read_text(encoding="utf-8")
        assert LABEL in text
        assert "Due: YYYY-MM-DD" in text
        assert "never deferrable" in text


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the deferral step runs under bash on ubuntu-latest",
)
class TestDeferralCheckIssueReadFailure:
    """Execute the ACTUAL deferral-validation step with ``gh`` stubbed.

    `2>/dev/null || true` used to collapse a transient API failure onto the
    same empty string as "this number is not an issue here", so a network blip
    made every referenced follow-up look unresolvable and the checker posted a
    refusal blaming the author for an untracked deferral they had in fact
    tracked. These cases pin the outcomes apart: HTTP 404 keeps its meaning
    (the reference does not resolve), a transient failure is absorbed by the
    bounded retry, and a read that never succeeds fails the check closed
    without posting the false refusal.
    """

    def _run_check(self, tmp_path: Path, mode: str):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("the step is Bash; skip where Bash is absent")
        jq = shutil.which("jq")
        if jq is None:
            pytest.skip("the step shells out to jq; skip where jq is absent")
        wf = yaml.safe_load(DEFERRAL_CHECK.read_text(encoding="utf-8"))
        steps = wf["jobs"]["validate-deferral"]["steps"]
        step = next(
            (
                s
                for s in steps
                if s.get("name") == "Check the deferral names a tracked follow-up issue"
            ),
            None,
        )
        assert step is not None, "deferral validation step not found"
        issue_file = tmp_path / "issue.json"
        issue_file.write_text(
            '{"state":"open","labels":[{"name":"deferred-finding"}],'
            '"assignees":[{"login":"owner"}],"body":"Due: 2031-01-01","milestone":null}',
            encoding="utf-8",
        )
        attempts = tmp_path / "gh-attempts"
        posts = tmp_path / "gh-posts"
        gh = tmp_path / "gh"
        # The stub serves two shapes: the comment POST at the refusal tail
        # (recorded, never failed) and the issue read (mode-dependent).
        stub = (
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *comments*)\n"
            f'    printf \'%s\\n\' "$*" >> "{posts}"\n'
            "    exit 0\n"
            "    ;;\n"
            "esac\n"
            f'printf x >> "{attempts}"\n'
        )
        if mode == "persistent-failure":
            stub += 'echo "gh: HTTP 500 Internal Server Error" >&2\nexit 1\n'
        elif mode == "not-found":
            stub += 'echo "gh: Not Found (HTTP 404)" >&2\nexit 1\n'
        elif mode == "transient-then-good":
            stub += (
                f'if [ "$(wc -c < "{attempts}")" -le 1 ]; then\n'
                '  echo "gh: HTTP 502 Bad Gateway" >&2\n'
                "  exit 1\n"
                "fi\n"
                f'cat "{issue_file}"\n'
            )
        else:
            stub += f'cat "{issue_file}"\n'
        gh.write_text(stub, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        env = {
            # tmp_path first so the `gh` stub wins; jq's own directory follows
            # so the hermetic PATH still finds it on hosts where it does not
            # live in a standard system dir. Starve any real gh of credentials
            # so a stub-resolution failure can never turn into a live API call.
            "PATH": (
                f"{tmp_path}{os.pathsep}{Path(jq).parent}{os.pathsep}"
                f"/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin"
            ),
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
            "LC_ALL": "C",
            "COMMENT_BODY": (
                "<!-- ai-review-disposition target=gpt head=abc -->\n"
                "- **finding**: accepted-and-deferred -- tracked in #123"
            ),
            "REPO": "example/repo",
            "PR_NUMBER": "999",
            "COMMENT_URL": "https://example.com/pr/999#comment-1",
            "TMPDIR": str(tmp_path),
        }
        result = subprocess.run(
            [bash, "-c", step["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        return result, attempts, posts

    def test_tracked_deferral_passes_on_one_read(self, tmp_path: Path) -> None:
        result, attempts, posts = self._run_check(tmp_path, "good")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "tracked by issue #123" in result.stdout, result.stdout
        assert not posts.exists(), "a refusal was posted for a tracked deferral"
        assert attempts.read_text(encoding="utf-8") == "x", "retry fired on a good read"

    def test_transient_issue_read_is_absorbed(self, tmp_path: Path) -> None:
        result, attempts, posts = self._run_check(tmp_path, "transient-then-good")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "tracked by issue #123" in result.stdout, result.stdout
        assert not posts.exists(), "a refusal was posted despite the retry succeeding"
        assert attempts.read_text(encoding="utf-8") == "xx", "expected exactly one retry"

    def test_persistent_read_failure_fails_closed_without_blaming_the_author(
        self, tmp_path: Path
    ) -> None:
        # The failure this pins: a read that never succeeds must fail the
        # check (re-runnable) instead of posting a refusal that blames the
        # author for an untracked deferral the checker never actually read.
        # What IS posted is the unverified-state notice -- this lane has no
        # PR-visible check row, so a silent red run would be a signal nobody
        # sees.
        result, attempts, posts = self._run_check(tmp_path, "persistent-failure")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "This is a read failure, not an untracked deferral" in result.stdout, result.stdout
        posted = posts.read_text(encoding="utf-8") if posts.exists() else ""
        assert posted, "the unverified-state notice was never posted"
        assert "Could not verify this deferral" in posted, posted
        assert "not yet tracked" not in posted, "the false refusal was still posted"
        assert attempts.read_text(encoding="utf-8") == "xxx", "expected three attempts"

    def test_missing_issue_keeps_meaning_an_untracked_deferral(self, tmp_path: Path) -> None:
        # HTTP 404 is the one failure that legitimately means "this reference
        # does not resolve": the refusal path must still fire, on the first
        # attempt, with no retry spent on it.
        result, attempts, posts = self._run_check(tmp_path, "not-found")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Deferral promise is incomplete" in result.stdout, result.stdout
        posted = posts.read_text(encoding="utf-8") if posts.exists() else ""
        assert "not yet tracked" in posted, "the refusal reply was never posted"
        assert attempts.read_text(encoding="utf-8") == "x", "a 404 must not be retried"
        # The reply is staged under the step's temp dir, so this test's TMPDIR
        # containment binds -- executing the real step must not leave an
        # artifact at a hardcoded host path that outlives the run.
        assert (tmp_path / "reply.md").exists(), "reply was not staged under TMPDIR"
