"""Apply an armed update at service boot, before the gateway serves anything.

The one place an update takes effect (decision entry "updates never restart a
running gateway"). A person's click only arms it (:mod:`kiro_crew.update_next_start`);
the next start runs this, ahead of the listeners and of the session restore, so
nothing is mid-turn and nothing has to be drained. Nothing here runs at shutdown:
the desktop app stops the gateway with a signal and a timeout, which would cut a
hook there.

An armed update is applied when the running version is not the armed one:

* the install already moved (a managed venv staged beside the running gateway, or
  an earlier ``kirocrew update``): no command runs, the process only re-execs;
* otherwise the provider's own apply runs as ``kirocrew update`` in a child
  process, bounded, so one apply covers a policy command, a managed venv and a git
  checkout exactly as the operator's own ``kirocrew update`` does. The child's
  output goes to the log.

A git checkout behind its upstream arms with that distance, so it is applied even
when its version equals the running one (a pull need not bump ``__version__``) and
it re-execs after a successful apply whether or not the version moved.

Then it re-execs onto the interpreter a restart would use. A failure clears the
arm, keeps the old version, and leaves ``pending_apply_error`` for the dashboard.

A gateway the desktop app hosts is skipped: the desktop owns the apply, at its
own quit and before it spawns the gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from kiro_crew import platform_compat, update_next_start
from kiro_crew.platform.update_provider import INSTALLER_TIMEOUT_TERM_GRACE_SECS

logger = logging.getLogger(__name__)

#: Longest one boot-time apply may run before it counts as failed. A managed venv
#: build downloads and installs a release, so this is generous, but it is a bound.
APPLY_TIMEOUT_SECS = update_next_start.APPLY_BOUND_SECS

#: How long the apply child gets to unwind after an interrupt before it is killed:
#: the rollback bound its own Ctrl-C handler allows the installer, plus a margin.
#: The installer lives in a session of its own that a group kill never reaches, so
#: the handler must be allowed to finish restoring the previous install.
INTERRUPT_GRACE_SECS = INSTALLER_TIMEOUT_TERM_GRACE_SECS + 15.0

#: Longest of the child's output kept for the log and the error.
_OUTPUT_TAIL_CHARS = 1500

#: Set by the desktop app on the gateway it spawns.
DESKTOP_HOSTED_ENV = "KIROCREW_DESKTOP_GATEWAY"


def hosted_by_desktop() -> bool:
    """Whether the desktop app spawned this gateway (and so owns the apply)."""
    return bool(os.environ.get(DESKTOP_HOSTED_ENV))


def _headless_desktop() -> bool:
    """The Windows desktop bundle with no desktop app hosting this gateway.

    ``False`` whenever a policy provider owns updates: its own apply (or its
    refusal) runs through ``kirocrew update`` below, and the desktop installer
    must never stand in for it.
    """
    from kiro_crew.platform.desktop_installer_update import (
        headless_desktop_install,
        provider_owns_updates,
    )

    return headless_desktop_install() and not provider_owns_updates()


async def _record_failure_best_effort(error: str) -> None:
    """End the arm with *error*; a record that cannot be written must not stop the start.

    The gateway then starts on the version it has, as every other refused apply does.
    """
    try:
        await asyncio.to_thread(lambda: update_next_start.finish(error=error))
    except Exception:
        logger.warning("Could not record the update failure; starting anyway", exc_info=True)


def _forget_desktop_hand_off() -> None:
    """Drop the hand-off record and the helper's result once the update runs."""
    from kiro_crew.platform import desktop_installer_update as desktop

    update_next_start.clear_applying()
    desktop.clear_result()


async def _hand_off_desktop_install(state: update_next_start.NextStartState, running: str) -> bool:
    """Verify the armed installer and hand it to the helper, then end this process.

    The installer replaces the files this process runs from, which Windows keeps
    locked while it runs, so the gateway cannot install it itself: a detached
    helper waits for this process to exit, verifies the file again, installs it
    and starts the gateway again with the same arguments. A refusal here, or a
    failure the helper recorded on an earlier start, ends the arm with the error
    and the gateway starts on the version it has. Returns ``False`` in those
    cases; a hand-off does not return.
    """
    from kiro_crew.platform import desktop_installer_update as desktop

    if await asyncio.to_thread(desktop.desktop_app_running):
        # The helper would wait on the app, and the app's own updater owns this
        # install while it runs: the arm stays for a start without it.
        logger.info(
            "Update armed (%s): the desktop app is running and owns the apply", state.armed_version
        )
        return False
    # The record a previous start left for its restart waiter: that start is over.
    await asyncio.to_thread(update_next_start.clear_applying)
    failed = await asyncio.to_thread(desktop.result_error_for, state.armed_version)
    if failed:
        logger.error(
            "Desktop update %s failed in the installer helper: %s", state.armed_version, failed
        )
        await _record_failure_best_effort(failed)
        try:
            await asyncio.to_thread(desktop.clear_result)
        except OSError:
            logger.warning("Could not clear the installer helper's result", exc_info=True)
        return False
    if state.attempts >= update_next_start.MAX_BOOT_ATTEMPTS:
        logger.error(
            "Armed update %s gave up after %d boot attempts", state.armed_version, state.attempts
        )
        await _record_failure_best_effort(
            f"The update to {state.armed_version} did not complete after "
            f"{state.attempts} starts; run `kirocrew update`."
        )
        return False
    try:
        await asyncio.to_thread(update_next_start.note_boot_attempt)
        # The download binds nothing and can take minutes: a restart waiter reads
        # this record to wait it out rather than report a wedged start.
        await asyncio.to_thread(update_next_start.mark_applying)
        prepared = await desktop.prepare(running, expected_version=state.armed_version)
        # The installer rewrites this install: copy every memory store first, and
        # install nothing when the copy did not land.
        from kiro_crew.platform.wheel_apply import (
            snapshot_memory_before_update,
            snapshot_refusal,
        )

        _copied, failure = await asyncio.to_thread(snapshot_memory_before_update)
        if failure:
            raise RuntimeError(snapshot_refusal(failure))
        helper = await asyncio.to_thread(
            lambda: desktop.hand_off(prepared, wait_pids=(os.getpid(),), restart=True)
        )
    except asyncio.CancelledError:
        await asyncio.to_thread(update_next_start.clear_applying)
        raise
    except Exception as exc:
        logger.error("Desktop update %s refused: %s", state.armed_version, exc)
        message = str(exc) or type(exc).__name__
        try:
            await asyncio.to_thread(update_next_start.clear_applying)
        except Exception:
            logger.debug("could not clear the applying record", exc_info=True)
        await _record_failure_best_effort(message)
        return False
    await asyncio.to_thread(update_next_start.mark_handed_off, helper)
    logger.info(
        "Update armed (%s): verified the installer; exiting so helper pid %d can install it",
        state.armed_version,
        helper,
    )
    # Before the listeners and the session restore, so nothing is cut; the
    # helper starts the gateway again once the installer has finished.
    raise SystemExit(0)


async def _stop_apply_child(proc: asyncio.subprocess.Process) -> None:
    """Interrupt the apply child, give its cleanup a bounded moment, then kill it."""
    if platform_compat.IS_POSIX and proc.returncode is None:
        try:
            # The child leads its own group (start_new_session), so its pid is its pgid.
            print("Stopping the update and restoring the previous version...", flush=True)
            platform_compat.kill_process_group(proc.pid, signal.SIGINT)
            await asyncio.wait_for(proc.communicate(), timeout=INTERRUPT_GRACE_SECS)
            return
        except (asyncio.TimeoutError, OSError, ValueError):
            pass
    await platform_compat.kill_and_reap(proc)


async def _run_apply_child() -> tuple[bool, str]:
    """Run ``kirocrew update`` as a child. ``(ok, tail of its output or the reason)``."""
    from kiro_crew.platform.context import PlatformCompositionError, redact_via_context
    from kiro_crew.security.redaction_switch import always_scan

    proc = await asyncio.create_subprocess_exec(
        *platform_compat.isolated_python_argv("-P", "-m", "kiro_crew", "update"),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=APPLY_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        await _stop_apply_child(proc)
        return False, f"the update did not finish within {int(APPLY_TIMEOUT_SECS)} seconds"
    except asyncio.CancelledError:
        await _stop_apply_child(proc)
        raise
    try:
        # The context's redactor applies a loaded companion's patterns too; a
        # standalone install gets the same ``security.redact`` as before. The tail
        # is cut below, so the credential pass runs in full whatever seam is open.
        with always_scan():
            text = redact_via_context((out or b"").decode("utf-8", "replace")).strip()
    except PlatformCompositionError:
        # No redactor we can trust: withhold the output rather than show it raw.
        return proc.returncode == 0, ""
    tail = text[-_OUTPUT_TAIL_CHARS:]
    return proc.returncode == 0, tail


def _failure_reason(tail: str) -> str:
    """One line for the dashboard from the failed apply's output."""
    if "EOFError" in tail:
        # The updater asked a question (local changes it would discard) and nobody
        # could answer at boot.
        return "The update needs your confirmation. Run `kirocrew update` in a terminal."
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1] if lines else "the update command failed"


async def _resolve_exec_target() -> tuple[str | None, str | None]:
    """``(launcher, interpreter)`` a restart would exec, one of them set; or ``(None, None)``."""
    from kiro_crew.gateway_restart import resolve_restart_launcher
    from kiro_crew.platform.wheel_engine import respawn_executable

    launcher = await asyncio.to_thread(resolve_restart_launcher)
    if launcher is not None:
        return launcher, None
    exe = await asyncio.to_thread(respawn_executable)
    if await asyncio.to_thread(platform_compat.execv_target_available, exe):
        return None, exe
    return None, None


def _reexec(launcher: str | None, exe: str | None) -> None:
    if launcher is not None:
        platform_compat.reexec_launcher(launcher, sys.argv[1:])
    else:
        platform_compat.reexec_python_module("kiro_crew", sys.argv[1:], executable=exe)


async def apply_armed_update_at_boot() -> bool:
    """Apply the armed update, if any, stopping it cleanly on SIGTERM.

    A service manager stops the gateway with SIGTERM. Left at its default that
    ends the process at once and orphans the apply child, which lives in a session
    of its own and keeps installing. For the duration of the apply, SIGTERM
    cancels it instead: the child is interrupted and given its rollback grace
    (:func:`_stop_apply_child`), then this process ends with the SIGTERM status.
    The previous handler is restored on every exit.
    """
    if not platform_compat.IS_POSIX:
        return await _apply_armed_update_at_boot()
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    stopped: list[bool] = []

    def _on_term() -> None:
        stopped.append(True)
        if task is not None:
            task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_term)
    except (NotImplementedError, RuntimeError, ValueError):
        return await _apply_armed_update_at_boot()
    try:
        return await _apply_armed_update_at_boot()
    except asyncio.CancelledError:
        if not stopped:
            raise
        _exit_with_sigterm_status()
        raise
    finally:
        with contextlib.suppress(Exception):
            loop.remove_signal_handler(signal.SIGTERM)


def _exit_with_sigterm_status() -> None:
    """End this process as a SIGTERM would have, once the apply child has been stopped."""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


def _desktop_running_reaches(running: str, armed: str) -> bool:
    """The desktop installer's ordering: a full nightly stamp and edition builds count.

    Unordered versions are not "reached" (equal text aside): the apply then runs
    and its own newer-than check refuses, with a reason, rather than dropping the arm.
    """
    if running == armed:
        return True
    from kiro_crew.platform.desktop_installer_update import is_newer

    return is_newer(armed, running) is False


def _running_reaches(running: str, armed: str) -> bool:
    """Whether *running* is *armed* or newer (equal text when the order cannot be read)."""
    if running == armed:
        return True
    from kiro_crew.dashboard.handlers.updates_verdict import _is_newer

    return _is_newer(armed, running) is False


async def _clear_arm_best_effort() -> None:
    """Clear an arm nothing needs to install; a record that will not clear must not stop the start."""
    try:
        await asyncio.to_thread(update_next_start.finish)
    except Exception:
        logger.warning("Could not clear the armed update record; starting anyway", exc_info=True)


async def _apply_armed_update_at_boot() -> bool:
    """Apply the armed update, if any. ``True`` only when a re-exec was attempted and returned.

    Never raises: a failure here must not stop the gateway from starting on the
    version it has. A successful re-exec does not return.
    """
    from kiro_crew import __version__ as running

    try:
        state = await asyncio.to_thread(update_next_start.read)
    except Exception:
        logger.warning("Armed update unreadable; starting without applying", exc_info=True)
        return False
    if not state.armed:
        if state.pending_apply_error and state.error_version and state.error_version != running:
            # The failure was about the version that ran then. The install moved
            # since (the person ran `kirocrew update` themselves), so the notice
            # would otherwise outlive the state it describes.
            await asyncio.to_thread(update_next_start.clear_error)
        return False
    if not state.armed_version:
        # No arm names a version any more; one left by an older build would install
        # without the person having been offered anything.
        logger.warning("Dropping an armed update that names no version")
        await _clear_arm_best_effort()
        return False
    # A git checkout can be behind its upstream with no version bump, so its arm
    # carries the distance and an equal version is not "already running" for it.
    git_ahead = state.armed_behind > 0
    headless_desktop = _headless_desktop()
    # Order decides only where the desktop installer applies: there an older arm is
    # always stale (that path never downgrades), and the arm names the feed's SemVer
    # while this build reports its PEP 440 form. Elsewhere a policy provider's check
    # may name an older version on purpose (a rollback), and that arm still applies,
    # so only equality clears it.
    if headless_desktop:
        reached = _desktop_running_reaches(running, state.armed_version)
    else:
        reached = not git_ahead and (
            _running_reaches(running, state.armed_version)
            if hosted_by_desktop()
            else running == state.armed_version
        )
    if hosted_by_desktop() and not reached:
        logger.info("Update armed (%s): the desktop app owns the apply", state.armed_version)
        return False
    # The desktop applies on quit with this gateway already gone, so the record it
    # armed here is cleared by the first start that runs the version it named.
    if reached:
        logger.info(
            "Armed update %s is already running (%s); nothing to apply",
            state.armed_version,
            running,
        )
        await _clear_arm_best_effort()
        if headless_desktop:
            await asyncio.to_thread(_forget_desktop_hand_off)
        return False
    if headless_desktop:
        return await _hand_off_desktop_install(state, running)
    if state.attempts >= update_next_start.MAX_BOOT_ATTEMPTS:
        logger.error(
            "Armed update %s gave up after %d boot attempts", state.armed_version, state.attempts
        )
        await asyncio.to_thread(
            lambda: update_next_start.finish(
                error=f"The update to {state.armed_version or 'the new version'} did not complete "
                f"after {state.attempts} starts; run `kirocrew update`."
            )
        )
        return False

    try:
        await asyncio.to_thread(update_next_start.note_boot_attempt)
        installed = await asyncio.to_thread(update_next_start.installed_version)
        moved = bool(installed) and installed != running
        if moved and (not state.armed_version or installed == state.armed_version):
            logger.info(
                "Update armed (%s): installed %s, restarting onto it",
                state.armed_version,
                installed,
            )
        else:
            logger.info(
                "Update armed (%s), running %s: applying before the gateway serves",
                state.armed_version or "-",
                running,
            )
            await asyncio.to_thread(update_next_start.mark_applying)
            try:
                ok, tail = await _run_apply_child()
            finally:
                await asyncio.to_thread(update_next_start.clear_applying)
            logger.log(logging.INFO if ok else logging.ERROR, "Boot-time update output: %s", tail)
            if not ok:
                reason = _failure_reason(tail)
                await asyncio.to_thread(lambda: update_next_start.finish(error=reason))
                return False
            installed = await asyncio.to_thread(update_next_start.installed_version)
            moved = bool(installed) and installed != running
        # Cleared BEFORE the exec: a restart that lands on this version again must
        # not apply again, for ever.
        await asyncio.to_thread(update_next_start.finish)
        if not moved and not git_ahead:
            logger.info("Update applied, and this install already runs it")
            return False
        launcher, exe = await _resolve_exec_target()
        if launcher is None and exe is None:
            await asyncio.to_thread(
                lambda: update_next_start.finish(
                    error="The update installed, but no usable interpreter was found to start it."
                )
            )
            return False
        try:
            _reexec(launcher, exe)
        except OSError as exc:
            logger.error("Re-exec onto the updated install failed: %s", exc)
            message = f"The update installed but did not start: {exc}"
            await asyncio.to_thread(lambda: update_next_start.finish(error=message))
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Boot-time update apply failed")
        message = str(exc) or type(exc).__name__
        try:
            await asyncio.to_thread(lambda: update_next_start.finish(error=message))
        except Exception:
            logger.debug("could not record the apply error", exc_info=True)
        return False
