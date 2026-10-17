"""Review-workflow tests for how the security-scope lanes reach a conclusion: one scope
surface, the verdict header they parse, unmeasured scopes, the conclusion ladder and
the per-head monotonic floor.
"""

from __future__ import annotations

import difflib
import functools
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    _SCOPE_LANES,
    ROOT,
    _bash,
    _child_env,
    _proc_log,
    _prompt,
    _step_by_name,
    _step_script,
    _workflow,
)


@functools.lru_cache(maxsize=1)
def _scope_candidates():
    """Load `scripts/scope_candidates.py` for its `_VALIDATE_REFUSALS` table.

    Read from the module the fork lane actually runs, so a rename of a dict key
    is caught rather than mirrored in a second copy of the names here. Registered
    in `sys.modules` before exec because the script's dataclasses resolve their
    own annotations through it. `deny_diff.py` sits beside it and is imported by
    path at load time, so both modules must be reachable -- they are, as siblings
    under `scripts/`.
    """
    path = ROOT / "scripts" / "scope_candidates.py"
    spec = importlib.util.spec_from_file_location("scope_candidates_for_workflow_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestTheScopeSurfaceIsResolvedOnce:
    """Both scope lanes ask "is this in scope?" of ONE list, and skip green.

    The surface was already spelled twice before this lane existed
    (``denial-differential.yml``'s ``on.paths`` and the same-repo lane's resolve
    step) with nothing pinning them equal. A third copy in the fork lane would be
    the one that matters most: it is the copy that decides whether a FORK pull
    request is reviewed at all, so a list that drifts short there skips the review
    silently, on exactly the changes nobody in this repository wrote.
    """

    SENTINELS = ("SCOPE-SURFACE-BEGIN", "SCOPE-SURFACE-END")

    def _surface(self) -> list[str]:
        text = _workflow("security-scope-review.yml")
        begin, end = self.SENTINELS
        body = text.split(begin, 1)[1].split(end, 1)[0]
        return re.findall(r"^\s*'([^']+)'\s*$", body, re.M)

    def test_the_same_repo_lane_carries_the_extractable_list(self) -> None:
        text = _workflow("security-scope-review.yml")
        for sentinel in self.SENTINELS:
            assert text.count(sentinel) == 1, sentinel
        surface = self._surface()
        # Every entry is on its own line and quoted, because that IS the
        # extraction contract: the fork lane's sed drops anything else, so a
        # reformatted entry narrows the surface it resolves rather than failing.
        assert len(surface) >= 8, surface
        assert "src/kiro_crew/security/" in surface
        assert "scripts/deny_diff.py" in surface

    def test_the_fork_lane_extracts_that_list_and_spells_none_of_it(self) -> None:
        fork = _workflow("fork-security-scope-review.yml")
        script = _step_script(fork, "Resolve review scope")
        for sentinel in self.SENTINELS:
            assert sentinel in script, sentinel
        assert "LANE: .github/workflows/security-scope-review.yml" in fork
        # The pin that matters: not one path from the shared list is written out
        # here. A copy is how the fork lane comes to disagree with the same-repo
        # lane about what the security surface is.
        for path in self._surface():
            if path == ".github/workflows/security-scope-review.yml":
                continue  # the file it READS the list out of, not a copied entry
            assert path not in script, f"fork lane re-spells {path}"

    def test_a_short_extraction_refuses_instead_of_skipping_the_review(self) -> None:
        # An empty or truncated extraction resolves to "no surface touched" for
        # nearly every change, which is a silent global skip of a blocking lane.
        script = _step_script(_workflow("fork-security-scope-review.yml"), "Resolve review scope")
        assert 'if [ "${#surface[@]}" -lt 2 ]; then' in script
        assert "exit 1" in script
        # An unreadable label list buys the expensive answer, never the cheap one.
        assert 'echo "in_scope=true" >> "$GITHUB_OUTPUT"' in script

    #: Both scope lanes, because this property is the one the fail-closed ruling
    #: rests on and it was pinned on the fork lane alone for eleven rounds.
    #: ``_UNSETTLED_CONCLUSION = "error"`` is defensible only because an
    #: off-surface pull request never reaches the model at all -- otherwise a
    #: Bedrock outage would red a PR this lane would not have judged, which is the
    #: cost `docs/ci/ci-and-reviews.md` says the ruling does NOT pay. One lane
    #: keeping the gate while its twin lost it would make the doc true of half the
    #: PRs and silently false of the other half.
    OUT_OF_SCOPE_GATED_LANES = ("security-scope-review.yml", "fork-security-scope-review.yml")

    def test_both_floor_reads_ask_for_every_check_run_not_just_the_latest(self) -> None:
        """`GET /commits/{ref}/check-runs` defaults to `filter=latest`.

        `latest` is one check-run PER NAME, and while the floor step runs, that one is
        this run's OWN in-progress check-run -- so the default returns a listing with
        no prior in it, every prior reads as "never judged", and the floor silently
        holds nothing. A resampled clean re-run then publishes success over a
        confirmed regression, which is the single thing the floor exists to stop.
        The gate was inert exactly this way until it was measured.

        Both lanes, and only these two reads: a check-runs read whose purpose is the
        CURRENT state (pr-readiness) is correct on the default, so this is not a
        repo-wide rule about the parameter -- it is a rule about a read of history.
        """
        for lane, job, step in (
            ("security-scope-review.yml", "publish", "Enforce the per-head monotonic floor"),
            ("fork-security-scope-review.yml", "publish", "Decide the lane's conclusion"),
        ):
            body = str(_step_by_name(lane, job, step)["run"])
            reads = [line for line in body.splitlines() if "check-runs?check_name=" in line]
            assert reads, f"{lane}: the floor step no longer reads the check-runs listing"
            for line in reads:
                assert "filter=all" in line, (
                    f"{lane}: a floor read without `filter=all` sees only this run's own "
                    f"in-progress check-run, so the floor holds nothing: {line.strip()}"
                )

    def test_neither_lane_pays_a_model_call_out_of_scope(self) -> None:
        gated = {
            "aws-actions/configure-aws-credentials",
            "anthropics/claude-code-action",
        }
        for lane in self.OUT_OF_SCOPE_GATED_LANES:
            doc = yaml.safe_load(_workflow(lane))
            steps = doc["jobs"]["generate"]["steps"]
            seen = 0
            for step in steps:
                uses = str(step.get("uses", ""))
                if not any(g in uses for g in gated):
                    continue
                seen += 1
                assert "steps.scope.outputs.in_scope == 'true'" in str(
                    step.get("if", "")
                ), f"{lane}: {uses} is not gated on the resolved scope"
            assert seen == 2, f"{lane}: expected the assume and the model call to be gated"
            # The scope step has to resolve BEFORE the credential is minted, or the
            # gate saves nothing.
            names = [str(s.get("name") or s.get("uses")) for s in steps]
            scope_at = names.index("Resolve review scope")
            creds_at = next(i for i, n in enumerate(names) if "configure-aws-credentials" in n)
            assert scope_at < creds_at, f"{lane}: the credential is minted before the scope gate"

    def test_the_fork_scope_step_runs_where_its_two_inputs_exist(self) -> None:
        """Order is a correctness input here, and both ways of getting it wrong are silent.

        The step diffs ``base...head``, and a fork head is reachable only through
        ``refs/pull/N/head`` -- so before the fetch step there is no head object,
        the diff resolves empty, and EVERY fork pull request reads as touching
        nothing: a global skip of a blocking lane, reported as a pass. And the
        contract step is gated on this step's output, so ahead of it that gate
        reads an unset value and the contract is never extracted at all.
        """
        doc = yaml.safe_load(_workflow("fork-security-scope-review.yml"))
        names = [
            str(step.get("name") or step.get("uses")) for step in doc["jobs"]["generate"]["steps"]
        ]
        fetch = next(i for i, n in enumerate(names) if n.startswith("Fetch authentic diff"))
        scope = names.index("Resolve review scope")
        contract = names.index("Extract the review contract from the base commit")
        assert fetch < scope, "the scope diff would have no head object to read"
        assert scope < contract, "the contract's scope gate would read an unset output"
        # And the gate is really there, so the order above is load-bearing rather
        # than incidental.
        for step in doc["jobs"]["generate"]["steps"]:
            if step.get("name") == "Extract the review contract from the base commit":
                assert "steps.scope.outputs.in_scope == 'true'" in str(step.get("if", ""))

    def test_an_out_of_scope_fork_pr_completes_success_and_posts_no_comment(self) -> None:
        fork = _workflow("fork-security-scope-review.yml")
        decide = _step_script(fork, "Decide the lane's conclusion")
        # SUCCESS, not neutral and not skipped: pr-readiness reads a lane that only
        # reports `skipped` as one that has not posted yet, and waits forever.
        assert 'conclusion="success"; title="nothing to scope' in decide
        # The LITERAL `false`, never "not true": an empty value means `generate`
        # died before the scope step ran, which is unmeasured and must stay red.
        assert 'if [ "${IN_SCOPE:-}" = "false" ]; then' in decide
        assert "IN_SCOPE: ${{ needs.generate.outputs.in_scope }}" in fork
        assert "in_scope: ${{ steps.scope.outputs.in_scope }}" in fork
        # No comment for a pull request this lane did not review, matching the
        # same-repo lane -- and still a comment when in_scope is merely UNKNOWN.
        doc = yaml.safe_load(fork)
        for step in doc["jobs"]["publish"]["steps"]:
            name = str(step.get("name", ""))
            if name in ("Assemble the comment body", "Post/update the scope review comment"):
                assert "needs.generate.outputs.in_scope != 'false'" in str(step.get("if", "")), name


class TestBothScopeLanesTolerateAnIndentedVerdictHeader:
    """The contract DISPLAYS the header indented, so both captures must accept it.

    ``.github/review-prompts/security-scope.md`` shows ``Scope-Verdict:`` indented
    four spaces inside its output-contract block. A model that copies that
    indentation writes a header an anchored ``^Scope-Verdict:`` grep cannot see, and
    the lane then reads ``UNKNOWN`` on a clean, fully-adjudicated review -- the fork
    lane turns that into ``conclusion=failure``. Fail-closed, so not a hole, but it
    is this lane over-refusing a legitimate outcome, which is the failure class the
    lane exists to catch. The two lanes must answer identically, so the pin runs one
    input through each lane's OWN expression.
    """

    @staticmethod
    def _capture_line(workflow: str) -> str:
        for line in _workflow(workflow).splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Case-insensitively, because the capture is: an expression that
            # lowercases the line before comparing spells the header in lower
            # case, and a selector keyed to one casing would skip it.
            if "scope-verdict:" not in stripped.lower():
                continue
            # The ASSIGNMENT, named by shape rather than by the program it runs:
            # a pin keyed to one tool silently stops finding the capture the day
            # the capture changes tool, and then measures nothing.
            if '="$(' in stripped:
                return stripped
        raise AssertionError(f"{workflow}: no Scope-Verdict capture expression")

    def test_the_contract_still_displays_the_header_indented(self) -> None:
        """If it stops, the pin below is measuring nothing that happens."""
        contract = _prompt("security-scope.md")
        header = [line for line in contract.splitlines() if "Scope-Verdict:" in line]
        assert header, "the contract names no Scope-Verdict header"
        assert any(line != line.lstrip() for line in header), header

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    # Every whitespace form `[[:space:]]` matches inside a line, because the
    # capture's own class has to match it: a narrower one silently stops reading
    # a header the contract's own indentation could produce.
    @pytest.mark.parametrize("indent", ("", "    ", "\t", "\v", "\f", "\r"))
    def test_each_lane_reads_the_same_verdict_however_it_is_indented(
        self, workflow: str, indent: str, tmp_path: Path
    ) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the capture is Bash; skip where Bash is absent")
        review = tmp_path / "scope-review.md"
        review.write_text(
            f"{indent}Scope-Verdict: PASS\n\n[SCOPE-REVIEWED] deadbeef\n", encoding="utf-8"
        )
        line = self._capture_line(workflow)
        name = line.split("=", 1)[0]
        # Both spellings of the input are supplied, because the two lanes read
        # different ones: the same-repo lane pipes a `$summary` variable, the fork
        # lane greps the `$OUT` file it just wrote. The expression itself is taken
        # from the workflow verbatim -- a test that retyped it would pass while the
        # lane it describes stayed broken.
        script = "\n".join(
            [
                "set -uo pipefail",
                'summary="$(cat "$IN")"',
                'OUT="$IN"',
                line,
                f'printf %s "${name}"',
            ]
        )
        # `-e` IS the production flag, and running without it is what let this pin
        # pass while the lane aborted. A `run:` block with no `shell:` key gets
        # `bash -e {0}`, which the lane's own job log records, so an expression
        # whose status is non-zero dies at the assignment -- above whatever
        # fallback was written for it. Asserting the STATUS as well as the value
        # is the half that catches that: a capture may legitimately come back
        # empty, and must never take the step down on its way.
        out = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "IN": str(review)},
            cwd=tmp_path,
        )
        assert out.returncode == 0, (
            f"{workflow}: indent {indent!r} aborted the step (rc={out.returncode}) "
            f"under the runner's own `bash -e`: {out.stderr.strip()}"
        )
        assert out.stdout == "PASS", (
            f"{workflow}: indent {indent!r} captured {out.stdout!r} "
            f"(rc={out.returncode}) {out.stderr.strip()}"
        )

    #: How many ``Scope-Verdict:`` lines the many-headers review carries. Chosen
    #: well above the smallest count that makes a ``grep | head -n1`` pipeline
    #: close the pipe on its producer (measured between 200 and 500 on Linux), and
    #: small enough that the fixture is tens of kilobytes rather than megabytes.
    _MANY_HEADERS = 2000

    #: Reviews whose header the capture cannot return, and the value each must
    #: yield. Every one is an ordinary model outcome, and in every one the
    #: capture's own exit status decides whether the step lives to read its
    #: fallback. ``many-headers`` is the case where a value IS in hand when the
    #: read ends early, so an expression that merely suppresses the status would
    #: hand the lane a verdict it never finished reading.
    _NO_VERDICT_REVIEWS = {
        "no-header": ("The change refuses nothing new.\n\nNo header here.\n", ""),
        "header-shaped-prose": ("I would write Scope-Verdict as a header if asked.\n", ""),
        "many-headers": (None, "PASS"),
    }

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    @pytest.mark.parametrize("review_kind", sorted(_NO_VERDICT_REVIEWS))
    def test_a_capture_that_returns_no_verdict_still_leaves_the_step_alive(
        self, workflow: str, review_kind: str, tmp_path: Path
    ) -> None:
        """An empty capture is an ANSWER, and must not be an abort.

        Each lane keeps a fallback one line under its capture -- ``UNKNOWN`` for the
        same-repo lane, a ``[ -n ]`` test for the fork lane -- so a review that names
        no verdict has a defined, fail-closed outcome. A ``run:`` block with no
        ``shell:`` key runs under ``bash -e``, and these steps add ``pipefail``, so a
        capture whose status is non-zero dies ABOVE that fallback: the lane writes no
        verdict output at all, its status step reads an empty verdict, and the comment
        that would have named the cause is never posted. A red either way, but one of
        them tells nobody why.

        The status assertion is the whole point. Asserting only the value passes an
        expression that returns the right value and takes the step down anyway.
        """
        bash = _bash()
        if bash is None:
            pytest.skip("the capture is Bash; skip where Bash is absent")
        body, expected = self._NO_VERDICT_REVIEWS[review_kind]
        if body is None:
            body = "Scope-Verdict: PASS\n" + "Scope-Verdict: BLOCK\n" * self._MANY_HEADERS
        review = tmp_path / "scope-review.md"
        review.write_text(body, encoding="utf-8")
        line = self._capture_line(workflow)
        name = line.split("=", 1)[0]
        script = "\n".join(
            [
                "set -uo pipefail",
                'summary="$(cat "$IN")"',
                'OUT="$IN"',
                line,
                f'printf %s "${name}"',
            ]
        )
        out = subprocess.run(
            [bash, "-e", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "IN": str(review)},
            cwd=tmp_path,
        )
        assert out.returncode == 0, (
            f"{workflow}: a {review_kind} review aborted the step "
            f"(rc={out.returncode}) under the runner's own `bash -e`, above the "
            f"fallback written for it: {out.stderr.strip()}"
        )
        assert out.stdout == expected, (
            f"{workflow}: a {review_kind} review captured {out.stdout!r}, " f"expected {expected!r}"
        )


class TestTheScopeLanesKeepTheirCopiedBlocksEqual:
    """The four blocks the fork lane keeps as COPIES of the same-repo lane's.

    The surface list is the one block the fork lane reads out of the same-repo lane
    at run time (`TestTheScopeSurfaceIsResolvedOnce`). These four stay copies, for
    the reason written at each fork copy: read out of that file, the model call's
    arguments would follow the pull request's base checkout, the probe and the
    header capture would be shell run as read, and the two comment bodies differ by
    design around the one stamp they share.

    A copy drifts, and a pin written per invariant catches only the drift someone
    thought to pin. So each test compares the WHOLE block across the two lanes and
    names every lane-native difference: a difference not listed here fails, the
    unforeseen ones included. When the two lanes stamp a body is run per lane in
    `test_ai_review_workflows_verdict_publication.py`; what is held here is that
    the stamp is one spelling.
    """

    SAME_REPO = "security-scope-review.yml"
    FORK = "fork-security-scope-review.yml"

    #: The one argument the two model calls set apart: the turn budget, a per-lane
    #: cost setting rather than part of the fence. Both values are named, so moving
    #: either one is an edit to this table.
    ARGUMENT_DIFFERENCES = {"--max-turns": ("60", "80")}

    #: The step each lane's credential-shape probe on the normalized corpus runs in.
    PROBE_STEPS = {
        SAME_REPO: "Validate the candidates against the base corpus",
        FORK: "Validate candidates against the base-owned corpus",
    }

    #: Each lane's own spellings in its probe, mapped onto one form: the corpus
    #: path, the interpreter's name, and the output key its own publish job reads the
    #: refusal code from.
    PROBE_REWRITES = {
        SAME_REPO: (('echo "refusal=', 'echo "<code>='),),
        FORK: (
            ('"$NORMALIZED"', "normalized.json"),
            ("python3 ", "python "),
            ('echo "rc=', 'echo "<code>='),
        ),
    }

    #: The same-repo lane writes `adjudicate=false` before each refusal; the fork
    #: lane writes it once, at the top of its step. Either way a refused corpus is
    #: never handed to the legs.
    PROBE_DROPPED = {SAME_REPO: ('echo "adjudicate=false" >> "$GITHUB_OUTPUT"',), FORK: ()}

    #: What each lane's header capture assigns, and what it reads: the same-repo
    #: lane the `$summary` it already holds, the fork lane the `$OUT` file it just
    #: wrote. The awk program between them is one program.
    CAPTURE_ENDS = {SAME_REPO: ("model", '<<< "$summary"'), FORK: ("v", '"$OUT"')}

    STAMP = "[SCOPE-REVIEWED] $HEAD"

    #: How each lane reads the stamp in the model's own review, from the same two
    #: inputs its capture reads.
    STAMP_READS = {
        SAME_REPO: 'grep -qF "[SCOPE-REVIEWED] $HEAD" <<< "$summary"',
        FORK: 'grep -qF "[SCOPE-REVIEWED] $HEAD" "$REVIEW"',
    }

    def _model_call_arguments(self, workflow: str) -> list[str]:
        doc = yaml.safe_load(_workflow(workflow))
        calls = [
            str(step["with"]["claude_args"])
            for step in doc["jobs"]["generate"]["steps"]
            if "anthropics/claude-code-action" in str(step.get("uses", ""))
        ]
        assert len(calls) == 1, f"{workflow}: expected one model call, found {len(calls)}"
        return [line.strip() for line in calls[0].splitlines() if line.strip()]

    def test_the_model_call_arguments_agree(self) -> None:
        same = self._model_call_arguments(self.SAME_REPO)
        fork = self._model_call_arguments(self.FORK)
        for flag, (same_value, fork_value) in self.ARGUMENT_DIFFERENCES.items():
            assert f"{flag} {same_value}" in same, (flag, same)
            assert f"{flag} {fork_value}" in fork, (flag, fork)

        def shared(args: list[str]) -> list[str]:
            return [arg for arg in args if arg.split(" ", 1)[0] not in self.ARGUMENT_DIFFERENCES]

        assert shared(same) == shared(fork), (shared(same), shared(fork))

    def _probe(self, workflow: str) -> list[str]:
        script = str(_step_by_name(workflow, "validate", self.PROBE_STEPS[workflow])["run"])
        lines = [line.strip() for line in script.splitlines()]
        start = next(
            i
            for i, line in enumerate(lines)
            if line.startswith("cp ") and line.endswith(" scrub-probe.json")
        )
        end = lines.index('echo "adjudicate=true" >> "$GITHUB_OUTPUT"', start)
        block = [
            line
            for line in lines[start:end]
            if line and not line.startswith("#") and line not in self.PROBE_DROPPED[workflow]
        ]
        for old, _new in self.PROBE_REWRITES[workflow]:
            # A listed spelling absent from the copy is a stale entry here.
            assert any(old in line for line in block), (workflow, old)
        for old, new in self.PROBE_REWRITES[workflow]:
            block = [line.replace(old, new) for line in block]
        return block

    def test_the_corpus_probe_agrees(self) -> None:
        same, fork = self._probe(self.SAME_REPO), self._probe(self.FORK)
        assert same == fork, "\n".join(difflib.unified_diff(same, fork, lineterm=""))
        # And it is the probe: the redactor's changed answer is what refuses.
        assert 'if [ "$probe" = "10" ]; then' in same

    def test_the_verdict_header_capture_agrees(self) -> None:
        programs = {}
        for workflow, (name, source) in self.CAPTURE_ENDS.items():
            line = TestBothScopeLanesTolerateAnIndentedVerdictHeader._capture_line(workflow)
            shape = rf"{re.escape(name)}=\"\$\(awk '([^']*)' {re.escape(source)}\)\""
            match = re.fullmatch(shape, line)
            assert match, f"{workflow}: the capture is not `{name}=\"$(awk '...' {source})\"`"
            programs[workflow] = match.group(1)
        assert programs[self.SAME_REPO] == programs[self.FORK], programs

    def test_the_stamp_agrees(self) -> None:
        for workflow in (self.SAME_REPO, self.FORK):
            lines = [
                line.strip()
                for line in _workflow(workflow).splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            written = [
                match.group(1)
                for line in lines
                for match in re.finditer(r'echo "(\[SCOPE-REVIEWED\][^"]*)"', line)
            ]
            assert written, f"{workflow}: no stamp is written"
            assert set(written) == {self.STAMP}, (workflow, written)
            reads = [line for line in lines if "grep" in line and "[SCOPE-REVIEWED]" in line]
            assert len(reads) == 1, (workflow, reads)
            assert self.STAMP_READS[workflow] in reads[0], (workflow, reads)
            # The stamp is the guarded upsert's proof, so both key it the same way.
            assert 'MARKER="<!-- security-scope-review -->"' in lines, workflow
            assert any(
                line.startswith('guarded_comment_upsert "$MARKER" "[SCOPE-REVIEWED]" ')
                for line in lines
            ), workflow


class TestTheForkLaneNamesARemedyThatClearsAForkPullRequest:
    """A fork PR clears this lane with `/ai-review override scope`, as a same-repo one does.

    The Stage-2 lane's `generate` job reads the record before it mints the Bedrock
    credential, and `publish` completes the check `success` past the per-head
    floor. So the lane's own messages name the override as a remedy and no line
    may still tell a fork contributor that it does nothing.
    """

    STALE = (
        "does NOT clear",
        "reads no /ai-review override",
        "consumes NO `/ai-review override",
        "consumes no override marker",
    )

    def test_no_fork_lane_message_says_the_override_does_nothing(self) -> None:
        fork = _workflow("fork-security-scope-review.yml")
        for line in fork.splitlines():
            assert not any(stale in line for stale in self.STALE), line

    def test_the_fork_lane_offers_it_where_a_row_was_redacted(self) -> None:
        fork = _workflow("fork-security-scope-review.yml")
        assert "/ai-review override scope <sha>: <reason>" in _step_script(
            fork, "Fold the reports into one verdict"
        )
        assert "/ai-review override scope $HEAD: <reason>" in _step_script(
            fork, "Assemble the comment body"
        )

    def test_the_same_repo_lane_still_offers_it(self) -> None:
        assert "/ai-review override scope" in _workflow("security-scope-review.yml")


class TestTheTwoSecurityGatesScopeOneSurface:
    """`SCOPE-SURFACE` and `denial-differential.yml`'s `paths:` share a core.

    Both same-repo gates answer a question about the same thing -- a tightened deny
    rule -- from different sides: the differential gates the committed corpus, the
    scope review probes the boundary it does not cover yet. Each carries its own
    hand-written list of the files that make a change a security change, and they
    overlap on seven entries with nothing holding those seven equal. A file added to
    one list and not the other is a change one gate measures and the other skips,
    which reads as a gate that passed rather than a gate that never ran.

    Pinned rather than merged into one file: each list has entries the other must
    NOT carry (each names its own workflow, and the scope lane also names the model
    prompt and the two scripts only it runs), so a single shared list would need a
    per-gate exclusion mechanism to express what two literals already say. The pin
    reads one workflow to hold another, the same idiom the fork lane uses to read
    this very array out of the same-repo lane at the base ref.
    """

    #: Entries each gate carries alone, with the reason it is not shared.
    _SCOPE_ONLY = frozenset(
        {
            "scripts/scope_candidates.py",  # only this lane runs it
            "scripts/scope_redact.py",  # only this lane runs it
            ".github/review-prompts/security-scope.md",  # only this lane has a model
            ".github/workflows/security-scope-review.yml",  # its own workflow
        }
    )
    _DIFFERENTIAL_ONLY = frozenset({".github/workflows/denial-differential.yml"})

    @staticmethod
    def _normalize(entry: str) -> str:
        """One spelling for one surface.

        The scope lane's array is `git diff` pathspecs, where a trailing `/` means
        the directory; the differential's is an Actions `paths:` glob, where `/**`
        means the same. Comparing them raw reports a difference that is only the
        two tools' syntax.
        """
        return entry.rstrip("/").removesuffix("/**").rstrip("/")

    def _scope_surface(self) -> list[str]:
        """The array between the extraction sentinels, as the fork lane reads it."""
        lane = _workflow("security-scope-review.yml")
        inside: list[str] = []
        collecting = False
        for line in lane.splitlines():
            if "SCOPE-SURFACE-BEGIN" in line:
                collecting = True
                continue
            if "SCOPE-SURFACE-END" in line:
                collecting = False
                continue
            if not collecting:
                continue
            stripped = line.strip()
            if stripped.startswith("'") and stripped.endswith("'"):
                inside.append(stripped.strip("'"))
        assert len(inside) > 5, f"extracted only {len(inside)} surface path(s)"
        return inside

    def _differential_paths(self) -> list[str]:
        doc = yaml.safe_load(_workflow("denial-differential.yml"))
        triggers = doc.get("on") or doc.get(True) or {}
        paths = ((triggers.get("pull_request") or {}).get("paths")) or []
        assert len(paths) > 5, f"extracted only {len(paths)} path(s)"
        return [str(entry) for entry in paths]

    def test_the_shared_core_is_identical(self) -> None:
        scope_surface = {self._normalize(e) for e in self._scope_surface()}
        differential = {self._normalize(e) for e in self._differential_paths()}
        scope_only = {self._normalize(e) for e in self._SCOPE_ONLY}
        differential_only = {self._normalize(e) for e in self._DIFFERENTIAL_ONLY}

        assert scope_surface - scope_only == differential - differential_only, (
            "the two same-repo security gates disagree about the security surface. "
            f"only in the scope lane: {sorted(scope_surface - scope_only - differential)}; "
            f"only in the differential: {sorted(differential - differential_only - scope_surface)}. "
            "Add the file to BOTH lists, or declare it gate-specific in this test's "
            "_SCOPE_ONLY / _DIFFERENTIAL_ONLY with the reason it is not shared"
        )

    def test_every_declared_exclusive_entry_is_really_in_its_own_list(self) -> None:
        # Otherwise an exclusion outlives the entry it excused, and the next file
        # added to one list slips through under a stale name.
        scope_surface = {self._normalize(e) for e in self._scope_surface()}
        differential = {self._normalize(e) for e in self._differential_paths()}
        for entry in self._SCOPE_ONLY:
            assert self._normalize(entry) in scope_surface, entry
        for entry in self._DIFFERENTIAL_ONLY:
            assert self._normalize(entry) in differential, entry


class TestAnUnmeasuredScopeNeverPassesTheRequiredStatus:
    """`in_scope` is a TRI-STATE, and the gate that carries the check name reads it.

    `generate` writes that output in ONE step, `Resolve review scope`. Anything
    failing before it -- the runner hardening, the checkout, the resolver itself --
    leaves the output EMPTY, and a `!= "true"` test reads empty as "measured, and
    there was nothing to scope". The required status then exits 0 with no model call
    and no differential: an infrastructure failure publishes a green gate over a
    change nobody measured, on the one lane whose whole purpose is to refuse that.

    So the skip is bought by the resolver's own literal `false` and by nothing else.
    The step is executed here rather than grepped, because what matters is the exit
    code each value produces -- it shells out to nothing, so it runs as written.
    """

    STEP = ("security-scope-review.yml", "publish", "Security scope status")

    def _run(self, tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
        bash = _bash()
        if bash is None:
            pytest.skip("the required-status step is Bash")
        script = tmp_path / "status.sh"
        script.write_text(_step_by_name(*self.STEP)["run"])
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HEAD": "cafe1234cafe1234cafe1234cafe1234cafe1234",
            "ACTOR": "some-author",
            "IN_SCOPE": "",
            "VERDICT": "",
            "WHY": "",
            "HUMAN_OVERRIDE": "",
            "OVERRIDE_ACTOR": "",
        }
        env.update(overrides)
        return subprocess.run(
            [bash, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_child_env(env),
        )

    def test_an_empty_in_scope_fails_the_lane(self, tmp_path: Path) -> None:
        """The defect itself: `generate` died before it could answer."""
        got = self._run(tmp_path)
        assert got.returncode == 1, f"an unmeasured change passed the gate: {got.stdout}"
        assert "never resolved" in got.stdout, got.stdout

    def test_an_unrecognized_in_scope_fails_the_lane(self, tmp_path: Path) -> None:
        """Anything but the two known answers is also "never measured"."""
        assert self._run(tmp_path, IN_SCOPE="TRUE").returncode == 1
        assert self._run(tmp_path, IN_SCOPE="maybe").returncode == 1

    def test_the_resolvers_own_false_still_passes(self, tmp_path: Path) -> None:
        """The honest skip must stay green, or every out-of-scope PR reds."""
        got = self._run(tmp_path, IN_SCOPE="false")
        assert got.returncode == 0, _proc_log(got)
        assert "nothing to scope" in got.stdout

    def test_in_scope_true_still_reaches_the_verdict_ladder(self, tmp_path: Path) -> None:
        """The tri-state replaced a guard, and must not swallow the ladder below it."""
        passed = self._run(tmp_path, IN_SCOPE="true", VERDICT="PASS", WHY="zero rows")
        assert passed.returncode == 0, passed.stdout + passed.stderr
        blocked = self._run(tmp_path, IN_SCOPE="true", VERDICT="BLOCK", WHY="a row")
        assert blocked.returncode == 1
        unmeasured = self._run(tmp_path, IN_SCOPE="true", VERDICT="", WHY="no verdict")
        assert unmeasured.returncode == 1

    def test_a_human_override_is_still_read_first(self, tmp_path: Path) -> None:
        """An override clears the lane even when `generate` never resolved scope."""
        got = self._run(tmp_path, HUMAN_OVERRIDE="true", OVERRIDE_ACTOR="maintainer")
        assert got.returncode == 0, _proc_log(got)

    @pytest.mark.parametrize("workflow", _SCOPE_LANES)
    def test_neither_lane_decides_a_skip_on_not_true(self, workflow: str) -> None:
        """The CLASS, not the site. Both lanes, every step that binds the output.

        A tri-state read as a boolean is what this closes, so the pin is on the
        spelling that does it -- `!= "true"` on a variable fed from
        `needs.<job>.outputs.in_scope` -- rather than on the one step that was
        reported. The fork lane's `Decide the lane's conclusion` already matches the
        literal `false`; this keeps both from drifting back.
        """
        for job_id, job in (yaml.safe_load(_workflow(workflow)).get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                bound = [
                    name
                    for name, expr in (step.get("env") or {}).items()
                    if "outputs.in_scope" in str(expr)
                ]
                if not bound:
                    continue
                for line in (step.get("run") or "").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    for name in bound:
                        assert f'"${name}" != "true"' not in stripped, (
                            f"{workflow}:{job_id} step {index} decides on "
                            f"NOT-true, so an empty {name} takes the skip: {stripped}"
                        )


class TestScopeConclusionLadderLivesInOnePlace:
    """The scope-review conclusion ladder -- fold result x model header x marker x
    platform gap -> lane conclusion -- was a ~40-line shell `case`/`if` block
    hand-copied into BOTH lanes, and the two copies had already diverged on the
    platform-gap source. It now lives once, in `scripts/scope_candidates.py
    conclude`, and each lane only normalizes its signals, calls the script, and
    maps the returned word to its surface. These tests fail if a shell ladder
    reappears in either workflow -- the whole point of the collapse is that they
    cannot drift again. Same shape as the credential-scrub pins above: read one
    workflow to hold another to a contract.
    """

    SAME_REPO = "security-scope-review.yml"
    FORK = "fork-security-scope-review.yml"

    def test_both_lanes_delegate_the_conclusion_to_the_shared_table(self) -> None:
        # Same-repo invokes the staged harness via a $SC variable; the fork runs
        # the trusted default-branch checkout by path. Both name the subcommand,
        # and neither hands the table a lane of its own: the table takes none.
        same, fork = _workflow(self.SAME_REPO), _workflow(self.FORK)
        assert 'python "$SC" conclude ${lane_flag[@]+"${lane_flag[@]}"} \\' in same
        assert "python3 scripts/scope_candidates.py conclude \\" in fork
        assert "conclude --lane" not in same + fork

    def test_the_same_repo_lane_names_a_lane_only_to_a_base_table_that_requires_one(
        self,
    ) -> None:
        # The table runs from the BASE blob, and a base can be a revision whose
        # `conclude` still requires `--lane`. The lane reads the staged copy for that
        # revision's own parser line, so the needle must be absent from the table
        # this tree ships -- or the lane would hand `--lane` to a parser that refuses
        # it. The behaviour on both shapes is run in
        # `test_ai_review_workflows_verdict_publication.py`.
        run = _step_script(_workflow(self.SAME_REPO), "Post the scope verdict")
        needle = 'c.add_argument("--lane"'
        assert f"grep -qF '{needle}' \"$SC\" && lane_flag=(--lane same-repo)" in run
        assert needle not in (ROOT / "scripts" / "scope_candidates.py").read_text(encoding="utf-8")

    def test_same_repo_keeps_no_shell_conclusion_ladder(self) -> None:
        run = _step_script(_workflow(self.SAME_REPO), "Post the scope verdict")
        # The model-verdict `case` was the heart of the old shell ladder; it may
        # not live in the lane again. (Normalizing $FOLDED to the table's
        # vocabulary is not a ladder -- it hands a signal to the script.)
        assert 'case "$model" in' not in run
        # The mapping's own decisions -- BLOCK-from-gap, the CONCERNS downgrade --
        # must not be re-derived in shell here.
        assert 'why="a demonstrated platform gap' not in run
        assert 'verdict="CONCERNS"; why="the reviewer wrote BLOCK' not in run
        assert 'python "$SC" conclude ' in run

    def test_fork_keeps_no_shell_conclusion_ladder(self) -> None:
        run = _step_script(_workflow(self.FORK), "Decide the lane's conclusion")
        assert 'case "${MODEL_VERDICT' not in run
        # The fork's old single-source gap flag and its BLOCK branch are gone.
        assert 'gap="yes"' not in run
        assert 'title="BLOCK ' not in run
        assert "scripts/scope_candidates.py conclude " in run

    def test_both_lanes_feed_both_gap_sources_to_the_table(self) -> None:
        # The divergence that motivated the collapse was the fork reading the
        # script's gap alone. Both lanes must now compute BOTH the script gap
        # (NO VERDICT) and the model-prose gap (UNADJUDICATED:) and hand them over.
        for name, step in (
            (self.SAME_REPO, "Post the scope verdict"),
            (self.FORK, "Decide the lane's conclusion"),
        ):
            run = _step_script(_workflow(name), step)
            assert "NO VERDICT" in run, name
            assert "UNADJUDICATED:" in run, name
            assert "--gap-script" in run and "--gap-model" in run, name

    def test_the_conclusion_table_is_run_from_a_trusted_copy_on_both_lanes(self) -> None:
        # Same-repo holds the comment-write token on a merge-ref checkout, so it
        # must run the base-owned STAGED harness ($HARNESS), not `scripts/` from
        # the tree. The $SC path is assigned from $HARNESS and then executed.
        same = _step_script(_workflow(self.SAME_REPO), "Post the scope verdict")
        assert 'SC="${HARNESS:-}/scope_candidates.py"' in same
        assert 'python "$SC" conclude ' in same
        # The fork publish job checks out the DEFAULT branch (the trusted harness)
        # and runs no PR tree, so `scripts/scope_candidates.py` there is trusted.
        fork = _step_script(_workflow(self.FORK), "Decide the lane's conclusion")
        assert "scripts/scope_candidates.py conclude" in fork

    def test_the_fork_lane_hands_the_validate_refusal_code_to_the_table(self) -> None:
        # A `validate` refusal dispatches no leg, so the fold reads `no-report` and
        # the table's generic sentence for that row names no cause. The code the
        # refusal writes as a job output is an INPUT to the shared table, so the
        # cause and its remedy are decided in the one place both lanes read.
        decide = _step_by_name(self.FORK, "publish", "Decide the lane's conclusion")
        assert decide["env"]["VALIDATE_RC"] == "${{ needs.validate.outputs.rc }}"
        run = decide["run"]
        assert '--refusal "${VALIDATE_RC:-}"' in run
        # The remedy is a key of its own because a check-run title is capped at 255
        # characters; the lane selects it by key, never by line number.
        assert "remedy=\"$(printf '%s\\n' \"$conc\" | sed -n 's/^remedy=//p')\"" in run
        assert 'echo "remedy=$remedy" >> "$GITHUB_OUTPUT"' in run
        # Not re-derived in shell: the cause text is authored in the table, so no
        # value of `$VALIDATE_RC` is ever interpolated into a published verdict.
        assert "corpus-credential" not in run
        assert "corpus-uncheckable" not in run

    def test_the_emitted_refusal_codes_are_exactly_the_tables_keys(self) -> None:
        # The one live coupling nothing else pins: `validate` writes a bare string
        # (`rc=corpus-credential`), the fork lane hands it to `conclude --refusal`,
        # and the table names a cause ONLY on an exact key match against
        # `_VALIDATE_REFUSALS` -- an unrecognized code falls through to the generic
        # "no leg reported" sentence. So a rename on either side, the emitted string
        # or the dict key, silently regresses a named refusal to that generic
        # sentence with no other test failing. Match the two SETS both ways: every
        # code the workflow emits must be a table key (or it explains nothing), and
        # every table key must be emitted by some `validate` branch (or it is a dead
        # entry no run can reach). Read the dict from the module the lane runs, not
        # a second copy of the names here, which would be one more thing to drift.
        emitted = set(
            re.findall(
                r'echo "rc=(corpus-[a-z-]+)" >> "\$GITHUB_OUTPUT"',
                _workflow(self.FORK),
            )
        )
        assert emitted, "the validate step emits no rc=corpus-* code any more"
        keys = set(_scope_candidates()._VALIDATE_REFUSALS)
        assert emitted == keys, (
            "the rc=corpus-* codes the fork lane emits and the _VALIDATE_REFUSALS "
            f"keys have drifted: emitted={sorted(emitted)}, keys={sorted(keys)}. "
            "A code with no key regresses to the generic sentence; a key with no "
            "emitter is unreachable."
        )

    def test_the_same_repo_lane_hands_the_validate_refusal_code_to_the_table(self) -> None:
        # Same coupling as the fork lane's, on the lane that runs for a branch of
        # this repository: a credential-shape refusal in `validate` names its cause
        # as a job output, and the post step hands it to `conclude --refusal`. With
        # no code the table can only answer the generic "no leg reported" sentence.
        workflow = yaml.safe_load(_workflow(self.SAME_REPO))
        outputs = workflow["jobs"]["validate"]["outputs"]
        assert outputs["refusal"] == (
            "${{ steps.probe.outputs.refusal || steps.validate.outputs.refusal }}"
        )
        post = _step_by_name(self.SAME_REPO, "publish", "Post the scope verdict")
        assert post["env"]["VALIDATE_REFUSAL"] == "${{ needs.validate.outputs.refusal }}"
        run = post["run"]
        assert '--refusal "${VALIDATE_REFUSAL:-}"' in run
        # Authored in the table, so no value of the code reaches the verdict text.
        assert "corpus-credential" not in run
        assert "corpus-uncheckable" not in run
        emitted = set(
            re.findall(
                r'echo "refusal=(corpus-[a-z-]+)" >> "\$GITHUB_OUTPUT"',
                _workflow(self.SAME_REPO),
            )
        )
        keys = set(_scope_candidates()._VALIDATE_REFUSALS)
        assert emitted == keys, (
            "the refusal=corpus-* codes the same-repo lane emits and the "
            f"_VALIDATE_REFUSALS keys have drifted: emitted={sorted(emitted)}, "
            f"keys={sorted(keys)}."
        )

    def test_the_same_repo_fold_stages_the_harness_when_no_leg_reported(
        self, tmp_path: Path
    ) -> None:
        # A run with no leg report -- a `validate` refusal, or the nothing-new short
        # circuit -- is exactly the run whose conclusion the table has to name. The
        # post step and the per-head floor run that table from the copy this step
        # stages, so a fold that exits on "no report" before staging it leaves them
        # reading "the scope harness is absent at both refs" over a harness that is
        # committed at both. Run the REAL step bash against a repository that
        # commits the harness, with no report on disk.
        bash = _bash()
        if bash is None or os.name == "nt":
            pytest.skip("the fold step is exercised with Bash on POSIX runners")
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        for name in ("scope_candidates.py", "deny_diff.py"):
            shutil.copyfile(ROOT / "scripts" / name, repo / "scripts" / name)
        git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        gitconfig = tmp_path / "gitconfig"
        gitconfig.touch()
        git_env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
        git_env["GIT_CONFIG_NOSYSTEM"] = "1"

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=repo,
                env=_child_env(git_env),
            ).stdout.strip()

        git("init", "-q")
        git("add", "scripts")
        git("commit", "-q", "-m", "harness")
        sha = git("rev-parse", "HEAD")

        step = _step_by_name(self.SAME_REPO, "publish", "Fold the per-platform reports")
        script = tmp_path / "fold.sh"
        script.write_text(str(step["run"]), encoding="utf-8")
        harness = tmp_path / "scope-harness-publish"
        output = tmp_path / "github-output.txt"
        env = {
            **git_env,
            "BASE_SHA": sha,
            "HEAD_SHA": sha,
            "HARNESS": str(harness),
            "REDACTOR": "",
            "EXPECT_LEGS": "ubuntu-latest macos-latest windows-latest",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        }
        result = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo,
            env=_child_env(env),
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "folded=none" in output.read_text(encoding="utf-8").splitlines()
        for name in ("scope_candidates.py", "deny_diff.py"):
            staged = harness / name
            assert staged.is_file(), f"{name} was not staged on a run with no leg report"
            assert staged.read_bytes() == (repo / "scripts" / name).read_bytes(), name

    def test_a_floor_override_drops_the_refusal_remedy(self) -> None:
        # The floor can REPLACE the title with its own reason. A remedy that outlived
        # it would explain a sentence the check-run does not carry.
        run = _step_script(_workflow(self.FORK), "Decide the lane's conclusion")
        after_floor = run.split("floor_ok", 1)[1]
        assert after_floor.count('remedy=""') >= 2, after_floor.count('remedy=""')

    def test_the_fork_check_run_summary_promises_no_rows_it_has_none_of(self) -> None:
        # A run where no leg folded any rows has none to point at, in a PR comment or
        # anywhere else -- and on the refusal path that comment is not posted either.
        publish = _step_by_name(self.FORK, "publish", "Publish check-run")
        assert publish["env"]["REMEDY"] == "${{ steps.decide.outputs.remedy }}"
        run = publish["run"]
        assert "See the PR comment for the confirmed rows" not in run
        assert "No leg produced confirmed rows" in run
        assert '[ -n "${REMEDY:-}" ]' in run
        # The comment pointer survives only in the arm that HAS a deterministic body
        # to point at, which is the arm that stamps and publishes one.
        body_arm = run.split('elif [ -s "$BODY" ]', 1)
        assert len(body_arm) == 2, "the deterministic-body arm is gone"
        assert "Full review in the PR comment" in body_arm[1]
        assert "Full review in the PR comment" not in body_arm[0]


_SCOPE_HEAD = "cafe1234cafe1234cafe1234cafe1234cafe1234"


def _checkruns_json(
    *conclusions: str,
    external_id: str | None = "scope-pr-7-11-1",
    pr: int | None = 7,
) -> str:
    """A check-runs listing envelope, as the Checks API returns one.

    ``external_id`` is the fork lane's own stamp (``None`` for a same-repo job
    row, which cannot set one); ``pr`` is what the API attributes the row to.
    """
    rows = []
    for conclusion in conclusions:
        row: dict = {"status": "completed", "conclusion": conclusion}
        if external_id is not None:
            row["external_id"] = external_id
        if pr is not None:
            row["pull_requests"] = [{"number": pr}]
        rows.append(row)
    return json.dumps({"total_count": len(rows), "check_runs": rows})


class TestPerHeadMonotonicFloor:
    """A re-run of the SAME head must never publish a conclusion SOFTER than one
    the lane already stands behind for that head. The candidate set is
    model-sampled, so a confirmed row is not guaranteed to be re-proposed, and
    without this floor a clean re-run replaces a prior BLOCK. State is the lane's
    OWN completed check-run conclusion for the head, read from the Checks API.
    Both lanes, because a fix in one is the failure mode this change closes.
    """

    def _stub_dir(
        self, tmp_path: Path, checkruns: str | None, *, gh_fail: bool, floor_rc: int = 0
    ) -> Path:
        stub = tmp_path / "stub"
        stub.mkdir(exist_ok=True)
        # `python` / `python3` shims -- the same-repo step calls `python`, the fork
        # step calls `python3`. `floor_rc` makes the harness exit non-zero with no
        # stdout, which is what a bad flag or a defect in the table looks like to
        # the step: a floor computation that did not settle.
        for name in ("python", "python3"):
            shim = stub / name
            if floor_rc:
                shim.write_text(f"#!/usr/bin/env bash\nexit {floor_rc}\n", encoding="utf-8")
            else:
                shim.write_text(
                    f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
                )
            shim.chmod(0o755)
        # `gh` shim: emits the fixture VERBATIM and exits 0. Verbatim is the
        # point -- the step hands the whole response to the shared table, so a
        # malformed or empty body has to reach it byte for byte. `None` means
        # "use a well-formed empty listing"; `""` means the API answered with no
        # body at all, which is a distinct case and must stay distinct.
        fixture = tmp_path / "checkruns.json"
        body = '{"total_count":0,"check_runs":[]}' if checkruns is None else checkruns
        fixture.write_text(body, encoding="utf-8")
        gh = stub / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            + ("exit 1\n" if gh_fail else "")
            + 'if [ "$1" = "api" ]; then\n'
            f'  cat "{fixture}"\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        return stub

    # ---- same-repo lane: a separate step whose exit code is the floor ---------

    def _run_same_repo(
        self,
        tmp_path: Path,
        *,
        checkruns: str | None = None,
        gh_fail: bool = False,
        floor_rc: int = 0,
        **overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("the floor step is Bash and encodes the check name with jq")
        stub = self._stub_dir(tmp_path, checkruns, gh_fail=gh_fail, floor_rc=floor_rc)
        harness = tmp_path / "harness"
        harness.mkdir(exist_ok=True)
        shutil.copy(ROOT / "scripts" / "scope_candidates.py", harness / "scope_candidates.py")
        script = tmp_path / "floor.sh"
        script.write_text(
            _step_by_name(
                "security-scope-review.yml", "publish", "Enforce the per-head monotonic floor"
            )["run"]
        )
        env = {
            "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}",
            "GH_TOKEN": "x",
            "REPO": "example/repo",
            "PR": "7",
            "HEAD": _SCOPE_HEAD,
            "ACTOR": "some-author",
            "IN_SCOPE": "true",
            "VERDICT": "PASS",
            "HUMAN_OVERRIDE": "false",
            "HARNESS": str(harness),
        }
        env.update(overrides)
        return subprocess.run(
            [bash, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_child_env(env),
        )

    def test_same_repo_prior_failure_holds_a_soft_rerun_red(self, tmp_path: Path) -> None:
        got = self._run_same_repo(
            tmp_path, checkruns=_checkruns_json("failure", "success", external_id=None)
        )
        assert got.returncode == 1, _proc_log(got)
        assert "held at the harder verdict" in got.stdout

    def test_same_repo_new_head_with_no_prior_stays_green(self, tmp_path: Path) -> None:
        got = self._run_same_repo(tmp_path, checkruns='{"total_count":0,"check_runs":[]}')
        assert got.returncode == 0, _proc_log(got)

    def test_same_repo_unreadable_prior_fails_closed(self, tmp_path: Path) -> None:
        got = self._run_same_repo(tmp_path, gh_fail=True)
        assert got.returncode == 1, _proc_log(got)

    def test_same_repo_human_override_is_not_refloored(self, tmp_path: Path) -> None:
        # The override's clearance for this head is a deliberate softening; the
        # floor must leave it standing or the SHA-scoped escape hatch is unusable.
        got = self._run_same_repo(
            tmp_path,
            checkruns=_checkruns_json("failure", external_id=None),
            HUMAN_OVERRIDE="true",
        )
        assert got.returncode == 0, _proc_log(got)

    def test_same_repo_out_of_scope_enforces_no_floor(self, tmp_path: Path) -> None:
        got = self._run_same_repo(
            tmp_path,
            checkruns=_checkruns_json("failure", external_id=None),
            IN_SCOPE="false",
        )
        assert got.returncode == 0, _proc_log(got)

    # ---- fork lane: the decide step raises its own published conclusion -------

    def _run_fork_decide(
        self,
        tmp_path: Path,
        *,
        checkruns: str | None = None,
        gh_fail: bool = False,
        floor_rc: int = 0,
        model: str = "PASS",
    ) -> tuple[int, dict[str, str], str]:
        bash = _bash()
        if bash is None or shutil.which("jq") is None:
            pytest.skip("the decide step is Bash and encodes the check name with jq")
        stub = self._stub_dir(tmp_path, checkruns, gh_fail=gh_fail, floor_rc=floor_rc)
        review = tmp_path / "scope-review-output.md"
        review.write_text(
            f"Scope-Verdict: {model}\n\n[SCOPE-REVIEWED] {_SCOPE_HEAD}\n", encoding="utf-8"
        )
        body = tmp_path / "scope-verdict.md"
        body.write_text("no confirmed rows\n", encoding="utf-8")
        outputs = tmp_path / "github-output"
        outputs.touch()
        script = tmp_path / "decide.sh"
        script.write_text(
            _step_by_name(
                "fork-security-scope-review.yml", "publish", "Decide the lane's conclusion"
            )["run"]
        )
        env = {
            "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}",
            "GH_TOKEN": "x",
            "REPO": "example/repo",
            "PR": "7",
            "HEAD": _SCOPE_HEAD,
            "ADJUDICATE": "true",
            "IN_SCOPE": "true",
            "VALIDATE_RC": "0",
            "FOLD_RC": "0",
            "MODEL_VERDICT": model,
            "REVIEW": str(review),
            "BODY": str(body),
            "GITHUB_OUTPUT": str(outputs),
            "RUNNER_TEMP": str(tmp_path),
        }
        result = subprocess.run(
            [bash, str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_child_env(env),
        )
        parsed = dict(
            line.split("=", 1)
            for line in outputs.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        return result.returncode, parsed, _proc_log(result)

    def test_fork_prior_failure_raises_a_clean_run_to_failure(self, tmp_path: Path) -> None:
        rc, out, log = self._run_fork_decide(tmp_path, checkruns=_checkruns_json("failure"))
        assert rc == 0, log
        assert out.get("conclusion") == "failure", out

    def test_fork_new_head_with_no_prior_publishes_clean(self, tmp_path: Path) -> None:
        rc, out, log = self._run_fork_decide(
            tmp_path, checkruns='{"total_count":0,"check_runs":[]}'
        )
        assert rc == 0, log
        assert out.get("conclusion") == "success", out

    def test_fork_unreadable_prior_fails_closed(self, tmp_path: Path) -> None:
        rc, out, log = self._run_fork_decide(tmp_path, gh_fail=True)
        assert rc == 0, log
        assert out.get("conclusion") == "failure", out

    def test_fork_only_this_prs_prior_sets_the_floor(self, tmp_path: Path) -> None:
        # A failure row from ANOTHER PR on a shared head must not floor this PR.
        other = _checkruns_json("failure", external_id="scope-pr-999-1-1")
        rc, out, log = self._run_fork_decide(tmp_path, checkruns=other)
        assert rc == 0, log
        assert out.get("conclusion") == "success", out

    def test_fork_a_prior_unsettled_flake_clears_on_a_clean_rerun(self, tmp_path: Path) -> None:
        # THE fix: a prior fork run that could not measure marked its own check-run
        # unsettled (a [scope-floor:unsettled] title prefix). It reds its own run,
        # but sets no floor, so this clean re-run publishes clean -- a flake no
        # longer reds the head forever.
        flake = json.dumps(
            {
                "total_count": 1,
                "check_runs": [
                    {
                        "status": "completed",
                        "conclusion": "failure",
                        "external_id": "scope-pr-7-11-1",
                        "output": {
                            "title": "[scope-floor:unsettled] Security Scope Review — incomplete"
                        },
                    }
                ],
            }
        )
        rc, out, log = self._run_fork_decide(tmp_path, checkruns=flake)
        assert rc == 0, log
        assert out.get("conclusion") == "success", out

    def test_fork_a_prior_confirmed_block_still_floors_a_clean_rerun(self, tmp_path: Path) -> None:
        # A confirmed block carries NO unsettled marker, so it still holds the head
        # across a re-run the model does not re-propose -- the defect the floor was
        # added for stays closed.
        block = json.dumps(
            {
                "total_count": 1,
                "check_runs": [
                    {
                        "status": "completed",
                        "conclusion": "failure",
                        "external_id": "scope-pr-7-11-1",
                        "output": {"title": "Security Scope Review — a platform gap"},
                    }
                ],
            }
        )
        rc, out, log = self._run_fork_decide(tmp_path, checkruns=block)
        assert rc == 0, log
        assert out.get("conclusion") == "failure", out

    # ---- static contract pins ------------------------------------------------

    def test_the_fork_lane_marks_an_unsettled_run_on_its_own_check_run(self) -> None:
        # The fork lane can carry the settled/unsettled distinction because it
        # authors its own check-run: decide emits `settled`, and publish stamps the
        # marker as a title PREFIX (ahead of any model-derived why) only when the
        # run could not measure.
        decide = _step_by_name(
            "fork-security-scope-review.yml", "publish", "Decide the lane's conclusion"
        )["run"]
        assert 'echo "settled=$settled" >> "$GITHUB_OUTPUT"' in decide
        publish = _step_by_name("fork-security-scope-review.yml", "publish", "Publish check-run")[
            "run"
        ]
        assert 'scope_prefix="[scope-floor:unsettled] "' in publish
        assert '-f "output[title]=${scope_prefix}Security Scope Review' in publish

    def test_the_same_repo_lane_cannot_mark_an_unsettled_run(self) -> None:
        # The same-repo prior IS the publish job's own conclusion, created by the
        # API, so it carries no settable output: the asymmetry resolves to the
        # stricter same-repo semantics (a flake there clears only via the override).
        same_repo = _workflow("security-scope-review.yml")
        assert "scope-floor:unsettled" not in same_repo

    def test_both_lanes_delegate_the_floor_to_the_shared_table(self) -> None:
        same_repo = _step_by_name(
            "security-scope-review.yml", "publish", "Enforce the per-head monotonic floor"
        )["run"]
        assert "monotonic --current success" in same_repo
        assert '--lane same-repo --pr "$PR"' in same_repo
        fork = _step_by_name(
            "fork-security-scope-review.yml", "publish", "Decide the lane's conclusion"
        )["run"]
        assert 'monotonic --current "$conclusion"' in fork
        assert '--lane fork --pr "$PR"' in fork

    def test_neither_lane_pre_extracts_the_conclusions_in_shell(self) -> None:
        # The envelope has to reach the shared table intact: a `--jq` that pulls
        # conclusions out in shell makes a no-body response indistinguishable from
        # a well-formed empty listing, and that reads as "no prior".
        for workflow, step in (
            ("security-scope-review.yml", "Enforce the per-head monotonic floor"),
            ("fork-security-scope-review.yml", "Decide the lane's conclusion"),
        ):
            run = _step_by_name(workflow, "publish", step)["run"]
            assert "check-runs?check_name=$enc" in run, workflow
            assert ".check_runs[]" not in run, (
                f"{workflow}: the read still extracts conclusions in shell, so an "
                "empty response cannot be told from an empty listing"
            )

    def test_the_same_repo_floor_reads_the_trusted_harness_not_the_merge_ref(self) -> None:
        run = _step_by_name(
            "security-scope-review.yml", "publish", "Enforce the per-head monotonic floor"
        )["run"]
        assert 'SC="${HARNESS:-}/scope_candidates.py"' in run
        assert "python scripts/scope_candidates.py" not in run

    def test_the_same_repo_publish_job_reads_checks_not_writes(self) -> None:
        perms = yaml.safe_load(_workflow("security-scope-review.yml"))["jobs"]["publish"][
            "permissions"
        ]
        assert perms.get("checks") == "read", perms
        assert "checks" not in [k for k, v in perms.items() if v == "write"]

    def test_same_repo_another_pull_requests_failure_does_not_red_this_one(
        self, tmp_path: Path
    ) -> None:
        # Two pull requests can share a head SHA. A sibling's block must not red
        # the pull request under test.
        got = self._run_same_repo(
            tmp_path, checkruns=_checkruns_json("failure", external_id=None, pr=999)
        )
        assert got.returncode == 0, _proc_log(got)

    def test_same_repo_ignores_a_fork_lane_check_run_on_the_same_sha(self, tmp_path: Path) -> None:
        # Both lanes publish under one check name; the fork stamp names the lane.
        got = self._run_same_repo(
            tmp_path, checkruns=_checkruns_json("failure", external_id="scope-pr-7-11-1", pr=7)
        )
        assert got.returncode == 0, _proc_log(got)

    def test_same_repo_an_exit_zero_empty_read_fails_closed(self, tmp_path: Path) -> None:
        # The API answers, with no body. That is not "there are no priors".
        got = self._run_same_repo(tmp_path, checkruns="")
        assert got.returncode == 1, _proc_log(got)
        assert "held at the harder verdict" in got.stdout

    def test_same_repo_a_well_formed_empty_listing_publishes_clean(self, tmp_path: Path) -> None:
        got = self._run_same_repo(tmp_path, checkruns='{"total_count":0,"check_runs":[]}')
        assert got.returncode == 0, _proc_log(got)

    def test_fork_an_exit_zero_empty_read_fails_closed(self, tmp_path: Path) -> None:
        rc, out, log = self._run_fork_decide(tmp_path, checkruns="")
        assert rc == 0, log
        assert out.get("conclusion") == "failure", out

    def test_fork_a_well_formed_empty_listing_publishes_clean(self, tmp_path: Path) -> None:
        rc, out, log = self._run_fork_decide(
            tmp_path, checkruns='{"total_count":0,"check_runs":[]}'
        )
        assert rc == 0, log
        assert out.get("conclusion") == "success", out

    def test_same_repo_an_errored_floor_computation_reds_the_lane(self, tmp_path: Path) -> None:
        # The harness exits 2 with no stdout. A floor that cannot be computed is a
        # could-not-settle outcome, not a pass.
        got = self._run_same_repo(tmp_path, floor_rc=2)
        assert got.returncode == 1, _proc_log(got)
        assert "did not settle" in got.stdout, got.stdout

    def test_fork_an_errored_floor_computation_reds_the_lane(self, tmp_path: Path) -> None:
        # The same input on the fork lane must reach the same verdict: a script
        # error may not buy the softer conclusion on the more hostile source.
        rc, out, log = self._run_fork_decide(tmp_path, floor_rc=2)
        assert rc == 0, log
        assert out.get("conclusion") == "failure", out
        assert "could not be computed" in out.get("title", ""), out

    def test_both_lanes_agree_when_the_floor_errors(self, tmp_path: Path) -> None:
        # One assertion on the ASYMMETRY itself: an errored floor is red on both
        # lanes, so neither publishes a verdict the other refuses.
        sr, fk = tmp_path / "sr", tmp_path / "fk"
        sr.mkdir()
        fk.mkdir()
        same_repo_red = self._run_same_repo(sr, floor_rc=2).returncode != 0
        fork_conclusion = self._run_fork_decide(fk, floor_rc=2)[1].get("conclusion")
        assert same_repo_red is True
        assert fork_conclusion == "failure"

    def test_same_repo_a_truncated_prior_page_reds_the_lane(self, tmp_path: Path) -> None:
        # `total_count` outruns the rows in hand, so the page is partial and the
        # row naming a block can be the row that fell off it.
        truncated = json.dumps(
            {
                "total_count": 150,
                "check_runs": [
                    {
                        "status": "completed",
                        "conclusion": "success",
                        "pull_requests": [{"number": 7}],
                    }
                ],
            }
        )
        got = self._run_same_repo(tmp_path, checkruns=truncated)
        assert got.returncode == 1, _proc_log(got)

    def test_fork_a_truncated_prior_page_reds_the_lane(self, tmp_path: Path) -> None:
        truncated = json.dumps(
            {
                "total_count": 150,
                "check_runs": [
                    {
                        "status": "completed",
                        "conclusion": "success",
                        "external_id": "scope-pr-7-11-1",
                    }
                ],
            }
        )
        rc, out, log = self._run_fork_decide(tmp_path, checkruns=truncated)
        assert rc == 0, log
        assert out.get("conclusion") == "failure", out

    def test_the_fork_lane_still_posts_exactly_one_check_run(self) -> None:
        # The floor read is a GET; it must not add a second POST to the job that
        # holds checks:write and executes no fork code.
        assert (
            _workflow("fork-security-scope-review.yml").count(
                'gh api --method POST "repos/$REPO/check-runs"'
            )
            == 1
        )
