"""Every workflow that opens a PR with GITHUB_TOKEN must survive the refusal.

This repository leaves "Allow GitHub Actions to create and approve pull
requests" OFF — a defensible setting, because the same single switch also
grants APPROVING and ``main``'s merge gate is a required human review. With it
off, ``gh pr create`` under ``GITHUB_TOKEN`` is refused outright:

    pull request create failed: GraphQL: GitHub Actions is not permitted to
    create or approve pull requests (createPullRequest)

Every one of these workflows pushes its branch BEFORE the create, so at the
moment of the refusal the work is already safe and only the last, cosmetic step
is unavailable. Letting the step fail turns a policy decision into a scheduled
run that is red every time and that nobody can act on — which is exactly how a
three-week-old handoff issue went untouched while a human opened the PR by hand.

So: catch that one message, print a compare link, exit 0. Any OTHER create
failure is a real error and must still fail loud, which is what the second
assertion below pins.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

REFUSAL = "not permitted to create or approve pull requests"


def _run_steps():
    """(workflow name, step name, script) for every step with a ``run:``."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (doc.get("jobs") or {}).values():
            for index, step in enumerate(job.get("steps") or []):
                script = step.get("run")
                if script:
                    yield path.name, step.get("name", f"step {index}"), script


def _pr_creating_steps():
    return [
        (workflow, step, script)
        for workflow, step, script in _run_steps()
        if re.search(r"\bgh pr create\b", script)
    ]


def test_the_scan_actually_finds_the_pr_creating_steps():
    """A scan that matched nothing would let every assertion below pass empty."""
    found = _pr_creating_steps()
    assert len(found) >= 4, f"expected the known `gh pr create` steps, found {len(found)}"


@pytest.mark.parametrize(
    "workflow,step,script",
    [pytest.param(w, s, c, id=f"{w}::{s}") for w, s, c in _pr_creating_steps()],
)
def test_pr_create_refusal_hands_off_instead_of_failing(workflow, step, script):
    assert REFUSAL in script, (
        f"{workflow} / {step!r} calls `gh pr create` with no handling for the "
        "repository's Actions-may-not-open-PRs refusal, so the whole scheduled "
        "run goes red on a policy decision even though the branch was already "
        "pushed. Capture the output, match "
        f"{REFUSAL!r}, print a compare link and exit 0."
    )
    # The exit status has to be captured rather than allowed to kill the step,
    # which `set -e` would otherwise do before the message can be inspected.
    assert re.search(r"\|\|\s*rc=\$\?", script), (
        f"{workflow} / {step!r} must capture `gh pr create`'s exit status "
        "(`|| rc=$?`); under `set -e` the step dies before the refusal message "
        "can be read."
    )
    assert "exit 0" in script, (
        f"{workflow} / {step!r} must exit 0 on the refusal so the scheduled run "
        "stays green and the handoff notice is the actionable signal."
    )


@pytest.mark.parametrize(
    "workflow,step,script",
    [pytest.param(w, s, c, id=f"{w}::{s}") for w, s, c in _pr_creating_steps()],
)
def test_any_other_create_failure_still_fails_loud(workflow, step, script):
    """The handoff must be scoped to the refusal, never a blanket `|| true`.

    A create that fails for any other reason — a bad base, a rejected body, an
    auth outage — is a real error, and swallowing it would hide the very class
    of breakage this workflow exists to report.
    """
    assert re.search(r'exit\s+"?\$rc"?', script), (
        f"{workflow} / {step!r} must re-raise a non-refusal `gh pr create` "
        'failure (`exit "$rc"`) rather than treating every failure as a handoff.'
    )
    assert not re.search(r"gh pr create[^\n]*\|\|\s*true", script), (
        f"{workflow} / {step!r} swallows every `gh pr create` failure with "
        "`|| true`; only the documented refusal may be tolerated."
    )
