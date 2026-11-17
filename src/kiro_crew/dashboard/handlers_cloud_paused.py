"""Record a cloud crew's Stop / Start on its Instances row.

``POST /api/cloud/{tag}/stop`` parks the EC2 instance, but the registry row that
reaches it knew nothing of that: auto-connect and the startup revive kept dialling
a machine that was off, and the panel showed the SSM ``TargetNotConnected`` failure
as a red Error. This module sets the row's ``cloud_stopped`` flag after a Stop and
clears it after a Start, so the connect path refuses a stopped crew and the panel
can show it as Stopped with Start as the way back.

Kept out of ``handlers_cloud`` because that module is past the size cap.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.dashboard.handlers_instances_gate import _apply_update, _registry

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.handlers_cloud_paused")


def _rows_for_target(reg, instance_id: str) -> list:
    """Every registry row reaching EC2 instance *instance_id*. Blocking."""
    return [inst for inst in reg.list() if inst.ssm_target == instance_id]


async def mark_cloud_stopped(
    state: "DashboardState", instance_id: str, tag: str, stopped: bool
) -> web.Response | None:
    """Set (or clear) ``cloud_stopped`` on every row reaching EC2 instance *instance_id*.

    *instance_id* is the one ``ec2.stop`` / ``ec2.start`` acted on, so a tag shared by
    two launches cannot mark the wrong crew. *tag* only names the log line. A stop also
    tears the tunnel down under the manager lock, so a connect racing it is either
    torn down or refused.

    Returns ``None`` on success. When a row cannot be marked the AWS action has still
    happened, so the answer is a 500 that says so and names the retry, instead of a
    200 that leaves a stale flag refusing every connect.
    """
    if not instance_id:
        return None
    reg = _registry(state)
    try:
        rows = await asyncio.to_thread(_rows_for_target, reg, instance_id)
        mgr = getattr(state, "instances_manager", None)
        for row in rows:
            if row.cloud_stopped == stopped:
                continue
            apply = functools.partial(_apply_update, reg, row.id, {"cloud_stopped": stopped})
            if stopped and mgr is not None:
                await mgr.reconfigure(row.id, apply)
            else:
                await asyncio.to_thread(apply)
    except Exception as exc:  # noqa: BLE001 - reported to the caller below
        action = "stop" if stopped else "start"
        logger.warning("could not record cloud %s on crew %s: %s", action, tag, exc)
        return web.json_response(
            {
                "error": f"The instance {action} completed, but Crew could not save the "
                f"crew's {'stopped' if stopped else 'running'} state. "
                f"Fix the problem and press {action.capitalize()} again to retry.",
                "code": "cloud_state_not_saved",
                "action_completed": True,
            },
            status=500,
        )
    return None
