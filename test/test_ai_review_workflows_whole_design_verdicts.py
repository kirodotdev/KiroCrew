"""Review-workflow tests for how the design, UX and first-principles lanes reach a
verdict and present it: calibration, current-head markers, take-away checks and
the PASS-or-BLOCK status steps.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    DESIGN_LANES,
    _bash,
    _flat,
    _proc_log,
    _step_script,
    _workflow,
)

# The three design/premise/UX lanes on a fork, whose verdict lands as a
# check-run this repo controls end to end.
CONCERNS_FORK_LANES = (
    ("fork-design-review.yml", "Design-Verdict:", "[DESIGN-REVIEWED]"),
    (
        "fork-first-principles-review.yml",
        "First-Principles-Verdict:",
        "[FIRST-PRINCIPLES-REVIEWED]",
    ),
    ("fork-ux-review.yml", "UX-Verdict:", "[UX-REVIEWED]"),
)

# Their same-repo twins, which own a JOB rather than a check-run and so cannot
# report themselves neutral: (workflow, posting step, status step, lane label).
# security-scope-review.yml is not one of them: it emits no CONCERNS digest into
# `GITHUB_STEP_SUMMARY` -- its summary carries the folded per-platform verdict
# and the confirmed rows instead -- so an entry here could only be satisfied by
# inventing a digest the lane does not have.
CONCERNS_SAME_LANES = (
    (
        "design-review.yml",
        "Post design review summary",
        "Design review status (gates on BLOCK)",
        "Design Review",
    ),
    (
        "first-principles-review.yml",
        "Post first-principles review summary",
        "First-principles review status (gates on BLOCK)",
        "First Principles Review",
    ),
    (
        "ux-review.yml",
        "Post UX review summary",
        "UX review status (gates on BLOCK)",
        "UX Review",
    ),
)


class TestDesignTakeAwayCheck:
    """Design Review checks the readers of anything a PR takes away.

    Hiding a path or pruning records can break a reader the author never
    listed, on an entry point the PR never mentions; the lane checks those
    readers against code instead of taking the compatibility claim on trust.
    """

    FIRST = "TAKE-AWAY CHECK (run it yourself"
    LAST = "in the repo) is not a finding."

    def _block(self, workflow: str) -> str:
        lines = _workflow(workflow).splitlines()
        start = next((i for i, line in enumerate(lines) if self.FIRST in line), None)
        assert start is not None, f"{workflow} carries no TAKE-AWAY CHECK"
        end = next(i for i, line in enumerate(lines[start:], start) if self.LAST in line)
        block = lines[start : end + 1]
        indent = len(block[0]) - len(block[0].lstrip())
        return "\n".join(line[indent:] if line.strip() else "" for line in block)

    def test_both_design_lanes_carry_an_identical_take_away_check(self) -> None:
        reference = self._block(DESIGN_LANES[0])
        for name in DESIGN_LANES:
            assert self._block(name) == reference, f"{name} TAKE-AWAY CHECK drifted"

    def test_an_unlisted_broken_reader_blocks_and_a_weak_reason_does_not(self) -> None:
        for name in DESIGN_LANES:
            flat = _flat(self._block(name))
            reader_line = (
                "Reader: <path>:<symbol> -- <entry: chat|cron|subagent|app|crew page|release>"
            )
            assert reader_line in flat, name
            assert (
                "an entry label outside that set (e.g. prompt builder) does not unlist it" in flat
            ), name
            assert "is not listed -> BLOCK (the take-away trigger under VERDICT)" in flat, name
            assert (
                "does not exercise that reader -> BLOCK only if you can write its Trigger and Outcome"
                in flat
            ), name
            # A capped description cannot prove the section absent.
            assert (
                "is the last section before that notice, its list may sit past the cut: do not BLOCK on the missing section"
                in flat
            ), name
            # The cap reaches only a section the reviewer could not see.
            assert "the notice is judged on its text" in flat, name
            assert (
                "A reader a `Compatible:` line names by `<path>:<symbol>` counts as listed" in flat
            ), name
            assert "TAKE-AWAY WITH AN UNLISTED BROKEN READER" in _workflow(name), name

    def test_every_entry_point_the_sweep_names_exists(self) -> None:
        # A moved entry point would leave the sweep grepping a path that is gone.
        root = Path(__file__).resolve().parent.parent
        block = self._block(DESIGN_LANES[0])
        named = (
            "website/src/",
            "src/kiro_crew/dashboard/chat_runner.py",
            "src/kiro_crew/dashboard/chat_turn/",
            "src/kiro_crew/session_agent_selection.py",
            "src/kiro_crew/subagent_manager/",
            "src/kiro_crew/subagent_persistence.py",
            "src/kiro_crew/cron_script.py",
            "src/kiro_crew/cron_service/",
            "src/kiro_crew/apps/",
            "src/kiro_crew/context_assembly/",
            "src/kiro_crew/execution_context.py",
            ".github/workflows/release.yml",
            "packaging/",
        )
        for path in named:
            leaf = path.removeprefix("src/kiro_crew/")
            assert leaf in block, f"the sweep no longer names {leaf}; update this list"
            assert (root / path).exists(), f"the sweep names {path}, which does not exist"


class TestDesignVerdictCalibration:
    """BLOCK must be REACHABLE for the class of change that takes a platform out.

    The flat tie-breaker ("when torn, choose CONCERNS", "if any is
    might/unclear it is CONCERNS at most") made it unreachable there by
    construction: the reviewer has no shell and no Windows host, so a platform
    premise is ALWAYS unclear to it. The worked example: the lane
    wrote the exact failure mode (absent macOS-only settings file read as False
    -> every classified Windows spawn fail-closes at session start) and still
    resolved CONCERNS. The calibration is therefore scoped by REVERSIBILITY, and
    an unverified premise is an INPUT to BLOCK rather than a reason to lower it.
    """

    FIRST = "Tie-breaker, SCOPED BY REVERSIBILITY"
    LAST = "Size is never a BLOCK."

    def _calibration(self, workflow: str) -> str:
        lines = _workflow(workflow).splitlines()
        start = next((i for i, line in enumerate(lines) if self.FIRST in line), None)
        assert start is not None, f"{workflow} carries no reversibility-scoped tie-breaker"
        end = next(i for i, line in enumerate(lines[start:], start) if self.LAST in line)
        block = lines[start : end + 1]
        indent = len(block[0]) - len(block[0].lstrip())
        return "\n".join(line[indent:] if line.strip() else "" for line in block)

    def test_both_design_lanes_carry_an_identical_calibration_block(self) -> None:
        # The fork lane is the one that runs on an outside contributor's PR, so a
        # calibration that lives in only one copy is a calibration that does not
        # apply to the PRs it was written for.
        blocks = {name: self._calibration(name) for name in DESIGN_LANES}
        reference = blocks[DESIGN_LANES[0]]
        for name, block in blocks.items():
            assert block == reference, (
                f"{name} verdict calibration drifted from {DESIGN_LANES[0]}; "
                "both design lanes must carry the same text"
            )

    def test_tie_breaker_is_decided_by_the_consequence_not_by_certainty(self) -> None:
        for name in DESIGN_LANES:
            flat = _flat(self._calibration(name))
            # Reversible -> PASS with a note, so an ordinary judgement call
            # does not block.
            assert "the consequence is REVERSIBLE" in flat, name
            assert "a regression a revert fixes cleanly" in flat, name
            assert "-> PASS, with the risk as a note" in flat, name
            # Irreversible-in-product -> BLOCK is the new half.
            assert "STOPS WORKING" in flat, name
            assert "session start, spawn, auth, gateway boot" in flat, name
            assert "no in-product remedy short of disabling a safety control" in flat, name
            assert "-> BLOCK" in flat, name

    def test_an_unverified_premise_is_a_block_input(self) -> None:
        for name in DESIGN_LANES:
            flat = _flat(self._calibration(name))
            assert "An UNVERIFIED premise is a BLOCK INPUT, not a reason to lower" in flat, name
            # The reviewer's own blindness is named, because that is why the old
            # rule collapsed: it cannot verify a platform claim, so the AUTHOR
            # must, and BLOCK is what asks them to.
            assert "no shell, no Windows host and no provider account" in flat, name
            assert "name the evidence that clears it" in flat, name

    def test_the_flat_tie_breaker_is_gone_from_both_lanes(self) -> None:
        # The exact sentences that made BLOCK unreachable must not come back --
        # either one restores the old behaviour on its own.
        for name in DESIGN_LANES:
            flat = _flat(_workflow(name))
            assert (
                "Tie-breaker: when torn between BLOCK and CONCERNS, choose CONCERNS." not in flat
            ), name
            assert (
                'If any is "might", "unclear", or a matter of taste, it is a CONCERNS at most'
                not in flat
            ), name

    def test_named_block_triggers_cover_the_shape_that_shipped(self) -> None:
        for name in DESIGN_LANES:
            flat = _flat(self._calibration(name))
            # (1) the availability path gated on unestablished platform semantics
            assert "gates a core availability path on a probe, file or setting" in flat, name
            assert "ON THE AFFECTED PLATFORM this repo never establishes" in flat, name
            assert "an absent file read as False is a decision, not a default" in flat, name
            # (2) a deleted pin treated as a gap -- with the check that tells the
            # two apart, since "it was a gap" is exactly what the PR asserted.
            assert "pinned the OPPOSITE behaviour" in flat, name
            assert "as a GAP rather than as a DECISION" in flat, name
            assert "read the test's own message and its git history" in flat, name
            # (3) an N/A manual-verification claim for an unexercised platform
            assert '"Manual verification: N/A"' in flat, name
            assert "a platform or in an environment CI does not exercise" in flat, name

    def test_the_anti_bloat_rules_survive_the_recalibration(self) -> None:
        # Making BLOCK reachable must not make it cheap: the budget and the
        # size-is-not-a-finding rule are what keep this lane from turning into
        # noise, so they are pinned alongside the new triggers.
        for name in DESIGN_LANES:
            flat = _flat(self._calibration(name))
            assert "at most 1 BLOCK per review" in flat, name
            assert "Size is never a BLOCK." in flat, name
            assert "A matter of taste is never a BLOCK" in flat, name
            assert "FALSIFY BEFORE YOU BLOCK" in flat, name
            assert "never merely because the change is large or far-reaching" in _flat(
                _workflow(name)
            ), name

    def test_every_blocker_states_a_trigger_an_outcome_and_what_clears_it(self) -> None:
        # A finding a coding loop cannot act on is a finding that does not get
        # acted on. `Clears when:` is the actionable half; Trigger and Outcome are
        # what make it a BLOCK at all (an item without both is read as PASS).
        for name in DESIGN_LANES:
            workflow = _workflow(name)
            blockers = workflow.index("### Blockers")
            notes = workflow.index("### Notes", blockers)
            section = _flat(workflow[blockers:notes])
            assert "`Clears when: <the concrete evidence or change that resolves this>`" in section
            assert "`Trigger: <the situation that occurs in practice" in section, name
            assert "`Outcome: <the observable cost carried" in section, name
            assert "a BLOCK with no Trigger or no Outcome is read as PASS" in section, name
            assert "### Watch" not in workflow and "### Suggestions" not in workflow, name


class TestThereIsNoSoftTier:
    """A lane says PASS or BLOCK. The CONCERNS tier, its digest and its neutral
    check-run conclusion are gone from every whole-design lane."""

    def test_no_lane_carries_a_soft_verdict_arm_or_digest(self) -> None:
        for name in ALL_CONCERNS_LANES:
            workflow = _workflow(name)
            assert "concerns_digest" not in workflow, name
            assert "CONCERNS_DIGEST" not in workflow, name
            assert "CONCERNS_PUNCHLINE" not in workflow, name
            assert "### Watch" not in workflow.replace("There is no `### Watch`", ""), name
            assert 'badge="🟡 CONCERNS"' not in workflow, name
            for line in workflow.splitlines():
                if "CONCERNS" in line:
                    # The only mention left is the comment naming the retired word.
                    assert line.lstrip().startswith("#"), (name, line)

    def test_fork_lanes_finalize_pass_and_block_and_nothing_between(self) -> None:
        for name, _, _ in CONCERNS_FORK_LANES:
            finalize = _step_script(_workflow(name), "Finalize check-run")
            assert "CONCERNS" not in finalize, name
            assert 'conclusion="success"' in finalize, name
            assert 'conclusion="failure"' in finalize, name
            # An incomplete run still resolves neutral, and a green tick is not
            # what a verdict-less run earns.
            assert 'title="review incomplete"' in finalize, name

    def test_same_repo_status_steps_gate_on_block_only(self) -> None:
        for name, _, status_step, lane in CONCERNS_SAME_LANES:
            script = _step_script(_workflow(name), status_step)
            assert "PASS)" in script, name
            assert "PASS|CONCERNS" not in script, name
            assert "BLOCK)" in script, name
            assert "exit 1" in script, name
            assert f"{lane.split()[0]}" in script, name

    @pytest.mark.parametrize(
        "entry",
        [pytest.param(entry, id=entry[0].removesuffix(".yml")) for entry in CONCERNS_SAME_LANES],
    )
    @pytest.mark.parametrize(
        ("verdict", "exit_code"),
        [("PASS", 0), ("BLOCK", 1), ("CONCERNS", 0), ("UNKNOWN", 0), ("", 0)],
    )
    def test_same_repo_status_step_exit_codes(
        self, entry: tuple[str, str, str, str], verdict: str, exit_code: int
    ) -> None:
        """Execute the real status step. A word that is not PASS or BLOCK (the
        retired CONCERNS included) is not a verdict, and a lane that produced no
        verdict never fails the gate."""
        bash = _bash()
        if bash is None:
            pytest.skip("the status step is Bash")
        name, _, status_step, _ = entry
        script = _step_script(_workflow(name), status_step)
        env = {
            **os.environ,
            "HEAD": "abc123",
            "ACTOR": "alice",
            "VERDICT": verdict,
            "HUMAN_OVERRIDE": "",
            "OVERRIDE_ACTOR": "",
        }
        result = subprocess.run(
            [bash, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        assert result.returncode == exit_code, _proc_log(result)


ALL_CONCERNS_LANES = tuple(name for name, *_ in CONCERNS_FORK_LANES) + tuple(
    name for name, *_ in CONCERNS_SAME_LANES
)
LANE_HEADERS = {
    "design": "Design-Verdict:",
    "first-principles": "First-Principles-Verdict:",
    "ux": "UX-Verdict:",
}


def _lane_header(name: str) -> str:
    for key, header in LANE_HEADERS.items():
        if key in name:
            return header
    raise AssertionError(name)


class TestReviewLanesPublishOnlyTheReview:
    """The six whole-design lanes capture the model's last message verbatim.
    A model that narrates before the header ("All facts verified against the
    base. Composing the final review.") ships that line to the PR above the
    punchline. Each lane trims to its own header, and
    each counts the prose outside the collapsed inventory so an overrun is a
    visible annotation rather than a longer comment."""

    def _capture_step(self, name: str) -> str:
        workflow = _workflow(name)
        # Whatever the step is called, the trim sits right after the
        # execution_file capture and before the header is parsed.
        start = workflow.index("select(.result != null) ] | (last.result")
        header = _lane_header(name)
        # The VERDICT read, in either spelling a lane uses for it, is the end of
        # the slice. Both spellings are listed because the two fork advisory lanes
        # read their header with `awk` and the rest with `grep`, and the boundary
        # is the read itself rather than the program: keying this to `grep` alone
        # would make the slice run past the capture on a converted lane and the
        # assertions below would then measure the wrong block. Neither pattern can
        # match the TRIM a few lines above it -- that one greps with `-q` and its
        # awk opens `f ||`, where the capture's opens `!found &&`.
        end_at = re.search(
            rf"grep -iE '\^{re.escape(header)}'"
            rf"|!found && tolower\(\$0\) ~ /\^{re.escape(header.lower())}/",
            workflow[start:],
        )
        assert end_at is not None, f"{name}: no verdict read follows the trim"
        return workflow[start : start + end_at.start()]

    def test_every_lane_trims_to_its_own_header(self) -> None:
        for name in ALL_CONCERNS_LANES:
            header = _lane_header(name)
            block = self._capture_step(name)
            assert f"if grep -qiE '^{header}' <<< \"$summary\"; then" in block, name
            assert (
                f"awk 'f || tolower($0) ~ /^{header.lower()}/ {{ f = 1; print }}' <<< \"$summary\""
                in block
            ), name
            # Trim only when a header exists: an unparseable body must still
            # reach the "returned no verdict header" path with its text intact.
            assert "without one the text is left whole" in block, name
            # No pipe upstream of a possibly-early exit, same as the digest.
            assert "printf '%s\\n' \"$summary\" | awk" not in block, name

    def test_every_lane_counts_the_prose_outside_the_inventory(self) -> None:
        caps = {"first-principles": 180, "design": 150, "ux": 150}
        for name in ALL_CONCERNS_LANES:
            workflow = _workflow(name)
            cap = next(v for k, v in caps.items() if k in name)
            assert 'if [ "$verdict" != "UNKNOWN" ]; then' in workflow, name
            assert (
                "awk '/<details>/ { skip = 1 } !skip { print } /<\\/details>/ { skip = 0 }' <<< \"$summary\" | wc -w"
                in workflow
            ), name
            assert f'if [ "${{words:-0}}" -gt {cap * 2} ]; then' in workflow, name
            assert "over length::$words words outside the inventory" in workflow, name
            assert f"the contract caps the review at ~{cap}." in workflow, name
            # It is a warning: the verdict and the comment do not move.
            block = workflow[workflow.index("over length::") :]
            assert "exit 1" not in block[:400], name

    def test_the_trim_and_count_execute_as_written(self, tmp_path: Path) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the lanes are Bash")
        # Run the FP lane's trim and count over a body with a narration line
        # above the header and a long collapsed inventory below the finding.
        capture = self._capture_step("first-principles-review.yml")
        trim = capture[
            capture.index("if grep -qiE") : capture.index("fi\n", capture.index("if grep -qiE")) + 3
        ]
        indent = len(trim) - len(trim.lstrip())
        trim = "\n".join(line[indent:] if line.strip() else "" for line in trim.splitlines())
        body = (
            "All facts verified against the base. Composing the final review.\n\n"
            "First-Principles-Verdict: CONCERNS\n\n**punchline**\n\n"
            "### Not justified as shipped\n- Item 1 — unjustified move: one two three.\n\n"
            "### What this change ships\n<details><summary>Inventory (9 items) — 5 justified</summary>\n"
            + ("1. inventory words that must not count toward the cap\n" * 40)
            + "</details>\n\n[FIRST-PRINCIPLES-REVIEWED] abc\n"
        )
        (tmp_path / "body.md").write_text(body, encoding="utf-8")
        script = (
            'summary="$(cat body.md)"\n'
            + trim
            # First line via `awk 'NR == 1'`, never `head -n1`: head closes the
            # pipe on its producer, and under this script's own `-o pipefail`
            # the producer's SIGPIPE (141) becomes the script's exit status.
            # Same reason the lanes themselves are held to it -- see
            # test_guard_function_is_byte_identical_across_all_lanes.
            + "\nprintf '%s\\n' \"$summary\" | awk 'NR == 1'\n"
            + "awk '/<details>/ { skip = 1 } !skip { print } /<\\/details>/ { skip = 0 }' <<< \"$summary\" | wc -w | tr -d ' '\n"
        )
        result = subprocess.run(
            [bash, "-euo", "pipefail", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        first_line, words = result.stdout.strip().splitlines()
        assert first_line == "First-Principles-Verdict: CONCERNS"
        # 40 inventory lines x 9 words would be 360 on their own; the count
        # excludes them and lands on the ~20 words of prose.
        assert int(words) < 40, words


class TestDesignAndUxPunchlinesOpenWithTheProblem:
    """Design and UX already report only problems by section, but their
    punchline template asking for "the single most important takeaway" and,
    on PASS, "why it's sound" yields CONCERNS punchlines of the shape `<what
    is sound>, but <problem>` ("Sound overlay-plus-reservation design ...,
    but"; "Fixed toggles and fullscreen are coherent and evidenced, but").
    The reader stops at the comma. The First Principles form -- open
    with the problem, PASS names the one thing to verify -- now applies to
    all four copies."""

    LANES = ("design-review.yml", "fork-design-review.yml", "ux-review.yml", "fork-ux-review.yml")

    def _punchline_rule(self, name: str) -> str:
        workflow = _workflow(name)
        start = workflow.index("Then a blank line and ONE bold REASON")
        return workflow[start : workflow.index("###", start)]

    def test_the_punchline_opens_with_the_problem(self) -> None:
        for name in self.LANES:
            rule = _flat(self._punchline_rule(name))
            assert "It OPENS with the problem" in rule, name
            assert "problem first" in rule, name
            assert "because the reader stops at the comma" in rule, name
            if "ux" in name:
                assert "never `<what works>, but <problem>`" in rule, name
                assert "never why the experience holds" in rule, name
            else:
                assert "never `<what is sound>, but <problem>`" in rule, name
                assert "never an argument for the author's case" in rule, name
            # PASS is nothing to check, or a few words on why nothing blocks --
            # not praise.
            assert "one to three sentences" in rule, name
            assert "`Nothing to check.`" in rule, name
            assert "a reviewer that argues the author's case is not reviewing" in rule, name
            # The old wording is gone in both lanes.
            assert "the single most important takeaway" not in rule, name
            assert "for PASS, why it's sound" not in rule, name
            assert "for PASS, why the experience holds" not in rule, name

    def test_same_repo_and_fork_copies_match(self) -> None:
        for same, fork in (
            ("design-review.yml", "fork-design-review.yml"),
            ("ux-review.yml", "fork-ux-review.yml"),
        ):
            assert _flat(self._punchline_rule(same)) == _flat(self._punchline_rule(fork)), (
                same,
                fork,
            )


CAUSE_LANES = (
    "design-review.yml",
    "ux-review.yml",
    "first-principles-review.yml",
    "fork-design-review.yml",
    "fork-ux-review.yml",
    "fork-first-principles-review.yml",
)


class TestIncompleteReviewNamesTheObservedCause:
    """A fallback notice must report what was OBSERVED, not a plausible cause.

    Every one of these lanes said "the model call errored or returned no verdict
    header" whenever no verdict parsed -- including when the model was never
    called at all, which is what happens when any earlier step in the job fails.
    A wrong-but-plausible cause is worse than "could not complete, see logs": it
    sends the contributor to debug their prompt or the model while the real
    failure is upstream. The step's own `outcome` already distinguishes the
    cases, so no new plumbing is needed to stop guessing.
    """

    @pytest.mark.parametrize("lane", CAUSE_LANES)
    def test_cause_is_derived_from_the_review_step_outcome(self, lane: str) -> None:
        workflow = _workflow(lane)
        assert (
            "the model call errored or returned no verdict header" not in workflow
        ), f"{lane}: still asserts a cause it did not observe"
        assert (
            "REVIEW_OUTCOME: ${{ steps.review.outcome }}" in workflow
        ), f"{lane}: the observed outcome is not wired into the comment step"

    @pytest.mark.parametrize("lane", CAUSE_LANES)
    def test_each_outcome_maps_to_a_distinct_honest_reason(self, lane: str) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the cause mapping is Bash; skip where Bash is absent")
        workflow = _workflow(lane)
        m = re.search(r'(case "\$\{REVIEW_OUTCOME:-\}" in.*?esac)', workflow, re.S)
        assert m, f"{lane}: no REVIEW_OUTCOME case block"
        block = "\n".join(line.strip() for line in m.group(1).splitlines())
        seen = {}
        for outcome in ("skipped", "failure", "cancelled", "success", ""):
            out = subprocess.run(
                [bash, "-e", "-c", block + '\nprintf "%s" "$why"'],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "REVIEW_OUTCOME": outcome},
            )
            assert out.returncode == 0, out.stderr
            seen[outcome] = out.stdout
        # The load-bearing distinction: a model that was never called must not be
        # described as a model that errored.
        assert "never ran" in seen["skipped"], f"{lane}: {seen['skipped']!r}"
        assert "no model call was made" in seen["skipped"]
        assert "review step failed" in seen["failure"]
        assert "review step was cancelled" in seen["cancelled"]
        assert "review step completed" in seen["success"]
        assert seen["failure"] != seen["skipped"] != seen["success"]
        assert len(set(seen.values())) == 5, f"{lane}: reasons collide: {seen}"


# The lanes whose missing head marker degrades to a NON-BLOCKING UNKNOWN.
# The Security Scope Review lanes are deliberately absent: a missing marker REDS
# them, because "no confirmed regression" over a tightening nobody measured is
# the exact false green that lane exists to prevent. Adding them here would
# assert the opposite of their contract.
ADVISORY_LANES = {
    "design-review.yml": "DESIGN-REVIEWED",
    "fork-design-review.yml": "DESIGN-REVIEWED",
    "ux-review.yml": "UX-REVIEWED",
    "fork-ux-review.yml": "UX-REVIEWED",
}


class TestAdvisoryVerdictRequiresCurrentHeadMarker:
    """The advisory lanes must score the verdict against the `[<LANE>-REVIEWED]
    <sha>` proof marker, not off the header ALONE. If the marker is decorative,
    a reply carrying a stale or rewritten marker still counts as
    a verdict for the current revision.

    These lanes are non-blocking, so the failure is not a bad merge gate; it is
    a badge that asserts "reviewed at this sha" without that being checked.
    """

    @pytest.mark.parametrize(("lane", "marker"), sorted(ADVISORY_LANES.items()))
    def test_the_head_marker_is_verified_not_just_emitted(self, lane: str, marker: str) -> None:
        flat = _flat(_workflow(lane))
        assert f'grep -qF "[{marker}] $HEAD"' in flat or (
            f'grep -qF "[{marker}] ${{HEAD:-}}"' in flat
        ), f"{lane}: verdict is accepted without proving the marker matches HEAD"
        assert 'verdict="UNKNOWN"' in flat, (
            f"{lane}: a missing marker must degrade to the existing "
            "non-blocking UNKNOWN path, not invent a verdict"
        )

    @pytest.mark.parametrize(("lane", "marker"), sorted(ADVISORY_LANES.items()))
    def test_the_marker_check_does_not_reintroduce_the_pipe_bug(
        self, lane: str, marker: str
    ) -> None:
        """The check must not be `printf … | grep -q`: under `pipefail` a long
        summary lets grep exit first, and the SIGPIPE status would silently
        turn every verdict into UNKNOWN (the same pipe-timing defect)."""
        flat = _flat(_workflow(lane))
        i = flat.index(f"[{marker}] $")
        window = flat[max(0, i - 200) : i]
        assert (
            "printf" not in window.split("if ")[-1]
        ), f"{lane}: marker check pipes a writer into grep"

    @pytest.mark.parametrize("marker", ["DESIGN-REVIEWED", "UX-REVIEWED"])
    def test_marker_matching_is_literal_and_head_scoped(self, marker: str) -> None:
        """Behavioural: the guard accepts only the CURRENT head's marker.

        `grep -qF` matters -- the marker is bracketed, and those are regex
        metacharacters, so a non-fixed match would not mean what it reads as.
        """
        bash = _bash()
        if bash is None:
            pytest.skip("the guard runs only under Bash")
        script = (
            "set -uo pipefail\n"
            'if ! grep -qF "[%s] $HEAD" <<< "$SUMMARY"; then\n'
            "  echo UNKNOWN\nelse\n  echo KEPT\nfi"
        ) % marker
        cases = {
            f"Verdict: PASS\n[{marker}] abc123": "KEPT",
            f"Verdict: PASS\n[{marker}] deadbeef": "UNKNOWN",
            "Verdict: PASS": "UNKNOWN",
        }
        for summary, want in cases.items():
            out = subprocess.run(
                [bash, "-c", script],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "HEAD": "abc123", "SUMMARY": summary},
            )
            assert out.returncode == 0, out.stderr
            assert out.stdout.strip() == want, f"{summary!r} -> {out.stdout!r}"
