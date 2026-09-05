"""Slice 2 (circle 2) — cron move coordination core (issue #7577).

Task 2.6: `kirocrew cron move <job-id> --to <crew>` and the Schedule-tab
action both need the SAME core — turn a source job into a MigrationBundle
plus the target requirements — so it lives here as a pure function and the
CLI / dashboard are thin shells over it. This keeps the allow-list and the
requirement derivation in one place (DRY), independently testable without
argparse or the dashboard.

Side-effect discipline: builds a CronJob in memory, returns a bundle. No CLI,
no store, no network.
"""

from __future__ import annotations

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.migration import protocol as P
from kiro_crew.migration.cron_adapter import CronMigrationAdapter
from kiro_crew.migration.cron_move import plan_cron_move


def _job(**over):
    base = dict(
        id="j1",
        name="nightly",
        message="run backup",
        schedule=CronSchedule(kind="cron", cron_expr="0 3 * * *"),
        agent_id="kirocrew",
        timezone="America/New_York",
        script="~/.kiro/crew/crons/x.py:go",
    )
    base.update(over)
    return CronJob(**base)


@pytest.mark.asyncio
async def test_plan_cron_move_builds_a_cron_bundle_for_the_target():
    adapter = CronMigrationAdapter(job_lookup={"j1": _job()})
    bundle = await plan_cron_move(adapter, "j1", target=P.CrewRef(crew_id="dst", label="target"))
    assert isinstance(bundle, P.MigrationBundle)
    assert bundle.bundle_kind == "cron"
    assert bundle.source_crew  # populated
    assert bundle.payload["name"] == "nightly"
    assert bundle.handoff_id  # idempotency key allocated


@pytest.mark.asyncio
async def test_plan_cron_move_carries_blocking_requirements():
    adapter = CronMigrationAdapter(job_lookup={"j1": _job()})
    bundle = await plan_cron_move(adapter, "j1", target=P.CrewRef(crew_id="dst"))
    kinds = {r.kind for r in bundle.requirements}
    # agent + script both present -> both required on target
    assert "agent" in kinds
    assert "script_path" in kinds
    assert all(r.severity == "blocking" for r in bundle.requirements)


@pytest.mark.asyncio
async def test_plan_cron_move_uses_supplied_source_crew_and_handoff_id():
    adapter = CronMigrationAdapter(job_lookup={"j1": _job()})
    bundle = await plan_cron_move(
        adapter,
        "j1",
        target=P.CrewRef(crew_id="dst"),
        source=P.CrewRef(crew_id="src", label="source"),
        handoff_id="fixed-h",
    )
    assert bundle.source_crew.crew_id == "src"
    assert bundle.handoff_id == "fixed-h"


@pytest.mark.asyncio
async def test_plan_cron_move_unknown_job_raises():
    adapter = CronMigrationAdapter(job_lookup={})
    with pytest.raises(KeyError):
        await plan_cron_move(adapter, "missing", target=P.CrewRef(crew_id="dst"))
