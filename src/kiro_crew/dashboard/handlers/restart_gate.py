"""``POST /api/restart/when-idle``: the gateway's own idle check before a restart.

``kirocrew restart --when-idle`` asks it before restarting, so a restart started
from another machine (the Remote Crew "Update" and "Restart" actions run that
command over SSH or SSM) waits for the turns, subagents and workflows that a
restart would cut. A plain ask is a READ of the one quiescence census
(:mod:`kiro_crew.quiescence`): it pauses nothing and holds nothing, so its
answer is only a snapshot. The CLI's last ask sets ``commit``, which takes the
same pause, drain and fence as a dashboard restart
(:func:`kiro_crew.restart_admission.admit_restart`, never forced): a turn that
starts after the answer is spooled, not cut, and the restart that follows
finds nothing new to interrupt. ``release`` reopens that admission when the
restart did not happen. This handler only gates the caller and reports the
answer.

A STRICT internal path (``server_internal_paths``): its only caller is the CLI
on this host with the local secret, never a browser. The handler requires the
secret-verified ``internal_auth`` mark, because a strict path without the
header falls through to cookie auth, and re-asserts host locality the way
``/api/update/approve`` does, because a ``local_only=False`` deployment
reclassifies strict paths as mixed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiohttp import web

from kiro_crew.config.loader import launched_as_managed_service
from kiro_crew.dashboard.handlers.updates_step_up import _loopback_peer
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.service.gateway_identity import service_identity

logger = logging.getLogger(__name__)

#: How long a committed answer keeps admission closed when the caller never
#: restarts or releases (its SSH session died, say). Far longer than a service
#: restart takes, short enough that a stuck caller cannot hold the gateway shut.
COMMIT_HOLD_SECS = 120.0


@dataclass(frozen=True)
class _GateBody:
    service_only: bool = False
    commit: bool = False
    release: bool = False


async def api_restart_when_idle(request: web.Request) -> web.Response:
    """POST /api/restart/when-idle — ``{idle, turns, background, service_managed}``.

    ``idle: true`` means nothing a restart would interrupt is running right now;
    admission is left alone. ``idle: false`` carries the counts. The
    optional body ``{"commit": true}`` makes an idle answer a promise: admission
    stays closed (paused and fenced) for the restart that follows, and the answer is
    ``idle: false`` when that could not be taken. ``{"release": true}`` reopens it.
    The optional body ``{"service_managed_only": true}`` says the caller will
    restart the kirocrew service. Without the managed-service marker the
    handler then asks the service manager (:func:`service_identity`) and adds
    ``service_identity``: ``"other"`` (the unit runs another process) answers
    ``idle: false`` with ``service_managed: false`` without reading the census,
    since a restart through the service will not reach this process; ``"service"``
    (the unit's main pid is this process) counts as managed; ``"unknown"``
    reads the census and leaves the call to the CLI. Any other body is
    ``400 invalid_body``. ``503 restart_gate_unavailable`` when
    this process runs no orchestrator to ask.
    """
    # Both checks are load-bearing. A loopback request with no X-Internal-Secret
    # falls through to cookie auth on a strict path, so loopback alone would let
    # any dashboard user (a shared-crew member behind a local proxy, say) pause
    # admission on repeat. ``internal_auth`` is set only after the middleware's
    # constant-time match on the secret, which the CLI always sends.
    if request.get("internal_auth") is not True or not _loopback_peer(request):
        return web.json_response(
            {
                "error": "the restart idle check is accepted from the gateway host only",
                "code": "restart_gate_not_local",
            },
            status=403,
        )
    body = await _read_body(request)
    if body is None:
        return web.json_response(
            {"error": "invalid request body", "code": "invalid_body"}, status=400
        )
    service_only = body.service_only
    service_managed = launched_as_managed_service()
    identity: str | None = None
    if service_only and not service_managed:
        # No marker: a unit installed before the marker existed still runs
        # without it, so ask the service manager whose process the unit runs.
        identity = await asyncio.to_thread(service_identity)
        service_managed = identity == "service"
    if identity == "other":
        # The caller's restart goes to the service, which runs another process:
        # this gateway's idleness is not the caller's question, and pausing
        # admission here would hold it for a restart that never comes.
        return web.json_response(
            {
                "idle": False,
                "turns": 0,
                "background": 0,
                "service_managed": False,
                "service_identity": identity,
            }
        )
    state: DashboardState = request.app["state"]
    probe = getattr(state, "_quiescence_probe", None)
    if not callable(probe):
        return web.json_response(
            {
                "error": "this gateway cannot check for in-flight work",
                "code": "restart_gate_unavailable",
            },
            status=503,
        )
    if body.release:
        # Only the hold this endpoint armed: a dashboard restart's own pause is not ours.
        held = getattr(state, "_restart_commit_hold", None) is not None
        _cancel_commit_hold(state)
        release = getattr(state, "_restart_release", None)
        if held and callable(release):
            await release()
        return web.json_response({"released": held})
    reason = ""
    if body.commit and not callable(getattr(state, "_restart_admission", None)):
        return web.json_response(
            {
                "error": "this gateway cannot hold admission for a restart",
                "code": "restart_gate_unavailable",
            },
            status=503,
        )
    if body.commit:
        # The explicit restart is a person's act: no typing window, and never
        # forced, so running work still refuses it.
        admission = await state._restart_admission(force=False)
        if admission.admitted:
            _arm_commit_hold(state)
        reason = admission.reason
        census = admission.census or probe(quiet_window_secs=0.0)
        idle = admission.admitted
    else:
        # Same judgment as the commit ask and admit_restart: the explicit restart
        # is a person's act, so only running work refuses it. The typing window
        # and the settle after busy work gate the unattended paths.
        census = probe(quiet_window_secs=0.0)
        idle = census.busy == 0
    turns = census.turns
    background = census.background + census.queued_messages + census.pending_approvals
    logger.info("Restart idle check: idle=%s turns=%d background=%d", bool(idle), turns, background)
    answer: dict[str, object] = {
        "idle": bool(idle),
        "turns": max(0, int(turns)),
        "background": max(0, int(background)),
        # Whether the service runs this gateway (its marker, or its unit's main
        # pid), so the CLI can tell that a service restart would reach it.
        "service_managed": service_managed,
    }
    if identity is not None:
        answer["service_identity"] = identity
    if reason:
        answer["reason"] = reason
    return web.json_response(answer)


def _cancel_commit_hold(state: DashboardState) -> None:
    handle = getattr(state, "_restart_commit_hold", None)
    if handle is not None:
        handle.cancel()
        state._restart_commit_hold = None


def _arm_commit_hold(state: DashboardState) -> None:
    """Reopen admission after :data:`COMMIT_HOLD_SECS` if nobody restarts or releases."""
    _cancel_commit_hold(state)

    def _expire() -> None:
        state._restart_commit_hold = None
        release = getattr(state, "_restart_release", None)
        if callable(release):
            task = asyncio.ensure_future(release())
            state._restart_commit_release_task = task

    state._restart_commit_hold = asyncio.get_running_loop().call_later(COMMIT_HOLD_SECS, _expire)


async def _read_body(request: web.Request) -> _GateBody | None:
    """The request body as a :class:`_GateBody`, or ``None`` if malformed.

    No body and ``{}`` both mean every flag ``False``; a flag must be a bool.
    """
    if not request.can_read_body:
        return _GateBody()
    try:
        body = await request.json()
    except ValueError:
        return None
    names = ("service_managed_only", "commit", "release")
    if not isinstance(body, dict) or set(body) - set(names):
        return None
    flags = [body.get(name, False) for name in names]
    if not all(isinstance(flag, bool) for flag in flags):
        return None
    return _GateBody(service_only=flags[0], commit=flags[1], release=flags[2])
