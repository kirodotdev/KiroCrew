"""Review-workflow tests for the First Principles lanes: their shell, the scope gate,
the PR intent they read, the fork lane's contract state, and the problems-first
shape of what they publish. The lane's review-contract pins
(``TestFirstPrinciplesReview``) are in ``test_ai_review_workflows.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    CAPTURE_SCRIPT,
    CAPTURE_SOURCE_LINE,
    FP_LANES,
    WORKFLOWS,
    _bash,
    _fp_contract,
    _line_containing,
    _reads_header_anchored,
    _review_contract_module,
    _step_script,
    _stub_path,
    _workflow,
)


class TestFirstPrinciplesProblemsFirstContract:
    """The lane's PASS was praise by construction, and its calibration made
    BLOCK unreachable for the one class where the reviewer is structurally
    unable to check the premise. Both were prompt text, so both are pinned
    here: provenance for a claimed defect, symmetry as INHERITED, a deleted pin
    as a prior decision, an availability-path premise as the BLOCK case, and an
    output that leads with the problems rather than with the inventory."""

    def test_a_claimed_defect_needs_a_provenance_the_reviewer_can_point_at(self) -> None:
        # A `fix` whose only support is the description asserting a defect is
        # indistinguishable from an addition: a change shipped a whole-platform
        # regression behind "verified security finding" and no repro.
        contract = _fp_contract()
        assert "PROVENANCE OF A REPORTED DEFECT" in contract
        assert "a test\n     this PR adds that fails on base" in contract
        assert "linked issue" in contract
        assert "has no provenance you can check, so the item is INHERITED" in contract

    def test_symmetry_with_a_twin_is_never_a_justification(self) -> None:
        # The measured failure: the lane tagged "aligns win32 with the macOS
        # twin" as `justified`, which its own provenance lens already calls
        # INHERITED. The tag has to be spelled out or the general rule loses.
        contract = _fp_contract()
        assert "SYMMETRY IS INHERITED, ALWAYS" in contract
        assert "aligns X with its twin" in contract
        assert "are NEVER a\n     justification on their own" in contract
        assert "the symmetry form of\n     INHERITED" in contract
        # The twin is not evidence about this side.
        assert "nameable WITHOUT the twin" in contract

    def test_a_deleted_pin_is_a_prior_decision_not_a_gap(self) -> None:
        # The PR deleted a test whose message said the opposite and called the
        # pin "a gap". That is the framing-contradicted-by-the-diff BLOCK
        # trigger, so the contract must route it there by name.
        contract = _fp_contract()
        assert "A DELETED OR REWRITTEN\n   PIN IS A PRIOR DECISION" in contract
        assert "pinned the OPPOSITE behaviour" in contract
        assert "Treat it as standing" in contract
        assert 'calls\n   it "a gap"' in contract
        assert "framing contradicted by the diff" in contract
        assert 'a deleted pin recast as "a gap" with no' in contract
        # A diff that deletes a comment reading "This deliberately supersedes
        # the earlier ... pill spec" plus its pin tests, with a description that
        # says nothing about it, is the same BLOCK case as one that mislabels
        # the deletion: a trigger naming only the MISLABELLED form lets the
        # UNMENTIONED form fall to the advisory tier. The only support that
        # counts is evidence the pin was wrong -- not consistency with the
        # other panel.
        assert "SILENCE IS THE SAME CASE, NOT A LESSER ONE" in contract
        assert "never mentions it" in contract
        assert "does not even learn a decision was reversed" in contract
        assert "and so is a deleted pin the description never\n  mentions" in contract
        assert '"consistency", "symmetry" or "matches the other panel" is not it' in contract

    def test_an_unverified_premise_on_an_availability_path_is_the_block_case(self) -> None:
        # "When torn, choose CONCERNS" made BLOCK unreachable exactly where the
        # reviewer cannot verify the premise at all -- it has no shell and no
        # second platform -- so the tie-breaker is scoped to reversible cases
        # and this one is named as a trigger.
        contract = _fp_contract()
        assert "UNVERIFIED PREMISE ON A CORE\n  AVAILABILITY PATH" in contract
        assert "ONLY where being wrong is REVERSIBLE" in contract
        assert 'Here "unclear" is the BLOCK case, not the CONCERNS case' in contract
        assert "the author can" in contract
        assert "Do not soften this to a CONCERNS item" in contract
        # The carve-outs stay a CLOSED set -- now three: (a) availability
        # premise, (b) rider, (c) product shape without a recorded decision,
        # which is also this lane's only "cannot evaluate". An open-ended
        # fourth would put the tie-breaker back in charge of everything.
        assert "there is no fourth" in contract
        assert "The three exceptions are named at the" in contract
        assert "there is no third" not in contract
        assert "The single exception is the combination" not in contract

    def test_undeclared_and_rides_along_are_inventory_tags_not_verdicts(self) -> None:
        # ~50% of PRs drew CONCERNS, so the signal cost nothing to ignore.
        # These two tags print as inventory and do not carry the verdict by
        # themselves; CONCERNS is reserved for premise and depth risks.
        contract = _fp_contract()
        assert "`undeclared` and `rides along`\n  are INVENTORY TAGS ONLY" in contract
        assert "on\n  their own they do NOT reach CONCERNS" in contract
        assert "a harm-free rider is inventory" in contract
        # The tags themselves survive on the item line.
        assert "undeclared | rides" in contract
        # A premise risk carried BY the rider still reaches CONCERNS.
        assert "when the rider itself\n  carries one of the premise risks" in contract

    def test_output_leads_with_problems_and_drops_the_praise_punchline(self) -> None:
        contract = _fp_contract()
        # The PASS punchline does not argue the author's case.
        assert "why every item earns its place" not in contract.split("Output EXACTLY")[0]
        assert "Never\nexplain why every item earns its place" in contract
        assert "the ONE thing a human should still verify before merge" in contract
        assert "`Nothing to check.`" in contract
        # Non-justified items are read FIRST, above the inventory.
        assert "### Not justified as shipped" in contract
        assert contract.index("### Not justified as shipped") < contract.index(
            "### What this change ships"
        )
        # `justified` carries no reason -- the parenthetical was the praise.
        assert "`justified` is exactly that ONE word" in contract
        assert "Only a NON-justified tag carries a reason" in contract

    def test_the_inventory_is_collapsed_on_every_verdict(self) -> None:
        # The block was left EXPANDED on CONCERNS/BLOCK -- exactly the verdicts
        # a human opens the comment for -- so the findings sat under a full
        # list of the items that were fine. Every item a human must act on is
        # already under `### Not justified as shipped`, so the inventory is an
        # audit trail and stays one click away on every verdict, with the
        # counts in the summary line.
        contract = _fp_contract()
        assert "ALWAYS COLLAPSED, on every verdict" in contract
        assert "<details><summary>Inventory (N items) — M justified</summary>" in contract
        assert "</details>" in contract
        # The old conditional is gone in both directions.
        assert "WHEN EVERY ITEM IS TAGGED `justified`" not in contract
        assert "leave the block EXPANDED" not in contract
        # The inventory is still always emitted -- collapsing is not omitting.
        assert "ALWAYS present, even on PASS" in contract
        assert "A PASS here is a claim about EVERY item" in contract
        # The findings a human acts on live above the block, not in it.
        assert "this block is the audit trail, not the summary" in contract

    def test_every_finding_states_what_would_clear_it(self) -> None:
        # A finding with no statable resolution is what produced 31 of 57
        # unanswered CONCERNS: nothing told the author when they were done.
        contract = _fp_contract()
        # Once on the item entry, once on the Blocker -- the two places a
        # finding can appear -- and nowhere else, because there is nowhere else.
        assert (
            contract.count("`Clears when: <the concrete evidence or change that resolves it>`") == 2
        )
        assert "REQUIRED on every\nitem whose tag reaches CONCERNS" in contract
        assert "is not a finding -- retag it or drop it" in contract

    def test_the_output_diet_tightened_and_kept_its_machine_read_lines(self) -> None:
        contract = _fp_contract()
        assert "review under ~180 words excluding the inventory lines" in contract
        assert "~250 words" not in contract
        # The two lines the workflows grep for are untouched, and still first
        # and last in the emitted shape.
        assert "First-Principles-Verdict: <PASS | CONCERNS | BLOCK>" in contract
        assert contract.rstrip().endswith("[FIRST-PRINCIPLES-REVIEWED] <head sha>")
        for name in FP_LANES:
            workflow = _workflow(name)
            assert _reads_header_anchored(workflow, "First-Principles-Verdict:"), name
        # The subtraction-only stance and the SYSTEM RULES block stay.
        assert contract.startswith("SYSTEM RULES (non-negotiable")
        assert "EVERY suggestion you emit must be a SUBTRACTION" in contract
        # Security boundaries, repo context and the user-count rule stay.
        assert "REPO CONTEXT: Kiro Crew is an open-source AI agent platform" in contract
        assert "DO NOT REASON FROM AN ASSUMED USER COUNT, in either direction" in contract
        assert "the AGENT is untrusted with respect to its own governance" in contract

    def test_over_engineering_is_judged_against_the_frozen_goal(self) -> None:
        # The body below `## What changed` is regenerated each round to match
        # the diff, so it cannot be the Goal a mechanism is measured against.
        # An out-of-goal mechanism must surface as a removable Subtraction, and
        # an older PR without the frozen headings still has a Goal to read.
        flat = " ".join(_fp_contract().split())
        assert "THE GOAL: when the description has a `**Goal:**` line" in flat
        assert "never from `## What changed` or anything below it" in flat
        assert "When there is no `**Goal:**` line (an older PR)" in flat
        # Today's template already has `## Problem / Motivation`, so that
        # heading alone must not switch a PR into frozen-goal mode.
        assert "a `## Problem / Motivation` section or" not in flat
        assert "does any mechanism go beyond the Goal's scope" in flat
        assert "If so, is that justified?" in flat
        assert "MINIMALITY PRINCIPLE: the change fits the original Goal" in flat
        assert "tagged `oversized`" in flat
        assert "`Clears when: the mechanism is removed, or a human amends the Goal.`" in flat


class TestFirstPrinciplesOneStatementPerProblem:
    """A review that says the same three items three times -- under
    `### Not justified as shipped`, again under `### Watch`, again under
    `### Subtractions` -- runs to ~600 words against a 180-word cap and buries
    the finding under the sections that restate it; a line of the model's own
    narration above the verdict header adds noise at the top. Collapsing the
    inventory does not fix that: the restating sections are what the template
    asks for. So the item entry is the finding, its `Clears when:` and its
    `Subtraction:` together, there is no later section, and the workflow
    trims anything before the header and counts the words that remain."""

    def test_the_item_entry_is_the_only_place_a_problem_appears(self) -> None:
        contract = _fp_contract()
        shape = contract.split("Output EXACTLY this shape")[1]
        assert "that entry is the item's ONLY\nappearance outside the inventory" in contract
        assert "there is no later section that\nsays it again" in contract
        assert "Never write a Watch, Subtractions or Suggestions heading" in contract
        assert "a second section that restates them\nis what buried the finding" in contract
        # The two restating sections are gone from the emitted shape.
        assert "### Watch" not in shape
        assert "### Subtractions" not in shape
        # What survives: the problems, the collapsed audit trail, the blockers.
        assert (
            shape.index("### Not justified as shipped")
            < shape.index("### What this change ships")
            < shape.index("### Blockers")
        )
        # Both per-item lines are named as lines ON the entry, not sections.
        assert "`Clears when: <the concrete evidence or change that resolves it>` --" in shape
        assert "`Subtraction: <the exact symbol/field/file" in shape
        # A Blocker is evidence for an item already listed once, not a copy.
        assert (
            "the Blockers entry carries the evidence, not a\nsecond copy of the reason" in contract
        )

    def test_the_style_bans_narration_and_says_each_problem_once(self) -> None:
        contract = _fp_contract()
        assert "NO narration of your own process" in contract
        assert "the verdict header is the FIRST byte of your last\nmessage" in contract
        assert "Each problem is\nstated ONCE" in contract
        assert "the\nworkflow counts them and flags an overrun" in contract

    def test_the_rule_the_local_loop_parses_still_holds(self) -> None:
        # The kirocrew-prepare-pr loop reads items out of `### Not justified as shipped`
        # by bullet + continuation lines, so an entry shaped as the contract
        # now asks (bullet, then indented `Clears when:` / `Subtraction:`)
        # must yield ONE item carrying both lines, not three.
        mod = _review_contract_module()
        body = (
            "First-Principles-Verdict: CONCERNS\n\n"
            "**punchline**\n\n"
            "### Not justified as shipped\n"
            "- Item 4 — unjustified move: reinstates pills against SidePanel.tsx:1459.\n"
            "  Subtraction: defer `panelTabStyles.ts`; keep `.side-tab-active`.\n"
            "  Clears when: a linked report names who misread the fused tabs.\n"
            "- Item 5 — undeclared: hide controls relabelled to X, description silent.\n\n"
            "### What this change ships\n"
            "<details><summary>Inventory (2 items) — 0 justified</summary>\n"
            "1. pills — unjustified move\n2. X icon — undeclared\n</details>\n\n"
            "[FIRST-PRINCIPLES-REVIEWED] abc\n"
        )
        items = mod.design_section_items(body)
        assert [section for section, _ in items] == [
            "Not justified as shipped",
            "Not justified as shipped",
        ]
        first = items[0][1]
        assert "Clears when: a linked report" in first
        assert "Subtraction: defer `panelTabStyles.ts`" in first
        assert "Item 5" not in first
        # `Clears when:` is the entry's LAST line for a reason: CLEARS_WHEN_RE
        # runs on the collapsed item and reads to its end, so a line after it
        # would be swallowed into the clearance. The contract orders
        # `Subtraction:` first, and the extracted clearance stays clean.
        clears = mod.CLEARS_WHEN_RE.search(first)
        assert clears is not None
        assert clears.group(1).strip() == "a linked report names who misread the fused tabs."
        assert "Subtraction" not in clears.group(1)
        contract = _fp_contract()
        shape = contract.split("Output EXACTLY this shape")[1]
        assert shape.index("`Subtraction: <") < shape.index("`Clears when: <")
        assert "It is the\nLAST line of the entry" in contract


class TestFirstPrinciplesShellSyntax:
    """Parse-check every `run:` block in both lanes.

    A workflow with a shell syntax error still parses as valid YAML and every
    string-matching test still passes -- the job simply dies at runtime, and for an
    advisory lane that surfaces as a red check nobody has to act on. This caught a
    truncated closing quote that an editing script left behind, which had silently
    swallowed the following steps into one `run:` body.
    """

    def _run_blocks(self, name: str) -> list[tuple[str, str]]:
        workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
        job = next(iter(workflow["jobs"].values()))
        return [
            (step.get("name", f"step {n}"), step["run"])
            for n, step in enumerate(job["steps"])
            if isinstance(step.get("run"), str)
        ]

    @pytest.mark.parametrize("lane", FP_LANES)
    def test_every_run_block_parses(self, lane: str, tmp_path: Path) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("run blocks are Bash; skip where Bash is absent")
        blocks = self._run_blocks(lane)
        assert blocks, f"{lane}: no run blocks found -- extraction is broken"
        for step_name, script in blocks:
            path = tmp_path / "step.sh"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [bash, "-n", str(path)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert result.returncode == 0, f"{lane} / {step_name}: {result.stderr.strip()}"


class TestFirstPrinciplesScopeGateBehavior:
    """Execute the ACTUAL surface-classification shell extracted from both lanes
    against a case table. A broken path regex fails here instead of silently
    skipping the reviewer on every real change (a green, invisible loss) or
    running a 2x-rate-card model on a docs-only diff."""

    def _classifier(self, name: str) -> str:
        workflow = _workflow(name)
        script = _step_script(workflow, "Detect reviewable surface")
        start = script.index('relevant="$(grep')
        end = script.index('if [ -n "$relevant" ]', start)
        return script[start:end]

    @pytest.mark.parametrize("lane", FP_LANES)
    @pytest.mark.parametrize(
        ("touched", "want"),
        [
            # A plain FIX of existing backend code now RUNS: judging whether it
            # reached the cause is this lane's whole point.
            ("src/kiro_crew/session.py", True),
            ("website/src/pages/Thing.tsx", True),
            ("config/defaults.json", True),
            ("scripts/check_brand_name.py", True),
            # This lane reviews its own kind of change too.
            (".github/workflows/first-principles-review.yml", True),
            # A mixed diff runs on the strength of its one source file.
            ("docs/guides/x.md\nsrc/kiro_crew/session.py", True),
            # Capability-free diffs skip: tests ship no capability, and docs,
            # screenshots and generated files never match at all.
            ("test/test_session.py", False),
            ("src/kiro_crew/apps/builtins/meetings/tests/test_routes.py", False),
            ("website/src/pages/Thing.test.tsx", False),
            ("docs/ci/ci-and-reviews.md", False),
            ("temp-screenshots/feature/shot.png", False),
            ("CHANGELOG.md", False),
            ("", False),
        ],
    )
    def test_surface_classification(self, lane: str, touched: str, want: bool) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("surface classification runs only under Bash")
        block = self._classifier(lane)
        # The file list arrives through the ENVIRONMENT, not argv: a multi-line
        # value survives intact that way, while Windows argv conversion (MSYS)
        # mangles an embedded newline and the case silently classified as "no
        # match". The workflow itself feeds this from `git diff` output, which is
        # newline-separated, so the env form is the faithful one.
        script = 'touched="$TOUCHED"\n' + block + '\nprintf "%s" "${relevant:+true}"'
        out = subprocess.run(
            [bash, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "TOUCHED": touched},
        )
        assert out.returncode == 0, out.stderr
        assert (out.stdout == "true") is want, f"{lane}: {touched!r} -> {out.stdout!r}"


class TestFirstPrinciplesIntentCapSurvivesALongBody:
    """Execute the ACTUAL PR-intent cap from both lanes against a body far past
    the cap.

    `printf '%s' "$stripped" | head -c 8000` reads as harmless and is not: `head`
    closes the pipe the moment it has its bytes, the upstream `printf` then takes
    EPIPE, and `pipefail` + the runner's default `bash -e` kill the whole step.
    A long PR description therefore aborted the reviewer before it ran, and the
    lane went on to report that as a fact about the contributor's diff. The `|| `
    fallback could not rescue it because it was the same construct.

    Only EXECUTING the block at a size past the pipe's capacity can see this --
    every string-matching test in this file passed while it was broken.
    """

    def _cap_block(self, lane: str) -> str:
        # The cap lives in the ONE shared capture both lanes source, so the block
        # under test is the block that runs. Assert the lane reaches it, then
        # execute the script's own copy: extracting per lane would assert against
        # a string the workflow does not contain.
        assert CAPTURE_SOURCE_LINE in _workflow(lane), lane
        script = CAPTURE_SCRIPT.read_text(encoding="utf-8")
        start = script.index("# Cap at 8000 bytes")
        end = script.index('rm -f "$full"', start) + len('rm -f "$full"')
        return script[start:end]

    @pytest.mark.parametrize("lane", FP_LANES)
    # 100_000 is past the old construct's abort threshold (the cap plus a pipe
    # buffer). Read the body from a tmp_path file: Windows caps the complete
    # CreateProcess environment at 32,767 characters.
    @pytest.mark.parametrize("body_bytes", (0, 100, 8000, 8001, 100_000))
    def test_cap_never_aborts_the_step(self, lane: str, body_bytes: int, tmp_path: Path) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the cap block is Bash; skip where Bash is absent")
        intent = tmp_path / "pr-intent.txt"
        body = tmp_path / "body.txt"
        body.write_bytes(b"x" * body_bytes)
        # Reproduce the step's own prologue: `pipefail` plus the runner's `bash -e`
        # are exactly what turned an EPIPE into a dead step.
        script = 'set -uo pipefail\nstripped="$(cat "$BODY_FILE")"\n' + self._cap_block(lane)
        out = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                **os.environ,
                "BODY_FILE": str(body),
                "INTENT": str(intent),
            },
            cwd=tmp_path,
        )
        assert out.returncode == 0, (
            f"{lane}: capping a {body_bytes}-byte body killed the step "
            f"(rc={out.returncode}) {out.stderr.strip()}"
        )
        written = intent.read_text(encoding="utf-8")
        prose = written.split("\n", 1)[0]
        assert len(prose) == min(body_bytes, 8000), f"{lane}: capped to {len(prose)}"
        marker = "[description TRUNCATED at 8000 bytes]"
        assert (marker in written) is (body_bytes > 8000), f"{lane}: marker wrong"

    @pytest.mark.parametrize("lane", FP_LANES)
    def test_cap_still_does_not_split_multibyte_utf8(self, lane: str, tmp_path: Path) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the cap block is Bash; skip where Bash is absent")
        # 7999 ASCII bytes + one 3-byte character: byte 8000 lands in the MIDDLE of
        # it, so a bare byte cap would leave an invalid UTF-8 tail. The Perl cap
        # must drop that partial character.
        intent = tmp_path / "pr-intent.txt"
        body = tmp_path / "body.txt"
        body.write_text("x" * 7999 + "€", encoding="utf-8")
        script = 'set -uo pipefail\nstripped="$(cat "$BODY_FILE")"\n' + self._cap_block(lane)
        out = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                **os.environ,
                "BODY_FILE": str(body),
                "INTENT": str(intent),
            },
            cwd=tmp_path,
        )
        assert out.returncode == 0, out.stderr
        raw = intent.read_bytes()  # bytes, so a split multibyte tail would survive
        assert raw.decode("utf-8").split("\n", 1)[0] == "x" * 7999


class TestIntentReadFailureFailsClosed:
    """Execute the ACTUAL PR-intent read from both lanes with ``gh`` stubbed.

    A bare `2>/dev/null || true` collapses a failed API read onto the same
    empty string as a PR with no description, which would have the reviewer
    judge a PR that appears to state no intent and blame the author for a
    description the workflow never read. These cases pin the three outcomes apart: a read
    that succeeds is judged as written, a transient failure is absorbed by the
    bounded retry, and a read that never succeeds fails the step closed while
    naming the read as the cause.
    """

    def _read_block(self, lane: str) -> str:
        # The read lives in the ONE shared snapshot both lanes reach through the
        # ONE shared capture they source. Assert the lane reaches it, then
        # execute the capture's own copy of the block that consumes it.
        assert CAPTURE_SOURCE_LINE in _workflow(lane), lane
        script = CAPTURE_SCRIPT.read_text(encoding="utf-8")
        start = script.index('. "${KC_SCRIPT_DIR:')
        end = script.index("# Strip embedded media")
        return script[start:end]

    def _run_read(self, tmp_path: Path, lane: str, gh_status: int = 0, fail_first: int = 0):
        bash = _bash()
        if bash is None:
            pytest.skip("the read block is Bash; skip where Bash is absent")
        # The snapshot fetches the WHOLE PR object once and splits the title
        # and the description out of it, so the stub answers with that object
        # rather than with the composed text two separate reads returned.
        body_file = tmp_path / "api-reply.json"
        body_file.write_text('{"title":"t","body":"prose"}\n', encoding="utf-8")
        attempts = tmp_path / "gh-attempts"
        gh = tmp_path / "gh"
        stub = f'#!/bin/sh\nprintf x >> "{attempts}"\n'
        if gh_status:
            # Stand in for an API failure on every attempt (5xx, rate limit).
            stub += f'echo "gh: could not reach the API" >&2\nexit {gh_status}\n'
        elif fail_first:
            stub += (
                f'if [ "$(wc -c < "{attempts}")" -le {fail_first} ]; then\n'
                '  echo "gh: HTTP 502" >&2\n'
                "  exit 1\n"
                "fi\n"
                f'cat "{body_file}"\n'
            )
        else:
            stub += f'cat "{body_file}"\n'
        gh.write_text(stub, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        out_file = tmp_path / "raw-out.txt"
        # Reproduce the step's own prologue (`pipefail` plus the runner's
        # `bash -e`), then persist `$raw` so the assertion reads what the rest
        # of the step would have been handed.
        script = (
            "set -uo pipefail\n"
            + self._read_block(lane)
            + f'\nprintf \'%s\' "$raw" > "{out_file}"\n'
        )
        result = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                # tmp_path first so the `gh` stub wins; starve any real gh of
                # credentials so a stub-resolution failure can never turn into
                # a live API call. `_stub_path` includes the host's real `jq`,
                # which the read block needs to split the title and body out of
                # one API response and which the Windows runners' Git Bash does
                # not put under the Unix defaults -- without it the read fails
                # closed there and the case reads as a broken lane.
                "PATH": _stub_path(tmp_path),
                "GH_TOKEN": "",
                "GITHUB_TOKEN": "",
                "LC_ALL": "C",
                "REPO": "example/repo",
                "PR": "1",
                "TMPDIR": str(tmp_path),
                # This block is executed as `bash -c` text, so $BASH_SOURCE is
                # empty and the sibling-path source cannot resolve itself. The
                # real lanes source the capture from a file and need no override.
                "KC_SCRIPT_DIR": str(CAPTURE_SCRIPT.parent),
            },
            cwd=tmp_path,
        )
        return result, attempts, out_file

    @pytest.mark.parametrize("lane", FP_LANES)
    def test_successful_read_is_judged_as_written(self, lane: str, tmp_path: Path):
        result, attempts, out_file = self._run_read(tmp_path, lane)
        assert result.returncode == 0, result.stdout + result.stderr
        assert out_file.read_text(encoding="utf-8").startswith("Title: t"), out_file.read_text(
            encoding="utf-8"
        )
        assert attempts.read_text(encoding="utf-8") == "x", "retry fired on a good read"

    @pytest.mark.parametrize("lane", FP_LANES)
    def test_transient_read_failure_is_absorbed(self, lane: str, tmp_path: Path):
        result, attempts, out_file = self._run_read(tmp_path, lane, fail_first=1)
        assert result.returncode == 0, result.stdout + result.stderr
        assert out_file.read_text(encoding="utf-8").startswith("Title: t")
        assert attempts.read_text(encoding="utf-8") == "xx", "expected exactly one retry"

    @pytest.mark.parametrize("lane", FP_LANES)
    def test_unreadable_intent_fails_closed_not_silent(self, lane: str, tmp_path: Path):
        # The failure this pins: a read that never succeeds must fail the step
        # (re-runnable) instead of handing the reviewer an empty intent file.
        result, attempts, _ = self._run_read(tmp_path, lane, gh_status=1)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "This is a read failure, not a missing description" in result.stdout, result.stdout
        assert attempts.read_text(encoding="utf-8") == "xxx", "expected three attempts"


class TestForkFirstPrinciplesContractStateIsThreeValued:
    """`steps.contract.outputs.available` has three states and two of them are
    opposite facts.

    `false` means the contract step RAN and the contract is genuinely not on the
    base commit. EMPTY means the step never ran. Collapsing them made an
    intent-fetch failure surface as a GREEN check-run asserting "no contract on
    the base commit" when nothing had ever looked for the contract, alongside a
    comment asserting the revision ships no reviewable capability when the scope
    step had just found that it does: two confident claims, neither checked.

    Reachable only in the fork lane, whose intent fetch sits BETWEEN the scope
    gate and the contract step; the same-repo lane orders the contract step first.
    """

    def _verdict_script(self) -> str:
        workflow = _workflow("fork-first-principles-review.yml")
        return _step_script(workflow, "Capture first-principles verdict")

    @pytest.mark.parametrize(
        ("contract", "want"),
        [
            # The step ran and found no contract: an honest, green skip.
            ("false", "NO_CONTRACT"),
            # The step never ran: nobody looked, so this must NOT claim the
            # contract is missing -- it is an incomplete review (-> NEUTRAL).
            ("", "UNKNOWN"),
        ],
    )
    def test_absent_contract_and_never_looked_are_different(
        self, contract: str, want: str, tmp_path: Path
    ) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the verdict block is Bash; skip where Bash is absent")
        out_file = tmp_path / "gh-output"
        out_file.touch()
        proc = subprocess.run(
            [bash, "-e", "-c", self._verdict_script()],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                **os.environ,
                "SURFACE": "true",
                "CONTRACT": contract,
                "EXEC_FILE": "",
                "HEAD_SHA": "0" * 40,
                "GITHUB_OUTPUT": str(out_file),
                "RUNNER_TEMP": str(tmp_path),
            },
            cwd=tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        emitted = out_file.read_text(encoding="utf-8")
        assert (
            f"verdict={want}" in emitted
        ), f"contract={contract!r} -> {emitted.strip()!r}, wanted verdict={want}"

    def test_no_contract_and_scope_skip_no_longer_share_one_message(self) -> None:
        # The two are different facts: a scope skip is a statement about the
        # contributor's diff, a missing contract is a statement about THIS repo's
        # base commit and says nothing about the diff. The fork lane reported the
        # second with the first's wording, and the same-repo lane never did --
        # so this is drift back to the lane it says it mirrors.
        workflow = _workflow("fork-first-principles-review.yml")
        script = _step_script(workflow, "Post/update first-principles review comment")
        assert 'heading="⏭️ no contract on the base commit"' in script
        assert 'heading="⏭️ skipped"' in script
        # The diff-level claim must be reachable ONLY from the scope skip.
        ships_nothing = _line_containing(script, "ships no reviewable capability")
        assert "docs, tests or generated files only" in ships_nothing
