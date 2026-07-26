"""Regression tests for human-readable and human-overridable AI reviews."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class TestHumanOverrideHandler:
    def test_handler_runs_from_trusted_issue_comment_context(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")

        assert "issue_comment:" in workflow
        assert "pull_request_target:" not in workflow
        assert "actions/checkout@" not in workflow
        assert "/ai-review override <fable|gpt|arbiter|all> <current-sha>: <reason>" in workflow

    def test_handler_requires_write_permission_fresh_sha_and_reason(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")

        assert 'if [ "$ACTOR" = "$author" ]; then' not in workflow
        assert "collaborators/$ACTOR/permission" in workflow
        assert "admin|maintain|write) allowed=true" in workflow
        assert 'if [[ "$head" != "$requested_sha"* ]]; then' in workflow
        assert 'if [ -z "$reason" ]; then' in workflow
        assert 'if [ "${#reason}" -gt 500 ]; then' in workflow
        assert "only a repository writer" in workflow

    def test_handler_records_a_bot_marker_before_changing_checks(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")
        marker = (
            "<!-- ai-review-human-override target=$target head=$head "
            "actor=$ACTOR source=$COMMENT_ID -->"
        )

        assert marker in workflow
        assert workflow.index(marker) < workflow.index("actions/runs/$run_id/rerun")
        assert "select(.head_sha == $head" in workflow
        assert 'name="Arbiter — judge from comments"' in workflow
        assert "-f status=completed -f conclusion=success" in workflow


class TestLineReviewHumanOverrides:
    def test_fable_consumes_only_a_bot_authored_sha_scoped_record(self) -> None:
        workflow = _workflow("claude-review.yml")

        assert "target=fable head=$HEAD" in workflow
        assert '.user.login == "github-actions[bot]"' in workflow
        assert "steps.human_override.outputs.active != 'true'" in workflow
        assert "✅ human override accepted" in workflow
        assert "Human judgment by $OVERRIDE_ACTOR overrides Fable 5" in workflow
        assert "/ai-review override fable $HEAD:" in workflow

    def test_gpt_has_clear_verdict_banner_and_human_override(self) -> None:
        workflow = _workflow("codex-review.yml")

        assert "target=gpt head=$HEAD" in workflow
        assert '.user.login == "github-actions[bot]"' in workflow
        assert "steps.human_override.outputs.active != 'true'" in workflow
        assert 'verdict="✅ no blocking findings"' in workflow
        assert (
            "GPT 5.6 completed its review of \\`$HEAD\\` and found no blocking issues." in workflow
        )
        assert "✅ human override accepted" in workflow
        assert "Human judgment by $OVERRIDE_ACTOR overrides GPT 5.6" in workflow
        assert "/ai-review override gpt $HEAD:" in workflow


class TestArbiterPresentation:
    def test_arbiter_replaces_stale_results_while_waiting(self) -> None:
        workflow = _workflow("longterm-arbiter.yml")

        assert 'TITLE="⏳ review pending"' in workflow
        assert "this replaces any stale verdict from the previous commit" in workflow
        assert "Always refresh the human-facing comment, including while waiting" in workflow

    def test_arbiter_has_clear_verdict_and_override_paths(self) -> None:
        workflow = _workflow("longterm-arbiter.yml")

        assert "target=arbiter head=$SHA" in workflow
        assert 'STATE="human_override"' in workflow
        assert 'TITLE="✅ no blocking findings"' in workflow
        assert (
            "Arbiter found no unresolved long-term items that require action before "
            "merging \\`$SHA\\`." in workflow
        )
        assert 'TITLE="✅ human override accepted"' in workflow
        assert "/ai-review override arbiter $SHA:" in workflow
        assert "defer-longterm" in workflow
