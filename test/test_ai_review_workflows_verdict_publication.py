"""Review-workflow tests that drive each lane's real guarded comment upsert: a posted
verdict is never buried, duplicated or claimed published without landing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    _GUARDED_LANES,
    _GUARDED_OVERRIDE_LANES,
    ROOT,
    _bash,
    _child_env,
    _fork_scope_comment,
    _step_script,
    _workflow,
)

_GUARDED_LANE_PARAMS = [pytest.param(lane, id=lane["id"]) for lane in _GUARDED_LANES]


def _missed_slot_rc(lane: dict) -> int:
    """The step's exit status when a completed verdict for the current head never
    reached the slot. Advisory lanes stay green and annotate; the same-repo GPT
    lane is fail-closed and exits 1, read off the upsert's `publication` output."""
    return int(lane.get("missed_slot_rc", 0))


class TestReviewLaneVerdictVisibility:
    """No review lane may bury a posted verdict under an incomplete body.

    Covers every lane that upserts a marker-keyed summary comment through the
    shared guard, the same-repo GPT lane included. Each lane defines
    the guarded upsert as a byte-identical ``guarded_comment_upsert`` bash
    function; the identity test pins every copy to one canonical body so the
    invariant cannot drift lane by lane, and the behavioral tests run each
    lane's REAL step bash with a stubbed ``gh`` for the contract cases. The
    one per-lane difference is the exit status after a completed verdict
    missed the slot (``_missed_slot_rc``): the fail-closed GPT lane exits 1
    where the advisory lanes stay green.

    The guarded transition is a NO-TOUCH, not a merge: a run whose body
    carries no ``"<stamp> <head>"`` proof marker leaves an existing comment
    entirely alone. Reading the body and PATCHing a merge of it back would
    reopen the same class of loss from the other side, because runs for
    different heads are not cancelled — a verdict published between the read
    and the write is restored away.
    """

    HEAD = "1234567890abcdef1234567890abcdef12345678"
    OLD = "aaaa567890abcdef1234567890abcdef1234aaaa"

    def _verdict_body(self, lane: dict, sha: str) -> str:
        return (
            f"{lane['marker']}\n"
            "## Review — 🔴 changes requested (blocking)\n"
            "\n"
            f"a completed review of `{sha}`.\n"
            "\n"
            f"{lane['stamp']} {sha}\n"
        )

    def _run_step(
        self,
        lane: dict,
        tmp_path: Path,
        *,
        existing_body: str | None,
        kind: str,
        extra_env: dict[str, str] | None = None,
        prepare: object | None = None,
    ) -> tuple[Path, "subprocess.CompletedProcess[bytes]"]:
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("lane upsert tests require Bash and jq")
        if lane["needs_perl"] and shutil.which("perl") is None:
            pytest.skip("this lane's posting step redacts with perl")
        if os.name == "nt":
            pytest.skip("stubbed-PATH gh interception is exercised on POSIX runners")

        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        redactor_env: dict[str, str] = {}
        if lane.get("needs_redactor"):
            # The REAL redactor, not a stub: the step's refusal branch turns on
            # this file being present and runnable, so a stand-in would let the
            # branch pass while the actual scrub was broken. `python` is shimmed
            # onto the stub PATH because the workflow's own runner gets it from
            # setup-python, and a host that spells it only `python3` would send
            # this step down its refusal branch for the wrong reason.
            python_shim = stub_dir / "python"
            python_shim.write_text(
                f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
            )
            python_shim.chmod(0o755)
            redactor_env["REDACTOR"] = str(ROOT / "scripts" / "scope_redact.py")
        calls_dir = tmp_path / "calls"
        calls_dir.mkdir()
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()
        cwd = tmp_path / "workspace"
        cwd.mkdir()

        finder_file = tmp_path / "finder-comments.json"
        if existing_body is None:
            finder_file.write_text("[]", encoding="utf-8")
        else:
            finder_file.write_text(
                json.dumps(
                    [
                        {"id": 999, "user": {"login": "mallory"}, "body": existing_body},
                        {
                            "id": 123,
                            "user": {"login": "github-actions[bot]"},
                            "body": existing_body,
                        },
                    ]
                ),
                encoding="utf-8",
            )

        if kind == "completed":
            case_env = lane["completed"](cwd, runner_temp, self.HEAD)
        elif kind == "incomplete":
            case_env = lane["incomplete"](cwd, runner_temp, self.HEAD)
        else:
            case_env = {}
        # A case that needs a file the lane's own kind fixtures do not write
        # (the classifier's folded verdict, say) adds it here, so the shared
        # fixtures keep meaning exactly what the seven contracts above assert.
        if prepare is not None:
            case_env = {**case_env, **prepare(cwd, runner_temp, self.HEAD)}

        gh_stub = stub_dir / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            "# Emulates the two gh surfaces the step uses; records mutations.\n"
            "# The finder branch runs the step's REAL --jq filter with real jq\n"
            "# over an array fixture, so a drift in the filter (dropped .id,\n"
            "# changed author guard) fails these tests instead of hiding.\n"
            'if [ "$1" = "api" ] && [ "$2" = "--method" ] && [ "$3" = "PATCH" ]; then\n'
            '  printf \'%s\\n\' "$4" >> "$STUB_CALLS/patch-calls.txt"\n'
            "  # Every attempt at the comment WRITE fails, so the retry is\n"
            "  # visible in the recorded call count. Records no body: a write\n"
            "  # that failed left nothing on the comment, and asserting on a\n"
            "  # body the API never accepted is how a lost write reads as a\n"
            "  # published one.\n"
            '  if [ -n "${STUB_PATCH_FAIL:-}" ]; then exit 6; fi\n'
            '  for a in "$@"; do\n'
            '    case "$a" in body=*) printf \'%s\' "${a#body=}" > "$STUB_CALLS/patched-body.md";; esac\n'
            "  done\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "api" ] && [ -n "${FINDER_FAIL:-}" ]; then\n'
            "  # Every attempt at the COMMENT lookup fails, so the retry is\n"
            "  # visible in the recorded call count; the step must not read a\n"
            '  # lookup error as "no comment exists".\n'
            '  case "$2" in\n'
            "    */comments)\n"
            '      printf \'%s\\n\' "$2" >> "$STUB_CALLS/comment-reads.txt"\n'
            "      exit 4\n"
            "      ;;\n"
            "  esac\n"
            "fi\n"
            'if [ "$1" = "api" ] && [ -n "${STUB_COMMENT_FAIL_FIRST:-}" ]; then\n'
            "  # Only the FIRST N comment lookups fail, then they succeed. This is\n"
            "  # the interleaving that makes a stale comment dangerous: the id\n"
            "  # lookup errors so the step takes the create path, and a later\n"
            "  # confirmation query then succeeds and can see the stale comment.\n"
            '  case "$2" in\n'
            "    */comments)\n"
            '      printf \'%s\\n\' "$2" >> "$STUB_CALLS/comment-reads.txt"\n'
            '      if [ "$(wc -l < "$STUB_CALLS/comment-reads.txt")" -le "$STUB_COMMENT_FAIL_FIRST" ]; then\n'
            "        exit 4\n"
            "      fi\n"
            "      ;;\n"
            "  esac\n"
            "fi\n"
            'if [ "$1" = "api" ]; then\n'
            "  # The PR object, from which the step reads the CURRENT head sha.\n"
            "  # STUB_PR_HEAD unset means the PR still points at this run;\n"
            "  # set-but-empty means every read fails, so the retry is visible\n"
            "  # in the recorded call count.\n"
            '  case "$2" in\n'
            "    */pulls/*)\n"
            '      printf \'%s\\n\' "$2" >> "$STUB_CALLS/pr-head-reads.txt"\n'
            '      if [ -z "${STUB_PR_HEAD-unset}" ]; then exit 5; fi\n'
            "      # STUB_PR_HEAD_AFTER=<n>:<sha> moves the head MID-RUN: the\n"
            "      # first n reads answer as usual and later ones answer <sha>,\n"
            "      # or fail when it is empty. That is the only way to reach the\n"
            "      # retry's own head re-confirmation, because the caller's\n"
            "      # check reads first and a head that has already moved there\n"
            "      # never gets as far as a write.\n"
            '      if [ -n "${STUB_PR_HEAD_AFTER:-}" ] \\\n'
            '        && [ "$(wc -l < "$STUB_CALLS/pr-head-reads.txt")" -gt "${STUB_PR_HEAD_AFTER%%:*}" ]; then\n'
            '        if [ -z "${STUB_PR_HEAD_AFTER#*:}" ]; then exit 5; fi\n'
            "        printf '%s\\n' \"${STUB_PR_HEAD_AFTER#*:}\"\n"
            "        exit 0\n"
            "      fi\n"
            "      printf '%s\\n' \"${STUB_PR_HEAD:-$HEAD}\"\n"
            "      exit 0\n"
            "      ;;\n"
            "  esac\n"
            "fi\n"
            'if [ "$1" = "api" ]; then\n'
            "  # One comment read by id: what a repeat uses to spot a human\n"
            "  # override before writing over it. STUB_TARGET_BODY set-but-empty\n"
            "  # makes the read FAIL, which is a different case from a body that\n"
            "  # simply is not an override. The PATCH branch above matches first,\n"
            "  # so a write to the same path never lands here.\n"
            '  case "$2" in\n'
            "    */issues/comments/[0-9]*)\n"
            '      printf \'%s\\n\' "$2" >> "$STUB_CALLS/target-reads.txt"\n'
            '      if [ -z "${STUB_TARGET_BODY-x}" ]; then exit 5; fi\n'
            "      printf '%s\\n' \"${STUB_TARGET_BODY:-## Lane Review -- verdict body}\"\n"
            "      exit 0\n"
            "      ;;\n"
            "  esac\n"
            "fi\n"
            'if [ "$1" = "api" ]; then\n'
            '  filter=""\n'
            "  grab=0\n"
            '  for a in "$@"; do\n'
            '    if [ "$grab" = 1 ]; then filter="$a"; grab=0; fi\n'
            '    [ "$a" = "--jq" ] && grab=1\n'
            "  done\n"
            '  jq -r "$filter" < "$FINDER_COMMENTS_FILE"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then\n'
            '  printf \'%s\\n\' "$3" >> "$STUB_CALLS/create-calls.txt"\n'
            "  shift 3\n"
            '  if [ -n "${STUB_CREATE_FAIL:-}" ]; then\n'
            "    # STUB_CREATE_LANDS emulates the lost-ack partial failure that\n"
            "    # makes a create unsafe to repeat: GitHub ACCEPTS the POST, so\n"
            "    # the comment now exists, but the CLI still reports failure.\n"
            "    # The body is written into the finder fixture, so the step's own\n"
            "    # confirmation query sees it through real jq.\n"
            '    if [ -n "${STUB_CREATE_LANDS:-}" ] && [ "$1" = "--body-file" ]; then\n'
            '      jq -n --arg b "$(cat "$2")" \\\n'
            "        '[{id:777,user:{login:\"github-actions[bot]\"},body:$b}]' \\\n"
            '        > "$FINDER_COMMENTS_FILE"\n'
            "    fi\n"
            "    exit 7\n"
            "  fi\n"
            '  if [ "$1" = "--body-file" ]; then cp "$2" "$STUB_CALLS/created-body.md"; fi\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o755)

        # `sleep` is an external command, so a shim earlier on PATH intercepts
        # the retry backoff without a test-only knob in the workflow: the lanes
        # keep their real production budget and these tests do not wait it out.
        # Records each interval, so the SCHEDULE is assertable rather than just
        # the attempt count.
        sleep_stub = stub_dir / "sleep"
        sleep_stub.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' "$1" >> "$STUB_CALLS/sleeps.txt"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        sleep_stub.chmod(0o755)

        script = _step_script(_workflow(lane["workflow"]), lane["step"])
        script_file = tmp_path / "step.sh"
        script_file.write_text(script, encoding="utf-8")

        env = {
            **os.environ,
            "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "REPO": "example/repo",
            "PR": "1",
            "HEAD": self.HEAD,
            "GH_TOKEN": "stub-token",
            "RUNNER_TEMP": str(runner_temp),
            "STUB_CALLS": str(calls_dir),
            "FINDER_COMMENTS_FILE": str(finder_file),
            "GITHUB_OUTPUT": str(tmp_path / "github-output.txt"),
            **lane["env"],
            **redactor_env,
            **case_env,
            **(extra_env or {}),
        }
        # GitHub runs `run:` blocks with `bash -e {0}` when no shell is set.
        result = subprocess.run(
            [bash, "-e", str(script_file)],
            check=False,
            capture_output=True,
            cwd=cwd,
            env=_child_env(env),
        )
        return calls_dir, result

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_incomplete_run_never_overwrites_a_posted_verdict(
        self, lane: dict, tmp_path: Path
    ) -> None:
        calls, result = self._run_step(
            lane, tmp_path, existing_body=self._verdict_body(lane, self.OLD), kind="incomplete"
        )

        assert result.returncode == 0, result.stderr.decode()
        # NOTHING was written: no PATCH of any comment, no new comment. The
        # posted verdict is therefore still exactly what the completed run
        # published, and no read-modify-write can restore a stale body over a
        # verdict a concurrent run publishes in the meantime.
        assert not (calls / "patch-calls.txt").exists()
        assert not (calls / "patched-body.md").exists()
        assert not (calls / "created-body.md").exists()
        # The step says so, naming the comment it deliberately left alone --
        # and it found that id through the real author-guarded --jq filter, so
        # it is the bot's comment (123), never the impostor's (999).
        stdout = result.stdout.decode()
        assert "left existing comment #123 untouched" in stdout
        assert "#999" not in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_incomplete_run_with_a_failed_lookup_posts_nothing(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # A lookup that errored cannot be read as "no comment exists": taking
        # the create path there plants a second marker comment over a possibly
        # live verdict, and a marker planted is not undone by a later run.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="incomplete",
            extra_env={"FINDER_FAIL": "1"},
        )

        assert result.returncode == 0, result.stderr.decode()
        assert not (calls / "patched-body.md").exists()
        assert not (calls / "created-body.md").exists()
        # Whether a comment exists is a gating input, so the read retries
        # before the step concludes it could not be determined -- on a budget
        # that outlasts the failure it exists for. App-wide API exhaustion
        # lasts minutes, so a few seconds of tolerance withholds a verdict the
        # lane has already earned.
        reads = (calls / "comment-reads.txt").read_text(encoding="utf-8").splitlines()
        assert len(reads) == 6, reads
        assert "comment lookup also failed" in result.stdout.decode()

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_superseded_completed_verdict_leaves_the_comment_alone(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The same loss arriving late: a run for an older head can COMPLETE
        # after a newer run published its verdict into the shared slot -- the
        # fork lanes' concurrency group is keyed per head so they are not
        # cancelled, and a cancelled same-repo run still executes this
        # if:always() step. Its verdict is real, but it is not this PR's
        # verdict any more, so it must not replace the current one.
        newer = "bbbb567890abcdef1234567890abcdef1234bbbb"
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, newer),
            kind="completed",
            extra_env={"STUB_PR_HEAD": newer},
        )

        assert result.returncode == 0, result.stderr.decode()
        assert not (calls / "patched-body.md").exists()
        assert not (calls / "created-body.md").exists()
        stdout = result.stdout.decode()
        assert "is no longer this PR's head" in stdout
        assert "left existing comment #123 untouched" in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_an_unreadable_pr_head_retries_then_withholds(self, lane: dict, tmp_path: Path) -> None:
        # Writing is the destructive half of the guard, so it must not proceed
        # on an unknown: a head that stays unreadable is NOT confirmed current
        # and the shared slot is left alone. A single blip is not evidence
        # about the PR, so the read retries first, as this lane's other gating
        # reads do.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="completed",
            extra_env={"STUB_PR_HEAD": ""},
        )

        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        reads = (calls / "pr-head-reads.txt").read_text(encoding="utf-8").splitlines()
        assert len(reads) == 6, reads
        assert not (calls / "patched-body.md").exists()
        assert not (calls / "created-body.md").exists()
        stdout = result.stdout.decode()
        assert "unreadable after 6 attempts" in stdout
        # A withheld verdict is reported as an ANNOTATION, not as one line in a
        # job log nobody opens. Withholding is the right call on an unreadable
        # head, but a lane that withholds still concludes `success`, so the
        # annotation is the only thing that tells a withheld verdict apart from
        # a published one.
        assert "::warning::Withholding" in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_completed_verdict_still_replaces_the_comment(self, lane: dict, tmp_path: Path) -> None:
        calls, result = self._run_step(
            lane, tmp_path, existing_body=self._verdict_body(lane, self.OLD), kind="completed"
        )

        assert result.returncode == 0, result.stderr.decode()
        patched = (calls / "patched-body.md").read_text(encoding="utf-8")
        # A completed verdict replaces the comment wholesale, exactly as before.
        assert patched.startswith(f"{lane['marker']}\n")
        assert f"{lane['stamp']} {self.HEAD}" in patched
        assert f"{lane['stamp']} {self.OLD}" not in patched
        assert not (calls / "created-body.md").exists()
        # The author guard ran inside the real filter: the PATCH must target
        # the bot's comment (123), not the marker-planting impostor's (999).
        patch_calls = (calls / "patch-calls.txt").read_text(encoding="utf-8")
        assert "/comments/123" in patch_calls
        assert "/comments/999" not in patch_calls

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_completed_verdict_creates_once_a_duplicate_is_ruled_out(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # A completed verdict must not be dropped merely because the caller's own
        # id lookup failed. That lookup is not the last word: the write re-reads,
        # and a clean re-read finding the slot empty has PROVED there is nothing
        # to duplicate, so the verdict publishes exactly as it would have.
        #
        # An UNKNOWN and an OCCUPIED slot are different answers from an empty
        # one, and only emptiness licenses the write. The sibling contract above
        # refuses to create when the lookup errored, because a marker planted is
        # not undone by a later run; the same unknown cannot mean "must not
        # create" for a notice and "must create" for a verdict.
        # `test_an_unreadable_duplicate_check_refuses_the_write` and
        # `test_a_stale_head_comment_also_occupies_the_slot` cover the other two.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=None,
            kind="completed",
            extra_env={"STUB_COMMENT_FAIL_FIRST": "6"},
        )

        assert result.returncode == 0, result.stderr.decode()
        created = (calls / "created-body.md").read_text(encoding="utf-8")
        assert f"{lane['stamp']} {self.HEAD}" in created
        assert not (calls / "patched-body.md").exists()

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_incomplete_with_no_existing_comment_creates_as_before(
        self, lane: dict, tmp_path: Path
    ) -> None:
        calls, result = self._run_step(lane, tmp_path, existing_body=None, kind="incomplete")

        assert result.returncode == 0, result.stderr.decode()
        created = (calls / "created-body.md").read_text(encoding="utf-8")
        assert created.startswith(f"{lane['marker']}\n")
        assert lane["incomplete_text"] in created
        assert not (calls / "patched-body.md").exists()

    # A row the classifier flipped: the whole point of the lane, and the thing a
    # reader needs in order to act -- which legitimate operation broke, at which
    # tier.
    ROWS = "| `chmod 0700 ~/.ssh` | allowed | REFUSED |"

    def _scope_lane(self) -> dict:
        return next(entry for entry in _GUARDED_LANES if entry["id"] == "security-scope")

    def _fold_rows(self, cwd: Path, runner_temp: Path, head: str) -> dict[str, str]:
        """Leave the folded verdict `scope_candidates.py verdict --out-md` writes."""
        (cwd / "verdict-body.md").write_text(
            "### Confirmed newly-refused operations\n\n"
            "| operation | base | head |\n| --- | --- | --- |\n"
            f"{self.ROWS}\n",
            encoding="utf-8",
        )
        return {}

    def test_a_confirmed_regression_publishes_its_rows_with_no_model_marker(
        self, tmp_path: Path
    ) -> None:
        # `deny_diff.py` classified these rows at the base ref and at this head,
        # so they describe this revision whatever the model produced. Before,
        # a run whose model half left no marker wrote a body the guard could not
        # accept as complete, so nothing was posted at all: the author saw a red
        # badge and had to open the job logs to learn what the script had
        # already decided.
        lane = self._scope_lane()
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="incomplete",
            prepare=self._fold_rows,
            extra_env={"FOLDED": "regression"},
        )

        assert result.returncode == 0, result.stderr.decode()
        patched = (calls / "patched-body.md").read_text(encoding="utf-8")
        assert self.ROWS in patched
        assert f"[SCOPE-REVIEWED] {self.HEAD}" in patched
        # The model's own text is withheld exactly as it was: with no
        # current-head marker it is a review of something else, and printing it
        # beside a verdict would read as that verdict's reasoning.
        assert "stale reasoning" not in patched
        assert "[SCOPE-REVIEWED] feedbead" not in patched
        assert "its text is withheld" in patched

    def test_an_accepted_override_replaces_a_standing_block(self, tmp_path: Path) -> None:
        # An override body is a completed verdict FOR THIS HEAD: the record the
        # generate stage read is keyed to this sha, so the acceptance describes
        # this revision on the human's own authority. Unstamped, the guard reads
        # it as a failure notice and leaves the prior BLOCK comment standing --
        # the pull request then shows a red block over an override a human has
        # accepted, and nothing later clears it, because every subsequent run on
        # this head takes the same override branch.
        lane = self._scope_lane()
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="completed",
            extra_env={"HUMAN_OVERRIDE": "true", "OVERRIDE_ACTOR": "maintainer"},
        )

        assert result.returncode == 0, result.stderr.decode()
        patched = (calls / "patched-body.md").read_text(encoding="utf-8")
        assert "human override accepted" in patched
        assert "@maintainer" in patched
        assert f"[SCOPE-REVIEWED] {self.HEAD}" in patched
        # The BLOCK it replaced is gone from the slot, not merged into it.
        assert "changes requested" not in patched

    def test_a_named_validate_refusal_is_the_reason_a_no_report_run_gives(
        self, tmp_path: Path
    ) -> None:
        # A candidate carrying a credential shape is refused in `validate`, so no
        # leg runs and the fold reads `none`. The lane's reason -- the text the
        # failing status names -- is then the refusal's own cause, not the generic
        # "no leg reported" sentence and not "the scope harness is absent".
        lane = self._scope_lane()
        _calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=None,
            kind="incomplete",
            extra_env={
                "FOLDED": "none",
                "ADJUDICATED": "false",
                "VALIDATE_RESULT": "failure",
                "ADJUDICATE_RESULT": "skipped",
                "VALIDATE_REFUSAL": "corpus-credential",
            },
        )

        assert result.returncode == 0, result.stderr.decode()
        lines = (tmp_path / "github-output.txt").read_text(encoding="utf-8").splitlines()
        assert "verdict=UNKNOWN" in lines
        why = next(line for line in lines if line.startswith("why="))
        assert "carries a credential shape" in why, why
        assert "harness is absent" not in why, why
        assert "no leg reported" not in why, why

    def test_a_base_table_that_still_requires_a_lane_still_answers(self, tmp_path: Path) -> None:
        # The post step runs the table from the BASE blob the fold step staged, and a
        # base can be a revision whose `conclude` still requires `--lane`. Handed none,
        # that table exits 2 and `bash -e` ends the step before the fail-closed
        # comment, so the run loses its reason to an argparse error. The case above
        # runs this tree's lane-free table; this one runs that revision's shape: the
        # same table, with its parser line back.
        source = (ROOT / "scripts" / "scope_candidates.py").read_text(encoding="utf-8")
        parser = (
            '    c = sub.add_parser("conclude", '
            'help="map the fold + model signals to ONE lane conclusion")\n'
        )
        assert parser in source, "the conclude parser line moved; rebuild the base shape"
        lane_arg = '    c.add_argument("--lane", required=True, choices=["fork", "same-repo"])\n'
        harness = tmp_path / "base-harness"
        harness.mkdir()
        (harness / "scope_candidates.py").write_text(
            source.replace(parser, parser + lane_arg), encoding="utf-8"
        )
        shutil.copyfile(ROOT / "scripts" / "deny_diff.py", harness / "deny_diff.py")
        lane = self._scope_lane()
        _calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=None,
            kind="incomplete",
            extra_env={
                "HARNESS": str(harness),
                "FOLDED": "none",
                "ADJUDICATED": "false",
                "VALIDATE_RESULT": "failure",
                "ADJUDICATE_RESULT": "skipped",
                "VALIDATE_REFUSAL": "corpus-credential",
            },
        )

        assert result.returncode == 0, result.stderr.decode()
        lines = (tmp_path / "github-output.txt").read_text(encoding="utf-8").splitlines()
        why = next(line for line in lines if line.startswith("why="))
        assert "carries a credential shape" in why, why

    def _fork_scope_lane(self) -> dict:
        return next(entry for entry in _GUARDED_LANES if entry["id"] == "fork-security-scope")

    def _fork_rows(self, cwd: Path, runner_temp: Path, head: str) -> dict[str, str]:
        """Re-compose the fork body with a folded verdict the model cannot claim."""
        return _fork_scope_comment(cwd, runner_temp, head, marker_head="feedbead", rows=True)

    def test_the_fork_lane_publishes_its_rows_with_no_model_marker_too(
        self, tmp_path: Path
    ) -> None:
        # The fork lane reaches the author through the same one comment slot, so a
        # row its classifier confirmed has to survive a dead model half there as
        # well. Withholding the body instead loses the row entirely on a re-run:
        # candidates are re-sampled every run, and the carry-forward that keeps a
        # confirmed row reachable reads it back out of THIS comment.
        lane = self._fork_scope_lane()
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="incomplete",
            prepare=self._fork_rows,
        )

        assert result.returncode == 0, result.stderr.decode()
        patched = (calls / "patched-body.md").read_text(encoding="utf-8")
        # The classifier's row, and the stamp that makes the guard accept it.
        assert "`git status`" in patched
        assert f"[SCOPE-REVIEWED] {self.HEAD}" in patched
        # The model's prose stays withheld: with no current-head marker it is a
        # review of something else, and printing it beside the rows would read as
        # the reasoning behind them.
        assert "no legitimate operation newly refused" not in patched
        assert "[SCOPE-REVIEWED] feedbead" not in patched
        assert "the model's text is withheld" in patched

    def test_a_no_verdict_run_still_carries_whatever_the_classifier_measured(
        self, tmp_path: Path
    ) -> None:
        # The fail-closed notice branch, where no verdict parsed at all. A leg
        # that reported NO VERDICT is still this head's measurement and names the
        # surface left unadjudicated, so it rides along with the notice.
        lane = self._scope_lane()
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="incomplete",
            prepare=self._fold_rows,
        )

        assert result.returncode == 0, result.stderr.decode()
        patched = (calls / "patched-body.md").read_text(encoding="utf-8")
        assert "could not complete" in patched
        assert self.ROWS in patched
        assert f"[SCOPE-REVIEWED] {self.HEAD}" in patched
        assert "stale reasoning" not in patched

    def test_a_run_that_measured_nothing_still_withholds_the_comment(self, tmp_path: Path) -> None:
        # The stamp comes from the classifier's OUTPUT, never from the fact that
        # the step ran. With no folded verdict nothing is attributable to this
        # head, so the shared slot is left alone -- otherwise publishing on the
        # classifier's authority would become a licence to bury a live verdict
        # under a notice, which is the very loss the guard exists to stop.
        lane = self._scope_lane()
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="incomplete",
            extra_env={"FOLDED": "regression"},
        )

        assert result.returncode == 0, result.stderr.decode()
        assert not (calls / "patched-body.md").exists()
        assert not (calls / "created-body.md").exists()
        assert "left existing comment #123 untouched" in result.stdout.decode()

    def test_withheld_fork_fp_body_leaves_a_posted_verdict_alone(self, tmp_path: Path) -> None:
        # The fork first-principles lane posts its withheld notice from a
        # separate early site; it routes through the same guarded upsert, so a
        # credential-shaped output discards the body without touching the
        # already-posted verdict.
        lane = next(entry for entry in _GUARDED_LANES if entry["id"] == "fork-first-principles")
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="none",
            extra_env={"WITHHELD": "true", "VERDICT": "UNKNOWN", "REVIEW_OUTCOME": "success"},
        )

        assert result.returncode == 0, result.stderr.decode()
        assert not (calls / "patched-body.md").exists()
        assert not (calls / "created-body.md").exists()
        assert "left existing comment #123 untouched" in result.stdout.decode()

    def test_skip_notice_still_replaces_the_comment_wholesale(self, tmp_path: Path) -> None:
        # A skip notice is a COMPLETED determination about the current head
        # (the revision ships no reviewable surface), not a review failure, so
        # it deliberately keeps the unguarded replace: guarding it would pin a
        # stale verdict onto a revision the lane has ruled out of scope.
        lane = next(entry for entry in _GUARDED_LANES if entry["id"] == "ux")
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="none",
            extra_env={"UI_SCOPE": "false", "EXEC_FILE": "", "REVIEW_OUTCOME": "success"},
        )

        assert result.returncode == 0, result.stderr.decode()
        patched = (calls / "patched-body.md").read_text(encoding="utf-8")
        assert "⏭️ skipped" in patched
        assert f"{lane['stamp']} {self.OLD}" not in patched

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_lost_patch_is_retried_and_never_claimed_as_published(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The lane HAS a completed verdict for the current head and is cleared
        # to claim the slot, but the write itself fails. One attempt under
        # `|| true` with an unconditional "Updated existing ..." echo leaves the
        # marker pinned to a superseded head while the job log claims the
        # opposite, and kirocrew-prepare-pr then refuses a PR whose every check passes.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="completed",
            extra_env={"STUB_PATCH_FAIL": "1"},
        )

        # An unpublishable verdict is an infrastructure failure, not a BLOCK
        # verdict, so it still must not fail an advisory lane's gate.
        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        # The write is retried on the gating reads' budget, not attempted once.
        attempts = (calls / "patch-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(attempts) == 6, attempts
        # The backoff SCHEDULE, not just the count: five waits between six
        # attempts, linear 5s, ~75s in total -- sized against exhaustion
        # windows that last minutes rather than seconds.
        waits = (calls / "sleeps.txt").read_text(encoding="utf-8").splitlines()
        assert waits == ["5", "10", "15", "20", "25"], waits
        # Each repeat re-confirms this run's head first, so six write attempts
        # carry five re-confirmations on top of the caller's own check. The
        # head is what gives this write the standing to replace the comment, so
        # a repeat that never re-checks it can restore a superseded body over a
        # newer head's verdict.
        heads = (calls / "pr-head-reads.txt").read_text(encoding="utf-8").splitlines()
        assert len(heads) == 6, heads
        # And each repeat re-reads the comment it is about to write, which is
        # what keeps an accepted override from being restored away. Five reads
        # for five repeats; the first write has nothing to guard against.
        targets = (calls / "target-reads.txt").read_text(encoding="utf-8").splitlines()
        assert len(targets) == 5, targets
        # Nothing landed, so the stub recorded no accepted body.
        assert not (calls / "patched-body.md").exists()
        stdout = result.stdout.decode()
        # The step does NOT claim to have updated the comment ...
        assert "Updated existing" not in stdout
        # ... and the loss is an annotation naming the head whose verdict is
        # unpublished, so it is visible without opening the job log.
        assert "::error::" in stdout
        assert self.HEAD in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_repeat_never_writes_over_an_accepted_override(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The head check cannot see a SAME-head writer, and one same-head body
        # must never be written over: an accepted human override replaces this
        # slot's comment, so restoring a verdict on top of it puts back the
        # block the override cleared, and no later run undoes that. A repeat
        # therefore re-reads the comment it is about to write.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="completed",
            extra_env={
                "STUB_PATCH_FAIL": "1",
                "STUB_TARGET_BODY": "## Lane Review -- human override accepted by a writer",
            },
        )

        # Advisory lanes stay green: a human decided what this slot says, so
        # this run publishing nothing is the correct outcome rather than a
        # loss. The fail-closed GPT lane reads the same `not_published` the
        # fork GPT finalize reads for this arm, and answers it the same way:
        # an override for THIS head is this run's own override input, and a
        # note that arrived mid-write belongs to the run the override handler
        # cancels and re-runs with that input -- so this run does not pass
        # quietly on a verdict that is not in the slot.
        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        # ONE attempt. The repeat stops at the guard, which is what leaves the
        # override in place.
        attempts = (calls / "patch-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(attempts) == 1, attempts
        assert not (calls / "patched-body.md").exists()
        stdout = result.stdout.decode()
        assert "accepted human override" in stdout
        assert "Updated existing" not in stdout
        # The shared function itself never annotates this arm as an error; the
        # fail-closed lane's one annotation comes from its own tail.
        assert stdout.count("::error::") == (1 if _missed_slot_rc(lane) else 0), stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_an_unreadable_target_stops_the_repeat(self, lane: dict, tmp_path: Path) -> None:
        # Same rule as the unreadable head: repeating is the destructive half,
        # so it does not proceed on an unknown. A re-run publishes the verdict.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="completed",
            extra_env={"STUB_PATCH_FAIL": "1", "STUB_TARGET_BODY": ""},
        )

        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        attempts = (calls / "patch-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(attempts) == 1, attempts
        assert not (calls / "patched-body.md").exists()
        stdout = result.stdout.decode()
        assert "could not be read" in stdout
        assert "::error::" in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_moved_head_stops_the_patch_retry(self, lane: dict, tmp_path: Path) -> None:
        # A replace has no slot re-read to protect it: the caller names the
        # comment id and the primitive never reads that comment's body, so the
        # repeat is blind. When the PR moves to a newer head between two
        # attempts, the run for that head owns the comment, and repeating this
        # write puts a superseded verdict back over a current one.
        newer = "cccc567890abcdef1234567890abcdef1234cccc"
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="completed",
            extra_env={"STUB_PATCH_FAIL": "1", "STUB_PR_HEAD_AFTER": f"1:{newer}"},
        )

        # Green: an unpublished verdict is an infrastructure outcome, and a
        # superseded run publishing nothing is the correct outcome besides.
        assert result.returncode == 0, result.stderr.decode()
        # ONE write attempt, not six. The head re-confirmation ends the loop
        # before the second, which is what keeps the newer verdict in place.
        attempts = (calls / "patch-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(attempts) == 1, attempts
        assert not (calls / "patched-body.md").exists()
        stdout = result.stdout.decode()
        assert "Updated existing" not in stdout
        assert newer in stdout
        assert "this PR has moved to a newer head" in stdout
        # Nothing is missing from the PR, so this is not an error annotation:
        # the run for the current head publishes the verdict that counts.
        assert "::error::" not in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_an_unreadable_head_stops_the_patch_retry_too(self, lane: dict, tmp_path: Path) -> None:
        # Repeating is the destructive half, so an unknown stops it on the same
        # terms as a moved head. The budget belongs to the write, not to this
        # read: one unreadable answer ends the loop, and a re-run publishes the
        # verdict. The opposite trade cannot be made safely -- a blind repeat
        # can restore a superseded body over a verdict that is current.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="completed",
            extra_env={"STUB_PATCH_FAIL": "1", "STUB_PR_HEAD_AFTER": "1:"},
        )

        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        attempts = (calls / "patch-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(attempts) == 1, attempts
        assert not (calls / "patched-body.md").exists()
        stdout = result.stdout.decode()
        assert "Updated existing" not in stdout
        assert "is unreadable, so this write was not repeated" in stdout
        # This one IS a loss: the verdict is not on the PR and nobody else is
        # publishing it, so it gets the annotation.
        assert "::error::" in stdout
        assert self.HEAD in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_landed_write_is_reported_landed_even_once_the_head_moves(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # Order inside the retry: the landing check runs BEFORE the head
        # re-confirmation. A write whose ack was lost is ON the PR, so the head
        # moving afterwards does not un-publish it, and calling it a superseded
        # no-write would be the same false claim as calling an unlanded write
        # published, only in the other direction.
        newer = "dddd567890abcdef1234567890abcdef1234dddd"
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=None,
            kind="completed",
            extra_env={
                "STUB_CREATE_FAIL": "1",
                "STUB_CREATE_LANDS": "1",
                "STUB_PR_HEAD_AFTER": f"1:{newer}",
            },
        )

        assert result.returncode == 0, result.stderr.decode()
        # One create, and no second one: the confirmation saw the landed write.
        attempts = (calls / "create-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(attempts) == 1, attempts
        stdout = result.stdout.decode()
        assert "the previous attempt landed" in stdout
        assert "moved to a newer head" not in stdout
        assert "::error::" not in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_lost_create_is_retried_and_never_claimed_as_published(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The same defect on the other write: with no existing comment the
        # guard CREATES, and that call carries the identical risk of a lost
        # write announced as a published one.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=None,
            kind="completed",
            extra_env={"STUB_CREATE_FAIL": "1"},
        )

        # Green, with an error annotation, for the same reason: the lane must
        # not manufacture a blocking verdict out of a failed write.
        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        attempts = (calls / "create-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(attempts) == 6, attempts
        assert not (calls / "created-body.md").exists()
        stdout = result.stdout.decode()
        assert "Published" not in stdout
        assert "::error::" in stdout
        assert self.HEAD in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_create_whose_ack_was_lost_is_not_posted_again(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # A PATCH is idempotent, so repeating it is safe. A create is a POST and
        # is not: GitHub can ACCEPT it and lose the ack, so a blind second
        # attempt plants a SECOND marker comment. Every id lookup in these lanes
        # selects one comment, so a later human override patches only that one
        # while the duplicate keeps its own [BLOCK-MERGE] line, and pr_status.py
        # stays blocked until somebody deletes the extra comment by hand. That is
        # the same gate-stranding this change exists to remove, reached from the
        # other side, so the retry confirms before it repeats.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=None,
            kind="completed",
            extra_env={"STUB_CREATE_FAIL": "1", "STUB_CREATE_LANDS": "1"},
        )

        assert result.returncode == 0, result.stderr.decode()
        creates = (calls / "create-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(creates) == 1, creates
        stdout = result.stdout.decode()
        # The occupancy check read cleanly and found the slot empty, so an id
        # carrying the stamp afterwards is this run's own landed POST.
        assert "the slot was confirmed empty before this run wrote" in stdout
        assert "Published" in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_an_existing_current_head_comment_is_never_duplicated(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The case that makes the FIRST write the dangerous one. A comment for
        # the current head is already on the PR, from a re-run of this lane or a
        # cancelled run whose if:always() step still executed. The caller's own
        # id lookup then errors on all six attempts, so it cannot see that
        # comment and takes the create path. Nothing here fails the write: the
        # POST would succeed and put a SECOND comment for this head in a slot
        # that holds one.
        #
        # That is unrecoverable without a person. Every id lookup in these lanes
        # selects one comment, so a later `/ai-review override` patches whichever
        # it picks while the other keeps its own [BLOCK-MERGE] line, and no run
        # undoes it. So the write re-reads first and declines.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.HEAD),
            kind="completed",
            extra_env={"STUB_COMMENT_FAIL_FIRST": "6"},
        )

        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        stdout = result.stdout.decode()
        # NOTHING was written: no create, not one attempt, and no PATCH either.
        # One comment answers for this lane, so every alternative loses something
        # no run can get back -- a second comment beside it is the one a later
        # human override cannot reach, and overwriting it can discard a verdict
        # for a head this run has no standing to judge.
        assert not (calls / "create-calls.txt").exists(), stdout
        assert not (calls / "patch-calls.txt").exists(), stdout
        assert not (calls / "patched-body.md").exists(), stdout
        # And the report is not a publication. A write-free path that answers
        # success is the same false claim as a silently lost write.
        # An earlier run for this same head filled the slot, which an advisory
        # lane loses nothing it can act on by. Stopping there is a success in
        # THIS lane class -- but it is named as that earlier run's write, never
        # as this run's.
        assert "already carries a verdict for this head" in stdout
        assert "written by an earlier run" in stdout
        assert "#123" in stdout
        assert "Published" not in stdout, stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_superseded_head_comment_is_refused_too(self, lane: dict, tmp_path: Path) -> None:
        # The needle the occupancy check must NOT use is the current head's
        # stamp. The steady state after any earlier review is a comment for an
        # OLDER head -- that comment is the slot, because every lane's id lookup
        # selects on the lane marker and finds it. Matching the current head's
        # stamp would miss it, and the POST would then sit a second comment
        # beside it: a later override patches whichever id the lookup picks,
        # while the other keeps its own [BLOCK-MERGE] line and no run undoes it.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.OLD),
            kind="completed",
            extra_env={"STUB_COMMENT_FAIL_FIRST": "6"},
        )

        # Green, with an error annotation. These lanes are advisory and
        # pr-readiness maps a FAILED lane to a blocking verdict on the required
        # check, so failing here would invent a BLOCK nobody judged from an
        # infrastructure fault. The annotation is what reports the loss, and
        # kirocrew-prepare-pr's marker evaluation is what refuses a PR whose head has no
        # verdict; the required status itself reads only conclusions.
        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        stdout = result.stdout.decode()
        # NOTHING was written: no create, not one attempt, and no PATCH either.
        # The occupant names a SUPERSEDED head, the slot's steady state
        # after any earlier review, and it is refused on the same terms.
        # One comment answers for this lane, so every alternative loses something
        # no run can get back -- a second comment beside it is the one a later
        # human override cannot reach, and overwriting it can discard a verdict
        # for a head this run has no standing to judge.
        assert not (calls / "create-calls.txt").exists(), stdout
        assert not (calls / "patch-calls.txt").exists(), stdout
        assert not (calls / "patched-body.md").exists(), stdout
        # And the report is not a publication. A write-free path that answers
        # success is the same false claim as a silently lost write.
        assert "already holds this lane's slot" in stdout
        assert "#123" in stdout
        assert "Published" not in stdout, stdout
        assert "::error::" in stdout, stdout
        # The annotation carries the remedy, because nothing in CI catches this
        # for an advisory lane: the required readiness status reads check-run
        # conclusions and has no stamp-freshness read, so the reader that sees a
        # head with no verdict is kirocrew-prepare-pr's own marker evaluation.
        assert "re-run this lane" in stdout, stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_concurrent_runs_comment_is_not_reported_as_this_runs_write(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # Same interleaving with the write also failing. The outcome is the same
        # refusal, and the point of the test is the REPORT: the comment in the
        # slot is another run's, so announcing it as this run's publication is
        # the same false "published" claim as the lost update, reached from the
        # other side.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.HEAD),
            kind="completed",
            extra_env={"STUB_COMMENT_FAIL_FIRST": "6", "STUB_CREATE_FAIL": "1"},
        )

        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        stdout = result.stdout.decode()
        assert not (calls / "create-calls.txt").exists(), stdout
        assert not (calls / "patched-body.md").exists(), stdout
        assert "did land" not in stdout
        # The comment in the slot is another run's. Announcing it as this run's
        # publication is the same false claim as a silently lost write, and
        # overwriting it discards that run's verdict, so neither happens.
        # An earlier run for this same head filled the slot, which an advisory
        # lane loses nothing it can act on by. Stopping there is a success in
        # THIS lane class -- but it is named as that earlier run's write, never
        # as this run's.
        assert "already carries a verdict for this head" in stdout
        assert "written by an earlier run" in stdout
        assert "Published" not in stdout, stdout
        assert "#123" in stdout

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_an_unreadable_duplicate_check_refuses_the_write(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The duplicate check is itself an API read on the same exhausted window,
        # so it can fail outright. It then knows neither that the slot is empty
        # nor that it is taken, and both readings are destructive to act on:
        # posting risks the duplicate no run undoes, and claiming publication
        # hides a lost verdict. So it posts nothing and says which cost that
        # buys -- a re-run republishes a verdict, a duplicate needs a person.
        #
        # Six outer lookups plus six duplicate checks, so every read fails.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=self._verdict_body(lane, self.HEAD),
            kind="completed",
            extra_env={"STUB_COMMENT_FAIL_FIRST": "12"},
        )

        # Green, with an error annotation: an unpublishable verdict is an
        # infrastructure fault, and an advisory lane that fails on one is turned
        # into a BLOCK verdict nobody judged.
        assert result.returncode == _missed_slot_rc(lane), result.stderr.decode()
        stdout = result.stdout.decode()
        assert not (calls / "create-calls.txt").exists(), stdout
        assert "did land" not in stdout
        assert "Published" not in stdout
        assert "is unreadable after 6 attempts, so nothing was posted" in stdout
        # The check spent its whole budget before refusing.
        reads = (calls / "comment-reads.txt").read_text(encoding="utf-8").splitlines()
        assert len(reads) == 12, len(reads)

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_first_write_still_happens_when_the_slot_is_provably_empty(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The guard must not cost the ordinary case. A duplicate check that reads
        # cleanly and finds no comment for this head has PROVED the slot empty,
        # so the verdict is posted on the first attempt with no confirmation read
        # in between. Without this, "refuse when unsure" could quietly become
        # "refuse", and a verdict nobody publishes is the original defect.
        calls, result = self._run_step(
            lane,
            tmp_path,
            existing_body=None,
            kind="completed",
        )

        assert result.returncode == 0, result.stderr.decode()
        creates = (calls / "create-calls.txt").read_text(encoding="utf-8").splitlines()
        assert len(creates) == 1, creates
        body = (calls / "created-body.md").read_text(encoding="utf-8")
        assert f"{lane['stamp']} {self.HEAD}" in body
        assert "Published" in result.stdout.decode()


class TestTheGuardedUpsertRecordsWhatReachedTheSlot:
    """``guarded_comment_upsert`` returns 0 on every arm, so it records the outcome.

    The record is the step output ``publication``, and the fork Opus and GPT
    finalize steps pass a verdict only when it says the verdict is in the slot.
    Every arm is driven through each lane's REAL step bash, because the function
    is pinned byte-identical and a record missing from any arm reads as a pass
    lost, not a pass gained.
    """

    HEAD = TestReviewLaneVerdictVisibility.HEAD
    OLD = TestReviewLaneVerdictVisibility.OLD
    NEWER = "bbbb567890abcdef1234567890abcdef1234bbbb"

    def _publication(self, lane: dict, tmp_path: Path, existing: str | None, **extra: str) -> str:
        harness = TestReviewLaneVerdictVisibility()
        body = None if existing is None else harness._verdict_body(lane, existing)
        _, result = harness._run_step(
            lane, tmp_path, existing_body=body, kind="completed", extra_env=extra
        )
        out = tmp_path / "github-output.txt"
        records = [
            line.partition("=")[2]
            for line in out.read_text(encoding="utf-8").splitlines()
            if line.startswith("publication=")
        ]
        assert len(records) == 1, records
        # The record is the function's; what the STEP does with it is the
        # lane's. Advisory lanes exit 0 whatever it says; the fail-closed GPT
        # lane reads `not_published` back and exits on it.
        expected_rc = _missed_slot_rc(lane) if records[0] == "not_published" else 0
        assert result.returncode == expected_rc, result.stderr.decode()
        return records[0]

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_replaced_comment_is_published(self, lane: dict, tmp_path: Path) -> None:
        assert self._publication(lane, tmp_path, self.OLD) == "published"

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_created_comment_is_published(self, lane: dict, tmp_path: Path) -> None:
        assert self._publication(lane, tmp_path, None) == "published"

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_rerun_replaces_an_earlier_sample_for_this_head(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The remedy a `verdict not published` failure names. With the slot's
        # lookup answering, a re-run on the same head takes the replace path,
        # so the earlier sample is overwritten by this run's verdict.
        assert self._publication(lane, tmp_path, self.HEAD) == "published"
        patched = (tmp_path / "calls" / "patched-body.md").read_text(encoding="utf-8")
        assert f"{lane['stamp']} {self.HEAD}" in patched, patched

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_lost_patch_is_not_published(self, lane: dict, tmp_path: Path) -> None:
        assert self._publication(lane, tmp_path, self.OLD, STUB_PATCH_FAIL="1") == "not_published"

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_lost_create_is_not_published(self, lane: dict, tmp_path: Path) -> None:
        assert self._publication(lane, tmp_path, None, STUB_CREATE_FAIL="1") == "not_published"

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_an_unreadable_head_with_a_slot_is_not_published(
        self, lane: dict, tmp_path: Path
    ) -> None:
        assert self._publication(lane, tmp_path, self.OLD, STUB_PR_HEAD="") == "not_published"

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_an_earlier_runs_sample_for_this_head_is_not_this_runs(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The slot carries this head's stamp, but the PR shows that earlier
        # sample, blocking line included, rather than the verdict this run
        # reached.
        assert (
            self._publication(lane, tmp_path, self.HEAD, STUB_COMMENT_FAIL_FIRST="6")
            == "not_published"
        )

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_a_head_the_pr_moved_past_is_superseded(self, lane: dict, tmp_path: Path) -> None:
        assert self._publication(lane, tmp_path, self.OLD, STUB_PR_HEAD=self.NEWER) == "superseded"

    @pytest.mark.parametrize("lane", _GUARDED_LANE_PARAMS)
    def test_an_override_note_a_repeat_stopped_at_is_not_this_runs(
        self, lane: dict, tmp_path: Path
    ) -> None:
        # The repeat stops at any comment carrying an override note, and that
        # note names no head the repeat can check: it may be an older head's.
        # The slot then shows the note and not this run's verdict, so it is no
        # pass for this head; an override that IS for this head reaches the
        # finalize step through its own input.
        assert (
            self._publication(
                lane,
                tmp_path,
                self.OLD,
                STUB_PATCH_FAIL="1",
                STUB_TARGET_BODY="## Lane -- ✅ human override accepted",
            )
            == "not_published"
        )


class TestForkLanePublishesTheOverrideNote:
    """The note replaces a BLOCK the head already shows, through the lane's slot.

    Every lane whose override note goes through the guarded upsert is driven
    here, the same-repo GPT lane included.
    """

    HEAD = TestReviewLaneVerdictVisibility.HEAD
    LANES = [
        pytest.param(lane, id=lane["id"])
        for lane in _GUARDED_LANES
        if lane["id"] in _GUARDED_OVERRIDE_LANES + ("fork-ux",)
    ]

    def _block_body(self, lane: dict) -> str:
        return (
            f"{lane['marker']}\n## Review — 🔴 BLOCK (blocking)\n\n"
            f"Design-Verdict: BLOCK\n\n{lane['stamp']} {self.HEAD}\n"
        )

    def _run(self, lane: dict, tmp_path: Path, existing_body: str | None):
        harness = TestReviewLaneVerdictVisibility()
        return harness._run_step(
            lane,
            tmp_path,
            existing_body=existing_body,
            kind="incomplete",
            extra_env={
                "HUMAN_OVERRIDE": "true",
                "OVERRIDE_ACTOR": "alice",
                "OVERRIDE_SOURCE": "42",
                "UI_SCOPE": "true",
                "HOME": str(tmp_path),
                "TMPDIR": str(tmp_path),
            },
        )

    @pytest.mark.parametrize("lane", LANES)
    def test_the_note_replaces_a_standing_block_for_this_head(self, lane: dict, tmp_path: Path):
        calls, result = self._run(lane, tmp_path, self._block_body(lane))
        assert result.returncode == 0, result.stderr.decode()
        patched = (calls / "patched-body.md").read_text(encoding="utf-8")
        assert patched.startswith(lane["marker"]), patched
        assert "✅ human override accepted" in patched
        assert "@alice" in patched and "#issuecomment-42" in patched
        # No review ran, so the note carries no review stamp for any head.
        assert f"{lane['stamp']} " not in patched, patched
        assert "[BLOCK-MERGE]" not in patched and "Verdict: BLOCK" not in patched
        assert not (calls / "created-body.md").exists()
        # A fork lane's finalize reads the override off this step's own output.
        # The same-repo GPT lane's gate reads it off the human_override step
        # instead, so this step writes no verdict output there.
        if lane["id"].startswith("fork-"):
            assert "verdict=OVERRIDE" in (tmp_path / "github-output.txt").read_text(
                encoding="utf-8"
            )

    @pytest.mark.parametrize("lane", LANES)
    def test_an_empty_slot_gets_the_note(self, lane: dict, tmp_path: Path):
        calls, result = self._run(lane, tmp_path, None)
        assert result.returncode == 0, result.stderr.decode()
        created = (calls / "created-body.md").read_text(encoding="utf-8")
        assert "✅ human override accepted" in created
        assert f"{lane['stamp']} " not in created, created

    def test_the_override_arm_comes_before_every_verdict_write(self) -> None:
        for workflow, step, first in (
            (
                "fork-design-review.yml",
                "Post/update design review comment",
                'guarded_comment_upsert "$MARKER" "[DESIGN-REVIEWED]"',
            ),
            (
                "fork-opus-review.yml",
                "Post/update summary comment",
                'guarded_comment_upsert "$MARKER" "[OPUS-REVIEWED]"',
            ),
            (
                "fork-gpt-review.yml",
                "Post/update summary comment",
                'guarded_comment_upsert "$MARKER" "[GPT-REVIEWED]"',
            ),
            (
                "fork-first-principles-review.yml",
                "Post/update first-principles review comment",
                'guarded_comment_upsert "$MARKER" "[FIRST-PRINCIPLES-REVIEWED]"',
            ),
            ("fork-ux-review.yml", "Post UX review summary", 'if [ "$UI_SCOPE" != "true" ]; then'),
        ):
            script = _step_script(_workflow(workflow), step)
            arm = script.index('if [ "${HUMAN_OVERRIDE:-}" = "true" ]; then')
            assert arm < script.index(first), workflow
