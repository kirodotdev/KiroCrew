"""Policy pin for .github/workflows/pr-readiness-sweep.yml's concurrency block.

Lives in its own module so it runs on EVERY matrix leg: the behavioural tests in
test_pr_readiness_sweep.py carry a module-level skipif for the POSIX toolchain
(bash, jq, GNU date) they execute the sweep script with, and a pure YAML
assertion parked there would be silently skipped -- not failed -- on Windows or
on any runner image missing jq, letting the ratchet protecting #8026 vanish for
reasons unrelated to the invariant it guards.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-readiness-sweep.yml"

pytestmark = pytest.mark.skipif(not WORKFLOW.exists(), reason="requires the workflow file")


def test_concurrency_never_cancels_the_incumbent_run() -> None:
    """The sweep is the backstop that unfreezes a stale required readiness
    status, so it must survive runner saturation. A queued run already holds
    the concurrency slot; with ``cancel-in-progress: true`` the next scheduled
    tick cancels it, and once queue latency exceeds the cron interval every run
    is killed by its own successor before executing a step -- the workflow can
    never run again exactly when it is most needed (#8026). The fixed group
    alone collapses overlap (GitHub keeps at most one pending run per group);
    the incumbent must never be a cancellation target."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert doc["concurrency"]["group"] == "pr-readiness-sweep"
    assert doc["concurrency"]["cancel-in-progress"] is False
