"""Dashboard move endpoints for crew-to-crew work migration (issue #7577).

Three POST routes, one per unit kind, each returning the migration PLAN rather
than performing it: the handoff id, how many allow-listed fields would travel,
the target's blocking requirements, and any advisory findings. The transmit /
quiesce / tombstone steps run over the crew tunnel and land with that wiring;
until then the plan is what the UI shows so a user can see what a move would do.

Why these live in the gateway and not only in the CLI:

  * a SESSION bundle is Layer A plus Layer B joined via session_map.json, and is
    only coherent when taken from the live slot. The gateway holds the slot; the
    CLI does not, which is why ``kirocrew session move`` refuses.
  * a TASK RUN held in memory carries ``WorkingMemory`` and ``current_task``,
    neither of which ``runs.json`` persists. Planning from the live record is
    therefore higher fidelity than planning from disk.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from aiohttp import web

from kiro_crew.migration import protocol as P
from kiro_crew.migration.move_plan import plan_unit_move

logger = logging.getLogger(__name__)


def _sel():
    from kiro_crew.security_event_log import sel

    return sel()


async def _read_target(request: web.Request) -> tuple[str, web.Response | None]:
    """Parse and validate the ``to_crew`` body field common to all three routes."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    target = (body.get("to_crew") or "").strip()
    if not target:
        return "", web.json_response(
            {
                "error": "to_crew is required (the target crew id/label)",
                "code": "migration_no_target",
            },
            status=400,
        )
    return target, None


def _finding_json(f: P.Finding) -> dict[str, Any]:
    return {"kind": f.kind, "detail": f.detail, "severity": f.severity, "detail_key": f.detail_key}


def _plan_json(
    bundle: P.MigrationBundle, target: str, *, findings: list[P.Finding] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "handoff_id": bundle.handoff_id,
        "bundle_kind": bundle.bundle_kind,
        "bundle_version": bundle.bundle_version,
        "target_crew": target,
        "ships": len(bundle.payload),
        "requirements": [
            {"kind": r.kind, "identity": r.identity, "severity": r.severity}
            for r in bundle.requirements
        ],
        "findings": [_finding_json(f) for f in (findings or [])],
        **extra,
    }


def _audit(operation: str, unit_id: str, target: str, handoff_id: str) -> None:
    try:
        _sel().log_api_access(
            caller="dashboard",
            operation=operation,
            outcome="planned",
            source="dashboard",
            resources=f"unit_id={unit_id} target={target} handoff_id={handoff_id}",
        )
    except Exception:  # pragma: no cover - audit must never break the response
        logger.debug("migration move audit failed", exc_info=True)


# ------------------------------------------------------------------ cron (2.6)


async def api_cron_move(request: web.Request) -> web.Response:
    """POST /api/crons/{job_id}/move — plan a cron job's migration."""
    from kiro_crew.migration.cron_adapter import CronMigrationAdapter

    state = request.app["state"]
    job_id = request.match_info["job_id"]
    target, err = await _read_target(request)
    if err is not None:
        return err

    job = state.crons.get_job(job_id)
    if job is None:
        return web.json_response(
            {"error": f"cron job not found: {job_id}", "code": "migration_unit_not_found"},
            status=404,
        )

    adapter = CronMigrationAdapter(job_lookup={job_id: job})
    bundle = await plan_unit_move(
        adapter,
        job_id,
        target=P.CrewRef(crew_id=target, label=target),
        source=P.CrewRef(crew_id="local", label="local"),
    )
    _audit("cron.move", job_id, target, bundle.handoff_id)
    return web.json_response({"ok": True, "plan": _plan_json(bundle, target)})


# --------------------------------------------------------------- taskrun (4.8)


async def api_taskrun_move(request: web.Request) -> web.Response:
    """POST /api/taskrunner/{task_id}/move — plan a task run's migration."""
    from kiro_crew.migration.taskrun_adapter import (
        TaskRunMigrationAdapter,
        describe_discarded_progress,
        run_fidelity_findings,
    )

    state = request.app["state"]
    task_id = request.match_info["task_id"]
    target, err = await _read_target(request)
    if err is not None:
        return err

    runner = getattr(state, "task_runner", None)
    if not runner:
        return web.json_response(
            {"error": "task runner is not available", "code": "migration_no_runner"}, status=503
        )

    # The LIVE record, which carries WorkingMemory and current_task -- state
    # runs.json does not persist. This is the fidelity advantage of planning
    # from the gateway rather than from disk.
    runs = getattr(runner, "_runs", {}) or {}
    run = runs.get(task_id)
    if run is None:
        return web.json_response(
            {"error": f"task run not found: {task_id}", "code": "migration_unit_not_found"},
            status=404,
        )

    adapter = TaskRunMigrationAdapter(run_lookup={task_id: run})
    # Refuse a run with a task in flight before describing a move that quiesce
    # would reject anyway.
    try:
        await adapter.quiesce(task_id)
    except P.MidRunError as exc:
        return web.json_response(
            {
                "error": f"{exc} — cannot migrate a run with a task mid-execution",
                "code": "migration_mid_run",
            },
            status=409,
        )

    bundle = await plan_unit_move(
        adapter,
        task_id,
        target=P.CrewRef(crew_id=target, label=target),
        source=P.CrewRef(crew_id="local", label="local"),
    )
    desc = describe_discarded_progress(run)
    _audit("taskrun.move", task_id, target, bundle.handoff_id)
    return web.json_response(
        {
            "ok": True,
            "plan": _plan_json(
                bundle,
                target,
                findings=run_fidelity_findings(run),
                completed_kept=desc["completed_count"],
            ),
        }
    )


# --------------------------------------------------------------- session (3.8)


async def _build_session_bundle(state, slot) -> dict:
    """Serialize the live slot into a Layer A + Layer B bundle.

    Split out as a module-level seam so the plan endpoint is testable without a
    real DashboardState: the transfer builder is the one piece that needs the
    live gateway.
    """
    from kiro_crew.dashboard.session_transfer import build_transfer_bundle_async

    return await build_transfer_bundle_async(state, slot, origin="local")


async def api_session_move(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/move — plan a chat session's migration."""
    from kiro_crew.migration.session_adapter import (
        SessionMigrationAdapter,
        carry_session_ledger,
        classify_session_portability,
        layer_b_fidelity_findings,
    )

    state = request.app["state"]
    slot_key = request.match_info["slot"]
    target, err = await _read_target(request)
    if err is not None:
        return err

    slot = state.get_slot(slot_key)
    if slot is None:
        return web.json_response(
            {"error": f"session not found: {slot_key}", "code": "migration_unit_not_found"},
            status=404,
        )

    raw = await _build_session_bundle(state, slot)
    portable, findings = classify_session_portability(raw)
    findings = findings + layer_b_fidelity_findings(raw)
    if raw.get("ledger"):
        carried, ledger_findings = carry_session_ledger(raw["ledger"])
        portable["ledger"] = carried
        findings = findings + ledger_findings

    # Requirements come from the adapter, not a hard-coded []. It reads the same
    # bundle we already have, so hand it back rather than rebuilding it.
    adapter = SessionMigrationAdapter(
        session_id=slot_key,
        controller=slot,
        bundle_builder=lambda _sid: raw,
        importer=lambda _p: "",
    )
    requirements = list(await adapter.requirements(slot_key))

    bundle = P.MigrationBundle(
        bundle_kind="session",
        bundle_version=2,  # matches session_transfer's Layer-B format
        handoff_id=uuid.uuid4().hex,
        created_ts=time.time(),
        source_crew=P.CrewRef(crew_id="local", label="local"),
        payload=portable,
        requirements=requirements,
    )
    _audit("session.move", slot_key, target, bundle.handoff_id)
    return web.json_response(
        {
            "ok": True,
            "plan": _plan_json(bundle, target, findings=findings),
        }
    )
