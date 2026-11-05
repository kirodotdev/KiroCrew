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
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew.apps.backend import (
    recorded_backend_port,
    start_app_backend,
    stop_app_backend,
    unstopped_backend_port,
)
from kiro_crew.apps.bridges import (
    RegistrationResult,
    deregister_app,
    register_app,
    snapshot_app_agent_configs,
)
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


async def _backend_platform_refusal(name: str, backend: Any) -> str | None:
    """The reason *name*'s backend was refused on this host, for a route's response.

    ``start_app_backend`` returns None both for "no backend declared" and for a native
    Windows exec (shell launcher) refusal, which it only logs. After a None this reads
    the refusal so the enable, install and update responses can carry it as
    ``backend_refused``. The app still enables: its other parts may work without the
    backend. Off the loop because it reads the manifest and the entry file.
    """
    if backend is not None:
        return None
    from kiro_crew.apps.backend import exec_backend_platform_refusal

    try:
        return await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), exec_backend_platform_refusal, name
        )
    except Exception:
        logger.warning("Backend refusal lookup failed for app %s", name, exc_info=True)
        return None


async def _start_backend_after_install(name: str) -> str | None:
    """Spawn an app's backend after a fresh install/register, if it has one.

    ``start_app_backend`` is a no-op for apps that declare no backend and is
    idempotent for already-running ones, so this is safe to call unconditionally.
    It blocks on a health-check poll, so run it off the event loop. Failures are
    logged but never abort the install — the backend also gets a retry on the
    next gateway boot via ``start_enabled_app_backends``.

    Returns the platform refusal (see :func:`_backend_platform_refusal`) for the
    caller's ``backend_refused`` response field, or None.
    """
    try:
        backend = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), start_app_backend, name
        )
    except Exception:
        logger.warning("Backend auto-start after install failed for app %s", name, exc_info=True)
        return None
    return await _backend_platform_refusal(name, backend)


async def _register_app_off_loop(
    name: str, prior_agent_configs: dict[str, dict[str, Any]] | None = None
) -> RegistrationResult:
    """Run ``register_app`` on the subprocess executor, off the event loop.

    ``register_app`` / ``deregister_app`` do real filesystem work under
    ``KIROCREW_HOME`` — manifest reads, skill-dir symlink walks, agent JSON
    writes, and ``mcp.json`` read + atomic write.  On a stalled filesystem
    (e.g. a dead network mount) those calls block in the kernel; run on the
    loop they freeze every task including the liveness heartbeat until the
    stall watchdog kills the gateway.  Same offload pattern as ``install_app``
    and ``start_app_backend`` on these handlers.

    ``prior_agent_configs`` is the snapshot an update took before it deregistered the
    app (:func:`_snapshot_agent_configs_off_loop`); it is passed on to ``register_app``
    so the user's agent edits survive. An empty one is the plain call every other
    caller makes.
    """
    from kiro_crew.mcp_gateway.overlay_refresh import refresh_after_agent_change

    loop = asyncio.get_running_loop()
    box: list = []
    job = (
        partial(register_app, name, prior_agent_configs)
        if prior_agent_configs
        else partial(register_app, name)
    )

    async def _register() -> bool:
        # Under the broker's refresh lock: no spec write between check and publish.
        try:
            box.append(await loop.run_in_executor(subprocess_executor(), job))
        except BaseException as exc:
            box.append(exc)
            raise
        return bool(getattr(box[0], "agents", None))

    await refresh_after_agent_change(_register)
    if not box:  # no broker serves, or the refresh failed before registering
        box.append(await loop.run_in_executor(subprocess_executor(), job))
    if isinstance(box[0], BaseException):
        raise box[0]
    return box[0]


async def _snapshot_agent_configs_off_loop(name: str) -> dict[str, dict[str, Any]]:
    """Read *name*'s materialized agent configs off the loop, before a deregister.

    ``deregister_app`` deletes them, and they hold the user's own edits (``model``,
    ``description``...), so an update takes this snapshot first and hands it to
    :func:`_register_app_off_loop` (see ``snapshot_app_agent_configs``).
    """
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), snapshot_app_agent_configs, name
    )


async def _deregister_app_off_loop(name: str) -> RegistrationResult:
    """Run ``deregister_app`` off the event loop (see ``_register_app_off_loop``)."""
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), deregister_app, name
    )


async def _stop_backend_and_observe(name: str) -> tuple[int | None, bool]:
    """Stop *name*'s backend on uninstall and report what the probe established.

    Runs :func:`kiro_crew.apps.teardown.stop_backend_and_observe`, the one
    spelling of the hint / stop / probe sequence that disable and trust withdrawal
    share, with this module's bindings of the three backend functions so a patch
    through ``kiro_crew.apps.routes`` reaches them. On uninstall ``onUninstall``
    has already run app code in the app directory, so the declared port the probe
    falls back to may be stale; the second half of the result says whether a
    gateway-owned hint existed.
    """
    from kiro_crew.apps.teardown import stop_backend_and_observe

    return await stop_backend_and_observe(
        name,
        recorded=recorded_backend_port,
        stop=stop_app_backend,
        probe=unstopped_backend_port,
    )


def _awaits_consent(info: dict[str, Any] | None) -> bool:
    """Whether a record is missing or waits for session-approval consent."""
    return not info or bool(info.get("sessionApprovalConsentPending"))


async def _read_app_record(name: str) -> dict[str, Any] | None:
    """Read *name*'s live installed record off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(subprocess_executor(), get_app, name)


async def _app_may_run_after_install(name: str) -> bool:
    """Read whether installed app resources may run after an install or update.

    The live record decides, for a fresh install and an update alike: True only
    for an app that is enabled and not waiting for session-approval consent.
    ``register_app`` never consults ``enabled``, so this read is what keeps a
    disabled app's agents, skills, MCP servers and crons unpublished and its
    backend stopped. A managed install writes its record disabled, so a fresh
    install answers False here; its resources are registered by the guarded
    ``enable_app`` transition that turns it on.
    """
    info = await _read_app_record(name)
    return not _awaits_consent(info) and bool(info and info.get("enabled"))


async def _app_may_enable_after_install(name: str) -> bool:
    """Read whether a just-installed app may be enabled without the user.

    False for a missing record and for one waiting for session-approval consent,
    which only the user gives from the disclosure surface. The caller still owes
    the ``enable_app`` transition; this read does not say the app is enabled.
    """
    return not _awaits_consent(await _read_app_record(name))


async def _restore_app_after_failed_update(
    name: str, prior_agent_configs: dict[str, dict[str, Any]] | None = None
) -> str | None:
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

    ``prior_agent_configs`` is the update's pre-deregister snapshot, so the restored
    agents keep the user's edits.

    Returns the restarted backend's platform refusal for the 400 body's
    ``backend_refused`` field, or None.
    """
    if not await _app_may_run_after_install(name):
        return None
    try:
        await _register_app_off_loop(name, prior_agent_configs)
    except Exception:
        logger.warning("Re-register after failed update failed for app %s", name, exc_info=True)
    return await _start_backend_after_install(name)


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
