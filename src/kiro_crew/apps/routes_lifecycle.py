"""Steps the install, update, uninstall, enable and disable handlers share.

The minimum-version check, warning redaction, registering and deregistering an app off
the event loop, starting a backend after an install and stopping one with its outcome
observed, whether an app may run once installed, restoring an app after a failed update,
suspending one for session-approval reconsent, the refusal while a startup hook still
runs, and dropping an app's notification channels.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps.backend import (
    recorded_backend_port,
    start_app_backend,
    stop_app_backend,
    unstopped_backend_port,
)
from kiro_crew.apps.bridges import RegistrationResult, deregister_app, register_app
from kiro_crew.apps.hooks_integration import stop_retained_startup_hooks
from kiro_crew.apps.manager import get_app
from kiro_crew.apps.version import check_min_version as _check_min_version_str
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


# ---------------------------------------------------------------------------
# Version compatibility check
# ---------------------------------------------------------------------------


def _check_min_version(manifest_data: dict[str, Any]) -> str | None:
    """Check if the app requires a newer Kiro Crew version.

    Returns an error message if the current version is too old, or None if OK.
    """
    return _check_min_version_str(manifest_data.get("minKiroCrewVersion", ""))


def _redact_warning(msg: str) -> str:
    """Redact credentials and exfiltration URLs from warning strings."""
    from kiro_crew.security import redact

    msg = redact(msg)
    return msg


def _unregister_notification_channels(request: web.Request, name: str) -> None:
    """Drop *name*'s notification channels from the bus registry.

    Called on uninstall/disable so channels don't linger as ghosts. Best
    effort and side-effect free beyond the in-memory registry: the push
    path independently re-checks enablement, so this is hygiene, not a
    security control. Re-enabling re-registers lazily on first push.
    """
    state = request.app.get("state")
    bus = getattr(state, "notification_bus", None) if state is not None else None
    if bus is None:
        return
    removed = bus.unregister_app_channels(name)
    if removed:
        logger.info("Unregistered %d notification channel(s) for app %s", removed, name)


async def _start_backend_after_install(name: str) -> None:
    """Spawn an app's backend after a fresh install/register, if it has one.

    ``start_app_backend`` is a no-op for apps that declare no backend and is
    idempotent for already-running ones, so this is safe to call unconditionally.
    It blocks on a health-check poll, so run it off the event loop. Failures are
    logged but never abort the install — the backend also gets a retry on the
    next gateway boot via ``start_enabled_app_backends``.
    """
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), start_app_backend, name
        )
    except Exception:
        logger.warning("Backend auto-start after install failed for app %s", name, exc_info=True)


async def _register_app_off_loop(name: str) -> RegistrationResult:
    """Run ``register_app`` on the subprocess executor, off the event loop.

    ``register_app`` / ``deregister_app`` do real filesystem work under
    ``KIROCREW_HOME`` — manifest reads, skill-dir symlink walks, agent JSON
    writes, and ``mcp.json`` read + atomic write.  On a stalled filesystem
    (e.g. a dead network mount) those calls block in the kernel; run on the
    loop they freeze every task including the liveness heartbeat until the
    stall watchdog kills the gateway.  Same offload pattern as ``install_app``
    and ``start_app_backend`` on these handlers.
    """
    from kiro_crew.mcp_gateway.overlay_refresh import refresh_after_agent_change

    loop = asyncio.get_running_loop()
    box: list = []

    async def _register() -> bool:
        # Under the broker's refresh lock: no spec write between check and publish.
        try:
            box.append(await loop.run_in_executor(subprocess_executor(), register_app, name))
        except BaseException as exc:
            box.append(exc)
            raise
        return bool(getattr(box[0], "agents", None))

    await refresh_after_agent_change(_register)
    if not box:  # no broker serves, or the refresh failed before registering
        box.append(await loop.run_in_executor(subprocess_executor(), register_app, name))
    if isinstance(box[0], BaseException):
        raise box[0]
    return box[0]


async def _deregister_app_off_loop(name: str) -> RegistrationResult:
    """Run ``deregister_app`` off the event loop (see ``_register_app_off_loop``)."""
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), deregister_app, name
    )


async def _stop_backend_and_observe(name: str) -> tuple[int | None, bool]:
    """Stop *name*'s backend and report what the probe could establish.

    Returns the port it is STILL listening on, if any, and whether the probe had
    gateway-owned evidence of which port to look at. Both halves are needed,
    because ``None`` from the probe means "nothing observed", NOT "definitely
    stopped": with no recorded port the probe falls back to the declared one in
    ``app.json``, which sits inside the app directory and is writable by any app
    trusted to run code. By the time the stop runs, ``onUninstall`` has already
    executed app-controlled code in that directory, so a fixed-port backend this
    gateway never tracked could have relabelled its port -- or dropped its
    ``entryPoint`` -- and the fallback would then probe the wrong port and answer
    ``None``. Reporting that as a clean stop is the same mistake as believing the
    stop's boolean, one level further out.

    ``stop_app_backend``'s own return value cannot answer the first half. It is
    ``False`` both for "there was nothing to stop" (never started, already dead)
    and for "something is running that I did not stop" (a fixed-port backend never
    adopted at boot, an adoption with no usable PIDs), and ``True`` only says the
    process it was TRACKING is gone -- which is silent about a detached worker the
    app spawned for itself. Those need opposite handling, so the port is OBSERVED
    rather than the flag believed. Same contract ``teardown_app_runtime`` applies
    on disable and on trust withdrawal.

    The hint is captured BEFORE the stop because the stop drops both the live
    tracking entry and the pidfile record, and those are the only gateway-owned
    evidence of which port this backend actually used.
    """
    loop = asyncio.get_running_loop()
    port_hint = await loop.run_in_executor(subprocess_executor(), recorded_backend_port, name)
    await loop.run_in_executor(subprocess_executor(), stop_app_backend, name)
    live_port = await loop.run_in_executor(
        subprocess_executor(), lambda: unstopped_backend_port(name, port_hint=port_hint)
    )
    return live_port, port_hint is not None


async def _app_may_run_after_install(name: str, *, fresh_install: bool = False) -> bool:
    """Read whether installed app resources may run after an install or update."""

    def _read_live_state() -> bool:
        info = get_app(name)
        if not info or info.get("sessionApprovalConsentPending"):
            return False
        return fresh_install or bool(info.get("enabled"))

    return await asyncio.get_running_loop().run_in_executor(subprocess_executor(), _read_live_state)


async def _restore_app_after_failed_update(name: str) -> None:
    """Put an app back the way a failed update found it.

    The update stopped the backend and scrubbed the resources before it touched
    any file, and the old tree is intact (``update_app`` restores it on a failed
    replacement), so the app is left usable rather than broken. What comes back
    is what was there: resources and a backend for an app that may run, nothing
    for a disabled one -- ``register_app`` never consults ``enabled``, so an
    unconditional re-register would publish a disabled app's agents, skills,
    MCP servers and crons, and nothing scrubs them again until the next
    enable/disable. Live read: a failed update leaves the record unchanged.

    Recovery failures are logged and never escape: the caller still owes the
    failed-update audit line and the 400 body, and the cause of the failed
    update (disk full, no free port) is often what breaks the restore too.
    """
    if not await _app_may_run_after_install(name):
        return
    try:
        await _register_app_off_loop(name)
    except Exception:
        logger.warning("Re-register after failed update failed for app %s", name, exc_info=True)
    await _start_backend_after_install(name)


async def _suspend_app_for_session_approval_reconsent(
    name: str,
) -> RegistrationResult:
    """Stop the old app and remove its resources until the user re-enables it."""
    await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
    await _deregister_app_off_loop(name)
    return RegistrationResult()


async def _refuse_while_startup_hook_runs(name: str, *, action: str) -> web.Response | None:
    """Refuse destructive lifecycle work while retained app code is still live."""
    stopped = await stop_retained_startup_hooks(name, bounded=True)
    if stopped:
        return None

    detail = "detached startup hook is still running or cleanup could not be verified"
    sel().log_api_access(
        caller="dashboard",
        operation=f"app_{action}",
        outcome="denied",
        resources=name,
        error=detail,
    )
    return web.json_response(
        {
            "error": (
                f"cannot {action} {name!r} while its timed-out startup hook "
                "is still running; retry after it exits"
            ),
            "code": "startup_hook_still_running",
            "retryable": True,
            "app": name,
        },
        status=409,
    )
