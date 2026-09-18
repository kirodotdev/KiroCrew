"""Every fork job that computes a base...head diff must check out that base, deep.

The `fork-*.yml` lanes all run privileged from the default branch and all derive
the fork's diff as `base_sha...head_sha`. That three-dot diff needs two things
from the checkout, and BOTH are silent when missing:

* `ref: <base_sha>` -- with no `ref`, `actions/checkout` takes `github.sha`, which
  on a `workflow_run` event is the DEFAULT BRANCH TIP, not the PR's base. Once
  `main` advances past the PR's base (the ordinary case), `base_sha` is not in the
  object store at all.
* `fetch-depth: 0` -- the default depth-1 clone has no history, so even when
  `base_sha` IS the tip there is no reachable merge-base to diff against.

Either omission makes `git diff` fail on essentially every fork PR. The lane then
reports "no verdict", and because these lanes are read by `PR Readiness`, a
blocking one turns into a permanent block on all external contributions -- the
exact outcome the same-repo callers' fork skip exists to avoid.

This is pinned as a test because five sibling lanes already agreed on it and
nothing enforced it: `fork-internal-content-scan.yml` shipped for review without
either setting, and it took two full review rounds to find. A unanimous
convention that no test asserts is a convention the sixth copy silently breaks.

WHY THE INVARIANT IS BOUND TO THE DIFFING JOB, not to every checkout
--------------------------------------------------------------------
It was written when every fork lane was a SINGLE job, so "the lane's checkout"
and "the checkout the diff is computed against" were the same step and either
spelling of the rule enforced the same thing. `fork-security-scope-review.yml` is
the first MULTI-JOB fork lane, and it is multi-job precisely because credentials
and write scope must not be held by one job: `generate` holds the Bedrock
credential and computes the diff, `validate` holds nothing and classifies, and
`adjudicate` / `publish` hold write scope and run the trusted BASE-OWNED harness.
Those last two deliberately check out the default branch, and forcing them onto
the PR's base would put the pull request's own tree under the jobs that carry
write scope -- inverting the split the lane exists for.

So the requirement follows the diff instead of the file: a job whose `run:`
bodies compute a `base_sha...` three-dot range must have the base-SHA `ref` and
`fetch-depth: 0`; a job that computes no such range is exempt. That is stricter
than the old form in three ways, not looser:

* a diffing job with NO checkout at all is an offender, which a rule that
  inspects only checkout steps cannot express at all;
* the inherited `needs.<job>.outputs.base_sha` spelling is accepted only when
  that job really is in this job's `needs` AND declares that output, so the
  expression cannot resolve to the empty string that `actions/checkout` reads as
  "give me the default branch";
* the two accepted spellings are exact, so a variant cannot creep in.
"""

from __future__ import annotations

import pathlib
import re

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
#: The in-job spelling, available only where `steps.pr` exists.
EXPECTED_REF = "${{ steps.pr.outputs.base_sha }}"
#: The inherited spelling a downstream job must use: `steps.pr` does not exist
#: outside the job that resolved the pull request, so a multi-job lane carries the
#: SAME commit across the boundary as a job output.
INHERITED_REF = re.compile(r"^\$\{\{ needs\.([A-Za-z0-9_-]+)\.outputs\.base_sha \}\}$")
#: A `base_sha...` three-dot range in any spelling the lanes use: `$BASE_SHA...`,
#: `${BASE_SHA}...`, or the expression form `${{ ...outputs.base_sha }}...`.
DIFF_RANGE = re.compile(r"base_sha[\"'}\s]*\.\.\.", re.IGNORECASE)


def _jobs(path: pathlib.Path) -> dict[str, dict]:
    # YAML 1.1 parses a bare `on:` key as the boolean True, so never index "on".
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc.get("jobs") or {}


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _computes_a_base_diff(job: dict) -> bool:
    """Does any step of this job compute a `base_sha...head` range itself?

    Bound to the `run:` bodies rather than to a step name: a step renamed, or a
    second diffing step added under a different name, must not fall out of the
    invariant.
    """
    return any(DIFF_RANGE.search(str(step.get("run") or "")) for step in job.get("steps") or [])


def _checkouts(job: dict) -> list[dict]:
    return [
        step.get("with") or {}
        for step in job.get("steps") or []
        if "actions/checkout" in (step.get("uses") or "")
    ]


def _audit(paths: list[pathlib.Path]) -> tuple[list[str], int]:
    """Every offending checkout across `paths`, plus how many were examined."""
    offenders: list[str] = []
    checked = 0
    for path in paths:
        jobs = _jobs(path)
        for job_id, job in jobs.items():
            if not _computes_a_base_diff(job):
                continue
            where = path.name + ":" + job_id
            checkouts = _checkouts(job)
            if not checkouts:
                offenders.append(
                    f"{where} computes a base_sha...head diff with no actions/checkout"
                    " step, so it has no object store to diff in"
                )
                continue
            for with_ in checkouts:
                checked += 1
                ref = str(with_.get("ref") or "")
                depth = with_.get("fetch-depth")
                # The EXACT expression, not a substring: `test_ai_review_workflows.py`
                # pins the same literal for the review lanes, and two spellings of one
                # invariant drift apart. Pinning the canonical forms here keeps this
                # structural check the stricter of the two rather than a looser
                # restatement that would quietly permit a variant.
                inherited = INHERITED_REF.match(ref)
                if inherited:
                    producer = inherited.group(1)
                    # An inherited expression naming a job this one does not need, or
                    # a job that declares no such output, resolves to the EMPTY
                    # STRING -- which `actions/checkout` reads as "no ref" and
                    # silently answers with the default branch tip.
                    if producer not in _needs(job):
                        offenders.append(
                            f"{where} checks out needs.{producer}.outputs.base_sha,"
                            f" but {producer} is not in its `needs` -- the expression"
                            " resolves to the empty string"
                        )
                    elif "base_sha" not in ((jobs.get(producer) or {}).get("outputs") or {}):
                        offenders.append(
                            f"{where} checks out needs.{producer}.outputs.base_sha,"
                            f" but job {producer} declares no `base_sha` output --"
                            " the expression resolves to the empty string"
                        )
                elif ref != EXPECTED_REF:
                    offenders.append(
                        f"{where} checks out {ref or '<default: the default branch tip>'}"
                        f" instead of the PR base (needs ref: {EXPECTED_REF}, or"
                        " ${{ needs.<job>.outputs.base_sha }} in a downstream job)"
                    )
                if depth != 0:
                    offenders.append(
                        f"{where} has fetch-depth={depth!r}"
                        " -- a shallow clone has no merge-base to diff against (needs 0)"
                    )
    return offenders, checked


def test_every_fork_lane_checks_out_the_pr_base_with_full_history() -> None:
    fork_lanes = sorted(WORKFLOWS.glob("fork-*.yml"))
    assert fork_lanes, "no fork-*.yml lanes found -- the glob or the layout moved"

    offenders, checked = _audit(fork_lanes)

    assert checked, "no diffing fork job had a checkout step -- the assertion went vacuous"
    detail = "\n  ".join(offenders)
    assert not offenders, "a fork lane checkout cannot produce a base...head diff:\n  " + detail


def _lane(tmp_path: pathlib.Path, name: str, jobs: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(
        "name: probe\non:\n  workflow_run:\n    workflows: [Fast Gate]\n"
        "    types: [completed]\njobs:\n" + jobs,
        encoding="utf-8",
    )
    return path


#: A two-job lane shaped like the real multi-job one: an upstream job resolves the
#: pull request, a downstream job inherits the base SHA and computes the diff.
_INHERITING_LANE = """  resolve:
    runs-on: ubuntu-latest
    outputs:
      base_sha: ${{ steps.pr.outputs.base_sha }}
    steps:
      - id: pr
        run: echo base_sha=deadbeef >> "$GITHUB_OUTPUT"
  diffing:
    runs-on: ubuntu-latest
    needs: [resolve]
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ needs.resolve.outputs.base_sha }}
          fetch-depth: 0
      - name: Fetch authentic diff
        env:
          BASE_SHA: ${{ needs.resolve.outputs.base_sha }}
        run: git diff --no-color "$BASE_SHA...$HEAD_SHA" > patch
"""


class TestTheWidenedInvariantStaysEnforcing:
    """The rule follows the diff, and every loss around that diff is still caught.

    Driven from synthetic lanes rather than the repository's own: the accepted
    inherited spelling has no diffing consumer on disk today, so asserting it only
    over the real files would leave that branch unreached -- present but unproven,
    which is how a widened invariant quietly stops enforcing anything.
    """

    def test_the_inherited_spelling_is_accepted(self, tmp_path: pathlib.Path) -> None:
        offenders, checked = _audit([_lane(tmp_path, "fork-probe.yml", _INHERITING_LANE)])
        assert offenders == []
        assert checked == 1

    def test_a_diffing_job_that_loses_the_ref_is_an_offender(self, tmp_path: pathlib.Path) -> None:
        lane = _INHERITING_LANE.replace(
            "          ref: ${{ needs.resolve.outputs.base_sha }}\n", ""
        )
        offenders, _ = _audit([_lane(tmp_path, "fork-probe.yml", lane)])
        assert any("the default branch tip" in line for line in offenders), offenders

    def test_a_diffing_job_that_loses_full_history_is_an_offender(
        self, tmp_path: pathlib.Path
    ) -> None:
        lane = _INHERITING_LANE.replace("fetch-depth: 0", "fetch-depth: 1")
        offenders, _ = _audit([_lane(tmp_path, "fork-probe.yml", lane)])
        assert any("fetch-depth=1" in line for line in offenders), offenders

    def test_an_inherited_ref_from_a_job_not_needed_is_an_offender(
        self, tmp_path: pathlib.Path
    ) -> None:
        lane = _INHERITING_LANE.replace("    needs: [resolve]\n", "")
        offenders, _ = _audit([_lane(tmp_path, "fork-probe.yml", lane)])
        assert any("is not in its `needs`" in line for line in offenders), offenders

    def test_an_inherited_ref_the_producer_never_declares_is_an_offender(
        self, tmp_path: pathlib.Path
    ) -> None:
        lane = _INHERITING_LANE.replace(
            "    outputs:\n      base_sha: ${{ steps.pr.outputs.base_sha }}\n", ""
        )
        offenders, _ = _audit([_lane(tmp_path, "fork-probe.yml", lane)])
        assert any("declares no `base_sha` output" in line for line in offenders), offenders

    def test_a_diffing_job_with_no_checkout_is_an_offender(self, tmp_path: pathlib.Path) -> None:
        lane = """  diffing:
    runs-on: ubuntu-latest
    steps:
      - name: Fetch authentic diff
        env:
          BASE_SHA: ${{ steps.pr.outputs.base_sha }}
        run: git diff --no-color "$BASE_SHA...$HEAD_SHA" > patch
"""
        offenders, checked = _audit([_lane(tmp_path, "fork-probe.yml", lane)])
        assert checked == 0
        assert any("no actions/checkout" in line for line in offenders), offenders

    def test_a_job_that_diffs_nothing_is_exempt(self, tmp_path: pathlib.Path) -> None:
        """The trusted-harness jobs check out the DEFAULT branch on purpose."""
        lane = """  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: python3 scripts/publish.py
"""
        offenders, checked = _audit([_lane(tmp_path, "fork-probe.yml", lane)])
        assert offenders == []
        assert checked == 0
