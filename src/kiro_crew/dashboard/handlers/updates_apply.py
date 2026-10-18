"""Applying an update and restarting the gateway.

``POST /api/update`` (the git fast-forward, the policy installer, the frontend
rebuild and the venv sync), its cancel and simulate routes, the tracked policy
installs the gateway's shutdown waits for, and the restart both an update and
``POST /api/restart`` end in.

Served through :mod:`kiro_crew.dashboard.handlers.updates`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import dep_sync, platform_compat, shutdown_event, update_ownership
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.gateway_restart import resolve_restart_launcher
from kiro_crew.git_divergence import UNREADABLE_TIMEOUT, DivergenceUnreadable, count_divergence
from kiro_crew.platform.update_capability import MANAGED_BY_GIT, derive_capability
from kiro_crew.platform.update_governance import resolve_remote_url, update_blocked_reason
from kiro_crew.platform_compat import (
    exit_after_failed_restart_exec,
    reexec_launcher,
    reexec_python_module,
)
from kiro_crew.pre_exec_stops import run_pre_exec_stops
from kiro_crew.safety_override import flush_breadcrumb_writes
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")


#: Policy applies started from the dashboard (``POST /api/update`` on a host
#: whose policy names an ``apply_command``) that are still running. Each runs as
#: its own task rather than inline in the request handler: aiohttp cancels
#: handlers only after its own shutdown timeout, which the gateway's graceful cap
#: exits ahead of, so an inline apply would never receive the cancel that stops
#: its installer with SIGTERM. The gateway's shutdown reads this set through
#: :func:`policy_applies_in_flight` and cancels + awaits them alongside the
#: update coordinator, within its installer-stop budget.
_policy_apply_tasks: set["asyncio.Task[bool | None]"] = set()


def policy_applies_in_flight() -> list["asyncio.Task[bool | None]"]:
    """The dashboard-started policy applies still running, for the shutdown path."""
    return [task for task in _policy_apply_tasks if not task.done()]


class _PolicyApplyStopped(Exception):
    """The gateway's shutdown cancelled a dashboard-started policy apply."""


async def _run_tracked_policy_apply(
    apply: Callable[[], Coroutine[Any, Any, bool | None]],
) -> bool | None:
    """Run ``apply`` as a tracked task the gateway's shutdown can stop.

    A caller that stops waiting (the browser closed the request) still cancels the
    apply, as an inline await would. A cancel that reaches the apply from the
    shutdown path, with this caller still waiting, is reported as
    :class:`_PolicyApplyStopped` so the handler can answer it rather than die.
    """
    task = asyncio.create_task(apply(), name="dashboard-policy-apply")
    _policy_apply_tasks.add(task)
    task.add_done_callback(_policy_apply_tasks.discard)
    try:
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if task.cancelled() and current is not None and not current.cancelling():
            raise _PolicyApplyStopped from None
        raise


async def _build_frontend(proj: str, state: DashboardState) -> None:
    """Build the in-tree ``website/`` frontend and stage it into ``static/dist``.

    Delegates to the shared :func:`kiro_crew.frontend.build_frontend_async`
    helper so the build/stage logic is not duplicated across the three update
    paths (CLI ``kirocrew update``, this dashboard endpoint, and the gateway
    auto-update). That helper runs ``npm ci`` (fallback ``npm install``) then
    ``npm run build`` in ``<proj>/website`` and copies ``website/dist`` into
    the served ``src/kiro_crew/static/dist`` — without which the dashboard
    would serve a stale bundle after an update.

    Graceful no-op when there is no ``website/`` directory or ``npm`` is not
    installed (a packaged checkout may ship prebuilt assets). Build warnings
    are surfaced via ``state.push_update_progress`` after credential/URL
    redaction.
    """
    from kiro_crew import frontend

    def _push(step: str, msg: str) -> None:
        msg, _ = redact_credentials(msg)
        msg, _ = redact_exfiltration_urls(msg)
        # frontend emits ("warning", detail); show it as a non-fatal build note.
        state.push_update_progress("building", msg)

    state.push_update_progress("building", "Building frontend (npm)…")
    await frontend.build_frontend_async(proj, push_progress=_push)


async def _venv_pip_install(proj: str, state: DashboardState) -> bool:
    """Install the pulled revision into this gateway's own venv.

    Returns ``True`` on success. On failure, pushes an error to ``state`` and
    returns ``False`` — caller should ``return``.

    Delegates the choice of HOW to :func:`kiro_crew.dep_sync.sync_or_reinstall`:
    an editable reinstall where it can run, and a dependency-only sync where it
    cannot. This endpoint is one of the paths that cannot always run it — the
    gateway it updates is normally started through the very console script pip
    would have to rewrite, which Windows locks, and a reinstall that dies there
    has already deleted the editable ``.pth`` and left the venv unable to import
    the package at all.

    ``dep_sync`` is imported at MODULE level (see its docstring): this runs after
    the pull, so an import deferred to here would parse the file the incoming
    revision shipped.
    """
    state.push_update_progress("building", "Installing package (pip)…")
    loop = asyncio.get_running_loop()

    def _publish(step: str, message: str) -> None:
        # dep_sync hands pip's output over raw; this is the surface that publishes
        # it, so redaction and the length cap belong here.
        message, _ = redact_credentials(message)
        message, _ = redact_exfiltration_urls(message)
        if len(message) > 1000:
            message = message[:1000] + "\n…(truncated)"
        state.push_update_progress(step, message)

    def _emit(message: str, error: bool) -> None:
        # Called from the EXECUTOR THREAD, and push_update_progress touches
        # loop-bound state (the SSE subscriber queues), so it is handed to the
        # serving loop rather than run here. Doing it inline is the kind of
        # cross-thread mutation that works until a client is actually connected
        # and then raises out of a worker thread. Same pattern as the log ring
        # handler in ``updates_streams``, which emits from arbitrary logger threads.
        loop.call_soon_threadsafe(_publish, "error" if error else "building", message)

    # Supervised: cancelling this coroutine (a gateway shutdown does) also cancels
    # the install, so one still queued, or cancelled before its last check ahead
    # of pip, never starts pip. A pip already running is left to finish (see
    # dep_sync.sync_or_reinstall_supervised for the exact window).
    rc = await dep_sync.sync_or_reinstall_supervised(
        subprocess_executor(),
        Path(proj),
        Path(sys.executable),
        _emit,
        # 600s rather than 120s on `pip install -e .`: the bound covers a
        # dependency install that may be resolving and downloading a set this
        # venv has never seen — and building a wheel for one of them — where
        # 120s is a routine, not an exceptional, overrun.
        # It is a real bound either way: dep_sync kills the pip child on
        # expiry rather than letting the step hang on a wedged index.
        timeout=600,
    )
    return rc == 0


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
    state: DashboardState, *, resolver: Callable[[], str] | None = None
) -> bool:
    """Save state, close sessions, and exec the selected gateway entry point once.

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
    try:
        state.push_update_progress("restarting", "Restarting server…")
        launcher, exe, refusal = await _resolve_restart_target(resolver)
        if refusal is not None:
            state.push_update_progress("error", refusal[1])
            return False
        # circular import: kiro_crew.dashboard.chat imports from
        # kiro_crew.dashboard.handlers (which re-exports the updates facade), so this
        # must stay inline to avoid an import cycle at module load.
        from kiro_crew.dashboard.chat import save_all_slots_to_history
        from kiro_crew.dashboard.chat_persistence import wait_for_unwinding_turns
        from kiro_crew.executors import subprocess_executor

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
        try:
            await state.sessions.close_all()
        except Exception:
            logger.debug("Session cleanup before restart failed", exc_info=True)
        # close_all() cut every turn that was mid-reply, and each one saves the text
        # its user watched stream as it unwinds, after the save above. The exec
        # below would beat that save.
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
            try:
                await stop_broker()
            except Exception:
                # Past the point of no return: a broker that will not stop must
                # not strand a gateway whose sessions are already closed.
                logger.debug("MCP broker stop before restart failed", exc_info=True)
        # The exec skips the cleanup hooks that stop supervised children too, and
        # a child whose parent pid survives the exec is never reclaimed. Each
        # such owner registers its stop with the pre-exec seam.
        await run_pre_exec_stops()
        sys.stdout.flush()
        sys.stderr.flush()
        # The safety-override record publishes on a worker thread (its callers sit
        # on the event loop), and os.execv replaces this process image without
        # draining that worker -- so a grant activated moments before a restart
        # would lose the very notice this restart is what makes necessary (found
        # in review). Offloaded so a stalled write cannot block the loop, and
        # bounded, so a restart is never held up by it.
        try:
            await asyncio.to_thread(flush_breadcrumb_writes, 2.0)
        except Exception:
            logger.debug("Breadcrumb flush before restart failed", exc_info=True)
        await asyncio.sleep(0.5)
        # Same point of no return as the orchestrator path: the refusal above
        # removed the reachable failures, but only the kernel can refuse the
        # image itself, and the sessions closed above do not come back. Exit on
        # that rather than fall through to ``return True``, which reports a
        # restart that did not happen from a process that cannot serve.
        try:
            if launcher is not None:
                reexec_launcher(launcher, sys.argv[1:])
            else:
                reexec_python_module("kiro_crew", sys.argv[1:], executable=exe)
        except OSError:
            await exit_after_failed_restart_exec(launcher or exe)
        return True
    finally:
        state._gateway_restart_in_progress = False


async def _snapshot_memory_or_refuse(state: DashboardState) -> web.Response | None:
    """Copy every memory store, or refuse a failed copy or gateway shutdown.

    The same copy-or-refuse step ``kirocrew update`` takes before each updater
    (``snapshot_memory_before_update``), called just before this handler rewrites
    the install. ``None`` means the copy landed and the apply may go on.
    """
    from kiro_crew.platform.wheel_apply import snapshot_memory_before_update, snapshot_refusal

    copied, failure = await asyncio.to_thread(snapshot_memory_before_update)
    if not failure:
        logger.info("Pre-update memory snapshot: %d store(s) copied", copied)
        if shutdown_event.is_set():
            return web.json_response(
                {"error": "The gateway is shutting down", "code": "shutting_down"}, status=503
            )
        return None
    logger.warning("Update refused: pre-update memory snapshot failed: %s", failure)
    message = snapshot_refusal(failure)
    state.push_update_progress("failed", message)
    return web.json_response({"error": message, "code": "memory_snapshot_failed"}, status=500)


async def api_update_apply(request: web.Request) -> web.Response:
    """POST /api/update — git pull, rebuild, restart gateway."""
    owner_denied = await require_owner_dashboard_request(request, "update.apply")
    if owner_denied is not None:
        return owner_denied
    state: DashboardState = request.app["state"]

    if shutdown_event.is_set():
        # An apply started now would be cut off by the shutdown mid-write.
        return web.json_response(
            {"error": "The gateway is shutting down", "code": "shutting_down"}, status=503
        )

    # A policy-defined provider OWNS the update on this host. Checked before the
    # git precondition below so an authenticated operator clicking Update cannot
    # run the built-in mechanism their own policy excluded. A dashboard token
    # proves who the caller is, not that this host may update by git.
    from kiro_crew.platform.update_provider import apply_policy_update, resolve_provider
    from kiro_crew.platform.wheel_engine import respawn_executable

    # Same gate as `kirocrew update`: a provider that is not configured to apply
    # (check-only, or on Windows) rewrites nothing, so it spends no copy.
    _provider = resolve_provider()
    if _provider is not None and _provider.can_apply():
        refused = await _snapshot_memory_or_refuse(state)
        if refused is not None:
            return refused

    # As a tracked task, not inline: the gateway's shutdown cancels it first and
    # waits for its installer to stop (``policy_applies_in_flight``). Inline, the
    # handler is cancelled only by aiohttp's own teardown, which the graceful
    # cap exits ahead of, and the installer would be left running unsupervised.
    try:
        applied = await _run_tracked_policy_apply(apply_policy_update)
    except _PolicyApplyStopped:
        state.push_update_progress("failed", "Update stopped: the gateway is shutting down")
        return web.json_response(
            {"error": "The gateway is shutting down", "code": "shutting_down"}, status=503
        )
    if applied is not None:
        if not applied:
            # A configured provider's failure is a failure. Falling through to
            # the git path would be the bypass the policy exists to prevent.
            state.push_update_progress("failed", "Policy-defined update command failed — see logs")
            return web.json_response(
                {
                    "error": "policy-defined update command failed",
                    "code": "policy_update_failed",
                    "governance": True,
                },
                status=500,
            )
        await _restart_gateway(state, resolver=respawn_executable)
        return web.json_response({"ok": True, "status": "updating"})

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        return web.json_response({"error": "KIROCREW_PROJECT_DIR not set"}, status=400)
    # Same derivation the check path uses, so this endpoint and the button the
    # dashboard renders from ``can_apply`` cannot disagree. Offloaded: it shells
    # out to git.
    capability = await asyncio.get_running_loop().run_in_executor(
        None, lambda: derive_capability(install_root=proj)
    )
    if capability.managed_by != MANAGED_BY_GIT:
        return web.json_response(
            {
                "error": "Not a git checkout — update this install with `kirocrew update`, then restart the gateway"
            },
            status=409,
        )

    # Source pin, checked before any state is pushed so a blocked update leaves
    # no "updating" spinner behind. A dashboard token proves the caller is the
    # operator, not that the fleet permits this host to pull from this remote.
    # Offloaded: the seam shells out to git.
    blocked = await asyncio.get_running_loop().run_in_executor(
        None, lambda: update_blocked_reason(resolve_remote_url(proj))
    )
    if blocked:
        logger.warning("Update refused: %s", blocked)
        return web.json_response({"error": blocked, "governance": True}, status=403)

    # Signal updating state via SSE
    state.push_refresh("updating")

    # Check for dirty working tree before updating
    dirty = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain",
        cwd=proj,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        dirty_out, _ = await asyncio.wait_for(dirty.communicate(), timeout=10)
    except asyncio.CancelledError:
        await platform_compat.kill_and_reap(dirty)
        raise
    except asyncio.TimeoutError:
        await platform_compat.kill_and_reap(dirty)
        return web.json_response(
            {"error": "Timed out checking working tree status"},
            status=500,
        )
    if dirty_out and dirty_out.strip():
        logger.warning("Update skipped: working tree has uncommitted changes")
        # ``code`` lets the UI show guided copy; ``changed`` is a count only, so
        # no local paths leave the gateway. ``error`` keeps its wording for
        # clients that predate the code.
        changed = sum(1 for line in dirty_out.splitlines() if line.strip())
        return web.json_response(
            {
                "error": "Working tree has uncommitted changes — commit or stash first",
                "code": "dirty_worktree",
                "changed": changed,
            },
            status=409,
        )

    # Diverged precondition, enforced at the layer that owns the destructive
    # action. The dashboard's own render-site guards can only cover the clients
    # that ran a fresh check; a stale client (an Update button armed before the
    # checkout gained local commits, a cached-changelog modal whose pre-apply
    # check never re-ran) still POSTs here. Fetch FIRST, fail closed: the
    # counts below read the remote-tracking refs, and refs from an old fetch
    # can report behind=0 for a checkout whose remote has since moved — which
    # would wave through the very state this guard exists to refuse.
    fetch = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        "--quiet",
        cwd=proj,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        await asyncio.wait_for(fetch.communicate(), timeout=30)
    except asyncio.CancelledError:
        await platform_compat.kill_and_reap(fetch)
        raise
    except asyncio.TimeoutError:
        await platform_compat.kill_and_reap(fetch)
        return web.json_response(
            {
                "error": "Timed out refreshing the remote before updating",
                "code": "git_fetch_failed",
            },
            status=500,
        )
    if fetch.returncode != 0:
        logger.warning("Update refused: pre-apply git fetch failed (rc=%s)", fetch.returncode)
        return web.json_response(
            {
                "error": "Could not reach the remote to verify the update",
                "code": "git_fetch_failed",
            },
            status=409,
        )
    counts = await count_divergence(proj, "@{u}")
    if isinstance(counts, DivergenceUnreadable):
        if counts.reason == UNREADABLE_TIMEOUT:
            return web.json_response(
                {"error": "Timed out checking upstream distance", "code": "git_read_failed"},
                status=500,
            )
        # A failed or unparseable count alike: the guard cannot read the
        # distance, so it refuses rather than waving the pull through.
        logger.warning("Update refused: could not count commits against upstream in %s", proj)
        return web.json_response(
            {
                "error": "Could not compare against upstream — check the tracked remote",
                "code": "git_read_failed",
            },
            status=409,
        )
    ahead, behind = counts.ahead, counts.behind
    if ahead > 0 and behind > 0:
        logger.warning(
            "Update refused: checkout diverged from upstream (%d ahead, %d behind)", ahead, behind
        )
        return web.json_response(
            {
                "error": "Checkout has diverged from its upstream — rebase or merge in a terminal",
                "code": "checkout_diverged",
                "commits_ahead": ahead,
                "commits_behind": behind,
            },
            status=409,
        )

    # Pin the revision this update will apply. The guard above fetched and
    # measured against `@{u}`, but a ref name is re-resolved by every later git
    # command, so a remote that moves between here and the apply would let the
    # floor check below judge one commit and the fast-forward land on another.
    # An OID cannot move: it is what gets floor-checked AND what gets applied.
    target_proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--verify",
        "@{u}^{commit}",
        cwd=proj,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        target_out, _ = await asyncio.wait_for(target_proc.communicate(), timeout=10)
    except asyncio.CancelledError:
        await platform_compat.kill_and_reap(target_proc)
        raise
    except asyncio.TimeoutError:
        await platform_compat.kill_and_reap(target_proc)
        return web.json_response(
            {"error": "Timed out resolving the upstream revision", "code": "git_read_failed"},
            status=500,
        )
    target = (target_out or b"").strip().decode()
    if target_proc.returncode != 0 or not target:
        logger.warning("Update refused: could not resolve the tracked upstream in %s", proj)
        return web.json_response(
            {
                "error": "Could not resolve the tracked upstream revision — check the tracked remote",
                "code": "git_read_failed",
            },
            status=409,
        )

    # Interpreter floor, checked against the pinned revision before the tree
    # moves to it. pip enforces the same floor during the reinstall, but by then
    # the checkout has already advanced to code this venv can never import: the
    # gateway keeps serving the old revision from memory while every lazy import
    # reads the new files, and each later click repeats the pull and the
    # refusal. Refusing here leaves the checkout where it was and names the
    # remedy. Offloaded: it shells out to git and probes the interpreter.
    try:
        floor_breach = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            lambda: dep_sync.incoming_python_floor_breach(Path(proj), target, Path(sys.executable)),
        )
    except dep_sync.IncomingFloorUnreadable as exc:
        # The pinned revision is real, but its floor could not be read. That is
        # not "no floor": waving it through would land exactly the revision
        # the gate exists to keep out, on the one path git happened to fail on.
        logger.warning("Update refused: could not read the incoming interpreter floor (%s)", exc)
        return web.json_response(
            {
                "error": "Could not read the incoming revision's interpreter requirement — "
                "check the tracked remote",
                "code": "git_read_failed",
            },
            status=409,
        )
    if floor_breach:
        logger.warning("Update refused: %s", floor_breach)
        return web.json_response(
            {"error": f"Update refused: {floor_breach}", "code": "python_floor"},
            status=409,
        )

    # Last check before the fast-forward rewrites the install: every refusal
    # above leaves the tree alone, so none of them spends a copy.
    refused = await _snapshot_memory_or_refuse(state)
    if refused is not None:
        return refused

    async def _apply() -> None:
        # Owns a missing bundle from the merge until the restart takes over.
        owned = contextlib.ExitStack()
        owned.enter_context(update_ownership.step(update_ownership.Step.DASHBOARD_UPDATE))
        # Set once the merge may have moved the tree, cleared once its
        # dependencies are installed: in between, a relaunch could die at import.
        tree_moved = False
        try:
            state.push_update_progress("pulling", "Pulling latest changes…")
            # Fast-forward to the PINNED commit, not `git pull`: a pull refetches
            # and re-resolves the upstream, so a remote that moved after the
            # floor check would land a revision nobody checked. --ff-only keeps
            # the non-fast-forward classes unreachable at the action primitive
            # itself, not just at the precondition above, so an upstream that
            # was rewritten in the window fails the merge instead of minting an
            # unrequested merge commit into the user's branch.
            pull = await asyncio.create_subprocess_exec(
                "git",
                "merge",
                "--ff-only",
                target,
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            # Only a merge that started can have moved the tree: a spawn that
            # raised wrote nothing.
            tree_moved = True
            # No cancel arm here, unlike the read-only git calls: a cancelled
            # apply does not kill git merge --ff-only, because a SIGKILL in
            # mid-checkout can leave .git/index.lock and a half-moved tree. The
            # cancel also ends the wait that carries its 60 s timeout, so nothing
            # stops the merge after that; a bounded TERM-first stop would be a
            # separate change.
            try:
                await asyncio.wait_for(pull.communicate(), timeout=60)
            except asyncio.TimeoutError:
                await platform_compat.kill_and_reap(pull)
                state.push_update_progress(
                    "error", f"Fast-forward to {target[:12]} timed out (git merge --ff-only)"
                )
                return
            if pull.returncode != 0:
                # The detail is what the failure card shows and what its
                # "Ask the agent" hand-off sends along, so it names the
                # command that ran and the revision it targeted.
                state.push_update_progress(
                    "error",
                    f"Fast-forward to {target[:12]} failed (git merge --ff-only) — "
                    "the checkout may have moved; check `git status` in a terminal",
                )
                return

            # Rebuild the in-tree frontend and stage website/dist into the
            # served static/dist (graceful no-op if no website/ or npm).
            # Done before pip install so the served bundle is refreshed even
            # if the package reinstall later hiccups.
            await _build_frontend(proj, state)

            # Reinstall the package so any new Python deps / entry points land.
            if not await _venv_pip_install(proj, state):
                # On the new revision with its dependencies unsynced, and not
                # restarting: a relaunch would die at import, so the stale-asset
                # watchdog must not take one either.
                update_ownership.refuse_restart(
                    "the dependency install after the dashboard update did not complete"
                )
                return
            # The tree and its dependencies agree again: a refusal an earlier
            # attempt recorded does not describe the install.
            tree_moved = False
            update_ownership.clear_restart_refusal()

            # Restart: save history + clean up sessions then exec the same process.
            logger.info("Update complete — saving history and cleaning up before restart")
            # Hand the gap to the restart: this step's ownership ends here and
            # the restart's begins as the await starts, with no yield between.
            owned.close()
            await _restart_gateway(state, resolver=respawn_executable)
        except Exception:
            logger.exception("Update failed")
            if tree_moved:
                update_ownership.refuse_restart(
                    "the dashboard update failed after the tree moved, before its "
                    "dependencies were installed"
                )
            state.push_update_progress("failed", "Update failed — check logs")
            state.push_refresh("update_failed")
        finally:
            owned.close()

    task = asyncio.create_task(_apply())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "status": "updating"})


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

    # Refuse a restart that cannot run BEFORE the 200, while the client still
    # holds the request: after it, a refusal reaches the user only as a
    # progress step, which a restart control waiting for the reload never
    # reads. ``_restart_gateway`` re-checks when it commits. Before the latch
    # below, so no await separates its check from the task it records.
    _launcher, _exe, refusal = await _resolve_restart_target()
    if refusal is not None:
        code, sentence = refusal
        return web.json_response({"ok": False, "error": sentence, "code": code}, status=409)

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
            await _restart_gateway(state)
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


# Named by the path callers import it from: the facade that serves it.
_PolicyApplyStopped.__module__ = "kiro_crew.dashboard.handlers.updates"
