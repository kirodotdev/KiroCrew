"""Steps the install, update, uninstall, enable and disable handlers share.

The minimum-version check, warning redaction, registering and deregistering an app off
the event loop, starting a backend after an install and stopping one with its outcome
observed, whether an app may run once installed, restoring an app after a failed update,
pruning the agents an update dropped, re-installing an installed app from the registry,
suspending one for session-approval reconsent, the refusal while a startup hook still
runs, dropping an app's notification channels, telling open dashboards an app's files
were replaced, and running ``setup.onEnable`` for an enable and again after an update of
an enabled app.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.apps.backend import (
    recorded_backend_port,
    start_app_backend,
    stop_app_backend,
    unstopped_backend_port,
)
from kiro_crew.apps.bridges import (
    RegistrationResult,
    _prune_stale_app_resources,
    _registration_source,
    deregister_app,
    register_app,
)
from kiro_crew.apps.hooks_integration import stop_retained_startup_hooks

# Aliased to keep `routes._run_lifecycle_script` patchable, which several tests rely on.
from kiro_crew.apps.lifecycle_scripts import run_lifecycle_script as _run_lifecycle_script
from kiro_crew.apps.manager import get_app
from kiro_crew.apps.manifest import PlatformConfig
from kiro_crew.apps.registry import install_from_registry
from kiro_crew.apps.version import check_min_version as _check_min_version_str
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

if TYPE_CHECKING:
    # Annotation-only, so the routes facade does not serve them.
    from collections.abc import Awaitable, Callable

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


def _announce_app_ui_changed(request: web.Request, name: str) -> None:
    """Tell every open dashboard that *name*'s installed files were just replaced.

    A dashboard keeps an app's UI module for the life of the page (the ESM module
    map is keyed by URL), so a tab that has the app open, or opens it again later,
    would run the old UI until a manual reload. This sends the ``app_reload`` frame
    the dev-mode watcher sends (``apps/dev_mode.py``), and AppHost answers it with a
    page reload. Best effort: the files are already in place, and a gateway with no
    dashboard socket has no tab to tell.
    """
    state = request.app.get("state")
    broadcast = getattr(state, "broadcast_ws", None) if state is not None else None
    if broadcast is None:
        return
    try:
        broadcast("app_reload", {"app": name, "ts": time.time()})
    except Exception:
        logger.debug("app_reload broadcast for %s failed", name, exc_info=True)


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
    job = partial(register_app, name)

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


async def _deregister_app_off_loop(name: str, *, keep_agents: bool = False) -> RegistrationResult:
    """Run ``deregister_app`` off the event loop (see ``_register_app_off_loop``)."""
    job = (
        partial(deregister_app, name, keep_agents=True)
        if keep_agents
        else partial(deregister_app, name)
    )
    return await asyncio.get_running_loop().run_in_executor(subprocess_executor(), job)


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


async def _restore_app_after_failed_update(name: str) -> str | None:
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

    Returns the restarted backend's platform refusal for the 400 body's
    ``backend_refused`` field, or None.
    """
    if not await _app_may_run_after_install(name):
        return None
    try:
        await _register_app_off_loop(name)
    except Exception:
        logger.warning("Re-register after failed update failed for app %s", name, exc_info=True)
    return await _start_backend_after_install(name)


async def _prune_dropped_resources(name: str) -> None:
    """Prune the agent specs and MCP servers *name*'s installed manifest does not declare.

    An update's take-down keeps the agent specs (``deregister_app(keep_agents=True)``,
    app-kit-platform §3), so once the new files are in place the specs of the agents the
    new version dropped are removed here, off the loop.
    """

    def _prune() -> None:
        manifest, app_root = _registration_source(name)
        if manifest is not None:
            _prune_stale_app_resources(name, manifest, app_root)

    try:
        await asyncio.get_running_loop().run_in_executor(subprocess_executor(), _prune)
    except Exception:
        # The files are in place; the boot reconcile prunes what this missed.
        logger.warning("Could not prune dropped resources for app %s", name, exc_info=True)


async def _reinstall_from_registry(
    name: str,
    registry_name: str,
    log_lines: list[str] | None = None,
    *,
    after_failure: Callable[[bool], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Re-install installed *name* from the registry, taking it down only to replace it.

    ``install_from_registry`` awaits the take-down right before it copies a managed
    app's new files in, so a refusal or a failed clone, build or ``onInstall`` leaves
    the app running. The take-down stops the backend (on Windows the copy fails while
    it holds a file open under the tree), then deregisters all but the agent specs: the
    app-kit-platform §17 order. The caller's registration rewrites each spec in place,
    so one it does not rewrite keeps its last good copy; the specs the installed
    manifest does not declare are pruned here. A failure after the take-down
    restores the app (:func:`_restore_app_after_failed_update`), or removes the kept
    specs of one that may not run. An install that makes a gateway-managed app
    self-managed (``resources: "app"``) skips the take-down, so once the record says so
    the backend is stopped and the app fully deregistered: it registers its own from
    then on.

    ``after_failure`` is awaited once for a failed install, with whether the app was
    taken down, before anything starts the old app again. The update route puts a
    switched trust grant back there, so the restored local code starts under the
    grant that covers it.
    """
    taken_down = False
    loop = asyncio.get_running_loop()

    async def _is_self_managed() -> bool:
        return (await _read_app_record(name) or {}).get("resources") == "app"

    was_self_managed = await _is_self_managed()

    async def _take_down() -> None:
        nonlocal taken_down
        taken_down = True
        await loop.run_in_executor(subprocess_executor(), stop_app_backend, name)
        await _deregister_app_off_loop(name, keep_agents=True)

    result = await install_from_registry(
        registry_name, log_lines=log_lines, before_replace=_take_down
    )
    if not result.get("ok") and after_failure is not None:
        await after_failure(taken_down)
    if not taken_down:
        if not was_self_managed and await _is_self_managed():
            await loop.run_in_executor(subprocess_executor(), stop_app_backend, name)
            await _deregister_app_off_loop(name)
        return result
    await _prune_dropped_resources(name)
    if result.get("ok"):
        return result
    if not await _app_may_run_after_install(name):
        # A failure after the copy can leave the record disabled (a new
        # ``sessionApproval``); the agent specs the take-down kept go with it.
        await _deregister_app_off_loop(name)
        return result
    restore_refused = await _restore_app_after_failed_update(name)
    if restore_refused:
        result["backend_refused"] = restore_refused
    return result


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


def _client_install_manifest(manifest: dict[str, Any]) -> PlatformConfig | None:
    """The app's :class:`PlatformConfig` when it is a CLIENT-install app, else ``None``.

    A ``client`` app's real payload is a desktop application the user installs on
    their OWN machine; what the gateway holds is metadata plus a dashboard page.
    So its lifecycle scripts address something that legitimately may not be on
    this host, which is what makes them advisory rather than a health check:
    see ``handle_enable_app``.

    Never raises. ``PlatformConfig.from_dict`` iterates ``os`` and ``arch``
    directly, so a hand-edited ``app.json`` carrying ``"os": null`` raises
    ``TypeError`` there; this is the first place the enable path parses
    ``platform`` at all, so an unguarded call would turn a malformed manifest into
    a 500 on enable. A manifest this app cannot read is treated as "not a client
    app", which keeps the strict rollback behavior rather than silently widening
    the advisory path.
    """
    platform_raw = manifest.get("platform")
    if not isinstance(platform_raw, dict):
        return None
    try:
        platform_cfg = PlatformConfig.from_dict(platform_raw)
    except (TypeError, ValueError) as exc:
        logger.warning("Ignoring an unreadable platform block on app enable: %s", exc)
        return None
    return platform_cfg if platform_cfg.installMode == "client" else None


async def _run_on_enable_script(
    name: str, manifest: dict[str, Any]
) -> tuple[dict[str, Any] | None, bool]:
    """Run *name*'s ``setup.onEnable`` for an enable, or again after an update.

    One runner for the enable route and every update of an enabled app, so the
    timeout, the client-install rules and the redaction cannot drift between them.
    Returns the response's ``onEnable`` block, ``None`` when *manifest* declares no
    script, and whether the caller must take the app down: True only when the
    script failed for an app that is not a ``platform.installMode: "client"`` app.
    A client app's script is advisory and is skipped on a host its ``platform.os``
    does not list (see ``handle_enable_app``). The timeout is
    ``setup.onEnableTimeout`` clamped here, where the script runs, under the
    caller's app lifecycle lock.
    """
    setup = manifest.get("setup") or {}
    on_enable = setup.get("onEnable", "")
    if not on_enable:
        return None, False
    client_platform = _client_install_manifest(manifest)
    if client_platform is not None and not client_platform.supports_platform(sys.platform):
        return {"output": "", "failed": False, "skipped": "unsupported_platform"}, False
    # Local imports keep these helpers out of the names the routes facade serves.
    from kiro_crew.apps.manifest import lifecycle_timeout_seconds
    from kiro_crew.security import redact_credentials

    timeout = lifecycle_timeout_seconds(
        setup.get("onEnableTimeout"), app_name=name, field_name="onEnableTimeout"
    )
    script_output = await _run_lifecycle_script(
        name, on_enable, timeout=timeout, action="on_enable"
    )
    cleaned, _ = redact_credentials(script_output.get("output", ""))
    failed = bool(script_output.get("failed", False))
    return {"output": cleaned, "failed": failed}, failed and client_platform is None


async def _rerun_on_enable_after_update(request: web.Request, name: str) -> dict[str, Any]:
    """Run an updated, still-enabled app's ``setup.onEnable`` again, as Enable runs it.

    An update swaps in a fresh copy of the app's files, which never carries
    ``node_modules`` or ``.venv``, and keeps the app enabled, so whatever the
    script installed into the installed folder is gone until it runs again. Every
    route that updates an installed app (both branches of ``handle_update_app``,
    and a registry install over an installed app, which is how an adopted app
    takes a catalog update) calls this once the app is re-registered and its
    backend started, the order Enable uses, and only while the app may still run.
    The script is the NEW version's, read from the live record.

    Returns the fields the caller adds to its successful update's response: none
    when the new version declares no script, else ``onEnable`` (the script's
    redacted result), plus ``onEnableFailed`` (the reason) when the script failed.
    A failure never takes the app down: an update must not turn a working enabled
    app off, so the app stays enabled with its crons, agents and grants as the
    update left them. The failure is logged and reaches the user as a bell note,
    and the next enable or update runs the script again. A client-install app's
    failure stays advisory, as on Enable, and reports nothing.
    """
    record = await _read_app_record(name) or {}
    block, failed = await _run_on_enable_script(name, record.get("manifest") or {})
    if block is None:
        return {}
    if not failed:
        return {"onEnable": block}
    reason = block["output"] or "the script exited with an error and printed nothing"
    logger.warning(
        "%s's setup.onEnable failed after its update; it stays enabled: %s", name, reason
    )
    _note_on_enable_failed_after_update(request, name, reason)
    return {"onEnable": block, "onEnableFailed": reason}


def _note_on_enable_failed_after_update(request: web.Request, name: str, reason: str) -> None:
    """Bell-note that *name*'s re-run ``onEnable`` failed, on the app-update channel.

    The same ``state.notify`` path and ``system.apps`` channel as the note that
    announces an app update, so a user who mutes app notes mutes this too. Titled
    by the app's NAME, as that note is. The script's output is app-authored text,
    so the body states what the gateway did first and quotes the output after it,
    labelled as the script's (the runner already kept only its last 20 lines).
    Total: the update has landed, so a note that cannot be sent is logged and never
    fails the route.
    """
    state = request.app.get("state")
    if state is None:
        return
    # Local import keeps these names out of the ones the routes facade serves.
    from kiro_crew.apps.update_notify import NOTE_CHANNEL, NOTE_KIND

    try:
        state.notify(
            NOTE_KIND,
            f"{name} updated, but its setup script failed",
            "The app stays enabled. Enable or update it again to re-run its"
            f" setup.onEnable script.\n\nThe script's output:\n{reason}",
            meta={"app": name},
            url=f"/apps/detail/{name}",
            channel=NOTE_CHANNEL,
        )
    except Exception:  # noqa: BLE001 - the update has landed
        logger.warning("could not deliver the onEnable failure note for %s", name, exc_info=True)
