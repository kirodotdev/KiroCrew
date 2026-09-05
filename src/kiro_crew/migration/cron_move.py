"""Cron move coordination core (slice 2, circle 2 — issue #7577).

The single place that turns a source cron job into a ``MigrationBundle`` ready
for the coordinator's transmit step. Both ``kirocrew cron move`` (CLI) and the
Schedule-tab "Move to crew…" action are thin shells over this — keeping the
allow-list and requirement derivation in exactly one place (Task 2.6).

Pure and adapter-driven: it calls the injected ``CronMigrationAdapter`` for
serialize + requirements and assembles the bundle. It does NOT quiesce,
transmit, or tombstone — those are the coordinator's ordered steps; this only
BUILDS what will be sent, so it is safe to call for a dry-run/preview too.
"""

from __future__ import annotations

import time

from kiro_crew.migration import protocol as P
from kiro_crew.migration.cron_adapter import CronMigrationAdapter
from kiro_crew.migration.move_plan import plan_unit_move


async def plan_cron_move(
    adapter: CronMigrationAdapter,
    job_id: str,
    *,
    target: P.CrewRef,
    source: P.CrewRef | None = None,
    handoff_id: str | None = None,
    clock=time.time,
) -> P.MigrationBundle:
    """Build the ``MigrationBundle`` for moving cron ``job_id`` to ``target``.

    Kept as the cron-named entry point; the body is the generic
    :func:`~kiro_crew.migration.move_plan.plan_unit_move`, because nothing here
    was ever cron-specific — it only used the adapter seam. Session and task-run
    moves call the generic function directly.
    """
    return await plan_unit_move(
        adapter, job_id, target=target, source=source, handoff_id=handoff_id, clock=clock
    )
