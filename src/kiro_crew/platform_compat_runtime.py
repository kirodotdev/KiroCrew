"""The interpreter and process image Kiro Crew runs as.

The UTF-8 environment and console, the re-exec and exit seams, the process alarm
and the boot and awake clocks, the open-file limit, ``strftime``, interpreter
discovery and the launcher hops, the desktop bundle's interpreter, the isolated argv
a Python child is spawned with, and Windows on Arm.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import asyncio
import ctypes.util
import io
import logging
import ntpath
import os
import platform
import shutil
import signal
import site
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from kiro_crew.executors import subprocess_executor
from kiro_crew.platform_compat_native import IS_POSIX, IS_WINDOWS
from kiro_crew.subprocess_utf8 import UTF8_TEXT

if IS_POSIX:
    import resource

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


_UTF8_PROCESS_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8:backslashreplace",
}


def _ensure_utf8_process_environment() -> None:
    """Pin UTF-8 for Python successors and child processes on every platform.

    ``sys.stdout.reconfigure`` can repair the current process, but Windows
    implements ``os.execv`` by creating a successor process.  Its standard
    streams are constructed before Kiro Crew code runs, so the encoding must be
    present in the environment at interpreter startup.  POSIX ``execv`` keeps
    the current environment, where an inherited ``PYTHONIOENCODING`` can also
    override the platform's normal UTF-8 defaults.  Overwrite inherited settings
    deliberately: Kiro Crew's process tree emits Unicode as part of its normal
    protocols and boot output.
    """
    os.environ.update(_UTF8_PROCESS_ENV)


#: Variables this process took out of its own environment so its descendants do
#: not inherit them, but that its OWN exec successor must see again. Each
#: ``reexec_*`` puts them back right before the exec.
_KEPT_FOR_REEXEC: dict[str, str] = {}


def keep_for_reexec(name: str, value: str) -> None:
    """Hand *name* back to this process's exec successor, and only to it."""
    _KEPT_FOR_REEXEC[name] = value


def kept_for_reexec() -> dict[str, str]:
    return dict(_KEPT_FOR_REEXEC)


#: The one module whose work must not outlive this process image: the
#: managed-venv update apply, whose build child would otherwise keep writing a
#: tree after the process that supervised it is gone.
_WHEEL_APPLY_MODULE = "kiro_crew.platform.wheel_apply"


def cancel_wheel_applies_in_flight(reason: str) -> None:
    """Cancel every managed-venv apply this process is running, if any can be.

    Called by both exec seams and :func:`hard_exit`, and directly by a shutdown
    that wants the apply stopped before its own teardown starts. The apply
    module is looked up in ``sys.modules`` and never imported: no apply can be
    in flight in a process that never loaded it, so an unloaded module costs
    nothing, and the exit paths must not import from a tree an update may have
    changed. Its cancel is synchronous and quick; one that raises never stops
    the exit or exec that called this.
    """
    module = sys.modules.get(_WHEEL_APPLY_MODULE)
    if module is None:
        return
    try:
        module.cancel_wheel_applies(reason)
    except Exception:
        pass


def hard_exit(code: int) -> None:
    """End the process at once, after cancelling any apply, skipping teardown.

    The one spelling for an ``os._exit`` that is not the gateway's own final
    exit: the owner's ``/kirocrew restart``, the second-signal force exit. Same
    contract as the exec seams: a managed-venv apply in flight does not outlive
    this one.
    """
    cancel_wheel_applies_in_flight("exit")
    os._exit(code)


def _disarm_process_alarm_before_exec() -> None:
    """Cancel any pending process alarm before ``execv`` replaces this image.

    ``execve`` preserves interval timers (``ITIMER_REAL`` included) and resets
    every caught signal to its default disposition.  A dump-then-exit deadline
    the loop-stall watchdog armed in this image would therefore reach the
    successor as a default-action ``SIGALRM`` it never armed -- during its own
    boot, before its watchdog exists to replace the deadline -- and end it with
    no dump and no log line.  No process-wide deadline may outlive the image
    that armed it, so both exec seams cancel it here, with no await between the
    cancel and the exec.  A no-op where the timer does not exist (Windows).
    """
    arm_process_alarm(0.0)


def reexec_launcher(launcher: str, args: Sequence[str]) -> None:
    """Re-enter a validated stable launcher, preserving its dispatch pathname.

    The launcher, not the core, replaces version-specific environment values.
    Windows execv joins its arguments without quoting, so quote each token for
    the native CRT parser. POSIX receives the original argument vector directly.
    The pending loop-stall alarm is cancelled immediately before the exec (see
    :func:`_disarm_process_alarm_before_exec`).
    """
    _ensure_utf8_process_environment()
    os.environ.update(_KEPT_FOR_REEXEC)
    argv = [launcher, *args]
    if IS_WINDOWS:
        argv = [subprocess.list2cmdline([arg]) for arg in argv]
    cancel_wheel_applies_in_flight("exec")
    _disarm_process_alarm_before_exec()
    os.execv(launcher, argv)


def reexec_python_module(module: str, args: Sequence[str], executable: str | None = None) -> None:
    """Replace this process with ``<executable> -m module``.

    ``executable`` defaults to ``sys.executable``. A caller restarting after a
    managed-venv promotion passes the STABLE-LINK interpreter instead: the
    cached ``sys.executable`` resolves into the superseded versioned tree
    (still on disk), so exec'ing it would silently resurrect the old version.

    Windows reconstructs an ``execv`` command line from ``argv`` and reparses
    it in the child.  A full ``argv[0]`` containing spaces is split before the
    module flag, so Python treats the path suffix as a script name.  The
    executable path passed separately to ``execv`` still selects the exact
    interpreter; only its display name needs to be space-free.  The pending
    loop-stall alarm is cancelled immediately before the exec (see
    :func:`_disarm_process_alarm_before_exec`).
    """
    # Publish UTF-8 before exec so in-app gateway restarts (Tailnet, update,
    # stale-assets, explicit restart) cannot create a successor that inherits a
    # Windows ANSI stream or a hostile POSIX PYTHONIOENCODING and crashes on the
    # first emoji printed during boot.
    _ensure_utf8_process_environment()
    os.environ.update(_KEPT_FOR_REEXEC)
    resolved = executable or sys.executable
    argv0 = ntpath.basename(resolved) if IS_WINDOWS else resolved
    # ``-P``: the successor inherits this process's cwd -- the home directory
    # for a service-launched gateway -- and ``-m`` would put it first on
    # sys.path, ahead of the standard library, so a stdlib-named directory
    # there would shadow the stdlib in the restarted process.
    argv = isolated_python_argv("-P", "-m", module, *args, executable=resolved)
    argv[0] = argv0
    cancel_wheel_applies_in_flight("exec")
    _disarm_process_alarm_before_exec()
    os.execv(resolved, argv)


def execv_target_available(path: str) -> bool:
    """Whether ``execv`` has a file here it may attempt. Two metadata syscalls.

    Distinct from ``is_executable_file`` (in ``platform_compat_files``), which answers a different
    question: that one decides whether to treat a file as a runnable HOOK, and on
    Windows it accepts a regular file by extension because there is no execute bit
    to read. A restart target is not a hook -- the process image itself depends on
    the answer, and an extension decides nothing -- so this asks only the two things
    the kernel will also ask, and reports on Windows whatever ``os.access`` does
    there.

    Kept beside the exec family because callers on the event loop must hand BOTH
    syscalls to a worker thread in ONE hop: a pathname on a stalled network mount
    blocks each of them, so offloading one and leaving the other inline still
    freezes the loop.
    """
    return os.path.isfile(path) and os.access(path, os.X_OK)


#: Exit status for a process replacement that failed after the final drain. The
#: generic failure code, matching the gateway's other unrecoverable exits; the
#: distinguishing signal is the CRITICAL log line, not a private number a
#: supervisor would have to be taught.
_POST_DRAIN_EXEC_FAILURE_EXIT = 1

# Ceiling on the event log's exit flush, covering the wait for a worker as well as the
# work. ``eventlog_hooks.drain_for_shutdown`` bounds the work itself at
# SHUTDOWN_DRAIN_SECONDS (5.0s); what no inner ceiling can bound is the queue wait for
# a thread to run on. Derived as that inner bound plus one second, the same rule
# ``cli.drain_log_queue_before_hard_exit`` applies to its own flush, so the two exit
# drains do not drift apart on a number nobody chose.
_EXIT_FLUSH_DEADLINE_SECS = 6.0


async def exit_after_failed_restart_exec(target: str | None) -> None:
    """Exit this process after a restart exec failed past the point of no return.

    Lives beside the two ``reexec_*`` functions because it is their partner: it
    answers the one outcome neither of them can, and a future reader editing
    either exec sees it here.

    Both update-restart paths reach their exec only after ``close_all()``, which
    makes the exec the last reversible instruction they have. Every session is
    already torn down, the final history save already ran, and the tree on disk
    is already the NEW version while this process image is still the old one.

    Callers validate the target BEFORE the drain, and that is what keeps the
    ordinary failures out of here -- but validating cannot make an exec
    infallible. The target can be replaced between the check and the call, and a
    file that is present and carries the exec bit can still be the wrong
    architecture, a truncated image, or a script whose interpreter is missing.
    The kernel is the authority on every one of those and reports them as
    ``OSError`` from ``execv`` itself, so the exec site is the only place they
    can be answered -- and answering them by returning is what left a gateway
    alive with nothing to serve.

    Answer by exiting. A surviving process here serves nothing it can serve
    honestly: its sessions are gone, and its code and the install on disk are
    different versions.
    It also holds the port, so the operator's own relaunch would fail to bind on
    top of it. Exiting makes the failure visible to whatever started this
    gateway, frees the port for that relaunch, and cannot serve the version skew.
    Reopening admission instead would not substitute: the sessions closed by
    ``close_all()`` do not come back, and the skew would then be served.

    Exits through ``os._exit``, which runs no ``atexit`` handler -- so the two
    bounded flushes the force-exit signal handler performs are repeated here for
    the same reason: the CRITICAL line is the whole diagnosis, and it is queued,
    not yet on disk. Both are best-effort; a wedged disk must delay this exit,
    never hold it. Unlike that signal handler, which cannot await, both run on a
    worker thread: waiting for a wedged log write on the event loop would hold every
    remaining task -- and the port this exit exists to release -- for the length of
    their own timeouts.

    Off-loop alone does not make the wait bounded, so the event log's flush also
    carries a deadline and runs on the dedicated subprocess pool rather than the
    default executor. ``asyncio.to_thread`` queues against a pool every other
    ``to_thread`` in the process shares: saturated, the await never resumes and the
    exit this function exists to perform simply never happens -- the loop is free,
    which is what the inner ceiling guarantees, but the process still serves nothing
    on a port it never releases. A deadline around the flush, plus a pool kept
    separate for calls that can block on a wedged kernel resource, is what makes "a
    wedged disk must delay this exit, never hold it" true of the wait and not only of
    the work.

    The queued SEL audit tail is drained first, then the ``gateway.log`` tail --
    the order :mod:`kiro_crew.slack.events` uses, so a SEL write failure's own log
    line still reaches a live listener. Neither is spelled out again here:
    :func:`kiro_crew.sel.flush_audit_queue_before_hard_exit` and
    :func:`kiro_crew.cli.drain_log_queue_before_hard_exit` are the shared async
    hard-exit drains for those two queues -- same pool, each its own outer
    deadline, and neither raises -- so every hard-exit path keeps one spelling and
    one ceiling for each.
    """
    logger.critical(
        "Gateway restart could not replace this process (target %r); exiting instead of "
        "serving with every session closed and an install this image does not match. "
        "Repair the install and start the gateway again.",
        target or sys.executable,
        exc_info=True,
    )
    from kiro_crew.cli import drain_log_queue_before_hard_exit
    from kiro_crew.sel import flush_audit_queue_before_hard_exit

    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _drain_event_log_for_exit
            ),
            timeout=_EXIT_FLUSH_DEADLINE_SECS,
        )
    except Exception:
        # Also the deadline: TimeoutError is an Exception, and a fatal exit must never
        # be blocked by bookkeeping or logging.
        pass
    # The queued SEL audit tail -- the restart's own security-of-record event among
    # it -- dies with the os._exit below unless it is drained first. This is a gateway
    # hard exit like the normal shutdown, so it drains SEL
    # the same bounded way. Flush SEL BEFORE the gateway.log tail (the order
    # ``slack/events_slash.py`` already uses): a SEL write failure logs its "dropped N events"
    # line, and draining gateway.log first would stop the listener that line needs, so
    # the audit loss would leave no trace. The shared helper offloads to this pool under
    # its own deadline and never raises.
    await flush_audit_queue_before_hard_exit()
    # The gateway.log tail has its own async hard-exit drain, which already offloads to
    # this pool under its own deadline and already never raises. Calling it keeps one
    # spelling and one ceiling for that queue across every hard-exit path.
    await drain_log_queue_before_hard_exit()
    os._exit(_POST_DRAIN_EXEC_FAILURE_EXIT)


def _drain_event_log_for_exit() -> None:
    """Flush the member event log's queue. Off-loop; bounded inside the module."""
    from kiro_crew import eventlog_hooks

    eventlog_hooks.drain_for_shutdown()


#: ARM machine strings as ``platform.machine()`` spells them on Windows.
#: ``ARM64`` is what a native arm64 interpreter reports; ``AARCH64`` is accepted
#: because that spelling reaches Windows through cross-built and MSYS/Cygwin
#: Pythons. Compared case-folded, so the casing here is documentation only.
_WINDOWS_ARM_MACHINES: frozenset[str] = frozenset({"arm64", "aarch64"})


def is_windows_on_arm() -> bool:
    """True when this interpreter is a NATIVE ARM64 process on Windows.

    Deliberately a property of the running PROCESS, not of the host CPU, because
    every caller cares about which wheel tags pip will accept here. Windows on ARM
    runs x86-64 processes under emulation, and in one of those ``platform.machine()``
    reports ``AMD64`` — correctly, since such an interpreter installs ``win_amd64``
    wheels and works fine. A host-architecture probe would report ARM for that same
    process and wrongly refuse a package that installs.

    Keyed off :data:`IS_WINDOWS` rather than ``platform.system()`` so there is one
    canonical Windows predicate in this module instead of two that can drift.
    """
    return IS_WINDOWS and platform.machine().casefold() in _WINDOWS_ARM_MACHINES


# ── Desktop-app bundled interpreter detection ──
#: Directory name the desktop build stages the bundled python-build-standalone
#: runtime under (``Resources/backend-dist/…`` inside the app bundle). The
#: authoritative spellings live in the packaging layer — electron-builder's
#: ``extraResources`` mapping in ``website/electron/package.json`` and the
#: staging steps in ``packaging/build-desktop.sh`` — and this constant MUST
#: match them: ``test/test_platform_compat_pinned_tools.py`` pins the two together so
#: a packaging rename breaks a test instead of a runtime guarantee.
BUNDLED_BACKEND_DIST_DIRNAME: str = "backend-dist"


def is_bundled_interpreter() -> bool:
    """Return True when this process runs on the desktop app's bundled interpreter.

    Contract: the desktop build ships a python-build-standalone runtime inside
    the application bundle, always under a ``backend-dist`` path component
    (see :data:`BUNDLED_BACKEND_DIST_DIRNAME`). On macOS that bundle is
    code-signed, so anything that would write into the interpreter's tree —
    most notably ``pip install`` into its site-packages — invalidates the
    signature and breaks subsequent launches/updates, and the write is
    discarded on every app update anyway. Callers use this predicate to refuse
    such writes loudly.

    This is the ONE place the packaging layout's directory name is interpreted
    at runtime; never re-inline the sentinel at a call site.
    """
    return BUNDLED_BACKEND_DIST_DIRNAME in Path(sys.executable).resolve().parts


_PYTHON_OPTIONS_WITH_VALUES: frozenset[str] = frozenset({"-W", "-X", "--check-hash-based-pycs"})


def isolated_python_argv(
    *args: str,
    executable: str | os.PathLike[str] | None = None,
    force_isolation: bool = False,
) -> list[str]:
    """Argv for a Kiro Crew-owned Python child with bundle-safe user-site policy.

    ``-s`` is the narrow control: it removes the user site while preserving
    ``PYTHONPATH``, the working-directory import path, and every other
    environment setting internal app and MCP launchers rely on. A caller that
    already chose ``-I`` keeps that stronger contract unchanged. Non-bundled
    parents that currently allow the user site preserve that policy because
    Kiro Crew itself may have been installed there. Only callers that supply
    the child's import path through ``PYTHONPATH`` or a script/entry path may
    set ``force_isolation``. This helper is for Kiro Crew-owned processes and
    installers, not user scripts or project tools.
    """
    resolved = os.fspath(executable or sys.executable)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--" or arg.startswith(("-m", "-c")) or not arg.startswith("-"):
            break
        if arg in ("-s", "-I") or (
            arg.startswith("-")
            and not arg.startswith(("--", "-W", "-X"))
            and ("s" in arg[1:] or "I" in arg[1:])
        ):
            return [resolved, *args]
        i += 2 if arg in _PYTHON_OPTIONS_WITH_VALUES else 1
    if not force_isolation and not is_bundled_interpreter() and site.ENABLE_USER_SITE is True:
        return [resolved, *args]
    return [resolved, "-s", *args]


def ensure_utf8_console() -> None:
    """Keep Kiro Crew's process tree UTF-8 and repair current Windows streams.

    Kiro Crew prints non-ASCII glyphs throughout its CLI/gateway output. On
    Windows the default console code page is cp1252, and when stdout is a pipe
    (e.g. the gateway launched detached with redirected output, or under the
    KiroCrewHub client) Python encodes prints as cp1252 — so the FIRST non-ASCII
    print raises ``UnicodeEncodeError: 'charmap' codec can't encode character``
    and the process dies before the gateway binds.  On every platform, publish
    the encoding contract for later re-exec and child processes; an inherited
    ``PYTHONIOENCODING`` can otherwise override POSIX UTF-8 defaults too.  On
    Windows, best-effort reconfigure (Python 3.7+) the current streams to UTF-8
    with backslashreplace so a stray un-encodable char degrades to an escape
    instead of crashing. Idempotent and safe to call once at startup.
    """
    _ensure_utf8_process_environment()
    if not IS_WINDOWS:
        return
    # Repair the current streams below, and separately make the invariant
    # inheritable by MCP/session children and any later re-exec.  Environment
    # variables affect the next interpreter at construction time; setting them
    # here is intentional even though they cannot retroactively rebuild the
    # current process's streams.
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:  # pythonw / fully detached — no stream to fix
            continue
        # Preferred: reconfigure in place (Python 3.7+ TextIOWrapper). Works for a
        # normal console or a redirect-to-file when the stream is a TextIOWrapper.
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            if (getattr(stream, "encoding", "") or "").lower().startswith("utf-8"):
                continue
        except (AttributeError, ValueError, OSError):
            # reconfigure() is absent or refused (e.g. the stream got replaced
            # with a plain object somewhere up a multi-process launch chain —
            # observed in the 3-layer Windows gateway spawn: kirocrew.exe launcher
            # -> venv python stub -> base python worker, where the worker's stderr
            # is NOT a reconfigure-able TextIOWrapper, so emoji log records crash
            # the stderr StreamHandler with UnicodeEncodeError under cp1252).
            pass
        # Fallback: wrap the underlying binary buffer in a fresh UTF-8 writer so
        # the encoding is guaranteed regardless of the original stream type.
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        try:
            setattr(
                sys,
                name,
                io.TextIOWrapper(
                    buffer, encoding="utf-8", errors="backslashreplace", line_buffering=True
                ),
            )
        except (AttributeError, ValueError, OSError):
            pass


def ensure_line_buffered_stdout() -> None:
    """Make every ``print()`` reach a non-terminal stdout as it is printed.

    CPython line-buffers ``sys.stdout`` only when it is attached to a terminal.
    A pipe or a file -- the journal socket under systemd, the launchd stdout
    file, the Desktop supervisor's log descriptor, a detached gateway's own
    ``gateway.log`` -- gets a block buffer that drains when it fills or when
    the interpreter exits cleanly, so a status line printed at boot surfaces
    hours late, stamped with the stop time, or not at all after a kill or an
    ``os.execv`` (which flushes nothing).  Switching the stream to line
    buffering once is the single seam every status print goes through; the
    lines themselves and their destination do not change.

    A terminal is left untouched: the interpreter already line-buffers it, and
    skipping ``reconfigure`` there spares even its flush.  ``sys.stderr`` is
    not touched either, because CPython line-buffers it whatever it is
    attached to, and :func:`ensure_utf8_console`'s fallback wrapper keeps it
    that way.

    Best-effort: a stream that is absent (``pythonw``), closed, or not a
    ``TextIOWrapper`` (a test's ``StringIO``, a plain object left by a launcher
    up a multi-process spawn chain) is left as it is.  Boot never fails over
    its console.
    """
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        if stream.isatty():
            return
        stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass


def _is_windows_store_python_stub(path: str) -> bool:
    """True if *path* is the Microsoft Store ``python`` App Execution Alias stub.

    On Windows, ``shutil.which("python"/"python3")`` resolves a 0-byte reparse
    point under ``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` when no real CPython
    is installed/on PATH. Spawning it does NOT run Python — it prints "Python was
    not found; run without arguments to install from the Microsoft Store…" and
    exits 9009. Detect it by its WindowsApps location so callers never execute
    it (which otherwise floods logs on every probe). Mirrors install.ps1's
    Find-RealPython, which rejects the same stub.
    """
    if not IS_WINDOWS:
        return False
    norm = path.replace("/", "\\").lower()
    return "\\microsoft\\windowsapps\\" in norm


def find_python_interpreter(reject: Optional[Callable[[str], bool]] = None) -> str | None:
    """Resolve a real CPython >= 3.12 interpreter, or None.

    Single source of truth for "where is a usable system python" on every
    platform. Prefers the exact tested version (``python3.12``), then bare
    ``python``/``python3``, with free-threaded-prone ``python3.13`` LAST so a
    usable 3.12 wins first. Rejects
    Brazil-path/build interpreters and — critically on Windows — the Microsoft
    Store alias stub (see :func:`_is_windows_store_python_stub`): running that
    stub is what emits the "Python was not found" nag, so we must never spawn it.

    ``reject`` is an optional predicate run against each >= 3.12 candidate path;
    return True to skip it and FALL THROUGH to the next candidate (not abort).
    Callers with extra constraints the shared resolver can't express — e.g. the
    STT prereq probe needs pip and a non-free-threaded build — pass it here so a
    single unusable interpreter does not short-circuit the whole search.

    Returns the interpreter path, or None when none is usable. Callers that just
    need *an* interpreter to re-exec Kiro Crew itself should prefer
    ``sys.executable``; this is for finding a SEPARATE system python (e.g. STT /
    whisper installs that must not land in the gateway's venv).
    """
    names = (
        ("python3.12", "python", "python3")
        if IS_WINDOWS
        else ("python3.12", "python3", "python3.13")
    )
    for name in names:
        p = shutil.which(name)
        if not p or "brazil-path" in p or "build/private" in p:
            continue
        if _is_windows_store_python_stub(p):
            continue
        try:
            # -I isolates the probe from the caller's environment: without it,
            # ``site`` imports any ``sitecustomize.py`` found on the caller's
            # PYTHONPATH at child startup, and that module can monkeypatch
            # ``sys.version_info`` to steer WHICH interpreter this loop selects.
            # Because -I implies -E (PYTHON* env vars ignored), the UTF-8 pin
            # must ride the argv as ``-X utf8``, matching
            # ``dep_sync._probe_interpreter``.
            out = subprocess.check_output(
                [p, "-I", "-X", "utf8", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                timeout=5,
                stderr=subprocess.DEVNULL,
                **UTF8_TEXT,
            ).strip()
            major, _, minor = out.partition(".")
            if not (int(major) == 3 and int(minor) >= 12):
                continue
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        # >= 3.12 and resolvable. Let the caller veto it (e.g. free-threaded /
        # no pip) and keep searching the remaining candidates.
        if reject is not None and reject(p):
            continue
        return p
    return None


# ---------------------------------------------------------------------------
# Process alarm and the suspend-inclusive clock
# ---------------------------------------------------------------------------


def process_alarm_available() -> bool:
    """Whether :func:`arm_process_alarm` can arm anything on this platform."""
    return hasattr(signal, "setitimer") and hasattr(signal, "ITIMER_REAL")


def arm_process_alarm(seconds: float) -> bool:
    """Deliver ``SIGALRM`` to this process after *seconds*; ``0`` cancels.

    ``setitimer(ITIMER_REAL)`` is the kernel's per-process countdown: it
    needs no thread, no GIL and no root, and re-arming replaces the pending
    deadline.  On Linux the kernel runs it on ``CLOCK_MONOTONIC``, which
    stands still through a suspend, so a deadline armed before a sleep keeps
    its remaining time on resume instead of firing the instant the host wakes
    (``copy_signal`` initialises the process's ``real_timer`` on that clock).
    macOS schedules it on the absolute mach timebase, which also stops during
    sleep.  Returns ``False`` on a platform without the timer (Windows), where
    the caller must do without a deadline of this kind.
    """
    if not process_alarm_available():
        return False
    signal.setitimer(signal.ITIMER_REAL, seconds)
    return True


def boottime_now() -> float | None:
    """Now, on the clock this host dates process starts against.

    Linux: ``CLOCK_BOOTTIME`` counts time spent suspended, exactly as
    ``/proc/uptime`` and the ``starttime`` field of ``/proc/<pid>/stat`` do.
    ``time.monotonic()`` (``CLOCK_MONOTONIC``) does not, so the two MUST NOT be
    mixed in one comparison: after a suspend of S seconds, a boot-clock age minus
    a monotonic stamp places a process S seconds EARLIER than it really started,
    which is how a live shell child comes to look like it predates its own
    dispatch.  Read beside ``time.monotonic()`` across one interval, the
    difference in their advance is the time the host spent suspended — the
    reading the loop watchdog uses to name a resume.

    macOS: ``libproc`` reports a process's start as an absolute wall-clock
    instant (``pbi_start_tvsec``), so the stamp is ``time.time()`` — the same
    clock, suspend included. That clock can STEP (NTP correction after a VM
    resume, an admin reset), and a backward step between the stamp and the
    runtime's fork dates a live child before its own dispatch. The liveness
    oracle pairs this stamp with :func:`kiro_crew.acp.liveness.steady_now` and
    refuses to attribute by start time once the two disagree (see
    :meth:`kiro_crew.acp.liveness.LivenessOracle._started_after_dispatch`);
    the stamp alone cannot tell a step from a slow spawn.

    Returns None where no such clock is available, which every caller must read
    as "cannot attribute" rather than as a time.
    """
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        if sys.platform == "darwin":
            return time.time()
        return None


def awake_now() -> float:
    """Now, in seconds, on a clock that stands still while the host is suspended.

    ``time.monotonic()`` is that clock on Linux (``CLOCK_MONOTONIC``) and macOS
    (the mach timebase). On Windows it counts a sleep, so there this reads the
    unbiased interrupt time (``QueryUnbiasedInterruptTime``, in 100 ns units,
    which leaves out sleep and hibernation) and falls back to
    ``time.monotonic()`` only if that call fails. Liveness checks measure
    silence on it, so a resume does not read as the whole sleep having passed
    without a heartbeat.
    """
    if IS_WINDOWS:
        ticks = ctypes.c_ulonglong(0)
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            if kernel32.QueryUnbiasedInterruptTime(ctypes.byref(ticks)):
                return ticks.value / 10_000_000
        except (AttributeError, OSError):
            pass
    return time.monotonic()


def process_start_boot_secs(starttime_ticks: float) -> float | None:
    """A process's ``starttime`` ticks as seconds on the :func:`boottime_now` clock.

    None when the tick rate cannot be read — including on a platform with no
    ``os.sysconf`` at all (Windows raises AttributeError, not OSError), where
    there is no ``/proc`` to date processes against either. Callers read None as
    "cannot attribute", never as a time.
    """
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (AttributeError, OSError, ValueError):
        return None
    if hz <= 0:
        return None
    return starttime_ticks / hz


def process_age_secs(starttime_ticks: int, *, now: float | None = None) -> float | None:
    """Age of a process whose stat ``starttime`` is *starttime_ticks*, or None.

    ``boottime_now() - process_start_boot_secs(ticks)``, floored at 0: both are
    on the suspend-inclusive clock ``starttime`` counts on. *now* replaces the
    ``boottime_now()`` reading, for a fixture process table that carries its
    own ``uptime``. None when either side cannot be read, which callers read as
    "age unknown".
    """
    started = process_start_boot_secs(starttime_ticks)
    if now is None:
        now = boottime_now()
    if started is None or now is None:
        return None
    return max(0.0, now - started)


# ---------------------------------------------------------------------------
# strftime portability
# ---------------------------------------------------------------------------


def strftime(dt: "object", fmt: str) -> str:
    """``dt.strftime(fmt)`` with the GNU/BSD no-pad directives made portable.

    ``%-I`` / ``%-d`` / ``%-m`` etc. (strip leading zero) are glibc/BSD
    extensions that raise ``ValueError`` on Windows' MSVCRT strftime, which
    spells the same thing ``%#I``. Translate the POSIX form to the Windows
    form on win32 so format strings written for macOS/Linux keep working.
    """
    if IS_WINDOWS:
        out = []
        i = 0
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt):
                nxt = fmt[i + 1]
                if nxt == "-" and i + 2 < len(fmt):
                    out.append("%#" + fmt[i + 2])
                    i += 3
                    continue
                out.append(fmt[i : i + 2])
                i += 2
                continue
            out.append(fmt[i])
            i += 1
        fmt = "".join(out)
    return dt.strftime(fmt)  # type: ignore[attr-defined]


def raise_nofile_soft_limit(target: int) -> None:
    """Best-effort raise of the open-file soft limit toward ``target``.

    POSIX: ``resource.setrlimit(RLIMIT_NOFILE)`` capped at the hard limit.
    Windows: no-op — there is no per-process descriptor rlimit; the C runtime
    uses a fixed handle table sized via ``_setmaxstdio`` which does not apply
    to sockets, so nothing to do.
    """
    if not IS_POSIX:
        return
    try:

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(target, hard), hard))
    except (ValueError, OSError, ImportError):
        logger.debug("Could not raise RLIMIT_NOFILE", exc_info=True)


def nofile_soft_limit() -> int:
    """This process's open-file soft limit, or ``0`` where there is none.

    POSIX: ``resource.getrlimit(RLIMIT_NOFILE)`` soft value; ``RLIM_INFINITY``
    reads as ``0``. Windows: ``0`` — there is no per-process descriptor rlimit
    (see :func:`raise_nofile_soft_limit`), so a caller sizing an fd budget from
    this value leaves the dimension unbounded, which matches the platform.
    """
    if not IS_POSIX:
        return 0
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError, ImportError):
        return 0
    if soft == resource.RLIM_INFINITY:
        return 0
    return max(0, int(soft))


def python_launcher_hops() -> int:
    """How many EXTRA processes a Python child spawned from ``sys.executable`` occupies.

    On Windows a virtual environment's ``Scripts\\python.exe`` is not an
    interpreter: it is the venv redirector, which reads ``pyvenv.cfg``,
    ``CreateProcess``-es the base interpreter as its own child, and stays alive
    as that child's parent until it exits. A ``-m`` child launched through
    ``sys.executable`` from a venv-hosted gateway is therefore TWO live processes
    in the child's Job, not one. A caller sizing an ``ActiveProcessLimit`` for
    "this child and nothing else" has to count that hop, or the redirector's own
    ``CreateProcess`` is what the limit refuses (``ERROR_NOT_ENOUGH_QUOTA``, which
    the redirector reports as ``Unable to create process using ...`` and exit
    101) and the child never runs at all.

    Returns ``1`` when ``sys.executable`` is such a redirector -- the interpreter
    Python actually runs is ``sys._base_executable`` and it is a different file --
    and ``0`` everywhere else: on POSIX a venv's
    ``bin/python`` is a symlink or a copy of the real interpreter and spawns
    nothing. Over-counting is harmless (a ceiling of two instead of one still
    bounds a fork bomb); under-counting is the defect this exists to remove.
    """
    if not IS_WINDOWS:
        return 0
    base = getattr(sys, "_base_executable", None)
    if not base:
        return 0
    try:
        same = os.path.normcase(os.path.realpath(sys.executable)) == os.path.normcase(
            os.path.realpath(base)
        )
    except OSError:
        # Cannot resolve either path: fall back to the unresolved spellings and
        # fail toward the hop, since over-counting is harmless and under-counting
        # is the defect this exists to remove.
        same = os.path.normcase(sys.executable) == os.path.normcase(base)
    return 0 if same else 1
