"""Review-workflow tests for human overrides: the handler that records and re-runs
them, and the PR-comment reads a verdict gates on, each failing closed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    _bash,
    _child_env,
    _fork_child_env,
    _line_containing,
    _proc_log,
    _script_file,
    _step_script,
    _stub_path,
    _workflow,
    _write_stub,
)
from skill_script_helpers import load_skill_script
from workflow_token_scopes import WORKFLOWS, load_workflow

RELEASE_RECORD_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "scripts" / "release_record.py"
)
release_record = load_skill_script("release_record_under_test", RELEASE_RECORD_PATH)


class TestOverrideHandlerReRunsTheBoundForkLaneRun:
    """Execute the handler's re-run step against a stubbed GitHub API."""

    HEAD = "1234567890abcdef1234567890abcdef12345678"
    TRIGGER = {"id": 900, "run_attempt": 2}

    def _check_run(
        self, cid: int, lane: str, attempt: int, conclusion: str, text: str, pr: int = 7
    ):
        return {
            "id": cid,
            "external_id": f"{lane}-pr-{pr}-900-{attempt}",
            "status": "completed",
            "conclusion": conclusion,
            "details_url": f"https://github.com/example/repo/runs/{cid}",
            "output": {"text": text},
        }

    @staticmethod
    def _marker(run_id: str) -> str:
        return f"Lane run: https://github.com/example/repo/actions/runs/{run_id}\n\n<!-- ai-review-fork-lane run={run_id} -->"

    def _run(
        self,
        tmp_path: Path,
        target: str,
        check_runs: list,
        runs: dict,
        head_repo: str = "fork/repo",
    ):
        bash = _bash()
        if bash is None or os.name == "nt":
            pytest.skip("the handler step is Bash with a stubbed gh; POSIX only")
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        data = tmp_path / "data"
        data.mkdir()
        calls = tmp_path / "calls"
        calls.mkdir()
        (data / "runs.json").write_text(
            json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 900,
                            "run_attempt": 2,
                            "path": ".github/workflows/fast-gate.yml",
                            "head_repository": {"full_name": "fork/repo"},
                            "head_branch": "feature",
                        },
                        # Sibling pull requests' newer Fast Gates on the same head:
                        # another fork, and the same fork's other branch.
                        {
                            "id": 950,
                            "run_attempt": 1,
                            "path": ".github/workflows/fast-gate.yml",
                            "head_repository": {"full_name": "other/repo"},
                            "head_branch": "feature",
                        },
                        {
                            "id": 960,
                            "run_attempt": 1,
                            "path": ".github/workflows/fast-gate.yml",
                            "head_repository": {"full_name": "fork/repo"},
                            "head_branch": "other-branch",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        (data / "checkruns.json").write_text(
            json.dumps({"check_runs": check_runs}), encoding="utf-8"
        )
        for run_id, payload in runs.items():
            (data / f"run-{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
        _write_stub(
            stub_dir / "gh",
            'args="$*"\n'
            f'printf \'%s\\n\' "$args" >> "{calls}/all.txt"\n'
            'case "$args" in\n'
            f'  *"/dispatches"*) printf \'%s\\n\' "$args" >> "{calls}/dispatch.txt"; exit 0 ;;\n'
            f'  *"/issues/"*"/comments"*) cat >> "{calls}/notice.txt"; exit 0 ;;\n'
            f'  *"/rerun"*) printf \'%s\\n\' "$args" >> "{calls}/rerun.txt"; exit 0 ;;\n'
            '  *"/cancel"*)\n'
            '    id="${args##*actions/runs/}"; id="${id%%/*}"\n'
            f'    touch "{calls}/cancelled-$id"; exit 0 ;;\n'
            f'  *"actions/runs?event=pull_request"*) cat "{data}/runs.json"; exit 0 ;;\n'
            f'  *"/check-runs?"*) cat "{data}/checkruns.json"; exit 0 ;;\n'
            '  *"actions/runs/"*)\n'
            '    id="${args##*actions/runs/}"; id="${id%% *}"\n'
            "    filter=.\n"
            '    case "$args" in *"--jq "*) filter="${args##*--jq }" ;; esac\n'
            f'    if [ -f "{data}/run-$id.json" ]; then\n'
            f'      if [ -f "{calls}/cancelled-$id" ]; then\n'
            f'        jq -c \'.status = "completed"\' "{data}/run-$id.json" | jq -r "$filter"\n'
            "      else\n"
            f'        jq -r "$filter" "{data}/run-$id.json"\n'
            "      fi\n"
            "      exit 0\n"
            "    fi\n"
            '    printf \'{"message":"Not Found","status":"404"}\'\n'
            '    echo "gh: Not Found (HTTP 404)" >&2\n'
            "    exit 1 ;;\n"
            "esac\n"
            'echo "unexpected gh call: $args" >&2\n'
            "exit 9\n",
        )
        _write_stub(stub_dir / "sleep", "exit 0\n")
        script = tmp_path / "step.sh"
        script.write_text(
            _step_script(
                _workflow("ai-review-human-override.yml"),
                "Re-run line reviewers with the human decision",
            ),
            encoding="utf-8",
            newline="\n",
        )
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env=_fork_child_env(
                tmp_path,
                stub_dir,
                REPO="example/repo",
                PR="7",
                HEAD=self.HEAD,
                HEAD_REPO=head_repo,
                HEAD_REF="feature",
                TARGET=target,
                IS_FORK="true",
                DEFAULT_BRANCH="main",
            ),
        )

        def read(name: str) -> str:
            f = calls / name
            return f.read_text(encoding="utf-8") if f.exists() else ""

        return result, read

    @staticmethod
    def _lane_run(run_id: int, workflow: str) -> dict:
        return {"id": run_id, "path": f".github/workflows/{workflow}", "status": "completed"}

    def test_it_re_runs_the_run_readiness_binds(self, tmp_path: Path) -> None:
        rows = [
            # An earlier attempt of the same trigger, and a sibling PR's row.
            self._check_run(10, "design", 1, "failure", self._marker("111")),
            self._check_run(11, "design", 2, "failure", self._marker("222")),
            self._check_run(12, "design", 2, "failure", self._marker("333"), pr=8),
        ]
        result, read = self._run(
            tmp_path, "design", rows, {"222": self._lane_run(222, "fork-design-review.yml")}
        )
        assert result.returncode == 0, _proc_log(result)
        assert "actions/runs/222/rerun" in read("rerun.txt"), _proc_log(result)
        assert "111" not in read("rerun.txt") and "333" not in read("rerun.txt")
        assert read("notice.txt") == ""
        assert read("dispatch.txt") == "", "a lane re-run reaches readiness through its sweep"

    def test_target_all_re_runs_only_the_red_lanes(self, tmp_path: Path) -> None:
        rows = [
            self._check_run(20, "design", 2, "failure", self._marker("401")),
            self._check_run(21, "ux", 2, "success", self._marker("402")),
            self._check_run(22, "first-principles", 2, "neutral", self._marker("403")),
            self._check_run(23, "scope", 2, "failure", self._marker("404")),
        ]
        runs = {
            "401": self._lane_run(401, "fork-design-review.yml"),
            "402": self._lane_run(402, "fork-ux-review.yml"),
            "403": self._lane_run(403, "fork-first-principles-review.yml"),
            "404": self._lane_run(404, "fork-security-scope-review.yml"),
        }
        result, read = self._run(tmp_path, "all", rows, runs)
        assert result.returncode == 0, _proc_log(result)
        reruns = read("rerun.txt")
        assert "runs/401/rerun" in reruns and "runs/404/rerun" in reruns, _proc_log(result)
        assert "402" not in reruns and "403" not in reruns, reruns
        # Opus and GPT have posted no row at this attempt: nothing to re-run,
        # and not a failure either.
        assert read("notice.txt") == "", read("notice.txt")

    @pytest.mark.parametrize(
        "text",
        [
            "no marker at all",
            "<!-- ai-review-fork-lane run=55a -->",
            "<!-- ai-review-fork-lane run= -->",
        ],
    )
    def test_a_row_without_a_digits_only_run_id_is_not_re_run(self, tmp_path: Path, text: str):
        rows = [self._check_run(30, "design", 2, "failure", text)]
        result, read = self._run(tmp_path, "design", rows, {})
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "carries no lane-run id" in result.stdout, _proc_log(result)
        assert "Design Review" in read("notice.txt")
        # Nothing was re-run, so readiness is dispatched to read the record now.
        dispatch = read("dispatch.txt")
        assert "pr-readiness.yml/dispatches" in dispatch, _proc_log(result)
        assert "inputs[pr]=7" in dispatch and f"inputs[sha]={self.HEAD}" in dispatch
        assert "ref=main" in dispatch

    def test_a_failed_run_read_is_reported_as_a_read_failure(self, tmp_path: Path) -> None:
        # gh prints the 404 JSON body on stdout. Swallowing the exit status read
        # that body's `.path` as empty and reported "points at ''".
        rows = [self._check_run(40, "design", 2, "failure", self._marker("777"))]
        result, read = self._run(tmp_path, "design", rows, {})
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "reading fork-design-review.yml run 777 failed" in result.stdout, _proc_log(result)
        assert "HTTP 404" in result.stdout
        assert "points at" not in result.stdout

    def test_a_run_of_another_workflow_is_refused(self, tmp_path: Path) -> None:
        rows = [self._check_run(50, "design", 2, "failure", self._marker("888"))]
        result, read = self._run(
            tmp_path, "design", rows, {"888": self._lane_run(888, "some-other.yml")}
        )
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "refusing to re-run it" in result.stdout, _proc_log(result)

    def test_a_passing_lane_is_left_alone_and_readiness_recomputes(self, tmp_path: Path) -> None:
        rows = [self._check_run(60, "design", 2, "success", self._marker("999"))]
        result, read = self._run(
            tmp_path, "design", rows, {"999": self._lane_run(999, "fork-design-review.yml")}
        )
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "already passes" in result.stdout
        assert "pr-readiness.yml/dispatches" in read("dispatch.txt")

    def test_the_newest_row_for_the_bound_id_wins(self, tmp_path: Path) -> None:
        # A re-run on an unchanged head keeps the trigger-bound id, so the stale
        # attempt's row and the fresh one share it; readiness reads the newest by
        # check-run id, and so does the handler.
        rows = [
            self._check_run(71, "design", 2, "failure", self._marker("111")),
            self._check_run(75, "design", 2, "failure", self._marker("222")),
            self._check_run(73, "design", 2, "failure", self._marker("333")),
        ]
        runs = {r: self._lane_run(int(r), "fork-design-review.yml") for r in ("111", "222", "333")}
        result, read = self._run(tmp_path, "design", rows, runs)
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt").strip().endswith("actions/runs/222/rerun"), read("rerun.txt")
        assert "111" not in read("rerun.txt") and "333" not in read("rerun.txt")

    def test_a_running_lane_is_cancelled_then_re_run(self, tmp_path: Path) -> None:
        row = self._check_run(80, "design", 2, "failure", self._marker("444"))
        row.update(status="in_progress", conclusion=None)
        run = self._lane_run(444, "fork-design-review.yml")
        run["status"] = "in_progress"
        result, read = self._run(tmp_path, "design", [row], {"444": run})
        assert result.returncode == 0, _proc_log(result)
        assert "actions/runs/444/cancel" in read("all.txt"), _proc_log(result)
        assert "actions/runs/444/rerun" in read("rerun.txt"), _proc_log(result)

    def test_a_deleted_fork_binds_no_run(self, tmp_path: Path) -> None:
        # Readiness binds no run to a head repository that is gone; neither may
        # the handler, by matching an empty name against a null one.
        rows = [self._check_run(90, "design", 2, "failure", self._marker("555"))]
        result, read = self._run(
            tmp_path,
            "design",
            rows,
            {"555": self._lane_run(555, "fork-design-review.yml")},
            head_repo="",
        )
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "head repository or branch is gone" in result.stdout, _proc_log(result)
        assert "pr-readiness.yml/dispatches" in read("dispatch.txt")


class TestHumanOverrideHandler:
    def test_handler_runs_from_trusted_issue_comment_context(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")

        assert "issue_comment:" in workflow
        assert "pull_request_target:" not in workflow
        assert "actions/checkout@" not in workflow
        assert (
            "/ai-review override <fable|gpt|design|ux|first-principles|scope|all> "
            "<current-sha> <class>[ #N]: <reason>" in workflow
        )

    def test_handler_covers_the_design_family_lanes(self) -> None:
        # Promoting UX / First Principles to blocking is only safe if a false
        # BLOCK has a human escape hatch. The override handler must accept the
        # design-family targets and re-run those lanes -- the re-run's
        # human-override step then skips the model and the gate passes.
        workflow = _workflow("ai-review-human-override.yml")
        # The command reader owns the lane list; the handler re-runs each one.
        assert set(release_record.LANES) == {
            "fable",
            "gpt",
            "design",
            "ux",
            "first-principles",
            "scope",
            "all",
        }
        assert 'rerun_reviewer "design-review.yml"' in workflow
        assert 'rerun_reviewer "ux-review.yml"' in workflow
        assert 'rerun_reviewer "first-principles-review.yml"' in workflow
        # Security Scope Review is blocking too, so it needs the same escape
        # hatch. On a same-repo PR the re-run's own override step skips the whole
        # review -- model call, candidate validation and differential alike -- and
        # the gate passes on the marker, so a script-confirmed regression clears
        # here just as a model-side BLOCK does. The fork Stage-2 lane consumes the
        # record the same way, ahead of its per-head floor.
        assert 'rerun_reviewer "security-scope-review.yml"' in workflow

    def test_rerun_resolves_fork_lane_runs_from_the_bound_check_run(self) -> None:
        # A fork PR's reviewers are the workflow_run-triggered Stage-2 lanes.
        # Their run objects are keyed to the DEFAULT branch context (head_sha
        # is main's tip, pull_requests is empty), so the same-repo lookup by
        # PR head can never find them. The re-run step reads the row PR
        # Readiness binds instead -- `<lane>-pr-<PR>-<Fast Gate run>-<attempt>`,
        # newest by check-run id -- and takes the lane's run id from the marker
        # the lane writes into that row's output.text. Never from details_url,
        # which GitHub stores as the check-run's own page.
        workflow = _workflow("ai-review-human-override.yml")
        script = _step_script(workflow, "Re-run line reviewers with the human decision")

        assert 'if [ "$IS_FORK" = "true" ]; then' in script
        assert ".details_url" not in script
        assert '--arg path ".github/workflows/fast-gate.yml"' in script
        assert '"\\(.id)-\\(.run_attempt // 1)"' in script
        assert 'want="$lane-pr-$PR-$fork_trigger"' in script
        assert "select(.external_id == $x)] | max_by(.id) // empty" in " ".join(script.split())
        # The default filter=latest returns one check-run per name, which can be
        # a sibling pull request's row on a shared head.
        assert "check-runs?check_name=$enc&per_page=100&filter=all" in script
        assert 'capture("<!-- ai-review-fork-lane run=(?<id>[0-9]+) -->")' in script
        # Digits only, whatever the capture produced.
        assert '[[ ! "$run_id" =~ ^[0-9]+$ ]]' in script
        # The resolved run must be verified to belong to the expected fork
        # lane before anything is re-run: any workflow with checks:write
        # could post a check-run of the same name.
        assert '[ "$run_path" != ".github/workflows/$fork_workflow" ]' in script
        for fork_lane in (
            "fork-opus-review.yml",
            "fork-gpt-review.yml",
            "fork-design-review.yml",
            "fork-ux-review.yml",
            "fork-first-principles-review.yml",
            "fork-security-scope-review.yml",
        ):
            assert f'"{fork_lane}"' in script

    def test_rerun_reads_keep_their_exit_status(self) -> None:
        # `gh` prints the API's JSON error body on STDOUT on an HTTP error, so a
        # read with its status swallowed hands that body on as the answer and the
        # failure is reported as whatever it parses to ("points at ''"). Judge
        # code lines only: the step's comments quote the shapes they replaced.
        script = _step_script(
            _workflow("ai-review-human-override.yml"),
            "Re-run line reviewers with the human decision",
        )
        code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
        assert not any("|| true" in ln for ln in code), [ln for ln in code if "|| true" in ln]
        assert not any("2>/dev/null" in ln for ln in code), [
            ln for ln in code if "2>/dev/null" in ln
        ]

    def test_rerun_failure_is_a_warning_once_the_judgment_recorded(self) -> None:
        # The judgment records in the step BEFORE the rerun. A rerun-lookup
        # failure after that must not red the run -- a red X there is
        # indistinguishable from a rejected override -- but it must stay
        # visible: a warning annotation plus a PR notice naming the lanes to
        # re-run manually.
        workflow = _workflow("ai-review-human-override.yml")
        script = _step_script(workflow, "Re-run line reviewers with the human decision")

        assert "::error::" not in script
        assert "::warning::" in script
        assert 'if [ -n "$failed_lanes" ]; then' in script
        assert "post_notice" in script
        assert "could not be re-run automatically" in script

    def test_fork_lanes_write_their_run_id_into_every_check_run_write(self) -> None:
        # The only link from a PR head back to the workflow_run-keyed lane run
        # is the lane-run marker in its check-run's output.text; the override
        # handler's fork re-run path reads it back. GitHub stores an
        # Actions-created check-run's details_url as the check-run's own page,
        # so that field cannot carry it. EVERY write of the row carries the
        # marker -- the opening POST, each completing PATCH and the finalize
        # fallback POST -- because whichever write landed last is the row the
        # handler reads. The fallback must also carry the external_id the
        # handler and readiness bind on, or the one row holding the marker is
        # never a candidate.
        marker = "<!-- ai-review-fork-lane run=%s -->"
        args = '"${GITHUB_SERVER_URL:-}" "$REPO" "${GITHUB_RUN_ID:-}" "${GITHUB_RUN_ID:-}")"'
        # `posts` is how many check-run POSTs the lane makes, and it is a
        # PERMISSION fact, not a style choice. The five lanes below open a
        # check-run early and re-POST a finalize fallback. The scope lane POSTs
        # exactly once because `checks: write` is held only by its publishing
        # job -- the one that executes nothing -- and the job that would open a
        # check-run early is the one running the fork's own classifier code.
        for name, lane, posts, patches in (
            ("fork-opus-review.yml", "opus", 2, 1),
            ("fork-gpt-review.yml", "gpt", 2, 1),
            ("fork-design-review.yml", "design", 2, 1),
            ("fork-ux-review.yml", "ux", 2, 1),
            ("fork-first-principles-review.yml", "first-principles", 2, 1),
            ("fork-security-scope-review.yml", "scope", 1, 0),
        ):
            workflow = _workflow(name)
            assert (
                workflow.count('gh api --method POST "repos/$REPO/check-runs"') == posts
            ), f"{name}: expected {posts} check-run POST(s)"
            assert workflow.count(marker) == posts, name
            assert workflow.count(args) == posts, name
            # Every POST and every PATCH names the marker text in its output.
            writes = workflow.count(
                'gh api --method POST "repos/$REPO/check-runs"'
            ) + workflow.count('gh api --method PATCH "repos/$REPO/check-runs/$1"')
            assert writes == posts + patches, name
            assert workflow.count("output[text]=") == writes, name
            assert (
                f'ext_args=(-f external_id="{lane}-pr-$PR-$WR_RUN_ID-$WR_RUN_ATTEMPT")' in workflow
            ), name
            assert "WR_RUN_ID: ${{ github.event.workflow_run.id }}" in workflow, name
            assert "WR_RUN_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}" in workflow, name

    def test_handler_requires_write_permission_fresh_sha_and_reason(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")

        assert 'if [ "$ACTOR" = "$author" ]; then' not in workflow
        assert "collaborators/$ACTOR/permission" in workflow
        assert "admin|maintain|write) allowed=true" in workflow
        assert 'if [[ "$head" != "$requested_sha"* ]]; then' in workflow
        # The reason bounds (20 to 500 characters) are release_record.py's, tested in
        # test_release_record.py; the handler must read them from there.
        assert 'python3 "$RELEASE_RECORD" parse' in workflow
        assert "only a repository writer" in workflow

    def test_permission_read_no_longer_swallows_its_exit_status(self) -> None:
        # The authority read that decides whether the actor may override must
        # not treat "the API did not answer" as "the actor is not a writer".
        # `2>/dev/null || true` made those two the same empty string.
        script = _step_script(
            _workflow("ai-review-human-override.yml"), "Validate and record the decision"
        )
        permission_read = _line_containing(script, "collaborators/$ACTOR/permission")

        assert "2>/dev/null" not in permission_read
        assert "|| true" not in permission_read
        # An explicit 404 stays a legitimate negative, so it must be matched
        # by name rather than folded into the unknown-failure arm.
        assert "HTTP 404|Not Found" in script
        # Fail-closed on an unknown read must stay BOUNDED: a permanently
        # failing API cannot be allowed to hold this job open.
        assert "for attempt in 1 2 3; do" in script

    def _override_step(self) -> str:
        return _step_script(
            _workflow("ai-review-human-override.yml"), "Validate and record the decision"
        )

    def test_the_step_parses_under_bash_3_2(self) -> None:
        # macOS runs these step tests under /bin/bash 3.2, which has no
        # `${x,,}` / `${x^^}` case expansion ("bad substitution"). Linux CI's
        # bash 5 accepts it, so only this pin catches it before macOS does.
        code = [
            line for line in self._override_step().splitlines() if not line.lstrip().startswith("#")
        ]
        offending = [line for line in code if ",,}" in line or "^^}" in line]
        assert offending == []

    @pytest.mark.parametrize(
        ("perm_mode", "want_rc", "want_notice", "want_error_annotation"),
        [
            # The read answered "write": the override is recorded.
            ("write", 0, "Human judgment recorded", False),
            # The read answered 404 -- the API saying "not a collaborator".
            # A real denial: same refusal wording as before, and NOT an
            # infrastructure error, so no ::error:: annotation.
            ("notfound", 1, "only a repository writer may override", False),
            # The read never answered. Also denies -- an unreadable permission
            # is not authorization -- but it must be DISTINGUISHABLE from the
            # 404 above, or an operator reads a GitHub outage as having lost
            # write access to the repository.
            ("transient", 1, "could not be READ", True),
        ],
    )
    def test_unreadable_permission_denies_but_says_so(
        self,
        perm_mode: str,
        want_rc: int,
        want_notice: str,
        want_error_annotation: bool,
        tmp_path: Path,
    ) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the handler step is Bash; skip where Bash is absent")
        if shutil.which("jq") is None:
            pytest.skip("the handler step shells out to jq")

        head = "a" * 40
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        notices = tmp_path / "notices.json"
        notices.touch()
        # Stub only the two calls this step makes, so a third call is a loud
        # failure rather than a silent pass.
        _write_stub(
            bin_dir / "gh",
            'if [ "${1:-}" = "api" ] && [ "${2:-}" = "--method" ]; then\n'
            f'  cat >> "{notices}"\n'
            "  exit 0\n"
            "fi\n"
            'case "${2:-}" in\n'
            f'  */pulls/*) printf \'{{"head":{{"sha":"{head}","repo":{{"full_name":"o/r"}}}}}}\'; exit 0 ;;\n'
            "  */permission)\n"
            '    case "$PERM_MODE" in\n'
            "      write) printf 'write\\n'; exit 0 ;;\n"
            '      notfound) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;\n'
            '      transient) echo "gh: Internal Server Error (HTTP 500)" >&2; exit 1 ;;\n'
            "    esac ;;\n"
            "esac\n"
            'echo "unexpected gh call: $*" >&2\n'
            "exit 9\n",
        )
        # Keep the bounded backoff from costing this test its own wall clock.
        _write_stub(bin_dir / "sleep", "exit 0\n")

        out_file = tmp_path / "gh-output"
        out_file.touch()
        proc = subprocess.run(
            [bash, "-e", _script_file(tmp_path, self._override_step())],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_child_env(
                {
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PERM_MODE": perm_mode,
                    "GH_TOKEN": "stub",
                    "REPO": "o/r",
                    "PR": "1",
                    "ACTOR": "someone",
                    "COMMENT_AUTHOR": "someone",
                    "COMMENT_ID": "42",
                    "RELEASE_RECORD": str(RELEASE_RECORD_PATH),
                    "COMMENT_BODY": f"/ai-review override gpt {head} false-positive: a stated reason that is long enough",
                    "GITHUB_OUTPUT": str(out_file),
                }
            ),
            cwd=tmp_path,
        )

        assert proc.returncode == want_rc, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert want_notice in notices.read_text(encoding="utf-8"), notices.read_text(
            encoding="utf-8"
        )
        assert ("::error::" in proc.stdout) is want_error_annotation, proc.stdout
        if perm_mode == "write":
            assert "actor=someone" in out_file.read_text(encoding="utf-8")

    def test_handler_records_a_bot_marker_before_changing_checks(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")
        marker = (
            "<!-- ai-review-human-override target=$target head=$head "
            "actor=$ACTOR source=$COMMENT_ID -->"
        )

        assert marker in workflow
        assert workflow.index(marker) < workflow.index("actions/runs/$run_id/rerun")
        assert "select(.head_sha == $head" in workflow

    def test_reviewer_comments_advertise_the_writer_only_policy(self) -> None:
        for name in ("claude-review.yml", "codex-review.yml"):
            workflow = _workflow(name)
            assert "The PR author or a repository writer" not in workflow
            assert "A repository writer can comment:" in workflow


class TestLineReviewHumanOverrides:
    def test_fable_consumes_only_a_bot_authored_sha_scoped_record(self) -> None:
        workflow = _workflow("claude-review.yml")

        assert "target=fable head=$HEAD" in workflow
        assert '.user.login == "github-actions[bot]"' in workflow
        assert "steps.human_override.outputs.active != 'true'" in workflow
        assert "✅ human override accepted" in workflow
        assert "Human judgment by $OVERRIDE_ACTOR overrides Opus 5.5" in workflow
        assert "/ai-review override fable $HEAD false-positive:" in workflow

    @pytest.mark.parametrize(
        "name,target,lane",
        [
            ("design-review.yml", "design", "Design Review"),
            ("ux-review.yml", "ux", "UX Review"),
            ("first-principles-review.yml", "first-principles", "First Principles Review"),
        ],
    )
    def test_design_family_consumes_a_bot_authored_sha_scoped_record(
        self, name, target, lane
    ) -> None:
        # The newly-blocking lanes mirror the fable/gpt override contract: a
        # bot-authored, SHA-scoped record skips the model review and passes the
        # gate, so a false BLOCK is clearable without a code change.
        workflow = _workflow(name)
        assert f"target={target} head=$HEAD" in workflow
        assert '.user.login == "github-actions[bot]"' in workflow
        assert "steps.human_override.outputs.active != 'true'" in workflow
        assert "✅ human override accepted" in workflow
        assert f"overrides {lane} for $HEAD. Passing gate." in workflow
        # The resolver MUST run before the OIDC/credentials step, and that step
        # must itself be gated on the override -- otherwise an OIDC failure
        # skips the resolver and the override can never clear an infra-failed
        # lane (regression guard for the round-2 ordering finding).
        assert workflow.index("name: Resolve human override") < workflow.index(
            "uses: aws-actions/configure-aws-credentials"
        )
        creds_if = workflow.split("uses: aws-actions/configure-aws-credentials")[1].split("with:")[
            0
        ]
        assert "steps.human_override.outputs.active != 'true'" in creds_if

    def test_gpt_has_clear_verdict_banner_and_human_override(self) -> None:
        workflow = _workflow("codex-review.yml")

        assert "target=gpt head=$HEAD" in workflow
        assert '.user.login == "github-actions[bot]"' in workflow
        assert "steps.human_override.outputs.active != 'true'" in workflow
        assert 'verdict="✅ no blocking findings"' in workflow
        assert (
            "GPT 6.1 completed its review of \\`$HEAD\\` and found no blocking issues." in workflow
        )
        assert "✅ human override accepted" in workflow
        assert "Human judgment by $OVERRIDE_ACTOR overrides GPT 6.1" in workflow
        assert "/ai-review override gpt $HEAD false-positive:" in workflow


OVERRIDE_READ_LANES = (
    "ux-review.yml",
    "design-review.yml",
    "claude-review.yml",
    "codex-review.yml",
    "first-principles-review.yml",
    "security-scope-review.yml",
)


# Lanes whose override read stops at the PRIMARY installation rate limit
# instead of retrying it.
PRIMARY_LIMIT_LANES = ("codex-review.yml",)


class TestOverrideReadFailureFailsClosed:
    """Execute the ACTUAL override-record read from each lane with ``gh`` stubbed.

    ``2>/dev/null || true`` collapses a failed comments read onto the
    same empty string as "no override recorded", so a transient API failure
    re-gates a verdict a human has already cleared with ``/ai-review
    override``. These cases pin the three outcomes apart: a read that succeeds
    resolves the recorded override, a transient failure is absorbed by the
    bounded retry, and a read that never succeeds fails the step closed while
    naming the read as the cause.
    """

    HEAD = "0" * 40

    def _read_block(self, lane: str) -> str:
        script = _step_script(_workflow(lane), "Resolve human override")
        start = script.index('exact="')
        end = script.index('actor="')
        return script[start:end]

    def _run_read(
        self,
        tmp_path: Path,
        lane: str,
        gh_status: int = 0,
        fail_first: int = 0,
        gh_error: str = "gh: could not reach the API",
    ):
        bash = _bash()
        if bash is None:
            pytest.skip("the read block is Bash; skip where Bash is absent")
        body = (
            f"<!-- ai-review-human-override target=all head={self.HEAD} "
            "actor=alice source=42 -->"
        )
        reply = tmp_path / "api-reply.json"
        reply.write_text(
            '[{"user":{"login":"github-actions[bot]"},"body":' + f'"{body}"' + "}]",
            encoding="utf-8",
        )
        attempts = tmp_path / "gh-attempts"
        gh = tmp_path / "gh"
        stub = f'#!/bin/sh\nprintf x >> "{attempts}"\n'
        if gh_status:
            # Stand in for an API failure on every attempt (5xx, rate limit).
            stub += f'echo "{gh_error}" >&2\nexit {gh_status}\n'
        elif fail_first:
            stub += (
                f'if [ "$(wc -c < "{attempts}")" -le {fail_first} ]; then\n'
                '  echo "gh: HTTP 502" >&2\n'
                "  exit 1\n"
                "fi\n"
                f'cat "{reply}"\n'
            )
        else:
            stub += f'cat "{reply}"\n'
        gh.write_text(stub, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        # The retry backoff is real in CI but pure latency in a test; stub it
        # to a no-op so the failure cases do not sleep through the suite.
        sleep_stub = tmp_path / "sleep"
        sleep_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        sleep_stub.chmod(0o755)
        out_file = tmp_path / "record-out.txt"
        # Reproduce the runner's own prologue (`bash -e`, no pipefail -- these
        # steps declare no `set` line), then persist `$record` so the assertion
        # reads what the rest of the step would have been handed.
        script = self._read_block(lane) + f'\nprintf \'%s\' "$record" > "{out_file}"\n'
        result = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                # tmp_path first so the `gh` stub wins; starve any real gh of
                # credentials so a stub-resolution failure can never turn into
                # a live API call. GH_CONFIG_DIR keeps a fallback gh from
                # loading the user's persisted authentication.
                "PATH": _stub_path(tmp_path),
                "GH_TOKEN": "",
                "GITHUB_TOKEN": "",
                "GH_CONFIG_DIR": str(tmp_path),
                "LC_ALL": "C",
                "REPO": "example/repo",
                "PR": "1",
                "HEAD": self.HEAD,
                "TMPDIR": str(tmp_path),
            },
            cwd=tmp_path,
        )
        return result, attempts, out_file, body

    @pytest.mark.parametrize("lane", OVERRIDE_READ_LANES)
    def test_successful_read_resolves_the_recorded_override(self, lane: str, tmp_path: Path):
        result, attempts, out_file, body = self._run_read(tmp_path, lane)
        assert result.returncode == 0, result.stdout + result.stderr
        assert out_file.read_text(encoding="utf-8") == body
        assert attempts.read_text(encoding="utf-8") == "x", "retry fired on a good read"

    @pytest.mark.parametrize("lane", OVERRIDE_READ_LANES)
    def test_transient_read_failure_is_absorbed(self, lane: str, tmp_path: Path):
        result, attempts, out_file, body = self._run_read(tmp_path, lane, fail_first=1)
        assert result.returncode == 0, result.stdout + result.stderr
        assert out_file.read_text(encoding="utf-8") == body
        assert attempts.read_text(encoding="utf-8") == "xx"

    @pytest.mark.parametrize("lane", OVERRIDE_READ_LANES)
    def test_persistent_read_failure_fails_closed(self, lane: str, tmp_path: Path):
        result, attempts, out_file, _ = self._run_read(tmp_path, lane, gh_status=1)
        assert result.returncode != 0, "a read that never succeeded passed the step"
        assert "::error::" in result.stdout
        assert "re-run this job" in result.stdout
        assert attempts.read_text(encoding="utf-8") == "xxx"
        assert not out_file.exists(), "a record was emitted from a failed read"

    @pytest.mark.parametrize("lane", PRIMARY_LIMIT_LANES)
    def test_primary_rate_limit_is_not_retried(self, lane: str, tmp_path: Path):
        # The primary installation limit resets hourly: a retry cannot
        # succeed and spends shared quota, so one attempt is the whole budget.
        result, attempts, out_file, _ = self._run_read(
            tmp_path,
            lane,
            gh_status=1,
            gh_error="gh: API rate limit exceeded for installation ID 1. (HTTP 403)",
        )
        assert result.returncode != 0, "a read that never succeeded passed the step"
        assert "PRIMARY rate limit" in result.stdout
        assert "after the hourly reset" in result.stdout
        assert attempts.read_text(encoding="utf-8") == "x"
        assert not out_file.exists(), "a record was emitted from a failed read"

    @pytest.mark.parametrize("lane", OVERRIDE_READ_LANES)
    def test_no_lane_still_swallows_the_override_read(self, lane: str):
        # The defect shape itself must not return: within the resolve block the
        # comments read carries no stderr/exit-status suppression. Judge only
        # code lines -- the block's own comment QUOTES the banned shape while
        # explaining why it is gone.
        block = "\n".join(
            line
            for line in self._read_block(lane).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert 'issues/$PR/comments" --paginate 2>/dev/null' not in block
        assert "|| true" not in block


class TestLedgerReadFailureFailsClosed:
    """Execute the ACTUAL round-convergence ledger read with ``gh`` stubbed.

    The old fail-soft (``|| printf '[]'``) collapsed a failed comments read
    onto "no prior rulings", so a transient API failure re-litigated findings
    a writer had already disposed.
    """

    def _read_block(self) -> str:
        script = _step_script(_workflow("codex-review.yml"), "Write review prompt")
        start = script.index('ledger_comments=""')
        end = script.index('disp_authors="')
        return script[start:end]

    def _run_read(self, tmp_path: Path, gh_status: int = 0, fail_first: int = 0):
        bash = _bash()
        if bash is None:
            pytest.skip("the read block is Bash; skip where Bash is absent")
        attempts = tmp_path / "gh-attempts"
        gh = tmp_path / "gh"
        stub = f'#!/bin/sh\nprintf x >> "{attempts}"\n'
        if gh_status:
            stub += f'echo "gh: could not reach the API" >&2\nexit {gh_status}\n'
        elif fail_first:
            stub += (
                f'if [ "$(wc -c < "{attempts}")" -le {fail_first} ]; then\n'
                '  echo "gh: HTTP 502" >&2\n'
                "  exit 1\n"
                "fi\n"
                "printf '%s' '[{\"a\":1}][{\"b\":2}]'\n"
            )
        else:
            # --paginate concatenates one JSON array per page; emit two pages
            # so the assertion proves the pages are merged, not just echoed.
            stub += "printf '%s' '[{\"a\":1}][{\"b\":2}]'\n"
        gh.write_text(stub, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        sleep_stub = tmp_path / "sleep"
        sleep_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        sleep_stub.chmod(0o755)
        out_file = tmp_path / "ledger-out.json"
        script = self._read_block() + f'\nprintf \'%s\' "$comments_json" > "{out_file}"\n'
        result = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                "PATH": _stub_path(tmp_path),
                "GH_TOKEN": "",
                "GITHUB_TOKEN": "",
                "GH_CONFIG_DIR": str(tmp_path),
                "LC_ALL": "C",
                "REPO": "example/repo",
                "PR": "1",
                "TMPDIR": str(tmp_path),
            },
            cwd=tmp_path,
        )
        return result, attempts, out_file

    def test_successful_read_merges_the_pages(self, tmp_path: Path):
        result, attempts, out_file = self._run_read(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(out_file.read_text(encoding="utf-8")) == [
            {"a": 1},
            {"b": 2},
        ]
        assert attempts.read_text(encoding="utf-8") == "x"

    def test_transient_read_failure_is_absorbed(self, tmp_path: Path):
        result, attempts, out_file = self._run_read(tmp_path, fail_first=1)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(out_file.read_text(encoding="utf-8")) == [
            {"a": 1},
            {"b": 2},
        ]
        assert attempts.read_text(encoding="utf-8") == "xx"

    def test_persistent_read_failure_fails_closed(self, tmp_path: Path):
        result, attempts, out_file = self._run_read(tmp_path, gh_status=1)
        assert result.returncode != 0, "a read that never succeeded passed the step"
        assert "::error::" in result.stdout
        assert attempts.read_text(encoding="utf-8") == "xxx"
        assert not out_file.exists(), "a ledger was emitted from a failed read"

    def test_ledger_read_no_longer_swallows_its_status(self):
        # The defect shape itself must not return. Judge only code lines --
        # the block's own comment QUOTES the banned shape while explaining
        # why it is gone.
        block = "\n".join(
            line for line in self._read_block().splitlines() if not line.lstrip().startswith("#")
        )
        assert 'issues/$PR/comments" --paginate 2>/dev/null' not in block
        assert "|| true" not in block
        assert "|| printf" not in block


class TestLedgerWriterGateFailsClosed:
    """Execute the ACTUAL ledger writer-gating permission read with ``gh`` stubbed.

    An empty ``perm`` matches no ``case`` arm, so a permission read that FAILED
    resolves its author to non-writer and drops that writer's disposition
    records from the ledger -- the reviewer then re-litigates findings a
    repository writer already ruled on, on a green run with no annotation.
    """

    DISPOSITION = (
        '[{"body":"<!-- ai-review-disposition target=gpt head=abc -->",'
        '"user":{"login":"someone"}}]'
    )

    def _writer_gate_block(self) -> str:
        script = _step_script(_workflow("codex-review.yml"), "Write review prompt")
        start = script.index('disp_authors="')
        end = script.index('ledger_full="')
        return script[start:end]

    def _run_gate(self, tmp_path: Path, perm_mode: str):
        bash = _bash()
        if bash is None:
            pytest.skip("the writer gate is Bash; skip where Bash is absent")
        attempts = tmp_path / "gh-attempts"
        gh = tmp_path / "gh"
        stub = f'#!/bin/sh\nprintf x >> "{attempts}"\n'
        if perm_mode == "write":
            stub += "printf 'write\\n'\n"
        elif perm_mode == "notfound":
            stub += 'echo "gh: Not Found (HTTP 404)" >&2\nexit 1\n'
        else:
            stub += 'echo "gh: Internal Server Error (HTTP 500)" >&2\nexit 1\n'
        gh.write_text(stub, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        sleep_stub = tmp_path / "sleep"
        sleep_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        sleep_stub.chmod(0o755)
        out_file = tmp_path / "writers.json"
        script = (
            f"comments_json='{self.DISPOSITION}'\n"
            + self._writer_gate_block()
            + f'\nprintf \'%s\' "$writers" > "{out_file}"\n'
        )
        result = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                "PATH": _stub_path(tmp_path),
                "GH_TOKEN": "",
                "GITHUB_TOKEN": "",
                "GH_CONFIG_DIR": str(tmp_path),
                "LC_ALL": "C",
                "REPO": "example/repo",
                "PR": "1",
                "TMPDIR": str(tmp_path),
            },
            cwd=tmp_path,
        )
        return result, attempts, out_file

    def test_a_readable_writer_is_gated_in(self, tmp_path: Path):
        result, attempts, out_file = self._run_gate(tmp_path, "write")
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(out_file.read_text(encoding="utf-8")) == ["someone"]
        assert attempts.read_text(encoding="utf-8") == "x"

    def test_a_404_stays_a_legitimate_non_writer(self, tmp_path: Path):
        # The API answering "not a collaborator" is a real negative: exclude the
        # author, without a retry and without failing the step.
        result, attempts, out_file = self._run_gate(tmp_path, "notfound")
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(out_file.read_text(encoding="utf-8")) == []
        assert attempts.read_text(encoding="utf-8") == "x"

    def test_an_unreadable_permission_fails_closed(self, tmp_path: Path):
        result, attempts, out_file = self._run_gate(tmp_path, "transient")
        assert result.returncode != 0, "an unreadable permission passed as non-writer"
        assert "::error::" in result.stdout
        # Bounded: a permanently failing API must not hold the job open.
        assert attempts.read_text(encoding="utf-8") == "xxx"
        assert not out_file.exists(), "the ledger was gated on a permission never read"

    def test_permission_read_no_longer_swallows_its_status(self):
        block = "\n".join(
            line
            for line in self._writer_gate_block().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "collaborators/$author/permission" in block
        # The read spans a continuation line and its redirection sits on the
        # SECOND one, so judge THAT line: a check scoped to the line naming the
        # endpoint passes while the exit status is still swallowed.
        redirection = _line_containing(block, "--jq '.permission'")
        assert "2>/dev/null" not in redirection
        assert "|| true" not in redirection
        # An explicit 404 stays a legitimate negative, so it must be matched by
        # name rather than folded into the unknown-failure arm.
        assert "HTTP 404|Not Found" in block
        assert "for attempt in 1 2 3; do" in block


_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "_review_contract.py"
)
review_contract = load_skill_script("review_contract_under_override_test", _CONTRACT_PATH)

_GOOD_ISSUE = {
    "state": "open",
    "labels": [{"name": "deferred-finding"}],
    "assignees": [{"login": "owner"}],
    "body": "Due: 2031-01-01",
    "milestone": None,
}
_LONG_REASON = "the finding does not apply to this change"


class TestOverrideHandlerRecordsAClass:
    """Execute the real "Validate and record" step against a stubbed GitHub API."""

    HEAD = "a" * 40

    def _run(
        self,
        tmp_path: Path,
        body: str,
        *,
        actor: str = "writer",
        pr_author: str = "author",
        comment_author: str | None = None,
        permission: str = "write",
        issue: dict | str | None = None,
        today: str = "2026-11-01",
    ):
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("the handler step is Bash and shells out to jq")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        notices = tmp_path / "notices.txt"
        notices.touch()
        reads = tmp_path / "issue-reads"
        reads.touch()
        issue_file = tmp_path / "issue.json"
        if isinstance(issue, dict):
            issue_file.write_text(json.dumps(issue), encoding="utf-8", newline="\n")
        _write_stub(
            bin_dir / "gh",
            'case "$*" in\n'
            f'  *"--method POST"*"/comments"*) cat >> "{notices}"; printf "\\n--\\n" >> "{notices}"; exit 0 ;;\n'
            f'  */pulls/*) printf \'{{"head":{{"sha":"{self.HEAD}","repo":{{"full_name":"o/r"}}}},'
            f'"user":{{"login":"{pr_author}"}}}}\'; exit 0 ;;\n'
            f'  */permission*) printf "{permission}\\n"; exit 0 ;;\n'
            "  */issues/*)\n"
            f'    printf x >> "{reads}"\n'
            '    case "$ISSUE_MODE" in\n'
            f'      json) cat "{issue_file}"; exit 0 ;;\n'
            '      notfound) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;\n'
            '      *) echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1 ;;\n'
            "    esac ;;\n"
            "esac\n"
            'echo "unexpected gh call: $*" >&2\n'
            "exit 9\n",
        )
        _write_stub(bin_dir / "sleep", "exit 0\n")
        out_file = tmp_path / "gh-output"
        out_file.touch()
        step = _step_script(
            _workflow("ai-review-human-override.yml"), "Validate and record the decision"
        )
        proc = subprocess.run(
            [bash, "-e", _script_file(tmp_path, step)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env=_child_env(
                {
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "ISSUE_MODE": "json" if isinstance(issue, dict) else (issue or "none"),
                    "RELEASE_RECORD": str(RELEASE_RECORD_PATH),
                    "RELEASE_RECORD_TODAY": today,
                    "GH_TOKEN": "stub",
                    "REPO": "o/r",
                    "PR": "1",
                    "ACTOR": actor,
                    "COMMENT_AUTHOR": actor if comment_author is None else comment_author,
                    "COMMENT_ID": "42",
                    "COMMENT_BODY": body,
                    "GITHUB_OUTPUT": str(out_file),
                }
            ),
        )
        return (
            proc,
            notices.read_text(encoding="utf-8"),
            out_file.read_text(encoding="utf-8"),
            len(reads.read_text(encoding="utf-8")),
        )

    def _cmd(self, cls: str, reason: str = _LONG_REASON, issue: str = "") -> str:
        number = f" #{issue}" if issue else ""
        return f"/ai-review override gpt {self.HEAD} {cls}{number}: {reason}"

    def _record_comment(self, notices: str) -> dict:
        """The posted decision as the bot comment every reader sees."""
        body = notices.split("\n--\n")[0]
        return {
            "user": {"type": "Bot", "login": "github-actions[bot]"},
            "body": json.loads(body)["body"],
        }

    def test_a_class_is_recorded_on_a_second_hidden_line(self, tmp_path: Path) -> None:
        proc, notices, out, _ = self._run(tmp_path, self._cmd("false-positive"))

        assert proc.returncode == 0, _proc_log(proc)
        lines = self._record_comment(notices)["body"].split("\n")
        assert lines[0] == (
            "<!-- ai-review-human-override target=gpt "
            f"head={self.HEAD} actor=writer source=42 -->"
        )
        assert lines[1] == (
            "<!-- ai-review-override-record class=false-positive issue=none "
            "author_is_actor=false -->"
        )
        assert "class=false-positive" in out

    def test_the_first_line_still_parses_as_the_override_marker(self, tmp_path: Path) -> None:
        _proc, notices, _out, _ = self._run(tmp_path, self._cmd("not-applicable"))
        comment = self._record_comment(notices)

        assert review_contract.parse_override_record(comment) == ("gpt", self.HEAD, "writer")
        assert review_contract.parse_override_class(comment) == {
            "class": "not-applicable",
            "issue": "",
            "author_is_actor": False,
        }

    def test_the_actor_who_authored_the_pr_is_recorded_as_such(self, tmp_path: Path) -> None:
        proc, notices, _out, _ = self._run(
            tmp_path, self._cmd("false-positive"), actor="Author", pr_author="author"
        )

        assert proc.returncode == 0, _proc_log(proc)
        assert "author_is_actor=true" in notices

    def test_a_tracked_deferral_is_recorded_with_its_issue(self, tmp_path: Path) -> None:
        proc, notices, _out, reads = self._run(
            tmp_path, self._cmd("deferred", issue="1234"), issue=_GOOD_ISSUE
        )

        assert proc.returncode == 0, _proc_log(proc)
        assert "class=deferred issue=1234 author_is_actor=false" in notices
        assert reads == 1

    @pytest.mark.parametrize(
        ("change", "missing"),
        [
            ({"state": "closed"}, "closed"),
            ({"labels": []}, "no deferred-finding label"),
            ({"assignees": []}, "no assignee (owner)"),
            ({"body": "nothing"}, "no due date"),
        ],
    )
    def test_an_untracked_deferral_is_refused_naming_what_is_missing(
        self, tmp_path: Path, change: dict, missing: str
    ) -> None:
        proc, notices, out, _ = self._run(
            tmp_path, self._cmd("deferred", issue="9"), issue={**_GOOD_ISSUE, **change}
        )

        assert proc.returncode == 1, _proc_log(proc)
        assert "issue #9 is not a tracked deferral" in notices
        assert missing in notices
        assert "ai-review-human-override target" not in notices
        assert out == ""

    def test_an_issue_that_does_not_exist_is_refused_as_such(self, tmp_path: Path) -> None:
        proc, notices, _out, reads = self._run(
            tmp_path, self._cmd("deferred", issue="9"), issue="notfound"
        )

        assert proc.returncode == 1
        assert "#9 is not an issue in this repository" in notices
        assert reads == 1, "a 404 is an answer, not a read to retry"

    def test_an_unreadable_issue_is_a_retry_not_an_invalid_deferral(self, tmp_path: Path) -> None:
        proc, notices, _out, reads = self._run(
            tmp_path, self._cmd("deferred", issue="9"), issue="unreadable"
        )

        assert proc.returncode == 1
        assert "could not be READ" in notices and "re-issue the command" in notices
        assert "not a tracked deferral" not in notices
        assert "ai-review-human-override target" not in notices
        assert reads == 3
        assert "::error::" in proc.stdout

    def test_a_non_writer_never_causes_an_issue_read(self, tmp_path: Path) -> None:
        proc, notices, _out, reads = self._run(
            tmp_path, self._cmd("deferred", issue="9"), permission="read", issue=_GOOD_ISSUE
        )

        assert proc.returncode == 1
        assert "only a repository writer" in notices
        assert reads == 0

    def test_an_edit_by_someone_else_is_never_recorded_as_the_authors(self, tmp_path: Path) -> None:
        # The sender made the edit; the comment's author did not. Judging the
        # edit as the author's would hand the sender's words the author's
        # authority (and the author's `author_is_actor` for later policy).
        proc, notices, out, reads = self._run(
            tmp_path,
            self._cmd("deferred", issue="9"),
            actor="editor",
            comment_author="writer",
            issue=_GOOD_ISSUE,
        )

        assert proc.returncode == 1, _proc_log(proc)
        assert "changed by @editor, not by its author @writer" in notices
        assert "ai-review-human-override target" not in notices
        assert out == ""
        assert reads == 0

    def test_an_author_who_edits_their_own_comment_is_still_recorded(self, tmp_path: Path) -> None:
        # Logins compare without case, as the PR-author comparison does.
        proc, notices, _out, _ = self._run(
            tmp_path, self._cmd("false-positive"), actor="Writer", comment_author="writer"
        )

        assert proc.returncode == 0, _proc_log(proc)
        assert "actor=Writer" in notices

    def test_the_actor_is_the_event_sender_not_the_comment_author(self) -> None:
        steps = load_workflow(WORKFLOWS / "ai-review-human-override.yml")["jobs"]["record"]["steps"]
        env = next(
            step["env"] for step in steps if step.get("name") == "Validate and record the decision"
        )

        assert env["ACTOR"] == "${{ github.event.sender.login }}"
        assert env["COMMENT_AUTHOR"] == "${{ github.event.comment.user.login }}"

    @pytest.mark.parametrize(
        "body",
        [
            "/ai-review override gpt {head} bogus-class: {reason}",
            "/ai-review override gpt {head} deferred: {reason}",
            "/ai-review override gpt {head} pre-existing: {reason}",
            "/ai-review override gpt {head} false-positive: too short",
            "/ai-review override gpt {head} owner-decision: {reason}",
        ],
    )
    def test_an_unknown_class_or_missing_requirement_posts_the_usage(
        self, tmp_path: Path, body: str
    ) -> None:
        proc, notices, out, _ = self._run(
            tmp_path, body.format(head=self.HEAD, reason=_LONG_REASON)
        )

        assert proc.returncode == 1, _proc_log(proc)
        assert "AI-review override not recorded" in notices
        assert "risk-release-records" in notices
        assert "ai-review-human-override target" not in notices
        assert out == ""

    def test_a_stale_sha_is_still_refused(self, tmp_path: Path) -> None:
        body = f"/ai-review override gpt {'b' * 40} false-positive: {_LONG_REASON}"
        proc, notices, _out, _ = self._run(tmp_path, body)

        assert proc.returncode == 1
        assert "is not the current PR head" in notices

    def test_the_old_grammar_is_recorded_with_a_warning_before_the_cutover(
        self, tmp_path: Path
    ) -> None:
        body = f"/ai-review override gpt {self.HEAD}: ok"
        proc, notices, out, _ = self._run(tmp_path, body, today="2026-11-01")

        assert proc.returncode == 0, _proc_log(proc)
        assert "class=legacy" in notices
        assert "stops working on" in notices
        assert "::warning::" in proc.stdout
        assert "class=legacy" in out

    def test_the_old_grammar_is_refused_from_the_cutover(self, tmp_path: Path) -> None:
        body = f"/ai-review override gpt {self.HEAD}: ok"
        proc, notices, out, _ = self._run(
            tmp_path, body, today=release_record.LEGACY_CUTOVER.isoformat()
        )

        assert proc.returncode == 1
        assert "retired" in notices and out == ""

    def test_shell_text_in_the_comment_is_never_executed(self, tmp_path: Path) -> None:
        # A bare name, not an absolute path: the step runs with `cwd=tmp_path`, so a command
        # that ran would still create `tmp_path / "pwned"`. A Windows path has backslashes, and
        # the notice file holds the comment as JSON, where each one is doubled, so a path in
        # the reason could never be found again by a plain substring match.
        canary = tmp_path / "pwned"
        reason = "$(touch pwned) `touch pwned` ; touch pwned # and more words here"
        proc, notices, _out, _ = self._run(tmp_path, self._cmd("false-positive", reason))

        assert proc.returncode == 0, _proc_log(proc)
        assert not canary.exists()
        assert "$(touch pwned)" in self._record_comment(notices)["body"]

    def test_the_comment_body_reaches_the_shell_only_through_env(self) -> None:
        workflow = _workflow("ai-review-human-override.yml")

        assert workflow.count("${{ github.event.comment.body }}") == 1
        assert "COMMENT_BODY: ${{ github.event.comment.body }}" in workflow
        for name in ("Load the release-record module", "Validate and record the decision"):
            assert "${{" not in _step_script(workflow, name), name
        assert "actions/checkout@" not in workflow

    def test_the_module_is_read_from_the_commit_the_workflow_runs_from(self) -> None:
        script = _step_script(
            _workflow("ai-review-human-override.yml"), "Load the release-record module"
        )

        assert "contents/.github/scripts/release_record.py?ref=$WORKFLOW_SHA" in script
        assert "WORKFLOW_SHA: ${{ github.sha }}" in _workflow("ai-review-human-override.yml")


class TestLedgerCarriesTheReleaseClass:
    """The GPT ledger keeps the class line, so a released finding is not re-raised blind."""

    def _ledger_for(self, tmp_path: Path, comments: list) -> str:
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("the ledger build is Bash and shells out to jq")
        script = _step_script(_workflow("codex-review.yml"), "Write review prompt")
        section = script[script.index('ledger_full="') : script.index('ledger="$(printf')]
        # The section is the production text, with two changes that only move where bytes
        # travel, never what jq computes. The filter is read with `jq -f` from a file, so a
        # multi-line program full of backslashes (`\n`, `\\s`) does not have to cross Git Bash's
        # argv into a native jq.exe, a hop no other Windows-run jq harness here makes. And jq's
        # stderr goes to a file instead of /dev/null, so a jq that fails says why instead of
        # leaving an empty ledger.
        opener = 'jq -r --argjson writers "$writers" \''
        closer = "' 2>/dev/null || true)\""
        first = section.index(opener) + len(opener)
        last = section.index(closer)
        filter_file = tmp_path / "ledger.jq"
        filter_file.write_text(section[first:last], encoding="utf-8", newline="\n")
        section = (
            section[: first - 1]
            + '-f "$LEDGER_FILTER" 2>"$LEDGER_JQ_ERR" || true)"'
            + section[last + len(closer) :]
        )
        comments_file = tmp_path / "comments.json"
        comments_file.write_text(json.dumps(comments), encoding="utf-8", newline="\n")
        out_file = tmp_path / "ledger.txt"
        out_file.touch()
        jq_err = tmp_path / "jq-err.txt"
        jq_err.touch()
        program = (
            'comments_json="$(cat "$COMMENTS_FILE")"\nwriters=\'[]\'\n'
            + section
            + '\nprintf \'%s\' "$ledger_full" > "$LEDGER_OUT"\n'
        )
        result = subprocess.run(
            [bash, "-e", "-c", program],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env=_child_env(
                {
                    "PATH": _stub_path(tmp_path),
                    "LC_ALL": "C",
                    "COMMENTS_FILE": comments_file.as_posix(),
                    "LEDGER_FILTER": filter_file.as_posix(),
                    "LEDGER_JQ_ERR": jq_err.as_posix(),
                    "LEDGER_OUT": out_file.as_posix(),
                    "TMPDIR": tmp_path.as_posix(),
                }
            ),
        )
        assert result.returncode == 0, _proc_log(result)
        assert jq_err.read_text(encoding="utf-8").strip() == "", _proc_log(result)
        return out_file.read_text(encoding="utf-8")

    @staticmethod
    def _override(login: str, record: str) -> dict:
        head = "a" * 40
        return {
            "created_at": "2026-11-01T00:00:00Z",
            "user": {"login": login},
            "body": (
                f"<!-- ai-review-human-override target=gpt head={head} actor=w source=1 -->\n"
                f"{record}\n## Human judgment recorded\n\n> the reason given\n"
            ),
        }

    def test_a_deferred_record_keeps_its_class_and_issue(self, tmp_path: Path) -> None:
        record = (
            "<!-- ai-review-override-record class=deferred issue=1234 " "author_is_actor=false -->"
        )
        ledger = self._ledger_for(tmp_path, [self._override("github-actions[bot]", record)])

        assert record in ledger
        assert "> the reason given" in ledger

    def test_a_forged_record_from_a_person_is_not_in_the_ledger(self, tmp_path: Path) -> None:
        record = "<!-- ai-review-override-record class=deferred issue=1 author_is_actor=false -->"
        ledger = self._ledger_for(tmp_path, [self._override("someone", record)])

        assert record not in ledger

    def test_the_ledger_header_explains_the_record_line(self) -> None:
        script = _step_script(_workflow("codex-review.yml"), "Write review prompt")

        assert "ai-review-override-record class=C issue=N" in script
        assert "class=deferred issue=N means a" in script


_REVIEW_CONTROL_WORKFLOWS = (
    "ai-review-human-override.yml",
    "disposition-deferral-check.yml",
    "sensitive-change-review-comment.yml",
)


def _run_steps(name: str) -> "list[tuple[str, str]]":
    jobs = load_workflow(WORKFLOWS / name)["jobs"]
    return [
        (step["name"], step["run"])
        for job in jobs.values()
        for step in job["steps"]
        if isinstance(step.get("run"), str) and step.get("name")
    ]


class TestTheHandlerScriptsReadTheSameOnEveryCheckout:
    """The step scripts reach bash from workflow text a Windows checkout may hand over as CRLF.

    The Windows runner stopped the handler with "unexpected EOF while looking for
    matching quote" after the script had grown, a failure only the success path
    reached because bash parses a script one top-level command at a time. A
    syntax-only parse of the WHOLE script (``bash -n``) reaches every path.
    """

    @pytest.mark.parametrize("name", _REVIEW_CONTROL_WORKFLOWS)
    def test_the_extractor_gives_the_same_script_from_a_crlf_copy(self, name: str) -> None:
        lf = _workflow(name)
        crlf = lf.replace("\n", "\r\n")

        assert "\r\n" in crlf
        for step_name, _run in _run_steps(name):
            got = _step_script(crlf, step_name)
            assert "\r" not in got, step_name
            assert got == _step_script(lf, step_name), step_name

    @pytest.mark.parametrize("name", _REVIEW_CONTROL_WORKFLOWS)
    def test_every_step_script_parses_whole_under_bash(self, name: str, tmp_path: Path) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the step scripts are Bash; skip where Bash is absent")
        crlf = _workflow(name).replace("\n", "\r\n")

        for step_name, _run in _run_steps(name):
            script = _script_file(tmp_path, _step_script(crlf, step_name))
            proc = subprocess.run(
                [bash, "-n", script],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=_child_env({**os.environ}),
            )
            assert proc.returncode == 0, f"{name}: {step_name}\n{_proc_log(proc)}"

    def test_a_handler_script_file_is_lf_only(self, tmp_path: Path) -> None:
        path = _script_file(tmp_path, "echo a\r\necho b\r\n")

        assert b"\r" not in Path(path).read_bytes()
