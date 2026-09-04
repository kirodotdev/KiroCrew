"""Cross-platform "keep the host awake while work is running" inhibitor.

A single :class:`SleepInhibitor` engages an OS-level sleep block while the agent
is running a task and releases it when idle. It is driven by the gateway's
prevent-sleep poll (see ``dashboard/server.py``) and gated by the
``dashboard.prevent_sleep`` config flag, which is OFF by default.

Backends, one per platform, all best-effort (a failure never raises into the
caller — the machine simply keeps its normal sleep behavior):

* **macOS** — a ``caffeinate -i -w <gateway_pid>`` subprocess. ``-i`` blocks idle
  *system* sleep (not the display) for as long as the process lives, and ``-w``
  makes it exit automatically when the gateway PID dies, so a hard gateway crash
  cannot leave the machine permanently awake.
* **Linux** — a ``systemd-inhibit --what=idle:sleep --mode=block`` subprocess
  whose held command polls the gateway PID (``kill -0``) and exits when it is
  gone, giving the same crash-safe auto-release. No-op on a host without
  ``systemd-inhibit`` (non-systemd distro).
* **Windows** — ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``
  via ctypes on engage and ``ES_CONTINUOUS`` alone to release. The OS clears the
  request automatically when the process exits, so there is nothing to leak.

The inhibitor is idempotent: ``set_active(True)`` twice engages once, and it
re-spawns a POSIX helper that has died so a long task cannot silently start
sleeping mid-run.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from typing import Optional

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

# Windows SetThreadExecutionState flags (winbase.h). ES_CONTINUOUS makes the
# request persist until the next call rather than applying to a single check;
# ES_SYSTEM_REQUIRED forbids idle *system* sleep (the display is left alone, to
# match caffeinate -i / systemd-inhibit idle:sleep).
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

# How often the Linux PID-watch child checks that the gateway is still alive.
# Short enough that the inhibitor lock is released promptly after a crash, long
# enough to be negligible overhead.
_LINUX_PID_WATCH_INTERVAL_SECS = 15

_DEFAULT_REASON = "Kiro Crew is running a task"

# Consecutive immediate-exit respawns of a POSIX helper before the inhibitor
# gives up until the next idle boundary. Guards a host where the helper spawns
# but dies at once (e.g. `systemd-inhibit --mode=block` with no logind session /
# polkit authorization): without a cap the poll would fork a doomed process
# every cycle for the whole turn while falsely reporting "engaged".
_MAX_HELPER_RESPAWNS = 3

# Keep-awake helpers are resolved from these fixed, system-owned absolute paths
# and NEVER via PATH (shutil.which). The poll launches the helper on the gateway
# event loop WITHOUT sandboxing, so a PATH-planted `caffeinate`/`systemd-inhibit`/
# `sh`/`sleep` shim (an agent can write to a PATH dir like a venv/`~/.local/bin`)
# would otherwise be executed with full gateway privileges. `/bin/sh` and the
# interpolated absolute `sleep` are likewise fixed so systemd-inhibit's child and
# the PID-watch loop resolve nothing through PATH (`kill` is a POSIX sh builtin).
_CAFFEINATE_PATHS = ("/usr/bin/caffeinate",)
_SYSTEMD_INHIBIT_PATHS = ("/usr/bin/systemd-inhibit", "/bin/systemd-inhibit")
_SLEEP_PATHS = ("/usr/bin/sleep", "/bin/sleep")
_SH_PATH = "/bin/sh"


def _resolve_trusted(paths: "tuple[str, ...]") -> Optional[str]:
    """Return the first existing, executable absolute path, or None.

    Deliberately does not consult PATH: see ``_CAFFEINATE_PATHS`` — the helper
    runs unsandboxed with gateway privileges, so only system-owned absolute
    locations are trusted (a PATH-planted shim must never be launchable).
    """
    for p in paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def backend_name() -> str:
    """Human-readable name of the active platform backend (for logs/tests)."""
    if platform_compat.IS_MACOS:
        return "caffeinate"
    if platform_compat.IS_LINUX:
        return "systemd-inhibit"
    if platform_compat.IS_WINDOWS:
        return "SetThreadExecutionState"
    return "none"


def _spawn_posix_inhibitor(reason: str) -> Optional["subprocess.Popen[bytes]"]:
    """Spawn the macOS/Linux keep-awake helper, or return None when unavailable.

    Both helpers watch the gateway PID so a hard crash of this process
    auto-releases the block instead of leaving the machine permanently awake.
    ``start_new_session`` isolates the child into its own process group so the
    group-kill on release reaps it (and its ``sh`` grandchild on Linux) cleanly.
    """
    pid = os.getpid()
    if platform_compat.IS_MACOS:
        exe = _resolve_trusted(_CAFFEINATE_PATHS)
        if not exe:
            return None
        argv = [exe, "-i", "-w", str(pid)]
    elif platform_compat.IS_LINUX:
        exe = _resolve_trusted(_SYSTEMD_INHIBIT_PATHS)
        sleep_exe = _resolve_trusted(_SLEEP_PATHS)
        if not exe or not sleep_exe:
            return None
        # systemd-inhibit holds the lock only while its command runs, so the
        # command is a PID watch that exits the instant the gateway is gone.
        # `sleep` is interpolated as an absolute path (not PATH-resolved by sh);
        # `kill` is a POSIX sh builtin, so it is not resolved through PATH.
        argv = [
            exe,
            "--what=idle:sleep",
            "--who=Kiro Crew",
            f"--why={reason}",
            "--mode=block",
            _SH_PATH,
            "-c",
            f"while kill -0 {pid} 2>/dev/null; "
            f"do {sleep_exe} {_LINUX_PID_WATCH_INTERVAL_SECS}; done",
        ]
    else:
        return None
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
    )


def _terminate(proc: "subprocess.Popen[bytes]") -> bool:
    """Kill a POSIX inhibitor helper's process tree if it is still running.

    The helper was spawned with ``start_new_session`` (its own process group),
    so a group-kill is required: on Linux ``systemd-inhibit`` forks the ``sh``
    PID-watch as a child, and signalling only the parent PID would release the
    inhibit lock but orphan that ``sh`` loop (it watches the still-live gateway
    PID, so it never exits on its own) — one leaked process per release cycle.

    Returns True when the helper is gone (already exited, or killed now), and
    False only when a real signal error left it running — so the caller keeps
    the handle and retries on the next idle poll / shutdown instead of leaking
    a live helper that holds sleep inhibited for the gateway's lifetime.
    """
    if proc.poll() is not None:
        return True
    try:
        platform_compat.kill_process_tree(proc.pid)
        return True
    except ProcessLookupError:
        return True  # exited between the poll and the signal — already gone
    except OSError:
        logger.debug("sleep inhibitor terminate failed", exc_info=True)
        return False


def _set_windows_execution_state(keep_awake: bool) -> bool:
    """Apply (or clear) the Windows keep-awake execution-state request.

    Returns True when the call succeeded. Must be invoked from the same thread
    across engage/release (the gateway event-loop thread), since the request is
    thread-scoped and persists via ES_CONTINUOUS until reset or the thread exits.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint]
        kernel32.SetThreadExecutionState.restype = ctypes.c_uint
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if keep_awake else 0)
        # A zero return means the request failed (no previous state to report).
        return kernel32.SetThreadExecutionState(flags) != 0
    except Exception:
        logger.debug("SetThreadExecutionState failed", exc_info=True)
        return False


class SleepInhibitor:
    """Idempotent OS sleep block. Call :meth:`set_active` to engage/release.

    Not thread-safe: the gateway drives it from a single poll coroutine on the
    event loop. All engage/release work is best-effort and swallows errors so a
    missing backend or a spawn failure degrades to "normal sleep behavior"
    rather than breaking the poll.
    """

    def __init__(self, reason: str = _DEFAULT_REASON) -> None:
        self._reason = reason
        self._active = False
        self._proc: Optional["subprocess.Popen[bytes]"] = None  # POSIX helper
        self._win_applied = False  # whether the Windows request is currently set
        self._warned_no_backend = False  # log the "no backend" notice only once
        self._consecutive_deaths = 0  # POSIX helper immediate-exit streak
        # Give up engaging until the next release (idle boundary). Set when the
        # POSIX helper keeps dying immediately or no backend binary exists, so a
        # long busy period does not re-probe PATH / re-fork a doomed helper every
        # poll. A release (turn end) re-arms us so the next turn retries.
        self._disabled_until_idle = False

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        """Engage the block when *active*, release it otherwise. Idempotent."""
        if not active:
            self._release()
            return
        if self._active:
            # Already engaged — on POSIX, make sure the helper is still alive.
            self._verify_alive()
            return
        if self._disabled_until_idle:
            # Gave up this busy period (repeated deaths / no backend); a release
            # re-arms us. Avoids re-probing PATH or re-forking every poll.
            return
        self._engage()

    def _verify_alive(self) -> None:
        """Respawn a dead POSIX helper, but give up if it keeps dying at once.

        The helper is a child process that can exit (killed, or its watched PID
        reused), in which case a long task would silently start sleeping while
        the flag still reads "on"; so respawn it. But a helper that exits
        *immediately* every time (no keep-awake backend actually engaged) must
        not be re-forked every poll — cap the streak and stand down until idle.
        """
        if not platform_compat.IS_POSIX or self._proc is None:
            return
        if self._proc.poll() is None:
            self._consecutive_deaths = 0  # healthy — reset the streak
            return
        self._proc = None
        self._active = False
        self._consecutive_deaths += 1
        if self._consecutive_deaths > _MAX_HELPER_RESPAWNS:
            self._disabled_until_idle = True
            logger.warning(
                "Sleep prevention helper (%s) exited immediately %d times; giving up "
                "until the next idle period",
                backend_name(),
                self._consecutive_deaths,
            )
            return
        logger.debug("sleep inhibitor helper had exited; respawning")
        self._engage()

    def _engage(self) -> None:
        ok = False
        try:
            if platform_compat.IS_WINDOWS:
                # Only record the applied state on success. A failed engage must
                # not clear a still-active request left set by a prior failed
                # release — that would strand the request set (machine never
                # sleeps) with nothing tracking it to clear on the next release.
                if _set_windows_execution_state(keep_awake=True):
                    self._win_applied = True
                    ok = True
            else:
                self._proc = _spawn_posix_inhibitor(self._reason)
                ok = self._proc is not None
                if not ok:
                    # No backend binary on this host — stable for the process
                    # lifetime, so latch to stop re-probing PATH every poll. A
                    # release re-arms us (cheap re-check once per idle boundary).
                    self._disabled_until_idle = True
        except Exception:
            logger.debug("sleep inhibitor engage failed", exc_info=True)
            ok = False
        if ok:
            self._active = True
            logger.info("Sleep prevention engaged (%s)", backend_name())
        elif not self._warned_no_backend:
            self._warned_no_backend = True
            logger.info(
                "Sleep prevention requested but no keep-awake backend is available "
                "on this platform (%s)",
                backend_name(),
            )

    def _release(self) -> None:
        was_active = self._active
        try:
            if platform_compat.IS_WINDOWS:
                if self._win_applied:
                    # Only clear the flag if the OS actually cleared the request;
                    # a failed clear leaves the request set, so keep tracking it.
                    self._win_applied = not _set_windows_execution_state(keep_awake=False)
            elif self._proc is not None:
                # Keep the handle if the kill genuinely failed, so the next idle
                # poll or shutdown can retry rather than leaking a live helper.
                if _terminate(self._proc):
                    self._proc = None
        except Exception:
            logger.debug("sleep inhibitor release failed", exc_info=True)
        self._active = False
        # A release is an idle boundary: re-arm engagement so the next turn
        # retries even if we gave up (repeated deaths / no backend) last period.
        self._disabled_until_idle = False
        self._consecutive_deaths = 0
        if was_active:
            logger.info("Sleep prevention released")
