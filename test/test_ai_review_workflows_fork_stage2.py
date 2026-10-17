"""Review-workflow tests for the fork Stage-2 lanes: their trigger, concurrency
groups and exact-head identity, their check-run lifecycle, and one publisher per
protected check name.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    FORK_REVIEW_LANES,
    _bash,
    _child_env,
    _concurrency_group,
    _flat,
    _proc_log,
    _step_env,
    _step_script,
    _stub_path,
    _workflow,
)

FORK_STAGE2_WORKFLOW_RUN_LANES = FORK_REVIEW_LANES + ("fork-internal-content-scan.yml",)
EXACT_IDENTITY_REVIEW_LANES = (
    "fork-opus-review.yml",
    "fork-gpt-review.yml",
    "fork-design-review.yml",
    "fork-ux-review.yml",
)
# The lanes whose resolver tells an obsolete head (the fork branch moved on)
# apart from a lagging open-PR listing: every exact-identity review lane.
OBSOLETE_HEAD_LANES = EXACT_IDENTITY_REVIEW_LANES


def _render_trusted_event_group(template: str, event: dict) -> str:
    """Render the deliberately tiny trusted-event expression used by fork lanes."""
    workflow_run = event.get("workflow_run") or {}
    pull_request = event.get("pull_request") or {}
    if "||" in template:
        identity = workflow_run.get("id") or pull_request.get("id")
        event_name = str(event.get("event_name") or "")
        expression = "${{ github.event.workflow_run.id || github.event.pull_request.id }}"
        assert event_name
        assert template.count("${{") == 2
        assert "${{ github.event_name }}" in template
        template = template.replace("${{ github.event_name }}", event_name)
    else:
        identity = workflow_run.get("id")
        expression = "${{ github.event.workflow_run.id }}"
        assert template.count("${{") == 1
    assert identity is not None
    assert expression in template
    return template.replace(expression, str(identity))


class TestForkStage2ConcurrencyIdentity:
    @pytest.mark.parametrize("lane", FORK_STAGE2_WORKFLOW_RUN_LANES)
    def test_case_only_and_long_refs_cannot_collide_or_expand_review_groups(
        self, lane: str
    ) -> None:
        template = _concurrency_group(lane)
        prefix = f"{lane.removesuffix('.yml')}-"
        sha = "a" * 40
        case_upper = {
            "workflow_run": {
                "id": 101,
                "run_attempt": 1,
                "head_repository": {"full_name": "outside/example"},
                "head_branch": "Feature/Case",
                "head_sha": sha,
            }
        }
        case_lower = {
            "workflow_run": {
                "id": 202,
                "run_attempt": 1,
                "head_repository": {"full_name": "outside/example"},
                "head_branch": "feature/case",
                "head_sha": sha,
            }
        }
        long_ref = {
            "workflow_run": {
                "id": 303,
                "run_attempt": 1,
                "head_repository": {"full_name": "outside/example"},
                "head_branch": "feature/" + "x" * 4096,
                "head_sha": sha,
            }
        }

        assert _render_trusted_event_group(template, case_upper) == f"{prefix}101"
        assert _render_trusted_event_group(template, case_lower) == f"{prefix}202"
        assert _render_trusted_event_group(template, long_ref) == f"{prefix}303"
        assert len(f"{prefix}303") < 64
        assert "head_branch" not in template
        assert "head_repository" not in template
        assert "head_sha" not in template

    @pytest.mark.parametrize("lane", FORK_STAGE2_WORKFLOW_RUN_LANES)
    def test_same_trigger_rerun_collapses_to_the_same_review_group(self, lane: str) -> None:
        template = _concurrency_group(lane)
        first = {"workflow_run": {"id": 987654321, "run_attempt": 1}}
        rerun = {"workflow_run": {"id": 987654321, "run_attempt": 2}}

        assert _render_trusted_event_group(template, first) == _render_trusted_event_group(
            template, rerun
        )

    def test_workflow_guard_uses_bounded_identity_for_both_event_shapes(self) -> None:
        template = _concurrency_group("fork-workflow-guard.yml")
        sha = "a" * 40
        workflow_run = {
            "event_name": "workflow_run",
            "workflow_run": {
                "id": 404,
                "run_attempt": 1,
                "head_repository": {"full_name": "outside/example"},
                "head_branch": "Feature/Case",
                "head_sha": sha,
            },
        }
        pull_request_target = {
            "event_name": "pull_request_target",
            "pull_request": {
                "id": 404,
                "head": {
                    "repo": {"full_name": "outside/example"},
                    "ref": "feature/" + "x" * 4096,
                    "sha": sha,
                },
            },
        }

        assert (
            _render_trusted_event_group(template, workflow_run)
            == "fork-workflow-guard-workflow_run-404"
        )
        assert (
            _render_trusted_event_group(template, pull_request_target)
            == "fork-workflow-guard-pull_request_target-404"
        )
        assert len(_render_trusted_event_group(template, pull_request_target)) < 64
        assert "head_branch" not in template
        assert "head.ref" not in template
        assert "head_sha" not in template


class TestForkStage2ExactHeadIdentity:
    """Execute the privileged resolvers against authoritative identity cases."""

    _SHA = "a" * 40
    _BASE = "b" * 40
    _REPO = "outside/example"
    _REF = "feature/exact-head"
    _SPECIAL_REF = 'feature/slash-"quote"-% space-유니코드'

    @staticmethod
    def _row(number: int, *, sha: str, repo: str, ref: str) -> dict:
        return {
            "number": number,
            "state": "open",
            "head": {"sha": sha, "repo": {"full_name": repo}, "ref": ref},
        }

    def _run_review_resolver(
        self,
        tmp_path: Path,
        lane: str,
        pages: list[list[dict]],
        *,
        repo: str | None = None,
        ref: str | None = None,
        list_rc: int = 0,
        branch_tip: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        bash = _bash()
        if bash is None:
            pytest.skip("the resolver step is Bash; skip where Bash is absent")

        pages_file = tmp_path / "pages.json"
        pages_file.write_text(
            "\n".join(json.dumps(page, ensure_ascii=False) for page in pages),
            encoding="utf-8",
        )
        calls = tmp_path / "gh-calls"
        calls.touch()
        gh = tmp_path / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' "$*" >> "$CALLS"\n'
            'case "${2:-}" in\n'
            '  *"pulls?state=open&per_page=100"*)\n'
            '    if [ "$LIST_RC" -ne 0 ]; then exit "$LIST_RC"; fi\n'
            '    cat "$PAGES"; exit 0 ;;\n'
            "  */pulls/*) printf '%s\\n' \"$BASE_SHA\"; exit 0 ;;\n"
            "  */branches/*)\n"
            '    if [ -z "$BRANCH_TIP" ]; then echo "gh: HTTP 404" >&2; exit 1; fi\n'
            "    printf '%s\\n' \"$BRANCH_TIP\"; exit 0 ;;\n"
            "esac\n"
            'echo "unexpected gh call: $*" >&2\n'
            "exit 9\n",
            encoding="utf-8",
        )
        sleep = tmp_path / "sleep"
        sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        gh.chmod(0o755)
        sleep.chmod(0o755)
        output = tmp_path / "github-output"
        output.touch()

        return subprocess.run(
            [
                bash,
                "-e",
                "-o",
                "pipefail",
                "-c",
                _step_script(
                    _workflow(lane), "Resolve and validate PR (authoritative from GitHub)"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_child_env(
                {
                    "PATH": _stub_path(tmp_path),
                    "GH_TOKEN": "stub",
                    "REPO": "kirodotdev/KiroCrew",
                    "WR_HEAD_SHA": self._SHA,
                    "WR_HEAD_REPO": self._REPO if repo is None else repo,
                    "WR_HEAD_REF": self._REF if ref is None else ref,
                    "PAGES": str(pages_file),
                    "CALLS": str(calls),
                    "LIST_RC": str(list_rc),
                    "BASE_SHA": self._BASE,
                    "BRANCH_TIP": branch_tip or "",
                    "GITHUB_OUTPUT": str(output),
                }
            ),
            cwd=tmp_path,
        )

    @pytest.mark.parametrize("lane", EXACT_IDENTITY_REVIEW_LANES)
    def test_same_sha_sibling_on_an_earlier_page_cannot_answer(
        self, lane: str, tmp_path: Path
    ) -> None:
        pages = [
            [self._row(11, sha=self._SHA, repo="another/fork", ref=self._REF)],
            [self._row(22, sha=self._SHA, repo=self._REPO, ref=self._REF)],
        ]

        result = self._run_review_resolver(tmp_path, lane, pages)

        assert result.returncode == 0, _proc_log(result)
        output = (tmp_path / "github-output").read_text(encoding="utf-8")
        assert "pr=22\n" in output

    @pytest.mark.parametrize("lane", EXACT_IDENTITY_REVIEW_LANES)
    def test_special_ref_is_compared_as_data(self, lane: str, tmp_path: Path) -> None:
        pages = [[self._row(22, sha=self._SHA, repo=self._REPO, ref=self._SPECIAL_REF)]]

        result = self._run_review_resolver(tmp_path, lane, pages, ref=self._SPECIAL_REF)

        assert result.returncode == 0, _proc_log(result)
        assert "pr=22\n" in (tmp_path / "github-output").read_text(encoding="utf-8")

    @pytest.mark.parametrize("lane", EXACT_IDENTITY_REVIEW_LANES)
    def test_matches_across_pages_have_authoritative_cardinality(
        self, lane: str, tmp_path: Path
    ) -> None:
        pages = [
            [self._row(21, sha=self._SHA, repo=self._REPO, ref=self._REF)],
            [self._row(22, sha=self._SHA, repo=self._REPO, ref=self._REF)],
        ]

        result = self._run_review_resolver(tmp_path, lane, pages)

        assert result.returncode != 0, _proc_log(result)
        assert "more than one open PR" in result.stdout + result.stderr

    @pytest.mark.parametrize("lane", EXACT_IDENTITY_REVIEW_LANES)
    def test_zero_match_keeps_the_review_lanes_fail_closed(self, lane: str, tmp_path: Path) -> None:
        result = self._run_review_resolver(tmp_path, lane, [[]])

        assert result.returncode != 0, _proc_log(result)
        assert "superseded or closed" in result.stdout + result.stderr

    @pytest.mark.parametrize("lane", EXACT_IDENTITY_REVIEW_LANES)
    def test_read_failure_is_not_reported_as_zero_match(self, lane: str, tmp_path: Path) -> None:
        result = self._run_review_resolver(tmp_path, lane, [[]], list_rc=7)

        assert result.returncode != 0, _proc_log(result)
        assert "query itself failed" in result.stdout + result.stderr
        assert "superseded or closed" not in result.stdout + result.stderr

    @pytest.mark.parametrize("lane", OBSOLETE_HEAD_LANES)
    def test_zero_match_on_a_moved_branch_is_obsolete_not_red(
        self, lane: str, tmp_path: Path
    ) -> None:
        # The branch reads and points at a newer commit, so nobody can merge
        # this head: the lane records it and stops without a verdict.
        result = self._run_review_resolver(tmp_path, lane, [[]], branch_tip="c" * 40)

        assert result.returncode == 0, _proc_log(result)
        output = (tmp_path / "github-output").read_text(encoding="utf-8")
        assert output == "obsolete=true\n"
        assert "no longer the tip" in result.stdout

    @pytest.mark.parametrize("lane", OBSOLETE_HEAD_LANES)
    def test_zero_match_while_the_branch_still_points_here_fails_closed(
        self, lane: str, tmp_path: Path
    ) -> None:
        # A lagging listing with the head still at the branch tip proves
        # nothing, so the lane keeps failing closed.
        result = self._run_review_resolver(tmp_path, lane, [[]], branch_tip=self._SHA)

        assert result.returncode != 0, _proc_log(result)
        assert "superseded or closed" in result.stdout + result.stderr
        assert "obsolete" not in (tmp_path / "github-output").read_text(encoding="utf-8")

    @pytest.mark.parametrize("lane", OBSOLETE_HEAD_LANES)
    def test_zero_match_with_an_unreadable_branch_fails_closed(
        self, lane: str, tmp_path: Path
    ) -> None:
        result = self._run_review_resolver(tmp_path, lane, [[]])

        assert result.returncode != 0, _proc_log(result)
        assert "superseded or closed" in result.stdout + result.stderr
        assert "obsolete" not in (tmp_path / "github-output").read_text(encoding="utf-8")

    @pytest.mark.parametrize("lane", OBSOLETE_HEAD_LANES)
    def test_every_step_after_the_resolver_skips_an_obsolete_head(self, lane: str) -> None:
        # The resolver exits 0 on an obsolete head, so a later step that does
        # not check the output would run against an empty PR number -- and a
        # check-run POST would publish a verdict for nothing.
        workflow = yaml.safe_load(_workflow(lane))
        steps = next(
            job["steps"]
            for job in workflow["jobs"].values()
            if any(step.get("id") == "pr" for step in job.get("steps", []))
        )
        resolver = next(i for i, step in enumerate(steps) if step.get("id") == "pr")
        later = steps[resolver + 1 :]
        assert later
        for step in later:
            label = step.get("name") or step.get("uses")
            cond = " ".join(str(step.get("if", "")).split())
            assert "steps.pr.outputs.obsolete != 'true'" in cond, label
            # A top-level `||` would run the step whenever its other side
            # holds, obsolete or not.
            assert "||" not in re.sub(r"\([^()]*(?:\([^()]*\)[^()]*)*\)", "", cond), label

    @pytest.mark.parametrize("lane", EXACT_IDENTITY_REVIEW_LANES)
    @pytest.mark.parametrize(("repo", "ref"), [("", _REF), (_REPO, "")])
    def test_empty_trigger_identity_fails_before_the_api_read(
        self, lane: str, repo: str, ref: str, tmp_path: Path
    ) -> None:
        result = self._run_review_resolver(tmp_path, lane, [[]], repo=repo, ref=ref)

        assert result.returncode != 0, _proc_log(result)
        assert "empty workflow_run head identity" in result.stdout + result.stderr
        assert (tmp_path / "gh-calls").read_text(encoding="utf-8") == ""

    def test_every_review_resolver_aggregates_pages_before_exact_matching(self) -> None:
        for lane in EXACT_IDENTITY_REVIEW_LANES:
            step = _step_script(
                _workflow(lane), "Resolve and validate PR (authoritative from GitHub)"
            )
            assert '--arg sha "$head_sha"' in step, lane
            assert '--arg repo "$WR_HEAD_REPO"' in step, lane
            assert '--arg ref "$WR_HEAD_REF"' in step, lane
            assert '.state == "open"' in step, lane
            assert ".head.repo.full_name == $repo" in step, lane
            assert ".head.ref == $ref" in step, lane
            assert ".head.sha == $sha" in step, lane
            assert '$repo == ""' not in step, lane
            assert '$ref == ""' not in step, lane
            assert "candidate_count" in step, lane
            assert re.search(r"--paginate \\\n\s+\| jq -rs", step), lane

    def test_workflow_guard_keeps_its_distinct_identity_dispositions(self, tmp_path: Path) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the resolver step is Bash; skip where Bash is absent")
        workflow = _workflow("fork-workflow-guard.yml")
        step = _step_script(workflow, "Evaluate workflow-change guard")
        step = step.split("\n  strip-stale-override:", 1)[0]
        gh = tmp_path / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'case "${2:-}" in\n'
            '  *"pulls?state=open&per_page=100"*)\n'
            '    if [ "$LIST_RC" -ne 0 ]; then exit "$LIST_RC"; fi\n'
            '    cat "$PAGES"; exit 0 ;;\n'
            "  */pulls/*/files) printf 'src/example.py\\n'; exit 0 ;;\n"
            "  */issues/*/labels) exit 0 ;;\n"
            "  --method) exit 0 ;;\n"
            "esac\n"
            'echo "unexpected gh call: $*" >&2\n'
            "exit 9\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)

        def run(
            pages: list[list[dict]], *, list_rc: int = 0, pull_request_target: bool = False
        ) -> subprocess.CompletedProcess[str]:
            pages_file = tmp_path / "guard-pages.json"
            pages_file.write_text("\n".join(json.dumps(page) for page in pages), encoding="utf-8")
            wr = ("", "", "") if pull_request_target else (self._SHA, self._REPO, self._REF)
            pr = (self._SHA, self._REPO, self._REF) if pull_request_target else ("", "", "")
            return subprocess.run(
                [bash, "-e", "-o", "pipefail", "-c", step],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=_child_env(
                    {
                        "PATH": _stub_path(tmp_path),
                        "GH_TOKEN": "stub",
                        "REPO": "kirodotdev/KiroCrew",
                        "WR_HEAD_SHA": wr[0],
                        "WR_HEAD_REPO": wr[1],
                        "WR_HEAD_REF": wr[2],
                        "PR_HEAD_SHA": pr[0],
                        "PR_HEAD_REPO": pr[1],
                        "PR_HEAD_REF": pr[2],
                        "OVERRIDE_LABEL": "allow-fork-workflow-change",
                        "PAGES": str(pages_file),
                        "LIST_RC": str(list_rc),
                    }
                ),
                cwd=tmp_path,
            )

        zero = run([[]])
        assert zero.returncode == 0, _proc_log(zero)
        assert "superseded or closed" in zero.stdout + zero.stderr

        ambiguous = run(
            [
                [self._row(21, sha=self._SHA, repo=self._REPO, ref=self._REF)],
                [self._row(22, sha=self._SHA, repo=self._REPO, ref=self._REF)],
            ]
        )
        assert ambiguous.returncode != 0, _proc_log(ambiguous)
        assert "more than one open PR" in ambiguous.stdout + ambiguous.stderr

        unreadable = run([[]], list_rc=7)
        assert unreadable.returncode != 0, _proc_log(unreadable)
        assert "query itself failed" in unreadable.stdout + unreadable.stderr
        assert "superseded or closed" not in unreadable.stdout + unreadable.stderr

        pull_request_target = run(
            [[self._row(22, sha=self._SHA, repo=self._REPO, ref=self._REF)]],
            pull_request_target=True,
        )
        assert pull_request_target.returncode == 0, _proc_log(pull_request_target)
        assert "guard verdict: success" in pull_request_target.stdout

    def test_workflow_guard_uses_exact_identity_for_both_event_shapes(self) -> None:
        workflow = _workflow("fork-workflow-guard.yml")
        step = _step_script(workflow, "Evaluate workflow-change guard")
        env = _step_env("fork-workflow-guard.yml", "Evaluate workflow-change guard")

        for name in (
            "WR_HEAD_SHA",
            "WR_HEAD_REPO",
            "WR_HEAD_REF",
            "PR_HEAD_SHA",
            "PR_HEAD_REPO",
            "PR_HEAD_REF",
        ):
            assert name in env
        assert '--arg repo "$head_repo"' in step
        assert '--arg ref "$head_ref"' in step
        assert '--arg sha "$head_sha"' in step
        assert ".head.repo.full_name == $repo" in step
        assert ".head.ref == $ref" in step
        assert ".head.sha == $sha" in step
        assert "candidate_count" in step
        assert 'gh api "repos/$REPO/pulls/$pr/files" --paginate' in step


FORK_FINALIZE_LANES = (
    "fork-opus-review.yml",
    "fork-gpt-review.yml",
    "fork-design-review.yml",
    "fork-ux-review.yml",
)


class TestForkLaneFinalizeRetries:
    """The fork lanes must not finalize their check-run with a bare
    `PATCH … || true`.

    One transient API failure there leaves the run `in_progress` forever, and
    pr-readiness counts ANY non-completed check-run of that name as pending --
    including after a successful re-run -- so the PR sits at
    `readiness: checking` with no event able to clear it. `|| true` also
    swallowed the failure, so nothing in the log said why.

    fork-first-principles-review.yml already carries the retry helper this
    pins; these tests keep the other four from drifting back.
    """

    @pytest.mark.parametrize("lane", FORK_FINALIZE_LANES)
    def test_the_finalize_patch_retries_before_giving_up(self, lane: str) -> None:
        flat = _flat(_workflow(lane))
        assert "complete() {" in flat, f"{lane}: no complete() helper"
        assert (
            "for attempt in 1 2; do" in flat
        ), f"{lane}: finalize does not retry, so one transient 5xx strands the run"

    @pytest.mark.parametrize("lane", FORK_FINALIZE_LANES)
    def test_a_permanent_finalize_failure_is_announced(self, lane: str) -> None:
        """`|| true` alone made a stranded run silent. A wedged PR must at
        least say so in the job log."""
        flat = _flat(_workflow(lane))
        assert (
            "could not complete check-run" in flat
        ), f"{lane}: a failed finalize leaves no trace in the log"

    @pytest.mark.parametrize("lane", FORK_FINALIZE_LANES)
    def test_the_bare_unretried_patch_is_gone(self, lane: str) -> None:
        """Shape guard: the defect is the un-retried form, so pin its absence
        rather than only the presence of the replacement."""
        flat = _flat(_workflow(lane))
        assert 'check-runs/$CHECK_ID" -f status="completed"' not in flat or (
            "complete() {" in flat
        ), f"{lane}: bare un-retried finalize PATCH is back"

    def test_the_helper_matches_the_reference_lane(self) -> None:
        """The first-principles lane is where this helper was introduced; the
        ported copies should not diverge from its retry shape."""
        ref = _flat(_workflow("fork-first-principles-review.yml"))
        assert "for attempt in 1 2; do" in ref
        assert "could not complete check-run" in ref
        for lane in FORK_FINALIZE_LANES:
            flat = _flat(_workflow(lane))
            assert "for attempt in 1 2; do" in flat, lane
            assert "sleep 5" in flat, f"{lane}: retry has no backoff"


# (lane file, check-run name, external_id prefix, finalize step name)
FORK_SWEEP_LANES = (
    ("fork-design-review.yml", "Design Review", "design", "Finalize check-run (advisory)"),
    (
        "fork-first-principles-review.yml",
        "First Principles Review",
        "first-principles",
        "Finalize check-run (advisory)",
    ),
    ("fork-gpt-review.yml", "GPT 6.1 Review", "gpt", "Finalize check-run (fail closed)"),
    ("fork-opus-review.yml", "Opus 5.5 Review", "opus", "Finalize check-run (fail closed)"),
    ("fork-ux-review.yml", "UX Review", "ux", "Finalize check-run (advisory)"),
)


class TestForkLaneStrandedRunSweeps:
    """The retry alone still loses when BOTH attempts fail, and it cannot touch
    a run stranded by a PREVIOUS workflow run. fork-first-principles-review.yml
    carries the sweep that lists still-incomplete check-runs of the lane's name
    on the head and completes every one THIS pull request created; the other
    four fork lanes carry the same sweep. All five lanes are pinned here,
    including the reference lane itself -- its sweep passes the run's computed
    verdict instead of a hardcoded neutral, the shape the ported lanes share.
    """

    @pytest.mark.parametrize(("lane", "check_name", "prefix", "finalize"), FORK_SWEEP_LANES)
    def test_check_run_is_created_with_a_pr_scoped_external_id(
        self, lane: str, check_name: str, prefix: str, finalize: str
    ) -> None:
        # Without an external_id at CREATION the sweep has nothing safe to
        # match on: a check-run of this name on this head can belong to a
        # different PR that shares the commit. The id is now two-dimensional
        # (PR + triggering run id + attempt) so a rerun on an unchanged head
        # cannot reuse the previous attempt's verdict.
        opened = _step_script(_workflow(lane), "Open check-run (in progress)")
        assert (
            f'-f external_id="{prefix}-pr-$PR-$WR_RUN_ID-$WR_RUN_ATTEMPT"' in opened
        ), f"{lane}: check-run created without a PR+attempt-scoped external_id"

    @pytest.mark.parametrize(("lane", "check_name", "prefix", "finalize"), FORK_SWEEP_LANES)
    def test_finalize_sweeps_stranded_check_runs(
        self, lane: str, check_name: str, prefix: str, finalize: str
    ) -> None:
        script = _step_script(_workflow(lane), finalize)
        assert (
            "completing stranded check-run" in script
        ), f"{lane}: no stranded-run sweep -- a doubly-failed finalize wedges the PR"
        assert "check-runs?check_name=$enc&per_page=100" in script, lane

    @pytest.mark.parametrize(("lane", "check_name", "prefix", "finalize"), FORK_SWEEP_LANES)
    def test_sweep_only_completes_check_runs_this_pr_created(
        self, lane: str, check_name: str, prefix: str, finalize: str
    ) -> None:
        # Two open PRs can share a head commit; an unscoped sweep would publish
        # a verdict computed from another PR's diff. The match is a PREFIX on the
        # PR dimension (not exact equality) so it still catches a row stranded by
        # a PREVIOUS attempt, whose id carries a different run+attempt suffix; the
        # trailing hyphen keeps -pr-9 from matching -pr-99.
        script = _step_script(_workflow(lane), finalize)
        assert (
            f'select(.external_id | startswith(\\"{prefix}-pr-$PR-\\"))' in script
        ), f"{lane}: sweep is not scoped by external_id prefix"
        assert (
            f'select(.external_id == \\"{prefix}-pr-$PR\\")' not in script
        ), f"{lane}: sweep still uses the old attempt-blind exact match"
        assert '[ -n "${PR:-}" ]' in script, f"{lane}: sweep runs without a resolved PR"
        assert (
            'select(.status != "completed") | .id' not in script
        ), f"{lane}: unscoped sweep must not come back"

    @pytest.mark.parametrize(("lane", "check_name", "prefix", "finalize"), FORK_SWEEP_LANES)
    def test_sweep_completes_with_the_computed_verdict(
        self, lane: str, check_name: str, prefix: str, finalize: str
    ) -> None:
        # A hardcoded neutral at the sweep site either outvotes a
        # genuine green re-run under pr-readiness's fail-precedence, or
        # launders a genuine BLOCK whose own PATCH lost both attempts into an
        # un-gated neutral. The sweep must pass the run's computed verdict --
        # with no verdict, $conclusion already holds the lane's
        # incomplete/advisory posture, so a genuinely-stranded run's behavior
        # is unchanged.
        script = _step_script(_workflow(lane), finalize)
        assert (
            'complete "$id" "$conclusion" "$title"' in script
        ), f"{lane}: sweep does not pass the computed verdict"
        assert (
            'complete "$id" "neutral"' not in script
        ), f"{lane}: hardcoded-neutral sweep must not come back"

    @pytest.mark.parametrize(("lane", "check_name", "prefix", "finalize"), FORK_SWEEP_LANES)
    def test_sweep_does_not_overwrite_a_newer_runs_row(
        self, lane: str, check_name: str, prefix: str, finalize: str
    ) -> None:
        # Concurrency is keyed to workflow_run.id, so a close/reopen (or any
        # same-sha retrigger) now yields two uncancelled lane runs on one head.
        # The PR-dim prefix spans both, so the sweep must NOT complete a row
        # whose external_id run-id dimension is newer than this run's -- that row
        # belongs to a later trigger and completing it would publish THIS run's
        # (older, stale) verdict for the newer attempt. The guard compares run
        # ids numerically; a row it cannot parse is still swept (fail-closed).
        script = _step_script(_workflow(lane), finalize)
        assert "newer than $WR_RUN_ID" in script, f"{lane}: sweep has no newest-run-wins guard"
        assert (
            '[ "$row_run" -gt "$WR_RUN_ID" ]' in script
        ), f"{lane}: sweep does not compare run ids numerically"
        # The guard reads the row's own external_id, so the jq must emit it
        # alongside the check-run id (escaped as it appears inside the --jq arg).
        assert (
            r"\"\(.id) \(.external_id)\"" in script
        ), f"{lane}: sweep must carry each row's external_id to the guard"

    def test_gpt_sweep_skips_newer_run_but_completes_older(self, tmp_path: Path) -> None:
        # Execute the gpt finalize sweep against two incomplete rows for this PR
        # and head: one from an OLDER run id (must be completed) and one from a
        # NEWER run id (must be left alone). This is the close/reopen two-run
        # case the workflow_run.id concurrency rekey introduced.
        bash = _bash()
        if bash is None:
            pytest.skip("the finalize step is Bash; skip where Bash is absent")

        calls = tmp_path / "patch-calls"
        calls.touch()
        gh = tmp_path / "gh"
        # Stub gh: the sweep's GET passes `--jq`, which real gh applies server-
        # side; the stub mimics the ALREADY-FILTERED output (one `<id> <ext>`
        # line per incomplete row matching the prefix). A PATCH to a check-run
        # id records that id; every other call (the fallback POST) is a silent OK.
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            '    *"/check-runs?check_name="*)\n'
            '      printf "%s\\n" "4001 gpt-pr-77-400-1" "6001 gpt-pr-77-600-1"\n'
            "      exit 0 ;;\n"
            "  esac\n"
            "done\n"
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            '    repos/*/check-runs/*) printf "%s\\n" "$a" >> "$CALLS"; exit 0 ;;\n'
            "  esac\n"
            "done\n"
            "exit 0\n",
            encoding="utf-8",
        )
        sleep = tmp_path / "sleep"
        sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        gh.chmod(0o755)
        sleep.chmod(0o755)

        result = subprocess.run(
            [
                bash,
                "-uo",
                "pipefail",
                "-c",
                _step_script(_workflow("fork-gpt-review.yml"), "Finalize check-run (fail closed)"),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_child_env(
                {
                    "PATH": _stub_path(tmp_path),
                    "GH_TOKEN": "stub",
                    "REPO": "kirodotdev/KiroCrew",
                    "HEAD": "a" * 40,
                    "CHECK_ID": "",
                    "PR": "77",
                    "WR_RUN_ID": "500",
                    "WR_RUN_ATTEMPT": "1",
                    "ADJ_DECISION": "",
                    "ADJ_NOTE": "",
                    "CALLS": str(calls),
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_RUN_ID": "999",
                }
            ),
            cwd=tmp_path,
        )

        assert result.returncode == 0, _proc_log(result)
        patched = calls.read_text(encoding="utf-8")
        # The older run's row (400 < 500) is swept; the newer (600 > 500) is not.
        assert "check-runs/4001" in patched, _proc_log(result)
        assert (
            "check-runs/6001" not in patched
        ), "sweep overwrote a NEWER run's row -- the close/reopen regression\n" + _proc_log(result)
        assert "is newer than 500" in (result.stdout + result.stderr), _proc_log(result)


class TestForkReviewersAreStageTwoOfFastGate:
    """The fork reviewers hold `pull-requests: write` and `id-token: write` on a
    PR whose author is untrusted, so they may not be reachable from any
    fork-controlled event: `workflow_run` is the ONLY trigger, and it fires only
    after a workflow on the DEFAULT branch has already run against the head
    commit.

    Which trusted workflow that is, is a cost decision, not a security one. It
    is Fast Gate, the eleven cheap blocking gates split out of CI, which finishes
    in about a minute; CI itself, whose median wall clock is ~54 minutes (73.7%
    of it the backend matrix), tells a code reviewer nothing. The trust boundary
    lives in the steps: harden-runner
    with a blocked egress policy, a checkout of the BASE commit, and the fork's
    diff read as data that is never applied to the tree.
    """

    def _triggers(self, name: str) -> dict:
        spec = yaml.safe_load(_workflow(name))
        # `on` is a YAML 1.1 boolean, so PyYAML keys the trigger block on True.
        on = spec.get("on", spec.get(True))
        assert isinstance(on, dict), name
        return on

    @pytest.mark.parametrize("name", FORK_REVIEW_LANES)
    def test_lane_waits_for_fast_gate_and_nothing_else(self, name: str) -> None:
        on = self._triggers(name)

        # Exactly one trigger, and it is the trusted-vouch one. Any additional
        # entry here (`pull_request_target`, `issue_comment`, `workflow_call`) is
        # a fork-reachable door into a privileged lane.
        assert set(on) == {"workflow_run"}, name
        assert on["workflow_run"]["workflows"] == ["Fast Gate"], name
        assert on["workflow_run"]["types"] == ["completed"], name

    @pytest.mark.parametrize("name", FORK_REVIEW_LANES)
    def test_lane_is_not_reachable_from_a_fork_controlled_event(self, name: str) -> None:
        on = self._triggers(name)
        workflow = _workflow(name)

        for fork_controlled in (
            "pull_request",
            "pull_request_target",
            "issue_comment",
            "pull_request_review",
            "pull_request_review_comment",
        ):
            assert fork_controlled not in on, f"{name}: {fork_controlled}"
        # And the job still only proceeds for a head that is actually a fork, so
        # a same-repo PR cannot double-publish through this lane.
        assert (
            "github.event.workflow_run.head_repository.full_name != github.repository" in workflow
        ), name

    @pytest.mark.parametrize("name", FORK_REVIEW_LANES)
    def test_swapping_the_trusted_gate_did_not_relax_the_trust_boundary(self, name: str) -> None:
        # Fast Gate is cheaper than CI, so the security properties that were
        # never CI's job to provide must be visibly still here.
        workflow = _workflow(name)

        assert "egress-policy: block" in workflow, name
        assert "ref: ${{ steps.pr.outputs.base_sha }}" in workflow, name
        assert "actions/checkout" in workflow, name
        # The head SHA comes from the event payload GitHub sets, never from
        # fork-authored text.
        assert "github.event.workflow_run.head_sha" in workflow, name

    @pytest.mark.parametrize("name", FORK_REVIEW_LANES)
    def test_lane_records_why_it_no_longer_waits_for_ci(self, name: str) -> None:
        # A future reader seeing a security-sensitive lane keyed on a one-minute
        # gate will otherwise "restore" the CI dependency and pay 54 minutes for
        # a precondition that was never load-bearing.
        flat = _flat(_workflow(name))

        assert "Fast Gate, not CI" in flat, name
        assert "median wall clock" in flat, name
        assert "backend matrix" in flat, name
        assert "never a security" in flat, name


class TestProtectedCheckNameHasOnePublisherPerPrType:
    """A required review status must never be satisfied by the OTHER lane's run.

    Each AI review is published by two workflows: the same-repo lane and the
    privileged Stage-2 ``fork-*-review.yml``. GitHub resolves a required status
    check to the NEWEST check-run of that name, and on a fork PR the same-repo
    lane reviews nothing. While the two lanes share a name, any
    ``pull_request`` event firing after the fork lane posted its verdict (a
    reopen; an ``edited`` title/body on codex-review) makes the same-repo
    lane's own run the newest one and clears the gate on a review that never
    ran. The same-repo lane therefore renames itself on a fork PR, leaving
    exactly one publisher of the protected name per PR type.

    That rename only renders because the fork guard sits on every STEP rather
    than on the job: GitHub does not evaluate a skipped job's ``name:``, so a
    job-level guard published the raw expression source as the fork PR's check
    name. The per-step gate is therefore load-bearing for the name AND the only
    thing keeping fork content out of this privileged lane, so it is asserted
    step by step -- a step added later without it would execute on a fork.
    """

    # (same-repo workflow, protected check name, Stage-2 fork workflow)
    PAIRS = (
        ("codex-review.yml", "GPT 6.1 Review", "fork-gpt-review.yml"),
        ("claude-review.yml", "Opus 5.5 Review", "fork-opus-review.yml"),
        ("design-review.yml", "Design Review", "fork-design-review.yml"),
        (
            "first-principles-review.yml",
            "First Principles Review",
            "fork-first-principles-review.yml",
        ),
        ("ux-review.yml", "UX Review", "fork-ux-review.yml"),
        (
            "security-scope-review.yml",
            "Security Scope Review",
            "fork-security-scope-review.yml",
        ),
    )

    GUARD = "github.event.pull_request.head.repo.full_name == github.repository"

    def _job(self, workflow: str) -> dict:
        """Return the job that PUBLISHES the protected check name.

        Counting jobs was a proxy for the property this class owns -- exactly
        one publisher of the protected name per PR type -- and it stops being
        one as soon as a lane needs stages. The scope lane has four jobs
        because the stage holding the Bedrock credential must not also execute
        the reviewed change's classifier code -- neither the differential's nor
        the validator's -- nor hold the write scope that publishes a verdict. So
        select on the thing that makes a job a
        publisher: its `name:` carries the fork guard, which IS the rename that
        keeps a fork PR's required status off this lane. Two such jobs would be
        two publishers, which is the hazard; more non-publishing jobs are not.
        """
        spec = yaml.safe_load(_workflow(workflow))
        publishers = sorted(
            job_id for job_id, job in spec["jobs"].items() if self.GUARD in str(job.get("name", ""))
        )
        assert len(publishers) == 1, (
            f"{workflow}: expected exactly one job publishing the protected "
            f"name, found {publishers}"
        )
        return spec["jobs"][publishers[0]]

    @pytest.mark.parametrize("workflow,check,fork", PAIRS)
    def test_same_repo_lane_keeps_the_protected_name_only_for_same_repo_prs(
        self, workflow: str, check: str, fork: str
    ) -> None:
        job = self._job(workflow)
        name = job["name"]

        # The name is CONDITIONAL on the head repo, not a constant.
        assert self.GUARD in name, workflow
        # Same-repo PRs keep the exact protected name -- branch protection keys
        # its required status check on this string, so it must not drift.
        assert f"&& '{check}'" in name, workflow
        # Fork PRs get a name branch protection does not require, so this
        # lane's `skipped` run can never stand in for the real fork verdict.
        alias = f"{check} (same-repo lane, not applicable to forks)"
        assert f"|| '{alias}'" in name, workflow
        assert alias != check

        # The guard must NOT be job-level: a skipped job's `name:` is never
        # evaluated, so that placement publishes the raw expression above as the
        # fork PR's check name -- the exact rendering bug the rename caused.
        #
        # `always()` is the one exempt expression, and it is exempt because it
        # can never evaluate false: a job carrying exactly that is never
        # skipped, so its `name:` is always evaluated and the bug is
        # unreachable. A staged lane needs it -- the publishing job must report
        # a verdict when an upstream stage failed, which is precisely the run
        # whose verdict matters. Nothing weaker qualifies: any other condition
        # can be false on a fork PR, and then the raw expression is the check
        # name again.
        job_if = str(job.get("if", "")).strip()
        assert job_if in ("", "always()"), (
            f"{workflow}: job-level `if: {job_if}` can evaluate false, so "
            "GitHub skips the job and publishes the raw name expression on "
            "fork PRs"
        )

    @pytest.mark.parametrize("workflow,check,fork", PAIRS)
    def test_every_step_carries_the_fork_guard(self, workflow: str, check: str, fork: str) -> None:
        # With no job-level `if:`, the per-step guard is the ONLY thing keeping
        # fork content out of a lane holding `pull-requests: write` and
        # `id-token: write`. One ungated step is a fork-triggered privileged
        # step, so the invariant is asserted per step rather than per job.
        job = self._job(workflow)
        steps = job["steps"]
        assert steps, workflow
        for index, step in enumerate(steps):
            label = step.get("name") or step.get("uses") or f"step {index}"
            assert self.GUARD in str(step.get("if", "")), f"{workflow}: {label}"

    @pytest.mark.parametrize("workflow,check,fork", PAIRS)
    def test_fork_lane_still_publishes_the_protected_name(
        self, workflow: str, check: str, fork: str
    ) -> None:
        # With the same-repo lane renamed on forks, the Stage-2 lane is the ONLY
        # publisher of the protected name on a fork PR. If it stopped posting
        # under that exact name, every fork PR would block on a status that is
        # never reported.
        assert f'-f name="{check}"' in _workflow(fork), fork

    @pytest.mark.parametrize("workflow,check,fork", PAIRS)
    def test_readiness_still_reads_the_protected_name_on_forks(
        self, workflow: str, check: str, fork: str
    ) -> None:
        # Readiness reads fork verdicts from the head SHA's check-runs by name,
        # bound to THIS PR and attempt via the spec's external_id prefix and
        # triggering-workflow fields. It treats "no completed run bound to this
        # PR+attempt" as pending, so the rename removes a `skipped` row without
        # making a missing review look green.
        readiness = _workflow("pr-readiness.yml")
        assert f'"checkrun:{check}|{check}|' in readiness, check
        assert '[ "$total" -eq 0 ] || [ "$incomplete" -gt 0 ]' in readiness
        assert 'pending+=("$label (not started)")' in readiness

    def test_no_lane_claims_a_skipped_run_satisfies_the_gate(self) -> None:
        # This was true before the Stage-2 fork lanes existed and is exactly the
        # hazard now closed; leaving it recorded as fact invites a revert.
        for workflow, _check, _fork in self.PAIRS:
            flat = _flat(_workflow(workflow))
            assert (
                'check as "skipped", which branch protection treats as satisfied' not in flat
            ), workflow
