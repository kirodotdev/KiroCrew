"""Auto-nudge HTTP API — list / start / stop / update loops for chat slots."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.autonudge import get_instance as _autonudge_get

# The security chokepoint lives in the transport-agnostic module (see its
# docstring); re-exported here so existing importers keep working. This file
# is intentionally a THIN HTTP mapping over it.
from kiro_crew.autonudge_authz import (  # noqa: F401 - re-exported
    _governance_session_key,
    authorize_and_add_nudge,
    authorize_and_update_nudge,
    resolve_stop_sentinel,
    vet_exit_gate_cmd,
)
from kiro_crew.dashboard.session_directive_apply import (
    _binding_work_generation,
    _gate_cwd,
    run_exit_gate_for_user,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def render_nudge_message(message: str, stop_sentinel_path: str | None) -> str:
    """Replace {{STOP_FILE}} template with the resolved sentinel path."""
    return message.replace("{{STOP_FILE}}", stop_sentinel_path or "")


def _serialize(loop: Any) -> dict:
    return asdict(loop)


async def api_autonudge_list(request: web.Request) -> web.Response:
    """GET /api/autonudge — list all active loops."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "loops": []})
    return web.json_response({"enabled": True, "loops": [_serialize(lp) for lp in svc.list_all()]})


async def api_autonudge_get(request: web.Request) -> web.Response:
    """GET /api/autonudge/{slot_key} — loop bound to this slot (or null)."""
    svc = _autonudge_get()
    slot_key = request.match_info["slot_key"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_slot(slot_key)
    return web.json_response({"enabled": True, "loop": _serialize(loop) if loop else None})


async def api_autonudge_start(request: web.Request) -> web.Response:
    """POST /api/autonudge — start or replace a loop on a slot.

    Body: { slot_key, message, idle_secs?, max_cycles?, max_runtime_secs?, stop_sentinel_path? }
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled (KIROCREW_AUTONUDGE not set)",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # idle_secs/max_cycles/max_runtime_secs come straight from the request
    # body: int() raises ValueError on "abc", TypeError on null/list, and
    # OverflowError on float("inf") (1e309 is legal JSON in aiohttp's parser),
    # any of which would surface as a 500 instead of a 400. Non-integral
    # floats are rejected rather than silently truncated (int(1.5) -> 1 would
    # store a value the caller never asked for). Coerce up front and reject
    # bad input, matching the sibling handlers_instances.api_instances_add
    # guard on the same pattern.
    try:
        for _name in ("idle_secs", "max_cycles", "max_runtime_secs"):
            _val = body.get(_name)
            if isinstance(_val, float) and not _val.is_integer():
                return web.json_response(
                    {"error": f"{_name} must be a whole number", "code": "not_a_whole_number"},
                    status=400,
                )
        idle_secs = int(body.get("idle_secs", 60))
        max_cycles = int(body.get("max_cycles", 0))
        max_runtime_secs = int(body.get("max_runtime_secs", 0))
    except (TypeError, ValueError, OverflowError):
        return web.json_response(
            {"error": "idle_secs, max_cycles and max_runtime_secs must be integers"}, status=400
        )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=(body.get("session_key") or body.get("slot_key") or ""),
        message=(body.get("message") or ""),
        idle_secs=idle_secs,
        max_cycles=max_cycles,
        stop_sentinel_path=(body.get("stop_sentinel_path") or ""),
        max_runtime_secs=max_runtime_secs,
        exit_gate_cmd=(body.get("exit_gate_cmd") or ""),
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_update(request: web.Request) -> web.Response:
    """PATCH /api/autonudge/{loop_id} — update message / idle_secs / active.

    Thin HTTP mapping over ``authorize_and_update_nudge``, which owns the
    message redaction, the integer coercion, and the audit-or-deny policy — see
    its docstring for why those live in the transport-agnostic module and not
    here.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    loop, error, status = await authorize_and_update_nudge(
        svc=svc,
        loop_id=loop_id,
        message=body.get("message"),
        idle_secs=body.get("idle_secs"),
        max_cycles=body.get("max_cycles"),
        active=body.get("active"),
        max_runtime_secs=body.get("max_runtime_secs"),
        exit_gate_cmd=body.get("exit_gate_cmd"),
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_delete(request: web.Request) -> web.Response:
    """DELETE /api/autonudge/{loop_id} — stop and remove a loop."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    # Capture slot_key for audit before removal (loop is gone after remove()).
    existing = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    await svc.remove(loop_id)
    sel().log_tool_invocation(
        session_key=existing.slot_key if existing else "",
        source="dashboard",
        tool_name="autonudge_delete",
        outcome="success" if existing else "noop",
        metadata={"loop_id": loop_id, "caller": request.remote or ""},
    )
    return web.json_response({"ok": True})


async def api_autonudge_run_gate(request: web.Request) -> web.Response:
    """POST /api/autonudge/{loop_id}/gate — USER-run exit-gate verification.

    This is the ONLY path that executes a loop's exit gate. It exists on the
    authenticated user surface precisely so that execution is bound to a
    fresh, explicit user action naming the exact command and cwd (GPT review
    on the exit-gate PR: the agent's auto-approved directives must never
    trigger execution of workspace content the agent can modify — the
    agent-side ``autonudge_stop`` only reads the result recorded here).

    Flow: re-vet the stored command against CURRENT policy (arm-time vetting
    can go stale when governance tightens) → resolve the slot's cwd anchor →
    execute via the strict-sandboxed runner → record ``gate_last_status`` /
    ``gate_last_ts`` on the loop. If the loop was PAUSED awaiting
    verification (``stopped_reason == "gate_pending"``) and the gate passes,
    the loop is removed — the verified closure the agent's stop deferred.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {"error": "auto-nudge disabled", "code": "autonudge_disabled"},
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    loop = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    if loop is None:
        return web.json_response(
            {"error": "unknown loop", "code": "loop_not_found"}, status=404
        )
    gate = str(getattr(loop, "exit_gate_cmd", "") or "").strip()
    if not gate:
        return web.json_response(
            {"error": "this loop has no exit gate", "code": "no_exit_gate"},
            status=400,
        )
    # AUDIT-OR-PROCEED: user-initiated execution is SEL-audited before it
    # runs (matching the arm path's invoked-before-mutation shape).
    sel().log_tool_invocation(
        session_key=loop.slot_key,
        source="dashboard",
        tool_name="autonudge_run_gate",
        outcome="invoked",
        metadata={"loop_id": loop_id, "caller": request.remote or ""},
    )
    # Re-vet against CURRENT policy: a governance profile tightened after
    # arm time must apply to the stored command. Off-thread (policy reads).
    revet_err = await asyncio.to_thread(
        vet_exit_gate_cmd, gate, _governance_session_key(loop.slot_key)
    )
    if revet_err:
        return web.json_response(
            {
                "error": f"stored gate no longer passes vetting: {revet_err}",
                "code": "gate_vet_failed",
            },
            status=409,
        )
    state: DashboardState | None = request.app.get("state")
    # REFUSE while the bound slot's turn is in flight, and BIND the run to
    # the session's work generation (GPT review): an idle precheck alone
    # misses a turn that STARTS during the 120s run — capture the turn-task
    # identity before executing and refuse to record when it changed after.
    running, work_gen = await asyncio.to_thread(
        _binding_work_generation, state, loop.slot_key
    )
    if running:
        return web.json_response(
            {
                "error": (
                    "the loop's session still has a turn in flight — wait "
                    "for the agent's turn to finish, then run the gate"
                ),
                "code": "slot_turn_running",
            },
            status=409,
        )
    gate_cwd = await asyncio.to_thread(_gate_cwd, state, loop.slot_key)
    # Snapshot the state the result will vouch for BEFORE the (up to 120s)
    # run: if the command is PATCHed or the loop fires again mid-run, the
    # result belongs to the old state and must not be recorded (GPT review:
    # a stale pass recorded as fresh would close an unverified loop). The
    # pending GENERATION is snapshotted too — a pass may close only the
    # pause it started under, never one that arrived mid-run (a run started
    # while the loop was active verified pre-pause work).
    fire_ts_before = float(getattr(loop, "last_fire_ts", 0.0) or 0.0)
    pending_gen: int | None = None
    if not getattr(loop, "active", True) and (
        str(getattr(loop, "stopped_reason", "") or "") == "gate_pending"
    ):
        pending_gen = int(getattr(loop, "gate_pause_gen", 0) or 0)
    outcome = await run_exit_gate_for_user(loop_id, gate, cwd=gate_cwd)
    status = str(outcome.get("status"))
    # Re-check the work generation AFTER the run: a turn that started (or
    # started-and-finished) during the 120s window mutated the workspace
    # the result claims to have verified — refuse to record it.
    running2, work_gen2 = await asyncio.to_thread(
        _binding_work_generation, state, loop.slot_key
    )
    if running2 or work_gen2 != work_gen:
        sel().log_tool_invocation(
            session_key=loop.slot_key,
            source="dashboard",
            tool_name="autonudge_run_gate",
            outcome="error",
            metadata={
                "loop_id": loop_id,
                "gate_status": status,
                "record": "session_worked_during_run",
            },
        )
        return web.json_response(
            {
                "error": (
                    "session work started during the gate run — the result "
                    "was NOT recorded; wait for the turn to finish and "
                    "re-run"
                ),
                "code": "session_worked_during_run",
                "gate_status": status,
                "note": outcome.get("note", ""),
            },
            status=409,
        )
    record = "skipped"
    if status in ("pass", "fail"):
        record = await svc.record_gate_result(
            loop_id,
            status=status,
            gate_cmd=gate,
            fire_ts=fire_ts_before,
            pending_gen=pending_gen,
        )
    closed = record == "closed"
    sel().log_tool_invocation(
        session_key=loop.slot_key,
        source="dashboard",
        tool_name="autonudge_run_gate",
        outcome="success" if status == "pass" else "error",
        metadata={
            "loop_id": loop_id,
            "gate_status": status,
            "record": record,
            "closed": closed,
        },
    )
    if record in ("stale", "gone"):
        return web.json_response(
            {
                "error": (
                    "the loop's gate command or fire state changed while the "
                    "gate ran (or the loop was removed) — result NOT "
                    "recorded; re-run to verify the current state"
                ),
                "code": "gate_result_stale",
                "gate_status": status,
                "note": outcome.get("note", ""),
            },
            status=409,
        )
    if status == "pass":
        return web.json_response(
            {
                "ok": True,
                "gate_status": status,
                "note": outcome.get("note", ""),
                "loop_closed": closed,
            }
        )
    # Non-pass (fail or not_run): an error response per the repo's error-code
    # contract — static status, machine-readable code, prose error. The
    # specific outcome stays in gate_status.
    return web.json_response(
        {
            "ok": False,
            "gate_status": status,
            "note": outcome.get("note", ""),
            "loop_closed": False,
            "error": "exit gate did not pass",
            "code": "gate_not_passed",
        },
        status=422,
    )
