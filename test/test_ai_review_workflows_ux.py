"""Review-workflow tests for the UX lanes: the scope gate, the blind pass that reads
the screenshots first, the evidence attached to the PR description, and lens 0. The
evidence a PR commits to its tree is in ``test_ai_review_workflows_ux_committed_evidence.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    ATTACHMENT_SOURCE_LINE,
    EVIDENCE_STEP,
    FORK_ATTACHMENT_STEP,
    SNAPSHOT_SOURCE_FRAGMENT,
    UX_EVIDENCE_STEP,
    UX_LANES,
    UX_REVIEW_STEP,
    UxEvidenceGateHarness,
    _attachment_script,
    _bash,
    _flat,
    _line_containing,
    _snapshot_script,
    _step,
    _step_env,
    _step_script,
    _workflow,
)


class TestUxLensZeroIsIdenticalInBothLanes:
    """Lens 0 (product coherence) is where the UX lane judges look, information
    architecture, element economy and, since the placement check joined it,
    whether a control sits on the page a user would open to find it. The fork
    lane is the copy that reviews an outside contributor's PR, so a rule that
    lives in one copy only is a rule that does not apply to the PRs it was
    written for. Both copies are pinned to each other, not to a literal, so a
    deliberate rewording lands in both or fails here.
    """

    FIRST = "0. PRODUCT COHERENCE"
    LAST = "1. FIRST-TIME COMPREHENSION"

    def _lens_zero(self, workflow: str) -> str:
        lines = _workflow(workflow).splitlines()
        start = next((i for i, line in enumerate(lines) if self.FIRST in line), None)
        assert start is not None, f"{workflow} carries no lens 0"
        end = next(i for i, line in enumerate(lines[start:], start) if self.LAST in line)
        block = lines[start:end]
        indent = len(block[0]) - len(block[0].lstrip())
        return "\n".join(line[indent:] if line.strip() else "" for line in block)

    def test_both_ux_lanes_carry_an_identical_lens_zero(self) -> None:
        blocks = {name: self._lens_zero(name) for name in UX_LANES}
        reference = blocks[UX_LANES[0]]
        for name, block in blocks.items():
            assert (
                block == reference
            ), f"{name} lens 0 drifted from {UX_LANES[0]}; both UX lanes must carry the same text"

    def test_lens_zero_judges_placement_across_the_whole_app(self) -> None:
        for name in UX_LANES:
            flat = _flat(self._lens_zero(name))
            assert "- PLACEMENT" in flat, name
            # Judged where a user would look, across the app, not inside the
            # one panel the screenshot shows.
            assert "where a user LOOKING FOR IT would go first" in flat, name
            assert "across the whole app, not one panel" in flat, name
            # "The issue asked for it here" is not a design decision.
            assert "is NOT a design decision and is itself a finding" in flat, name


class TestUxScopeGateSurvivesAWideDiff:
    """Execute the ACTUAL UI-detection shell from both UX lanes against a diff
    big enough to expose the pipe-timing bug.

    Under ``pipefail``, ``printf … | grep -q`` reports 141 when the match is
    found early enough that ``grep`` exits while ``printf`` is still writing:
    ``printf`` dies on SIGPIPE and the pipeline's status becomes the writer's.
    The gate then reads a MATCHING diff as "not UI-relevant" and the reviewer
    skips green -- a silent, invisible loss rather than a visible failure.

    Parameterized on the size of the non-matching tail because the defect is
    latent at small sizes (printf finishes before grep exits, status 0) and
    only appears once the write blocks -- which is exactly why it survived
    review and only bites on wide diffs.
    """

    def _gate(self, name: str) -> str:
        script = _step_script(_workflow(name), "Detect UI-relevant changes")
        start = script.index("if grep -qE")
        end = script.index("fi", start)
        return script[start:end] + "fi"

    @pytest.mark.parametrize("lane", UX_LANES)
    @pytest.mark.parametrize("tail_files", [1, 200_000])
    def test_a_ui_change_is_detected_regardless_of_diff_width(
        self, lane: str, tail_files: int, tmp_path: Path
    ) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the scope gate runs only under Bash")
        # The UI file comes FIRST so `grep -q` can answer immediately -- the
        # worst case for the writer, and the one that manufactured 141.
        touched = "website/src/App.tsx\n" + "\n".join(
            f"src/kiro_crew/module_{i}.py" for i in range(tail_files)
        )
        # Via a FILE, not the environment: a 200k-line value blows past the
        # execve argument/environment limit (E2BIG) long before it reaches the
        # gate, and the test would fail on the harness rather than the defect.
        # Both scratch paths live under tmp_path so pytest owns the cleanup.
        listing = tmp_path / "touched.txt"
        listing.write_text(touched)
        github_output = tmp_path / "github_output"
        github_output.touch()  # the Actions runtime pre-creates $GITHUB_OUTPUT
        script = (
            "set -euo pipefail\n"
            'changed="$(cat "$TOUCHED_FILE")"\n' + self._gate(lane) + '\ncat "$GITHUB_OUTPUT"'
        )
        out = subprocess.run(
            [bash, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env={
                **os.environ,
                "TOUCHED_FILE": str(listing),
                "GITHUB_OUTPUT": str(github_output),
            },
        )
        assert out.returncode == 0, f"{lane}: gate exited {out.returncode}: {out.stderr}"
        assert "ui=true" in out.stdout, (
            f"{lane}: a diff touching website/ was classified as not-UI-relevant "
            f"with a {tail_files}-file tail -- the UX review would skip green"
        )

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_a_non_ui_diff_still_skips(self, lane: str, tmp_path: Path) -> None:
        """The fix must not turn the gate into an always-true: a backend-only
        diff still has to skip, or every PR pays for a UX review."""
        bash = _bash()
        if bash is None:
            pytest.skip("the scope gate runs only under Bash")
        touched = "src/kiro_crew/session.py\ndocs/ci/ci-and-reviews.md"
        github_output = tmp_path / "github_output"
        github_output.touch()  # the Actions runtime pre-creates $GITHUB_OUTPUT
        script = (
            "set -euo pipefail\n"
            'changed="$TOUCHED"\n' + self._gate(lane) + '\ncat "$GITHUB_OUTPUT"'
        )
        out = subprocess.run(
            [bash, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env={
                **os.environ,
                "TOUCHED": touched,
                "GITHUB_OUTPUT": str(github_output),
            },
        )
        assert out.returncode == 0, out.stderr
        assert "ui=false" in out.stdout, f"{lane}: backend-only diff was read as UI"

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_the_gate_keeps_the_writer_out_of_the_pipeline(self, lane: str) -> None:
        """Pin the SHAPE, not just the behaviour: the behavioural test above
        needs a 200k-line diff to fail, so a revert to the piped form would
        pass every small-input check and only regress in production."""
        gate = self._gate(lane)
        assert "<<<" in gate, f"{lane}: expected a here-string feeding grep"
        assert "printf" not in gate, f"{lane}: writer is back in the pipeline"


UX_BLIND_STEP = "Blind read of the screenshots (Opus 5.5)"

UX_CAPTURE_STEP = "Capture the blind-read report"


class TestUxReviewReadsTheScreenshotsBlindFirstPipeline:
    """A UX change minimized a banner into a corner chip labelled "Pinned turn".
    Every AI lane PASSed it -- this one wrote "self-teaching ... a visibly
    labelled 'Pinned turn' chip" -- and the product owner could not tell what
    the chip was. The reviewer had read the diff and the description before it
    looked at the pixels, so the author's vocabulary had already primed it; a
    prompt asking it to "imagine an uninformed reader" cannot undo that.

    The fix is structural, not prose: a FIRST model call that can see only the
    committed screenshots, and a SECOND that adjudicates its report against the
    diff. These tests pin the wall between them.
    """

    def _steps(self, lane: str = "ux-review.yml") -> list[dict]:
        doc = yaml.safe_load(_workflow(lane))
        return list(doc["jobs"].values())[0]["steps"]

    def _index(self, steps: list[dict], name: str) -> int:
        for i, step in enumerate(steps):
            if step.get("name") == name:
                return i
        raise AssertionError(f"no step named {name!r}")

    def test_the_blind_pass_can_see_only_the_screenshots(self) -> None:
        with_ = _step("ux-review.yml", UX_BLIND_STEP)["with"]
        args = with_["claude_args"]
        tools = _line_containing(args, "--allowedTools").strip()
        # A PATH-SCOPED Read and nothing else: no shell (no `git diff`, no
        # `gh pr view`), no Grep/Glob (no way to discover the code it is not
        # supposed to read), and no bare `Read` -- an unscoped Read would let a
        # screenshot carrying an injected instruction open /proc/self/environ
        # and pull the job's Bedrock credentials into the transcript. Only the
        # opaque-copy directory and the list file are admitted; the leading
        # `/` before the expression makes the `//`-prefixed absolute-path rule.
        assert tools.startswith('--allowedTools "Read('), tools
        grants = tools[len('--allowedTools "') : -1].split(",")
        assert grants == [
            "Read(/${{ runner.temp }}/ux-blind/**)",
            "Read(/${{ runner.temp }}/ux-screenshots.txt)",
        ], grants
        denied = _line_containing(args, "--disallowedTools")
        for tool in ("Bash", "Grep", "Glob", "WebFetch"):
            assert tool in denied, f"{tool} not denied to the blind reader"
        # The checkout's CLAUDE.md / .claude/ are PR-controlled instructions the
        # action would otherwise auto-load into the blind reader -- a channel
        # that bypasses the wall without a single tool call. Only the runner's
        # (empty) user source may be consulted.
        assert _line_containing(args, "--setting-sources").strip() == "--setting-sources user"
        # The only path it is handed is the screenshot list; the diff, the PR
        # text and the blind-read report are never named to it.
        system = _line_containing(args, "--append-system-prompt")
        assert "ux-screenshots.txt" in system
        # The map back to repository filenames is pass 2's; handing it to the
        # blind reader would reopen the filename leak.
        assert "ux-screenshot-map.txt" not in system
        prompt = _flat(with_["prompt"])
        for leak in ("git diff", "gh pr", "authentic.patch", "ux-blind-read.md", "pull request"):
            assert leak not in prompt, f"blind-read prompt names {leak!r}"
        assert "FIRST time" in prompt
        assert "read no code, no ticket and no description" in prompt
        assert "[UX-BLIND-READ]" in prompt

    def test_the_blind_pass_runs_before_the_review_and_feeds_it_a_data_file(self) -> None:
        steps = self._steps()
        evidence = self._index(steps, UX_EVIDENCE_STEP)
        blind = self._index(steps, UX_BLIND_STEP)
        capture = self._index(steps, UX_CAPTURE_STEP)
        review = self._index(steps, UX_REVIEW_STEP)
        assert evidence < blind < capture < review
        # Pass 2 reads the report the job wrote, not the pass-1 transcript.
        review_args = _step("ux-review.yml", UX_REVIEW_STEP)["with"]["claude_args"]
        system = _line_containing(review_args, "--append-system-prompt")
        assert "ux-blind-read.md" in system
        assert _step_env("ux-review.yml", UX_CAPTURE_STEP)["REPORT"].endswith("/ux-blind-read.md")
        # The verdict the lane publishes is still pass 2's (id: review).
        assert steps[review]["id"] == "review"
        assert steps[blind]["id"] == "blind"

    def test_each_ux_model_call_has_its_own_credential_assume(self) -> None:
        """Same rule the GPT/Opus lanes pin: one AssumeRole session per call,
        strictly interleaved, so pass 2 never inherits a session pass 1 spent."""
        steps = self._steps()
        creds, calls = [], []
        for i, step in enumerate(steps):
            uses = step.get("uses") or ""
            if "configure-aws-credentials" in uses:
                creds.append(i)
            elif "claude-code-action" in uses:
                calls.append(i)
        assert len(calls) == 2, [steps[i].get("name") for i in calls]
        assert len(creds) == 2
        for slot, (call, assume) in enumerate(zip(calls, creds)):
            assert assume < call
            if slot + 1 < len(creds):
                assert call < creds[slot + 1]

    def test_a_failed_blind_read_degrades_to_an_evidence_gap_not_a_red_lane(self) -> None:
        blind = _step("ux-review.yml", UX_BLIND_STEP)
        assert blind.get("continue-on-error") is True
        script = _step_script(_workflow("ux-review.yml"), UX_CAPTURE_STEP)
        # The report is accepted only with the pass-1 marker (a mid-run message
        # is not a report), and every no-report case writes WORDS pass 2 reads
        # as a gap -- never an empty file that reads as "nothing to say".
        assert 'grep -qF "[UX-BLIND-READ]" <<< "$report"' in script
        assert "BLIND READ NOT PERFORMED" in script
        assert "BLIND READ UNAVAILABLE" in script
        # And pass 2 is told what the absence means.
        prompt = _flat(_step("ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "If it says the blind read was not performed or is unavailable" in prompt
        assert "every user-visible control this PR adds or changes is an evidence gap" in prompt

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_review_prompts_carry_no_expression_so_the_21000_cap_cannot_bite(
        self, lane: str
    ) -> None:
        """GitHub rejects the whole workflow file -- zero jobs, no error on the
        PR -- when any expression-bearing string exceeds 21000 characters. Both
        UX prompts sat at ~19000 WITH expressions before these rules were
        added; they now exceed the cap, so the identity must ride in
        `--append-system-prompt` and `prompt:` must stay expression-free."""
        for step in self._steps(lane):
            with_ = step.get("with") or {}
            prompt = with_.get("prompt")
            if not prompt:
                continue
            assert "${{" not in prompt, f"{lane}/{step.get('name')}: prompt carries an expression"
            args = with_["claude_args"]
            for value in args.split("\n"):
                if "${{" in value:
                    assert (
                        len(value) < 2000
                    ), f"{lane}: an expression-bearing claude_args line is {len(value)} chars"
        review = _step(lane, UX_REVIEW_STEP)["with"]
        system = _line_containing(review["claude_args"], "--append-system-prompt")
        assert "HEAD sha ${{" in system, f"{lane}: the HEAD sha is not handed to the reviewer"
        # `#` starting a claude_args line is stripped as a comment by the
        # action's parser; the PR number must not be written as `#N` at a line
        # start, and no claude_args line may begin with `#`.
        for value in review["claude_args"].split("\n"):
            assert not value.strip().startswith(
                "#"
            ), f"{lane}: claude_args line would be stripped as a comment"
        prompt = _flat(review["prompt"])
        assert "[UX-REVIEWED] <HEAD sha>" in prompt
        assert "copied verbatim from your system prompt" in prompt

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_the_five_second_proxy_is_replaced_not_duplicated(self, lane: str) -> None:
        prompt = _flat(_step(lane, UX_REVIEW_STEP)["with"]["prompt"])
        # The old lens asked a primed reviewer to imagine being unprimed. Two
        # copies of the cold-read test -- one real, one imagined -- would let
        # the imagined one PASS what the real one could not.
        assert "Five-second proxy" not in prompt
        assert "SCREENSHOT FIDELITY" not in prompt
        assert "12. BLIND-READ RECONCILIATION & SCREENSHOT EVIDENCE" in prompt
        assert "13. STATE-TRANSITION CONTINUITY" in prompt
        assert "YOU JUDGE THE SURFACE, NOT THE CODE" in prompt
        assert "### Evidence gaps" in prompt
        # An evidence gap is a BLOCK the lane reports as "cannot evaluate":
        # a UI diff with no admissible screenshot, filed as CONCERNS, reads
        # as green in readiness although nobody looked. A hard swap and a
        # misread primary control are the other decidable BLOCKs. A
        # cap-below-PASS wording lets an unevaluated change read green, so
        # it is asserted absent.
        assert "the verdict cannot be PASS" not in prompt
        assert "cannot evaluate: missing" in prompt
        assert "An evidence gap (lens 12 or 13)" in prompt
        assert "or the evidence is incomplete" not in prompt
        assert "flag ? <Chip/> : <Card/>" in prompt
        # Scoped to a persistent, already-identified element: the mechanical
        # predicate must not fire on loading/empty/error conditionals.
        assert "PERSISTENT element the user has already seen" in prompt
        assert "NOT in scope: async lifecycle states" in prompt
        assert "An async lifecycle state is not this exit" in prompt
        # A stochastic "a guess" self-rating is not a BLOCK; a misread is.
        assert '"A guess" alone is not this exit' in prompt
        assert "`prefers-reduced-motion` disables the motion, not the continuity" in prompt
        assert "needs a recording" in prompt
        assert "Pinned turn" in prompt  # the vocabulary-collision example stays concrete

    def test_the_fork_lane_says_it_has_no_blind_read_rather_than_faking_one(self) -> None:
        fork = _workflow("fork-ux-review.yml")
        steps = self._steps("fork-ux-review.yml")
        assert all(step.get("name") != UX_BLIND_STEP for step in steps)
        assert sum("claude-code-action" in (s.get("uses") or "") for s in steps) == 1
        prompt = _flat(_step("fork-ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "NO BLIND READ IN THIS LANE" in prompt
        assert "Read it FIRST" not in prompt
        # The fork head is never checked out, so the fork lane must not claim
        # to hand the reviewer a screenshot list or a report it cannot have.
        system = _line_containing(
            _step("fork-ux-review.yml", UX_REVIEW_STEP)["with"]["claude_args"],
            "--append-system-prompt",
        )
        assert "ux-blind-read.md" not in system
        assert "ux-screenshots.txt" not in system
        assert "authentic.patch" in system
        assert "never applied to the tree" in fork


class TestUxReviewReadsTheScreenshotsBlindFirstDescriptionEvidence(UxEvidenceGateHarness):
    """The evidence step reads what the PR description attaches.

    Screenshots and recordings fetched from the description, the failures that fail
    the step closed, and the caps on what counts as evidence.
    """

    def _steps(self, lane: str = "ux-review.yml") -> list[dict]:
        doc = yaml.safe_load(_workflow(lane))
        return list(doc["jobs"].values())[0]["steps"]

    def test_each_case_gets_its_own_job_temp_not_the_shard_s(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One case is one job, so the harness must not hand two cases the same
        `$RUNNER_TEMP`.

        The evidence step reads the PR description through a snapshot cached
        under `$RUNNER_TEMP`, which is the right scope on a runner and the wrong
        one here: a runner sets that variable job-wide, so an inherited value
        lets case 1's snapshot answer for case 2, whose `gh` stub is then never
        called. That is silent off-runner, where the variable is unset, which is
        why it is pinned rather than left to the next reader to notice.
        """
        monkeypatch.setenv("RUNNER_TEMP", "/the/whole/shard/temp")
        seen: list[str] = []
        for case in ("case-one", "case-two"):
            (tmp_path / case).mkdir()
            got = self._git_env(tmp_path / case)["RUNNER_TEMP"]
            assert got != "/the/whole/shard/temp", "the harness inherited the shard's job temp"
            assert Path(got).is_dir(), got
            assert Path(got).parent == tmp_path / case, got
            seen.append(got)
        assert seen[0] != seen[1], "two cases were handed one job temp"

    def test_the_evidence_step_copies_regular_images_under_opaque_names(
        self, tmp_path: Path
    ) -> None:
        """Execute the ACTUAL evidence script. The blind reader is handed
        copies named shot-NN.<ext>, never the repository paths: an author-named
        `pinned-turn-chip.png` would prime it with the exact word the wall
        hides. A tracked symlink under temp-screenshots/ would let a PR point
        the reader -- which has only the Read tool -- at any file on the runner;
        recordings are listed for the continuity lens and a GIF counts as both."""
        if os.name == "nt":
            pytest.skip("symlink creation needs privileges on Windows")
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "README").write_text("base\n")
        self._git(repo, "add", "README")
        self._git(repo, "commit", "-qm", "base")
        base = self._git(repo, "rev-parse", "HEAD")
        shots = repo / "temp-screenshots" / "f"
        shots.mkdir(parents=True)
        (shots / "pinned-turn-chip.png").write_bytes(b"\x89PNG")
        (shots / "restore.GIF").write_bytes(b"GIF89a")
        (shots / "c.webm").write_bytes(b"\x1a\x45")
        (shots / "link.png").symlink_to(repo / "README")
        (repo / "website").mkdir()
        (repo / "website" / "App.tsx").write_text("x")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "head")

        shot_list, shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo, base, tmp_path
        )
        # Opaque, order-numbered, extension kept (lower-cased) -- and nothing
        # of the author's naming survives into what pass 1 can see.
        assert shot_list.splitlines() == [
            f"{blind_dir.as_posix()}/shot-01.png",
            f"{blind_dir.as_posix()}/shot-02.gif",
        ]
        assert "pinned-turn" not in shot_list
        assert sorted(p.name for p in blind_dir.iterdir()) == ["shot-01.png", "shot-02.gif"]
        assert (blind_dir / "shot-01.png").read_bytes() == b"\x89PNG"
        # Pass 2 gets the map back to the repository paths.
        assert shot_map.splitlines() == [
            "shot-01.png\ttemp-screenshots/f/pinned-turn-chip.png",
            "shot-02.gif\ttemp-screenshots/f/restore.GIF",
        ]
        # `git diff --name-only` orders by path, so the recording list is
        # byte-ordered too: c.webm sorts before restore.GIF.
        assert clip_list.splitlines() == [
            "temp-screenshots/f/c.webm",
            "temp-screenshots/f/restore.GIF",
        ]
        assert "screens=true" in output

    def test_the_evidence_step_reports_no_screens_for_a_code_only_ui_change(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "README").write_text("base\n")
        self._git(repo, "add", "README")
        self._git(repo, "commit", "-qm", "base")
        base = self._git(repo, "rev-parse", "HEAD")
        (repo / "website").mkdir()
        (repo / "website" / "App.tsx").write_text("x")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "head")
        shot_list, shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo, base, tmp_path
        )
        assert shot_list == "" and shot_map == "" and clip_list == ""
        assert list(blind_dir.iterdir()) == []
        assert "screens=false" in output
        # ...and the blind pass is gated on that output, so no model call is
        # spent reading nothing.
        blind = _step("ux-review.yml", UX_BLIND_STEP)
        assert "steps.evidence.outputs.screens == 'true'" in str(blind["if"])

    def test_every_download_failing_fails_the_same_repo_evidence_step(self, tmp_path: Path) -> None:
        """Execute the ACTUAL evidence step against a description whose one
        attachment fails to download for a transport reason. No image reached
        the blind reader and nothing the author supplied could be judged, for
        a reason a re-run can change: that is the lane's own failure, and a
        lane that could not evaluate the change must not read as advisory --
        so the step FAILS the run (red check, readiness holds, re-run is the
        remedy), the way a hard model-step error already does, instead of
        asking pass 2 to cap the verdict at CONCERNS, which readiness would
        score green. The `unfetched` output is written before the exit so the
        posting step can name the cause. Anything the author chose -- no
        attachment at all, or attachments that downloaded but are recordings
        or non-images -- reaches the capture step as NOT PERFORMED and pass 2
        blocks on it, because a re-run cannot conjure a still from a video."""
        repo, base = self._code_only_ui_repo(tmp_path)
        gone = "https://github.com/user-attachments/assets/0f3b2c1a-5555-4bcd-9e8f-0123456789ab"
        shot_list, _shot_map, _clips, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"![after]({gone})\n",
            fixtures={gone: "FAIL:22"},
            expect_failure=True,
        )
        assert shot_list == "" and list(blind_dir.iterdir()) == []
        assert "screens=false" in output
        assert "unfetched=true" in output
        assert f"SKIPPED (download failed): {gone}" in self._evidence_stdout
        assert (
            "::error::1 of 1 attachment download(s) from the PR description failed for a "
            "transport reason, so the blind reader cannot see everything the author supplied; "
            "this lane cannot evaluate the change on partial evidence and fails rather than read "
            "as advisory. Re-run this workflow"
        ) in self._evidence_stdout, self._evidence_stdout
        # The capture step never sees this case, so it has no fetch-failure
        # branch: with no image it writes NOT PERFORMED, and only a pass 1 that
        # ran on admitted images and failed is UNAVAILABLE.
        capture = _step("ux-review.yml", UX_CAPTURE_STEP)
        assert "UNFETCHED" not in capture["env"]
        script = _step_script(_workflow("ux-review.yml"), UX_CAPTURE_STEP)
        assert 'elif [ "$SCREENS" != "true" ]; then' in script
        assert "$UNFETCHED" not in script
        assert "could not download" not in script
        # The posting step names the cause and the remedy in the PR comment.
        post = _step("ux-review.yml", "Post UX review summary")
        assert post["env"]["UNFETCHED"] == "${{ steps.evidence.outputs.unfetched }}"
        post_script = _step_script(_workflow("ux-review.yml"), "Post UX review summary")
        assert 'if [ "${UNFETCHED:-}" = "true" ]; then' in post_script
        assert "could not evaluate" in post_script
        assert "The evidence step failed the run, so PR readiness holds" in post_script
        self._assert_unfetched_comment_is_reachable_and_published(post_script)
        # The gate step reddens for the stated reason instead of printing the
        # verdict-less "NOT blocking" warning under a job that is already red.
        gate = _step("ux-review.yml", "UX review status (gates on BLOCK)")
        assert gate["env"]["UNFETCHED"] == "${{ steps.evidence.outputs.unfetched }}"
        assert 'if [ "${UNFETCHED:-}" = "true" ]; then' in gate["run"]
        assert "::error::UX review for $HEAD could not evaluate the change" in gate["run"]
        assert gate["run"].index('if [ "${UNFETCHED:-}" = "true" ]; then') < gate["run"].index(
            'case "$VERDICT" in'
        )
        bash = _bash()
        if bash is None:
            pytest.skip("the capture step runs only under Bash")

        def capture_report(screens: str, blind_outcome: str) -> str:
            report = tmp_path / f"blind-{screens}-{blind_outcome}.md"
            run = subprocess.run(
                [bash, "-c", script],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={
                    **os.environ,
                    "EXEC_FILE": "",
                    "BLIND_OUTCOME": blind_outcome,
                    "SCREENS": screens,
                    "REPORT": str(report),
                },
            )
            assert run.returncode == 0, run.stderr
            return report.read_text(encoding="utf-8")

        not_performed = capture_report("false", "skipped")
        assert not_performed.startswith("BLIND READ NOT PERFORMED:")
        assert "UNAVAILABLE" not in not_performed
        unavailable = capture_report("true", "failure")
        assert unavailable.startswith("BLIND READ UNAVAILABLE:")
        assert "outcome 'failure'" in unavailable
        assert "NOT PERFORMED" not in unavailable
        # Pass 2 is told what each header means for the verdict, and the
        # fetch-failure case is not among them: it never reaches pass 2.
        prompt = _flat(_step("ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "BLIND READ NOT PERFORMED means the PR supplied no admissible image" in prompt
        assert (
            "BLIND READ UNAVAILABLE means the lane itself failed on evidence the PR supplied"
            in (prompt)
        )
        assert "images admitted and pass 1 itself failed" in prompt
        assert "an attachment download did not complete" not in prompt
        assert "a UI change with a video and no still is missing its stills" in prompt

    def test_the_evidence_step_reports_unfetched_false_when_nothing_was_offered(
        self, tmp_path: Path
    ) -> None:
        repo, base = self._code_only_ui_repo(tmp_path)
        _shots, _shot_map, _clips, output, _blind_dir = self._run_evidence_gate(
            repo, base, tmp_path, body="No screenshots here.\n", fixtures={}
        )
        assert "screens=false" in output
        assert "unfetched=false" in output

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_a_recording_only_description_is_missing_evidence_not_a_fetch_failure(
        self, tmp_path: Path, lane: str
    ) -> None:
        """A UI PR whose only attachments downloaded fine but are a video and a
        text file has no still for the blind reader. That is the author's
        choice of evidence, so it must NOT read as a transport failure: the
        same-repo step reports unfetched=false (capture then writes NOT
        PERFORMED, which pass 2 blocks on) and the fork step leaves the image
        list empty with no UNAVAILABLE sentinel. Routing this to UNAVAILABLE
        would let a screenshot-less UI change pass readiness unevaluated."""
        repo, base = self._code_only_ui_repo(tmp_path)
        webm = "https://github.com/user-attachments/assets/0f3b2c1a-3333-4bcd-9e8f-0123456789ab"
        text = "https://github.com/user-attachments/assets/0f3b2c1a-4444-4bcd-9e8f-0123456789ab"
        shot_list, _shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"{webm}\n\n![notes]({text})\n",
            fixtures={webm: self.WEBM, text: self.TEXT},
            lane=lane,
        )
        assert shot_list == "" and list(blind_dir.iterdir()) == []
        assert clip_list.splitlines() == [webm]
        assert "UNAVAILABLE" not in shot_list
        if lane == "ux-review.yml":
            assert "screens=false" in output
            assert "unfetched=false" in output
        assert "SKIPPED (download failed)" not in self._evidence_stdout

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_a_404_attachment_is_the_authors_url_not_a_fetch_failure(
        self, tmp_path: Path, lane: str
    ) -> None:
        """A stale, deleted or fabricated attachment URL answers 404. That is
        the author's evidence problem -- no re-run changes what the asset
        host says -- so it must not count as a transport failure: the
        step exits 0 with unfetched=false in both lanes (NOT PERFORMED, and
        the reviewer blocks). A 5xx or a throttle (403/429) stays a transport
        failure, since a re-run can clear it: both lanes fail the run on it,
        and the fork lane's Finalize step turns that into a failed check-run."""
        repo, base = self._code_only_ui_repo(tmp_path)
        missing = "https://github.com/user-attachments/assets/0f3b2c1a-9999-4bcd-9e8f-0123456789ab"
        shot_list, _shot_map, _clips, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"![after]({missing})\n",
            fixtures={missing: "HTTP:404"},
            lane=lane,
        )
        assert shot_list == "" and list(blind_dir.iterdir()) == []
        assert (
            f"SKIPPED (HTTP 404, the attachment URL does not resolve to an asset): {missing}"
            in (self._evidence_stdout)
        )
        assert "SKIPPED (download failed)" not in self._evidence_stdout
        assert "unfetched=false" in output
        if lane == "ux-review.yml":
            assert "screens=false" in output
        # ...whereas a 503 is transport, and a re-run is the remedy.
        throttled = (
            "https://github.com/user-attachments/assets/0f3b2c1a-aaaa-4bcd-9e8f-0123456789ab"
        )
        shot_list, _shot_map, _clips, output, _blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"![after]({throttled})\n",
            fixtures={throttled: "HTTP:503"},
            lane=lane,
            expect_failure=True,
        )
        assert f"SKIPPED (download failed): {throttled}" in self._evidence_stdout
        assert shot_list == ""
        assert "unfetched=true" in output
        assert "::error::1 of 1 attachment download(s) from the PR description failed" in (
            self._evidence_stdout
        )

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_a_partial_fetch_fails_the_step_like_a_total_one(
        self, tmp_path: Path, lane: str
    ) -> None:
        """Two attachments, one kept and one 503. The reviewer would see one
        image the author attached and not the other, so a control shown only
        in the failed one would read as an author-closable gap -- unless the
        prompt were handed a rule about which controls to exempt, and a rule
        the prompt applies is one it can misapply. So ANY transport failure
        fails the evidence step, exactly like the all-failed case: red check,
        readiness holds, re-run is the remedy. Neither prompt carries a
        partial-fetch exemption, and the map carries no failure row -- the
        reviewer never sees a partial fetch."""
        repo, base = self._code_only_ui_repo(tmp_path)
        ok = "https://github.com/user-attachments/assets/0f3b2c1a-1111-4bcd-9e8f-0123456789ab"
        down = "https://github.com/user-attachments/assets/0f3b2c1a-bbbb-4bcd-9e8f-0123456789ab"
        shot_list, shot_map, _clips, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"![before]({ok})\n![after]({down})\n",
            fixtures={ok: self.PNG, down: "HTTP:503"},
            lane=lane,
            expect_failure=True,
        )
        stem = "shot" if lane == "ux-review.yml" else "attachment"
        assert shot_list.splitlines() == [f"{blind_dir.as_posix()}/{stem}-01.png"]
        assert shot_map.splitlines() == [f"{stem}-01.png\t{ok}"]
        assert "DOWNLOAD-FAILED" not in shot_map
        assert "unfetched=true" in output
        if lane == "ux-review.yml":
            assert "screens=true" in output
        assert "::error::1 of 2 attachment download(s) from the PR description failed" in (
            self._evidence_stdout
        )
        prompt = _flat(_step(lane, UX_REVIEW_STEP)["with"]["prompt"])
        assert "DOWNLOAD-FAILED" not in prompt
        assert "partial fetch" in prompt
        assert "fails the evidence step" in prompt

    def test_all_four_evidence_lanes_admit_the_same_two_evidence_classes(self) -> None:
        """The admissibility predicate is spelled in four prompts. This pins
        the two classes every one of them admits -- a github.com
        user-attachments asset in the description, or an image committed at
        HEAD -- and that none admits a third, so a lane cannot drift into
        accepting a raw URL pinned to a commit outside the PR, which is the
        shape that let a screenshot-less UI change through."""
        for lane in (*UX_LANES, "design-review.yml", "fork-design-review.yml"):
            prompt = (
                _flat(_step(lane, UX_REVIEW_STEP)["with"]["prompt"])
                if lane in UX_LANES
                else _flat(
                    next(
                        s
                        for s in yaml.safe_load(_workflow(lane))["jobs"][lane[: -len(".yml")]][
                            "steps"
                        ]
                        if (s.get("with") or {}).get("prompt")
                    )["with"]["prompt"]
                )
            )
            assert "github.com/user-attachments" in prompt, lane
            assert "committed" in prompt and "HEAD" in prompt, lane
            assert (
                "hosted off a commit outside this PR" in prompt or "pinned to a commit" in prompt
            ), lane
            assert "raw.githubusercontent" not in prompt, lane

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_a_recording_that_downloaded_beside_a_failed_still_fails_the_step(
        self, tmp_path: Path, lane: str
    ) -> None:
        """A video downloaded fine and the one still 403'd. The recording is
        listed, the still is a transport failure, and the step fails on it:
        the reviewer is not asked to judge the stills on the strength of a
        recording. A 403 counts as transport, since GitHub throttles with it;
        the count in the annotation is downloads, so the recording is one of
        the two."""
        repo, base = self._code_only_ui_repo(tmp_path)
        webm = "https://github.com/user-attachments/assets/0f3b2c1a-3333-4bcd-9e8f-0123456789ab"
        still = "https://github.com/user-attachments/assets/0f3b2c1a-cccc-4bcd-9e8f-0123456789ab"
        shot_list, shot_map, clip_list, output, _blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"{webm}\n\n![after]({still})\n",
            fixtures={webm: self.WEBM, still: "HTTP:403"},
            lane=lane,
            expect_failure=True,
        )
        assert shot_list == ""
        assert clip_list.splitlines() == [webm]
        assert shot_map == ""
        assert f"SKIPPED (download failed): {still}" in self._evidence_stdout
        assert "unfetched=true" in output
        assert "::error::1 of 2 attachment download(s) from the PR description failed" in (
            self._evidence_stdout
        )
        if lane == "ux-review.yml":
            assert "screens=false" in output

    def test_the_fork_lane_fails_an_all_failed_fetch_and_reddens_its_check_run(
        self, tmp_path: Path
    ) -> None:
        """An errored fork run resolves NEUTRAL, which pr-readiness scores as a
        pass -- so for the fork lane, failing the evidence step alone would hold
        nothing. The step still fails (no model call on nothing), and writes
        `unfetched=true` first; the Finalize step reads that output and
        completes the check-run as `failure` -- the conclusion readiness already
        scores as a blocker for this lane -- with a title that names the re-run
        as the remedy. The fork reviewer therefore never sees an all-failed
        fetch, and its prompt has no sentinel exception to misapply: an empty
        image list is always the author's gap."""
        repo, base = self._code_only_ui_repo(tmp_path)
        gone = "https://github.com/user-attachments/assets/0f3b2c1a-5555-4bcd-9e8f-0123456789ab"
        shot_list, _shot_map, _clips, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"![after]({gone})\n",
            fixtures={gone: "FAIL:22"},
            lane="fork-ux-review.yml",
            expect_failure=True,
        )
        assert shot_list == ""
        assert "UNAVAILABLE" not in shot_list
        assert list(blind_dir.iterdir()) == []
        assert "unfetched=true" in output
        assert (
            "::error::1 of 1 attachment download(s) from the PR description failed for a "
            "transport reason, so the reviewer cannot see everything the author supplied"
        ) in self._evidence_stdout, self._evidence_stdout
        workflow = _workflow("fork-ux-review.yml")
        finalize = _step("fork-ux-review.yml", "Finalize check-run (advisory)")
        assert finalize["env"]["UNFETCHED"] == "${{ steps.attachments.outputs.unfetched }}"
        script = finalize["run"]
        assert 'if [ "${UNFETCHED:-}" = "true" ]; then' in script
        assert (
            'conclusion="failure"; title="cannot evaluate — attachment download(s) failed; '
            're-run this workflow"'
        ) in script
        # The verdict-driven mapping is untouched underneath: CONCERNS stays
        # neutral and an incomplete run stays neutral.
        assert 'conclusion="neutral"; title="CONCERNS — read the Watch items"' in script
        assert 'conclusion="neutral"; title="review incomplete (advisory)"' in script
        post = _step("fork-ux-review.yml", "Post UX review summary")
        assert post["env"]["UNFETCHED"] == "${{ steps.attachments.outputs.unfetched }}"
        assert "could not evaluate" in post["run"]
        assert "The check-run is completed as a failure, so PR readiness holds" in post["run"]
        self._assert_unfetched_comment_is_reachable_and_published(post["run"])
        # pr-readiness scores a fork UX `failure` as a blocker already; the
        # lane only has to reach that conclusion.
        readiness = _workflow("pr-readiness.yml")
        assert 'failed+=("$label (BLOCK)")' in readiness
        prompt = _flat(_step("fork-ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "UNAVAILABLE:" not in prompt
        assert "An empty image list is the author's gap, never the lane's" in prompt
        assert "the workflow fails the run on it" in prompt
        assert "missing its stills, and that blocks" in prompt
        assert "the shape TRUNCATED already uses" not in workflow

    @staticmethod
    def _assert_unfetched_comment_is_reachable_and_published(post_script: str) -> None:
        """A failed evidence step leaves no transcript, so the post step's
        "nothing to post" exit would return before the UNFETCHED comment is
        built and leave a stale comment in place. The exit is guarded on the
        output, the comment is built after it, and it carries the head stamp
        guarded_comment_upsert requires -- without the stamp the upsert
        withholds the notice whenever a comment already exists, which is
        exactly the stale-comment case the notice is for."""
        early = 'if [ -z "$summary" ] && [ "${UNFETCHED:-}" != "true" ]; then'
        branch = 'if [ "${UNFETCHED:-}" = "true" ]; then'
        assert early in post_script
        assert post_script.index(early) < post_script.index(branch)
        stamp = post_script.index('echo "[UX-REVIEWED] $HEAD"')
        assert post_script.index(branch) < stamp < post_script.index("guarded_comment_upsert ")
        # An empty transcript does not trip the stale-marker log line, and the
        # verdict header is parsed only when there is a transcript: a grep that
        # matches nothing exits 1, and under pipefail plus the runner's default
        # -e a failed command substitution would abort the step before the
        # notice is posted. The parse itself tolerates a header-less transcript
        # for the same reason.
        assert 'if [ -n "$summary" ] && ! grep -qF "[UX-REVIEWED] $HEAD"' in post_script
        parse = post_script.index("{ grep -iE '^UX-Verdict:' || true; }")
        assert post_script.index('if [ -n "$summary" ]; then') < parse < post_script.index(branch)
        assert post_script.index('if [ -n "$summary" ]; then') > post_script.index(early)

    def _code_only_ui_repo(self, tmp_path: Path) -> tuple[Path, str]:
        """A repository whose head touches website/ and commits no image, so
        every screenshot the step finds has to come from the PR description."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "README").write_text("base\n")
        self._git(repo, "add", "README")
        self._git(repo, "commit", "-qm", "base")
        base = self._git(repo, "rev-parse", "HEAD")
        (repo / "website").mkdir()
        (repo / "website" / "App.tsx").write_text("x")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "head")
        return repo, base

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_the_evidence_step_downloads_attachments_from_the_pr_description(
        self, tmp_path: Path, lane: str
    ) -> None:
        """Execute the ACTUAL evidence script against a PR body. Evidence lives
        in the description as GitHub attachments, so the step has to fetch
        them into the same opaque pipeline a committed file goes through: an
        image becomes shot-NN.<ext> with the URL, alt text and body order
        hidden from the blind reader; a video is listed for the continuity
        lens; a GIF is both. The body is untrusted, so only GitHub's asset
        hosts may be fetched (a look-alike host and a repository-scoped asset
        path, a shape gh does not emit, are never contacted), the same URL is
        fetched once, the download
        carries no credential, and the type comes from the bytes: a text
        payload and a URL that does not resolve to an asset (404) are each
        logged and skipped, never fatal -- only a transport failure is."""
        repo, base = self._code_only_ui_repo(tmp_path)
        shots = repo / "temp-screenshots" / "f"
        shots.mkdir(parents=True)
        (shots / "pinned-turn-chip.png").write_bytes(b"\x89PNG")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "committed screenshot")
        png = "https://github.com/user-attachments/assets/0f3b2c1a-1111-4bcd-9e8f-0123456789ab"
        jpeg = "https://github.com/user-attachments/assets/0f3b2c1a-2222-4bcd-9e8f-0123456789ab"
        webm = "https://github.com/user-attachments/assets/0f3b2c1a-3333-4bcd-9e8f-0123456789ab"
        text = "https://github.com/user-attachments/assets/0f3b2c1a-4444-4bcd-9e8f-0123456789ab"
        gone = "https://github.com/user-attachments/assets/0f3b2c1a-5555-4bcd-9e8f-0123456789ab"
        gif = "https://github.com/user-attachments/assets/0f3b2c1a-8888-4bcd-9e8f-0123456789ab"
        other_repo = "https://github.com/other/repo/assets/1/deadbeef-6666-4000-8000-000000000006"
        look_alike = "https://evil.example.com/user-attachments/assets/0f3b2c1a-7777-4bcd"
        body = (
            "Pinned turn chip, before and after:\n"
            f"![pinned turn chip]({png})\n"
            f'<img src="{jpeg}" width="400">\n\n'
            f"{webm}\n\n"
            f"{png}\n"
            f"![note]({text})\n"
            f"![gone]({gone})\n"
            f"![other]({other_repo})\n"
            f"![evil]({look_alike})\n"
            f"![restore]({gif})\n"
        )
        shot_list, shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=body,
            lane=lane,
            fixtures={
                png: self.PNG,
                jpeg: self.JPEG,
                webm: self.WEBM,
                text: self.TEXT,
                gone: "HTTP:404",
                gif: self.GIF,
                other_repo: self.PNG,
                look_alike: self.PNG,
            },
        )
        # Description first, in body order, then -- in the same-repo lane,
        # which has a checkout -- the committed file; every copy is opaque
        # and carries only the format the bytes earned. The fork lane has no
        # checkout, so its list is the description alone.
        prefix = blind_dir.as_posix()
        stem = "shot" if lane == "ux-review.yml" else "attachment"
        names = [f"{stem}-01.png", f"{stem}-02.jpg", f"{stem}-03.gif"]
        origins = [png, jpeg, gif]
        if lane == "ux-review.yml":
            names.append("shot-04.png")
            origins.append("temp-screenshots/f/pinned-turn-chip.png")
        assert shot_list.splitlines() == [
            f"{prefix}/{name}" for name in names
        ], self._evidence_stdout
        assert "pinned" not in shot_list and "github.com" not in shot_list
        assert sorted(p.name for p in blind_dir.iterdir()) == names
        assert (blind_dir / names[0]).read_bytes() == self.PNG
        # Pass 2 gets each copy's origin: the URL for an attachment, the
        # repository path for a committed file. Neither skip leaves a row: the
        # text payload downloaded and just is not an image, and the 404 is the
        # author's URL, not evidence.
        expected_map = [f"{name}\t{origin}" for name, origin in zip(names, origins)]
        assert shot_map.splitlines() == expected_map
        assert clip_list.splitlines() == [webm, gif]
        if lane == "ux-review.yml":
            assert "screens=true" in output
        # Skips are logged, per URL, and the step still succeeded.
        assert f"SKIPPED (mime text/plain): {text}" in self._evidence_stdout
        assert f"SKIPPED (HTTP 404, the attachment URL does not resolve to an asset): {gone}" in (
            self._evidence_stdout
        )
        # Both skips are annotations the author sees on the run, not bare log
        # lines, and the summary reconciles: 6 attempted = kept + 2 skipped.
        assert "::warning::SKIPPED (mime text/plain)" in self._evidence_stdout
        # Four of six downloads reached the reviewer, so this is not the
        # all-skipped case and no error annotation is raised.
        assert "::error::" not in self._evidence_stdout
        assert "6 attachment URL(s) matched the allowlist, 6 download(s) attempted, 2 skipped" in (
            self._evidence_stdout
        )
        # The summary also counts what reached the reviewer from the
        # description: three images kept, two recordings listed (the GIF is
        # both); the same-repo lane adds its committed file after this line.
        assert (
            "2 skipped, 3 image(s) kept, 2 recording(s) listed (bytes not kept"
            in self._evidence_stdout
        )
        # Only the allowlisted hosts were contacted, each URL once, in body
        # order -- the look-alike host and the foreign repository never were.
        calls = self._curl_calls()
        assert [call[-1] for call in calls] == [png, jpeg, webm, text, gone, gif]
        for call in calls:
            joined = " ".join(call)
            assert "-H" not in call and "--header" not in call, joined
            assert "authorization" not in joined.lower(), joined
            assert "-o" in call and "--proto" in call and "=https" in call, joined
            assert "--max-filesize" in call and "104857600" in call, joined

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_a_transient_api_failure_is_retried_and_the_evidence_still_collected(
        self, tmp_path: Path, lane: str
    ) -> None:
        """A 5xx or a rate limit on the description read is a transient, not a
        verdict: the read is retried (bounded), the run log says so, and the
        attachments are collected exactly as on a clean read. Three failures
        fail the step closed with an error annotation, because an empty body
        would read as no evidence at all."""
        repo, base = self._code_only_ui_repo(tmp_path)
        url = "https://github.com/user-attachments/assets/0f3b2c1a-0001-4bcd-9e8f-0123456789ab"
        shot_list, shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"![shot]({url})\n",
            fixtures={url: self.PNG},
            lane=lane,
            gh_fail_first=2,
        )
        assert self._gh_calls == 3, self._evidence_stdout
        assert (
            self._evidence_stdout.count("Reading the PR title and description failed on attempt")
            == 2
        )
        assert [p.name for p in blind_dir.iterdir()] == [
            "shot-01.png" if lane == "ux-review.yml" else "attachment-01.png"
        ]
        assert "1 attachment URL(s) matched the allowlist, 1 download(s) attempted, 0 skipped" in (
            self._evidence_stdout
        )
        assert "::error::" not in self._evidence_stdout

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_three_api_failures_fail_the_step_closed_not_as_no_evidence(
        self, tmp_path: Path, lane: str
    ) -> None:
        repo, base = self._code_only_ui_repo(tmp_path)
        shot_list, shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body="![shot](https://github.com/user-attachments/assets/0f3b2c1a-0001-4bcd-9e8f-0123456789ab)\n",
            lane=lane,
            gh_fail_first=3,
            expect_failure=True,
        )
        assert self._gh_calls == 3, self._evidence_stdout
        assert (
            "::error::Could not read this PR's description after 3 attempts"
            in self._evidence_stdout
        ), self._evidence_stdout
        # Nothing was fetched or listed, and no summary line pretends otherwise.
        assert list(blind_dir.iterdir()) == [] and shot_list == "" and clip_list == ""
        assert "attachment URL(s) matched the allowlist" not in self._evidence_stdout

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_every_download_skipped_is_an_error_annotation_not_a_quiet_gap(
        self, tmp_path: Path, lane: str
    ) -> None:
        """When every attempted download is skipped the reviewer is about to
        judge with no evidence at all -- the shape a moved asset host or a broken
        egress allowlist takes -- so the step says so once, as an error annotation
        on the run, instead of leaving only per-URL warnings behind."""
        repo, base = self._code_only_ui_repo(tmp_path)
        urls = [
            f"https://github.com/user-attachments/assets/0f3b2c1a-{i:04d}-4bcd-9e8f-0123456789ab"
            for i in range(2)
        ]
        shot_list, shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body="".join(f"![shot]({url})\n" for url in urls),
            fixtures={urls[0]: "FAIL:22", urls[1]: "FAIL:6"},
            lane=lane,
            expect_failure=True,
        )
        assert shot_map == ""
        assert clip_list == ""
        assert list(blind_dir.iterdir()) == []
        # No image reached the reviewer, and it is the transport that failed:
        # both lanes record that as unfetched and FAIL the run -- a lane that
        # could not evaluate must not read as advisory; readiness holds and a
        # re-run is the remedy. The same-repo job's red is the signal; the
        # fork lane's Finalize step turns the output into a failed check-run,
        # because its errored run would otherwise resolve neutral.
        assert shot_list == ""
        assert "unfetched=true" in output
        if lane == "ux-review.yml":
            assert "screens=false" in output
        assert (
            "::error::Every one of the 2 attachment download(s) was skipped; no evidence "
            "from the PR description reached the reviewer."
        ) in self._evidence_stdout, self._evidence_stdout
        # The shared script's annotation plus the step's own failure annotation
        # that names the remedy.
        assert "::error::2 of 2 attachment download(s) from the PR description failed" in (
            self._evidence_stdout
        )
        assert self._evidence_stdout.count("::error::") == 2
        assert "2 attachment URL(s) matched the allowlist, 2 download(s) attempted, 2 skipped" in (
            self._evidence_stdout
        )

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_description_recordings_are_capped_and_never_count_as_screens(
        self, tmp_path: Path, lane: str
    ) -> None:
        """Videos are listed, not opened, so the cap on them only bounds the
        downloads -- and a description carrying only videos leaves the blind
        reader with nothing to look at, so it must not turn the blind pass on."""
        repo, base = self._code_only_ui_repo(tmp_path)
        cap = int(_step_env(lane, EVIDENCE_STEP[lane])["MAX_CLIPS"])
        urls = [
            f"https://github.com/user-attachments/assets/0f3b2c1a-{i:04d}-4bcd-9e8f-0123456789ab"
            for i in range(cap + 1)
        ]
        shot_list, shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body="".join(f"{url}\n\n" for url in urls),
            fixtures={url: self.WEBM for url in urls},
            lane=lane,
        )
        assert clip_list.splitlines() == urls[:cap] + [
            f"TRUNCATED: more than {cap} recordings in the PR description; one was not listed"
        ], self._evidence_stdout
        assert shot_list == "" and shot_map == ""
        assert list(blind_dir.iterdir()) == []
        if lane == "ux-review.yml":
            assert "screens=false" in output

    @pytest.mark.parametrize("lane", UX_LANES)
    def test_description_images_share_the_committed_cap_and_downloads_are_bounded(
        self, tmp_path: Path, lane: str
    ) -> None:
        """MAX_SHOTS is one cap for both sources -- past it an image is written
        into the lists as TRUNCATED so pass 2 knows its evidence is partial --
        and once both caps are spent the step stops downloading altogether, so
        a description cannot keep the job fetching."""
        repo, base = self._code_only_ui_repo(tmp_path)
        urls = [
            f"https://github.com/user-attachments/assets/0f3b2c1a-{i:04d}-4bcd-9e8f-0123456789ab"
            for i in range(3)
        ]
        shot_list, shot_map, clip_list, output, blind_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body="".join(f"![shot]({url})\n" for url in urls),
            fixtures={url: self.PNG for url in urls},
            max_shots="1",
            max_clips="1",
            lane=lane,
        )
        first = "shot-01.png" if lane == "ux-review.yml" else "attachment-01.png"
        assert shot_list.splitlines() == [
            f"{blind_dir.as_posix()}/{first}",
            "TRUNCATED: more than 1 images; one was not listed",
        ], self._evidence_stdout
        assert shot_map.splitlines() == [f"{first}\t{urls[0]}", f"TRUNCATED\t{urls[1]}"]
        assert clip_list == ""
        if lane == "ux-review.yml":
            assert "screens=true" in output
        # Two caps of one each bound the downloads at two; the third URL is
        # named in the log as not fetched and curl never saw it.
        assert [call[-1] for call in self._curl_calls()] == urls[:2]
        assert f"not fetched: {urls[2]}" in self._evidence_stdout

    def test_the_pr_description_reaches_the_evidence_step_only_as_grep_input(self) -> None:
        """The description is author-controlled text. It enters the step as an
        environment value (never spliced into the script by an expression) and
        the script reads it exactly once, as a here-string into grep -- never
        as a pattern, an argument or a command word -- so nothing it says can
        change what the step runs. The URLs grep returns are the only thing
        fetched, from GitHub's own asset hosts and no other, with no
        credential on the request."""
        env = _step_env("ux-review.yml", UX_EVIDENCE_STEP)
        assert "BODY" not in env, "the body is read from the API at run time, not the event"
        assert env["REPO"] == "${{ github.repository }}"
        assert env["PR"] == "${{ github.event.pull_request.number }}"
        assert env["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        assert env["MAX_CLIPS"] == "4"
        step_script = _step_script(_workflow("ux-review.yml"), UX_EVIDENCE_STEP)
        assert ATTACHMENT_SOURCE_LINE in step_script
        assert "gh api" not in step_script, "the step itself makes no API call"
        script = _attachment_script()
        code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
        assert any(
            SNAPSHOT_SOURCE_FRAGMENT in ln for ln in code
        ), "the shared script takes the description from this job's one snapshot read"
        assert any(
            ln.strip() == 'body="$(cat "$KC_PR_BODY_FILE")"' for ln in code
        ), "and reads it into one variable, as before"
        assert not [ln for ln in code if "gh api" in ln], [ln for ln in code if "gh api" in ln]
        # A transient API failure is retried, bounded, and then fails closed.
        # The retry lives in the snapshot, the only reader now; the fail-closed
        # MESSAGE stays here, because a failed read costs each consumer
        # something different.
        snapshot = _snapshot_script()
        assert "for _kc_snap_try in 1 2 3; do" in snapshot
        assert 'sleep "$_kc_snap_try"' in snapshot
        assert "::error::Could not read this PR's description after 3 attempts" in script
        body_lines = [ln for ln in code if "$body" in ln or "${body" in ln]
        assert len(body_lines) == 1, body_lines
        assert re.search(r'grep -oE "\$allow" <<< "\$body"', body_lines[0]), body_lines[0]
        self._assert_attachment_fetch_is_allowlisted_and_anonymous(script)

    def test_the_fork_lane_fetches_attachments_from_the_api_body_with_the_same_guards(
        self,
    ) -> None:
        """The fork lane runs under pull_request_target with secrets, so its
        step reads the description from the API (the event payload can lag an
        edit) into one variable, hands that variable to grep as a here-string
        and nothing else, and downloads only allowlisted GitHub asset URLs with
        the same anonymous, size-capped curl as the same-repo lane. The step is
        gated on the UI-scope pass (and skipped under an accepted human
        override), and the egress allowlist the job already carries admits both
        hosts a download touches: github.com and the user-asset S3 bucket its
        302 points at."""
        step = _step("fork-ux-review.yml", FORK_ATTACHMENT_STEP)
        assert " ".join(step["if"].split()) == (
            "steps.human_override.outputs.active != 'true' && ("
            "steps.pr.outputs.obsolete != 'true' && (steps.scope.outputs.ui == 'true'))"
        )
        env = _step_env("fork-ux-review.yml", FORK_ATTACHMENT_STEP)
        assert env["REPO"] == "${{ github.repository }}"
        assert env["PR"] == "${{ steps.pr.outputs.pr }}"
        assert env["MAX_SHOTS"] == "40"
        assert env["MAX_CLIPS"] == "4"
        assert "BODY" not in env, "the fork lane reads the body from the API, not the event"
        step_script = _step_script(_workflow("fork-ux-review.yml"), FORK_ATTACHMENT_STEP)
        assert ATTACHMENT_SOURCE_LINE in step_script
        assert "gh api" not in step_script, "the step itself makes no API call"
        steps = [st.get("name") for st in self._steps("fork-ux-review.yml")]
        assert steps.index("Checkout base (trusted)") < steps.index(FORK_ATTACHMENT_STEP)
        script = _attachment_script()
        code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
        body_lines = [ln for ln in code if "$body" in ln or "${body" in ln]
        assert any(
            SNAPSHOT_SOURCE_FRAGMENT in ln for ln in code
        ), "the shared script takes the description from this job's one snapshot read"
        assert any(
            ln.strip() == 'body="$(cat "$KC_PR_BODY_FILE")"' for ln in code
        ), "and reads it into one variable, as before"
        assert not [ln for ln in code if "gh api" in ln], [ln for ln in code if "gh api" in ln]
        # A transient API failure is retried, bounded, and then fails closed.
        # The retry lives in the snapshot, the only reader now; the fail-closed
        # MESSAGE stays here, because a failed read costs each consumer
        # something different.
        snapshot = _snapshot_script()
        assert "for _kc_snap_try in 1 2 3; do" in snapshot
        assert 'sleep "$_kc_snap_try"' in snapshot
        assert "::error::Could not read this PR's description after 3 attempts" in script
        assert len(body_lines) == 1, body_lines
        assert (
            "currently github-production-user-asset-6210df.s3.amazonaws.com; in fork-ux-review.yml"
            in script
        ), "the all-skipped error names the egress host to re-check"
        assert re.search(r'grep -oE "\$allow" <<< "\$body"', body_lines[0]), body_lines[0]
        self._assert_attachment_fetch_is_allowlisted_and_anonymous(script)
        # Downloads land under runner.temp and the reviewer is told where.
        assert 'DEST_DIR="$ATTACH_DIR"' in step_script
        assert 'mv -- "$tmp" "$DEST_DIR/$name"' in script
        system = _line_containing(
            _step("fork-ux-review.yml", UX_REVIEW_STEP)["with"]["claude_args"],
            "--append-system-prompt",
        )
        for data_file in ("ux-attachments.txt", "ux-attachment-map.txt", "ux-recordings.txt"):
            assert data_file in system, data_file
        # The folded scalar is a whitespace-separated list of host:port tokens;
        # compare whole tokens, so a host that merely contains the name as a
        # substring cannot satisfy the check.
        endpoints = set(
            _step("fork-ux-review.yml", "Harden runner (egress allowlist)")["with"][
                "allowed-endpoints"
            ].split()
        )
        assert {
            "github.com:443",
            "github-production-user-asset-6210df.s3.amazonaws.com:443",
        } <= endpoints, (
            "user-attachments URLs redirect to that bucket; without both hosts every "
            f"fork-lane download is egress-blocked: {sorted(endpoints)}"
        )

    @staticmethod
    def _assert_attachment_fetch_is_allowlisted_and_anonymous(script: str) -> None:
        code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
        # The allowlist is exactly the one asset URL shape `gh --attach` and the
        # web editor emit; nothing is built from the repository name, so no
        # escaping machinery exists to get wrong.
        allow = [ln.strip() for ln in code if ln.strip().startswith("allow=")]
        assert allow == [
            'allow="https://github\\.com/user-attachments/assets/[0-9A-Za-z-]+"',
        ]
        assert "repo_re" not in script
        # Every attempted download skipped is announced as an error annotation,
        # not left to per-URL warnings: that is how a moved asset host shows up.
        assert 'if [ "$fetched" -gt 0 ] && [ "$skipped" -eq "$fetched" ]; then' in script
        assert "::error::Every one of the $fetched attachment download(s) was skipped" in script
        curl = [ln for ln in code if re.search(r"\bcurl\b", ln)]
        assert len(curl) == 1, curl
        for forbidden in ("-H ", "--header", "Authorization", "GH_TOKEN", "GITHUB_TOKEN"):
            assert forbidden not in curl[0], curl[0]
        for flag in (
            "-sSfL",
            "--proto '=https'",
            "--max-redirs 5",
            "--max-time 60",
            "--max-filesize 104857600",
            "-w '%{http_code}'",
            '-o "$tmp" "$url"',
        ):
            assert flag in curl[0], curl[0]
        # Failure is logged and skipped, never fatal; the type is the bytes'.
        # A definite 4xx is the author's URL, not transport, so it never
        # counts as a fetch failure.
        assert "SKIPPED (download failed)" in script
        assert "SKIPPED (HTTP $code, the attachment URL does not resolve to an asset)" in script
        assert "4[0-9][0-9]) transient=0 ;;" in script
        # GitHub answers a secondary rate limit with 403 as well as 429.
        assert "403|408|429) transient=1 ;;" in script
        assert 'mime="$(file --mime-type -b -- "$tmp")"' in script
        assert "SKIPPED (mime $mime)" in script

    def test_both_lanes_name_the_description_as_the_normal_home_for_evidence(self) -> None:
        """A PR carrying its screenshots as description attachments is the
        normal case; a committed file still counts. Pass 2 is told so, the
        not-performed report names both sources, and the fork lane is told the
        workflow downloaded the attachments for it rather than asked only for
        committed files."""
        script = _step_script(_workflow("ux-review.yml"), UX_CAPTURE_STEP)
        assert (
            "BLIND READ NOT PERFORMED: the PR description carries no image attachment "
            "and this revision commits no image under temp-screenshots/ or .github/screenshots/"
        ) in script
        prompt = _flat(_step("ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "The normal source is an image attached to the PR description" in prompt
        assert (
            "an image committed under temp-screenshots/ or .github/screenshots/ still counts"
            in prompt
        )
        assert "at least one screenshot the PR supplies" in prompt
        assert "The recording list in your system prompt carries every one the PR supplies" in (
            prompt
        )
        system = _line_containing(
            _step("ux-review.yml", UX_REVIEW_STEP)["with"]["claude_args"], "--append-system-prompt"
        )
        assert "the attachment URL in the PR description, or the repository path" in system
        fork_prompt = _flat(_step("fork-ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "the normal evidence is an image or video attached to the PR description" in (
            fork_prompt
        )
        assert "The workflow downloads each one for you" in fork_prompt
        assert "A committed image counts the same" in fork_prompt
        assert "at least one screenshot the PR supplies" in fork_prompt
