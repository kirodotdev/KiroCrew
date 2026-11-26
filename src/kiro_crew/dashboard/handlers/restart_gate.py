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
restart did not happen. ``restart`` asks the gateway to restart itself in place
(:func:`kiro_crew.dashboard.handlers.updates_apply._start_gateway_restart`, the
exec ``POST /api/restart`` ends in), so the process keeps its pid and a
supervisor that started it keeps its child; a committed hold is taken over by
that restart. Only the host owner's CLI may ask it: a caller with no session
and no app claim whose kernel-identified peer is an ordinary host process. Only
a supervised gateway restarts in place: one no service unit launched, with no
terminal on stdin, inside its parent's process group. Every other gateway
refuses, and the CLI stops it and starts a detached one. Each restart-mode
decision is a SEL record. Apart from that restart,
this handler only gates the caller and reports the answer.

A STRICT internal path (``server_internal_paths``): its only caller is the CLI
on this host with the local secret, never a browser. The handler requires the
secret-verified ``internal_auth`` mark, because a strict path without the
header falls through to cookie auth, and re-asserts host locality the way
``/api/update/approve`` does, because a ``local_only=False`` deployment
reclassifies strict paths as mixed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.config.loader import launched_as_managed_service
from kiro_crew.dashboard.handlers.updates_apply import _start_gateway_restart
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
    restart: bool = False


async def api_restart_when_idle(request: web.Request) -> web.Response:
    """POST /api/restart/when-idle — ``{idle, turns, background, service_managed}``.

    ``idle: true`` means nothing a restart would interrupt is running right now;
    admission is left alone. ``idle: false`` carries the counts. The
    optional body ``{"commit": true}`` makes an idle answer a promise: admission
    stays closed (paused and fenced) for the restart that follows, and the answer is
    ``idle: false`` when that could not be taken. ``{"release": true}`` reopens it.
    ``{"restart": true}`` restarts the gateway in place, from the host owner's CLI
    only (:func:`_restart_in_place`: ``403 restart_caller_not_cli`` for a caller
    that declares a session or an app, ``403 restart_in_place_unsupported`` off
    POSIX, ``403 restart_caller_not_host_owner`` for a
    peer the kernel does not name as an ordinary host process,
    ``403 restart_in_place_unsupervised`` for a gateway no supervisor runs): it
    takes over a committed hold (not forced, no second admission), and without
    one it is forced, like the dashboard's Restart button. It answers
    ``200 {"ok": true, "status": "restarting"}`` (plus ``"already_in_progress":
    true`` while a restart runs, from any route; that join is answered before the
    preflight, which would read the running restart's pause as held elsewhere),
    or ``409 {"ok": false, "error", "code"}``
    for a refusal read before the restart starts (``restart_launcher_invalid``,
    ``restart_interpreter_missing``, ``shutting_down``, and without a hold
    ``pause_held_elsewhere``). ``restart`` with another true flag is
    ``400 invalid_body``.
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
    if body.restart:
        return await _restart_in_place(request)
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
    named = getattr(census, "named_holders", None)
    if not idle and callable(named) and _is_host_cli_caller(request):
        answer["holders"], answer["holders_more"] = named()
    if identity is not None:
        answer["service_identity"] = identity
    if reason:
        answer["reason"] = reason
    return web.json_response(answer)


#: SEL operation of every restart-mode decision.
_RESTART_OPERATION = "gateway_restart_in_place"


async def _restart_in_place(request: web.Request) -> web.Response:
    """The restart mode: the host owner's CLI starts or joins a supervised gateway's restart.

    Two caller checks, both required. :func:`_is_host_cli_caller` refuses a caller that
    declares a session or carries an app claim (``restart_caller_not_cli``). The
    local secret is readable inside the agent sandbox and leaving the session
    header out costs nothing, so :func:`_host_owner_provenance` must then name the
    kernel-identified peer as an ordinary host process of the owner, not an app
    backend, the check ``/api/token/local`` mints an owner token on
    (``restart_caller_not_host_owner``). Off POSIX it refuses before that check
    (``restart_in_place_unsupported``): an exec there starts a new process, and
    the host-owner check verifies nothing there.
    Then, for a verified caller only, the gateway itself: one no supervisor runs
    (:func:`_unsupervised_reason`) refuses with ``restart_in_place_unsupervised``
    and a ``why`` token, before the orchestrator check, the commit hold or the
    restart are read. A gateway in a terminal, a detached one or a service's keeps
    the CLI's stop and spawn, which starts the new gateway with the calling shell's
    environment and install.
    A CLI this refuses falls back to its stop-and-spawn. Every decision is one SEL
    record.
    """
    if not _is_host_cli_caller(request):
        # The secret proves the call came from inside, not who made it: an
        # agent's tool call carries it too, and declares its session.
        return _restart_refusal(
            request,
            403,
            "restart_caller_not_cli",
            "an in-place restart is accepted from the CLI on the gateway host only",
        )
    if not platform_compat.IS_POSIX:
        # Off POSIX the exec starts a new process, so nothing stays in place, and
        # the host-owner check admits every peer unverified there.
        return _restart_refusal(
            request,
            403,
            "restart_in_place_unsupported",
            "this gateway's platform cannot restart in place",
        )
    provenance = await _host_owner_provenance(request)
    if provenance is None or provenance.app is not None:
        app = getattr(provenance, "app", None)
        return _restart_refusal(
            request,
            403,
            "restart_caller_not_host_owner",
            "the gateway could not verify this process as the host owner, so it does not "
            "restart in place",
            detail=f" app={app}" if app else "",
        )
    why = _unsupervised_reason()
    if why is not None:
        # No supervisor is waiting on this gateway, so it keeps the CLI's stop and
        # spawn, which starts it again with the calling shell's environment and
        # install.
        return _restart_refusal(
            request,
            403,
            "restart_in_place_unsupervised",
            "this gateway is not run by a supervisor, so it does not restart in place",
            detail=f" why={why}",
        )
    state: DashboardState = request.app["state"]
    if not callable(getattr(state, "_quiescence_probe", None)):
        return _restart_refusal(
            request, 503, "restart_gate_unavailable", "this gateway cannot check for in-flight work"
        )
    # The hold is read after the restart's last await, so one that expired or
    # was released meanwhile is not taken over, and dropped only once it starts.
    answer = await _start_gateway_restart(
        state,
        force=True,
        holds_admission=lambda: getattr(state, "_restart_commit_hold", None) is not None,
        hand_over_admission=lambda: _cancel_commit_hold(state),
        join_running=True,
    )
    _audit_restart_answer(answer, basis=provenance.basis)
    return answer


def _unsupervised_reason() -> str | None:
    """Why this gateway is not a supervisor's child, or ``None`` when it is.

    ``service`` when a generated unit or plist launched it: the service manager
    restarts it, and the CLI restarts the service. Otherwise the shape this
    process runs in (:func:`kiro_crew.platform_compat.supervision_shape_failure`):
    ``terminal`` for a terminal on stdin, ``own_group`` for a gateway that leads
    its process group or whose parent is outside it.
    """
    if launched_as_managed_service():
        return "service"
    return platform_compat.supervision_shape_failure()


async def _host_owner_provenance(request: web.Request) -> Any:
    """``member_memory_auth.local_owner_bootstrap_provenance`` of the caller, off the event loop.

    The peer pid comes from the socket, never from a header; the check reads
    ``/proc`` or runs ``lsof``, so it runs on the local-auth executor.
    """
    from kiro_crew.executors import local_auth_executor
    from kiro_crew.member_memory_auth import local_owner_bootstrap_provenance

    return await asyncio.get_running_loop().run_in_executor(
        local_auth_executor(), local_owner_bootstrap_provenance, request
    )


def _restart_refusal(
    request: web.Request, status: int, code: str, error: str, *, detail: str = ""
) -> web.Response:
    """A restart-mode refusal before anything starts, recorded as a denied SEL decision."""
    # The session header is the caller's own text, so the record names only its
    # kind: a caller can put anything there, the local secret included. The app
    # claim is the middleware's, derived from the authenticated session.
    app = request.get("app")
    if isinstance(app, str) and app:
        caller = app
    elif request.headers.get("X-Session-Key"):
        caller = "session"
    else:
        caller = "unknown"
    _audit_restart(caller, "denied", f"reason={code}{detail}")
    return web.json_response({"error": error, "code": code}, status=status)


def _audit_restart_answer(answer: web.Response, *, basis: str) -> None:
    """Record what the shared restart helper answered a verified host-owner CLI."""
    try:
        body = json.loads(answer.body) if isinstance(answer.body, bytes) else {}
    except ValueError:
        body = {}
    if answer.status == 200:
        status = "joined" if body.get("already_in_progress") else "started"
        _audit_restart("cli", "allowed", f"status={status} basis={basis}")
    else:
        _audit_restart("cli", "denied", f"reason={body.get('code') or answer.status}")


def _audit_restart(caller: str, outcome: str, resources: str) -> None:
    # Best-effort, as ``/api/shutdown`` audits: a non-critical record is a
    # non-blocking enqueue on the loop. Refusing the restart because a record could
    # not be written would not stop it, since the CLI answers a refusal with the
    # stop and spawn through ``/api/shutdown``; it would only cost the supervisor.
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=_RESTART_OPERATION,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # noqa: BLE001 - audit must not undo the decision
        logger.debug("SEL audit for %s failed", _RESTART_OPERATION, exc_info=True)


def _is_host_cli_caller(request: web.Request) -> bool:
    """Whether this caller has the shape of the CLI on this host.

    The secret proves the call came from inside, not who made it, so the
    middleware derives the caller from the session it declares. A caller that
    declares one (an app's script cron, an agent's tool call, a sub-agent) is
    acting for that session: it gets the counts alone, never the names of the
    sessions and jobs they are, and it may not restart the gateway. Only a
    caller with no session and no app claim passes: the shape of the CLI on
    this host, and the caller ``GET /api/crons`` already lists every job to.
    """
    app = request.get("app")
    if (isinstance(app, str) and app) or request.get("is_dashboard_user") is False:
        return False
    try:
        return not request.headers.get("X-Session-Key", "")
    except Exception:
        return False


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

    No body and ``{}`` both mean every flag ``False``; a flag must be a bool, and
    ``restart`` must be the only true one.
    """
    if not request.can_read_body:
        return _GateBody()
    try:
        body = await request.json()
    except ValueError:
        return None
    names = ("service_managed_only", "commit", "release", "restart")
    if not isinstance(body, dict) or set(body) - set(names):
        return None
    flags = [body.get(name, False) for name in names]
    if not all(isinstance(flag, bool) for flag in flags):
        return None
    if flags[3] and any(flags[:3]):
        return None
    return _GateBody(service_only=flags[0], commit=flags[1], release=flags[2], restart=flags[3])
