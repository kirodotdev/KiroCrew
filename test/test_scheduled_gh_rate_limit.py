"""Every scheduled job that calls ``gh`` either waits out a spent rate limit or says why not.

A scheduled job's token draws on the repository's shared installation rate
limit, which the rest of the CI fleet can spend. A single ``gh`` call made
then fails with "API rate limit exceeded" and fails the run, although the limit
resets within the hour. The shared action ``.github/actions/gh-rate-limit``
puts a ``gh`` wrapper on PATH that waits for the reset, bounded, and retries
only a rate-limit error (its behaviour is pinned in
test_ship_report_rate_limit.py).

Not every job is worth a wait: a sweep that ticks every half hour simply runs
again. So each scheduled job that calls ``gh`` is in exactly one of two sets:

* it installs the action before its first ``gh`` call, from a checkout that
  holds it, with a job timeout longer than the wait budget; or
* it is listed in ``NOT_WRAPPED`` below with the reason a failed run costs
  nothing the next run does not re-cover.

A new scheduled workflow, or a ``gh`` call added to a scheduled job that had
none, fails here until it picks one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION_REF = "./.github/actions/gh-rate-limit"
ACTION = ROOT / ACTION_REF.removeprefix("./") / "action.yml"

# (workflow file, job id) -> why a failed run there loses nothing worth a wait.
NOT_WRAPPED: dict[tuple[str, str], str] = {
    ("add-contributor.yml", "add"): (
        "daily, and every run re-derives the complete contributor set, so the"
        " next run re-covers a failed one; its sweeps already retry"
    ),
    ("fix-loop-analysis.yml", "analyze"): (
        "its model step holds Write over the runner's disk, so neither a wrapper"
        " installed before it nor an action read from the checkout after it can"
        " be trusted by the steps that hold the token"
    ),
    ("fork-pr-label.yml", "label"): "hourly sweep; the next tick labels what this one missed",
    ("memory-benchmark.yml", "accept"): (
        "daily; the next run measures again and opens the same baseline PR"
    ),
    ("merge-queue-ruleset.yml", "check"): (
        "a read-only audit every 6 hours; the next tick reads the same rules"
    ),
    ("pr-merge-conflict-label.yml", "label"): (
        "sweep every 30 minutes; the next tick labels what this one missed"
    ),
    ("pr-readiness-sweep.yml", "sweep"): ("frequent sweep; the next tick re-reads every open PR"),
}

# A `gh <subcommand>` invocation: `gh` at a word start in shell position,
# followed by a lower-case subcommand. Whole-line comments are dropped first,
# so prose that names a command is not a call.
_GH_CALL = re.compile(r"(?:^|[\s;|&(`!{\"'])gh\s+[a-z][a-z-]*\b")


def _calls_gh(script: str) -> bool:
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    return bool(_GH_CALL.search(code))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    # PyYAML reads a bare ``on:`` key as the boolean True.
    triggers = document.get("on") or document.get(True) or {}
    return triggers if isinstance(triggers, dict) else {}


def _scheduled_jobs_calling_gh() -> dict[tuple[str, str], dict]:
    found: dict[tuple[str, str], dict] = {}
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        document = _load(path)
        if "schedule" not in _triggers(document):
            continue
        for job_id, job in (document.get("jobs") or {}).items():
            steps = job.get("steps") or []
            if any(_calls_gh(step.get("run") or "") for step in steps):
                found[(path.name, job_id)] = job
    return found


SCHEDULED = _scheduled_jobs_calling_gh()
WRAPPED = sorted(key for key in SCHEDULED if key not in NOT_WRAPPED)


def _max_wait(step: dict) -> int:
    default = _load(ACTION)["inputs"]["max-wait"]["default"]
    return int((step.get("with") or {}).get("max-wait", default))


@pytest.mark.parametrize(
    "script",
    [
        'gh api "repos/$REPO/issues"',
        "x=$(gh pr list --json number)",
        "  if gh issue view 1; then :; fi",
        "echo hi | gh api --input - repos/x",
    ],
)
def test_the_detector_sees_a_gh_call(script: str) -> None:
    assert _calls_gh(script)


@pytest.mark.parametrize(
    "script",
    [
        "# `gh pr create` would fail here",
        "      # gh api graphql is paginated",
        "python scripts/pick_gh_ledger.py",
        "echo high api",
    ],
)
def test_the_detector_ignores_what_is_not_a_gh_call(script: str) -> None:
    assert not _calls_gh(script)


def test_the_sweep_finds_scheduled_gh_jobs() -> None:
    """Guards the guard: a parser change that found nothing would pass every
    check below vacuously."""
    assert ("ship-report.yml", "report") in SCHEDULED
    assert len(WRAPPED) >= 4


@pytest.mark.parametrize("key", sorted(NOT_WRAPPED))
def test_every_exemption_names_a_live_scheduled_gh_job(key: tuple[str, str]) -> None:
    """A stale entry would quietly exempt whatever job later takes its name."""
    assert key in SCHEDULED, f"{key} no longer calls gh on a schedule; drop it from NOT_WRAPPED"
    job = SCHEDULED[key]
    assert not any(
        step.get("uses") == ACTION_REF for step in job.get("steps") or []
    ), f"{key} installs the wrapper; drop it from NOT_WRAPPED"


@pytest.mark.parametrize("key", WRAPPED, ids=lambda k: f"{k[0]}:{k[1]}")
def test_a_scheduled_gh_job_waits_out_a_spent_rate_limit(key: tuple[str, str]) -> None:
    workflow, job_id = key
    steps = SCHEDULED[key].get("steps") or []
    installs = [i for i, s in enumerate(steps) if s.get("uses") == ACTION_REF]
    assert installs, (
        f"{workflow} job {job_id!r} calls gh on a schedule with no rate-limit handling."
        f" Add `uses: {ACTION_REF}` before its first gh call, or list it in NOT_WRAPPED"
        " with the reason a failed run loses nothing."
    )
    install = installs[0]
    first_gh = next(i for i, s in enumerate(steps) if _calls_gh(s.get("run") or ""))
    assert install < first_gh, f"{workflow}:{job_id} calls gh before installing the wrapper"

    checkouts = [
        s for s in steps[:install] if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts, f"{workflow}:{job_id} uses a local action with no checkout before it"
    sparse = (checkouts[-1].get("with") or {}).get("sparse-checkout")
    if sparse:
        paths = str(sparse).split()
        wanted = ACTION_REF.removeprefix("./")
        assert any(
            wanted == p or wanted.startswith(p.rstrip("/") + "/") for p in paths
        ), f"{workflow}:{job_id}'s sparse checkout does not fetch {wanted}"

    timeout = SCHEDULED[key].get("timeout-minutes")
    assert isinstance(timeout, int), f"{workflow}:{job_id} needs a job timeout-minutes"
    assert timeout * 60 > _max_wait(
        steps[install]
    ), f"{workflow}:{job_id}'s timeout-minutes leaves no room for the wrapper's wait budget"
