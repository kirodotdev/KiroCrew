"""Review-workflow tests for the shared PR-description capture: one read per job, no
live description grant, and the digest each verdict stamps.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    ATTACHMENT_SCRIPT,
    CAPTURE_SCRIPT,
    CAPTURE_SOURCE_LINE,
    ROOT,
    SCRUB_CALL,
    SNAPSHOT_SCRIPT,
    SNAPSHOT_SOURCE_FRAGMENT,
    WORKFLOWS,
    _attachment_script,
    _bash,
    _child_env,
    _proc_log,
    _snapshot_script,
    _stub_path,
    _workflow,
)

# --------------------------------------------------------------------------
# Description provenance: one capture, one digest, one stamp.
#
# A review lane that judges the author's stated intent has two ways to get the
# description, and they are not equivalent. A grant that lets the MODEL fetch it
# leaves the verdict with no revision to name: the model picks the moment, so the
# verdict answers for whatever the text said then, and a reader cannot tell a
# current verdict from one whose description-derived finding the author has since
# corrected. A workflow step that captures it ONCE gives the verdict a single,
# nameable input, and the digest of those bytes is what the verdict carries.
#
# The grant is also a live redirect. `--allowedTools` Bash grants are
# PREFIX-matched, so `Bash(gh pr view:*)` equally admits
# `gh pr view ... > <path>`: text injected into a diff can overwrite the lane's
# own input files. These pins hold both properties at once, and they enumerate
# every workflow from disk rather than a list, so a lane added later is covered
# the day it lands.
# --------------------------------------------------------------------------

#: The marker a published verdict carries to name the description it read.
STAMP_MARKER = "[DESCRIPTION-READ]"
STAMP_HEADING = 'echo "### Description read"'
STAMP_GATE = 'if [ -n "$badge" ] && [ -n "${DESCRIPTION_DIGEST:-}" ]; then'
STAMP_ECHO = 'echo "[DESCRIPTION-READ] $DESCRIPTION_DIGEST"'
#: Lanes whose model is given no description at all, so a stamp would be a claim
#: about an input they never received. Named rather than derived because the
#: point of the assertion is that the asymmetry is deliberate.
NO_DESCRIPTION_LANES = ("claude-review.yml", "fork-opus-review.yml")


def _every_workflow() -> tuple[str, ...]:
    """Every workflow file on disk, so a lane added later is covered."""
    names = tuple(sorted(p.name for p in WORKFLOWS.glob("*.yml")))
    assert len(names) > 10, names
    return names


def _sources_capture(name: str) -> bool:
    return CAPTURE_SOURCE_LINE in _workflow(name)


def _emits_stamp(name: str) -> bool:
    return STAMP_ECHO in _workflow(name)


def _capture_lanes() -> tuple[str, ...]:
    return tuple(n for n in _every_workflow() if _sources_capture(n))


def _stamp_block(name: str) -> list[str]:
    """The stamp's own shell lines, from its gate to the closing `fi`."""
    lines = _workflow(name).splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == STAMP_GATE]
    assert len(starts) == 1, (name, starts)
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    for end in range(start + 1, len(lines)):
        if lines[end].strip() == "fi" and len(lines[end]) - len(lines[end].lstrip()) == indent:
            return lines[start : end + 1]
    raise AssertionError(f"{name}: the stamp gate is never closed")


class TestBothBodyReadersShareOneRead:
    """The two readers of the PR's mutable text read it ONCE per job.

    Two independent fetches, in adjacent steps of one job, are the hazard:
    pr-attachment-evidence.sh collects the description's attachments and
    pr-description-capture.sh captures its prose. A description edited between
    two such reads pairs the OLD attachments with the NEW prose, and the
    manifest digest the lane publishes is taken over that pair -- a composite
    revision that never existed, which a reader recomputing it reads as a match.
    Nothing corrects it afterwards either, because the lanes fire on
    `opened, synchronize, reopened` and a description edit starts no run.

    The window was ordinary rather than adversarial: pushing a commit starts the
    run, and pasting a screenshot or rewording the body in the next minute lands
    inside it.

    pr-body-snapshot.sh holds one fetch of both fields for the whole job. These
    cases pin that structurally -- neither consumer calls the API itself -- and
    then EXECUTE both consumers over a body that changes between them, which is
    the only way to show the two halves name one revision.
    """

    def _job(self, tmp_path: Path) -> dict[str, str]:
        """A fake `gh` that counts calls and serves the body live off disk.

        Reading the body from a file on every call is what lets a case edit it
        BETWEEN the two consumers, which is the defect's exact shape.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'echo call >> "$GH_CALLS"\n'
            'printf \'{"title":%s,"body":%s}\\n\' '
            '"$(jq -Rs . < "$GH_TITLE")" "$(jq -Rs . < "$GH_BODY")"\n',
            encoding="utf-8",
            newline="\n",
        )
        gh.chmod(0o755)
        (tmp_path / "title.in").write_text("the title", encoding="utf-8", newline="")
        (tmp_path / "calls").write_text("", encoding="utf-8")
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        return _child_env(
            {
                # bin_dir first so the stub wins; _stub_path resolves the host's
                # real jq, which the Windows shards do not have on the defaults.
                "PATH": os.pathsep.join([str(bin_dir), _stub_path(tmp_path)]),
                "GH_CALLS": str(tmp_path / "calls"),
                "GH_TITLE": str(tmp_path / "title.in"),
                "GH_BODY": str(tmp_path / "body.in"),
                "REPO": "o/r",
                "PR": "1",
                "GH_TOKEN": "t",
                # The snapshot is keyed to the JOB, and this is what makes two
                # separate consumer processes one job.
                "RUNNER_TEMP": str(runner_temp),
                "TMPDIR": str(tmp_path),
                "TEMP": str(tmp_path),
                "TMP": str(tmp_path),
            }
        )

    def _source(
        self, script: Path, tmp_path: Path, env: dict[str, str], extra: dict[str, str]
    ) -> "subprocess.CompletedProcess[str]":
        bash = _bash()
        if bash is None:
            pytest.skip("the readers are Bash; skip where Bash is absent")
        runner = tmp_path / f"run-{script.stem}.sh"
        runner.write_text(f'. "{script}"\n', encoding="utf-8", newline="\n")
        return subprocess.run(
            [bash, str(runner)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env={**env, **extra},
            cwd=tmp_path,
        )

    def _collect_evidence(
        self, tmp_path: Path, env: dict[str, str]
    ) -> "subprocess.CompletedProcess[str]":
        """Run the attachment reader. Its downloads are not under test here, so
        curl answers a definite 4xx; the READ is what these cases measure."""
        curl = Path(env["PATH"].split(os.pathsep)[0]) / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\nprintf 404\nexit 22\n", encoding="utf-8", newline="\n"
        )
        curl.chmod(0o755)
        for name in ("shots", "map", "clips"):
            (tmp_path / name).write_text("", encoding="utf-8")
        return self._source(
            ROOT / ATTACHMENT_SCRIPT,
            tmp_path,
            env,
            {
                "FETCH_DIR": str(tmp_path / "fetch"),
                "DEST_DIR": str(tmp_path / "dest"),
                "NAME_STEM": "shot",
                "SHOTS": str(tmp_path / "shots"),
                "SHOT_MAP": str(tmp_path / "map"),
                "CLIPS": str(tmp_path / "clips"),
                "MAX_SHOTS": "40",
                "MAX_CLIPS": "4",
            },
        )

    def _capture_prose(
        self, tmp_path: Path, env: dict[str, str], intent: Path
    ) -> "subprocess.CompletedProcess[str]":
        return self._source(CAPTURE_SCRIPT, tmp_path, env, {"INTENT": str(intent)})

    def _calls(self, tmp_path: Path) -> int:
        return len((tmp_path / "calls").read_text(encoding="utf-8").split())

    def test_neither_reader_calls_the_api_itself(self) -> None:
        """Structural half: the snapshot is the ONLY place either reader's API
        call can live, so a lane cannot acquire a second read by accident."""
        for script in (_attachment_script(), CAPTURE_SCRIPT.read_text(encoding="utf-8")):
            code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
            assert not [ln for ln in code if "gh api" in ln], [ln for ln in code if "gh api" in ln]
            assert any(SNAPSHOT_SOURCE_FRAGMENT in ln for ln in code), script[:400]
        snapshot = _snapshot_script()
        reads = [
            ln for ln in snapshot.splitlines() if not ln.lstrip().startswith("#") and "gh api" in ln
        ]
        assert len(reads) == 1, reads
        assert 'gh api "repos/$REPO/pulls/$PR"' in reads[0], reads[0]
        # Both fields come out of that ONE response. Two `--jq` reads would be
        # two revisions again, however adjacent the calls.
        assert "jq -r '.title'" in snapshot
        assert "jq -r '.body // \"\"'" in snapshot

    def test_two_consumers_in_one_job_spend_one_api_read(self, tmp_path: Path) -> None:
        env = self._job(tmp_path)
        (tmp_path / "body.in").write_text("prose\n", encoding="utf-8", newline="\n")
        evidence = self._collect_evidence(tmp_path, env)
        assert evidence.returncode == 0, _proc_log(evidence)
        assert self._calls(tmp_path) == 1, self._calls(tmp_path)
        capture = self._capture_prose(tmp_path, env, tmp_path / "pr-intent.txt")
        assert capture.returncode == 0, _proc_log(capture)
        assert (
            self._calls(tmp_path) == 1
        ), f"the second consumer read the API again: {self._calls(tmp_path)} calls"
        assert "no second API read" in capture.stdout, capture.stdout

    def test_a_host_without_jq_fails_closed_instead_of_reading_twice(self, tmp_path: Path) -> None:
        """Splitting one response needs a standalone `jq`, so a host without one
        has to say so rather than quietly fall back.

        The fallback that is NOT allowed is the defect: two `gh api --jq` reads
        are two revisions however adjacent they are. A reader with no title and
        no body is a visible failure the lane reports; a reader with a matching
        pair drawn from two revisions is not. The review lanes run on
        ubuntu-latest, where `jq` is present; the case that meets this branch in
        practice is a Windows test shard, whose Git Bash keeps `jq` outside the
        Unix default directories -- which is why every harness here resolves the
        host's real `jq` onto the child PATH instead of assuming `/usr/bin`.
        """
        env = self._job(tmp_path)
        (tmp_path / "body.in").write_text("prose\n", encoding="utf-8", newline="\n")
        # Keep the `gh` stub reachable and drop everything else: the point is
        # that a read is NOT attempted, so the stub must be able to record one.
        env["PATH"] = env["PATH"].split(os.pathsep)[0]
        out = self._source(ROOT / SNAPSHOT_SCRIPT, tmp_path, env, {})
        assert "::error::jq is not available" in out.stdout, _proc_log(out)
        assert self._calls(tmp_path) == 0, "it read the API before checking it could split it"
        assert "no second API read" not in out.stdout, out.stdout

    def test_an_edit_between_the_two_consumers_cannot_split_them(self, tmp_path: Path) -> None:
        """The defect itself. The author edits the description after the
        evidence step and before the capture step; the capture must still see
        the revision the evidence was collected from, so the digest names one
        revision rather than a composite of two."""
        env = self._job(tmp_path)
        body = tmp_path / "body.in"
        body.write_text("v1 prose\n", encoding="utf-8", newline="\n")
        assert self._collect_evidence(tmp_path, env).returncode == 0
        body.write_text("v2 prose, edited mid-job\n", encoding="utf-8", newline="\n")
        intent = tmp_path / "pr-intent.txt"
        capture = self._capture_prose(tmp_path, env, intent)
        assert capture.returncode == 0, _proc_log(capture)
        captured = intent.read_text(encoding="utf-8")
        assert "v1 prose" in captured, captured
        assert "v2 prose" not in captured, captured

    def test_a_later_job_reads_the_edit(self, tmp_path: Path) -> None:
        """The snapshot is scoped to a job, not cached across them: a re-run
        after an edit judges the new text, which is what the lanes have always
        promised. Holding one revision across jobs would freeze the PR."""
        first = self._job(tmp_path / "job-a")
        (tmp_path / "job-a" / "body.in").write_text("v1 prose\n", encoding="utf-8", newline="\n")
        intent_a = tmp_path / "job-a" / "pr-intent.txt"
        assert self._capture_prose(tmp_path / "job-a", first, intent_a).returncode == 0
        second = self._job(tmp_path / "job-b")
        (tmp_path / "job-b" / "body.in").write_text("v2 prose\n", encoding="utf-8", newline="\n")
        intent_b = tmp_path / "job-b" / "pr-intent.txt"
        assert self._capture_prose(tmp_path / "job-b", second, intent_b).returncode == 0
        assert "v1 prose" in intent_a.read_text(encoding="utf-8")
        assert "v2 prose" in intent_b.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "body",
        ["plain body", "", "one trailing\n", "two trailing\n\n", "a\ttab and a 'quote'"],
        ids=["plain", "empty", "one-nl", "two-nl", "punctuation"],
    )
    def test_the_captured_bytes_match_the_old_single_read(self, body: str, tmp_path: Path) -> None:
        """A digest published BEFORE the snapshot must still recompute to the
        same value, or every stamp already on an open PR silently becomes a
        mismatch -- which reads as "the description moved", the one thing the
        stamp exists to report. Command substitution dropped the old `--jq`
        template's trailing newline and drops `jq -r`'s here, so the composed
        bytes are unchanged; these cases check that where the two forms could
        differ, which is a body's trailing newlines.
        """
        env = self._job(tmp_path)
        (tmp_path / "body.in").write_text(body, encoding="utf-8", newline="")
        intent = tmp_path / "pr-intent.txt"
        assert self._capture_prose(tmp_path, env, intent).returncode == 0
        old_form = f"Title: the title\n\nDescription:\n{body}".rstrip("\n")
        # The capture appends one newline of its own when it writes the file.
        assert intent.read_text(encoding="utf-8").rstrip("\n") == old_form, (
            intent.read_text(encoding="utf-8"),
            old_form,
        )

    def test_a_read_that_never_succeeds_fails_each_consumer_closed(self, tmp_path: Path) -> None:
        """Each consumer keeps its OWN fail-closed message, because what a
        failed read costs is different: one lane collects no evidence, the
        other judges a PR that appears to state no intent."""
        for job, runner, needle in (
            ("job-1", "evidence", "attachment evidence cannot be collected"),
            ("job-2", "capture", "appears to state no intent"),
        ):
            here = tmp_path / job
            env = self._job(here)
            (here / "body.in").write_text("prose\n", encoding="utf-8", newline="\n")
            dead = Path(env["PATH"].split(os.pathsep)[0]) / "gh"
            dead.write_text(
                '#!/usr/bin/env bash\necho call >> "$GH_CALLS"\nexit 1\n',
                encoding="utf-8",
                newline="\n",
            )
            dead.chmod(0o755)
            if runner == "evidence":
                result = self._collect_evidence(here, env)
            else:
                result = self._capture_prose(here, env, here / "intent.txt")
            assert result.returncode != 0, _proc_log(result)
            assert needle in result.stdout, result.stdout
            # Three attempts, then closed -- the bound the snapshot owns now.
            assert self._calls(here) == 3, self._calls(here)

    def test_a_snapshot_of_another_pull_request_is_not_reused(self, tmp_path: Path) -> None:
        """A job serves one PR, so this cannot differ in practice. It is pinned
        because a snapshot that silently answered for another PR would be this
        same corruption one level up."""
        env = self._job(tmp_path)
        (tmp_path / "body.in").write_text("pr-1 prose\n", encoding="utf-8", newline="\n")
        assert self._capture_prose(tmp_path, env, tmp_path / "a.txt").returncode == 0
        first = self._calls(tmp_path)
        (tmp_path / "body.in").write_text("pr-2 prose\n", encoding="utf-8", newline="\n")
        assert self._capture_prose(tmp_path, {**env, "PR": "2"}, tmp_path / "b.txt").returncode == 0
        assert self._calls(tmp_path) == first + 1, (first, self._calls(tmp_path))
        assert "pr-2 prose" in (tmp_path / "b.txt").read_text(encoding="utf-8")


class TestNoLaneGrantsALiveDescriptionRead:
    """No `--allowedTools` line anywhere grants a live description fetch.

    `Bash(gh pr view:*)` is prefix-matched, so it admits every `gh pr view`
    spelling including one that redirects its output over a file the job wrote.
    A lane that needs the description reads it from the shared capture instead.

    Scans every workflow rather than a named set: the grant is wrong in any lane,
    including one that does not exist yet.
    """

    def test_no_workflow_grants_gh_pr_view(self) -> None:
        offenders = []
        granting = 0
        for name in _every_workflow():
            for line in _workflow(name).splitlines():
                stripped = line.strip()
                if not stripped.startswith("--allowedTools"):
                    continue
                granting += 1
                if "Bash(gh pr view" in stripped:
                    offenders.append((name, stripped))
        # A control: an empty scan would satisfy the assertion above for the
        # wrong reason, so require that grant lines were actually examined.
        assert granting >= 8, granting
        assert offenders == [], offenders

    def test_no_workflow_grants_any_bash_at_all(self) -> None:
        """The invariant is about the MATCHER, not the verb.

        Stating it as "no `Bash(gh pr view:*)`" was too narrow, because prefix
        matching is a property of `--allowedTools` and not of the command named:
        `Bash(git diff:*)` equally admits `git diff ... > pr-intent.txt`, and the
        description digest is computed at capture time, so an overwrite AFTER
        capture leaves the stamp naming the original bytes while a reader who
        recomputes it is told the verdict is current.

        No narrower grant closes that. Without `:*` a lane cannot pass a range,
        and any form that accepts arguments accepts shell text; `--disallowedTools`
        denies TOOLS rather than command substrings, so it cannot express "no
        redirect" at all. The primitive therefore goes away with the last Bash
        grant, not with a better one -- which is why every review lane reads its
        diff from a data file instead.
        """
        offenders = []
        granting = 0
        for name in _every_workflow():
            for line in _workflow(name).splitlines():
                stripped = line.strip()
                if not stripped.startswith("--allowedTools"):
                    continue
                granting += 1
                if "Bash(" in stripped:
                    offenders.append((name, stripped))
        assert granting >= 8, granting
        assert offenders == [], offenders

    def test_every_lane_that_reviews_a_diff_is_handed_one(self) -> None:
        """Dropping the grant must not leave a lane with no diff at all.

        Removing Bash and forgetting the prefetch would give the reviewer nothing
        to review while every grant assertion still passed, so the two halves are
        pinned together: a lane whose prompt names a patch data file must also
        contain the step that writes one, and that step must fail closed.

        The SIZE cap is asserted only where a lane states one.
        `first-principles-review.yml` prefetches and fails closed on an empty diff
        but sets no cap, which predates this change; asserting a cap everywhere
        would impose a uniformity this change did not create and cannot verify.
        Where a cap exists it must be the 1 MB one and must fail closed, so a
        lane cannot acquire a cap that silently truncates instead.
        """
        checked = capped = 0
        for name in _capture_lanes():
            workflow = _workflow(name)
            if "authentic.patch" not in workflow:
                continue
            checked += 1
            assert re.search(
                r'git diff --no-color "\$(BASE|MERGE_BASE|merge_base)\b', workflow
            ), name
            assert "failing closed" in workflow, name
            if "exceeds 1 MB" in workflow:
                capped += 1
                assert "1000000" in workflow, name
        assert checked >= 6, checked
        assert capped >= 5, capped

    @pytest.mark.parametrize("name", ("design-review.yml", "ux-review.yml"))
    def test_a_capped_lane_measures_this_change_not_the_base_branch(self, name: str) -> None:
        """A size cap must be measured against the merge ref's own first parent.

        `github.event.pull_request.base.sha` is fixed when the pull request
        opens and does not track the base branch, while the checkout is a merge
        ref minted at push time. Diffing one against the other counts every
        commit the base gained in between, so on a busy base the cap fires on
        other people's work and the reviewer reads the base's history as the
        change under review. The first parent of the merge ref IS the base tip
        it was minted against, which makes the diff the change itself.

        Pinned on the two lanes that carry both a cap and a merge-ref checkout.
        The fork lanes take the same shape from their own trigger and are not
        this case's subject.
        """
        workflow = _workflow(name)
        # The shallow checkout's merge-base step picks the base: the merge
        # ref's first parent, else the merge-base of base.sha and the checkout.
        assert "git rev-parse -q --verify HEAD^2" in workflow, name
        assert 'patch_base="$(git rev-parse HEAD^1)"' in workflow, name
        assert 'merge_base "$BASE_SHA" "$' in workflow, name
        assert "BASE: ${{ steps.merge_base.outputs.patch_base }}" in workflow, name
        assert 'git diff --no-color "$BASE..HEAD"' in workflow, name
        assert 'git diff --no-color "$BASE_SHA' not in workflow, name


class TestStampFollowsTheSharedCapture:
    """A lane stamps a description digest exactly when it captures one.

    The biconditional is the assertion. A lane that captures but does not stamp
    keeps the provenance private to the job log; a lane that stamps without
    capturing would be naming bytes it never read.
    """

    def test_capture_and_stamp_are_the_same_set(self) -> None:
        capture = {n for n in _every_workflow() if _sources_capture(n)}
        stamp = {n for n in _every_workflow() if _emits_stamp(n)}
        assert capture == stamp, {"captures only": capture - stamp, "stamps only": stamp - capture}
        assert len(capture) >= 6, sorted(capture)

    @pytest.mark.parametrize("name", NO_DESCRIPTION_LANES)
    def test_a_lane_given_no_description_makes_no_claim_about_one(self, name: str) -> None:
        workflow = _workflow(name)
        assert CAPTURE_SOURCE_LINE not in workflow, name
        assert STAMP_MARKER not in workflow, name

    def test_every_capture_lane_reads_the_shared_script_and_no_copy(self) -> None:
        """No lane carries its own copy of the read, the strip or the cap.

        Two implementations would make one digest mean two different things, so a
        reader recomputing it could get a mismatch from a description nobody had
        touched. The distinctive lines of the capture therefore appear in the
        script and in no workflow -- and the READ's distinctive line lives one
        level further down still, in the snapshot both body readers share, which
        is what keeps the evidence half and the prose half on one revision.
        """
        owners = {
            CAPTURE_SCRIPT.read_text(encoding="utf-8"): (
                "[description TRUNCATED at 8000 bytes]",
                'INTENT_DIGEST="$($_kc_sha',
            ),
            _snapshot_script(): (
                # Named by the variables the values land in, because the bare
                # forms are not distinctive: `ai-review-human-override.yml`
                # reads the same endpoint for `head.sha`, and
                # `deferred-findings-audit.yml` runs the same jq filter over an
                # issue. Neither is a description read and neither is a copy.
                '_kc_snap_json="$(gh api "repos/$REPO/pulls/$PR")"',
                'jq -r \'.body // ""\' > "$KC_PR_BODY_FILE"',
            ),
        }
        for script, needles in owners.items():
            for needle in needles:
                assert needle in script, needle
        for name in _every_workflow():
            workflow = _workflow(name)
            for needles in owners.values():
                for needle in needles:
                    assert needle not in workflow, (name, needle)


class TestStampIsWorkflowWrittenAndGatedOnARealVerdict:
    """The digest reaches the comment from the capture step, never from the model.

    Three properties, one per way the stamp could lie. It is gated on a parsed
    verdict, so a "could not complete" notice cannot claim to have read a
    description. It is written after the redaction pass, so that pass cannot
    rewrite the digest into something a reader cannot reproduce. And its value
    comes from the capture step's own output, so the model cannot supply it.
    """

    @pytest.mark.parametrize("name", _capture_lanes())
    def test_gated_on_a_parsed_verdict_and_a_present_digest(self, name: str) -> None:
        block = _stamp_block(name)
        assert block[0].strip() == STAMP_GATE, (name, block[0])
        assert any(STAMP_ECHO in ln for ln in block), name
        assert any(STAMP_HEADING in ln for ln in block), name

    @pytest.mark.parametrize("name", _capture_lanes())
    def test_written_after_the_redaction_pass(self, name: str) -> None:
        lines = _workflow(name).splitlines()
        redactions = [i for i, ln in enumerate(lines) if SCRUB_CALL in ln]
        stamps = [i for i, ln in enumerate(lines) if STAMP_ECHO in ln]
        assert len(redactions) == 1, (name, redactions)
        assert len(stamps) == 1, (name, stamps)
        assert redactions[0] < stamps[0], (name, redactions, stamps)

    @pytest.mark.parametrize("name", _capture_lanes())
    def test_the_digest_comes_from_the_capture_step_output(self, name: str) -> None:
        workflow = _workflow(name)
        doc = yaml.safe_load(workflow)
        binding = "DESCRIPTION_DIGEST: ${{ steps.intent.outputs.description_digest }}"
        assert binding in workflow, name
        # The step id the binding names must exist, and must be the step that
        # sources the capture -- otherwise the digest describes another step.
        sourcing_ids = set()
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = step.get("run")
                if isinstance(run, str) and CAPTURE_SOURCE_LINE in run:
                    sourcing_ids.add(step.get("id"))
        assert sourcing_ids == {"intent"}, (name, sourcing_ids)


class TestStampBlockIsByteIdenticalAcrossLanes:
    """Lanes that cover the same thing publish the same stamp, down to the bytes.

    A reader learns one shape and a verifier parses one shape. Only the comment
    file each lane appends to differs, so compare the emitted lines and not the
    redirect.

    There are exactly TWO shapes, and the split is the point rather than drift: a
    lane whose verdict reads only the prose names the prose, and a lane whose
    verdict also reads the evidence downloaded from the description names that
    evidence too. Collapsing them would force one of the two to lie -- either the
    prose-only lanes claim coverage they do not have, or the evidence lanes send
    a reader to recompute over inputs that are not what was judged. What this
    forbids is a THIRD shape: within each class the bytes must match, so no
    single lane can drift into bespoke wording, and both classes must be
    non-empty so a shape cannot quietly lose all its members.
    """

    def test_emitted_lines_match_within_each_coverage_class(self) -> None:
        by_class: dict[bool, dict[str, list[str]]] = {True: {}, False: {}}
        for name in _capture_lanes():
            block = _stamp_block(name)
            emitted = [
                ln.strip() for ln in block if ln.strip().startswith("echo") or ln.strip() == "echo"
            ]
            covers_evidence = "EVIDENCE_LIST" in _workflow(name)
            by_class[covers_evidence][name] = emitted
        # Both classes must exist, or the assertion below could pass on an empty
        # one and the split would be unverified.
        assert by_class[True], "no lane covers evidence"
        assert by_class[False], "no prose-only lane remains"
        for covers_evidence, lanes in by_class.items():
            shapes = {tuple(v) for v in lanes.values()}
            assert len(shapes) == 1, (covers_evidence, lanes)
        prose_shape = next(iter({tuple(v) for v in by_class[False].values()}))
        assert prose_shape[1] == STAMP_HEADING, prose_shape
        assert prose_shape[3] == STAMP_ECHO, prose_shape
        # The heading and marker line are what a verifier parses, so they must be
        # common to BOTH shapes; only the explanatory sentence differs.
        evidence_shape = next(iter({tuple(v) for v in by_class[True].values()}))
        assert evidence_shape[1] == STAMP_HEADING, evidence_shape
        assert evidence_shape[3] == STAMP_ECHO, evidence_shape


class TestCaptureDigestNamesTheModelsInput:
    """Execute the shared capture and check what its digest covers.

    The digest is only worth publishing if it names the bytes the model was
    given. So it must equal the hash of the intent file as written -- after the
    media strip and the cap -- and it must move when that file moves and hold
    still when it does not. A digest taken over the raw API body instead would
    report an image-URL swap as a description the verdict never saw, which is the
    same false confidence in the other direction.
    """

    def _capture(
        self,
        tmp_path: Path,
        body: str,
        evidence: list[bytes] | None = None,
        evidence_names: list[str] | None = None,
        list_missing: bool = False,
        pathlists: list[tuple[str, bytes]] | None = None,
        pathlist_missing: bool = False,
    ) -> tuple[str, str, bytes]:
        bash = _bash()
        if bash is None:
            pytest.skip("the capture is Bash; skip where Bash is absent")
        tmp_path.mkdir(parents=True, exist_ok=True)
        # The capture reaches the API through pr-body-snapshot.sh, which fetches
        # the whole PR object ONCE and splits the title and the description out
        # of it, so the stub answers with that object. The composed bytes are
        # identical to what the old single `--jq` template returned, which is
        # what keeps a digest published before the snapshot reproducible.
        reply = tmp_path / "api-reply.json"
        reply.write_text(
            json.dumps({"title": "t", "body": body}) + "\n", encoding="utf-8", newline="\n"
        )
        gh = tmp_path / "gh"
        gh.write_text(f'#!/bin/sh\ncat "{reply}"\n', encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        intent = tmp_path / "pr-intent.txt"
        outputs = tmp_path / "gh-output"
        outputs.write_text("", encoding="utf-8")
        extra: dict[str, str] = {}
        wants_list = (
            evidence is not None or list_missing or pathlists is not None or pathlist_missing
        )
        if wants_list:
            # Write the evidence files the lane would have collected, then the
            # list naming them -- the same two-file shape the lanes build, so the
            # pin exercises the real contract rather than a paraphrase of it.
            names = evidence_names or [f"evidence-{i}.bin" for i in range(len(evidence or []))]
            paths = []
            for name, blob in zip(names, evidence or [], strict=True):
                target = tmp_path / name
                target.write_bytes(blob)
                paths.append(target)
            listing = tmp_path / "intent-evidence-list.txt"
            # A lane lists the files whose own bytes are per-run paths FIRST and
            # marks them `pathlist:`, then the evidence whose bytes ARE the
            # evidence. Same order as the four real lanes, so an ordinal here
            # means what it means there.
            lines = []
            for name, blob in pathlists or []:
                target = tmp_path / name
                target.write_bytes(blob)
                lines.append(f"pathlist:{target}")
            if pathlist_missing:
                lines.append(f"pathlist:{tmp_path / 'never-written.txt'}")
            lines += [str(p) for p in paths]
            if list_missing:
                lines.append(str(tmp_path / "never-written.bin"))
            listing.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8", newline="\n")
            extra["EVIDENCE_LIST"] = str(listing)
        runner = tmp_path / "run.sh"
        runner.write_text(
            f'. "{CAPTURE_SCRIPT}"\nprintf %s "$INTENT_DIGEST"\n',
            encoding="utf-8",
            newline="\n",
        )
        env = _child_env(
            {
                "PATH": _stub_path(tmp_path),
                "REPO": "o/r",
                "PR": "1",
                "GH_TOKEN": "t",
                "INTENT": str(intent),
                "GITHUB_OUTPUT": str(outputs),
                # The capture takes an `mktemp` scratch for the pre-cap body, so
                # without these it lands in the system temp dir rather than this
                # test's own. `_child_env` uses `setdefault`, so naming TEMP/TMP
                # here keeps the Windows passthrough from reinstating the host's.
                "TMPDIR": str(tmp_path),
                "TEMP": str(tmp_path),
                "TMP": str(tmp_path),
                **extra,
            }
        )
        result = subprocess.run(
            [bash, str(runner)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
            cwd=tmp_path,
        )
        if list_missing or pathlist_missing:
            # The fail-closed path is the subject of its own pin, so hand the
            # caller the failure instead of asserting success here.
            return (
                str(result.returncode),
                outputs.read_text(encoding="utf-8"),
                result.stderr.encode(),
            )
        assert result.returncode == 0, _proc_log(result)
        reported = result.stdout.strip().splitlines()[-1].strip()
        return reported, outputs.read_text(encoding="utf-8"), intent.read_bytes()

    def test_the_digest_is_the_hash_of_the_file_the_model_reads(self, tmp_path: Path) -> None:
        reported, outputs, written = self._capture(tmp_path, "a plain description")
        assert reported == hashlib.sha256(written).hexdigest(), (reported, written)
        assert f"description_digest={reported}" in outputs, outputs

    def test_a_changed_description_changes_the_digest(self, tmp_path: Path) -> None:
        first, _, _ = self._capture(tmp_path / "a", "the original claim")
        second, _, _ = self._capture(tmp_path / "b", "the corrected claim")
        assert first != second, first

    def test_a_stripped_image_does_not_move_the_digest(self, tmp_path: Path) -> None:
        """What the strip erases cannot change the model's input, so it cannot
        change the digest -- IN A LANE THAT READS PROSE ONLY, which is what
        passing no evidence list models here. Two bodies differing only in an
        image URL the strip replaces therefore hash the same, and a reader
        recomputing after such an edit is told the verdict is current, which in
        that lane it is. A lane whose verdict also reads what those URLs resolved
        to hands over an evidence list and is covered by the pins below, because
        there the same edit DOES change what was judged.
        """
        one, _, wrote_one = self._capture(
            tmp_path / "a", "prose\n\n![shot](https://example.com/one.png)\n"
        )
        two, _, wrote_two = self._capture(
            tmp_path / "b", "prose\n\n![shot](https://example.com/two.png)\n"
        )
        assert b"[image removed]" in wrote_one, wrote_one
        assert wrote_one == wrote_two, (wrote_one, wrote_two)
        assert one == two, (one, two)

    def test_the_digest_is_bare_hex_when_the_path_needs_escaping(self, tmp_path: Path) -> None:
        """`sha256sum <file>` escapes a name holding a backslash and prefixes the
        whole line with one, so reading the hash off that line yields `\\<hex>`:
        a stamp no reader can reproduce. A Windows path is all backslashes, so
        the shard reproduced it and this board could not. A backslash is a legal
        POSIX filename character, so digesting one here makes the class visible
        wherever the suite runs, not only where the OS forces it.
        """
        reported, outputs, written = self._capture(tmp_path / "a\\b", "a plain description")
        assert reported == hashlib.sha256(written).hexdigest(), (reported, written)
        assert set(reported) <= set("0123456789abcdef"), reported
        assert len(reported) == 64, reported
        assert f"description_digest={reported}" in outputs, outputs

    def test_an_unusable_digest_fails_the_step_closed(self, tmp_path: Path) -> None:
        """The guard is a shape assertion, not an emptiness one. `\\<64 hex>` is
        non-empty, so a guard testing only for empty publishes it; the step must
        refuse anything that is not exactly 64 hex characters, because a stamp a
        reader cannot reproduce is worse than no stamp at all.
        """
        script = CAPTURE_SCRIPT.read_text(encoding="utf-8")
        guard = script[script.index("INTENT_DIGEST=") :]
        assert "-ne 64" in guard, guard[:400]
        assert "*[!0-9a-f]*" in guard, guard[:400]
        assert guard.count("exit 1") >= 2, guard[:600]

    def test_the_digest_reads_stdin_and_falls_back_to_shasum(self) -> None:
        """Two portability properties of EVERY digest command in the script.

        Reading stdin keeps a filename out of the output, so no path can escape
        into the hash. The `shasum` fallback lets a reader on a Mac reproduce the
        stamp by hand, which the verdict explicitly invites -- macOS ships no
        `sha256sum`, and this repository already picks between the two the same
        way in `cli.sh`, `playwright-cli.sh` and `ensure-node.sh`.

        The property is per-SITE, not a count. The script takes more than one
        digest -- the captured description, and the manifest that folds in a
        lane's evidence -- and a count assertion fails on a new site that holds
        the property just as loudly as on one that breaks it. So every site is
        checked, with a lower bound as the control so an over-narrow filter
        matching nothing cannot pass vacuously.
        """
        script = CAPTURE_SCRIPT.read_text(encoding="utf-8")
        code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
        digest = [ln for ln in code if "INTENT_DIGEST=" in ln and "cut" in ln]
        assert len(digest) >= 1, digest
        for line in digest:
            assert " < " in line, line
        assert "shasum -a 256" in "\n".join(code), digest
        assert any("command -v sha256sum" in ln for ln in code), digest
        # The bare `sha256sum "$INTENT"` spelling is the one whose escaped output
        # produced a stamp no reader could reproduce; it must appear nowhere. The
        # same goes for any other digest taken over a PATH rather than stdin.
        assert not any('sha256sum "$INTENT"' in ln for ln in code), digest
        hashed_paths = [ln for ln in code if "_kc_sha" in ln and "cut" in ln and " < " not in ln]
        assert hashed_paths == [], hashed_paths

    def test_evidence_the_verdict_read_moves_the_digest(self, tmp_path: Path) -> None:
        """The defect this closes: the media strip replaces every attachment URL
        with the same placeholder, so in a lane whose verdict also reads what
        those URLs resolved to, swapping one attachment for another leaves the
        captured prose byte-identical. A digest over the prose alone reports a
        match on a verdict formed from other evidence. Same body, different
        evidence bytes, so only the evidence can account for the difference.
        """
        one, _, wrote_one = self._capture(tmp_path / "a", "same prose", evidence=[b"pixels-A"])
        two, _, wrote_two = self._capture(tmp_path / "b", "same prose", evidence=[b"pixels-B"])
        assert wrote_one == wrote_two, (wrote_one, wrote_two)
        assert one != two, one

    def test_no_evidence_list_keeps_the_intent_only_digest(self, tmp_path: Path) -> None:
        """The other direction, which is why the coverage is per-lane and not
        global: a lane that judges prose only must keep naming the prose alone,
        or a media-only edit it provably never saw would be reported as a
        description that changed -- the same false confidence inverted.
        """
        reported, _, written = self._capture(tmp_path, "prose only")
        assert reported == hashlib.sha256(written).hexdigest(), reported

    def test_the_manifest_names_ordinals_not_paths(self, tmp_path: Path) -> None:
        """A runner temp path is per-run, so digesting it would move the stamp on
        a re-run that read byte-identical evidence and tell a reader the verdict
        was stale. Identical bytes under different names must hash the same.
        """
        one, _, _ = self._capture(
            tmp_path / "a", "same prose", evidence=[b"same"], evidence_names=["first.bin"]
        )
        two, _, _ = self._capture(
            tmp_path / "b", "same prose", evidence=[b"same"], evidence_names=["second.bin"]
        )
        assert one == two, (one, two)

    def test_two_evidence_files_are_not_one_longer_one(self, tmp_path: Path) -> None:
        """The manifest is per-file lines, not concatenated bytes, so splitting
        the same total differently cannot collide."""
        one, _, _ = self._capture(tmp_path / "a", "same prose", evidence=[b"ab", b"c"])
        two, _, _ = self._capture(tmp_path / "b", "same prose", evidence=[b"a", b"bc"])
        assert one != two, one

    def test_an_unreadable_listed_evidence_file_fails_closed(self, tmp_path: Path) -> None:
        """A stamp must never overstate its coverage. If the lane named evidence
        the capture cannot read, silently hashing the rest would publish a digest
        claiming to cover what it never saw, so the capture fails instead.
        """
        code, outputs, stderr = self._capture(
            tmp_path, "prose", evidence=[b"present"], list_missing=True
        )
        assert code != "0", (code, stderr)
        assert "description_digest=" not in outputs, outputs

    def test_every_lane_reading_attachment_evidence_names_it_in_the_digest(self) -> None:
        """Enumerated from source, because a named site is a sample. A lane whose
        model is pointed at the evidence downloaded from the description must
        hand that evidence to the capture, or its stamp names less than its
        verdict read. The predicate is sourcing the attachment-evidence script:
        that is what downloads the bytes, and every lane that does it feeds them
        to its model either as images or as the rendered-evidence manifest.

        The check is STRUCTURAL -- the capture step's own `env` must carry the
        key -- not a substring search of the file. A substring is satisfied by a
        mention in a comment, and by a renamed key: `EVIDENCE_LIST_DISABLED`
        contains `EVIDENCE_LIST`, so a lane that had stopped handing its evidence
        over would still read as covered.
        """
        offenders = []
        covered = 0
        for name in _every_workflow():
            text = _workflow(name)
            if "pr-attachment-evidence.sh" not in text:
                continue
            if "pr-description-capture.sh" not in text:
                continue
            covered += 1
            doc = yaml.safe_load(text)
            named = False
            for job in doc["jobs"].values():
                for step in job.get("steps") or []:
                    run = step.get("run") or ""
                    if "pr-description-capture.sh" not in run:
                        continue
                    if "EVIDENCE_LIST" in (step.get("env") or {}):
                        named = True
            if not named:
                offenders.append(name)
        # A control: zero matches would pass the assertion vacuously, and the
        # count is the census this pin exists to hold -- four lanes download
        # attachment evidence AND capture the description.
        assert covered == 4, covered
        assert offenders == [], offenders

    def test_a_list_file_holding_a_non_path_line_does_not_fail_the_capture(
        self, tmp_path: Path
    ) -> None:
        """A UX lane hands over `ux-screenshots.txt`, whose lines are NOT all
        paths: a `TRUNCATED: more than N images` prose notice is appended to it
        when the cap drops evidence. Passing that prose as an evidence path trips
        the capture's fail-closed readability guard, and the lane goes red on any
        PR carrying more than the cap -- a guard firing correctly on input that
        was never a path. So a lane lists the list FILE as content and only those
        of its lines that are really files; this pin holds the capture's half of
        that contract by proving a prose-bearing list file is digestible.
        """
        reported, outputs, _ = self._capture(
            tmp_path,
            "prose",
            pathlists=[
                (
                    "shots.txt",
                    b"/nonexistent/shot-01.png\n"
                    b"TRUNCATED: more than 40 images; one was not listed\n",
                )
            ],
        )
        assert len(reported) == 64, reported
        assert f"description_digest={reported}" in outputs, outputs

    def test_a_dropped_evidence_notice_moves_the_digest(self, tmp_path: Path) -> None:
        """Digesting the list file as CONTENT is what makes the truncation notice
        load-bearing: an edit that changes WHICH evidence a cap drops changes that
        list's bytes even when no kept file changed, so the verdict's stamp moves.
        Filtering the notice out and digesting only real paths would lose this.

        Held in the `pathlist:` form, which is what the four lanes list. The
        notice does not start with `/`, so normalizing to basenames leaves it
        whole and this property survives that change -- which is the point of
        pinning it here rather than on the plain form.
        """
        kept = b"/x/_temp/shots/shot-01.png\n"
        one, _, _ = self._capture(
            tmp_path / "a",
            "same prose",
            pathlists=[
                ("shots.txt", kept + b"TRUNCATED: more than 40 images; one was not listed\n")
            ],
        )
        two, _, _ = self._capture(
            tmp_path / "b",
            "same prose",
            pathlists=[
                ("shots.txt", kept + b"TRUNCATED: more than 40 images; two were not listed\n")
            ],
        )
        assert one != two, one

    def test_a_marked_lists_temp_root_stays_out_of_the_digest(self, tmp_path: Path) -> None:
        """The screenshot list holds `"$DEST_DIR/$name"` lines and $DEST_DIR is
        under `runner.temp`, so digesting its BYTES puts a per-run directory
        inside the stamp: a re-run that read byte-identical evidence publishes a
        different digest, and a reader recomputing is told a sound verdict is
        stale. That is the same defect the manifest's ordinal labels exist to
        prevent, one level down -- in a listed file's contents rather than in its
        label. The `pathlist:` form reduces every line starting with `/` to its
        basename, so the same evidence under two temp roots hashes the same.

        The two renderings are asserted DIFFERENT first. Without that the pin
        would also pass on a harness that fed identical bytes twice, which is how
        an invariance test passes while measuring nothing.
        """
        one_bytes = b"/home/runner/work/_temp/ux-shots/shot-01.png\n"
        two_bytes = b"/mnt/other/_work/_temp/ux-shots/shot-01.png\n"
        assert one_bytes != two_bytes
        one, _, _ = self._capture(
            tmp_path / "a", "same prose", pathlists=[("shots.txt", one_bytes)]
        )
        two, _, _ = self._capture(
            tmp_path / "b", "same prose", pathlists=[("shots.txt", two_bytes)]
        )
        assert one == two, (one, two)

    def test_a_marked_map_still_moves_when_an_attachment_is_swapped(self, tmp_path: Path) -> None:
        """Normalizing must drop the directory and nothing else. The origin map is
        `<opaque name>\\t<origin>` and does not start with `/`, so it survives
        whole -- which is what keeps a swapped attachment nameable. The strip
        replaces both URLs with the same placeholder, so the captured prose is
        byte-identical across this edit and the map is the only thing that moves.
        Had normalizing reduced the map too, the swap would go unstamped.
        """
        one, _, _ = self._capture(
            tmp_path / "a",
            "same prose",
            pathlists=[("map.txt", b"shot-01.png\thttps://example.com/one.png\n")],
        )
        two, _, _ = self._capture(
            tmp_path / "b",
            "same prose",
            pathlists=[("map.txt", b"shot-01.png\thttps://example.com/two.png\n")],
        )
        assert one != two, one

    def test_an_unreadable_marked_list_file_still_fails_closed(self, tmp_path: Path) -> None:
        """The marker changes HOW a listed file is folded in, never WHETHER it has
        to be readable. A stamp that quietly skipped a marked file it could not
        open would overstate its coverage exactly as the plain form would.
        """
        code, outputs, stderr = self._capture(tmp_path, "prose", pathlist_missing=True)
        assert code != "0", (code, stderr)
        assert "description_digest=" not in outputs, outputs

    def test_every_lane_marks_the_path_lists_it_hands_over(self) -> None:
        """Enumerated from source, because a named site is a sample. Two shapes
        carry absolute paths in their own bytes: the screenshot list a UX lane
        hands over directly, and the rendered-evidence manifest a design lane
        hands over, which embeds that list with `cat`. Each must be listed
        `pathlist:` or this run's temp directory enters the stamp.

        The converse is pinned in the same pass: a line appending ONE image must
        NOT be marked. Those bytes are the evidence, and normalizing a PNG would
        hash a reading of its lines instead of its content.
        """
        source_line = '. "$GITHUB_WORKSPACE/.github/scripts/pr-description-capture.sh"'
        marker = "printf 'pathlist:%s\\n'"
        offenders = []
        covered = 0
        for name in _every_workflow():
            text = _workflow(name)
            if "EVIDENCE_LIST" not in text or source_line not in text:
                continue
            doc = yaml.safe_load(text)
            for job_id, job in doc["jobs"].items():
                for step in job.get("steps") or []:
                    run = step.get("run") or ""
                    if source_line not in run or "EVIDENCE_LIST" not in run:
                        continue
                    covered += 1
                    appends = 0
                    for line in run.splitlines():
                        stripped = line.strip()
                        if not stripped.endswith('>> "$EVIDENCE_LIST" || :'):
                            continue
                        appends += 1
                        marked = marker in stripped
                        # `"$f"` is the UX loop over its three list files;
                        # `"$DESIGN_EVIDENCE"` is the design lanes' manifest.
                        # Anything else appending to the list is one file whose
                        # bytes are the evidence.
                        a_path_list = '"$f"' in stripped or '"$DESIGN_EVIDENCE"' in stripped
                        if a_path_list and not marked:
                            offenders.append((name, job_id, "path list unmarked", stripped))
                        if marked and not a_path_list:
                            offenders.append((name, job_id, "byte evidence marked", stripped))
                    if appends == 0:
                        offenders.append((name, job_id, "no append line found", ""))
        # Control: exactly four lanes hand evidence to the capture -- two UX and
        # two design. The two first-principles lanes read prose plus the diff and
        # set no EVIDENCE_LIST, so a count other than four means this walk stopped
        # matching the lanes rather than that they are clean.
        assert covered == 4, covered
        assert offenders == [], offenders

    def test_every_ux_lane_covers_its_recording_list(self) -> None:
        """The recording list is named in each UX lane's prompt as a data file the
        model reads, so a recording-only description edit must move the digest. A
        lane that hands over its images and map but not its recordings publishes a
        stamp claiming coverage it lost. Enumerated from source: the predicate is
        the lane naming a recordings file at all.
        """
        offenders = []
        covered = 0
        for name in _every_workflow():
            text = _workflow(name)
            if "ux-recordings.txt" not in text:
                continue
            if "pr-description-capture.sh" not in text:
                continue
            covered += 1
            doc = yaml.safe_load(text)
            named = False
            for job in doc["jobs"].values():
                for step in job.get("steps") or []:
                    run = step.get("run") or ""
                    if "pr-description-capture.sh" not in run:
                        continue
                    env = step.get("env") or {}
                    if "CLIPS" in env and "EVIDENCE_LIST" in env and "$CLIPS" in run:
                        named = True
            if not named:
                offenders.append(name)
        # Control: both UX lanes name a recordings file and capture a description.
        assert covered == 2, covered
        assert offenders == [], offenders

    def test_no_lane_pipes_a_whole_list_file_into_the_evidence_list(self) -> None:
        """The defect this forbids, stated structurally: `cat "$SHOTS" >>
        "$EVIDENCE_LIST"` copies every line of a list file in as a path, and that
        list carries prose. A lane must filter to real files instead, which is
        what the `[ -f ` test below is.
        """
        offenders = []
        for name in _every_workflow():
            text = _workflow(name)
            if "EVIDENCE_LIST" not in text:
                continue
            if 'cat "$SHOTS" >> "$EVIDENCE_LIST"' in text:
                offenders.append((name, "cats a list file in as paths"))
            if "$SHOTS" in text and "[ -f " not in text:
                offenders.append((name, "reads $SHOTS without a real-file test"))
        assert offenders == [], offenders

    def test_a_lane_that_names_evidence_says_so_where_the_verdict_is_read(self) -> None:
        """A reader recomputes over whatever the stamp's sentence names. A lane
        folding evidence in while still printing the description-only sentence
        would send that reader to recompute over the description alone, get a
        mismatch, and read a sound verdict as stale -- a false alarm the fix
        itself manufactured. So the sentence must branch on the count.
        """
        offenders = []
        for name in _every_workflow():
            text = _workflow(name)
            if "EVIDENCE_LIST" not in text:
                continue
            if "EVIDENCE_COUNT" not in text or "names everything this verdict read" not in text:
                offenders.append(name)
        assert offenders == [], offenders


class TestASourcedScriptNeverRunsWithCredentialsLive:
    """Every sourced script in a review lane runs before that lane's credentials.

    The invariant, as one sentence: in any workflow that both sources a script
    from `.github/scripts/` and assumes a role, every source must appear before
    every assume. It is stated over the WHOLE file rather than per lane, because
    the reason is not local -- a session assumed once persists for every later
    step in the job, so "before the model call" is not the same bar as "before
    any credentials exist".

    On a same-repo pull request the checkout is the PR's merge ref, so a sourced
    script is the PR's own editable copy; running it after an assume executes
    PR-authored shell with Bedrock credentials in its environment. The fork lanes
    check out base_sha and so are not exposed today, but they are held to the same
    order deliberately: an exception resting on which ref a lane checks out breaks
    silently the day that ref changes, and this pin is what makes the property
    independent of it.

    `design-review.yml` documents this ordering in prose. Prose is not a gate: it
    describes an order without holding it, and an order that must hold in six
    lanes at once needs something that fails when one of them drifts. That is
    what this test is.
    """

    #: A run: step's shell is the workflow's own code, so an `aws` call inside one
    #: is not what this pin is about. What it looks for is the credential ACTION.
    _ASSUME = "aws-actions/configure-aws-credentials"
    _SOURCE = '. "$GITHUB_WORKSPACE/.github/scripts/'

    def _positions(self, text: str) -> "tuple[list[int], list[int]]":
        sources, assumes = [], []
        for i, line in enumerate(text.splitlines()):
            if line.lstrip().startswith("#"):
                continue
            if self._SOURCE in line:
                sources.append(i + 1)
            if self._ASSUME in line:
                assumes.append(i + 1)
        return sources, assumes

    def test_the_invariant_holds_in_every_workflow_on_disk(self) -> None:
        offenders = []
        covered = 0
        for name in _every_workflow():
            sources, assumes = self._positions(_workflow(name))
            if not sources or not assumes:
                continue
            covered += 1
            if max(sources) > min(assumes):
                offenders.append((name, sources, assumes))
        # A control: the assertion below is only meaningful if the scan actually
        # found files holding both halves. Zero would pass it vacuously.
        assert covered >= 4, covered
        assert offenders == [], offenders

    @pytest.mark.parametrize("name", _capture_lanes())
    def test_each_capture_lane_sources_before_it_assumes(self, name: str) -> None:
        sources, assumes = self._positions(_workflow(name))
        assert sources, name
        if not assumes:
            pytest.skip(f"{name} assumes no role")
        assert max(sources) < min(assumes), (name, sources, assumes)


class TestCaptureLanesKeepTheirTriggerSet:
    """Capturing a description does not earn a lane an `edited` trigger.

    A fresh reading of the description and a re-roll of the whole verdict are
    different things. `edited` buys the first by paying for the second: with
    `cancel-in-progress`, a body edit on an unchanged head discards the verdict
    the lane already published and replaces it with another roll of a
    non-deterministic reviewer. The stamp gives a reader the freshness signal
    without that trade, so the trigger sets stay as they are.
    """

    @pytest.mark.parametrize("name", _capture_lanes())
    def test_no_capture_lane_reruns_on_a_description_edit(self, name: str) -> None:
        doc = yaml.safe_load(_workflow(name))
        trigger = (doc.get(True) or doc.get("on") or {}).get("pull_request")
        if trigger is None:
            # The fork lanes are dispatched by a stage-1 gate, not by
            # `pull_request` directly; they have no trigger set to hold.
            assert "workflow_run" in (doc.get(True) or doc.get("on") or {}), name
            return
        types = trigger.get("types") or []
        assert "edited" not in types, (name, types)
        assert "synchronize" in types, (name, types)
