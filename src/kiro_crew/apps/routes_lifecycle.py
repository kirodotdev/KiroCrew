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
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.backend import (
    PidfileRevokeFailed,
    _unrevoke_app_pid,
    app_backend_lifecycle_flock,
    backend_notice,
    capture_backend_provenance,
    quarantine_backend,
    recorded_backend_port,
    revoke_backend_provenance,
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


def _attach_backend_notice(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """Add *name*'s ``backend_notice`` to *payload* when its backend needs the user.

    The notice says why an enabled app's backend is not running when only the user
    can fix it (a port held by a process this gateway cannot attribute, an update
    that skipped its restart). A separate key from ``notice``, which update and
    uninstall already use for a machine-read token. The app detail page renders it.
    """
    notice = backend_notice(name)
    if notice:
        payload["backend_notice"] = notice
    return payload


# The update skipped its restart because the old spawn record's removal was not
# confirmed: starting now could adopt the old version still holding the port.
_UPDATE_NOT_RESTARTED_NOTICE = (
    "Update installed, but the background service for {name} was not restarted "
    "because the previous version may still be running and holding its port. "
    "Close that program, then turn the app off and on again."
)


# The update's files landed but its spawn record could not be written (disk full),
# so the backend is held down rather than started against a stale record. Names the
# actual cause, unlike _UPDATE_NOT_RESTARTED_NOTICE (which blames a port-holder).
_UPDATE_PROVENANCE_WRITE_FAILED_NOTICE = (
    "Update installed for {name}, but its background service could not be registered "
    "(the disk may be full), so the service was not started. Free disk space, then "
    "update {name} again."
)


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
    # The spawn-provenance row has ALREADY been dropped by the uninstall at its
    # durable commit boundary (after the files are removed, on result.ok), so this
    # stop takes the PLAIN path and does not touch provenance again. Doing the
    # delete here instead would make it non-terminal -- onUninstall has already run
    # by now, so a failed delete could not abort -- which is exactly the hazard the
    # commit-boundary deletion exists to remove.
    await loop.run_in_executor(subprocess_executor(), lambda: stop_app_backend(name))
    live_port = await loop.run_in_executor(
        subprocess_executor(), lambda: unstopped_backend_port(name, port_hint=port_hint)
    )
    return live_port, port_hint is not None


@contextlib.asynccontextmanager
async def _hold_backend_lifecycle_flock(name: str):
    """Hold the cross-process backend lifecycle flock across an update's await points.

    An update stops the backend, revokes the pre-update row, replaces the
    files and revokes again — but a backend spawn racing those steps holds
    ``app_backend_lifecycle_flock`` while it publishes ``ap`` and then calls
    ``_record_app_pid``, which writes the row UNCONDITIONALLY and so overwrites the
    revoked tombstone. If the update takes no lifecycle lock, a delayed
    ``_record_app_pid`` can land between the stop and the revoke (or between the two
    revocations), restoring a non-revoked row that the restart then adopts as the new
    version while an old-code survivor still serves. Holding this flock across the
    whole stop → replacement → final revocation makes the update and the spawn
    mutually exclusive: the update waits for an in-flight spawn to finish recording,
    then revokes atomically; a spawn that starts later blocks until the update
    releases. Symmetric with ``uninstall_app``, which holds the same flock across its
    revoke + removal.

    The sync flock blocks on ``flock()``, so it is entered and exited OFF the event
    loop (both ``uninstall_app`` callers run off-loop too); the fd is held for the
    duration. Released BEFORE the restart — the restart's own spawn re-acquires it to
    record the new generation's identity, so holding it across the restart would
    self-deadlock against that spawn.
    """
    loop = asyncio.get_running_loop()
    cm = app_backend_lifecycle_flock(name)
    await loop.run_in_executor(subprocess_executor(), cm.__enter__)
    try:
        yield
    finally:
        await loop.run_in_executor(subprocess_executor(), cm.__exit__, None, None, None)


async def _revoke_provenance_before_replacement(name: str) -> bool:
    """DURABLY revoke the pre-update spawn row BEFORE the update replaces files.

    An update reuses the port, so the pre-update row keeps vouching for whatever
    rebinds it — including a child that survived the stop above and forked a
    replacement — until it is invalidated. Doing that invalidation only AFTER the
    files are replaced leaves a window: the new code is on disk, the old row still
    vouches, and if the post-replacement revoke then cannot persist (ENOSPC/EDQUOT),
    a restart or same-name adoption attributes the OLD-code survivor as the updated
    app. So the durable revoke happens HERE, before replacement, and the caller
    REFUSES the replacement when it cannot be confirmed — nothing has been destroyed,
    the operator frees the disk and retries. The stop above may have RESTORED the row
    (an ordinary stop that found a detached survivor keeps the recovery row), so there
    is a live row to revoke even though the stop forgot it first.

    Returns ``True`` when the row is durably revoked (or there was none) and the
    update may proceed; ``False`` when the revoke could not be confirmed and the
    caller must refuse the replacement.
    """
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), revoke_backend_provenance, name
        )
        return True
    except PidfileRevokeFailed as exc:
        logger.warning(
            "update of %r: could not durably revoke the pre-update spawn record (%s); "
            "refusing to replace files so a same-name adoption cannot attribute an "
            "old-code survivor as the updated app",
            name,
            exc,
        )
        return False


async def _invalidate_provenance_before_restart(name: str) -> bool:
    """Durably revoke the pre-update spawn record and report whether that is confirmed.

    An update replaces the code behind the name while reusing the port, so the
    pre-update row still vouches for whatever rebinds it and a restart would adopt a
    survivor running the OLD code as the new version. Called once the update has
    committed (the rollback path has already returned), this invalidates the stale
    row so the new spawn records its own.

    Returns ``True`` when the record is CONFIRMED invalid on disk and the caller may
    restart. Returns ``False`` when invalidation could not be made durable
    (ENOSPC/EDQUOT) — the caller must then NOT restart, or it would adopt the
    stale-row survivor; the files are updated regardless and the operator restarts
    once the disk is writable.
    """
    loop = asyncio.get_running_loop()
    # DURABLE revoke first: stamp the row ``revoked`` and confirm the stamp reached
    # disk BEFORE the restart. The restart's adopt path refuses a revoked row, and —
    # unlike the in-memory quarantine, which a restart loses — the stamp rides on the
    # row the startup stale-reap retains, so it still refuses an old-code survivor
    # after a crash or restart. When the stamp cannot be confirmed we refuse the
    # restart rather than start onto a row that would re-attribute the survivor.
    try:
        await loop.run_in_executor(subprocess_executor(), revoke_backend_provenance, name)
    except PidfileRevokeFailed as exc:
        # Belt-and-braces: also hold the in-memory quarantine for THIS generation,
        # which needs no disk write and so holds under the very disk-full that
        # defeated the durable stamp. The operator frees the disk and retries.
        await loop.run_in_executor(
            subprocess_executor(),
            quarantine_backend,
            name,
            f"update could not durably revoke the spawn record: {exc}",
        )
        logger.warning(
            "update of %r: could not durably revoke the spawn record (%s); not "
            "starting the backend so a restart does not adopt an old-code survivor "
            "as the new version",
            name,
            exc,
        )
        return False
    # The row is durably revoked; KEEP the revoked tombstone rather than deleting it.
    #
    # Deleting here reopens the same resurrection race the uninstall path
    # closes by retaining its tombstone. A ceiling-driven stop can overlap this update
    # while a detached child still serves; if the delete makes the name ABSENT, the
    # delayed stop's _restore_app_pid / _restore_for_retry (guarded only by
    # name-absence) restores its cached PRE-update, non-revoked row — and a later
    # enable then attributes and adopts the pre-update old-code child as the updated
    # backend. The revoked tombstone makes both guards hold: _restore_app_pid refuses
    # (the name is present) and _adoption_provenance refuses (the row is revoked). The
    # new spawn's _record_app_pid overwrites the tombstone with its own un-revoked
    # identity (the new generation), and the startup stale-reap drops a tombstone whose
    # leader is dead and whose group has no survivor — so the fence neither leaks
    # adoption nor accumulates. Symmetric with uninstall_app's commit boundary.
    return True


async def _app_may_run_after_install(name: str, *, fresh_install: bool = False) -> bool:
    """Read whether installed app resources may run after an install or update."""

    def _read_live_state() -> bool:
        info = get_app(name)
        if not info or info.get("sessionApprovalConsentPending"):
            return False
        return fresh_install or bool(info.get("enabled"))

    return await asyncio.get_running_loop().run_in_executor(subprocess_executor(), _read_live_state)


async def _restore_provenance_after_failed_update(
    name: str, preserved_row: dict[str, Any] | None
) -> None:
    """Put back the spawn record a failed update captured, while the caller holds the flock.

    Called inside ``_hold_backend_lifecycle_flock``, never after it: once the flock is
    released a CLI uninstall queued on it runs at once, revokes this same spawn's row and
    removes the app, and a restore landing after that would clear the uninstall's
    revocation -- a same-name reinstall could then adopt the removed app's survivor.
    Under the flock the restore and the uninstall are ordered, and the uninstall's
    revocation is the last word. ``_restore_app_after_failed_update`` runs after release
    and does not touch the row again.

    *preserved_row* is the spawn record captured BEFORE the update's stop. The stop
    forgets a tracked backend's row, so without this the rollback restart's adopt
    branch would re-capture a surviving detached child, find no record attributing
    it, and refuse it -- leaving the enabled app unreachable. Restored BEFORE the
    restart via _unrevoke_app_pid: the pre-replacement F2 revoke may have stamped the
    live row ``revoked``, and plain _restore_app_pid (setdefault) would see the name
    present and leave that stamp standing, so the still-installed app's own backend
    would be refused; _unrevoke_app_pid clears the stamp when the live row is this
    spawn's revoked incarnation while leaving a successor a fresh spawn recorded. It
    runs whether or not the app may run: a DISABLED app whose update failed is not
    restarted, but a detached survivor can still be holding its port, and the recovery
    row is the only handle a later re-enable has to re-attribute it.

    GATED on the rollback having actually restored the app: when ``update_app``'s own
    rollback ALSO failed (a non-writable tree the copy could not be undone), it leaves
    ``installed.json`` ABSENT, so the app is not installed. Un-revoking then would
    leave a live row for a name that has no app -- and a same-name reinstall (which does
    not revoke or replace the row before starting) would adopt the surviving old backend
    against it, managing the previous installation's process as the new one. So the row
    is un-revoked ONLY when the record is still present; a rollback that left the record
    gone keeps the row REVOKED (fail-closed), which is exactly what refuses that
    survivor on the next install.
    """
    if preserved_row is None:
        return
    record_present = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), lambda: get_app(name) is not None
    )
    if record_present:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _unrevoke_app_pid, name, preserved_row
        )


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


@dataclass
class _ReplaceOutcome:
    """The result of replacing an installed app under the provenance transaction.

    ``kind`` is one of:
      ``revoke_failed``   -- the pre-replacement revoke could not be confirmed, so
                             the files were NOT replaced; the app is untouched and the
                             pre-update row was restored under the flock.
      ``install_failed``  -- the replace rolled back cleanly (``committed`` false); the
                             pre-update row was restored under the flock.
      ``committed_provenance_failed`` -- the replace landed on disk but its new spawn
                             row could not be persisted; the pre-update row stays
                             REVOKED and the backend must NOT be restarted.
      ``committed``       -- the replace landed; ``provenance_dropped`` says whether the
                             pre-update row's revocation was confirmed (so the caller may
                             restart) or not (do NOT restart, record the notice).
    ``result`` is the replace callable's own dict (``install_from_registry``'s), None
    only on ``revoke_failed`` (nothing was replaced).
    """

    kind: str
    result: dict[str, Any] | None
    provenance_dropped: bool


async def _replace_installed_app_with_provenance(
    name: str, do_replace: Callable[[], Awaitable[dict[str, Any]]]
) -> _ReplaceOutcome:
    """Run ``do_replace`` (which replaces an INSTALLED app's files) under the provenance
    transaction, so a survivor of the old version is never adopted as the new one.

    The single mechanism both the update endpoint and the registry-install endpoints go
    through when they replace an already-installed app. Holding the cross-process
    lifecycle flock for the whole span, it: captures the pre-update spawn row (before the
    stop forgets it), stops the backend, durably REVOKES the row BEFORE the replace (so a
    detached survivor rebinding the reused port is not vouched for), runs ``do_replace``,
    and then invalidates-or-restores the row by outcome. Every write that must not race a
    concurrent spawn's ``_record_app_pid`` happens inside the flock; the caller does its
    own register/restart AFTER the flock (start_app_backend re-acquires it, so holding it
    across a restart would self-deadlock). Callers must invoke this ONLY for an
    already-installed app and must NOT wrap it in a second provenance transaction -- it is
    the one mechanism.
    """
    outcome_kind: str
    result: dict[str, Any] | None = None
    provenance_dropped = False
    async with _hold_backend_lifecycle_flock(name):
        preserved_row = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), capture_backend_provenance, name
        )
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), stop_app_backend, name
        )
        await _deregister_app_off_loop(name)
        if not await _revoke_provenance_before_replacement(name):
            outcome_kind = "revoke_failed"
            await _restore_provenance_after_failed_update(name, preserved_row)
        else:
            result = await do_replace()
            if not result.get("ok"):
                if result.get("committed"):
                    # Landed on disk but a post-commit step failed: the new code is in
                    # place, so the row stays revoked (restoring it would re-attribute an
                    # old-code survivor). Fail closed, do not restart.
                    outcome_kind = "committed_provenance_failed"
                else:
                    # Clean rollback: nothing landed, so the pre-update row legitimately
                    # vouches for the app's own backend and is restored.
                    outcome_kind = "install_failed"
                    await _restore_provenance_after_failed_update(name, preserved_row)
            else:
                provenance_dropped = await _invalidate_provenance_before_restart(name)
                outcome_kind = "committed"
    return _ReplaceOutcome(kind=outcome_kind, result=result, provenance_dropped=provenance_dropped)


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
