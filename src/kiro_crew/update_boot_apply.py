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
    from kiro_crew.security import redact

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
    text = redact((out or b"").decode("utf-8", "replace")).strip()
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
    if hosted_by_desktop():
        logger.info("Update armed (%s): the desktop app owns the apply", state.armed_version or "-")
        return False
    # A git checkout can be behind its upstream with no version bump, so its arm
    # carries the distance and an equal version is not "already running" for it.
    git_ahead = state.armed_behind > 0
    if state.armed_version and state.armed_version == running and not git_ahead:
        logger.info("Armed update %s is already running; nothing to apply", running)
        await asyncio.to_thread(update_next_start.finish)
        return False
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
