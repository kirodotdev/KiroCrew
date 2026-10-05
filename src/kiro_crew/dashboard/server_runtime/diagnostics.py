"""Boot telemetry and the diagnostic recorder.

The gateway-start telemetry event and its pre-computed fields, the recorder started
beside the loop stall watchdog, and the recorder's shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        current_context,
        logger,
    )


def _precompute_telemetry(state: "DashboardState") -> None:
    """Pre-compute telemetry data (blocking I/O — call before server starts)."""
    from kiro_crew.dashboard.handlers_system import _get_owner_hash, _get_static_system_info

    _log = logging.getLogger(__name__)
    owner_hash = "unknown"
    try:
        owner_hash = _get_owner_hash(state)
    except Exception:
        _log.warning("Failed to pre-compute owner hash", exc_info=True)
    static_info: dict = {}
    try:
        static_info = dict(_get_static_system_info())
    except Exception:
        _log.warning("Failed to pre-compute system info", exc_info=True)

    # Backend telemetry sink (PlatformContext).  The Default TelemetryProvider's
    # record_event is a no-op, so standalone is unchanged; the companion
    # records a gateway-start event.  Best-effort — a telemetry failure never
    # blocks server startup.
    try:
        current_context().telemetry.record_event(
            "gateway_start",
            {
                "owner_id_hash": owner_hash,
                "os_type": static_info.get("os", ""),
                "arch": static_info.get("arch", ""),
            },
        )
    except Exception:
        _log.debug("telemetry.record_event(gateway_start) failed", exc_info=True)


def _register_diag_recorder_shutdown(app: web.Application) -> None:
    """Register the cleanup hook that stops the diagnostic recorder.

    Called before ``runner.setup()`` for the same reason as the watchdog hook:
    appending to ``on_cleanup`` after ``runner.setup()`` raises "Cannot modify
    frozen list". The recorder is created after setup and reached through its module
    singleton, so this resolves whatever instance boot published (and nothing,
    harmlessly, in a test that never started one). Stopping it cancels its two
    tasks, stops the GIL probe thread and removes the gc callback that probe
    installed -- a dashboard spun up repeatedly in tests would otherwise
    accumulate both.
    """

    async def _diag_recorder_shutdown(app_: web.Application) -> None:
        from kiro_crew.diag.recorder import get_recorder

        recorder = get_recorder()
        if recorder is not None:
            try:
                # Awaited, not fired and forgotten: stop() hands back the task
                # doing the off-loop finish, and cleanup returning before it runs
                # lets loop teardown drop the closing event and leave the probe
                # thread joined by nobody. Awaiting a thread yields, so this does
                # not put the join back on the loop.
                pending = recorder.stop()
                if pending is not None:
                    await pending
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("diag recorder stop failed", exc_info=True)

    app.on_cleanup.append(_diag_recorder_shutdown)


def _start_diag_recorder(state: DashboardState) -> None:
    """Start the diagnostic recorder with the gateway's own counters as a source.

    Sits beside the watchdog because it answers the question the watchdog
    cannot: the watchdog captures the moment the loop wedges, and the adaptive
    controller samples the host every 5s into a 60-entry in-memory ring that
    dies with the process — so "what was this host doing at 21:50:44?" had no
    answer at all. The recorder writes one row every 30s to
    ``<config_dir>/diag/snapshots-<day>.jsonl`` and keeps a week.

    Deliberately NOT stashed on ``state``: ``Recorder.start`` publishes the
    instance through ``diag.recorder.get_recorder()``, the single publication
    point a future reader resolves. A second reference here would be a second
    source of truth for the same object -- and an attribute this class does not
    declare, which mypy rejects.
    """
    from kiro_crew.diag.recorder import Recorder as _DiagRecorder

    _diag_recorder = _DiagRecorder()

    def _diag_gateway_source() -> dict:
        """Gateway-owned counters for one recorder row.

        Registered here rather than read inside the recorder so that module
        keeps no dashboard import — it starts on this boot path, where an import
        cycle is fatal. Every read is an EXISTING canonical accessor
        (``state.sessions.count``, ``state.subagents.count``,
        ``inventory_gauges.read_active_monitor_loops``,
        ``resource_status.adaptive_state``), so the recorder's numbers are the
        same ones the dashboard and ``resource_status`` report rather than a
        second opinion. Each field degrades to ``None`` on its own, because a
        gauge that cannot be read must not cost the whole row.
        """
        from kiro_crew import resource_status as _rs
        from kiro_crew.metrics import inventory_gauges as _gauges

        out: dict = {}
        try:
            out["sessions"] = state.sessions.count
        except Exception:  # noqa: BLE001 - a gauge failure is a null field
            out["sessions"] = None
        try:
            out["subagents"] = state.subagents.count if state.subagents else 0
        except Exception:  # noqa: BLE001
            out["subagents"] = None
        try:
            out["live_loops"] = _gauges.read_active_monitor_loops()
        except Exception:  # noqa: BLE001
            out["live_loops"] = None
        try:
            adaptive = _rs.adaptive_state() or {}
        except Exception:  # noqa: BLE001
            adaptive = {}
        last = adaptive.get("last_sample") or {}
        decision = adaptive.get("last") or {}
        try:
            # The same choice ``resource_status`` makes between the effective cap
            # and the ceiling, rather than a second reading of it. It answers 0
            # for "unknown", which must not reach the drop detector as a cap that
            # fell to zero — so it becomes None.
            cap = _rs.adaptive_exec_cap() or None
        except Exception:  # noqa: BLE001
            cap = None
        out.update(
            {
                # ``adaptive_cap`` is the key ``_detect_adaptive_drop`` watches,
                # so a cap that falls becomes an event rather than a number a
                # reader has to diff by hand.
                "adaptive_cap": cap,
                "adaptive_action": decision.get("action"),
                "adaptive_reason": decision.get("reason"),
                "adaptive_signals": decision.get("signals"),
                "adaptive_paused": decision.get("paused"),
                "adaptive_enabled": adaptive.get("enabled"),
                "subagents_running": last.get("running"),
                "subagents_queued": last.get("queued"),
                "controller_loop_lag_ms": last.get("loop_lag_ms"),
            }
        )
        return out

    _diag_recorder.register_source("gateway", _diag_gateway_source)
    try:
        _diag_recorder.start(asyncio.get_running_loop())
    except Exception:  # noqa: BLE001 - a diagnostic must never block the boot
        logger.warning("diag recorder failed to start", exc_info=True)
