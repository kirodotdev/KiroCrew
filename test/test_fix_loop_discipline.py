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

from pathlib import Path

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
