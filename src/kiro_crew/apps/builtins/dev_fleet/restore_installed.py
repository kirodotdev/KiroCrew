"""Dev Fleet's "Restore installed build": the way back from Make Live.

Make Live leaves two pieces of state that keep the gateway booting a worktree: the
live-target pointer (``<config_dir>/live_target.json``) and the service definition --
the ``make-live.conf`` drop-in on a drivable systemd host, or the rewritten
``live-gateway`` launcher on launchd. Undo only returns one level, and only to a
provisioned checkout, so a wheel install is never its target. This module clears
both pieces and restarts the gateway through the same backend a cutover uses. With
no pointer and no drop-in, the base unit's ``ExecStart`` runs the installed
``kirocrew``; on launchd the launcher is written back from the record the first
cutover took (:mod:`installer_launcher`). ``live_target.maybe_reexec`` then returns
without setting ``KIROCREW_PROJECT_DIR``, and the install layout (and with it the
dashboard's channel switch) is the installer's again.

It is a cutover to "no live target", so it takes every guard the cutover takes, read
through the :mod:`live` facade: the gateway-process boundary, ``_MAKE_LIVE_LOCK``, the
``_MAKE_LIVE_COMMITTED`` latch, the removal lease and the pod check. The route that
calls it (``gateway_routes.handle_restore_installed``) carries the cutover's
owner-only gate and SEL audit.
"""

from __future__ import annotations

import asyncio

from kiro_crew.apps.builtins.dev_fleet import gateway_service, installer_launcher, live, runtime
from kiro_crew.executors import subprocess_executor
from kiro_crew.service import live_target

_RESTART_PENDING = {
    "ok": False,
    "code": "restart_pending",
    "error": "a cutover has been scheduled; the gateway is restarting — retry after it comes back",
}

_REMOVAL_BUSY = {
    "ok": False,
    "code": "busy",
    "error": "a worktree removal is in progress — retry once it has completed",
}

Snapshot = tuple["str | None", "str | None", bool]


def _restart_notice(*, desktop_app: bool) -> str:
    """What the owner finishes by hand when nothing managed can bounce the gateway."""
    cmd = live._manual_restart_command()
    quit_app = "quit the Kiro Crew desktop app, then " if desktop_app else ""
    return (
        "Restore stops using the worktree. To finish, "
        f"{quit_app}open a terminal (not one inside a worktree folder) and run `{cmd}`. "
        "The dashboard stays on the worktree until you do."
    )


async def _restore_installed(dry_run: bool = False) -> dict:
    """Unpin the live target so the gateway boots the build the installer put here.

    The pointer is reset to :data:`live_target.NO_TARGET_DOCUMENT` (which keeps the
    name maskable) and the systemd drop-in is removed, or on launchd the installer's
    launcher is written back from its record; then the gateway restarts. A failure
    after the first write restores the prior pointer AND the prior definition, as a
    failed cutover does, and keeps the launchd record. The record is deleted only
    once the restore succeeded.

    launchd whose launcher is a worktree's but has no record (the first cutover
    predates recording, or the record was removed) is refused with ``restore_no_installer_record``, and a record that
    fails validation with ``restore_installer_record_invalid``: the installed
    build's launcher cannot be re-derived from inside a gateway that runs a worktree
    (``kirocrew_bin`` would find the worktree's venv through PATH), and deleting the
    launcher would leave the plist with nothing to execute.
    """
    if live.running_in_backend():
        return {
            "ok": False,
            "code": "wrong_process",
            "error": (
                "restore-installed must run in the gateway process (POST "
                "/api/apps/dev-fleet/restore-installed), not in the Dev Fleet backend"
            ),
        }
    if live._MAKE_LIVE_COMMITTED:
        return dict(_RESTART_PENDING)
    _off = asyncio.get_running_loop().run_in_executor

    pod = await _off(subprocess_executor(), live._in_pod)
    if pod is None or pod:
        return {
            "ok": False,
            "code": "pod_indeterminate" if pod is None else "pod",
            "error": (
                "refusing restore-installed: this request is not attributable to the "
                "real live gateway (run it from the live dashboard, not a pod)"
            ),
        }

    svc = live._gateway_backend()
    kind = getattr(svc, "kind", "") if svc is not None else ""
    systemd = kind == "systemd"
    launchd = svc if isinstance(svc, gateway_service.LaunchdBackend) else None
    # The backend whose definition a cutover staged, and that this restore rewrites.
    definer = svc if systemd or launchd is not None else None
    unit_status = await live._live_user_unit_status()
    can_restart = svc is not None and unit_status == "ok"
    desktop_app = False
    if not can_restart and live._foreground_eligible(unit_status):
        # A foreground gateway respawns the console script it was started from, which a
        # Make Live made the worktree's, so it is never restarted here: Restore only
        # tells a desktop-supervised host apart and leaves the restart to the owner.
        fg = live._foreground_backend()
        fg_status = None if fg is None else await fg.status()
        desktop_app = fg_status == gateway_service.STATUS_DESKTOP_SUPERVISED

    def _snapshot_sync() -> Snapshot:
        pointer = live_target.snapshot()
        definition = definer.snapshot() if definer is not None else None
        return pointer, definition, live_target.is_pinned()

    async def _snapshot() -> Snapshot | dict:
        try:
            return await _off(subprocess_executor(), _snapshot_sync)
        except (OSError, ValueError) as exc:
            # Unreadable is not absent: restore(None) / rollback(None) on the unwind
            # would DESTROY state that was never captured, so refuse before writing.
            return {
                "ok": False,
                "code": "write_failed",
                "error": (
                    "refusing restore-installed: the live target or service "
                    "definition exists but could not be read, so a failed restore "
                    f"could not be rolled back: {runtime._redact(str(exc))}"
                ),
            }

    def _nothing_to_restore(snap: Snapshot) -> dict | None:
        _pointer, definition, pinned = snap
        if pinned:
            return None
        if launchd is not None:
            # A launcher that is not the installer's still boots a worktree.
            if definition is not None and not installer_launcher.is_installers(definition):
                return None
        elif definition is not None:
            return None
        return {
            "ok": False,
            "code": "no_live_target",
            "error": "no live target is set; the gateway already runs the installed build",
        }

    async def _installer_record(snap: Snapshot) -> str | None | dict:
        """The recorded launcher to write back on launchd, or a refusal.

        ``None`` when nothing needs writing back: not launchd, or the launcher is
        already the installer's (a foreground or staged-only cutover pinned the
        pointer and never rewrote it, so no record exists and none is needed). A
        launcher that is missing needs the record: clearing the pointer alone would
        leave launchd with nothing to execute.
        """
        definition = snap[1]
        if launchd is None:
            return None
        if definition is None:
            # No launcher at all: with no launchd agent to run it (a foreground host)
            # there is nothing to write back; with an agent, clearing the pointer alone
            # would leave launchd with nothing to execute.
            if live._foreground_eligible(unit_status):
                return None
        elif installer_launcher.is_installers(definition):
            return None
        try:
            record = await _off(
                subprocess_executor(), installer_launcher.load_record, launchd.live_program()
            )
        except installer_launcher.InvalidRecord as exc:
            return {
                "ok": False,
                "code": "restore_installer_record_invalid",
                "error": (
                    "refusing restore-installed: the record of the installed build's "
                    f"launchd launcher is not trustworthy ({runtime._redact(str(exc))}). "
                    "Remove it and run `kirocrew service install` from the installed "
                    "build (not a worktree) to rewrite the launcher"
                ),
            }
        except OSError as exc:
            return {
                "ok": False,
                "code": "write_failed",
                "error": (
                    "refusing restore-installed: the record of the installed build's "
                    f"launchd launcher could not be read: {runtime._redact(str(exc))}"
                ),
            }
        if record is None:
            return {
                "ok": False,
                "code": "restore_no_installer_record",
                "error": (
                    "Restore installed build has no record of the installed build's "
                    "launchd launcher (it was not captured, or was removed). "
                    "Run `kirocrew service install` from the installed build (not a "
                    "worktree) to rewrite the launcher"
                ),
            }
        return record

    snap = await _snapshot()
    if isinstance(snap, dict):
        return snap
    refused = _nothing_to_restore(snap)
    if refused is not None:
        return refused
    record = await _installer_record(snap)
    if isinstance(record, dict):
        return record
    plan = {
        "action": "restore_installed",
        "pointer_path": str(await _off(subprocess_executor(), live_target.pointer_path)),
        "clears_dropin": systemd and snap[1] is not None,
        "rewrites_launcher": record is not None,
        "restart": "service" if can_restart else "manual",
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}
    if live.removal_in_progress():
        return dict(_REMOVAL_BUSY)
    if live._MAKE_LIVE_LOCK.locked():
        return {"ok": False, "code": "busy", "error": "another make-live cutover is in progress"}
    async with live._MAKE_LIVE_LOCK:
        if live._MAKE_LIVE_COMMITTED:
            return dict(_RESTART_PENDING)
        if live.removal_in_progress():
            return dict(_REMOVAL_BUSY)
        # Re-snapshot under the lock: the unwind restores the state on disk NOW,
        # not what the early probe saw.
        snap = await _snapshot()
        if isinstance(snap, dict):
            return snap
        refused = _nothing_to_restore(snap)
        if refused is not None:
            return refused
        record = await _installer_record(snap)
        if isinstance(record, dict):
            return record
        prior_pointer, prior_definition, _pinned = snap

        def _unwind_sync() -> bool:
            ok = live_target.restore(prior_pointer)
            if definer is not None:
                ok = definer.rollback(prior_definition) and ok
            return ok

        async def _unwind() -> bool:
            rolled_back = await _off(subprocess_executor(), _unwind_sync)
            if definer is not None:
                await definer.reload()
            return rolled_back

        def _clear_sync() -> str | None:
            if not live_target.restore(None):
                return "the live-target pointer could not be cleared"
            if systemd and svc is not None and not svc.rollback(None):
                return "the make-live drop-in could not be removed"
            if launchd is not None and record is not None and not launchd.rollback(record):
                return "the installed build's launchd launcher could not be written back"
            return None

        async def _forget_record() -> None:
            if launchd is None:
                return
            if not await _off(subprocess_executor(), installer_launcher.forget_record):
                runtime.logger.warning(
                    "restore-installed succeeded but its launchd launcher record could "
                    "not be deleted; the next Make Live keeps the existing record"
                )

        clear_err = await _off(subprocess_executor(), _clear_sync)
        if clear_err is not None:
            return {
                "ok": False,
                "code": "write_failed",
                "rolled_back": await _unwind(),
                "error": clear_err,
            }
        if definer is not None:
            # Drop the removed override from the loaded definition before restarting
            # (a no-op on launchd, whose plist is never rewritten). A refused reload
            # leaves the manager on the worktree definition, so restarting would boot
            # the worktree while reporting success. With no override removed and no
            # restart here, a refused reload changes nothing and is not fatal.
            reloaded = await definer.reload()
            if not reloaded and (can_restart or (systemd and prior_definition is not None)):
                return {
                    "ok": False,
                    "code": "write_failed",
                    "rolled_back": await _unwind(),
                    "error": "the service manager did not reload; the live target was left as it was",
                }
        live._LIVE_WORKTREE = None
        live._LIVE_CHECK_AT = 0.0

        if can_restart:
            assert svc is not None
            start_id = await live._gateway_start_id()
            restarted, err = await svc.restart_detached()
            if not restarted:
                return {
                    "ok": False,
                    "code": "restart_failed",
                    "rolled_back": await _unwind(),
                    "error": runtime._redact(err),
                }
            live._MAKE_LIVE_COMMITTED = True
            await _forget_record()
            return {"ok": True, "restored": True, "plan": plan, "start_id": start_id}
        # Fail safe, as for a staged cutover: the pointer is cleared and correct, and
        # the next start of the gateway, however it happens, boots the installed
        # build. Rolling back would turn "finish with one command" into "start over".
        await _forget_record()
        return {
            "ok": True,
            "restored": True,
            "staged_only": True,
            "plan": {**plan, "restart": "manual"},
            "manual_restart": live._manual_restart_command(),
            "notice": _restart_notice(desktop_app=desktop_app),
        }


__all__ = ("_restore_installed",)
