"""Every Stage-2 fork lane must check out the PR's BASE with full history.

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
"""

from __future__ import annotations

import pathlib

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
EXPECTED_REF = "${{ steps.pr.outputs.base_sha }}"


def _checkout_steps(path: pathlib.Path) -> list[tuple[str, dict]]:
    """Return (job_id, `with:` mapping) for each actions/checkout step."""
    # YAML 1.1 parses a bare `on:` key as the boolean True, so never index "on".
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    found: list[tuple[str, dict]] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if "actions/checkout" in (step.get("uses") or ""):
                found.append((job_id, step.get("with") or {}))
    return found


def test_every_fork_lane_checks_out_the_pr_base_with_full_history() -> None:
    fork_lanes = sorted(WORKFLOWS.glob("fork-*.yml"))
    assert fork_lanes, "no fork-*.yml lanes found -- the glob or the layout moved"

    offenders: list[str] = []
    checked = 0
    for path in fork_lanes:
        for job_id, with_ in _checkout_steps(path):
            checked += 1
            where = path.name + ":" + job_id
            ref = str(with_.get("ref") or "")
            depth = with_.get("fetch-depth")
            # The EXACT expression, not a substring: `test_ai_review_workflows.py`
            # pins the same literal for the review lanes, and two spellings of one
            # invariant drift apart. Pinning the canonical form here keeps this
            # structural check the stricter of the two rather than a looser
            # restatement that would quietly permit a variant.
            if ref != EXPECTED_REF:
                offenders.append(
                    f"{where} checks out {ref or '<default: the default branch tip>'}"
                    f" instead of the PR base (needs ref: {EXPECTED_REF})"
                )
            if depth != 0:
                offenders.append(
                    f"{where} has fetch-depth={depth!r}"
                    " -- a shallow clone has no merge-base to diff against (needs 0)"
                )

    assert checked, "no checkout step found in any fork lane -- the assertion went vacuous"
    detail = "\n  ".join(offenders)
    assert not offenders, "a fork lane checkout cannot produce a base...head diff:\n  " + detail
