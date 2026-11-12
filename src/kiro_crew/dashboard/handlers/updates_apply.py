"""Arming an update and restarting the gateway.

``POST /api/update`` (it ARMS the update for the next start and installs nothing),
its cancel and simulate routes, and the restart ``POST /api/restart`` ends in. The
apply itself runs at the next start (:mod:`kiro_crew.update_boot_apply`).

Served through :mod:`kiro_crew.dashboard.handlers.updates`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from collections.abc import Callable
from typing import Any

from aiohttp import web

from kiro_crew import (
    platform_compat,
    shutdown_event,
    update_next_start,
    update_ownership,
)
from kiro_crew.cron import stop_cron_before_restart
from kiro_crew.dashboard import restart_resume
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.gateway_restart import resolve_restart_launcher
from kiro_crew.platform_compat import (
    exit_after_failed_restart_exec,
    reexec_launcher,
    reexec_python_module,
)
from kiro_crew.pre_exec_stops import run_pre_exec_stops
from kiro_crew.restart_session_close import close_sessions_for_restart
from kiro_crew.safety_override import flush_breadcrumb_writes
from kiro_crew.security import (  # noqa: F401 - pinned by test_updates_handlers_facade.py
    redact_credentials,
    redact_exfiltration_urls,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")


#: ``POST /api/update`` bodies: arm the update for the next start, or disarm it.
WHEN_NEXT_START = "next-start"
WHEN_CANCEL = "cancel"


async def _resolve_restart_target(
    resolver: Callable[[], str] | None = None,
) -> tuple[str | None, str | None, tuple[str, str] | None]:
    """Return ``(launcher, exe, refusal)``: what a restart would exec, or why it cannot.

    ``refusal`` is ``(code, sentence)``: a machine-readable code and the
    sentence the progress push and the 409 body carry.

    The refusals a restart makes BEFORE its drain, in one place, so
    ``POST /api/restart`` can answer them before its 200 (every restart
    control then shows them as a failure) and ``_restart_gateway`` still
    re-checks at the moment it commits.
    """
    # Resolve off-loop. An explicit broken launcher must not fall back to the
    # running bundle's interpreter and import path.
    try:
        launcher = await asyncio.to_thread(resolve_restart_launcher)
    except (ValueError, OSError) as exc:
        logger.warning("Gateway launcher unavailable: %s", exc)
        return (
            None,
            None,
            ("restart_launcher_invalid", "Cannot restart: invalid gateway launcher path"),
        )
    if launcher is not None:
        return launcher, None, None
    # Applying callers load this resolver before the install can replace
    # their import tree. A plain restart has no apply, so load it here.
    if resolver is None:
        from kiro_crew.platform.wheel_engine import respawn_executable

        resolver = respawn_executable
    exe = await asyncio.to_thread(resolver)
    # ONE hop for both syscalls, the contract this helper's docstring
    # states: the pathname can be a stalled mount, and this function is
    # on the event loop -- the await above proves it -- so a bare stat
    # here would hold every session while the restart decides.
    if not await asyncio.to_thread(platform_compat.execv_target_available, exe):
        # The interpreter this process ran under is gone, which is what an
        # apply that prunes the previous versioned tree leaves behind.
        # REFUSE HERE, while this process is still serving: the drain is the
        # point of no return, and main reached its exec only after
        # ``close_all()``, where the failure left the gateway alive with
        # admission shut and nothing able to reopen it. Refusing before the
        # drain keeps every session answerable and leaves the operator a
        # repair-then-relaunch they can actually perform. The pruned tree
        # took the bundle too, so a deferred restart into an applied update
        # does not hold the watchdog's exit.
        update_ownership.clear_restart_deferral()
        return (
            None,
            exe,
            ("restart_interpreter_missing", "Cannot restart: invalid Python executable path"),
        )
    return None, exe, None


@update_ownership.owning(update_ownership.Step.RESTART)
async def _restart_gateway(
    state: DashboardState, *, resolver: Callable[[], str] | None = None, force: bool = False
) -> bool:
    """Save state, close sessions, and exec the selected gateway entry point once.

    Takes the same pause, drain and fence the gateway's own scheduler takes
    (``restart_admission.admit_restart``) before it touches a session, and
    refuses while work is in flight: a refused restart returns ``False`` with
    admission reopened and the reason on the update progress stream. ``force``
    skips only that judgement, for a restart the person confirmed explicitly.

    Restart is a process-wide transition.  Two callers must never both drain
    sessions and race separate successors for the same listener/lock, so the
    claim is made synchronously before the first await.  A successful exec does
    not return; a refused or test-double exec releases the claim.  An exec the
    kernel refuses does not: by then the sessions are closed, so it exits the
    process rather than release a claim nothing can use.

    Owns a missing bundle while it runs (``update_ownership``): after an apply,
    or after an installer run from a terminal, the stale-asset watchdog must not
    shut the gateway down in the middle of this teardown.
    """
    if state._gateway_restart_in_progress:
        logger.info("Gateway restart already in progress; coalescing duplicate request")
        return False
    state._gateway_restart_in_progress = True
    admitted = False
    committed = False
    try:
        state.push_update_progress("restarting", "Restarting server…")
        launcher, exe, refusal = await _resolve_restart_target(resolver)
        if refusal is not None:
            state.push_update_progress("error", refusal[1])
            return False
        # Unwired only where no orchestrator runs (a bare dashboard state), which
        # has no channel work to cut.
        admit = getattr(state, "_restart_admission", None)
        if callable(admit):
            admission = admit(force=force)
            if inspect.isawaitable(admission):
                admission = await admission
            # Only a wired gate answering ``admitted is False`` refuses: a state
            # stand-in's auto-attribute is not an answer.
            if getattr(admission, "admitted", None) is False:
                logger.info("Gateway restart refused: %s", admission.reason)
                state.push_update_progress("error", admission.detail())
                return False
            admitted = getattr(admission, "admitted", None) is True
        # circular import: kiro_crew.dashboard.chat imports from
        # kiro_crew.dashboard.handlers (which re-exports the updates facade), so this
        # must stay inline to avoid an import cycle at module load.
        from kiro_crew.dashboard.chat import save_all_slots_to_history
        from kiro_crew.dashboard.chat_persistence import wait_for_unwinding_turns
        from kiro_crew.executors import subprocess_executor

        loop = asyncio.get_running_loop()
        started = loop.time()

        def step(name: str) -> None:
            # Logged as each step starts, so a restart that stalls names its step.
            logger.info("Gateway restart: %s (+%.1fs)", name, loop.time() - started)

        step("saving chat history")
        try:
            # Offload the synchronous per-slot save (per-session lock + disk I/O)
            # to the bounded subprocess_executor with a deadline: on the event loop
            # a contended session raises HistoryLockTimeout and a wedged disk would
            # block the restart, so a slot's final save must run off-loop and be
            # time-bounded rather than stall (or silently drop) here.
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), save_all_slots_to_history, state
                ),
                timeout=5.0,
            )
        except Exception:
            logger.debug("History save before restart failed", exc_info=True)
        # Past the last refusal: the restart commits here, so a deferred restart
        # into an applied update is over. Not at entry: a restart that coalesces
        # or finds a broken launcher above leaves the deferral it found in place.
        update_ownership.restart_committed()
        # The exec skips the gateway's _shutdown, where the cron service stops:
        # stop it here, before close_all() as _shutdown orders it, so a run in
        # flight is filed cancelled, its marker cleared and its owner told.
        await stop_cron_before_restart(getattr(state, "crons", None))
        committed = True
        # A planned restart: the turns close_all() cuts resume on the next boot.
        # Imported at module load, before an apply can remove this tree; that
        # also preloads it for the gateway's update restart.
        step("recording running chats")
        await restart_resume.record_running_slots(state)
        # Bounded, and the bound holds against a task that outlives its cancel:
        # close_all shuts turn admission first, and only the exec below ends
        # this image.
        step("closing sessions")
        await close_sessions_for_restart(state.sessions.close_all())
        # close_all() cut every turn that was mid-reply, and each one saves the text
        # its user watched stream as it unwinds, after the save above. The exec
        # below would beat that save.
        step("waiting for cut turns")
        await wait_for_unwinding_turns(state)
        # The broker this gateway spawned dies with it, the same as on a clean
        # shutdown; an adopted daemon belongs to its own owner and is left alone,
        # as ``GatewayManager.shutdown`` already does. The exec below does not run
        # that shutdown, and the successor can only replace a survivor whose owner
        # pid is its own (an exec that kept the pid) or gone. Through a launcher
        # that runs the new gateway as a child, this pid lives on as its
        # supervisor: the daemon's owner-liveness check keeps passing, and the
        # successor refuses a broker "owned by another live gateway" for its whole
        # lifetime. Sessions are closed, so nothing is mid-call. The stop is
        # bounded: the daemon's own drain budget on SIGTERM, then a SIGKILL and
        # a reap of its pooled backends if the drain does not finish.
        # Wired by the orchestrator after dashboard init; absent means no broker.
        stop_broker = getattr(state, "_mcp_gateway_stop", None)
        if stop_broker is not None:
            step("stopping the MCP broker")
            try:
                await stop_broker()
            except Exception:
                # Past the point of no return: a broker that will not stop must
                # not strand a gateway whose sessions are already closed.
                logger.debug("MCP broker stop before restart failed", exc_info=True)
        # The exec skips the cleanup hooks that stop supervised children too, and
        # a child whose parent pid survives the exec is never reclaimed. Each
        # such owner registers its stop with the pre-exec seam.
        step("running pre-exec stops")
        await run_pre_exec_stops()
        sys.stdout.flush()
        sys.stderr.flush()
        # The safety-override record publishes on a worker thread (its callers sit
        # on the event loop), and os.execv replaces this process image without
        # draining that worker -- so a grant activated moments before a restart
        # would lose the very notice this restart is what makes necessary (found
        # in review). Offloaded so a stalled write cannot block the loop, and
        # bounded, so a restart is never held up by it.
        step("flushing safety-override records")
        try:
            await asyncio.to_thread(flush_breadcrumb_writes, 2.0)
        except Exception:
            logger.debug("Breadcrumb flush before restart failed", exc_info=True)
        await asyncio.sleep(0.5)
        step("exec")
        # Same point of no return as the orchestrator path: the refusal above
        # removed the reachable failures, but only the kernel can refuse the
        # image itself, and the sessions closed above do not come back. Exit on
        # that rather than fall through to ``return True``, which reports a
        # restart that did not happen from a process that cannot serve.
        # The exec skips the orderly exit that marks the crash dump clean, so it
        # is marked here, with nothing left between the mark and the exec.
        from kiro_crew.dashboard import crash_dump_store

        await crash_dump_store.mark_clean_exit()
        try:
            if launcher is not None:
                reexec_launcher(launcher, sys.argv[1:])
            else:
                reexec_python_module("kiro_crew", sys.argv[1:], executable=exe)
        except OSError:
            await crash_dump_store.unmark_clean_exit()
            await exit_after_failed_restart_exec(launcher or exe)
        return True
    finally:
        if admitted and not committed:
            # Refused or cancelled before the point of no return: the gateway
            # keeps serving, so the pause the admission took must not outlive it.
            release = getattr(state, "_restart_release", None)
            if callable(release):
                try:
                    await release()
                except Exception:
                    logger.warning(
                        "Could not reopen turn admission after a stopped restart", exc_info=True
                    )
        state._gateway_restart_in_progress = False


#: How often a client that was told "deferred" asks again.
_DEFERRED_RETRY_SECS = 30


async def _read_force_flag(request: web.Request, *, default: bool) -> bool | web.Response:
    """The request's ``{"force": <bool>}`` confirmation, or a 400 for any other body.

    Absent body and ``{}`` mean *default*. Only a JSON boolean counts: a string or
    a number is a malformed confirmation, not consent to interrupt running work.
    """
    if getattr(request, "can_read_body", False) is not True:
        return default
    try:
        body = await request.json()
    except ValueError:
        body = None
    if (
        not isinstance(body, dict)
        or set(body) - {"force"}
        or not isinstance(body.get("force", default), bool)
    ):
        return web.json_response(
            {"error": "invalid request body", "code": "invalid_body"}, status=400
        )
    return body.get("force", default) is True


def _quiescence_of(state: DashboardState, *, quiet_window_secs: float) -> Any:
    """The shared quiescence census, or ``None`` where no orchestrator answers."""
    probe = getattr(state, "_quiescence_probe", None)
    if not callable(probe):
        return None
    try:
        return probe(quiet_window_secs=quiet_window_secs)
    except Exception:
        # A census that cannot be read must not look like an idle gateway.
        logger.warning("Quiescence probe failed", exc_info=True)
        return None


async def _read_update_when(request: web.Request) -> str | web.Response:
    """The request's ``{"when": "next-start" | "cancel"}``, or a 400 for any other body.

    No body, ``{}`` and a body without ``when`` all mean ``next-start``. A legacy
    ``force`` key is accepted and ignored: nothing the update does depends on it.
    """
    if getattr(request, "can_read_body", False) is not True:
        return WHEN_NEXT_START
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "invalid request body", "code": "invalid_body"}, status=400
        )
    when = body.get("when", WHEN_NEXT_START)
    if when not in (WHEN_NEXT_START, WHEN_CANCEL):
        return web.json_response(
            {"error": "invalid request body", "code": "invalid_body"}, status=400
        )
    return str(when)


async def api_update_apply(request: web.Request) -> web.Response:
    """POST /api/update: ARM the update for the next start; install nothing now.

    ``{"when": "next-start"}`` (also the default) records the update the person
    asked for and answers ``202 {"armed": true, "version": ...}``. The apply runs
    at the next start, before the gateway serves or restores a session
    (:mod:`kiro_crew.update_boot_apply`); this never runs ``apply_command``, an
    installer or a git update, and never restarts the gateway, whatever is
    running. ``{"when": "cancel"}`` disarms it. A legacy ``force`` is ignored.
    """
    owner_denied = await require_owner_dashboard_request(request, "update.apply")
    if owner_denied is not None:
        return owner_denied
    state: DashboardState = request.app["state"]

    if shutdown_event.is_set():
        return web.json_response(
            {"error": "The gateway is shutting down", "code": "shutting_down"}, status=503
        )
    when = await _read_update_when(request)
    if isinstance(when, web.Response):
        return when
    if when == WHEN_CANCEL:
        removed = await asyncio.to_thread(update_next_start.disarm)
        state.push_refresh("update_available")
        return web.json_response({"ok": True, "armed": False, "disarmed": removed})

    from kiro_crew.dashboard.handlers.updates_verdict import _update_info
    from kiro_crew.platform import update_provider
    from kiro_crew.platform.update_capability import (
        CHECK_SUCCEEDED,
        MANAGED_BY_ELECTRON,
        MANAGED_BY_GIT,
        derive_capability,
    )

    # A managed venv, wheel or pipx install is approved through the host-local
    # step-up (nonce, loopback, audit), and an arm here would let the boot apply
    # install without any of it. Only a git checkout, a policy-defined provider and
    # the desktop app (whose own updater owns the apply) arm from this route.
    if update_provider.resolve_provider() is None:
        capability = await asyncio.to_thread(derive_capability)
        if capability.managed_by not in (MANAGED_BY_GIT, MANAGED_BY_ELECTRON):
            return web.json_response(
                {
                    "error": "Not a git checkout: update this install with "
                    "`kirocrew update`, then restart the gateway",
                    "code": "not_git_checkout",
                },
                status=409,
            )

    if _update_info.get("check_status") == CHECK_SUCCEEDED and not _update_info.get(
        "update_available"
    ):
        return web.json_response(
            {"error": "There is no newer version to install", "code": "no_update"}, status=409
        )
    version = str(_update_info.get("latest_version") or "")
    behind = _update_info.get("commits_behind")
    armed = await asyncio.to_thread(
        lambda: update_next_start.arm(version, behind=behind if isinstance(behind, int) else 0)
    )
    state.push_refresh("update_available")
    return web.json_response(
        {"ok": True, "armed": True, "version": armed.armed_version}, status=202
    )


async def api_update_cancel(request: web.Request) -> web.Response:
    """POST /api/update/cancel — dismiss a stuck/failed update overlay."""
    state: DashboardState = request.app["state"]
    state.clear_update_progress()
    state.push_update_progress("failed", "Update cancelled by user")
    # Give clients a moment to receive the failed event, then clear
    await asyncio.sleep(0.2)
    state.clear_update_progress()
    return web.json_response({"ok": True})


async def api_update_simulate(request: web.Request) -> web.Response:
    """POST /api/update/simulate — walk through update steps with delays.

    For local testing only. Cycles through each progress step with a
    configurable delay (default 2s per step).
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    from kiro_crew.dashboard.handlers._shared import body_not_object_error

    if (not_object := body_not_object_error(body)) is not None:
        return not_object

    # Simulate a pre-flight rejection (e.g. dirty working tree)
    if body.get("reject"):
        msg = body.get(
            "reject_message", "Working tree has uncommitted changes — commit or stash first"
        )
        return web.json_response({"error": msg}, status=409)

    delay = body.get("delay", 2)
    fail_at = body.get("fail_at", "")  # optional: step name to fail at

    async def _sim() -> None:
        steps = [
            ("pulling", "Pulling latest changes…"),
            ("building", "Installing package (pip)…"),
            ("building", "Building frontend (npm)…"),
            ("restarting", "Restarting server…"),
        ]
        for step, detail in steps:
            if fail_at and step == fail_at:
                state.push_update_progress("failed", f"Simulated failure at {step}")
                return
            state.push_update_progress(step, detail)
            await asyncio.sleep(delay)
        # Simulate completion — broadcast "done" so frontend clears the overlay
        state.push_update_progress("done", "Update complete")
        state.clear_update_progress()

    task = asyncio.create_task(_sim())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "status": "simulating"})


async def api_gateway_restart(request: web.Request) -> web.Response:
    """POST /api/restart — restart the gateway process without updating anything.

    The missing half of the non-desktop update flow. A wheel install cannot
    replace its own code, so the panel hands the user an installer command to
    run in a terminal; once they have, the gateway is still executing the OLD
    code with no in-app way to pick up the new one. Short of this endpoint the
    only route was killing the process by hand.

    Deliberately NOT part of ``POST /api/update``: that endpoint pulls, rebuilds
    and reinstalls before restarting, and it refuses every layout that is not a
    git checkout. Restart has no such precondition — it is valid on every
    layout, including a desktop bundle's embedded gateway.
    """
    owner_denied = await require_owner_dashboard_request(request, "update.restart")
    if owner_denied is not None:
        return owner_denied
    state: DashboardState = request.app["state"]

    # The click is a person's own act, so by default it restarts under running
    # work, after the pause, drain and fence. `{"force": false}` asks instead for
    # a refusal while work is in flight, before the 200: after it, a refusal
    # reaches the user only as a progress step.
    force = await _read_force_flag(request, default=True)
    if isinstance(force, web.Response):
        return force
    if not force:
        census = _quiescence_of(state, quiet_window_secs=0.0)
        if census is not None and isinstance(census.busy, int) and census.busy > 0:
            counts = census.payload()
            logger.info("Restart refused while work is in flight: %s", counts)
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "Running work would be interrupted by a restart. "
                        "Restart again once it has finished, or confirm to restart anyway."
                    ),
                    "code": "work_in_flight",
                    "turns": counts["turns"],
                    "background": counts["background"],
                    "queued_messages": counts["queued_messages"],
                    "pending_approvals": counts["pending_approvals"],
                    "busy": counts["busy"],
                    "quiet_remaining_secs": counts["quiet_remaining_secs"],
                    "settle_remaining_secs": counts["settle_remaining_secs"],
                    "force_available": True,
                },
                status=409,
            )

    # Refuse a restart that cannot run BEFORE the 200, while the client still
    # holds the request: after it, a refusal reaches the user only as a
    # progress step, which a restart control waiting for the reload never
    # reads. ``_restart_gateway`` re-checks when it commits. Before the latch
    # below, so no await separates its check from the task it records.
    _launcher, _exe, refusal = await _resolve_restart_target()
    if refusal is not None:
        code, sentence = refusal
        return web.json_response({"ok": False, "error": sentence, "code": code}, status=409)

    # The admission refusals that need no side effect (the gateway is shutting
    # down, another update step holds turn admission) also answer before the 200.
    # A refusal after the pause, drain or fence still reaches the user only as a
    # progress step.
    preflight = getattr(state, "_restart_preflight", None)
    if callable(preflight):
        early = preflight()
        if getattr(early, "admitted", None) is False:
            return web.json_response(
                {"ok": False, "error": early.detail(), "code": early.reason}, status=409
            )

    # Coalesce repeat clicks/requests BEFORE the response-flush sleep below.
    # Without this latch, every POST creates a restart task and they all reach
    # session drain together.  _restart_gateway has its own process-wide claim
    # for callers from other routes; this task-level latch also avoids needless
    # duplicate work on this public endpoint.
    existing = state._gateway_restart_task
    if existing is not None and not existing.done():
        return web.json_response({"ok": True, "status": "restarting", "already_in_progress": True})

    # Reply BEFORE restarting. os.execv replaces the process image, so a restart
    # kicked off inline would tear down the connection mid-response and the
    # client could not distinguish "restarting" from "the request failed".
    async def _restart() -> None:
        # Let the response flush before the process image is replaced.
        await asyncio.sleep(0.25)
        try:
            await _restart_gateway(state, force=force)
        except Exception:
            logger.exception("Gateway restart failed")
            state.push_update_progress("failed", "Restart failed — check logs")

    task = asyncio.create_task(_restart())
    state._gateway_restart_task = task
    state._background_tasks.add(task)

    def _restart_done(done: asyncio.Task[None]) -> None:
        state._background_tasks.discard(done)
        if state._gateway_restart_task is done:
            state._gateway_restart_task = None

    task.add_done_callback(_restart_done)
    return web.json_response({"ok": True, "status": "restarting"})
