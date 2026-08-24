"""A cache-writing ``setup-*`` action may appear at most ONCE per job.

``astral-sh/setup-uv`` and the ``actions/setup-*`` family derive their cache key
from inputs that are fixed for the whole job -- action version, target triple,
runner image, interpreter version, lockfile hash. Nothing a later step does can
change that key, so a SECOND invocation in the same job reserves the identical
key and races the first step's own post-job cache save. GitHub then reports::

    Failed to save: Unable to reserve cache with key setup-uv-2-...,
    another job may be creating this cache.

The "another job" wording is what makes this hard to spot: it reads as benign
cross-job concurrency on a matrix, so a duplicated step survives review and
then warns on every nightly forever. ``build-desktop.yml`` carried exactly that
-- two ``setup-uv`` steps in one job, warning on the macOS and Linux-arm64 legs
of every Nightly Build run.

Static and offline: this reads only the workflow YAML, so it cannot flake.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# Prefixes whose actions maintain a tool cache keyed on job-invariant inputs.
# Deliberately a prefix match, not a fixed list: a future setup-<tool> lands
# under the ratchet without anyone remembering to enroll it.
#
# This OVER-APPROXIMATES on purpose. Real keys also fold in inputs, so two
# setup-python steps pinning different versions would not actually collide, yet
# this still flags them. The repo has no such job today, and the cost of the
# over-approximation is one conversation whereas the cost of under-matching is a
# warning on every nightly forever. If a genuine two-interpreter job ever lands,
# widen this to match on (action, key-feeding inputs) -- do not delete the test.
_CACHING_ACTION_PREFIXES = ("astral-sh/setup-", "actions/setup-")


def _duplicate_cache_setups() -> list[str]:
    """Return one ``workflow:job action xN`` line per duplicated setup action."""
    findings: list[str] = []
    for wf in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            actions = [
                step["uses"].split("@", 1)[0]
                for step in (job.get("steps") or [])
                if isinstance(step, dict) and isinstance(step.get("uses"), str)
            ]
            for action, count in sorted(Counter(actions).items()):
                if count > 1 and action.startswith(_CACHING_ACTION_PREFIXES):
                    findings.append(f"{wf.name}:{job_id} uses {action} x{count}")
    return findings


def test_workflows_exist() -> None:
    """Guard the guard: an empty glob would make the ratchet vacuously green."""
    assert list(WORKFLOWS.glob("*.yml")), f"no workflows found under {WORKFLOWS}"


def test_no_job_installs_the_same_cache_setup_action_twice() -> None:
    duplicates = _duplicate_cache_setups()
    assert not duplicates, (
        "a cache-writing setup-* action is declared more than once in one job; "
        "the second invocation reserves the same cache key and races the "
        "first's post-job save ('Unable to reserve cache with key ...'). Keep "
        "one invocation per job and fold any rationale into its comment:\n  "
        + "\n  ".join(duplicates)
    )
