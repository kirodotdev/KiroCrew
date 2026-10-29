"""Remote-dispatch headroom: ``GET /api/chat/headroom`` and the ``crew_busy`` gate.

The peer half of placement in ``docs/request-for-change/rfc-conductor-remote-dispatch.md``
§2b (wave 3). A conductor on another machine reads this crew's headroom before it
places a work item here, and ``POST /api/chat/slots`` with a ``work_item`` asks
:func:`create_admission` first: a short resource refuses the create as
``crew_busy`` before anything is minted. A create without ``work_item`` is never
gated; it only gains the ``headroom`` field on its reply.

Everything here is behind the ``work_ledger.remote_dispatch`` preview switch,
read on every request. Off (the default), the route answers 404 and the create
handler sees ``(None, None)``: no gate, no new reply field.

The snapshot carries numbers and status words only. Every field is rebuilt from
a typed reading here, never copied from a probe's text, so no path, user name or
credential can reach it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

#: The ``short`` words a ``crew_busy`` refusal names.
SHORT_MEMORY = "memory"
SHORT_CPU = "cpu"
SHORT_DISK = "disk"

#: ``load_per_cpu`` at or above which the CPU is short. Nothing on main turns CPU
#: into a posture, so the gate needs its own threshold (RFC §2b).
CPU_LOAD_THRESHOLD = 1.0

_GB = 1024**3


@dataclass(frozen=True)
class RemoteDispatchSettings:
    """The ``work_ledger.remote_dispatch*`` keys as one read."""

    enabled: bool
    memory_gb: float
    disk_gb: float


_DISABLED = RemoteDispatchSettings(enabled=False, memory_gb=0.0, disk_gb=0.0)


def remote_dispatch_settings() -> RemoteDispatchSettings:
    """The preview switch and the two estimates. Blocking: call off the loop.

    A config that cannot load reads as OFF, so a broken config leaves every
    create exactly as it is without the preview.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        section = KiroCrewConfig.load().work_ledger
    except Exception:
        logger.warning("remote dispatch: config unreadable; preview treated as off", exc_info=True)
        return _DISABLED
    if section.remote_dispatch is not True:
        return _DISABLED
    return RemoteDispatchSettings(
        enabled=True,
        memory_gb=float(section.remote_dispatch_memory_gb),
        disk_gb=float(section.remote_dispatch_disk_gb),
    )


def _reading(value: object) -> float | None:
    """*value* as a non-negative finite number, else None. Not rounded: the gate
    compares this, so a reading just under an estimate stays under it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _shown(value: float | None) -> float | None:
    """A reading as the snapshot shows it, rounded to 2 places."""
    return None if value is None else round(value, 2)


def _disk_free_gb() -> float | None:
    """Free GB on the filesystem holding the scratch root, unrounded, or None.

    The scratch root is created lazily, so a crew that has not allocated scratch
    yet measures the nearest existing ancestor: the same filesystem a worker's
    clone would land on.
    """
    try:
        from kiro_crew.agent_scratch import scratch_root

        path: Path = scratch_root()
        while not path.exists() and path.parent != path:
            path = path.parent
        return _reading(shutil.disk_usage(path).free / _GB)
    except Exception:
        logger.debug("remote dispatch: scratch disk unreadable", exc_info=True)
        return None


def _running_agents() -> int | None:
    """Running agents at the busiest admission point, or None outside the gateway.

    The controller's ``saturating`` figure: each admission point (subagents, the
    runner lane) is bounded by the execution cap on its own, so the cap is full
    when one point fills it, not when the two together reach it.

    None while the controller is disabled: it stops sampling then, so its last
    sample is stale and must not refuse dispatch for as long as it stays off.
    """
    from kiro_crew.resource_status import adaptive_state

    state = adaptive_state() or {}
    if state.get("enabled") is not True:
        return None
    sample = state.get("last_sample") or {}
    running = sample.get("saturating") if isinstance(sample, dict) else None
    if not isinstance(running, int) or isinstance(running, bool) or running < 0:
        return None
    return running


def _open_workers() -> int:
    """Peer slots bound to a ``hub:`` mailbox item and not closed.

    Always 0 in this wave: the ``hub:`` mailbox arrives with wave 2.
    """
    return 0


def measure(settings: RemoteDispatchSettings) -> tuple[dict[str, Any], str]:
    """Take the headroom snapshot and name the first short resource ("" for none).

    Blocking: call off the loop. Never raises. A reading that cannot be taken is
    ``None`` in the snapshot and is never the reason for a refusal, as
    ``resource_status.admission_check`` fails open on an unknown posture.
    """
    from kiro_crew import resource_status

    try:
        status = resource_status.probe()
        mem_free, cpu_count = _reading(status.available_gb), status.cpu_count
        load, posture = status.load_per_cpu, status.posture
    except Exception:
        logger.warning("remote dispatch: resource probe failed", exc_info=True)
        mem_free, cpu_count, load, posture = None, None, None, resource_status.POSTURE_UNKNOWN
    if posture not in (
        resource_status.POSTURE_AMPLE,
        resource_status.POSTURE_TIGHT,
        resource_status.POSTURE_CRITICAL,
    ):
        posture = resource_status.POSTURE_UNKNOWN
    load_per_cpu = _reading(load)
    disk_free = _disk_free_gb()
    headroom: dict[str, Any] = {
        "mem_free_gb": _shown(mem_free),
        "cpu_count": cpu_count if isinstance(cpu_count, int) and cpu_count > 0 else None,
        "load_per_cpu": _shown(load_per_cpu),
        "posture": posture,
        "disk_free_gb": _shown(disk_free),
        "open_workers": _open_workers(),
        "measured_at": round(time.time(), 3),
    }
    return headroom, _short_resource(settings, posture, load_per_cpu, disk_free)


def _short_resource(
    settings: RemoteDispatchSettings,
    posture: str,
    load_per_cpu: float | None,
    disk_free: float | None,
) -> str:
    from kiro_crew import resource_status
    from kiro_crew.subagent import check_memory_available

    try:
        memory_ok, _ = check_memory_available(settings.memory_gb)
    except Exception:
        logger.warning("remote dispatch: memory check failed", exc_info=True)
        memory_ok = True
    if not memory_ok or posture == resource_status.POSTURE_CRITICAL:
        return SHORT_MEMORY
    if load_per_cpu is not None and load_per_cpu >= CPU_LOAD_THRESHOLD:
        return SHORT_CPU
    try:
        cap = resource_status.adaptive_exec_cap()
        running = _running_agents()
    except Exception:
        logger.warning("remote dispatch: execution cap unreadable", exc_info=True)
        cap, running = 0, None
    if cap > 0 and running is not None and running >= cap:
        return SHORT_CPU
    if disk_free is not None and disk_free < settings.disk_gb:
        return SHORT_DISK
    return ""


async def create_admission(
    request: web.Request, body: dict[str, Any]
) -> tuple[web.Response | None, dict[str, Any] | None]:
    """The create handler's one hook: ``(refusal, headroom)``.

    ``(None, None)`` when the preview is off or the caller is not the owner, so
    the create runs and answers as without it: the snapshot is the owner's, as
    on ``GET /api/chat/headroom``, and only the owner dispatches (RFC §4).
    Otherwise the snapshot to put on the reply, and a ``crew_busy`` refusal when
    the body carries a ``work_item`` and a resource is short.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        return None, None
    settings = await asyncio.to_thread(remote_dispatch_settings)
    if not settings.enabled:
        return None, None
    headroom, short = await asyncio.to_thread(measure, settings)
    if body.get("work_item") is not None and short:
        return (
            web.json_response(
                {
                    "error": "crew_busy",
                    "code": "crew_busy",
                    "short": short,
                    "headroom": headroom,
                },
                status=503,
            ),
            headroom,
        )
    return None, headroom


async def api_chat_headroom(request: web.Request) -> web.Response:
    """GET /api/chat/headroom — this crew's headroom snapshot, owner only.

    Preview off: 404, the answer for a route that does not exist.
    """
    settings = await asyncio.to_thread(remote_dispatch_settings)
    if not settings.enabled:
        raise web.HTTPNotFound()
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "chat.headroom.read")
    if denied is not None:
        return denied
    headroom, _ = await asyncio.to_thread(measure, settings)
    return web.json_response(headroom)
