"""Ratchet: no workflow job may use fetch-depth: 0 together with a tight
timeout-minutes budget.

A full-history checkout of a large repository is a multi-minute operation that
races any job timeout <= 10 min.  This test makes the pattern machine-checked
so a new violation cannot be added silently.

A job that genuinely needs full history within a tight budget must be added to
ALLOWED with a comment explaining why it cannot be migrated to a bounded fetch.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: Tight-timeout ceiling.  A full-history fetch of a repo this size takes
#: 2.5-4.5 min on the hosted CodeBuild pool; any job budget <= this value is a
#: coin flip when paired with fetch-depth: 0.
_THRESHOLD = 10

#: (workflow filename, job id) pairs that are allowed to keep fetch-depth: 0
#: within the tight budget.  Each entry must have a comment explaining why.
#: Remove an entry once the job is migrated to a bounded fetch.
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        # add-contributor reads `git log` across arbitrary merged-PR history to
        # collect commit authors; a bounded fetch would miss author commits.
        ("add-contributor.yml", "add"),
        # code-review.yml jobs are being migrated to bounded fetches in a
        # separate open PR; remove these entries once those jobs are migrated.
        ("code-review.yml", "autosde-rules"),
        ("code-review.yml", "inclusive-language"),
        ("code-review.yml", "pr-hygiene"),
        # internal-content-scan.yml is a shared workflow_call target that
        # handles pull_request, push AND merge_group events.  All three shapes
        # supply different SHA pairs; migrating requires event-shaped fetch
        # logic across a shared workflow.
        ("internal-content-scan.yml", "scan"),
        # release.yml stable-gate runs only on release events and reads the
        # changelog history to verify the release is documented.
        ("release.yml", "stable-gate"),
    }
)


def _violations() -> list[tuple[str, str]]:
    """Return (workflow filename, job id) pairs that violate the invariant."""
    found: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        for job_id, job_body in (doc.get("jobs") or {}).items():
            if not isinstance(job_body, dict):
                continue
            timeout = job_body.get("timeout-minutes")
            if timeout is None or timeout > _THRESHOLD:
                continue
            for step in job_body.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                uses = step.get("uses") or ""
                if "actions/checkout" not in uses:
                    continue
                with_block = step.get("with") or {}
                if with_block.get("fetch-depth") == 0:
                    found.append((path.name, job_id))
                    break  # one checkout per job is the norm; stop at first hit
    return found


def test_guard_finds_violations_in_current_allowed_set() -> None:
    """Self-check: the scanner must find at least the known allowed violations.

    If this fails, YAML structure changed or the scanner has a bug -- every
    assertion below would be vacuously true.
    """
    found = set(_violations())
    # ALLOWED may shrink as other PRs migrate their jobs; check only the entries
    # that are expected to still be present (the ones not yet migrated).
    still_expected = {
        ("add-contributor.yml", "add"),
        ("internal-content-scan.yml", "scan"),
        ("release.yml", "stable-gate"),
    }
    missing = still_expected - found
    assert not missing, (
        f"The scanner no longer finds these expected violations: {sorted(missing)}.  "
        "Either the workflows were migrated (great -- remove them from still_expected) "
        "or the scanner has stopped matching the checkout form this repo uses."
    )


def test_no_new_full_history_checkout_under_tight_timeout() -> None:
    """The invariant.

    Any job that pairs fetch-depth: 0 with timeout-minutes <= 10 must appear in
    ALLOWED with a documented reason.  A new violation means a full-history
    checkout was added without assessing its flakiness risk; fix it with a
    bounded fetch (fetch-depth: 2 + diff HEAD^1 HEAD) or add it to ALLOWED
    with an explanation.
    """
    new_violations = [v for v in _violations() if v not in ALLOWED]
    assert new_violations == [], (
        f"These workflow jobs use fetch-depth: 0 with timeout-minutes <= {_THRESHOLD} "
        f"and are not in the ALLOWED set: {new_violations}.\n"
        "Fix with a bounded fetch:\n"
        "  1. Change actions/checkout fetch-depth to 2.\n"
        "  2. Replace git diff BASE...HEAD / BASE..HEAD with `git diff HEAD^1 HEAD`.\n"
        "     HEAD^1 is the base tip; HEAD is the merge commit. No extra fetch step needed.\n"
        "Or add the job to ALLOWED with a comment explaining why full history is required."
    )
