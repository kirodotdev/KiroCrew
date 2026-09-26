"""Cross-platform compatibility shims for POSIX-only APIs.

All helpers are safe no-ops (or best-effort fallbacks) on Windows where
the underlying syscall does not exist. Callers should use these instead
of raw ``os.*`` / ``fcntl`` / ``signal`` calls for anything that is
POSIX-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import enum
import errno
import functools
import importlib
import io
import ipaddress
import logging
import ntpath
import os
import pathlib
import platform
import shutil
import signal
import site
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import zlib
from asyncio import subprocess as aio_subprocess
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, NamedTuple, Optional, Sequence

from kiro_crew import windows_acl
from kiro_crew.executors import subprocess_executor
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

IS_WINDOWS: bool = sys.platform == "win32"
IS_POSIX: bool = not IS_WINDOWS
IS_LINUX: bool = sys.platform == "linux"
IS_MACOS: bool = sys.platform == "darwin"


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


def reexec_launcher(launcher: str, args: Sequence[str]) -> None:
    """Re-enter a validated stable launcher, preserving its dispatch pathname.

    The launcher, not the core, replaces version-specific environment values.
    Windows execv joins its arguments without quoting, so quote each token for
    the native CRT parser. POSIX receives the original argument vector directly.
    """
    _ensure_utf8_process_environment()
    argv = [launcher, *args]
    if IS_WINDOWS:
        argv = [subprocess.list2cmdline([arg]) for arg in argv]
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
    interpreter; only its display name needs to be space-free.
    """
    # Publish UTF-8 before exec so in-app gateway restarts (Tailnet, update,
    # stale-assets, explicit restart) cannot create a successor that inherits a
    # Windows ANSI stream or a hostile POSIX PYTHONIOENCODING and crashes on the
    # first emoji printed during boot.
    _ensure_utf8_process_environment()
    resolved = executable or sys.executable
    argv0 = ntpath.basename(resolved) if IS_WINDOWS else resolved
    # ``-P``: the successor inherits this process's cwd -- the home directory
    # for a service-launched gateway -- and ``-m`` would put it first on
    # sys.path, ahead of the standard library, so a stdlib-named directory
    # there would shadow the stdlib in the restarted process.
    argv = isolated_python_argv("-P", "-m", module, *args, executable=resolved)
    argv[0] = argv0
    os.execv(resolved, argv)


def execv_target_available(path: str) -> bool:
    """Whether ``execv`` has a file here it may attempt. Two metadata syscalls.

    Distinct from ``is_executable_file`` further down, which answers a different
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

    The ``gateway.log`` tail is not spelled out again here.
    :func:`kiro_crew.cli.drain_log_queue_before_hard_exit` is the shared async
    hard-exit drain for that queue -- same pool, its own outer deadline, and it never
    raises -- so every hard-exit path keeps one spelling and one ceiling for it.
    """
    logger.critical(
        "Gateway restart could not replace this process (target %r); exiting instead of "
        "serving with every session closed and an install this image does not match. "
        "Repair the install and start the gateway again.",
        target or sys.executable,
        exc_info=True,
    )
    from kiro_crew.cli import drain_log_queue_before_hard_exit

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
    # The gateway.log tail has its own async hard-exit drain, which already offloads to
    # this pool under its own deadline and already never raises. Calling it keeps one
    # spelling and one ceiling for that queue across every hard-exit path.
    await drain_log_queue_before_hard_exit()
    os._exit(_POST_DRAIN_EXEC_FAILURE_EXIT)


def _drain_event_log_for_exit() -> None:
    """Flush the member event log's queue. Off-loop; bounded inside the module."""
    from kiro_crew import eventlog_hooks

    eventlog_hooks.drain_for_shutdown()


# Python's os.rename() replaces an existing empty directory on POSIX. Directory
# publication sometimes needs the stronger create-if-absent contract, which the
# kernel exposes but the stdlib does not: renameat2(RENAME_NOREPLACE) on Linux
# and renameatx_np(RENAME_EXCL) on macOS. Resolve the native seam once so callers
# can advertise the capability honestly and fail closed everywhere else.
_RENAME_NOREPLACE_FN: Any = None
_RENAME_NOREPLACE_FLAG = 0

#: ``SYS_renameat2`` numbers per ``platform.machine()``. The syscall has existed
#: in Linux since 3.15 (2014), but the glibc *wrapper* symbol was only added in
#: glibc 2.28. On glibc 2.26/2.27 (e.g. Amazon Linux 2) the kernel supports the
#: call yet ``getattr(libc, "renameat2")`` raises ``AttributeError`` — so we
#: reach the kernel directly through the generic ``syscall()`` entry point,
#: which every glibc exposes, keyed by this table. Numbers are arch-stable ABI.
_SYS_RENAMEAT2_BY_MACHINE: dict[str, int] = {
    "x86_64": 316,
    "i386": 353,
    "i686": 353,
    "aarch64": 276,
    "armv7l": 382,
    "armv6l": 382,
    "ppc64le": 357,
    "ppc64": 357,
    "s390x": 347,
    "riscv64": 276,
}


def _build_renameat2_via_syscall(libc: "ctypes.CDLL") -> Any:
    """Return a renameat2(2) callable via the raw ``syscall()`` seam, or None.

    Used only when the glibc ``renameat2`` wrapper symbol is absent but the
    running kernel supports the syscall (glibc 2.26/2.27 on a modern kernel).
    The returned callable matches the wrapper's 5-argument shape
    ``(olddirfd, oldpath, newdirfd, newpath, flags)`` and preserves errno so
    :func:`rename_noreplace`'s EEXIST / ENOSYS handling is unchanged. Returns
    ``None`` when the architecture's syscall number is unknown, so the caller
    still fails closed rather than issuing a wrong-numbered syscall.
    """
    nr = _SYS_RENAMEAT2_BY_MACHINE.get(platform.machine())
    if nr is None:
        return None
    try:
        _syscall = libc.syscall
    except AttributeError:
        return None
    _syscall.restype = ctypes.c_long
    # syscall() is variadic; ctypes needs the long syscall number typed, and the
    # trailing args are passed positionally with the same ctypes types the
    # wrapper used. c_long for the number, then int/char_p/int/char_p/uint.
    _syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]

    def _renameat2(
        olddirfd: int,
        oldpath: bytes,
        newdirfd: int,
        newpath: bytes,
        flags: int,
    ) -> int:
        return _syscall(nr, olddirfd, oldpath, newdirfd, newpath, flags)

    return _renameat2


if IS_LINUX or IS_MACOS:
    try:
        _rename_libc = ctypes.CDLL(None, use_errno=True)
        _rename_symbol = "renameat2" if IS_LINUX else "renameatx_np"
        try:
            _RENAME_NOREPLACE_FN = getattr(_rename_libc, _rename_symbol)
            _RENAME_NOREPLACE_FN.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            _RENAME_NOREPLACE_FN.restype = ctypes.c_int
        except AttributeError:
            # glibc < 2.28 lacks the renameat2 wrapper symbol. On Linux the
            # syscall itself is available on any kernel >= 3.15, so reach it
            # directly; macOS has no such fallback (renameatx_np is the only
            # path), so it stays None and fails closed there.
            _RENAME_NOREPLACE_FN = _build_renameat2_via_syscall(_rename_libc) if IS_LINUX else None
        if _RENAME_NOREPLACE_FN is not None:
            _RENAME_NOREPLACE_FLAG = 1 if IS_LINUX else 4
    except OSError:
        _RENAME_NOREPLACE_FN = None

RENAME_NOREPLACE_AVAILABLE: bool = _RENAME_NOREPLACE_FN is not None

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


# Portable signal constants — signal.SIGKILL is undefined on Windows.
SIGKILL: int = getattr(signal, "SIGKILL", 9)

# Our own process group, captured at import time (POSIX; 0 on Windows where
# os.getpgid doesn't exist). Used by kill_process_tree's broadcast guard so
# the self-check is stable and immune to test-time os.getpgid patching.
_OWN_PGID: int = os.getpgid(0) if hasattr(os, "getpgid") else 0
SIGTERM: int = getattr(signal, "SIGTERM", 15)
# The hangup a vanished controlling terminal delivers. Undefined on Windows, where
# the ConPTY backend tears a console down by handle rather than by signal.
SIGHUP: int = getattr(signal, "SIGHUP", 1)

# Portable subprocess creation flags — these constants exist ONLY on Windows
# (the subprocess module has no such attributes on POSIX). Referencing
# ``subprocess.CREATE_NEW_PROCESS_GROUP`` directly fails mypy's ``[attr-defined]``
# check on the Linux build fleet even when guarded by ``if IS_WINDOWS:`` (mypy
# resolves attributes statically, ignoring the runtime guard). Expose them via
# ``getattr`` so the names resolve to 0 on POSIX (where they are never used) and
# to the real flags on Windows. Mirrors the ``SIGKILL`` pattern above.
CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
DETACHED_PROCESS: int = getattr(subprocess, "DETACHED_PROCESS", 0)
# CREATE_SUSPENDED has no ``subprocess`` alias to getattr from (that module
# re-exports only a subset of the Win32 creation flags), so the value is spelled
# out. It is the load-bearing half of race-free Job object assignment: a process
# created suspended has not executed a single instruction, so it provably has no
# descendants yet and none can escape the job. See :func:`apply_job_limits` and
# :func:`resume_process_main_thread`. 0 on POSIX, where it is never used, so a
# caller can OR it into ``creationflags`` unconditionally.
CREATE_SUSPENDED: int = 0x00000004 if os.name == "nt" else 0
# For the short-lived helper tools this module shells out to on Windows
# (whoami / netstat / taskkill / powershell): a console-less parent
# (gateway respawned with DETACHED_PROCESS, or pythonw) would otherwise
# allocate a NEW visible console per spawn — a black-window flash on the
# user's desktop for every status poll / secret write / kill. 0 on POSIX.
_SUBPROCESS_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Spawn process-group isolation (for clean tree-kill later): pass these TWO
# keyword args EXPLICITLY to subprocess.Popen / asyncio.create_subprocess_exec —
#     start_new_session=platform_compat.IS_POSIX,
#     creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
# Do NOT build a dict and ``**unpack`` it into the spawn call: that defeats
# mypy's Popen overload resolution on the build fleet ("no overload variant
# matches"). On POSIX start_new_session=True calls setsid (so killpg reaps the
# group) and creationflags=0 is a no-op; on Windows there is no setsid
# (start_new_session is silently ignored) and CREATE_NEW_PROCESS_GROUP makes the
# child tree taskkill /T-reapable. Add DETACHED_PROCESS to the flags for a
# fully detached, console-less child (e.g. the gateway respawn).

# ── Desktop-app bundled interpreter detection ──
#: Directory name the desktop build stages the bundled python-build-standalone
#: runtime under (``Resources/backend-dist/…`` inside the app bundle). The
#: authoritative spellings live in the packaging layer — electron-builder's
#: ``extraResources`` mapping in ``website/electron/package.json`` and the
#: staging steps in ``packaging/build-desktop.sh`` — and this constant MUST
#: match them: ``test_platform_compat.py`` pins the two together so a packaging
#: rename breaks a test instead of a runtime guarantee.
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


# ── macOS TCC-protected home subdirectories ──
# macOS gates these home subdirectories behind TCC (Transparency, Consent and
# Control). The FIRST read of any one of them by a given app triggers a modal
# "…would like to access files in your Downloads folder" prompt, and consent is
# recorded PER (app, folder) pair — so incidentally touching three of them
# during one operation produces THREE separate prompts, not one.
#
# Nothing KiroCrew does at startup needs these folders: they are only ever
# reached INCIDENTALLY, by a breadth-first walk that was rooted at $HOME as a
# catch-all fallback (the @-mention file picker's search root). Pruning them
# from such unscoped walks removes the prompts entirely, which is strictly
# better than pre-declaring NS*FolderUsageDescription strings — those change
# the prompt's wording but still prompt, once per folder.
#
# This does NOT restrict a user's EXPLICIT navigation: an operation whose root
# the user named (a project dir, or a browse request for ~/Downloads itself) is
# scoped by definition and never consults this set. macOS still shows its own
# one-time prompt for that deliberate access, which is the expected contract.
#
# Names only (no leading path): matched against a single path component so the
# same set works for both os.walk dirname pruning and scandir entry filtering.
TCC_PROTECTED_HOME_DIRS: frozenset[str] = frozenset(
    {
        "Downloads",
        "Documents",
        "Desktop",
        "Pictures",
        "Movies",
        "Music",
    }
)
# ``Library`` is deliberately absent from the set ABOVE, but it is not
# unpruned — see TCC_LIBRARY_WALKABLE_CHILDREN. It cannot be a plain member
# here because it must be *descended into* to reach the cloud-drive mounts,
# which a top-level name prune would make unreachable.

#: Children of ``~/Library`` that stay walkable; every other child is pruned
#: from a home-rooted walk. This is an ALLOWLIST on purpose.
#:
#: Much of ``~/Library`` is gated behind Full Disk Access — ``Mail``,
#: ``Messages``, ``Safari``, ``Calendars``, ``HomeKit``, ``Cookies``,
#: ``IdentityServices``, ``Suggestions``, ``PersonalizationPortrait``,
#: ``Metadata/CoreSpotlight``, ``Containers/com.apple.*`` and several
#: ``Application Support`` leaves (``AddressBook``, ``CallHistoryDB``,
#: ``MobileSync``, ``com.apple.TCC``) — and Apple keeps ADDING to that list
#: with each release. A denylist would therefore go stale and silently start
#: leaking prompts again on the next macOS version, so the rule is inverted:
#: name the two paths worth reaching and drop the rest.
#:
#: The two kept entries are the modern cloud-drive mount points, which are
#: common project homes and hold real search hits:
#: ``~/Library/CloudStorage/<Provider>/`` (OneDrive / Google Drive / Dropbox)
#: and ``~/Library/Mobile Documents/`` (iCloud Drive).
TCC_LIBRARY_WALKABLE_CHILDREN: frozenset[str] = frozenset(
    {
        "CloudStorage",
        "Mobile Documents",
    }
)

#: The single ``~/Library`` component name, kept as a constant because the walk
#: pruner compares it positionally rather than by membership.
_LIBRARY_DIR = "Library"


def rename_noreplace(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically rename *src* to an absent *dst*, or raise.

    Unlike :func:`os.rename`, an existing destination is never replaced. Both
    names are resolved relative to caller-pinned directory descriptors. A
    filesystem or platform that cannot preserve that contract raises
    :class:`NotImplementedError`; callers must not fall back to a check followed
    by ordinary rename because another writer can create the destination between
    those two operations.
    """
    fn = _RENAME_NOREPLACE_FN
    if fn is None:
        raise NotImplementedError("atomic no-replace rename is unavailable")
    src_bytes = os.fsencode(src)
    dst_bytes = os.fsencode(dst)
    ctypes.set_errno(0)
    if (
        fn(
            src_dir_fd,
            src_bytes,
            dst_dir_fd,
            dst_bytes,
            _RENAME_NOREPLACE_FLAG,
        )
        == 0
    ):
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), os.fspath(dst))
    unsupported = {errno.ENOSYS, errno.EINVAL}
    unsupported.add(getattr(errno, "EOPNOTSUPP", errno.EINVAL))
    unsupported.add(getattr(errno, "ENOTSUP", errno.EINVAL))
    if error in unsupported:
        raise NotImplementedError("filesystem lacks atomic no-replace rename")
    raise OSError(error, os.strerror(error), os.fspath(dst))


def publish_dir_noreplace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """Atomically rename directory *src* to an ABSENT *dst*, never replacing.

    POSIX ``os.rename`` silently replaces an EMPTY destination directory, so a
    check-then-rename publish can destroy a racer's just-created directory and
    its metadata. This wrapper closes that window: on POSIX it uses
    :func:`rename_noreplace` with both names pinned to their shared parent's
    directory descriptor; on Windows plain ``os.rename`` already refuses any
    existing destination. Raises :class:`FileExistsError` when *dst* exists,
    and :class:`ValueError` when the two paths do not share a parent (the
    staging-sibling contract every caller follows).

    Hosts without the no-replace primitive (glibc < 2.28, and NFS/SMB/FUSE
    filesystems that reject RENAME_NOREPLACE with ENOSYS/EINVAL/EOPNOTSUPP)
    fall back to an atomic-exclusive ``os.mkdir`` CLAIM of the destination
    followed by a plain rename: the mkdir raises :class:`FileExistsError` when
    the destination is occupied, and the only directory the rename can then
    replace is the empty claim this very call created -- the no-replace
    guarantee for creation races is preserved, and the operation keeps working
    instead of crashing with ``NotImplementedError``.
    """
    if IS_WINDOWS:
        os.rename(src, dst)
        return
    src_abs = os.path.abspath(os.fspath(src))
    dst_abs = os.path.abspath(os.fspath(dst))
    parent = os.path.dirname(dst_abs)
    if os.path.dirname(src_abs) != parent:
        raise ValueError("publish_dir_noreplace requires sibling src and dst")
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            rename_noreplace(
                os.path.basename(src_abs),
                os.path.basename(dst_abs),
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            return
        except NotImplementedError:
            pass
    finally:
        os.close(parent_fd)
    os.mkdir(dst_abs)
    try:
        os.rename(src_abs, dst_abs)
    except BaseException:
        # Drop OUR claim so a retry is not permanently blocked by an orphaned
        # empty directory. os.rmdir only ever removes an empty directory, so
        # the worst it can touch is the claim this call just created; its own
        # failure is suppressed in favour of the original rename error.
        with contextlib.suppress(OSError):
            os.rmdir(dst_abs)
        raise


def tcc_protected_dirs_for_walk(root: str | os.PathLike) -> frozenset[str]:
    """Return the TCC-protected dir names to prune when walking *root*.

    Empty off macOS (no TCC), and empty unless *root* is the user's home
    directory itself: a walk the user explicitly scoped to ``~/Downloads`` (or
    to a project that happens to live under it) must still see its own
    contents. Only the incidental ``$HOME``-as-fallback walk is pruned.

    Callers MUST pass the same ``str`` they hand to :func:`os.walk` — the
    returned names are compared against ``os.walk``'s ``dirpath``, which is
    byte-identical to the ``top`` argument it was given.

    On any resolution failure this returns the empty set, i.e. it prunes
    nothing. That degrades to today's behavior (a prompt may appear) rather
    than silently hiding a directory the caller asked for.
    """
    if not IS_MACOS:
        return frozenset()
    try:
        if os.path.realpath(root) != os.path.realpath(os.path.expanduser("~")):
            return frozenset()
    except (OSError, ValueError):
        # OSError: EACCES / ELOOP / ENAMETOOLONG on an exotic path.
        # ValueError: realpath() rejects a path containing a null byte — NOT an
        # OSError subclass, so it would otherwise escape to the caller and 500
        # the /api/file-search request (same class as agent.py's guard).
        return frozenset()
    return TCC_PROTECTED_HOME_DIRS


def tcc_prune_walk_dirs(root: str, dirpath: str, dirnames: list[str]) -> list[str]:
    """Return *dirnames* minus the TCC-gated entries for this walk position.

    Single entry point for ``os.walk`` pruning: call it with the ``top`` passed
    to :func:`os.walk` plus the ``dirpath``/``dirnames`` of the current step and
    assign the result back into ``dirnames[:]``.

    Two positions prune, and only when *root* is the user's home directory
    itself (see :func:`tcc_protected_dirs_for_walk` for why an explicitly
    scoped root is never pruned):

    * at *root* — drop the gated top-level folders (``Downloads``, ``Desktop``,
      ...). ``Library`` is NOT dropped here, so the walk can reach the cloud
      mounts below it.
    * at ``<root>/Library`` — keep only TCC_LIBRARY_WALKABLE_CHILDREN and drop
      every other child, most of which is Full-Disk-Access gated.

    Every deeper position returns *dirnames* untouched. A name that merely
    matches a gated folder further down the tree (a project's own
    ``Documents/``) is not gated and stays walkable.

    Callers MUST pass the same ``str`` they hand to :func:`os.walk`: the
    positional comparisons rely on ``dirpath`` being built by joining onto
    ``top`` verbatim, which is what ``os.walk`` guarantees.
    """
    if not IS_MACOS:
        return dirnames
    # Positional gate FIRST: only two walk positions can prune, so every other
    # directory in a large tree returns without paying the realpath syscall
    # that the home check below costs.
    at_root = dirpath == root
    at_library = not at_root and dirpath == os.path.join(root, _LIBRARY_DIR)
    if not (at_root or at_library):
        return dirnames
    # Doubles as the "is root the home directory" test and inherits that
    # helper's failure handling (an unresolvable root prunes nothing).
    protected = tcc_protected_dirs_for_walk(root)
    if not protected:
        return dirnames
    if at_root:
        return [d for d in dirnames if d not in protected]
    return [d for d in dirnames if d in TCC_LIBRARY_WALKABLE_CHILDREN]


def ensure_utf8_console() -> None:
    """Keep Kiro Crew's process tree UTF-8 and repair current Windows streams.

    KiroCrew prints non-ASCII glyphs throughout its CLI/gateway output. On
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


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------

if IS_POSIX:
    import fcntl  # noqa: F401 — re-exported
    import resource  # noqa: F401 — POSIX-only; used by the resource shims below
else:
    import msvcrt  # type: ignore[import-not-found]


# msvcrt's blocking lock codes (LK_LOCK / LK_RLCK) are NOT the equivalent of
# fcntl.flock(LOCK_EX): rather than waiting until the lock is free, they retry
# ~10 times at 1s intervals and then RAISE EDEADLOCK (errno 36). Swallowing
# that as "acquired" lets a caller run its read-modify-write with no exclusion
# and silently lose writes. So the Windows "blocking" acquire spins on the
# non-blocking code (LK_NBLCK) instead — the same idiom cron._file_lock uses.
# It is bounded rather than truly unbounded because a contended fd and a
# non-writable fd are indistinguishable on Windows (both surface as errno 13
# EACCES), so an unbounded spin would turn a permission error into a hang.
#
# Two ceilings, because on-loop and off-loop have opposite needs:
#  - OFF the loop (cron, app backends — threads/subprocesses): the wait must
#    cover a legitimately long holder that can hold the lock across a
#    multi-second operation, and a waiter there must NOT give up and race it. So
#    use a generous ceiling that no real hold approaches.
#  - ON the loop (e.g. bridges._mcp_lock during app enable): a spin-sleep would
#    freeze chat/heartbeat, so that path never sleeps at all (single-shot).
_LOCK_POLL_SECS = 0.01
# Backoff cap for the POSIX poll. A kernel-blocking acquire sleeps at zero cost,
# so a flat 10ms poll would add ~30k pointless wakeups across the full ceiling;
# doubling up to this bound keeps the first polls tight (a holder finishing a
# sub-second critical section is still seen at once) and makes a long wait cheap.
_LOCK_POLL_MAX_SECS = 0.25
# Generous off-loop ceiling: longer than any legitimate hold, short enough that
# a truly stuck/permission-denied fd still fails.
_LOCK_TIMEOUT_SECS = 300.0

# The ceiling is not Windows-only. ``fcntl.flock`` has no timeout argument, so an
# unbounded POSIX acquire waits on a stuck holder without limit -- and on the boot
# path that is a gateway which never binds its port and logs nothing, which reads
# as a slow start rather than a failure. Both platforms refuse past this same
# ceiling, so the failure is reportable. ``_WIN_*`` are aliases of these names.
_WIN_LOCK_POLL_SECS = _LOCK_POLL_SECS
_WIN_LOCK_TIMEOUT_SECS = _LOCK_TIMEOUT_SECS


def _lock_timeout_message(timeout: float, *, exclusive: bool = True) -> str:
    """The one refusal string both platforms raise when the ceiling is hit.

    Names the ceiling and the reason. A caller that catches this as best-effort
    work has nothing else to report, so this message is the only evidence of WHY
    the critical section was declined.
    """
    kind = "exclusive" if exclusive else "shared"
    return (
        f"could not acquire {kind} file lock within {timeout:g}s "
        "(a holder is stuck); refusing to proceed unserialized"
    )


def _posix_acquire_blocking(
    fd: int,
    mode: int,
    *,
    timeout: float | None = None,
) -> bool:
    """POSIX bounded lock acquire: poll ``LOCK_NB`` until free or *timeout*.

    Returns True if the lock was taken, False if the ceiling was reached.

    ``fcntl.flock`` takes no timeout, so polling the non-blocking code is the only
    way to bound the wait -- the same shape :func:`_win_acquire_blocking` uses, for
    the same reason: a stuck holder must not be waited on without limit.

    Retries cover contention ONLY. ``EAGAIN``/``EACCES``/``EWOULDBLOCK`` mean
    another holder has it; any other errno is about this fd and propagates at
    once, so a real defect is not reported as a stuck holder at the ceiling.

    NEVER polls on the asyncio event-loop thread, matching
    :func:`_win_acquire_blocking`: ``time.sleep`` there would freeze chat and
    heartbeat for the whole wait, and a freeze long enough to miss a heartbeat is
    a supervisor kill. On the loop the acquire is single-shot -- take it if free,
    else refuse at once -- so a caller fails closed instead of stalling every
    other session. Off the loop, which is where the lock is normally taken, it
    polls to the ceiling as a real wait.

    The sleep BACKS OFF from ``_LOCK_POLL_SECS`` to ``_LOCK_POLL_MAX_SECS``: a
    flat 10ms poll would wake ~30k times across the full ceiling for no benefit,
    while a kernel-blocking acquire wakes not at all. The first polls stay tight,
    so a holder finishing its sub-second critical section is picked up promptly,
    and a long wait settles into a cheap idle.
    """
    nb_mode = mode | fcntl.LOCK_NB

    def _try_once() -> bool:
        try:
            fcntl.flock(fd, nb_mode)
            return True
        except OSError as exc:
            # Only "someone holds it" is retryable. EBADF/EINVAL and friends are
            # real errors about THIS fd and must surface now, not at the ceiling.
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            return False

    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        on_loop = False
    if on_loop:
        # Single attempt only -- a poll-sleep here blocks the event loop.
        return _try_once()

    ceiling = _LOCK_TIMEOUT_SECS if timeout is None else timeout
    deadline = time.monotonic() + ceiling
    delay = _LOCK_POLL_SECS
    while True:
        if _try_once():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Never sleep past the deadline, so the refusal lands at the ceiling
        # rather than up to one backed-off interval after it.
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, _LOCK_POLL_MAX_SECS)


def _win_acquire_blocking(fd: int, *, timeout: float = _LOCK_TIMEOUT_SECS) -> bool:
    """Windows blocking lock acquire: spin on LK_NBLCK until free or timeout.

    Returns True if the lock was taken, False if it could not be.

    NEVER spins on the asyncio event-loop thread: ``time.sleep`` there would
    freeze chat/heartbeat for the whole wait. A few callers still take the lock
    on the loop (e.g. bridges._mcp_lock during app enable), so when a running
    loop is detected the acquire is single-shot — take it if free, else return
    False at once — and the caller fails closed rather than stalling the loop.
    Off the loop (the common case) it polls up to ``timeout`` as a real
    blocking wait, so a legitimately long holder is waited out rather than
    raced.
    """

    def _try_once() -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        on_loop = False
    if on_loop:
        # Single attempt only — a spin-sleep here blocks the event loop.
        return _try_once()

    deadline = time.monotonic() + timeout
    while True:
        if _try_once():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LOCK_POLL_SECS)


@contextlib.contextmanager
def file_lock(
    fd: int,
    *,
    exclusive: bool = True,
    required: bool = False,
    wait: bool = True,
    timeout: float | None = None,
) -> Iterator[None]:
    """Acquire an advisory lock on ``fd`` for the duration of the block.

    POSIX: ``fcntl.flock(LOCK_EX|LOCK_SH)`` with ``LOCK_UN`` release, acquired by
    polling the non-blocking code up to ``_LOCK_TIMEOUT_SECS``. ``flock`` itself
    takes no timeout, and an unbounded wait on a stuck holder is not a wait but a
    hang: on the boot path it leaves a gateway that binds no port and logs
    nothing, which no supervisor or health check can act on.
    Windows: ``msvcrt.locking`` on the first byte, acquired by spinning on the
    non-blocking code up to ``_LOCK_TIMEOUT_SECS`` — because msvcrt's own
    "blocking" code gives up after ~10s with EDEADLOCK, which cannot be treated
    as a wait. ``msvcrt`` has no shared mode, so a shared request is satisfied
    with an exclusive lock (correctness over concurrency — readers genuinely
    serialize with the holder, but never see torn writes).

    On BOTH platforms, if the lock cannot be taken within the ceiling
    ``file_lock`` FAILS CLOSED — it raises rather than entering the critical
    section unserialized, since proceeding lock-less is the exact fail-open that
    loses writes. On BOTH platforms the acquire is additionally single-shot when
    called on the asyncio event-loop thread: a poll-sleep there would freeze chat
    and heartbeat for the whole wait, and a freeze long enough to miss a heartbeat
    is a supervisor kill, so a contended on-loop caller is refused at once and
    fails closed rather than stalling every other session. The timeout is a safety
    ceiling against a stuck holder, not a normal wait. ``required`` is kept for
    call-site intent and does not change the outcome (both paths refuse to proceed
    without the lock).

    *timeout* overrides that ceiling for a caller whose critical section is
    legitimately long, and must be set by any caller that can hold the lock past
    ``_LOCK_TIMEOUT_SECS``. The default suits the common case — a sub-second read
    plus an atomic rename — but it is NOT an upper bound on every in-tree holder:
    ``frontend._staging_lock`` spans an ``npm run build`` plus an install, so a
    default ceiling would refuse a contender while the holder is still working
    rather than because it is stuck. A ceiling shorter than the holder's real
    work turns a wait into a spurious refusal, which is the failure this
    parameter exists to prevent.

    *wait* is for a caller whose work is OPTIONAL and retried later, and which
    may run on the event-loop thread: with ``wait=False`` the acquire is
    single-shot on every platform and raises :class:`BlockingIOError` at once
    when the lock is held, instead of blocking the loop for as long as the holder
    keeps it. POSIX gets that from ``LOCK_NB``; Windows already behaves this way
    on the loop thread, and a zero timeout makes it uniform off the loop too.
    ``BlockingIOError`` is an ``OSError``, so it is a NARROWING of what a caller
    already had to handle, and it separates "someone else is writing right now"
    from the stuck-holder ceiling above. It changes only how long we are willing
    to wait, never whether the critical section is serialized -- a contended
    ``wait=False`` acquire raises rather than proceeding.

    Note: on Windows, ``msvcrt.locking`` requires seeking to byte 0, so the
    ``fd`` must be a dedicated lock file; callers must not rely on the file
    offset being preserved across the context manager boundary.
    """
    if IS_POSIX:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not wait:
            # BlockingIOError (an OSError) when held: same fail-closed contract
            # as the Windows branch, reported by the platform rather than by us.
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        elif not _posix_acquire_blocking(fd, mode, timeout=timeout):
            # Past the ceiling the holder is stuck, not busy. Refuse LOUDLY
            # rather than wait on it without limit: an unbounded wait here leaves
            # a boot with no port bound and no log line, while a raise is
            # something the caller can report and recover from -- the gateway
            # boot path logs it at ERROR, prints the repair command, and still
            # binds its port.
            raise OSError(
                _lock_timeout_message(
                    _LOCK_TIMEOUT_SECS if timeout is None else timeout,
                    exclusive=exclusive,
                )
            )
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    else:
        # Fail CLOSED, not open: if the lock cannot be taken within the ceiling
        # (a stuck/crashed holder — never a normal sub-second hold), raise rather
        # than enter the critical section unserialized. Entering anyway is the
        # exact fail-open that loses writes; a loud error in that rare case is
        # strictly safer, and callers already run under `with`, so the fd is
        # cleaned up. `required` is kept for call-site intent and does not
        # change the outcome — both paths refuse to proceed lock-less.
        # The waiting path with no explicit ceiling is called with no keyword, so
        # the default-argument call shape existing tests stub out is preserved.
        if not wait:
            ceiling = 0.0
            acquired = _win_acquire_blocking(fd, timeout=0.0)
        elif timeout is None:
            ceiling = _LOCK_TIMEOUT_SECS
            acquired = _win_acquire_blocking(fd)
        else:
            ceiling = timeout
            acquired = _win_acquire_blocking(fd, timeout=timeout)
        if not acquired:
            if not wait:
                # Held right now. BlockingIOError so the caller can tell this
                # from the stuck-holder ceiling below, matching POSIX LOCK_NB.
                raise BlockingIOError("file lock is held; not waiting for it")
            raise OSError(_lock_timeout_message(ceiling, exclusive=exclusive))
        try:
            yield
        finally:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                pass


@contextlib.contextmanager
def flock_exclusive(fd: int) -> Iterator[None]:
    """Acquire an exclusive advisory lock on ``fd`` (see :func:`file_lock`)."""
    with file_lock(fd, exclusive=True):
        yield


@contextlib.contextmanager
def open_lock_file(path: "str | os.PathLike[str]") -> Iterator[int]:
    """Open *path* for locking WITHOUT truncating it (GH-9248).

    ``open(path, "w")`` truncates the file before any lock is held. On POSIX
    that is survivable; on Windows the subsequent acquire routes to
    ``msvcrt.locking`` on the already-truncated file, so a contending process
    can observe or produce an empty lock file and crash out of the critical
    section — the loss lands only on a specific interleaving, which is why it
    read as shard flake rather than a deterministic failure.
    ``O_RDWR | O_CREAT`` creates-or-opens in one syscall and never truncates.

    Yields the raw integer fd, ready for :func:`file_lock` /
    :func:`flock_exclusive`. The lock file's CONTENT is never meaningful to
    the lock itself; this exists so contenders cannot watch it flicker empty.
    """
    fd = os.open(os.fspath(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        yield fd
    finally:
        os.close(fd)


def acquire_lock(fd: int, *, exclusive: bool = True) -> None:
    """Low-level lock acquire for the acquire-now / release-later fd-handoff
    pattern (where a context manager does not fit).

    POSIX and Windows both wait up to ``_LOCK_TIMEOUT_SECS`` off the asyncio loop
    thread and are both single-shot on it: POSIX polls ``fcntl.flock`` with
    ``LOCK_NB`` (:func:`_posix_acquire_blocking`), Windows polls ``msvcrt.locking``
    (:func:`_win_acquire_blocking`). If the lock cannot be taken it FAILS
    CLOSED — raises rather than letting the caller proceed unserialized — since
    a stuck holder past the ceiling is an error, not a routine wait, and
    proceeding lock-less is the fail-open that loses writes. Pair every call
    with :func:`release_lock` on the same ``fd``.
    """
    if IS_POSIX:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not _posix_acquire_blocking(fd, mode):
            raise OSError(_lock_timeout_message(_LOCK_TIMEOUT_SECS, exclusive=exclusive))
        return
    if not _win_acquire_blocking(fd):
        raise OSError(_lock_timeout_message(_LOCK_TIMEOUT_SECS))


def release_lock(fd: int) -> None:
    """Release a lock acquired via :func:`acquire_lock` / :func:`try_acquire_lock`."""
    if IS_POSIX:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    except OSError:
        pass


def try_acquire_lock(fd: int, *, exclusive: bool = False) -> bool:
    """Attempt a non-blocking lock acquire. Returns True iff the lock was taken.

    POSIX: ``fcntl.flock(... | LOCK_NB)``. Windows: ``msvcrt.locking`` with the
    non-blocking codes. On success, the caller must :func:`release_lock` the fd.
    """
    if IS_POSIX:
        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, mode)
            return True
        except (BlockingIOError, OSError):
            return False
    # msvcrt has no shared lock; LK_NBLCK is a non-blocking exclusive lock.
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def probe_file_persistence(directory: Path) -> str | None:
    """Verify that *directory* supports every primitive the Kiro Crew
    persistence paths depend on: creating a new file (``tempfile.mkstemp``),
    writing bytes to it, taking an advisory lock (:func:`file_lock`),
    atomically replacing it (``os.replace``), and removing it — the exact
    operations ``atomic_write`` and the ``.lock``-file helpers perform.

    Returns ``None`` when all of them work, otherwise a human-readable
    description of the first failure. A process whose environment breaks any
    of these primitives cannot save chat history, cron history, or session
    state — but it CAN still serve traffic and append to already-open log fds,
    so without this probe it limps along losing writes silently. The known way
    to get into that state is inheriting a seccomp syscall filter from a
    sandboxed parent (seccomp survives fork/exec, ``nohup`` included):
    filtered syscalls fail with ``ENOSYS`` while everything else looks
    healthy. The returned message names that cause when ``errno`` says so.

    Probe files carry a ``.persistence-probe-`` prefix, and their removal is
    part of the probed contract: an environment that allows creating files but
    denies deleting them (delete-scoped ACLs) breaks the atomic
    rename/replace paths just the same, so a failed cleanup is reported as a
    preflight failure rather than suppressed. On the failure path probe files
    are best-effort removed; one may remain only when removal itself is what
    is broken.
    """
    fd: int | None = None
    path: str | None = None
    replaced: str | None = None
    step = "create files in"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=directory, prefix=".persistence-probe-")
        step = "write files in"
        os.write(fd, b"probe")
        step = "flush files in"
        os.fsync(fd)
        step = "lock files in"
        with file_lock(fd, exclusive=True):
            pass
        os.close(fd)
        fd = None
        step = "atomically replace files in"
        replaced = f"{path}.target"
        # The same replace primitive atomic_write commits with: plain
        # os.replace on POSIX, bounded retry over the Windows AV/indexer
        # sharing-violation window — a healthy Windows data home must not fail
        # the preflight over that transient. Imported lazily because
        # atomic_write imports this module at top level.
        from kiro_crew.atomic_write import replace_with_retry

        replace_with_retry(path, replaced)
        path = None
        step = "remove files from"
        os.unlink(replaced)
        replaced = None
    except OSError as exc:
        hint = ""
        if exc.errno == errno.ENOSYS:
            hint = (
                " (ENOSYS from a basic file syscall usually means this process"
                " inherited a seccomp filter from a sandboxed parent — e.g. a"
                " gateway spawned from inside an agent session; start it from a"
                " regular shell or the system service instead)"
            )
        return f"cannot {step} {directory}: {exc}{hint}"
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        for leftover in (path, replaced):
            if leftover is not None:
                with contextlib.suppress(OSError):
                    os.unlink(leftover)
    return None


# ---------------------------------------------------------------------------
# Win32 struct layouts
# ---------------------------------------------------------------------------
# These MUST stay at module scope, never inside the functions that use them.
# ``ctypes.POINTER(T)`` memoises T -> POINTER(T) in a module-level dict inside
# ctypes and never evicts it, so a Structure subclass declared in a function
# body pins a BRAND-NEW pair of type objects on every call. The helpers below
# are polled (the dashboard's system metrics, the RSS-recycle watchdog, the
# tree-kill parent-map walk, the MCP pipe's per-connection peer check), which
# turns that into unbounded growth in a long-lived gateway. Declared once here,
# the memo holds a single entry for the process lifetime.
#
# ``wintypes`` supplies type aliases only, so these definitions import cleanly
# on POSIX; the functions below still resolve the DLLs lazily, which is what
# keeps them patchable from the non-Windows test fleet.


class _ProcessEntry32(ctypes.Structure):
    """Toolhelp ``PROCESSENTRY32`` — process-enumeration snapshot entry."""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    """psapi ``PROCESS_MEMORY_COUNTERS`` — per-process working set."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _MemoryStatusEx(ctypes.Structure):
    """kernel32 ``MEMORYSTATUSEX`` — system-wide physical memory."""

    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SidAndAttributes(ctypes.Structure):
    """advapi32 ``SID_AND_ATTRIBUTES``."""

    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    """advapi32 ``TOKEN_USER`` — the ``TokenUser`` information-class payload."""

    _fields_ = [("User", _SidAndAttributes)]


class _IoCounters(ctypes.Structure):
    """kernel32 ``IO_COUNTERS`` — the I/O accounting block inside a job's limits.

    Never read; present only so the extended-limit layout below has the correct
    size and field offsets.
    """

    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    """kernel32 ``JOBOBJECT_BASIC_LIMIT_INFORMATION``."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    """kernel32 ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` — the ceiling payload.

    ``ActiveProcessLimit`` (in the basic block) bounds the process count where
    ``TasksMax`` bounds tasks, and ``JobMemoryLimit`` is the ``MemoryMax``
    equivalent. See :func:`apply_job_limits` for why the process row is not a
    one-for-one mapping.
    """

    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    """Toolhelp ``THREADENTRY32`` — thread-enumeration snapshot entry.

    Used by :func:`resume_process_main_thread`, which takes
    ``ctypes.POINTER(_ThreadEntry32)`` for the ``Thread32First`` /
    ``Thread32Next`` argtypes — so this layout in particular MUST stay at module
    scope: it is pointed at, which is exactly what pins a type in ctypes'
    unbounded memo, and the helper runs once per agent spawn.
    """

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


# ---------------------------------------------------------------------------
# Process introspection
# ---------------------------------------------------------------------------

# Byte layout of the macOS ``proc_vnodepathinfo`` struct filled by
# ``proc_pidinfo(PROC_PIDVNODEPATHINFO)``: two ``vnode_info_path`` records (the
# process's cwd, then its root), each a fixed-size ``vnode_info`` header
# followed by a NUL-terminated path of up to ``MAXPATHLEN``. Only the header
# size matters to us, since it is the offset the cwd path starts at.
_DARWIN_PROC_PIDVNODEPATHINFO = 9
_DARWIN_VNODE_INFO_SIZE = 152
_DARWIN_MAXPATHLEN = 1024
_DARWIN_VNODE_INFO_PATH_SIZE = _DARWIN_VNODE_INFO_SIZE + _DARWIN_MAXPATHLEN
_DARWIN_PROC_VNODEPATHINFO_SIZE = 2 * _DARWIN_VNODE_INFO_PATH_SIZE

# ``proc_pidinfo(PROC_PIDTBSDINFO)`` fills a ``proc_bsdinfo`` struct whose
# parent pid and start-time pair live at fixed offsets. ``pbi_ppid`` is the
# fifth uint32 (16), followed later by ``pbi_start_tvsec`` and
# ``pbi_start_tvusec`` as two uint64s (120 / 128, struct size 136). The total
# size doubles as the layout check.
_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_PROC_BSDINFO_SIZE = 136
_DARWIN_PBI_PPID_OFFSET = 16
_DARWIN_PBI_START_TVSEC_OFFSET = 120
_DARWIN_PBI_START_TVUSEC_OFFSET = 128


class ProcessStartIdentity(NamedTuple):
    """A process start ID and parent PID captured by one kernel read."""

    start_id: str
    ppid: int


# ``proc_pidinfo(PROC_PIDTASKINFO)`` fills a ``proc_taskinfo`` struct that opens
# with six uint64 fields — ``pti_virtual_size``, ``pti_resident_size``,
# ``pti_total_user``, ``pti_total_system``, ``pti_threads_user``,
# ``pti_threads_system`` (48 bytes) — followed by twelve int32 counters (48),
# for a struct size of 96. Only the two total-CPU fields matter here, and both
# are already NANOSECONDS; the total size doubles as the layout check.
_DARWIN_PROC_PIDTASKINFO = 4
_DARWIN_PROC_TASKINFO_SIZE = 96
_DARWIN_PTI_TOTAL_USER_OFFSET = 16
_DARWIN_PTI_TOTAL_SYSTEM_OFFSET = 24

_darwin_libproc: Any = None
_darwin_libproc_loaded = False


def _darwin_libproc_handle() -> Any:
    """Cached ``libproc`` handle, or None when it cannot be loaded.

    Cached rather than opened per call because the cwd probe runs on a poll
    cadence per open terminal, and a fresh ``CDLL`` would dlopen every time.
    """
    global _darwin_libproc, _darwin_libproc_loaded
    if _darwin_libproc_loaded:
        return _darwin_libproc
    _darwin_libproc_loaded = True
    try:
        path = ctypes.util.find_library("proc")
        if path is None:
            return None
        lib = ctypes.CDLL(path)
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
        _darwin_libproc = lib
    except Exception:
        _darwin_libproc = None
    return _darwin_libproc


def _darwin_process_cwd(pid: int) -> str | None:
    """macOS cwd of *pid* via ``libproc``, or None when it cannot be read.

    Requires no entitlement for a same-uid process. The kernel reports how many
    bytes it filled; anything other than the exact struct size means the layout
    assumed by the offsets above no longer matches, so the answer is refused
    rather than sliced out of the wrong place.
    """
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PROC_VNODEPATHINFO_SIZE)
        filled = lib.proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDVNODEPATHINFO,
            0,
            buf,
            _DARWIN_PROC_VNODEPATHINFO_SIZE,
        )
        if filled != _DARWIN_PROC_VNODEPATHINFO_SIZE:
            return None
        raw = buf.raw[_DARWIN_VNODE_INFO_SIZE:_DARWIN_VNODE_INFO_PATH_SIZE]
        cwd = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        return cwd or None
    except Exception:
        return None


def _darwin_process_start_identity(pid: int) -> ProcessStartIdentity | None:
    """Read macOS start ID and PPID atomically from one ``proc_bsdinfo``.

    ``proc_pidinfo(PROC_PIDTBSDINFO)`` needs no entitlement for a same-uid
    process and never spawns a subprocess. Reading both fields from this one
    kernel-filled struct prevents a recycled pid from pairing old parentage
    with a new process identity. The start instant is absolute wall time at
    microsecond resolution, six decimal orders finer than ``ps -o lstart=``.
    """
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PROC_BSDINFO_SIZE)
        filled = lib.proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTBSDINFO,
            0,
            buf,
            _DARWIN_PROC_BSDINFO_SIZE,
        )
        if filled != _DARWIN_PROC_BSDINFO_SIZE:
            return None
        # Both x86_64 and arm64 macOS are little-endian.
        ppid = int.from_bytes(
            buf.raw[_DARWIN_PBI_PPID_OFFSET : _DARWIN_PBI_PPID_OFFSET + 4],
            "little",
        )
        sec = int.from_bytes(
            buf.raw[_DARWIN_PBI_START_TVSEC_OFFSET:_DARWIN_PBI_START_TVUSEC_OFFSET],
            "little",
        )
        usec = int.from_bytes(
            buf.raw[_DARWIN_PBI_START_TVUSEC_OFFSET:_DARWIN_PROC_BSDINFO_SIZE],
            "little",
        )
        if sec <= 0:
            return None
        return ProcessStartIdentity(f"{sec}.{usec:06d}", ppid)
    except Exception:
        return None


def _darwin_process_start_microtime(pid: int) -> str | None:
    """macOS start time of *pid* via the atomic ``proc_bsdinfo`` reader."""
    identity = _darwin_process_start_identity(pid)
    return identity.start_id if identity is not None else None


def _darwin_process_cpu_nanos(pid: int) -> int | None:
    """macOS total (user+system) CPU nanoseconds of *pid* via ``libproc``.

    Same contract as the start-time probe above: no entitlement is needed for a
    same-uid process, nothing is exec'd, and a fill size other than the exact
    struct size means the assumed layout no longer matches, so the answer is
    refused rather than sliced out of the wrong place.
    """
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PROC_TASKINFO_SIZE)
        filled = lib.proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTASKINFO,
            0,
            buf,
            _DARWIN_PROC_TASKINFO_SIZE,
        )
        if filled != _DARWIN_PROC_TASKINFO_SIZE:
            return None
        # Both x86_64 and arm64 macOS are little-endian.
        user = int.from_bytes(
            buf.raw[_DARWIN_PTI_TOTAL_USER_OFFSET:_DARWIN_PTI_TOTAL_SYSTEM_OFFSET], "little"
        )
        system = int.from_bytes(
            buf.raw[_DARWIN_PTI_TOTAL_SYSTEM_OFFSET : _DARWIN_PTI_TOTAL_SYSTEM_OFFSET + 8],
            "little",
        )
        return user + system
    except Exception:
        return None


# Further ``proc_bsdinfo`` fields the liveness backend reads: ``pbi_status`` is
# the second uint32 (offset 4). ``SZOMB`` is the BSD process-state code for
# a zombie; in practice the kernel refuses ``PROC_PIDTBSDINFO`` for a zombie
# outright, so the code is a second line of defence, not the primary test.
_DARWIN_PBI_STATUS_OFFSET = 4
_DARWIN_SZOMB = 5

# ``proc_pidpath`` fails unless handed ``PROC_PIDPATHINFO_MAXSIZE`` bytes, which
# is four times ``MAXPATHLEN``.
_DARWIN_PIDPATH_MAXSIZE = 4 * _DARWIN_MAXPATHLEN

# ``sysctl(CTL_KERN, KERN_PROCARGS2, pid)`` returns an ``int`` argc, the exec
# path, NUL padding, then the argv strings and finally the environment. The
# kernel truncates silently to the buffer handed in (no error), so a bounded
# buffer costs at most the tail of an unusually long argv — never a failure.
_DARWIN_CTL_KERN = 1
_DARWIN_KERN_PROCARGS2 = 49
_DARWIN_PROCARGS_BUFSIZE = 64 * 1024

# The environment sits AFTER argv in that same record, so the environ probe
# cannot share the argv probe's bound: the kernel truncates silently, and a
# process with a long argv -- a recursively self-appending launcher is exactly
# that shape -- would have its environment cut off and read as "no marker",
# failing closed on the very process an identity gate most needs to place. Sized
# at the kernel's own ``ARG_MAX`` ceiling on argv plus environment instead, so
# truncation is impossible rather than merely unlikely.
_DARWIN_PROCARGS_ENV_BUFSIZE = 1024 * 1024

# ``sysctl(CTL_KERN, KERN_PROC, KERN_PROC_PID, pid)`` answers for a ZOMBIE where
# ``proc_pidinfo`` refuses: the kernel walks its zombie list for this query as
# well as the live one, and a zombie's ``proc`` still carries its start instant.
# The record is a ``kinfo_proc`` whose leading ``extern_proc`` holds the same
# ``p_start`` the ``PROC_PIDTBSDINFO`` probe reports -- ``p_starttime`` (offset
# 0: int64 seconds, int32 microseconds) -- and the BSD state code ``p_stat``
# (offset 36; ``SZOMB`` above). The kernel writes exactly ``sizeof(kinfo_proc)``
# bytes per process, 648 on every 64-bit macOS; any other length means the
# layout these offsets assume does not hold, so the answer is refused rather
# than sliced out of the wrong place (the rule the libproc probes follow). A pid
# that does not exist is not an error: the call succeeds with a zero-length
# answer. ``KERN_PROC_PGRP`` lists every member of a process group the same way.
_DARWIN_KERN_PROC = 14
_DARWIN_KERN_PROC_PID = 1
_DARWIN_KERN_PROC_PGRP = 2
_DARWIN_KINFO_PROC_SIZE = 648
_DARWIN_KP_START_TVSEC_OFFSET = 0
_DARWIN_KP_START_TVUSEC_OFFSET = 8
_DARWIN_KP_STAT_OFFSET = 36
_DARWIN_KP_PID_OFFSET = 40
# Headroom for processes that join a group between the size query and the read,
# doubled on each of the retries a still-growing group is given.
_DARWIN_KINFO_PGRP_SLACK = 16
_DARWIN_KINFO_PGRP_ATTEMPTS = 4

# ``proc_listchildpids`` writes ``pid_t`` values and returns HOW MANY it wrote.
# A childless parent and a pid that does not exist both answer 0, so a caller
# that needs to tell them apart reads the parent's own facts first.
_DARWIN_CHILD_LIST_INITIAL = 1024


class DarwinProcessFacts(NamedTuple):
    """One process as ``PROC_PIDTBSDINFO`` describes it.

    ``start_secs`` is the absolute wall-clock start instant (``pbi_start_tvsec``
    + ``pbi_start_tvusec``), i.e. the same clock ``time.time()`` reads — the pair
    a caller compares to date a process against an event it stamped itself.
    """

    zombie: bool
    start_secs: float


_darwin_libproc_tree_bound = False


def _darwin_libproc_tree_handle() -> Any:
    """The cached ``libproc`` handle with the tree-walk entry points declared.

    Declared lazily and separately from :func:`_darwin_libproc_handle` so a
    libproc missing either symbol still serves the cwd / start-time / CPU probes
    that need only ``proc_pidinfo``. Returns None when the extra entry points
    cannot be bound.
    """
    global _darwin_libproc_tree_bound
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    if _darwin_libproc_tree_bound:
        return lib
    try:
        lib.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        lib.proc_listchildpids.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
    except Exception:
        return None
    _darwin_libproc_tree_bound = True
    return lib


def darwin_child_pids(ppid: int) -> list[int] | None:
    """Direct children of *ppid* via ``proc_listchildpids``; None when unreadable.

    In-process and unprivileged: pid enumeration needs no entitlement for any
    process. The buffer grows until the kernel's answer fits, so a parent with
    more children than the initial capacity is enumerated completely rather than
    truncated. An empty list is a genuine answer only for a parent that exists —
    the syscall answers 0 for an unknown pid too — so callers that assert
    absence pair this with :func:`darwin_process_facts` on the parent.
    """
    lib = _darwin_libproc_tree_handle()
    if lib is None:
        return None
    capacity = _DARWIN_CHILD_LIST_INITIAL
    try:
        while True:
            buf = ctypes.create_string_buffer(capacity * 4)
            count = lib.proc_listchildpids(ppid, buf, capacity * 4)
            if count < 0:
                return None
            if count < capacity:
                return list(struct.unpack_from(f"<{count}i", buf.raw, 0))
            capacity *= 2
    except Exception:
        return None


def darwin_process_facts(pid: int) -> DarwinProcessFacts | None:
    """``PROC_PIDTBSDINFO`` of *pid*, or None when the process cannot be read.

    None covers "gone", "zombie" (the kernel refuses the query for one) and
    "not ours to inspect" (another user's process) alike: each is a process the
    caller cannot attribute work to. A same-uid live process always answers.
    """
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PROC_BSDINFO_SIZE)
        filled = lib.proc_pidinfo(pid, _DARWIN_PROC_PIDTBSDINFO, 0, buf, _DARWIN_PROC_BSDINFO_SIZE)
        if filled != _DARWIN_PROC_BSDINFO_SIZE:
            return None
        status = struct.unpack_from("<I", buf.raw, _DARWIN_PBI_STATUS_OFFSET)[0]
        sec = struct.unpack_from("<Q", buf.raw, _DARWIN_PBI_START_TVSEC_OFFSET)[0]
        usec = struct.unpack_from("<Q", buf.raw, _DARWIN_PBI_START_TVUSEC_OFFSET)[0]
        if sec <= 0:
            return None
        return DarwinProcessFacts(zombie=status == _DARWIN_SZOMB, start_secs=sec + usec / 1_000_000)
    except Exception:
        return None


def darwin_process_path(pid: int) -> str | None:
    """Executable path of *pid* via ``proc_pidpath``, or None when unreadable."""
    lib = _darwin_libproc_tree_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PIDPATH_MAXSIZE)
        length = lib.proc_pidpath(pid, buf, _DARWIN_PIDPATH_MAXSIZE)
        if length <= 0:
            return None
        return buf.raw[:length].decode("utf-8", errors="replace") or None
    except Exception:
        return None


_darwin_libc_sysctl: Any = None
_darwin_libc_sysctl_loaded = False


def _darwin_sysctl_handle() -> Any:
    """Cached ``libc`` handle with ``sysctl`` declared, or None when unavailable.

    Cached for the same reason as the libproc handle: the argv probe runs per
    descendant on the liveness oracle's cadence, and a fresh ``CDLL`` per call
    would dlopen every time.
    """
    global _darwin_libc_sysctl, _darwin_libc_sysctl_loaded
    if _darwin_libc_sysctl_loaded:
        return _darwin_libc_sysctl
    _darwin_libc_sysctl_loaded = True
    try:
        path = ctypes.util.find_library("c")
        if path is None:
            return None
        libc = ctypes.CDLL(path)
        libc.sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        libc.sysctl.restype = ctypes.c_int
        _darwin_libc_sysctl = libc
    except Exception:
        _darwin_libc_sysctl = None
    return _darwin_libc_sysctl


def darwin_process_argv(pid: int) -> list[str] | None:
    """argv of *pid* via ``sysctl KERN_PROCARGS2``, or None when unreadable.

    Same-uid processes only (the kernel answers EPERM for another user's), and
    bounded to :data:`_DARWIN_PROCARGS_BUFSIZE` — the kernel truncates rather
    than fails, so a very long argv comes back with its tail cut, which still
    carries the program and the head of its arguments. Returns None rather than
    an empty list when the record cannot be parsed, so a caller can fall back to
    the executable path instead of treating "unreadable" as "no arguments".
    """
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    try:
        mib = (ctypes.c_int * 3)(_DARWIN_CTL_KERN, _DARWIN_KERN_PROCARGS2, pid)
        buf = ctypes.create_string_buffer(_DARWIN_PROCARGS_BUFSIZE)
        size = ctypes.c_size_t(_DARWIN_PROCARGS_BUFSIZE)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[: size.value]
        if len(raw) < 4:
            return None
        argc = struct.unpack_from("<i", raw, 0)[0]
        if argc <= 0:
            return None
        rest = raw[4:]
        exe_end = rest.find(b"\0")
        if exe_end < 0:
            return None
        rest = rest[exe_end:].lstrip(b"\0")
        parts = rest.split(b"\0")[:argc]
        argv = [p.decode("utf-8", errors="replace") for p in parts if p]
        return argv or None
    except Exception:
        return None


def darwin_process_environ(pid: int) -> list[bytes] | None:
    """Exec-time environment of *pid* via ``sysctl KERN_PROCARGS2``, or None.

    Returns the raw ``KEY=VALUE`` entries. ``None`` means the record could not
    be read or parsed -- never an empty list for an unreadable process, so a
    caller can tell "no such variable" apart from "could not look".

    Same-uid processes only, and no entitlement or elevated privilege for our
    own: the same kernel record and the same permission contract
    :func:`darwin_process_argv` already reads. The environment here is the
    kernel's copy fixed at exec, which is why it is ownership evidence a
    process cannot forge for another, unlike anything on disk.

    The ``argc`` argv entries are skipped BY COUNT, empty strings included, so
    an *argument* that merely looks like an environment entry can never be read
    as one -- the point of the read is that a user's own shell can reproduce any
    argv.
    """
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    try:
        mib = (ctypes.c_int * 3)(_DARWIN_CTL_KERN, _DARWIN_KERN_PROCARGS2, pid)
        buf = ctypes.create_string_buffer(_DARWIN_PROCARGS_ENV_BUFSIZE)
        size = ctypes.c_size_t(_DARWIN_PROCARGS_ENV_BUFSIZE)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[: size.value]
        if len(raw) < 4:
            return None
        argc = struct.unpack_from("<i", raw, 0)[0]
        if argc <= 0:
            return None
        rest = raw[4:]
        exe_end = rest.find(b"\0")
        if exe_end < 0:
            return None
        rest = rest[exe_end:].lstrip(b"\0")
        entries = [token for token in rest.split(b"\0")[argc:] if token]
        # No entries past argv is a record whose environment is missing, not a
        # process running with an empty one: every exec'd process has some.
        return entries or None
    except Exception:
        return None


class DarwinKinfoProc(NamedTuple):
    """One process as ``sysctl KERN_PROC`` describes it -- zombies included.

    ``start_id`` is formatted exactly as :func:`get_process_start_id` formats
    the libproc answer for the same process, so the two are comparable: both
    read the kernel's ``p_start`` instant.
    """

    pid: int
    zombie: bool
    start_id: str


def _darwin_kinfo_proc_parse(raw: bytes) -> DarwinKinfoProc | None:
    """One ``kinfo_proc`` record, or None when its start instant is implausible."""
    sec = struct.unpack_from("<q", raw, _DARWIN_KP_START_TVSEC_OFFSET)[0]
    usec = struct.unpack_from("<i", raw, _DARWIN_KP_START_TVUSEC_OFFSET)[0]
    stat = struct.unpack_from("<b", raw, _DARWIN_KP_STAT_OFFSET)[0]
    pid = struct.unpack_from("<i", raw, _DARWIN_KP_PID_OFFSET)[0]
    if sec <= 0 or usec < 0 or pid <= 0:
        return None
    return DarwinKinfoProc(pid=pid, zombie=stat == _DARWIN_SZOMB, start_id=f"{sec}.{usec:06d}")


_darwin_kinfo_size_mismatch_logged = False


def _darwin_kinfo_query(selector: int, arg: int, capacity: int) -> bytes | None:
    """Raw ``sysctl KERN_PROC/<selector>/<arg>`` bytes, or None when unreadable.

    Empty bytes is a real answer ("no such process / empty group"); None is
    "could not ask". A result that is not a whole number of records means the
    struct size assumed by the offsets is wrong, and is refused the same way --
    with one warning per process, because that refusal silently disables every
    zombie reader on this host and the teardown falls back to waiting out its
    grace on an exited leader. The callers poll, so it is not logged per call.
    """
    global _darwin_kinfo_size_mismatch_logged
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    try:
        mib = (ctypes.c_int * 4)(_DARWIN_CTL_KERN, _DARWIN_KERN_PROC, selector, arg)
        buf = ctypes.create_string_buffer(capacity)
        size = ctypes.c_size_t(capacity)
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[: size.value]
        if len(raw) % _DARWIN_KINFO_PROC_SIZE:
            if not _darwin_kinfo_size_mismatch_logged:
                _darwin_kinfo_size_mismatch_logged = True
                logger.warning(
                    "sysctl KERN_PROC returned %d bytes, not a multiple of the %d-byte "
                    "kinfo_proc this build expects; the macOS zombie readers are "
                    "disabled and an exited provider root is read as still running",
                    len(raw),
                    _DARWIN_KINFO_PROC_SIZE,
                )
            return None
        return raw
    except Exception:
        return None


def darwin_kinfo_proc(pid: int) -> DarwinKinfoProc | None:
    """``sysctl KERN_PROC_PID`` facts for *pid*, or None when it cannot be read.

    Unlike :func:`darwin_process_facts` this ANSWERS for a zombie, which is the
    one state the teardown code needs to see: an exited-but-unreaped child still
    owns its pid (and, for a group leader, its pgid), and its identity must stay
    readable so the reaper can prove the pid was not recycled before waiting on
    it. None covers "gone" and "unreadable" alike; callers that must tell those
    apart use :func:`darwin_pid_is_zombie`.
    """
    if pid <= 0:
        return None
    raw = _darwin_kinfo_query(_DARWIN_KERN_PROC_PID, pid, _DARWIN_KINFO_PROC_SIZE)
    if not raw:
        return None
    facts = _darwin_kinfo_proc_parse(raw)
    if facts is None or facts.pid != pid:
        return None
    return facts


def darwin_pid_is_zombie(pid: int) -> bool | None:
    """Whether *pid* is a zombie: True / False, or None when it cannot be read.

    "Gone" is reported as None by :func:`darwin_kinfo_proc`; here it is
    distinguished, because a caller asking "has this process finished running"
    needs a pid the kernel does not list to read as finished, not as unknown.
    """
    if pid <= 0:
        return None
    raw = _darwin_kinfo_query(_DARWIN_KERN_PROC_PID, pid, _DARWIN_KINFO_PROC_SIZE)
    if raw is None:
        return None
    if not raw:
        return True  # the kernel has no such process: it exited and was reaped
    facts = _darwin_kinfo_proc_parse(raw)
    if facts is None or facts.pid != pid:
        return None
    return facts.zombie


def darwin_pgroup_members(pgid: int) -> list[DarwinKinfoProc] | None:
    """Every process the kernel lists in group *pgid*, or None when unreadable.

    Zombies are included and flagged, so a caller can tell a group that is
    genuinely empty from one held open only by its retained zombie leader --
    ``killpg(pgid, 0)`` cannot make that distinction. Sized by a first query
    with slack for processes that join between the two calls. A group that
    outgrows the slack fails the read (``ENOMEM``), and that failure is retried
    with the slack doubled each time rather than reported: the caller reads None
    as "cannot prove the group is ours" and withholds the group SIGKILL, so a
    tree forking fast enough during the teardown must not be able to make its
    own group unreadable. An answer still overflowing after the last attempt is
    refused (None) rather than returned truncated.
    """
    if pgid <= 0:
        return None
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    mib = (ctypes.c_int * 4)(_DARWIN_CTL_KERN, _DARWIN_KERN_PROC, _DARWIN_KERN_PROC_PGRP, pgid)
    slack = _DARWIN_KINFO_PGRP_SLACK
    for _attempt in range(_DARWIN_KINFO_PGRP_ATTEMPTS):
        try:
            size = ctypes.c_size_t(0)
            if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
                return None
        except Exception:
            return None
        raw = _darwin_kinfo_query(
            _DARWIN_KERN_PROC_PGRP, pgid, size.value + slack * _DARWIN_KINFO_PROC_SIZE
        )
        if raw is not None:
            members: list[DarwinKinfoProc] = []
            for start in range(0, len(raw), _DARWIN_KINFO_PROC_SIZE):
                facts = _darwin_kinfo_proc_parse(raw[start : start + _DARWIN_KINFO_PROC_SIZE])
                if facts is not None:
                    members.append(facts)
            return members
        slack *= 2
    return None


def process_cwd(pid: int) -> str | None:
    """Current working directory of *pid*, or None when no source can answer.

    Never spawns a subprocess. Callers poll this per open terminal, where a
    fork+exec of the whole gateway costs orders of magnitude more than the
    answer is worth. ``/proc`` serves Linux; macOS goes to ``libproc``. Windows
    and any host with neither source get None, leaving the caller to decide
    whether a costlier fallback is warranted.
    """
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    if sys.platform == "darwin":
        return _darwin_process_cwd(pid)
    return None


# ---------------------------------------------------------------------------
# Process termination / existence
# ---------------------------------------------------------------------------


def get_ppid(pid: int) -> int:
    """Return the parent PID of *pid*, or ``-1`` on failure.

    Linux: ``/proc/<pid>/status``.
    macOS: ``libproc.proc_pidinfo`` (no entitlement required).
    Windows: ``CreateToolhelp32Snapshot``.
    """
    if sys.platform == "linux":
        try:
            for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
                if ln.startswith("PPid:"):
                    return int(ln.split()[1])
        except Exception:
            pass
        return -1
    if sys.platform == "darwin":
        try:
            path = ctypes.util.find_library("proc")
            if path is None:
                return -1
            lib = ctypes.CDLL(path)
            lib.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            lib.proc_pidinfo.restype = ctypes.c_int
            buf = ctypes.create_string_buffer(136)
            ret = lib.proc_pidinfo(pid, 3, 0, buf, 136)  # PROC_PIDTBSDINFO=3
            if ret <= 0:
                return -1
            return struct.unpack_from("<I", buf.raw, 16)[0]
        except Exception:
            return -1
    if IS_WINDOWS:
        try:

            TH32CS_SNAPPROCESS = 0x00000002  # noqa: N806 — Windows API constant
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            entry_ptr = ctypes.POINTER(_ProcessEntry32)

            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32First.argtypes = [wintypes.HANDLE, entry_ptr]
            kernel32.Process32First.restype = wintypes.BOOL
            kernel32.Process32Next.argtypes = [wintypes.HANDLE, entry_ptr]
            kernel32.Process32Next.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == wintypes.HANDLE(-1).value:
                return -1
            try:
                entry = _ProcessEntry32()
                entry.dwSize = ctypes.sizeof(_ProcessEntry32)
                if not kernel32.Process32First(snap, ctypes.byref(entry)):
                    return -1
                while True:
                    if entry.th32ProcessID == pid:
                        return entry.th32ParentProcessID
                    if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                        return -1
            finally:
                kernel32.CloseHandle(snap)
        except Exception:
            return -1
    return -1


# macOS ``struct proc_bsdinfo`` (PROC_PIDTBSDINFO, 136 bytes) field offsets used
# below. Verified empirically against ``ps -o lstart=`` on darwin: ``pbi_ppid``
# at 16 (see get_ppid), ``pbi_start_tvsec`` at 120, ``pbi_start_tvusec`` at 128.
_DARWIN_BSDINFO_SIZE = 136
_DARWIN_OFF_START_TVSEC = 120
_DARWIN_OFF_START_TVUSEC = 128


def get_process_start_identity(
    pid: int, *, proc_root: Path | None = None
) -> ProcessStartIdentity | None:
    """Return ``(start_id, ppid)`` from one per-pid kernel record.

    Linux reads both fields from one ``/proc/<pid>/stat`` value. macOS reads
    both from one ``PROC_PIDTBSDINFO`` value. Other platforms return ``None``;
    callers that can tolerate the coarser ``ps`` fallback must bracket it
    separately so process recycling cannot join parentage from one process to
    the identity of another.
    """
    if pid <= 0:
        return None
    if sys.platform == "linux":
        stat_path = (
            Path(f"/proc/{pid}/stat") if proc_root is None else proc_root / str(pid) / "stat"
        )
        try:
            stat_data = stat_path.read_text(encoding="utf-8", errors="replace")
            close_paren = stat_data.rfind(")")
            if close_paren < 0:
                return None
            fields = stat_data[close_paren + 2 :].split()
            ppid = int(fields[1])
            start_id = fields[19]
            if ppid < 0 or not start_id.isdigit():
                return None
            return ProcessStartIdentity(start_id, ppid)
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        return _darwin_process_start_identity(pid)
    return None


def get_process_start_id(pid: int) -> str | None:
    """Return a stable per-process start-time identity string, or ``None``.

    Two processes that reuse the same PID at different times get DIFFERENT
    values, so callers can tell "still the process I spawned" from "this PID was
    recycled". The value is stable for the whole lifetime of a process and is
    safe to persist and compare from a *different* process (unlike a builtin
    ``hash()``, which is PYTHONHASHSEED-randomized per interpreter).

    Never contains ``:``, so callers may embed it in colon-delimited records.

    Implementation is deliberately **in-process and non-blocking** on every
    platform — no ``subprocess``/fork — so it is safe to call directly from the
    asyncio event loop:

    - Linux: ``/proc/<pid>/stat`` field 22 (starttime in clock ticks since boot).
    - macOS: ``libproc.proc_pidinfo`` ``pbi_start_tvsec``/``pbi_start_tvusec``
      (microsecond resolution, so processes spawned in the same second do not
      alias — unlike ``ps -o lstart=``, which is 1-second granularity).
    - Windows: creation FILETIME from a query-only process handle (100 ns).
    - Any failure: ``None``, meaning "identity unknown"; identity-sensitive
      callers must refuse authorization when they cannot confirm it.
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return process_start_time(pid)
    if sys.platform == "linux":
        identity = get_process_start_identity(pid)
        return identity.start_id if identity is not None else None
    if sys.platform == "darwin":
        try:
            start = _darwin_libproc_start_id(pid)
        except _DarwinLibprocUnavailable:
            # No libproc at all is an unrecognised host, not a zombie: the
            # identity stays unknown, and identity-sensitive callers refuse.
            return None
        if start is not None:
            return start
        # libproc refuses a zombie outright, yet a zombie still owns its pid and
        # must stay identifiable: the teardown compares this value before it
        # waits on the exited leader, and an unreadable identity would leave
        # that zombie unreaped for good. sysctl reads the same ``p_start`` from
        # the kernel's zombie list, in the same format.
        facts = darwin_kinfo_proc(pid)
        return facts.start_id if facts is not None else None
    return None


class _DarwinLibprocUnavailable(Exception):
    """``libproc`` could not be loaded -- distinct from it refusing one pid."""


def _darwin_libproc_start_id(pid: int) -> str | None:
    """macOS start instant of *pid* via ``proc_pidinfo``, or None when refused.

    Refused for a zombie (the kernel has no task to describe) and for another
    user's process alike; :func:`get_process_start_id` owns the zombie fallback.
    Raises :class:`_DarwinLibprocUnavailable` when the library itself cannot be
    loaded, so the caller can tell "this pid was refused" (fall back) from
    "nothing here can be asked" (no identity).
    """
    try:
        path = ctypes.util.find_library("proc")
        lib = ctypes.CDLL(path) if path is not None else None
    except Exception:
        lib = None
    if lib is None:
        raise _DarwinLibprocUnavailable
    try:
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
        buf = ctypes.create_string_buffer(_DARWIN_BSDINFO_SIZE)
        ret = lib.proc_pidinfo(pid, 3, 0, buf, _DARWIN_BSDINFO_SIZE)  # PROC_PIDTBSDINFO=3
        if ret <= 0:
            return None
        sec = struct.unpack_from("<Q", buf.raw, _DARWIN_OFF_START_TVSEC)[0]
        usec = struct.unpack_from("<Q", buf.raw, _DARWIN_OFF_START_TVUSEC)[0]
        if sec == 0:
            return None  # implausible — treat as unknown rather than a value
        return f"{sec}.{usec:06d}"
    except Exception:
        return None


def process_namespaces_match(pid: int, reference_pid: int) -> bool | None:
    """Compare live Linux user AND mount namespaces; unknown is never a match.

    These kernel identities survive reparenting and cannot be replaced by a
    descendant with identities from its parent's user namespace. Comparing both
    prevents a new mount view in the same user namespace from borrowing host
    authority. Process incarnation checks bracket the reads to reject PID reuse.
    """
    if sys.platform != "linux" or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 1
        for value in (pid, reference_pid)
    ):
        return None
    try:
        starts = [get_process_start_id(value) for value in (pid, reference_pid)]
        if not all(starts):
            return None
        identities = []
        for value in (pid, reference_pid):
            identity = []
            for namespace in ("user", "mnt"):
                info = Path(f"/proc/{value}/ns/{namespace}").stat()
                identity.append((info.st_dev, info.st_ino))
            identities.append(identity)
        if starts != [get_process_start_id(value) for value in (pid, reference_pid)]:
            return None
        return identities[0] == identities[1]
    except (OSError, ValueError):
        return None


def process_is_sandboxed(pid: int) -> bool | None:
    """Read inherited macOS Seatbelt state without applying a policy.

    The null-operation sandbox_check query reports whether the process has a
    sandbox, including after its original parent exits. Missing SPI, errors and
    process recycling are unknown; callers must not treat them as unsandboxed.
    """
    if sys.platform != "darwin" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        start = get_process_start_id(pid)
        if not start:
            return None
        library = ctypes.CDLL("/usr/lib/libsandbox.dylib", use_errno=True)
        check = library.sandbox_check
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        result = check(pid, None, 0)
        if get_process_start_id(pid) != start or result not in (0, 1):
            return None
        return result == 1
    except (AttributeError, OSError, ValueError):
        return None


def process_can_read_under_sandbox(pid: int, path: Path) -> bool | None:
    """Query a live Darwin process's Seatbelt read permission without reading.

    A sandboxed V1 runtime can read Global memory; a private member cannot.
    The sandbox-presence bit alone cannot distinguish those policies. Callers
    supply a trusted absolute path and accept only an explicit True result.
    """
    if sys.platform != "darwin" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        if not path.is_absolute():
            return None
        start = get_process_start_id(pid)
        if not start:
            return None
        library = ctypes.CDLL("/usr/lib/libsandbox.dylib", use_errno=True)
        check = library.sandbox_check
        # sandbox_check is variadic. Declare only its three fixed arguments;
        # Apple ARM64 passes the fourth (path) argument using the varargs ABI.
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        no_report = ctypes.c_int.in_dll(library, "SANDBOX_CHECK_NO_REPORT").value
        # SANDBOX_FILTER_PATH is 1 in Apple's SandboxSPI.h declaration.
        result = check(pid, b"file-read-data", 1 | no_report, ctypes.c_char_p(os.fsencode(path)))
        if get_process_start_id(pid) != start or result not in (0, 1):
            return None
        return result == 0
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _normalized_tcp_endpoint(endpoint: tuple[str, int]) -> tuple[str, int]:
    address = ipaddress.ip_address(endpoint[0])
    if isinstance(address, ipaddress.IPv6Address):
        address = address.ipv4_mapped or address
    if not address.is_loopback or not 0 < endpoint[1] <= 65535:
        raise ValueError("A concrete loopback endpoint is required")
    return str(address), endpoint[1]


def _linux_tcp_peer_pid(server: tuple[str, int], client: tuple[str, int]) -> int | None:
    """Map the reverse kernel connection inode to its unique process owner."""

    def endpoint(raw: str) -> tuple[str, int]:
        address, port = raw.split(":")
        packed = bytes.fromhex(address)
        # /proc uses native-endian 32-bit address words, including IPv6.
        packed = b"".join(
            int.from_bytes(packed[i : i + 4], sys.byteorder).to_bytes(4, "big")
            for i in range(0, len(packed), 4)
        )
        return _normalized_tcp_endpoint((str(ipaddress.ip_address(packed)), int(port, 16)))

    inodes: set[str] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/net/{table}").read_text(encoding="ascii").splitlines()[1:]
        except FileNotFoundError:
            continue  # IPv6 can be disabled on the host.
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "01":
                continue
            try:
                if endpoint(fields[1]) == client and endpoint(fields[2]) == server:
                    if fields[9].isdigit() and int(fields[9]) > 0:
                        inodes.add(fields[9])
            except ValueError:
                continue  # Non-loopback rows cannot identify this caller.
    if len(inodes) != 1:
        return None
    target = f"socket:[{next(iter(inodes))}]"
    owners: set[int] = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            for descriptor in (process / "fd").iterdir():
                try:
                    if os.readlink(descriptor) == target:
                        owners.add(int(process.name))
                        break
                except OSError:
                    continue  # An unrelated descriptor closed during the scan.
        except (OSError, ValueError):
            continue
    # A socket shared across processes, or hidden by /proc access controls,
    # cannot positively identify a caller. In-sandbox clients should use UDS.
    return next(iter(owners)) if len(owners) == 1 else None


def _macos_tcp_peer_pid(server: tuple[str, int], client: tuple[str, int]) -> int | None:
    """Read system lsof's machine fields, never a human-formatted port listing."""
    binary = trusted_system_bin("lsof")
    if binary is None:
        return None
    output = subprocess.check_output(
        [binary, "-nP", "-a", f"-iTCP:{client[1]}", "-sTCP:ESTABLISHED", "-Fpn"],
        stderr=subprocess.DEVNULL,
        timeout=2,
    ).decode("ascii")
    owners: set[int] = set()
    pid = 0
    for line in output.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else 0
        elif pid > 0 and line.startswith("n") and "->" in line:
            local, remote = line[1:].split("->", 1)
            try:
                local_host, local_port = local.rsplit(":", 1)
                remote_host, remote_port = remote.rsplit(":", 1)
                if (
                    _normalized_tcp_endpoint((local_host.strip("[]"), int(local_port))) == client
                    and _normalized_tcp_endpoint((remote_host.strip("[]"), int(remote_port)))
                    == server
                ):
                    owners.add(pid)
            except ValueError:
                continue
    return next(iter(owners)) if len(owners) == 1 else None


class _WindowsTcpOwnerRow(NamedTuple):
    """One row from a Windows TCP owner-PID table."""

    state: int
    local_address: bytes
    local_scope_id: int
    local_port: int
    remote_address: bytes
    remote_scope_id: int
    remote_port: int
    pid: int


_WINDOWS_TCP_TABLE_OWNER_PID_LISTENER = 3
_WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS = 4
_WINDOWS_TCP_STATE_LISTEN = 2
_WINDOWS_TCP_STATE_ESTABLISHED = 5
_WINDOWS_TCP_TABLE_MAX_BYTES = 8 * 1024 * 1024
_WINDOWS_ERROR_INSUFFICIENT_BUFFER = 122


def _windows_tcp_owner_rows(
    ipv6: bool,
    table_class: int,
) -> list[_WindowsTcpOwnerRow] | None:
    """Read one Windows TCP owner-PID table, or ``None`` on uncertainty."""
    if not IS_WINDOWS or table_class not in {
        _WINDOWS_TCP_TABLE_OWNER_PID_LISTENER,
        _WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS,
    }:
        return None
    row_format = struct.Struct("<16sII16sIIII") if ipv6 else struct.Struct("<I4sI4sII")
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)  # type: ignore[attr-defined]
        query = iphlpapi.GetExtendedTcpTable
        query.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
            wintypes.ULONG,
            ctypes.c_int,
            wintypes.ULONG,
        ]
        query.restype = wintypes.DWORD
        size = wintypes.DWORD()
        family = 23 if ipv6 else 2  # Windows AF_INET6 / AF_INET.
        if (
            query(None, ctypes.byref(size), False, family, table_class, 0)
            != _WINDOWS_ERROR_INSUFFICIENT_BUFFER
        ):
            return None
        for _ in range(3):
            if not 4 <= size.value <= _WINDOWS_TCP_TABLE_MAX_BYTES:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            result = query(
                buffer,
                ctypes.byref(size),
                False,
                family,
                table_class,
                0,
            )
            if result == _WINDOWS_ERROR_INSUFFICIENT_BUFFER:
                continue
            if result != 0 or not 4 <= size.value <= len(buffer):
                return None
            raw = buffer.raw[: size.value]
            count = struct.unpack_from("<I", raw)[0]
            if 4 + count * row_format.size > len(raw):
                return None
            rows: list[_WindowsTcpOwnerRow] = []
            for index in range(count):
                fields = row_format.unpack_from(raw, 4 + index * row_format.size)
                if ipv6:
                    (
                        local_address,
                        local_scope_id,
                        local_port_raw,
                        remote_address,
                        remote_scope_id,
                        remote_port_raw,
                        state,
                        pid,
                    ) = fields
                else:
                    (
                        state,
                        local_address,
                        local_port_raw,
                        remote_address,
                        remote_port_raw,
                        pid,
                    ) = fields
                    local_scope_id = remote_scope_id = 0
                # Ports occupy the first two bytes of a DWORD in network order.
                local_port = int.from_bytes(struct.pack("<I", local_port_raw)[:2], "big")
                remote_port = int.from_bytes(struct.pack("<I", remote_port_raw)[:2], "big")
                rows.append(
                    _WindowsTcpOwnerRow(
                        int(state),
                        bytes(local_address),
                        int(local_scope_id),
                        local_port,
                        bytes(remote_address),
                        int(remote_scope_id),
                        remote_port,
                        int(pid),
                    )
                )
            return rows
    except (AttributeError, OSError, TypeError, ValueError, struct.error):
        logger.debug("Cannot read Windows TCP owner-PID table", exc_info=True)
    return None


def get_tcp_peer_pid(
    server_endpoint: tuple[str, int], client_endpoint: tuple[str, int]
) -> int | None:
    """Resolve a loopback TCP caller from the kernel's exact 4-tuple.

    Pass the accepted socket's sockname then peername, never HTTP headers.
    Only a unique ESTABLISHED reverse connection is accepted. An unreadable,
    changing or ambiguous table returns None; authorization must fail closed.
    Linux uses /proc socket inodes, macOS system lsof, Windows the owner-PID
    table. Call from a worker thread; Unix socket peer credentials are preferred.
    """
    if not IS_WINDOWS:
        try:
            server_tuple = _normalized_tcp_endpoint(server_endpoint)
            client_tuple = _normalized_tcp_endpoint(client_endpoint)
            if sys.platform == "linux":
                return _linux_tcp_peer_pid(server_tuple, client_tuple)
            if sys.platform == "darwin":
                return _macos_tcp_peer_pid(server_tuple, client_tuple)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            logger.debug("Cannot verify loopback TCP peer PID", exc_info=True)
        return None
    try:
        server = ipaddress.ip_address(server_endpoint[0])
        client = ipaddress.ip_address(client_endpoint[0])
        addresses = [server, client]
        normalized = [
            (
                address.ipv4_mapped or address
                if isinstance(address, ipaddress.IPv6Address)
                else address
            )
            for address in addresses
        ]
        if not all(address.is_loopback for address in normalized):
            return None
        if not all(0 < endpoint[1] <= 65535 for endpoint in (server_endpoint, client_endpoint)):
            return None
        ipv6 = any(address.version == 6 for address in addresses)
        if ipv6:
            packed = [
                (
                    address.packed
                    if address.version == 6
                    else ipaddress.IPv6Address(f"::ffff:{address}").packed
                )
                for address in addresses
            ]
        else:
            packed = [address.packed for address in addresses]
        rows = _windows_tcp_owner_rows(
            ipv6,
            _WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS,
        )
        if rows is None:
            return None
        matches = [
            row.pid
            for row in rows
            if row.state == _WINDOWS_TCP_STATE_ESTABLISHED
            and row.pid > 0
            and row.local_scope_id == 0
            and row.remote_scope_id == 0
            and row.local_address == packed[1]
            and row.local_port == client_endpoint[1]
            and row.remote_address == packed[0]
            and row.remote_port == server_endpoint[1]
        ]
        return matches[0] if len(matches) == 1 else None
    except (OSError, TypeError, ValueError):
        logger.debug("Cannot verify loopback TCP peer PID", exc_info=True)
    return None


def _descendants_from_parent_map(
    root_pid: int, parent_map: dict[int, int], *, limit: int | None = None
) -> list[int]:
    """Return a breadth-first descendant list from a PID -> PPID snapshot.

    Sibling PIDs are in ascending order; ``limit`` bounds the result.
    """

    result: list[int] = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        parents = set(frontier)
        frontier = []
        for child_pid, parent_pid in sorted(parent_map.items()):
            if parent_pid in parents and child_pid not in seen:
                if limit is not None and len(seen) >= limit:
                    raise _WindowsTreeOverflow("Windows cleanup identity capacity exceeded")
                seen.add(child_pid)
                result.append(child_pid)
                frontier.append(child_pid)
    return result


@functools.lru_cache(maxsize=None)
def _folded_env_allowlist(allowed: frozenset[str] | tuple[str, ...]) -> frozenset[str]:
    """Upper-cased view of *allowed*, cached per allowlist constant."""
    return frozenset(name.upper() for name in allowed)


def env_key_allowed(key: str, allowed: frozenset[str] | tuple[str, ...]) -> bool:
    """Whether env-var *key* is in *allowed*, honoring Windows' case-insensitive env.

    On Windows, environment variable names are case-INSENSITIVE and CPython's
    ``os.environ`` upper-cases every key, so ``os.environ.items()`` yields
    ``SYSTEMROOT`` — never the ``SystemRoot`` spelling Microsoft documents and
    that env allowlists are written in. A literal membership test therefore
    drops exactly the variables the allowlist was extended to carry, and the
    failure is silent at the boundary and only surfaces in the spawned child as
    an unrelated-looking error: a Windows process without ``SystemRoot`` cannot
    resolve side-by-side assemblies or initialize Winsock, so it dies before
    ``main()`` or fails a fetch with ``getaddrinfo() thread failed to start``.

    Folding on Windows only, rather than upper-casing the allowlists, keeps
    POSIX exact: ``PATH`` and ``Path`` are genuinely different variables there,
    and a case-insensitive match would let a lookalike through.

    This is the single shared membership predicate for subprocess env
    allowlists. Each caller keeps its own *allowed* set — the sets are
    deliberately different trust boundaries — and only the matching convention
    is shared, so correctness never depends on an individual allowlist's
    casing. *allowed* must be hashable (a frozenset or tuple); the folded view
    is cached per distinct allowlist value.
    """
    if IS_WINDOWS:
        return key.upper() in _folded_env_allowlist(allowed)
    return key in allowed


_TRUSTED_SYSTEM_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/run/current-system/sw/bin")

# Windows argv carries a bare name (``taskkill``) while the file on disk carries
# an extension (``taskkill.exe``), so a trusted lookup must try the suffixes the
# loader would rather than requiring callers to spell them.
_WINDOWS_BIN_SUFFIXES = ("", ".exe", ".com")


def _windows_system_dirs() -> tuple[str, ...]:
    """Return the Windows directories a system binary may be resolved from.

    ``GetSystemDirectoryW`` is the authoritative source and, unlike
    ``%SystemRoot%``, is not read from the process environment — which is
    precisely the input this module declines to trust. The environment variable
    and the conventional install path follow only as fallbacks for the
    unexpected case where the API call fails. PowerShell ships in a versioned
    directory beside the system binaries, not inside it, so it is appended per
    root rather than assumed to sit alongside ``taskkill``.

    **The early ``return`` below is what makes "fallback" mean fallback**, and it is
    load-bearing rather than tidy. Appending the environment-derived path alongside a
    successful API read reintroduces the input this function exists to avoid: it carries
    a different casefold from what ``GetSystemDirectoryW`` reports, so the dedupe does
    not collapse the two, and any caller treating the result as "directories the user
    cannot write" then trusts a path the user names. Measured: with ``SystemRoot``
    pointed at a temp directory, ``<temp>\\System32`` appears in this tuple while the API
    answers normally, and ``computer_use.launch_windows`` accepts a binary planted there
    as system-installed. ``HKCU\\Environment`` is writable without elevation, so a
    restarted process inherits such a value.
    """

    dirs: list[str] = []
    try:
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        written = ctypes.windll.kernel32.GetSystemDirectoryW(  # type: ignore[attr-defined]
            buf, len(buf)
        )
        if 0 < written < len(buf):
            dirs.append(buf.value)
    except Exception:
        pass
    if dirs:
        # The API answered. Adding an environment-derived sibling here would buy
        # nothing (the real directory is already in hand) and would cost the
        # guarantee every caller of this function relies on.
        return tuple(dirs) + tuple(os.path.join(d, "WindowsPowerShell", "v1.0") for d in dirs)
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    # Case-insensitive dedupe: the conventionally-cased fallback can name the same
    # directory as the environment-derived one and must not be probed twice.
    seen: set[str] = set()
    for fallback in (os.path.join(root, "System32"), r"C:\Windows\System32"):
        if fallback.casefold() not in seen:
            seen.add(fallback.casefold())
            dirs.append(fallback)
    return tuple(dirs) + tuple(os.path.join(d, "WindowsPowerShell", "v1.0") for d in dirs)


#: ``FOLDERID_Documents`` — the Known Folder PowerShell derives ``$PROFILE``
#: from (``Environment.GetFolderPath(MyDocuments)``). Read through the shell API
#: rather than ``%USERPROFILE%\Documents`` because the folder is redirectable
#: (OneDrive, roaming profiles, group policy) and PowerShell follows the
#: redirection, so a guess from the environment would check the wrong place.
_FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"

#: ``(sub-directory, file)`` pairs under Documents that Windows PowerShell 5.1
#: reads before running a ``-Command``. This is the shell kiro-cli spawns, so
#: this is the profile set that matters here. The two files are the all-hosts
#: ``profile.ps1`` and its host-specific companion. PowerShell 7 (``pwsh``) has
#: its own profiles under ``Documents\PowerShell``; kiro-cli does not spawn it,
#: so those files never run before the command and are deliberately not listed.
_POWERSHELL_USER_PROFILES = (
    ("WindowsPowerShell", "profile.ps1"),
    ("WindowsPowerShell", "Microsoft.PowerShell_profile.ps1"),
)


def windows_documents_dir() -> str | None:
    """The user's Documents Known Folder, or ``None`` when Windows cannot say.

    ``None`` is the fail-closed answer for every caller: a check that needs to
    know what is in Documents cannot proceed on a guess.
    """

    if not IS_WINDOWS:
        return None
    try:

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_uint8 * 8),
            ]

        text = _FOLDERID_DOCUMENTS.strip("{}")
        parts = text.split("-")
        guid = _GUID()
        guid.Data1 = int(parts[0], 16)
        guid.Data2 = int(parts[1], 16)
        guid.Data3 = int(parts[2], 16)
        tail = bytes.fromhex(parts[3] + parts[4])
        guid.Data4 = (ctypes.c_uint8 * 8)(*tail)
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        if result != 0 or not out.value:
            return None
        try:
            return str(out.value)
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:
        return None


def windows_powershell_profile_paths() -> tuple[str, ...] | None:
    """Every per-user PowerShell profile path, or ``None`` when Documents is unknown.

    These are the scripts PowerShell runs BEFORE the command it was handed,
    unless it was started with ``-NoProfile``. They live under the user's own
    Documents folder, so whatever can write as the user -- an agent included --
    can write them, and a function defined there shadows any program name. A
    caller that vouches for a program by name has to know none of them exist.
    Existence is the caller's question; this only names where to look.
    """

    documents = windows_documents_dir()
    if documents is None:
        return None
    return tuple(
        os.path.join(documents, subdir, filename) for subdir, filename in _POWERSHELL_USER_PROFILES
    )


# Names already probed for the diagnostic below, so the message costs one PATH
# scan per name per process. Only the *message* is one-shot; resolution itself
# stays uncached, so a tool that lands in a trusted directory later is still
# found on the next call.
_UNPINNED_TOOL_PROBED: set[str] = set()


def _log_tool_outside_trusted_dirs(name: str, directories: tuple[str, ...]) -> None:
    """Log once per *name* when the pin is what made a present tool unavailable.

    A host that keeps its binaries outside the FHS system directories (NixOS's
    ``/run/current-system/sw/bin``, a Homebrew or conda prefix) has a working
    ``lsof`` that this lookup still declines, and the caller's degradation is
    otherwise indistinguishable from the tool not being installed:
    ``listening_pid_tool_available()`` would tell such an operator to install a
    tool they already have, and ``kirocrew stop`` would quietly no-op. The
    ``PATH`` result is read to write the message and is never spawned, so
    reporting it does not widen what may run.
    """

    if name in _UNPINNED_TOOL_PROBED:
        return
    # Concurrent first probes of one name can duplicate the line. That is
    # cheaper than serializing a filesystem scan behind a lock for a message.
    _UNPINNED_TOOL_PROBED.add(name)
    on_path = shutil.which(name)
    if not on_path:
        return
    logger.warning(
        "%s is on PATH at %s but does not resolve under the trusted system "
        "directories (%s), so it is treated as unavailable; OS introspection "
        "that needs it degrades instead of running a PATH-chosen binary",
        name,
        on_path,
        ", ".join(directories),
    )


#: Git for Windows' fixed install roots. ``trusted_system_bin`` only probes the
#: system directories, and git is never there on Windows, so without this every
#: Windows source install resolves ``git`` to ``None``. Fixed literal roots, not
#: ``%ProgramFiles%``: reading the environment would let a poisoned variable
#: redirect the lookup to an agent-writable directory — the exact hole the pin
#: exists to close. A non-default-drive install still misses and degrades to
#: "unavailable", which is honest: the fallback widens the pin only to paths an
#: unprivileged attacker cannot write.
_WINDOWS_GIT_DIRS = (
    r"C:\Program Files\Git\cmd",
    r"C:\Program Files (x86)\Git\cmd",
)


def trusted_git_bin() -> str | None:
    """The ``git`` executable resolved off ``PATH``, or ``None`` if untrustworthy.

    :func:`trusted_system_bin` plus the Windows install-root fallback, shared by
    every caller that spawns git for a privileged or unattended purpose (the
    doctor's read-only probes, and the update seam — where what git returns
    decides which code the process installs and re-executes).

    ``None`` means "do not spawn git at all". Callers MUST treat it as a refusal;
    falling back to a bare ``"git"`` reinstates the hazard.
    """
    git = trusted_system_bin("git")
    if git is None and IS_WINDOWS:
        for directory in _WINDOWS_GIT_DIRS:
            candidate = os.path.join(directory, "git.exe")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None
    return git


def trusted_system_bin(name: str) -> str | None:
    """Resolve *name* from fixed system directories, ignoring ``PATH``.

    A gateway's ``PATH`` can legitimately lead with agent-writable directories
    (a worktree venv's ``bin``, ``~/.local/bin``), so a bare argv name lets a
    planted shim run with the gateway's environment. Callers that shell out for
    OS introspection resolve through here and treat ``None`` as "unavailable".

    A miss on a host whose tools live elsewhere is a real functional
    degradation, so it is logged once per name rather than left silent. The pin
    still decides; the log only makes the decision diagnosable.

    Deliberately uncached. The lookup is a handful of ``stat`` calls on
    teardown and introspection paths, and caching the *miss* would pin "tool
    absent" for the lifetime of a long-lived gateway, so an ``lsof`` installed
    after boot would never be picked up.
    """

    if IS_WINDOWS:
        directories: tuple[str, ...] = _windows_system_dirs()
        suffixes: tuple[str, ...] = _WINDOWS_BIN_SUFFIXES
    else:
        directories = _TRUSTED_SYSTEM_BIN_DIRS
        suffixes = ("",)
    for directory in directories:
        for suffix in suffixes:
            candidate = os.path.join(directory, name + suffix)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    _log_tool_outside_trusted_dirs(name, directories)
    return None


#: The install prefix :data:`_TRUSTED_SYSTEM_BIN_DIRS` deliberately omits. It is
#: the default ``--bin-dir`` of AWS's own CLI installers, so a tool resolved ONLY
#: from here is common — but on an Intel macOS with Homebrew this same directory
#: is owned by the console user, which is exactly the "a same-uid process can
#: supply the binary" hole the trusted lookup exists to close. Membership alone
#: therefore proves nothing; :func:`trusted_aws_bin` gates it on ownership.
_LOCAL_SYSTEM_BIN_DIR = "/usr/local/bin"

#: Set once the ``/usr/local/bin/aws`` copy has been declined and logged.
_UNTRUSTED_AWS_BIN_LOGGED = False


#: Hard stop on symlink expansion while resolving one path. Linux's own limit is
#: 40; a path needing more than that is a loop or an attack rather than an install
#: layout, and an unbounded walk would hang instead of answering.
_MAX_SYMLINK_HOPS = 40


def _root_owned_entry(path: str) -> bool:
    """*path* is root-owned, and no non-root principal can write it.

    Two instruments, because the mode bits alone are an incomplete answer.
    ``st_uid`` plus the group and world write bits cover the POSIX permission
    model; ``os.access(..., effective_ids=True)`` covers what that model cannot
    express — a POSIX ACL's named-user entry (``user:me:w``) does not appear in
    ``st_mode`` at all, since the group bits show the ACL *mask* rather than the
    entry, so a mode-only check calls a root-owned ``0755`` path safe while the
    account this gate defends against can rewrite it. ``faccessat(AT_EACCESS)``
    evaluates the full ACL, which is why :data:`_ACCESS_HONOURS_EFFECTIVE_IDS`
    gates that arm rather than a bare ``os.access``: without ``faccessat`` the
    call would answer about the real ids only and the ACL arm would be silently
    absent.

    The ACL arm cannot run as root, and that is why running as root DECLINES rather
    than accepts. As root ``os.access`` answers True for essentially everything, so
    the arm has no signal — and the entry it would have caught is one granting a
    NON-root user write, which is precisely the case root must not execute. Treating
    "cannot establish" as "safe" on a path about to be exec'd as root would turn the
    strongest caller into the least protected one. The cost is bounded and visible: a
    root-run diagnostic loses only this ``/usr/local/bin`` fallback, since
    :func:`trusted_system_bin` does not route through here, and its caller reports the
    decline rather than silently resolving nothing.

    Not to be confused with :func:`path_writable_by_current_user`, which answers
    the opposite question ("could this account write it") over two LEXICAL chains
    and rounds unknown to writable. That shape cannot see a mid-chain symlink hop,
    which is why :func:`_is_root_owned_path` walks :func:`traversed_components` and
    calls this per entry instead of delegating wholesale.

    ``os.stat`` rather than ``os.lstat`` on purpose: every caller has already
    established that *path* is not a symlink, so the two agree, and ``os.stat`` is
    the call the rest of this module's ownership checks use.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    if not IS_POSIX:
        return True
    if 0 in (os.getuid(), os.geteuid()):
        return False
    if _ACCESS_HONOURS_EFFECTIVE_IDS and os.access(path, os.W_OK, effective_ids=True):
        return False
    return True


def traversed_components(path: str | os.PathLike[str]) -> list[Path] | None:
    """Every directory a component-by-component walk to *path* passes through, then the target.

    Resolves *path* one COMPONENT at a time, expanding each symlink it meets — a
    directory component's as much as the final name's — and records every
    directory the walk actually reads, on the original side and on each expanded
    target's side alike, followed by the final resolved target. Visit order, each
    entry once. No ownership or permission question is asked here: this is the
    ENUMERATION that any executable-trust predicate must ask its question over,
    kept separate so that callers with different questions ("root's alone to
    change", "not another uid's", "not writable by this process") share the walk
    rather than each spelling a weaker one.

    Two shorter shapes were tried for this enumeration and both were bypassable,
    which is why every caller must use this one rather than either:

    * ``realpath`` and then walk the result collapses the chain, so
      ``/usr/local/bin/aws -> /tmp/link -> /usr/bin/aws`` yields ``/usr/bin`` and
      its ancestors with ``/tmp`` — the one directory where the retarget
      happens — never named;
    * walking ``os.path.dirname`` / ``Path.parents`` lexically misses a symlinked
      COMPONENT, because ``os.stat`` follows symlinks while ``dirname`` does not:
      for ``/usr/local/bin -> /opt/x/bin`` the target's own parent ``/opt/x``,
      which can replace it wholesale, is never visited.

    Every lexical ancestor of the fully resolved target is in the result (the
    walk builds the target's path one directory at a time, and each directory is
    recorded before a name is joined onto it), so a question asked over this list
    is asked over a superset of ``resolved.parents`` plus the target. Symlinks
    met on the way are deliberately absent: their mode is meaningless (0777 on
    Linux) and they cannot be edited in place, only replaced, which the directory
    holding them — present in the result — already governs.

    POSIX path semantics (``os.sep``-rooted); Windows callers keep their own
    ACL-driven chains and must not route through here.

    ``None`` on any ``OSError`` and on any path needing more than
    :data:`_MAX_SYMLINK_HOPS` expansions. The fail direction is "could not
    enumerate", never a shorter list: a caller that treats ``None`` as anything
    but a refusal is answering a question it did not ask.
    """
    text = os.fspath(path)
    if not os.path.isabs(text):
        text = os.path.abspath(text)
    # Reversed, so `pop()` yields the next component and a symlink's own components
    # can be pushed on to be consumed before the rest of the original path.
    pending = text.split(os.sep)
    pending.reverse()
    resolved = os.sep
    hops = 0
    visited: dict[str, None] = {}
    while pending:
        name = pending.pop()
        if name in ("", os.curdir):
            continue
        if name == os.pardir:
            resolved = os.path.dirname(resolved)
            continue
        # About to read `resolved` as a directory, so it is one the walk depends
        # on. Recorded here rather than after descending, so the target side of
        # an expanded symlink is covered by the same line.
        visited[resolved] = None
        candidate = os.path.join(resolved, name)
        try:
            is_link = os.path.islink(candidate)
        except OSError:
            return None
        if not is_link:
            resolved = candidate
            continue
        hops += 1
        if hops > _MAX_SYMLINK_HOPS:
            return None
        try:
            target = os.readlink(candidate)
        except OSError:
            return None
        if os.path.isabs(target):
            resolved = os.sep
        pending.extend(reversed(target.split(os.sep)))
    visited[resolved] = None
    return [Path(component) for component in visited]


def _is_root_owned_path(path: str) -> bool:
    """True when nothing on the way to *path*'s target is another uid's to change.

    :func:`_root_owned_entry` asked over :func:`traversed_components` — every
    directory the walk reads and the final target. A directory has to be
    root-owned with no group or world write bit, because replacing an entry needs
    write on its DIRECTORY rather than on the entry, and a group-writable
    directory is writable by more than root whoever owns it. The final target has
    to satisfy the same rule as a file, since a writable regular file can be
    edited in place without touching any directory.

    POSIX semantics. Windows callers do not reach it (:func:`trusted_aws_bin`
    answers ``None`` there before the gate).

    A walk that cannot be enumerated (``None``) answers ``False``. The fail
    direction is "decline", never "assume".
    """
    components = traversed_components(path)
    if components is None:
        return False
    return all(_root_owned_entry(str(component)) for component in components)


def _local_aws_bin_candidate() -> str | None:
    """The ``/usr/local/bin/aws`` file, if there is an executable one. No gate."""
    if IS_WINDOWS:
        return None
    candidate = os.path.join(_LOCAL_SYSTEM_BIN_DIR, "aws")
    if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
        return None
    return candidate


def _is_native_program(path: str) -> bool:
    """*path*'s own bytes ARE the program: not a script naming an interpreter.

    A ``#!`` line hands execution to a DIFFERENT file, and which file that is lives
    in the script's CONTENT — so the ownership walk, which validates the path, says
    nothing about it. A root-owned wrapper whose shebang points into a user-writable
    tree is not exotic: ``sudo pip install awscli`` against a pyenv or
    home-directory Python produces exactly that.

    Rather than validate the interpreter — and then its libraries, and then its own
    module search path, a set with no end — the fallback simply does not trust a
    script. The case it exists for is unaffected: AWS CLI v2's installer ships a
    native executable, reached through the symlink this resolver already follows.

    A read failure answers ``False``: unreadable is not shown-to-be-safe.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(2) != b"#!"
    except OSError:
        return False


def _local_aws_bin_is_trusted(candidate: str) -> bool:
    """Every condition the ``/usr/local/bin`` fallback requires, in ONE place.

    One predicate because two callers ask it — the resolver and the
    decline-reporter — and a resolver that accepted what the reporter called
    refused (or the reverse) would state two contradictory facts about one file.
    That has already gone wrong once on this path, so the conditions live together
    rather than being spelled out twice.
    """
    return _is_root_owned_path(candidate) and _is_native_program(candidate)


def trusted_aws_bin() -> str | None:
    """The ``aws`` executable to spawn, or ``None`` if there is none to trust.

    :func:`trusted_system_bin` plus a ``/usr/local/bin`` fallback, the same shape
    as :func:`trusted_git_bin` — one tool-specific resolver owning that tool's
    install-location knowledge, so its callers state a tool and not a search
    policy. The fallback exists because ``/usr/local/bin`` is the default
    ``--bin-dir`` of AWS's own installers, and a lookup that missed it made the
    doctor report "no AWS CLI" on a perfectly ordinary host.

    It is NOT a loosening. The directory is not in
    :data:`_TRUSTED_SYSTEM_BIN_DIRS` and must not be: on an Intel macOS the
    Homebrew prefix IS ``/usr/local``, owned by the console user, so membership
    alone would let a same-uid process supply every pinned tool. The copy is
    accepted only when :func:`_is_root_owned_path` finds it, its realpath and
    every directory above both owned by root and not group/world-writable.

    ``None`` on Windows for the fallback half (the directory is a POSIX
    convention), and ``None`` when the gate declines — the same "unavailable"
    answer as an absent CLI, which callers already handle. A caller that must
    tell the two apart asks :func:`aws_bin_declined_on_ownership`.
    """
    global _UNTRUSTED_AWS_BIN_LOGGED

    system_copy = trusted_system_bin("aws")
    if system_copy:
        return system_copy
    candidate = _local_aws_bin_candidate()
    if candidate is None:
        return None
    if not _local_aws_bin_is_trusted(candidate):
        if not _UNTRUSTED_AWS_BIN_LOGGED:
            # Once per process, matching `_log_tool_outside_trusted_dirs`: a
            # caller may probe several times in one run, and the verdict
            # cannot change between them.
            _UNTRUSTED_AWS_BIN_LOGGED = True
            logger.warning(
                "%s is not root-owned or sits under a writable directory — declining to run it",
                candidate,
            )
        return None
    return candidate


def aws_bin_declined_on_ownership() -> str | None:
    """The ``/usr/local/bin/aws`` that :func:`trusted_aws_bin` REFUSED, if any.

    ``None`` when there is no such copy, and ``None`` when the gate accepts the
    one there is. For a caller that must tell an operator "you have this tool,
    and I will not run it" apart from "you do not have this tool" — two different
    facts, where reporting the second while the first holds is a confident wrong
    answer, not a cost of the gate.

    Worth knowing how ordinary the decline is: Debian policy has directories
    under ``/usr/local`` owned ``root:staff`` mode ``2775``, so on a stock Debian
    or Ubuntu host the gate declines by DEFAULT and the fallback never fires.
    That degradation is intended — a member of ``staff`` can replace the binary,
    which is the hole the gate exists to close, and loosening it to accept a
    group-writable directory would forfeit the property outright. What is not
    acceptable is a diagnostic that reads the decline as absence.

    ``None`` as well when :func:`trusted_system_bin` found a copy, because then no
    decline explains anything: the resolver succeeded, and a caller reporting "the
    local copy was refused" would be offering that as the reason for some later,
    unrelated failure.
    """
    if trusted_system_bin("aws"):
        return None
    candidate = _local_aws_bin_candidate()
    if candidate is None or _local_aws_bin_is_trusted(candidate):
        return None
    return candidate


def trusted_system_path() -> str | None:
    """A ``PATH`` value containing only the trusted system directories.

    Pinning a spawned binary is not always enough: some launchers are shell
    scripts that dispatch to helpers of their own through ``PATH``, and
    ``xdg-open`` is the one that matters here — it reaches for ``gio``,
    ``gvfs-open``, ``exo-open`` or ``kde-open``. Handing such a process the
    gateway's inherited ``PATH`` would reopen at one remove exactly the hole
    :func:`trusted_system_bin` closes, so callers replace ``PATH`` with this and
    leave the rest of the environment alone (``DISPLAY``,
    ``DBUS_SESSION_BUS_ADDRESS`` and ``XDG_*`` are what let a launcher reach the
    running desktop session).

    ``None`` on Windows, where helpers live beside their install rather than
    being resolved from a colon-separated search path.
    """

    if IS_WINDOWS:
        return None
    return os.pathsep.join(_TRUSTED_SYSTEM_BIN_DIRS)


def reveal_in_file_manager(target: str) -> bool:
    """Show *target* in the host's file manager. ``True`` if a launcher started.

    Off macOS the **containing folder** is opened, never the target itself, and
    unconditionally so: ``explorer.exe <file>`` launches the file's *associated
    application* — the execution sink this capability exists to avoid — and making
    the rule structural means a caller handing over a request-derived path cannot
    reach it, with no filesystem probe of that path needed to decide. It also
    matches what this endpoint already did before the launcher moved here. macOS is
    the exception that needs no derivation: ``open -R`` reveals its argument and
    never opens it.

    This lives beside :func:`trusted_system_bin` rather than in the dashboard
    handler that wants it, because three separate rules meet on the ``Popen``
    lines below and only this location satisfies all of them:

    * The launcher must be an ABSOLUTE path, never a bare ``open`` / ``xdg-open``
      argv name: a gateway's ``PATH`` can lead with an agent-writable directory,
      so a bare name lets a planted shim run on a click the user initiated.
    * Those absolute paths are POSIX path literals, which the cross-platform
      portability gate rejects everywhere except this module — the module it
      excludes precisely because such literals have to live somewhere.
    * The command position must be a literal at the call site, with the
      caller-supplied target as a later element of that same literal list.
      Hoisting either into a variable makes the whole command line read as
      user-controlled to the SAST passes.

    A launcher that is absent (or present and refusing to run — AppLocker, a
    revoked exec bit, an exhausted process table) returns ``False`` so the caller
    can degrade rather than fail the request.
    """

    env = _reveal_env()
    try:
        if sys.platform == "darwin":
            if not os.path.isfile("/usr/bin/open"):
                return False
            subprocess.Popen(["/usr/bin/open", "-R", target], env=env)
            return True
        # Everything else opens the CONTAINING FOLDER, never the target itself —
        # `explorer.exe <file>` would launch the file's associated application, the
        # execution sink this capability exists to avoid. Unconditional, so no
        # filesystem probe of a caller-supplied path is needed to decide, and so a
        # caller cannot reach the sink by handing over a file.
        folder = os.path.dirname(target)
        if not folder:
            return False
        if IS_WINDOWS:
            # A literal, so the command position stays constant. The conventional
            # location is not universal, so an image that keeps Windows elsewhere
            # reads as "no file manager here" rather than spawning something else.
            if not os.path.isfile(r"C:\Windows\explorer.exe"):
                return False
            subprocess.Popen([r"C:\Windows\explorer.exe", folder], env=env)
            return True
        if not os.path.isfile("/usr/bin/xdg-open"):
            return False
        subprocess.Popen(["/usr/bin/xdg-open", folder], env=env)
        return True
    except OSError:
        logger.warning(
            "file manager did not start for %s; caller should degrade", target, exc_info=True
        )
        return False


def open_with_default_app(target: str) -> bool:
    """Launch *target* with its associated application. ``True`` if it started.

    Separate from :func:`reveal_in_file_manager` because it is the opposite
    intent — this one deliberately RUNS what the path points at — and because
    Windows is refused outright: there, launching by association is reached
    through the shell rather than an argv the caller can inspect, and the path
    typically arrives from a request. POSIX keeps it: ``open`` / ``xdg-open`` hand
    the file to the desktop's handler without a shell in between.
    """

    if IS_WINDOWS:
        return False
    env = _reveal_env()
    try:
        if sys.platform == "darwin":
            if not os.path.isfile("/usr/bin/open"):
                return False
            subprocess.Popen(["/usr/bin/open", target], env=env)
            return True
        if not os.path.isfile("/usr/bin/xdg-open"):
            return False
        subprocess.Popen(["/usr/bin/xdg-open", target], env=env)
        return True
    except OSError:
        logger.warning(
            "default application did not start for %s; caller should degrade", target, exc_info=True
        )
        return False


def _reveal_env() -> dict[str, str]:
    """The gateway environment with ``PATH`` pinned to trusted system directories.

    Pinning the launcher binary is not enough on its own: ``xdg-open`` is a shell
    script that dispatches to whichever helper it finds on ``PATH`` — ``gio``,
    ``gvfs-open``, ``exo-open``, ``kde-open``. Only ``PATH`` is replaced, because
    the rest of the environment is what lets a launcher reach the running desktop
    session (``DISPLAY``, ``DBUS_SESSION_BUS_ADDRESS``, ``XDG_*``).
    """

    env = dict(os.environ)
    pinned = trusted_system_path()
    if pinned is not None:
        env["PATH"] = pinned
    return env


def tool_outside_trusted_dirs(name: str) -> str | None:
    """Where ``PATH`` finds *name* when :func:`trusted_system_bin` declined it.

    ``None`` means the pin is not the reason the tool is unavailable: either it
    resolved normally, or it is not installed anywhere ``PATH`` can see. Callers
    use this to word a diagnostic — an operator on a host that keeps its
    binaries elsewhere needs to be told where theirs actually is, not to install
    a tool they already have. The path is reported and never spawned, so asking
    does not widen what may run.
    """

    if trusted_system_bin(name) is not None:
        return None
    return shutil.which(name)


def _posix_process_parent_map() -> dict[int, int]:
    """Return the PID -> PPID view of the shared POSIX process snapshot."""

    processes = _posix_process_snapshot()
    if processes is None:
        return {}
    return {process_pid: process.ppid for process_pid, process in processes.items()}


class ProcessIdentitySource(enum.Enum):
    """The encoding and reader that produced a process-start identity."""

    ATOMIC = "atomic"
    WINDOWS = "windows"
    LSTART = "lstart"


class ProcessDescendantIdentity(NamedTuple):
    """A descendant PID bound to its parent, start identity, and source."""

    pid: int
    ppid: int
    start_time: str
    source: ProcessIdentitySource = ProcessIdentitySource.ATOMIC


class _ProcessStartOrder(enum.Enum):
    """Creation order for one process ancestry edge."""

    LATER = "later"
    EARLIER = "earlier"
    INCONCLUSIVE = "inconclusive"


def _process_start_order(
    child_token: str,
    parent_token: str,
) -> _ProcessStartOrder:
    """Compare process-start identities without collapsing uncertainty."""

    def _numeric_parts(token: str) -> tuple[int, str] | None:
        whole, separator, fraction = token.partition(".")
        if not whole.isdigit() or (separator and not fraction.isdigit()):
            return None
        return int(whole), fraction

    child_numeric = _numeric_parts(child_token)
    parent_numeric = _numeric_parts(parent_token)
    if child_numeric is not None or parent_numeric is not None:
        if child_numeric is None or parent_numeric is None:
            return _ProcessStartOrder.INCONCLUSIVE
        child_whole, child_fraction = child_numeric
        parent_whole, parent_fraction = parent_numeric
        if child_whole > parent_whole:
            return _ProcessStartOrder.LATER
        if child_whole < parent_whole:
            return _ProcessStartOrder.EARLIER
        width = max(len(child_fraction), len(parent_fraction))
        child_fraction = child_fraction.ljust(width, "0")
        parent_fraction = parent_fraction.ljust(width, "0")
        if child_fraction > parent_fraction:
            return _ProcessStartOrder.LATER
        if child_fraction < parent_fraction:
            return _ProcessStartOrder.EARLIER
        return _ProcessStartOrder.INCONCLUSIVE

    try:
        child_time = time.strptime(child_token, "%a %b %d %H:%M:%S %Y")[:6]
        parent_time = time.strptime(parent_token, "%a %b %d %H:%M:%S %Y")[:6]
    except ValueError:
        return _ProcessStartOrder.INCONCLUSIVE
    if child_time > parent_time:
        return _ProcessStartOrder.LATER
    if child_time < parent_time:
        return _ProcessStartOrder.EARLIER
    return _ProcessStartOrder.INCONCLUSIVE


class _PosixProcessSnapshotRow(NamedTuple):
    """One parsed POSIX process row; identity may be unavailable."""

    ppid: int
    start_time: str | None


def _linux_direct_child_pids(pid: int, proc_root: Path) -> list[int] | None:
    """Return direct children from this pid's kernel ``children`` files."""
    task_root = proc_root / str(pid) / "task"
    try:
        tasks = list(task_root.iterdir())
    except OSError:
        return None
    children: set[int] = set()
    read_any = False
    for task in tasks:
        if not task.name.isdigit():
            continue
        try:
            raw = (task / "children").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        read_any = True
        try:
            children.update(int(token) for token in raw.split())
        except ValueError:
            return None
    return sorted(children) if read_any else None


def _direct_child_pids_for_identity_walk(pid: int, proc_root: Path | None) -> list[int] | None:
    """Return direct children without scanning unrelated host processes."""
    if IS_LINUX:
        root = proc_root if proc_root is not None else Path("/proc")
        return _linux_direct_child_pids(pid, root)
    if sys.platform == "darwin":
        return darwin_child_pids(pid)
    return None


def _atomic_process_descendant_identities(
    root_pid: int, proc_root: Path | None
) -> list[ProcessDescendantIdentity] | None:
    """Walk descendants whose parentage and start ID share one kernel read."""

    def _identity(pid: int) -> ProcessStartIdentity | None:
        if IS_LINUX:
            root = proc_root if proc_root is not None else Path("/proc")
            return get_process_start_identity(pid, proc_root=root)
        return get_process_start_identity(pid)

    root_identity = _identity(root_pid)
    if root_identity is None:
        return None
    frontier = [(root_pid, root_identity)]
    seen = {root_pid}
    descendants: list[ProcessDescendantIdentity] = []
    index = 0
    while index < len(frontier):
        parent_pid, expected_parent = frontier[index]
        index += 1
        children = _direct_child_pids_for_identity_walk(parent_pid, proc_root)
        if children is None:
            return None
        # The child list belongs to the expected parent only if that parent kept
        # the same kernel identity across the enumeration.
        if _identity(parent_pid) != expected_parent:
            return None
        for child_pid in children:
            if child_pid in seen or child_pid <= 0:
                continue
            child_identity = _identity(child_pid)
            # The child-list row and identity read must describe one stable task.
            # Disappearance or reparenting makes the whole walk inconclusive.
            if child_identity is None or child_identity.ppid != parent_pid:
                return None
            start_order = _process_start_order(
                child_identity.start_id,
                expected_parent.start_id,
            )
            if start_order is _ProcessStartOrder.INCONCLUSIVE:
                return None
            if start_order is _ProcessStartOrder.EARLIER:
                continue
            seen.add(child_pid)
            descendants.append(
                ProcessDescendantIdentity(
                    child_pid,
                    child_identity.ppid,
                    child_identity.start_id,
                )
            )
            frontier.append((child_pid, child_identity))
    return descendants


def _posix_process_snapshot() -> dict[int, _PosixProcessSnapshotRow] | None:
    """Parse one trusted ``ps`` snapshot without aborting on malformed rows."""
    if IS_WINDOWS:
        return None
    ps_bin = trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        output = subprocess.check_output(
            [ps_bin, "-Ao", "pid=,ppid=,lstart="],
            timeout=5,
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    processes: dict[int, _PosixProcessSnapshotRow] = {}
    for line in output.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        try:
            process_pid = int(parts[0])
            parent_pid = int(parts[1])
        except ValueError:
            continue
        if process_pid <= 0 or parent_pid < 0:
            continue
        start_time = parts[2].strip() if len(parts) == 3 else ""
        processes[process_pid] = _PosixProcessSnapshotRow(
            parent_pid,
            start_time or None,
        )
    return processes


def _posix_process_identity_map(
    root_pid: int | None = None,
) -> dict[int, ProcessDescendantIdentity] | None:
    """Keep stable identities; require a complete first-snapshot root subtree."""
    before = _posix_process_snapshot()
    if before is None:
        return None
    after = _posix_process_snapshot()
    if after is None:
        return None
    if root_pid is not None and root_pid in before:
        parent_map = {pid: row.ppid for pid, row in before.items()}
        root_subtree = [root_pid, *_descendants_from_parent_map(root_pid, parent_map)]
        if any(
            before[pid].start_time is None or after.get(pid) != before[pid] for pid in root_subtree
        ):
            return None
    return {
        process_pid: ProcessDescendantIdentity(
            process_pid,
            process.ppid,
            process.start_time,
            ProcessIdentitySource.LSTART,
        )
        for process_pid, process in before.items()
        if process.start_time is not None and after.get(process_pid) == process
    }


def _ordered_posix_descendant_identities(
    root_pid: int,
    identities: Mapping[int, ProcessDescendantIdentity],
) -> list[ProcessDescendantIdentity] | None:
    """Return ordered fallback descendants, preserving uncertain edges."""
    parent_map = {process_pid: identity.ppid for process_pid, identity in identities.items()}
    admitted = {root_pid}
    descendants: list[ProcessDescendantIdentity] = []
    for process_pid in _descendants_from_parent_map(root_pid, parent_map):
        identity = identities[process_pid]
        parent = identities.get(identity.ppid)
        if identity.ppid not in admitted or parent is None:
            continue
        start_order = _process_start_order(
            identity.start_time,
            parent.start_time,
        )
        if start_order is _ProcessStartOrder.INCONCLUSIVE:
            return None
        if start_order is _ProcessStartOrder.EARLIER:
            continue
        admitted.add(process_pid)
        descendants.append(identity)
    return descendants


def _windows_chain_to_root(
    candidate_pid: int,
    root_pid: int,
    parent_map: dict[int, int],
) -> tuple[int, ...] | None:
    """Return candidate-to-root PIDs, empty when foreign, or none on a cycle."""
    chain: list[int] = []
    seen: set[int] = set()
    current = candidate_pid
    while current != root_pid:
        if current in seen:
            return None
        seen.add(current)
        parent = parent_map.get(current)
        if type(parent) is not int or parent <= 0:
            return ()
        chain.append(current)
        current = parent
    chain.append(root_pid)
    return tuple(chain)


def _windows_process_descendant_identities(
    root_pid: int,
    candidate_pids: set[int] | None = None,
) -> list[ProcessDescendantIdentity] | None:
    """Return creation-time-bound Windows descendants or listener candidates.

    Toolhelp supplies PID-to-PPID edges but no process identity. Take the edge
    snapshot twice and bracket it with the query-only creation-time primitive.
    Each candidate must keep the same candidate-to-root chain, including every
    edge and creation ID. A strictly earlier child is excluded; an equal or
    unparseable order makes that candidate inconclusive. Unrelated helper
    siblings may appear or disappear without invalidating a stable listener
    owner's chain.
    """
    try:
        first_map = _windows_process_parent_map()
        if root_pid not in first_map:
            return None
        if candidate_pids is None:
            candidates = _descendants_from_parent_map(root_pid, first_map)
        else:
            candidates = sorted(
                process_pid
                for process_pid in candidate_pids
                if type(process_pid) is int and process_pid > 1 and process_pid != root_pid
            )
        first_chains = {
            process_pid: _windows_chain_to_root(process_pid, root_pid, first_map)
            for process_pid in candidates
        }
        tracked = {root_pid}
        for chain in first_chains.values():
            if chain:
                tracked.update(chain)
        first_ids = {process_pid: get_process_start_id(process_pid) for process_pid in tracked}

        second_map = _windows_process_parent_map()
        if root_pid not in second_map:
            return None
        second_chains = {
            process_pid: _windows_chain_to_root(process_pid, root_pid, second_map)
            for process_pid in candidates
        }
        second_ids = {process_pid: get_process_start_id(process_pid) for process_pid in tracked}
    except Exception:
        return None

    first_root_id = first_ids.get(root_pid)
    second_root_id = second_ids.get(root_pid)
    if (
        not isinstance(first_root_id, str)
        or not first_root_id
        or second_root_id != first_root_id
        or first_map[root_pid] != second_map[root_pid]
    ):
        return None

    identities: list[ProcessDescendantIdentity] = []
    inconclusive = False
    for candidate_pid in candidates:
        first_chain = first_chains[candidate_pid]
        second_chain = second_chains[candidate_pid]
        if first_chain is None or second_chain is None:
            inconclusive = True
            continue
        if first_chain != second_chain:
            if first_chain or second_chain:
                inconclusive = True
            continue
        if not first_chain:
            continue
        if any(
            not isinstance(first_ids.get(process_pid), str)
            or not first_ids[process_pid]
            or second_ids.get(process_pid) != first_ids[process_pid]
            for process_pid in first_chain
        ):
            inconclusive = True
            continue
        creation_order_valid = True
        creation_order_inconclusive = False
        for child_pid, parent_pid in zip(first_chain, first_chain[1:]):
            child_start = first_ids.get(child_pid)
            parent_start = first_ids.get(parent_pid)
            if not isinstance(child_start, str) or not isinstance(parent_start, str):
                creation_order_inconclusive = True
                break
            start_order = _process_start_order(child_start, parent_start)
            if start_order is _ProcessStartOrder.INCONCLUSIVE:
                creation_order_inconclusive = True
                break
            if start_order is _ProcessStartOrder.EARLIER:
                creation_order_valid = False
                break
        if creation_order_inconclusive:
            inconclusive = True
            continue
        if not creation_order_valid:
            continue
        start_id = first_ids[candidate_pid]
        if not isinstance(start_id, str):
            inconclusive = True
            continue
        identities.append(
            ProcessDescendantIdentity(
                candidate_pid,
                first_map[candidate_pid],
                start_id,
            )
        )
    if identities:
        return identities
    return None if inconclusive else []


def process_descendant_identities(
    pid: int,
    *,
    proc_root: Path | None = None,
    candidate_pids: set[int] | None = None,
) -> list[ProcessDescendantIdentity] | None:
    """Return descendants with start identity bound atomically to parentage.

    Linux and macOS walk only the root's current descendants. Each candidate
    pid's start ID and PPID come from one ``/proc/<pid>/stat`` or
    ``PROC_PIDTBSDINFO`` kernel record. A strictly later child is accepted, a
    strictly earlier child is excluded, and equal or unparseable order is
    inconclusive. Windows validates each requested listener candidate's complete
    listener candidate's complete ancestry chain across two Toolhelp snapshots;
    unrelated siblings do not invalidate that proof. With no candidates it
    returns every independently stable descendant chain. If the native POSIX
    path is unavailable, other POSIX hosts fall back to two whole-process ``ps``
    snapshots and retain only rows whose parentage and one-second ``lstart``
    identity are unchanged.
    """
    if type(pid) is not int or pid <= 1:
        return []
    if IS_WINDOWS:
        return _windows_process_descendant_identities(pid, candidate_pids)
    if IS_LINUX or sys.platform == "darwin":
        descendants = _atomic_process_descendant_identities(pid, proc_root)
        if descendants is not None:
            return descendants
    if not IS_POSIX:
        return []
    identities = _posix_process_identity_map(pid)
    if identities is None:
        return None
    if pid not in identities:
        return []
    return _ordered_posix_descendant_identities(pid, identities)


def process_descendants(pid: int) -> list[int]:
    """Return *pid*'s descendants, breadth-first, from a single OS snapshot.

    Best-effort: an unreadable process table yields an empty list rather than
    raising, so callers using this to broaden a kill still perform their
    primary kill.

    Snapshot BEFORE killing anything. A kill reparents surviving orphans to
    init, erasing the PPID links that identify them, so a post-kill snapshot
    cannot find the very processes a caller needs to clean up.
    """

    if type(pid) is not int or pid <= 1:
        return []
    try:
        parent_map = _windows_process_parent_map() if IS_WINDOWS else _posix_process_parent_map()
    except Exception:  # noqa: BLE001 - introspection must never break a kill path
        return []
    return _descendants_from_parent_map(pid, parent_map)


def attributed_descendants(root_pid: int, root_token: str) -> list[int]:
    """*root_pid*'s descendants with EVERY parent-child edge attributed, not just the root.

    :func:`created_after` compares one child against one parent. Applying it with the
    ROOT's token for a whole flattened descendant list is weaker than it looks: it
    asks "was this process created after the root", which every process started since
    the root satisfies -- including a stale orphan that the parent map lists under a
    RECYCLED INTERMEDIATE pid. Such an orphan is unrelated to this tree, often
    belongs to the same user, and a caller acting on the set then terminates a
    stranger's process tree irreversibly.

    Walking level by level and attributing each edge against the parent it was
    reached THROUGH separates them: a real grandchild was created after its own
    parent, while the orphan of a recycled intermediate was created before the
    process that now holds that number. A child failing its edge is dropped WITH ITS
    SUBTREE -- everything below an unattributable edge is reached only through it, so
    none of it is provably part of this tree either.

    Still the token-only form: it needs no handles, so it serves the callers that
    cannot hold an exact root handle. :func:`descendant_termination_handles` remains
    the stronger answer where one IS available. A process whose creation identity
    cannot be read at all is left alone rather than guessed at, exactly as
    :func:`created_after` documents.

    Best-effort like :func:`process_descendants`: an unreadable process table yields
    an empty list rather than raising, and the SAME snapshot ordering rule applies --
    call this BEFORE killing anything.
    """

    if type(root_pid) is not int or root_pid <= 1 or not root_token:
        return []
    try:
        parent_map = _windows_process_parent_map() if IS_WINDOWS else _posix_process_parent_map()
    except Exception:  # noqa: BLE001 - introspection must never break a kill path
        return []

    children_of: dict[int, list[int]] = {}
    for child, parent in parent_map.items():
        children_of.setdefault(parent, []).append(child)

    out: list[int] = []
    seen = {root_pid}
    frontier = [(root_pid, root_token)]
    while frontier:
        next_frontier: list[tuple[int, str]] = []
        for parent_pid, parent_token in frontier:
            for child in sorted(children_of.get(parent_pid, ())):
                if child in seen:
                    continue
                child_token = process_start_time(child)
                if not child_token or not created_after(child_token, parent_token):
                    # Unattributable edge: this child is not provably ours, and
                    # nothing below it is reachable except through it.
                    continue
                seen.add(child)
                out.append(child)
                next_frontier.append((child, child_token))
        frontier = next_frontier
    return out


def created_after(child_token: str, parent_token: str) -> bool:
    """Whether a process the parent map lists under another is really its child.

    The Toolhelp snapshot behind :func:`process_descendants` records a parent as a
    bare pid, and Windows keeps that number after the parent dies. When the dead
    parent's pid is later recycled, an unrelated process appears as a child of the
    recycler -- and, being unrelated, is often one this user cannot terminate, so
    ending it fails and a caller that treats the set as a tree draws the wrong
    conclusion in whichever direction hurts it (killing a stranger, or calling a
    foreign listener its own).

    A genuine child was created after its parent, while such a stray was created
    while the pid still belonged to the process it was born under, so comparing the
    creation identities separates the two exactly. Both tokens are the creation
    ``FILETIME`` as decimal text (:func:`process_start_time`); a token that is not
    (nothing on Windows produces one) is not attributable, and the caller must leave
    that process alone rather than act on a guess.

    Lives HERE, beside the primitive whose staleness it compensates for, because
    three callers need the same Boolean rule: the pod backend's ``stop``,
    ``pod.runtime.port_owner``, and the test harness's Windows teardown. This thin
    wrapper validates their numeric FILETIME domain, then delegates ordering to
    the shared tri-state core so no second comparison implementation can drift.
    All three reach it through
    :func:`attributed_descendants`, which applies this comparison to EVERY
    parent-child edge -- applying it with only the ROOT's token admits a stale orphan
    sitting under a recycled INTERMEDIATE pid, which also postdates the root.
    :func:`descendant_termination_handles` is the stronger form still --
    exact per-process handles, every edge validated against creation AND exit times
    across two snapshots -- and is the right answer for a caller that holds an exact
    root handle; this is the token-only form for callers that do not.
    """
    try:
        child_numeric = str(int(child_token))
        parent_numeric = str(int(parent_token))
    except ValueError:
        return False
    return _process_start_order(child_numeric, parent_numeric) is _ProcessStartOrder.LATER


def short_path_name(path: str) -> str:
    """Return *path*'s Windows 8.3 short form via ``GetShortPathNameW``, or ``""``.

    ``""`` covers every unavailable case alike: a non-Windows host, a path that
    does not exist, a volume with 8.3 name generation disabled, or the API
    erroring — this function never raises, and the failing error code goes to
    DEBUG so an operator can tell those cases apart. The API can also succeed
    by returning the input spelling unchanged (the volume keeps no separate
    short alias); that is returned as-is, and whether the spelling is usable
    is the caller's question.

    Two-call size protocol: the first call (NULL buffer) answers the required
    buffer length INCLUDING the terminating NUL; the second call writes the
    string and answers its length EXCLUDING the NUL, so a second answer >= the
    first means the buffer was too small (the path changed between calls) and
    the result cannot be trusted.
    """
    try:
        # getattr, not an attribute reference: ``ctypes.WinDLL`` is
        # Windows-only in typeshed (see the mypy note above), and getattr is
        # also the seam the cross-platform protocol test injects a fake
        # kernel32 through.
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        needed = kernel32.GetShortPathNameW(path, None, 0)
        if not needed:
            get_last_error = getattr(ctypes, "get_last_error", None)
            logger.debug(
                "GetShortPathNameW(%r) failed: error %s",
                path,
                get_last_error() if callable(get_last_error) else "unknown",
            )
            return ""
        buf = ctypes.create_unicode_buffer(needed)
        written = kernel32.GetShortPathNameW(path, buf, needed)
        if not written or written >= needed:
            return ""
        return buf.value
    except Exception:
        return ""


def _windows_process_parent_map() -> dict[int, int]:
    """Return one Toolhelp PID -> PPID snapshot, raising if enumeration fails."""

    if not IS_WINDOWS:
        return {}
    try:
        th32cs_snapprocess = 0x00000002
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        entry_ptr = ctypes.POINTER(_ProcessEntry32)

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32First.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32Next.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        set_last_error = getattr(kernel32, "SetLastError", None)
        get_last_error = getattr(kernel32, "GetLastError", None)

        snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise OSError("Windows process snapshot creation failed")
        try:
            entry = _ProcessEntry32()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                raise OSError("Windows first process enumeration failed")
            result: dict[int, int] = {}
            while True:
                if len(result) >= _WINDOWS_CLEANUP_SNAPSHOT_LIMIT:
                    raise _WindowsTreeOverflow("Windows cleanup process snapshot capacity exceeded")
                result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if callable(set_last_error):
                    set_last_error(0)
                else:
                    ctypes_set_last_error = getattr(ctypes, "set_last_error", None)
                    if callable(ctypes_set_last_error):
                        ctypes_set_last_error(0)
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    error = (
                        int(get_last_error()) if callable(get_last_error) else _windows_last_error()
                    )
                    if error not in (0, 18):  # ERROR_NO_MORE_FILES
                        raise OSError(error, "Windows process enumeration failed")
                    return result
        finally:
            kernel32.CloseHandle(snapshot)
    except OSError:
        raise
    except Exception as exc:
        raise OSError("Windows process enumeration failed") from exc


def _open_process_termination_handle(pid: int, *, failure: list[str] | None = None) -> int | None:
    """Open a termination handle; optionally capture a sanitized failure locally."""

    if not IS_WINDOWS:
        return None
    evidence = "unknown"
    try:
        process_terminate = 0x0001
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            process_terminate | process_query_limited_information | synchronize,
            False,
            pid,
        )
        if handle:
            return int(handle)
        # Read ctypes' saved error immediately, before any other Windows call.
        with contextlib.suppress(Exception):
            evidence = f"winerror={_windows_last_error()}"
    except Exception as exc:
        evidence = f"exception={type(exc).__name__[:64]}"
    if failure is not None:
        with contextlib.suppress(Exception):
            failure.append(evidence)
    return None


def open_process_termination_handle(pid: int, expected_token: str) -> int | None:
    """Open *pid* only when its exact process object matches *expected_token*.

    The caller owns a returned handle and closes it with
    :func:`close_process_handle`. Opening by numeric PID alone races PID reuse,
    so the handle's creation FILETIME is compared with the authoritative token
    before it is returned. A mismatch, unreadable identity, or malformed token
    closes the handle and returns ``None``.
    """

    if type(pid) is not int or pid <= 1:
        raise ValueError(f"open_process_termination_handle: refusing invalid pid {pid!r}")
    handle = _open_process_termination_handle(pid)
    if handle is None:
        return None
    try:
        expected_creation = int(expected_token)
        identity = _windows_process_handle_identity(handle)
        if identity is None or identity[0] != pid or identity[1] != expected_creation:
            close_process_handle(handle)
            return None
    except (TypeError, ValueError):
        close_process_handle(handle)
        return None
    return handle


def duplicate_asyncio_process_handle(process: object) -> int | None:
    """Duplicate asyncio's original Windows process handle for tree anchoring."""

    if not IS_WINDOWS:
        return None
    try:
        transport = getattr(process, "_transport", None)
        get_extra_info = getattr(transport, "get_extra_info", None)
        popen = get_extra_info("subprocess") if callable(get_extra_info) else None
        source = getattr(popen, "_handle", None)
        if not isinstance(source, int):
            return None
        source_value = int(source)
        if source_value <= 0:
            return None

        duplicate_same_access = 0x00000002
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        owner = kernel32.GetCurrentProcess()
        duplicate = wintypes.HANDLE()
        if not kernel32.DuplicateHandle(
            owner,
            wintypes.HANDLE(source_value),
            owner,
            ctypes.byref(duplicate),
            0,
            False,
            duplicate_same_access,
        ):
            return None
        return int(duplicate.value) if duplicate.value else None
    except Exception:
        return None


def _windows_last_error() -> int:
    """Return ctypes' thread-local Win32 error without POSIX stub assumptions."""

    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if callable(getter) else 0


# Bounds for the exited-but-exit-FILETIME-unpublished window (see
# _windows_process_handle_identity). The window closes within a few tens of
# milliseconds; the ceiling is generous enough to absorb a loaded host without
# letting a genuinely unreadable handle stall a caller.
_WINDOWS_EXIT_FILETIME_TIMEOUT_SECS = 0.25
_WINDOWS_EXIT_FILETIME_POLL_SECS = 0.002


def _windows_process_handle_identity(
    handle: int, *, await_exit_time: bool = True
) -> tuple[int, int, int | None] | None:
    """Return ``(pid, creation_time, exit_time)`` for an exact process handle.

    ``await_exit_time=False`` answers with the creation half alone: liveness is
    not decided, the exit FILETIME is neither awaited nor reported, and the third
    element is ``None`` by construction. That mode performs no wait and no sleep,
    so it is safe to call from the asyncio event loop; a caller that must certify
    a process has exited needs the exit bound and keeps the default.
    """

    if not IS_WINDOWS or type(handle) is not int or handle <= 0:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel32.GetProcessId.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        process_handle = wintypes.HANDLE(handle)
        pid = int(kernel32.GetProcessId(process_handle))
        creation = wintypes.FILETIME()
        exit_ = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        exit_code = wintypes.DWORD()

        def _read_times() -> bool:
            return bool(
                kernel32.GetProcessTimes(
                    process_handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                )
            )

        if (
            pid <= 1
            or not _read_times()
            or not kernel32.GetExitCodeProcess(
                process_handle,
                ctypes.byref(exit_code),
            )
        ):
            return None

        def _filetime_value(value: "wintypes.FILETIME") -> int:
            return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

        if not await_exit_time:
            # Creation-only: the caller asks which process object this pid names,
            # and the creation FILETIME answers that on its own. Skipping the exit
            # half keeps this read free of the wait and the poll below, which is
            # what lets an event-loop caller use it directly.
            creation_only = _filetime_value(creation)
            return (pid, creation_only, None) if creation_only > 0 else None
        # A process may exit with code 259, the value STILL_ACTIVE reserves, so
        # GetExitCodeProcess alone cannot decide liveness: such a child reads back
        # as running for as long as a handle to it is held, and a drain waiting for
        # its exit never finishes while its reservation stays charged. A zero-timeout
        # wait answers from the object's signal state, which carries no collision:
        # a signalled process object is a terminated one. Handles opened for query
        # alone lack SYNCHRONIZE and cannot be waited on; those callers read identity
        # without draining anything, so they keep the exit-code reading rather than
        # losing the answer, and every handle a drain retains is opened to wait.
        still_active = 259
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        waited = int(kernel32.WaitForSingleObject(process_handle, 0))
        if waited == wait_object_0:
            active = False
        elif waited == wait_timeout:
            active = True
        else:
            active = exit_code.value == still_active
        # The exit FILETIME is not defined for a live process. If the status
        # says the process exited, read the times again after that observation
        # so the returned exit bound belongs to the terminated object.
        if not active and not _read_times():
            return None

        creation_value = _filetime_value(creation)
        exit_value = _filetime_value(exit_)
        # GetExitCodeProcess reports the exit BEFORE the kernel publishes the
        # exit FILETIME, so a just-terminated process reads back as
        # exited-with-exit_time==0 for a brief window (sub-millisecond to a few
        # tens of milliseconds). Treating that window as "no identity" makes the
        # caller reject a perfectly good handle, so poll briefly for the real
        # value. The bound stays short because the only alternative to a
        # published exit time is refusing the handle.
        if not active and exit_value <= 0:
            deadline = time.monotonic() + _WINDOWS_EXIT_FILETIME_TIMEOUT_SECS
            while exit_value <= 0 and time.monotonic() < deadline:
                time.sleep(_WINDOWS_EXIT_FILETIME_POLL_SECS)
                if not _read_times():
                    return None
                exit_value = _filetime_value(exit_)
        if creation_value <= 0 or (not active and exit_value <= 0):
            return None
        return pid, creation_value, None if active else exit_value
    except Exception:
        return None


def _windows_lineage_matches_lifetimes(
    child_pid: int,
    root_pid: int,
    parent_map: Mapping[int, int],
    identities: Mapping[int, tuple[int, int, int | None]],
) -> bool:
    """Bind numeric Toolhelp ancestry to the exact handles' lifetimes."""

    current = child_pid
    seen: set[int] = set()
    while current != root_pid:
        if current in seen:
            return False
        seen.add(current)
        parent_pid = parent_map.get(current)
        child_identity = identities.get(current)
        parent_identity = identities.get(parent_pid) if parent_pid is not None else None
        if (
            parent_pid is None
            or child_identity is None
            or parent_identity is None
            or child_identity[0] != current
            or parent_identity[0] != parent_pid
        ):
            return False
        child_created = child_identity[1]
        parent_created = parent_identity[1]
        parent_exited = parent_identity[2]
        if child_created < parent_created:
            return False
        if parent_exited is not None and child_created >= parent_exited:
            return False
        current = parent_pid
    return True


def _windows_process_query_diagnostic(pid: int) -> str:
    """Observe an unvalidated exact object, never acquire termination authority."""

    handle = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only
        if not handle:
            return f"winerror={_windows_last_error()}"
        return f"identity={_windows_process_handle_identity(int(handle))}"
    except Exception as exc:
        return f"exception={type(exc).__name__[:64]}"
    finally:
        if handle:
            close_process_handle(int(handle))


def _windows_descendant_failure_details(
    failed: set[int],
    first_map: Mapping[int, int],
    fresh_map: Mapping[int, int],
    pinned: Mapping[int, int],
    root_identity: tuple[int, int, int | None],
    opening_errors: Mapping[int, str],
) -> str:
    """Bounded, failure-only observations; nothing here certifies death or ancestry."""

    root_pid = root_identity[0]
    identities: dict[int, tuple[int, int, int | None] | None] = {}

    def chain(child: int, parents: Mapping[int, int]) -> list[int]:
        result: list[int] = []
        while child not in result and len(result) < 8:
            result.append(child)
            if child == root_pid or child not in parents:
                break
            child = parents[child]
        return result

    details = []
    for child in sorted(failed)[:3]:
        first_chain = chain(child, first_map)
        fresh_chain = chain(child, fresh_map)
        # Only already-pinned objects on the relevant chains are queried.
        relevant = sorted({root_pid, *first_chain, *fresh_chain}.intersection(pinned))
        for process_pid in relevant:
            if process_pid not in identities:
                identities[process_pid] = _windows_process_handle_identity(pinned[process_pid])
        facts = {process_pid: identities[process_pid] for process_pid in relevant}
        details.append(
            f"pid={child} open={opening_errors.get(child, 'unknown')} "
            f"first_chain={first_chain} fresh_chain={fresh_chain} pinned={facts} "
            f"query_unvalidated={_windows_process_query_diagnostic(child)}"
        )
    return (
        f"diagnostic_only(total={len(failed)}, candidates<=3, chain<=8, "
        f"root_at_scan={root_identity}): " + "; ".join(details)
    )


def _windows_process_query_creation(pid: int) -> int | None:
    """Return *pid*'s creation FILETIME through a query-only handle, or ``None``.

    Termination rights are not requested, so this answers for a process whose
    termination handle is refused -- which is the only case that needs it.
    ``None`` means the instant is unknown and nothing may be concluded from it.
    """

    handle = _open_process_query_handle(pid)
    if handle is None:
        return None
    try:
        identity = _windows_process_handle_identity(handle)
    finally:
        _close_process_handle(handle)
    if identity is None or identity[0] != pid:
        return None
    return identity[1]


def _windows_foreign_descendant_pids(
    candidates: set[int],
    observed: set[int],
    parent_map: dict[int, int],
    pinned: Mapping[int, int],
    root_pid: int,
    root_created: int,
) -> set[int]:
    """Return observed PIDs whose numeric ancestry creation order disproves them.

    A descendant is created after the root it descends from, so a chain node that
    already existed before the root holds a recycled PID naming an unrelated
    process. Every observed PID reaching the root only through such a node is
    foreign as well: its claimed parent's PID was taken over by a process older
    than the root, so that parent died before the root started and cannot have
    belonged to this tree.

    Only *candidates* seed the reads, so a scan that opened every handle pays
    nothing. A node in *pinned* is read from its own handle, whose object cannot
    have been recycled; the rest are read once each by PID. An unreadable instant
    disproves nothing and leaves its chain intact.
    """

    creations: dict[int, int | None] = {}

    def created(process_pid: int) -> int | None:
        if process_pid not in creations:
            handle = pinned.get(process_pid)
            if handle is None:
                creations[process_pid] = _windows_process_query_creation(process_pid)
            else:
                identity = _windows_process_handle_identity(handle)
                creations[process_pid] = (
                    identity[1] if identity is not None and identity[0] == process_pid else None
                )
        return creations[process_pid]

    disproven: set[int] = set()
    for candidate_pid in sorted(candidates):
        chain = _windows_chain_to_root(candidate_pid, root_pid, parent_map)
        if not chain:
            continue
        for process_pid in chain:
            if process_pid == root_pid:
                continue
            instant = created(process_pid)
            if instant is not None and instant < root_created:
                disproven.add(process_pid)
    if not disproven:
        return set()
    foreign: set[int] = set()
    for process_pid in observed:
        chain = _windows_chain_to_root(process_pid, root_pid, parent_map)
        if chain and not disproven.isdisjoint(chain):
            foreign.add(process_pid)
    return foreign


def descendant_termination_handles(
    pid: int,
    retained_handles: Mapping[int, int] | None = None,
    root_handle: int | None = None,
) -> dict[int, int]:
    """Return exact Windows process handles for newly observed descendants.

    Toolhelp exposes numeric parent PIDs, which can be recycled after an
    unpinned process exits. Every edge is therefore checked against creation/exit times
    from exact root, retained-parent, and newly-opened child handles in two
    snapshots. This admits a genuine child created before an immediate launcher
    exit while rejecting a tree attached to a recycled root or intermediate PID.
    An unopenable candidate requires fresh full-snapshot absence, or a creation
    instant proving it predates the root; unreadable identities or incomplete
    ancestry raise OSError, never certify a subset.
    On failure only newly opened handles are closed; retained/root handles
    stay caller-owned.
    """

    if type(pid) is not int or pid <= 1:
        raise ValueError(f"descendant_termination_handles: refusing non-int/reserved pid {pid!r}")
    if not IS_WINDOWS:
        return {}
    if type(root_handle) is not int or root_handle <= 0:
        raise ValueError("descendant_termination_handles: exact root handle required")
    if retained_handles is not None and len(retained_handles) > _WINDOWS_CLEANUP_IDENTITY_LIMIT:
        raise _WindowsTreeOverflow("Windows cleanup retained identity capacity exceeded")
    retained = dict(retained_handles or {})
    root_identity = _windows_process_handle_identity(root_handle)
    if root_identity is None or root_identity[0] != pid:
        raise ValueError("descendant_termination_handles: root handle identity mismatch")

    first_map = _windows_process_parent_map()
    first = set(_descendants_from_parent_map(pid, first_map, limit=_WINDOWS_CLEANUP_IDENTITY_LIMIT))
    # Check the union without allocating an oversized union or opening a child.
    count = len(retained) + (pid not in retained)
    for child in first:
        if child not in retained:
            count += 1
            if count > _WINDOWS_CLEANUP_IDENTITY_LIMIT:
                raise _WindowsTreeOverflow("Windows cleanup identity capacity exceeded")
    opened: dict[int, int] = {}
    try:
        unopened: set[int] = set()
        opening_errors: dict[int, str] = {}
        # Retained handles stay open across scans, pinning each process object
        # and preventing PID reuse even after exit; no replacement can hide here.
        for child_pid in sorted(first - set(retained)):
            failure: list[str] = []
            handle = _open_process_termination_handle(child_pid, failure=failure)
            if handle is None:
                unopened.add(child_pid)
                opening_errors[child_pid] = failure[0] if failure else "unknown"
            else:
                opened[child_pid] = handle
        if unopened:
            # OpenProcess failure (including ACCESS_DENIED) is not proof of
            # death. Require a fresh, successful FULL snapshot and account for
            # surviving entries that still reference a vanished candidate.
            # Enumeration errors propagate; pid_exists(False) is ambiguous.
            fresh_map = _windows_process_parent_map()
            remaining = unopened.intersection(fresh_map)
            if remaining:
                # A process that already existed before the root cannot descend
                # from it, and that instant is readable through a query-only
                # handle where a termination handle is refused. Drop such a
                # stranger together with everything whose only route to the root
                # runs through it, so a PID it inherited cannot turn the owned
                # tree into a fatal incomplete one. A chain implicating a pinned
                # retained identity stays fail-closed: dropping it would discard
                # authority an earlier scan already proved.
                foreign = _windows_foreign_descendant_pids(
                    remaining,
                    first,
                    first_map,
                    {**retained, **opened, pid: root_handle},
                    pid,
                    root_identity[1],
                )
                if foreign and foreign.isdisjoint(retained):
                    unopened -= foreign
                    for child_pid in sorted(foreign.intersection(opened)):
                        close_process_handle(opened.pop(child_pid))
                    remaining = unopened.intersection(fresh_map)
            if remaining:
                errors = {child: opening_errors[child] for child in sorted(remaining)[:3]}
                details = f"diagnostic_only=unknown; open_errors={errors}"
                with contextlib.suppress(Exception):
                    details = _windows_descendant_failure_details(
                        remaining,
                        first_map,
                        fresh_map,
                        {**retained, **opened, pid: root_handle},
                        root_identity,
                        opening_errors,
                    )
                raise OSError(
                    f"Windows descendant handles unavailable: {sorted(remaining)[:3]}; {details}"
                )
            vanished = unopened.difference(fresh_map)
            dangling = sorted(
                (child, parent) for child, parent in fresh_map.items() if parent in vanished
            )
            if dangling:
                # These children may have appeared only after the first scan.
                # Numeric edges cannot prove identity or grant kill authority,
                # but they prevent certifying this observed branch as gone.
                raise OSError(
                    "Windows descendant ancestry incomplete: vanished unopened parents; "
                    f"diagnostic_only(total={len(dangling)}, child_parent_ids<=3): {dangling[:3]}"
                )
        if not opened:
            return {}

        handles = {
            **{child_pid: handle for child_pid, handle in retained.items() if child_pid in first},
            **opened,
            pid: root_handle,
        }

        def read_identities() -> dict[int, tuple[int, int, int | None]]:
            identities: dict[int, tuple[int, int, int | None]] = {}
            for process_pid, handle in handles.items():
                identity = _windows_process_handle_identity(handle)
                if identity is None:
                    raise OSError(f"Windows descendant handle identity unreadable: {process_pid}")
                identities[process_pid] = identity
            return identities

        first_identities = read_identities()
        # A vanished, unpinned intermediary cannot supply a lifetime bound for
        # a surviving child. Do not silently drop that child's unknown chain.
        for child_pid in first.intersection(handles):
            if first_map[child_pid] not in handles:
                raise OSError(f"Windows descendant ancestry incomplete: {child_pid}")
        eligible = {
            child_pid
            for child_pid in opened
            if child_pid in first
            and _windows_lineage_matches_lifetimes(
                child_pid,
                pid,
                first_map,
                first_identities,
            )
        }

        second_map = _windows_process_parent_map()
        second_identities = {
            process_pid: identity
            for process_pid, identity in read_identities().items()
            if (previous := first_identities.get(process_pid)) is not None
            and identity[:2] == previous[:2]
            and (previous[2] is None or identity[2] == previous[2])
        }
        for child_pid in eligible:
            current = child_pid
            while current != pid and current in second_identities:
                identity = second_identities[current]
                if current not in second_map and identity[2] is None:
                    raise OSError(f"Windows live descendant missing from snapshot: {current}")
                current = first_map[current]
        # Toolhelp drops exited intermediaries even while their exact handles
        # remain pinned. Preserve an observed edge only if its object is still
        # identical and either its PPID agrees or its absence is explained by a
        # proven exit. Recheck every lifetime with the newly read exit bounds;
        # a live child born after that exit belongs to a recycled PID, not us.
        continuous_map = {
            process_pid: parent_pid
            for process_pid, parent_pid in first_map.items()
            if process_pid in second_identities
            and (
                second_map.get(process_pid) == parent_pid
                or (process_pid not in second_map and second_identities[process_pid][2] is not None)
            )
        }
        for child_pid in tuple(opened):
            if child_pid not in eligible or not _windows_lineage_matches_lifetimes(
                child_pid,
                pid,
                continuous_map,
                second_identities,
            ):
                close_process_handle(opened.pop(child_pid))
        return opened
    except Exception:
        for handle in opened.values():
            close_process_handle(handle)
        raise


def terminate_process_handle(handle: int) -> bool:
    """Terminate the exact Windows process object referenced by *handle*."""

    if type(handle) is not int or handle <= 0:
        raise ValueError(f"terminate_process_handle: refusing invalid handle {handle!r}")
    if not IS_WINDOWS:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    process_handle = wintypes.HANDLE(handle)
    exit_code = wintypes.DWORD()
    still_active = 259
    if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
        raise OSError(_windows_last_error(), "GetExitCodeProcess failed")
    if exit_code.value != still_active:
        return False
    if not kernel32.TerminateProcess(process_handle, 1):
        raise OSError(_windows_last_error(), "TerminateProcess failed")
    return True


def process_handle_active(handle: int) -> bool:
    """Return whether an identity-stable Windows process handle is still live."""

    if type(handle) is not int or handle <= 0:
        raise ValueError(f"process_handle_active: refusing invalid handle {handle!r}")
    if not IS_WINDOWS:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        exit_code = wintypes.DWORD()
        return bool(
            kernel32.GetExitCodeProcess(
                wintypes.HANDLE(handle),
                ctypes.byref(exit_code),
            )
            and exit_code.value == 259
        )
    except Exception:
        return False


def close_process_handle(handle: int) -> None:
    """Close a handle returned by :func:`descendant_termination_handles`."""

    if not IS_WINDOWS or type(handle) is not int or handle <= 0:
        return
    with contextlib.suppress(Exception):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(handle))


_WINDOWS_TREE_REAP_TIMEOUT_SECS = 5.0
_WINDOWS_TREE_REAP_POLL_SECS = 0.01
_PENDING_WINDOWS_TREE_RETRY_LIMIT = 8


# Separate cleanup bookkeeping budget, not a pool, RSS, or Job limit. Sixty-four
# physical trees leave room beyond ordinary concurrent ACP starts; 4096 exact
# identities per tree allow build/browser fan-out without unbounded retention.
_WINDOWS_CLEANUP_ROOT_LIMIT = 64
_WINDOWS_CLEANUP_IDENTITY_LIMIT = 4096
# Toolhelp includes unrelated host processes. Bound that temporary input too,
# before retention; an incomplete snapshot cannot prove a tree empty.
_WINDOWS_CLEANUP_SNAPSHOT_LIMIT = 65536

# The interpreters the capture bindings are measured against, quoted verbatim in
# the refusals so the operator is told which ones work rather than only that
# theirs does not.
_WINDOWS_CAPTURE_MEASURED_VERSIONS = "CPython 3.12, 3.13 or 3.14"


class WindowsCleanupCapacityError(OSError):
    """Cleanup admission refused, or an owned tree needs manual handling."""


class _WindowsTreeOverflow(WindowsCleanupCapacityError):
    """A snapshot/identity bound prevented complete lineage observation."""


class _PendingWindowsTreeCleanup:
    """One reservation, from before physical spawn through verified retirement."""

    __slots__ = (
        "handles",
        "key",
        "lock",
        "retired",
        "root_pid",
        "signalled",
        "terminally_scanned",
        "manual_required",
        "raw_root_pin",
        "retire_app_tracking",
    )

    def __init__(
        self, root_handle: int = 0, identity: tuple[int, int, int | None] = (0, 0, None)
    ) -> None:
        self.root_pid = identity[0]
        self.key = identity[:2]
        self.handles = {self.root_pid: root_handle} if root_handle else {}
        self.terminally_scanned: set[int] = set()
        self.signalled: set[int] = set()
        self.lock = threading.Lock()
        self.retired = False
        self.manual_required = False
        # Only a subprocess.Handle (an int with Close/__del__), NEVER Popen or
        # its transport. Keeps the original pin on duplicate/identity failure.
        self.raw_root_pin: int | None = None
        self.retire_app_tracking = False


_PENDING_WINDOWS_TREE_CLEANUPS: dict[tuple[int, int], _PendingWindowsTreeCleanup] = {}
_PENDING_WINDOWS_TREE_CLEANUPS_LOCK = threading.RLock()
_WINDOWS_TREE_ADMISSIONS: set[_PendingWindowsTreeCleanup] = set()


def reserve_windows_tree_cleanup(
    key: tuple[int, int] | None = None,
) -> _PendingWindowsTreeCleanup:
    """Atomically admit a physical tree, or reuse its already charged identity."""
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if key is not None:
            for state in _WINDOWS_TREE_ADMISSIONS:
                if state.key == key:
                    return state
                if state.root_pid == key[0] and state.key[1] == 0:
                    raise OSError("Windows original root identity is pending verification")
        manual = sorted(
            state.root_pid for state in _WINDOWS_TREE_ADMISSIONS if state.manual_required
        )
        if manual:
            # The refusal outlives the tree that caused it, so the text has to
            # carry the cause and the remedy: an operator meeting it hours later
            # sees only a failed session start, and the log line that named the
            # root has scrolled away. Naming every quarantined root is bounded by
            # the same root admission limit that bounds the registry.
            roots = ", ".join(str(pid) for pid in manual)
            raise WindowsCleanupCapacityError(
                "Windows session start refused: cleanup of the process tree rooted at "
                f"pid {roots} could not be accounted for, so its surviving processes must "
                "be ended by hand; starts resume after this gateway restarts"
            )
        if len(_WINDOWS_TREE_ADMISSIONS) >= _WINDOWS_CLEANUP_ROOT_LIMIT:
            raise WindowsCleanupCapacityError("Windows cleanup root capacity exhausted")
        state = _PendingWindowsTreeCleanup()
        if key is not None:
            state.key = key
        _WINDOWS_TREE_ADMISSIONS.add(state)
        return state


def release_windows_tree_reservation(state: _PendingWindowsTreeCleanup) -> None:
    """Refund only an empty reservation. Pins/manual debt cannot be discarded."""
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if state.handles or state.raw_root_pin is not None or state.manual_required:
            return
        _WINDOWS_TREE_ADMISSIONS.discard(state)
        state.retired = True


def _manual_windows_tree(state: _PendingWindowsTreeCleanup) -> None:
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if not state.manual_required:
            state.manual_required = True
            logger.error(
                "Windows cleanup manual-handling-required: root=%d retained=%d; "
                "new physical starts refused; this is not OS isolation",
                state.root_pid,
                len(state.handles),
            )


async def _bind_windows_cleanup_process(
    state: _PendingWindowsTreeCleanup, process: asyncio.subprocess.Process
) -> None:
    """Capture the original suspended child before any cancellable resume work.

    The exact handle is pinned into ``state.handles`` synchronously, before the
    single await, so a cancellation that lands while the identity is being read
    still leaves this child retained for maintenance rather than dropped.

    The identity read is the one step that goes off the loop. A child that has
    already exited reads back as exited-with-no-exit-FILETIME until the kernel
    publishes that value, and ``_windows_process_handle_identity`` polls for it
    for up to ``_WINDOWS_EXIT_FILETIME_TIMEOUT_SECS``. This runs during session
    start on a loop that is serving every other session, so a child that dies
    the instant it resumes must not stall them: the wait belongs on
    :func:`kiro_crew.executors.subprocess_executor`, the same pool the rest of
    this module's spawn-adjacent waits use.
    """
    state.root_pid = process.pid
    state.key = (process.pid, 0)
    transport = getattr(process, "_transport", None)
    popen = transport.get_extra_info("subprocess") if transport is not None else None
    raw = getattr(popen, "_handle", None)
    if isinstance(raw, int):
        state.raw_root_pin = raw
    handle = duplicate_asyncio_process_handle(process)
    if handle is None and isinstance(raw, int):
        # Borrow the original reference-counted Handle if duplication failed.
        # It must never be explicitly closed while Popen can still use it.
        handle = int(raw)
    elif handle is not None:
        state.raw_root_pin = None
    setattr(process, "_windows_cleanup_state", state)
    if handle is not None:
        state.handles[process.pid] = handle
        identity = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _windows_process_handle_identity, handle
        )
        if identity is not None and identity[0] == process.pid:
            with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                state.key = identity[:2]
            return
    else:
        # An unsupported transport with no original pin has no retry authority.
        _manual_windows_tree(state)
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        _PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
    raise OSError("Windows original child pin/identity unavailable")


async def create_windows_cleanup_owned_process(factory: Any) -> asyncio.subprocess.Process:
    """Charge before spawning; cancellation waits for the child's ownership handoff.

    The factory is invoked only after reservation and is never cancelled by its
    waiter. An OS-spawn failure has no returned child; a returned child is pinned
    before the factory task settles, even when the caller has been cancelled.
    POSIX invokes the factory directly and has no cleanup admission policy.
    """
    if not IS_WINDOWS:
        return await factory()
    state = reserve_windows_tree_cleanup()

    async def launch() -> asyncio.subprocess.Process:
        process = await factory(windows_cleanup_owner=state)
        await _bind_windows_cleanup_process(state, process)
        return process

    creation = launch()
    try:
        task = asyncio.ensure_future(creation)
    except BaseException:
        creation.close()
        release_windows_tree_reservation(state)
        raise
    cancelled = False
    try:
        while True:
            try:
                process = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    process = task.result()
                    break
    except BaseException:
        if state.handles:
            # Creation capture precedes transport initialization. No returned
            # Process is needed to retain this exact child for maintenance.
            with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                _PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
        release_windows_tree_reservation(state)
        raise
    if cancelled:
        # This child never reaches its caller; cleanup owns it independently.
        with contextlib.suppress(OSError):
            await terminate_windows_asyncio_tree(process)
        raise asyncio.CancelledError
    return process


async def finish_windows_cleanup_owned_spawn(factory: Any) -> Any:
    """Drain the resume worker before cancellation can race tree retirement."""
    if not IS_WINDOWS:
        return await factory()
    task = asyncio.ensure_future(factory())
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


def _drain_windows_process_tree(state: _PendingWindowsTreeCleanup) -> bool:
    """Advance one exact-handle tree; retain every handle when the pass fails."""

    if state.manual_required:
        raise WindowsCleanupCapacityError("Windows tree requires manual handling")
    if state.key[1] == 0:
        identity = _windows_process_handle_identity(state.handles[state.root_pid])
        if identity is None or identity[0] != state.root_pid:
            raise OSError("Windows process-tree root identity unavailable")
        with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
            if _PENDING_WINDOWS_TREE_CLEANUPS.get(state.key) is state:
                del _PENDING_WINDOWS_TREE_CLEANUPS[state.key]
            state.key = identity[:2]
            _PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
    deadline = time.monotonic() + _WINDOWS_TREE_REAP_TIMEOUT_SECS
    while True:
        for pid, handle in tuple(state.handles.items()):
            if pid in state.terminally_scanned:
                continue
            if time.monotonic() >= deadline:
                raise OSError("Windows process tree did not drain before the deadline")
            before = _windows_process_handle_identity(handle)
            if before is None or before[0] != pid:
                raise OSError(f"Windows process-tree identity unreadable: {pid}")
            state.handles.update(descendant_termination_handles(pid, state.handles, handle))
            after = _windows_process_handle_identity(handle)
            if after is None or after[:2] != before[:2]:
                raise OSError(f"Windows process-tree identity changed or unreadable: {pid}")
            if before[2] is not None and after[2] is not None:
                state.terminally_scanned.add(pid)
            elif after[2] is None and pid not in state.signalled:
                terminate_process_handle(handle)
                state.signalled.add(pid)
        if state.terminally_scanned == set(state.handles):
            return True
        time.sleep(_WINDOWS_TREE_REAP_POLL_SECS)


def _advance_owned_windows_tree(
    state: _PendingWindowsTreeCleanup,
    *,
    try_only: bool = False,
) -> tuple[bool, bool]:
    """Return ``(drained, completed_here)`` and retire a verified handle set.

    A completed drain retires the tree's PID-file records and protected-PID
    shield BEFORE any handle is closed and while ``state.lock`` is still held, so
    the retirement runs while the root handle still pins the old incarnation — a
    recycled pid cannot slip in between the close and the untrack. A retirement
    that raises leaves the state un-retired and its handles open, so the receipt
    survives for the next tick rather than being lost with the handles.

    ``try_only`` acquires ``state.lock`` non-blockingly: a state already draining
    on another caller returns ``(False, False)`` untouched so a maintenance sweep
    rotates it behind its peers instead of blocking the whole pass on one busy
    tree. Caller-initiated cleanup leaves it False and keeps its serialization.
    """

    handles_to_close: tuple[int, ...] = ()
    completed_here = False
    if try_only:
        acquired = state.lock.acquire(blocking=False)
        if not acquired:
            return False, False
    else:
        state.lock.acquire()
    try:
        if state.retired:
            return True, False
        try:
            result = _drain_windows_process_tree(state)
        except _WindowsTreeOverflow:
            _manual_windows_tree(state)
            raise
        if result:
            try:
                # circular import: session_pid imports this module at its own top,
                # so the tracking retirement it owns can only be reached in-function.
                from kiro_crew.session_pid import retire_windows_tree_tracking

                retire_windows_tree_tracking(state.root_pid)
                if state.retire_app_tracking:
                    # circular import: apps.backend also imports this module at its top.
                    from kiro_crew.apps.backend import retire_windows_app_tracking

                    retire_windows_app_tracking(*state.key)
            except Exception:
                logger.warning(
                    "Windows exact-handle tree retirement failed for PID %d; "
                    "retaining pinned handles for retry",
                    state.root_pid,
                    exc_info=True,
                )
                return False, False
            state.retired = True
            completed_here = True
            handles_to_close = tuple(state.handles.values())
            # The state remains locked and charged until all pins close.
            for handle in handles_to_close:
                if state.raw_root_pin is None or handle != int(state.raw_root_pin):
                    close_process_handle(handle)
            state.handles.clear()
            state.signalled.clear()
            state.terminally_scanned.clear()
            state.raw_root_pin = None
            with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                if _PENDING_WINDOWS_TREE_CLEANUPS.get(state.key) is state:
                    del _PENDING_WINDOWS_TREE_CLEANUPS[state.key]
                release_windows_tree_reservation(state)
    finally:
        state.lock.release()
    return result, completed_here


def terminate_windows_process_tree_owned(
    root_handle: int,
    *,
    reservation: _PendingWindowsTreeCleanup | None = None,
) -> bool:
    """Drain an exact handle, retaining failed ownership for maintenance.

    An unadmitted caller at capacity retains ownership of its input handle;
    capacity refusal never closes it. PID-based callers reserve before opening.
    """
    if not IS_WINDOWS:
        close_process_handle(root_handle)
        raise ValueError("Windows process-tree termination requires Windows")
    identity = _windows_process_handle_identity(root_handle)
    if identity is None:
        # Even an unidentifiable imported handle must not be consumed at full
        # capacity. Its caller keeps ownership when admission is refused.
        empty = reserve_windows_tree_cleanup() if reservation is None else reservation
        if empty.handles:
            raise OSError("Windows process-tree root identity unavailable")
        close_process_handle(root_handle)
        release_windows_tree_reservation(empty)
        raise OSError("Windows process-tree root identity unavailable")
    key = identity[:2]
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        existing = next((item for item in _WINDOWS_TREE_ADMISSIONS if item.key == key), None)
        if existing is not None:
            if reservation is not None and reservation is not existing:
                release_windows_tree_reservation(reservation)
            state = existing
        else:
            state = reserve_windows_tree_cleanup(key) if reservation is None else reservation
        if state.key == (0, 0):
            state.key = key
    with state.lock:
        if state.retired:
            close_process_handle(root_handle)
            return state.root_pid > 0
        if not state.handles:
            state.root_pid = identity[0]
            state.key = key
            state.handles[state.root_pid] = root_handle
        elif state.key != key:
            raise ValueError("Windows cleanup reservation identity mismatch")
        elif state.handles[state.root_pid] != root_handle:
            close_process_handle(root_handle)
        with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
            _PENDING_WINDOWS_TREE_CLEANUPS[key] = state
    drained, _ = _advance_owned_windows_tree(state)
    return drained


def retry_pending_windows_process_trees(
    limit: int = _PENDING_WINDOWS_TREE_RETRY_LIMIT,
) -> tuple[int, ...]:
    """Advance a finite, fair snapshot of process-owned failed Windows drains.

    Each selected tree gets one attempt. Failures remain visible and move behind
    their peers, so a permanently denied tree cannot starve a healthy one. A tree
    already draining on another caller is skipped non-blockingly and rotated
    behind its peers, so one busy entry cannot hold up the whole sweep. This is
    same-process maintenance, not crash recovery; it acquires no PID authority.
    Returns the exact root PIDs whose handle-verified drains completed AND whose
    tracking retirement succeeded.
    """
    if not IS_WINDOWS or type(limit) is not int or limit <= 0:
        return ()
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        selected = [
            (key, state)
            for key, state in _PENDING_WINDOWS_TREE_CLEANUPS.items()
            if not state.manual_required
        ][:limit]
    completed: list[int] = []
    for key, state in selected:
        try:
            _, completed_here = _advance_owned_windows_tree(state, try_only=True)
            if completed_here:
                completed.append(state.root_pid)
            else:
                # Busy trees and failed metadata writes both yield to peers.
                # Fully retired entries have already left the registry.
                with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                    if _PENDING_WINDOWS_TREE_CLEANUPS.get(key) is state:
                        del _PENDING_WINDOWS_TREE_CLEANUPS[key]
                        _PENDING_WINDOWS_TREE_CLEANUPS[key] = state
        except Exception as exc:
            logger.warning(
                "Windows exact-handle tree cleanup remains pending for PID %d (%s)",
                state.root_pid,
                exc,
            )
            with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
                if _PENDING_WINDOWS_TREE_CLEANUPS.get(key) is state:
                    del _PENDING_WINDOWS_TREE_CLEANUPS[key]
                    _PENDING_WINDOWS_TREE_CLEANUPS[key] = state
    return tuple(completed)


async def terminate_windows_asyncio_tree(process: asyncio.subprocess.Process) -> bool:
    """Drain the original asyncio Windows process tree even after root exit.

    asyncio's transport retains the original Popen in its extra-info mapping.
    Duplicate that handle and transfer its ownership to process-local cleanup
    state before draining. The exact root and every observed intermediary then
    outlive provider/client GC after a failed pass. Repeated caller cancellation
    is re-delivered only after the current cleanup attempt settles.
    """
    candidate = getattr(process, "_windows_cleanup_state", None)
    admitted = isinstance(candidate, _PendingWindowsTreeCleanup)
    state: _PendingWindowsTreeCleanup
    handle: int | None
    if isinstance(candidate, _PendingWindowsTreeCleanup):
        state = candidate
        handle = None
        # Publish cleanup debt before executor submission: a rejected worker
        # must still leave the original pins reachable by maintenance after GC.
        with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
            if not state.retired:
                _PENDING_WINDOWS_TREE_CLEANUPS[state.key] = state
    else:
        state = reserve_windows_tree_cleanup()
        handle = duplicate_asyncio_process_handle(process)
        if handle is None:
            release_windows_tree_reservation(state)
            raise OSError("Cannot retain the original Windows runtime process handle")

    def drain() -> bool:
        if admitted:
            result, _ = _advance_owned_windows_tree(state)
        else:
            assert handle is not None
            result = terminate_windows_process_tree_owned(handle, reservation=state)
        if not result:
            raise OSError("Windows tree tracking retirement did not complete")
        return result

    async def cleanup() -> bool:
        loop = asyncio.get_running_loop()
        try:
            worker = loop.run_in_executor(subprocess_executor(), drain)
        except BaseException:
            if handle is not None:
                # Executor submission never transferred this duplicate.
                close_process_handle(handle)
                release_windows_tree_reservation(state)
            raise
        result = await asyncio.shield(worker)
        await asyncio.wait_for(process.wait(), timeout=_WINDOWS_TREE_REAP_TIMEOUT_SECS)
        return result

    task = asyncio.ensure_future(cleanup())
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


def process_matches(pid: int, needles: tuple[str, ...]) -> bool:
    """Return True iff *pid*'s command line / image name contains any *needle*.

    Used to guard against PID recycling before killing a tracked process.
    Linux: ``/proc/<pid>/cmdline``. macOS: ``ps -o command=``.
    Windows: the image name from ``CreateToolhelp32Snapshot`` (the full command
    line is not cheaply available). A harness hosted by an interpreter reads as
    that interpreter's image there — a Node-hosted ACP adapter is ``node.exe`` —
    so a needle naming the adapter itself cannot match on Windows, and a caller
    that needs a per-harness answer reads a recorded start identity instead
    (``get_process_start_id``). Returns False on any failure.
    """
    try:
        if sys.platform == "linux":
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            return any(n.encode() in cmdline for n in needles)
        if sys.platform == "darwin":
            ps_bin = trusted_system_bin("ps")
            if ps_bin is None:
                return False
            out = subprocess.check_output(
                [ps_bin, "-o", "command=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return any(n.encode() in out for n in needles)
        if IS_WINDOWS:
            name = _win_process_image_name(pid)
            if name is None:
                return False
            low = name.lower()
            return any(n.lower() in low for n in needles)
    except Exception:
        return False
    return False


def process_image_name(pid: int) -> str:
    """The process image (exe) name for *pid* on Windows, ``""`` anywhere else.

    Exposed because an image name is the only per-process identity Windows offers
    cheaply, and a caller that wants to compare it EXACTLY cannot use
    :func:`process_matches`, whose Windows arm is a substring test. Off Windows the
    answer is ``""`` rather than a POSIX equivalent: the platforms that have a real
    command line should read that instead of a name that would drop the interpreter
    distinction.
    """
    if not IS_WINDOWS:
        return ""
    return _win_process_image_name(pid) or ""


def _win_process_image_name(pid: int) -> str | None:
    """Return the image (exe) name for *pid* on Windows, or None on failure."""
    try:

        TH32CS_SNAPPROCESS = 0x00000002  # noqa: N806 — Windows API constant
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        entry_ptr = ctypes.POINTER(_ProcessEntry32)

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32First.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32Next.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wintypes.HANDLE(-1).value:
            return None
        try:
            entry = _ProcessEntry32()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32)
            if not kernel32.Process32First(snap, ctypes.byref(entry)):
                return None
            while True:
                if entry.th32ProcessID == pid:
                    return entry.szExeFile.decode(errors="replace")
                if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                    return None
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return None


def process_argv_matches_exact(pid: int, expected_argv: Sequence[str]) -> bool:
    """Return True iff *pid*'s FULL command line is exactly *expected_argv*.

    The strict identity check behind reclaiming a child this process's own
    lineage spawned and then lost (a recorded pid surviving a supervisor
    hard-kill): a recorded pid may have been recycled onto an unrelated
    process, and :func:`process_matches`-style substring needles cannot tell
    the two apart — partial argv matching against the process table is exactly
    what once killed forwards operators had started themselves. So the whole
    argv must match, element for element, and every failure answers False.

    For a DESTRUCTIVE decision this check must be paired with a
    :func:`process_start_time` pin recorded when the child was spawned: argv
    equality alone cannot rule out a recycled pid running an identical
    command line, and on macOS the comparison basis below makes equality
    necessary but not sufficient for vector equality. The pair fails toward
    "do not signal" on either mismatch.

    Linux: ``/proc/<pid>/cmdline`` NUL-split and compared element-wise (an
    empty cmdline — a zombie — never matches). macOS: ``ps -ww -o command=``
    reports the argv space-joined, so the comparison is against
    ``" ".join(expected_argv)``; exact only when no expected element contains
    a space, which holds for the argv shapes this guards (option tokens and
    validated host/target strings). Windows: always False — the raw
    ``Win32_Process.CommandLine`` string (see :func:`process_command_line`)
    carries shell quoting rather than an argv vector, so element-exact
    equality is not verifiable there; the guard fails closed and callers must
    not signal.
    """
    if type(pid) is not int or pid <= 1 or not expected_argv:
        return False
    try:
        if sys.platform == "linux":
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            if not raw:
                return False  # zombie / kernel thread: no argv to confirm
            parts = raw.split(b"\0")
            if parts and parts[-1] == b"":
                parts.pop()  # trailing NUL terminator
            return parts == [a.encode() for a in expected_argv]
        if sys.platform == "darwin":
            ps_bin = trusted_system_bin("ps")
            if ps_bin is None:
                return False
            out = subprocess.check_output(
                [ps_bin, "-ww", "-o", "command=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return out.decode(errors="replace").strip() == " ".join(expected_argv)
    except Exception:
        return False
    return False


def listening_pid_tool() -> str:
    """Return the external tool find_listening_pids relies on: 'lsof' / 'netstat'."""
    return "netstat" if IS_WINDOWS else "lsof"


def listening_pid_tool_available() -> bool:
    """Whether the port->PID lookup tool (lsof on POSIX / netstat on Windows) is
    resolvable. Lets callers distinguish "tool absent" from "no listener found",
    which find_listening_pids() alone collapses into an empty list — without that
    a genuinely-running gateway reads as stopped when lsof is missing.

    Resolves through :func:`trusted_system_bin`, the same lookup
    :func:`find_listening_pids` performs. Probing ``PATH`` here instead would
    let the two disagree: a shim on ``PATH`` would answer "available" for a tool
    the pinned lookup refuses to run, turning a live gateway into one that reads
    as stopped — the exact failure this probe exists to prevent. A tool that is
    installed but outside those directories therefore reads as absent, which
    :func:`trusted_system_bin` logs so the answer can be explained.
    """
    return trusted_system_bin(listening_pid_tool()) is not None


class PortListener(NamedTuple):
    """One LISTEN socket on a TCP port: the owning PID plus the local address
    it bound, so callers can scope port ownership to the address they actually
    probed instead of claiming every listener on the port."""

    pid: int
    #: Normalized local host part, brackets stripped: ``"127.0.0.1"``,
    #: ``"0.0.0.0"``, ``"::"``, ``"::1"``, a specific interface address, or
    #: ``"*"`` (lsof prints the wildcard bind of either family as ``*``).
    address: str
    #: Address family: ``"4"``, ``"6"``, or ``""`` when the source did not say.
    #: Load-bearing for wildcards — lsof spells both families ``*``, and only
    #: the family tells a v4 wildcard apart from a possibly-v6-only one.
    family: str = ""


# Local addresses whose listener can receive a connect to ``127.0.0.1``: the v4
# loopback itself, the v4 wildcard, lsof's family-agnostic wildcard ``*``, and
# the v6 wildcard ``::`` (dual-stack sockets accept v4-mapped loopback; treating
# it as non-covering would refuse adoption of a legitimately ``[::]``-bound
# backend, which is the breaking direction). ``::1`` is deliberately absent: a
# v6-loopback-only listener can never answer a probe addressed to 127.0.0.1.
_LOOPBACK_COVERING_ADDRESSES = frozenset({"127.0.0.1", "0.0.0.0", "*", "::"})

# Bound so a wedged lsof (stale mount, jammed process table) degrades to "no
# listener found" instead of hanging every caller of the port->PID lookup; the
# Windows netstat branch carries its own inline bound.
_LSOF_TIMEOUT_SECS = 5


def _normalize_local_address(address: str) -> str:
    """Bare lowercase host part: brackets stripped, v4-mapped prefix removed."""
    addr = address.strip().strip("[]").lower()
    if addr.startswith("::ffff:"):
        # v4-mapped form of a v4 address; compare the embedded v4 part.
        addr = addr[len("::ffff:") :]
    return addr


def address_covers_loopback(address: str) -> bool:
    """Whether a listener bound to *address* can receive a ``127.0.0.1`` connect.

    Used to scope port ownership to the address a health probe actually talked
    to: a listener on some other specific local address shares the port number
    but was never the thing that answered the probe.
    """
    return _normalize_local_address(address) in _LOOPBACK_COVERING_ADDRESSES


def loopback_owner_pids(listeners: list[PortListener]) -> list[int]:
    """PIDs of the listener(s) a successful ``127.0.0.1:<port>`` connect reached.

    Mirrors the kernel's most-specific-bind dispatch — the first non-empty
    tier wins, and every PID within it is returned (pre-fork / multi-worker
    backends legitimately share one listening socket):

    1. Exact v4-loopback binds (``127.0.0.1``, incl. the v4-mapped spelling):
       when one exists the kernel routes a loopback connect to it, so wildcard
       listeners on the same port never saw the probe.
    2. IPv4 wildcard binds: a v4 connect reaches the v4 wildcard socket in
       preference to a dual-stack v6 one.
    3. Remaining loopback-covering binds (the v6 wildcard, or one whose family
       the source did not report): callers only ask after a successful
       127.0.0.1 probe, so when nothing more specific exists, what is left
       must have been the responder (a dual-stack socket).

    Tier 2 is what keeps an unrelated ``IPV6_V6ONLY`` wildcard process from
    being claimed alongside the real v4 owner sharing its port.
    """
    exact = [e for e in listeners if _normalize_local_address(e.address) == "127.0.0.1"]
    if exact:
        return list(dict.fromkeys(e.pid for e in exact))
    covering = [e for e in listeners if address_covers_loopback(e.address)]
    v4 = [e for e in covering if e.family == "4"]
    if v4:
        return list(dict.fromkeys(e.pid for e in v4))
    return list(dict.fromkeys(e.pid for e in covering))


def probe_port_listeners(
    port: int, *, process_pid: int | None = None
) -> tuple[list[PortListener], bool]:
    """Return LISTEN sockets on *port* and whether the lookup completed.

    POSIX asks ``lsof -nP -iTCP:<port> -sTCP:LISTEN -Fptn``. When
    *process_pid* is supplied it adds ``-a -p <pid>`` so lsof inspects only that
    process. Exit 1 with no stdout or stderr is lsof's ordinary completed "no
    matches" answer. A diagnostic, timeout, execution error, unavailable binary,
    or any other nonzero exit is not a completed lookup. Windows parses
    ``netstat -ano`` and requires a zero exit; it has no PID-scoped command mode.

    The status bit is for security-sensitive callers that must distinguish a
    completed empty answer from an operational failure. Ordinary callers should
    use :func:`find_port_listeners`, which preserves the existing best-effort
    ``[]``-on-any-failure contract.
    """
    if IS_POSIX:
        lsof_bin = trusted_system_bin("lsof")
        if lsof_bin is None:
            return [], False
        try:
            argv = [lsof_bin, "-nP"]
            if process_pid is not None:
                argv.extend(["-a", "-p", str(process_pid)])
            argv.extend([f"-iTCP:{port}", "-sTCP:LISTEN", "-Fptn"])
            out = subprocess.check_output(
                # -n/-P keep addresses and ports numeric so the field parse
                # below never sees a resolved host or service name; the t
                # (type) field carries the family, without which the two
                # wildcard binds are indistinguishable (both print ``*``).
                argv,
                text=True,
                stderr=subprocess.PIPE,
                timeout=_LSOF_TIMEOUT_SECS,
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 1 and not exc.output and not exc.stderr:
                return [], True
            return [], False
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return [], False
        suffix = f":{port}"
        listeners: list[PortListener] = []
        seen: set[PortListener] = set()
        cur_pid: int | None = None
        cur_family = ""
        for line in out.splitlines():
            if not line:
                continue
            tag, value = line[0], line[1:]
            if tag == "p":
                cur_pid = int(value) if value.isdigit() else None
                cur_family = ""
            elif tag == "t":
                cur_family = {"IPv4": "4", "IPv6": "6"}.get(value, "")
            elif tag == "n" and cur_pid is not None and value.endswith(suffix):
                # ``n127.0.0.1:8080`` / ``n*:8080`` / ``n[::1]:8080`` — strip
                # the port suffix and the v6 brackets to the bare host part.
                entry = PortListener(cur_pid, value[: -len(suffix)].strip("[]"), cur_family)
                if entry not in seen:
                    seen.add(entry)
                    listeners.append(entry)
        return listeners, True
    # Windows: netstat -ano. Lines look like:
    #   TCP    127.0.0.1:7777         0.0.0.0:0    LISTENING    17152   (IPv4)
    #   TCP    [::1]:7777             [::]:0       LISTENING    17152   (IPv6)
    # No `-p tcp`: that flag restricts output to IPv4 TCP on Windows and
    # silently drops IPv6 listeners entirely, so `kirocrew stop` /
    # `kirocrew restart` no-op on a dual-stack or `[::]`-bound gateway
    # Windows netstat labels IPv6 rows with proto column
    # "TCP" too — the bracketed address form is what distinguishes v4 vs
    # v6, not the proto token — so once `-p tcp` is dropped the existing
    # port suffix match already handles both families uniformly.
    netstat_bin = trusted_system_bin("netstat")
    if netstat_bin is None:
        return [], False
    try:
        out = subprocess.check_output(
            [netstat_bin, "-ano"],
            # encoding="oem" (Windows-only pseudo-codec): netstat emits the
            # console OEM codepage when piped; text=True would decode with the
            # ANSI codepage and can raise UnicodeDecodeError on non-Western
            # locales — which, as a ValueError, would escape a
            # (SubprocessError, OSError) net and crash stop/status instead of
            # degrading to []. errors="replace" belts the rest.
            encoding="oem",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError, ValueError):
        return [], False
    suffix = f":{port}"
    listeners = []
    seen = set()
    for line in out.splitlines():
        parts = line.split()
        # Expect: proto local foreign state pid.
        # startswith("TCP") not `== "TCP"` for defensive future-proofing:
        # today Windows netstat prints plain "TCP" for both families, but a
        # future Windows build could relabel IPv6 rows as "TCP6" (the netstat
        # -p flag already accepts "tcpv6"). UDP rows never carry a LISTEN
        # state, so the listener check below is the load-bearing filter for
        # the non-TCP case, but keeping the proto guard avoids feeding
        # malformed ICMPv6 / RAW lines into the port match.
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        # Listener detection via the FOREIGN address, not the state column:
        # netstat localizes state names ("ABHÖREN" on German Windows,
        # Cyrillic on Russian), so matching the English "LISTENING" literal
        # returns [] on any non-English locale and stop/restart silently
        # no-op. A TCP row whose foreign endpoint is the wildcard 0.0.0.0:0 /
        # [::]:0 is in LISTEN state by definition, locale-independently.
        # Accept the English literal too as a defensive second signal.
        if parts[2] not in ("0.0.0.0:0", "[::]:0") and parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        # Suffix-match the local-address port. Formats: A.B.C.D:port for
        # TCP4 and [::]:port / [::1]:port / [fe80::...]:port for TCP6.
        # ']' is never a digit, so no false-positive against a bracketed
        # suffix (e.g. [::1234]:7 would not endswith ":234").
        if not local.endswith(suffix):
            continue
        pid_str = parts[-1]
        if pid_str.isdigit():
            # The bracketed address form is what distinguishes v4 vs v6 on
            # Windows netstat output (the proto column says "TCP" for both).
            entry = PortListener(
                int(pid_str),
                local[: -len(suffix)].strip("[]"),
                "6" if local.startswith("[") else "4",
            )
            if entry not in seen:
                seen.add(entry)
                listeners.append(entry)
    return listeners, True


def _linux_loopback_listener_inodes(port: int, proc_root: Path) -> set[str] | None:
    """LISTEN socket inodes on *port* that can answer IPv4 loopback.

    ``/proc/net/tcp`` stores IPv4 addresses little-endian. ``tcp6`` stores each
    32-bit word little-endian. The browser child is asked to bind 127.0.0.1, but
    wildcard and IPv4-mapped wildcard-compatible rows are included because a
    successful loopback health probe can reach them too.
    """
    v4_addresses = {"00000000", "0100007F"}
    v6_addresses = {
        "0" * 32,
        "0000000000000000FFFF00000100007F",
    }
    inodes: set[str] = set()
    completed = False
    for name, accepted_addresses in (("tcp", v4_addresses), ("tcp6", v6_addresses)):
        try:
            rows = (proc_root / "net" / name).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        completed = True
        for row in rows.splitlines()[1:]:
            fields = row.split()
            if len(fields) <= 9 or fields[3].upper() != "0A":
                continue
            try:
                address, port_hex = fields[1].rsplit(":", 1)
                bound_port = int(port_hex, 16)
            except (ValueError, IndexError):
                continue
            if bound_port == port and address.upper() in accepted_addresses:
                inode = fields[9]
                if inode.isdigit() and int(inode) > 0:
                    inodes.add(inode)
    return inodes if completed else None


def _windows_loopback_listener_owner_pids(port: int) -> set[int] | None:
    """Owner PIDs for Windows listeners that can answer IPv4 loopback."""
    if not IS_WINDOWS or type(port) is not int or not 1 <= port <= 65535:
        return None
    listeners: list[PortListener] = []
    for ipv6 in (False, True):
        rows = _windows_tcp_owner_rows(
            ipv6,
            _WINDOWS_TCP_TABLE_OWNER_PID_LISTENER,
        )
        if rows is None:
            return None
        family = "6" if ipv6 else "4"
        for row in rows:
            if (
                row.state != _WINDOWS_TCP_STATE_LISTEN
                or row.pid <= 0
                or row.local_scope_id != 0
                or row.local_port != port
            ):
                continue
            try:
                address = str(ipaddress.ip_address(row.local_address))
            except ValueError:
                continue
            listeners.append(PortListener(row.pid, address, family))
    return set(loopback_owner_pids(listeners))


def process_owns_loopback_listener(
    pid: int, port: int, *, proc_root: Path | None = None
) -> bool | None:
    """Whether *pid* owns a LISTEN socket that can answer 127.0.0.1:*port*.

    ``True`` and ``False`` are completed per-process observations. ``None``
    means this host cannot make the observation, so callers may use a separate
    child-specific proof without mistaking probe failure for non-ownership.

    Linux compares socket inodes from ``/proc/net/tcp{,6}`` with symlink targets
    under ``/proc/<pid>/fd``. Other POSIX hosts run the existing listener parser
    with lsof scoped by ``-a -p <pid>``. Windows reads the IPv4 and IPv6 owner-PID
    listener tables in-process with ``GetExtendedTcpTable``; it never needs
    ``netstat`` for this per-process decision.
    """
    if type(pid) is not int or pid <= 0 or type(port) is not int or not 1 <= port <= 65535:
        return None
    if IS_WINDOWS:
        owners = _windows_loopback_listener_owner_pids(port)
        return None if owners is None else pid in owners
    if IS_LINUX:
        root = proc_root if proc_root is not None else Path("/proc")
        listener_inodes = _linux_loopback_listener_inodes(port, root)
        if listener_inodes is None:
            return None
        if not listener_inodes:
            return False
        try:
            descriptors = list((root / str(pid) / "fd").iterdir())
        except OSError:
            return None
        readable = 0
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            readable += 1
            if target.startswith("socket:[") and target.endswith("]"):
                if target[8:-1] in listener_inodes:
                    return True
        if descriptors and readable == 0:
            return None
        return False

    if not IS_POSIX:
        return None
    listeners, completed = probe_port_listeners(port, process_pid=pid)
    if not completed:
        return None
    return pid in loopback_owner_pids(listeners)


def find_port_listeners(port: int) -> list[PortListener]:
    """Return LISTEN sockets on *port*, or ``[]`` on any lookup failure.

    Best-effort, deduped on ``(pid, address, family)``, and never raises. Use
    :func:`probe_port_listeners` when a caller must distinguish a completed
    empty lookup from an operational failure.
    """
    listeners, _completed = probe_port_listeners(port)
    return listeners


def find_listening_pids(port: int) -> list[int]:
    """Return PIDs with a LISTEN socket on TCP *port* (best-effort, deduped).

    Address-agnostic accessor over :func:`find_port_listeners` for callers that
    only care whether/which processes hold the port. A dual-stack listener
    appears once per bound address (``0.0.0.0`` and ``::``) under the same PID —
    collapsed to one entry here, first-seen order preserved. Same failure
    contract: ``[]`` on any failure, never raises.
    """
    return list(dict.fromkeys(entry.pid for entry in find_port_listeners(port)))


def linux_process_name(pid: int, *, proc_root: Path | None = None) -> str | None:
    """Return Linux ``comm`` for *pid*, or ``None`` when it is unproven.

    The kernel caps ``comm`` and exposes it independently of environ, which
    makes it suitable only for conservative process-class decisions. *proc_root*
    is a test seam for fixture-owned process tables.
    """
    if sys.platform != "linux":
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        name = (
            (root / str(pid) / "comm")
            .read_bytes()
            .decode("utf-8", errors="surrogateescape")
            .strip()
        )
    except OSError:
        return None
    return name or None


def process_cgroups_match(
    pid: int,
    reference_pid: int,
    *,
    proc_root: Path | None = None,
) -> bool | None:
    """Compare two Linux processes' stable unified-cgroup memberships.

    Returns ``True`` for equal cgroup v2 memberships, ``False`` for proven
    different memberships, and ``None`` off Linux, on cgroup v1, or when either
    read is unavailable or changes during the probe. Reading both files twice
    prevents a concurrent cgroup move from licensing a destructive action.
    *proc_root* is a test seam for fixture-owned process tables.
    """
    if sys.platform != "linux":
        return None
    root = proc_root if proc_root is not None else Path("/proc")

    def read_membership(process_pid: int) -> str | None:
        raw = (
            (root / str(process_pid) / "cgroup")
            .read_bytes()
            .decode("utf-8", errors="surrogateescape")
        )
        lines = tuple(line for line in raw.splitlines() if line)
        if len(lines) != 1 or not lines[0].startswith("0::/"):
            return None
        return lines[0]

    try:
        pid_first = read_membership(pid)
        reference_first = read_membership(reference_pid)
        pid_second = read_membership(pid)
        reference_second = read_membership(reference_pid)
    except OSError:
        return None
    if (
        pid_first is None
        or reference_first is None
        or pid_first != pid_second
        or reference_first != reference_second
    ):
        return None
    return pid_first == reference_first


def process_command_line(pid: int) -> str:
    """Return the full command line of *pid*, or ``""`` on failure (best-effort).

    Linux: ``/proc/<pid>/cmdline`` (NUL-joined → spaces).
    macOS: ``ps -o command= -p <pid>``.
    Windows: ``Win32_Process.CommandLine`` via WMI (PowerShell ``Get-CimInstance``).
    Used to confirm a listener PID is actually a KiroCrew gateway when the image
    name alone is ambiguous (the venv ``kirocrew.exe`` re-execs ``python.exe``).
    """
    try:
        if sys.platform == "linux":
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            return raw.replace(b"\x00", b" ").decode(errors="replace").strip()
        if sys.platform == "darwin":
            ps_bin = trusted_system_bin("ps")
            if ps_bin is None:
                return ""
            out = subprocess.check_output(
                [ps_bin, "-o", "command=", "-p", str(pid)],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return out.strip()
        if IS_WINDOWS:
            # Query WMI for the exact PID's command line. PowerShell is always
            # present on supported Windows; -NoProfile keeps it fast.
            powershell_bin = trusted_system_bin("powershell")
            if powershell_bin is None:
                return ""
            out = subprocess.check_output(
                [
                    powershell_bin,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
                    ".CommandLine",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=_SUBPROCESS_NO_WINDOW,
            )
            return out.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return ""


def process_owner_uid(pid: int) -> int | None:
    """Return the uid owning *pid*, or ``None`` when it cannot be determined.

    Linux: ``os.stat("/proc/<pid>").st_uid``.
    macOS: ``ps -o uid= -p <pid>``.
    Windows: ``None`` — there is no uid concept, and a WMI ``GetOwner`` round
    trip costs a PowerShell spawn per call; callers that need an ownership gate
    must decide what to do with ``None`` explicitly rather than assume a match.

    Used to confirm that a pid a client is about to trust belongs to the calling
    user (see ``port_resolution._gateway_owns_port``), which is what makes pid
    recycling into a *foreign* user's process non-exploitable.
    """
    try:
        if sys.platform == "linux":
            return os.stat(f"/proc/{int(pid)}").st_uid
        if sys.platform == "darwin":
            # An unresolvable ``ps`` yields None, which ``_gateway_owns_port``
            # treats as "ownership unproven" and denies on — the same direction
            # as every other failure in that gate.
            ps_bin = trusted_system_bin("ps")
            if ps_bin is None:
                return None
            out = subprocess.check_output(
                [ps_bin, "-o", "uid=", "-p", str(int(pid))],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            raw = out.strip()
            return int(raw) if raw.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


# Tri-state liveness results for pid_liveness().
PID_DEAD = "dead"  # confirmed not running -> safe to prune
PID_ALIVE = "alive"  # confirmed running
PID_UNSIGNALABLE = "unsignalable"  # exists but we cannot signal it (POSIX EPERM)


def live_thread_group_leaders() -> frozenset[int] | None:
    """Every pid on the host that is a PROCESS, or ``None`` when unknowable.

    Linux numbers threads from the same space as processes and exposes
    ``/proc/<tid>`` for them, and POSIX permits signalling a tid — so a tid
    satisfies both :func:`pid_exists` and :func:`pid_liveness` while naming no
    process at all. A caller holding a recorded pid therefore cannot tell "my
    process is still alive" from "that number now belongs to some unrelated
    process's thread", which matters once the pid counter wraps
    (``/proc/sys/kernel/pid_max`` is commonly 4194304 and a busy host cycles it
    in hours).

    The discriminator is ``/proc`` itself: its top-level listing enumerates
    ONLY thread-group leaders. A non-leader tid is absent from that listing
    even though ``/proc/<tid>`` stays directly openable — which is exactly why
    the cheaper per-pid probes cannot see the difference.

    Deliberately ONE directory read for the whole host rather than a read per
    pid. A sweep over N recorded mappings costs a single ``os.listdir`` instead
    of N opens of ``/proc/<pid>/status`` (measured on Linux: 1.5 ms once versus
    7.8 ms across 233 mappings), so no per-entry synchronous file read happens
    on the caller's thread at all. Also cheaper than ``process_matches``, which
    shells out to ``ps`` on macOS and so cannot be used per entry in a sweep.

    Returns ``None`` — never an empty set — whenever the answer is not knowable
    (non-Linux, unreadable ``/proc``, or a listing with no numeric entries).
    Callers use this to *narrow* a liveness check, so an inconclusive result
    must never be the thing that decides a pid is stale: treat ``None`` as
    "retain everything".
    """
    if not IS_LINUX:
        return None
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    leaders = {int(name) for name in entries if name.isdigit()}
    if not leaders:
        return None
    return frozenset(leaders)


def is_thread_group_leader(pid: int) -> bool | None:
    """Whether ``pid`` names a PROCESS right now, or ``None`` when unknowable.

    The per-pid counterpart to :func:`live_thread_group_leaders`, for the one
    question a host-wide snapshot cannot answer. A snapshot is a reading taken at
    an instant, so a pid recycled AFTER it was taken is absent from it while
    naming a live process. A caller about to act destructively on "absent from
    the snapshot" therefore needs a reading taken now, for that pid alone.

    ``/proc/<pid>/status`` carries ``Tgid``, the pid of the thread group's
    leader, so ``Tgid == pid`` is a process while a non-leader tid reports its
    leader's pid instead. One file read is the cheap way to ask about one pid,
    where the top-level ``/proc`` listing is the cheap way to ask about all of
    them -- which is why this narrows the snapshot rather than replacing it.

    Returns ``None`` -- never ``False`` -- whenever the answer is not knowable
    (non-Linux, the pid is gone, an unreadable or malformed ``status``), so an
    inconclusive read can never be the thing that licenses a destructive action.
    """
    if not IS_LINUX:
        return None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("Tgid:"):
                    return int(line.split()[1]) == pid
    except (OSError, ValueError, IndexError):
        return None
    return None


def pid_liveness(pid: int) -> str:
    """Three-way liveness probe: PID_DEAD / PID_ALIVE / PID_UNSIGNALABLE.

    Unlike :func:`pid_exists` (which collapses ALIVE and UNSIGNALABLE into
    ``True``), this distinguishes "exists but owned by another user / can't be
    signalled" (POSIX ``EPERM``) from "running and ours". Callers that must
    LEAVE an unsignalable PID untouched (the orphan sweep — pruning or killing
    a PID we merely can't signal is wrong) branch on ``PID_UNSIGNALABLE``.

    POSIX: ``os.kill(pid, 0)`` — ``ProcessLookupError`` -> DEAD,
    ``PermissionError`` or an out-of-range PID -> UNSIGNALABLE, success ->
    ALIVE.
    Windows: no EPERM distinction for our processes; map ``pid_exists`` onto
    DEAD/ALIVE (UNSIGNALABLE never returned).
    """
    if IS_POSIX:
        try:
            os.kill(pid, 0)
            return PID_ALIVE
        except ProcessLookupError:
            return PID_DEAD
        except PermissionError:
            return PID_UNSIGNALABLE
        except OverflowError:
            # The PID may have come from corrupt persistent state.  It is not
            # evidence that the named process is dead, so preserve fail-closed
            # callers by classifying it as unknown/unsignalable.
            return PID_UNSIGNALABLE
        except OSError:
            # Unknown errno — be conservative and treat as unsignalable
            # (leave it alone) rather than risk pruning/killing a live PID.
            return PID_UNSIGNALABLE
    return PID_ALIVE if pid_exists(pid) else PID_DEAD


def pgroup_exists(pgid: int) -> bool:
    """Return True iff any member of process GROUP ``pgid`` is alive (best-effort).

    The tree-faithful liveness probe for a child spawned with
    ``start_new_session=True``: the launcher's pid doubles as the group id and
    ordinary descendants keep it after the launcher exits, so the group
    outlives the launcher exactly as long as any member does. A descendant
    that ``setsid()``s out of the group evades this probe precisely as it
    evades ``kill_process_tree`` -- callers that must catch those use the
    escaped-children reapers, not this.

    POSIX: ``os.killpg(pgid, 0)`` -- conservative on EPERM (unsignalable
    reads as alive). Windows: process groups in this sense do not exist and
    ``kill_process_tree`` already walks the whole child tree via
    ``taskkill /T``, so the group id (== the launcher pid) is probed as a
    plain pid via :func:`pid_exists`.
    """
    if not IS_POSIX:
        return pid_exists(pgid)
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but we can't signal it
    return True


def pgroup_of(pid: int) -> int | None:
    """The process GROUP *pid* belongs to, or ``None`` when it cannot be read.

    Distinct from :func:`pgroup_of_leader`, which answers "what group does this
    LEADER name" and falls back to the pid itself so a reaped leader's group can
    still be signalled. This one asks a plain membership question about a pid that
    may be any descendant, so there is no leader contract to fall back on and an
    unreadable answer is ``None`` rather than a guess.

    Callers use it to tell an in-group descendant (a group kill covers it) from one
    that has ``setsid``'d out of the group (it has to be signalled on its own), so
    a wrong guess here either signals a stranger or leaves a live writer behind --
    which is why the failure answers "unknown".

    POSIX ONLY, deliberately unguarded: ``os.getpgid`` does not exist on Windows,
    and a caller reaching here on that platform has the wrong primitive.
    """
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def pgroup_of_leader(pid: int) -> int | None:
    """Resolve the process GROUP to signal for a group leader ``pid``.

    Companion to :func:`pgroup_exists` for the teardown side: a caller that must
    signal a group needs its id even when the leader itself has been reaped,
    because the group outlives its leader and the children left in it keep
    running. ``os.getpgid`` cannot name such a group -- it raises
    ``ProcessLookupError`` once the leader is gone -- and reading that as "the
    group is gone" signals nothing at all while the tree survives.

    So a reaped leader resolves to ``pid`` itself: for a child spawned with
    ``start_new_session=True`` the leader's pid IS the group id (the same
    identity :func:`pgroup_exists` documents), so that number still names the
    group. ``killpg`` on a group that really is empty raises
    ``ProcessLookupError``, which callers already absorb, so the fallback costs
    nothing when the group is gone and is the whole teardown when it is not.

    Returns:
        The group id, or ``None`` when the group cannot be resolved because
        signalling it is denied (pid recycled to another user, or reduced
        privilege) -- a caller must not proceed to signal on ``None``.

    POSIX ONLY, deliberately unguarded: ``os.getpgid`` does not exist on
    Windows, so a caller reaching here on Windows has the wrong teardown
    primitive and gets an ``AttributeError`` saying so rather than a silent
    no-op. Windows teardown goes through ``kill_process_tree``.
    """
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return pid
    except PermissionError:
        return None


def pid_exists(pid: int) -> bool:
    """Return True iff ``pid`` currently exists (best-effort).

    POSIX: ``os.kill(pid, 0)``.
    Windows: ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)``.
    """
    if IS_POSIX:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError, OverflowError):
            return True  # exists but we can't signal it
    try:
        _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806 — Windows API constant
        _STILL_ACTIVE = 259  # noqa: N806 — Windows STILL_ACTIVE exit code
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            # OpenProcess SUCCEEDS for an EXITED process as long as any handle to
            # the kernel process object is still open — and asyncio's Proactor
            # transport keeps its duplicated handle open until GC, so a
            # just-killed child we awaited would read back as "exists". Confirm
            # with GetExitCodeProcess: STILL_ACTIVE means genuinely running;
            # any other code means it has exited (a defunct handle to a dead
            # PID), so report not-exists. Without this every Windows session
            # recycle logged a false "PID survived kill" and left dead PIDs in
            # the tracker until the periodic sweep.
            try:
                code = wintypes.DWORD()
                got = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                if got and code.value != _STILL_ACTIVE:
                    return False
                return True
            finally:
                kernel32.CloseHandle(handle)
        return getattr(ctypes, "get_last_error", lambda: 0)() == 5  # ERROR_ACCESS_DENIED → exists
    except Exception:
        return False


#: Seconds before the ``ps`` start-time probe is abandoned. Only the BSD leg
#: spawns anything; Linux reads /proc and Windows calls the kernel directly.
_START_TIME_PS_TIMEOUT = 2


def _process_lstart(pid: int) -> str | None:
    """Read the one-second ``ps -o lstart=`` identity encoding."""
    ps_bin = trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        out = subprocess.check_output(
            [ps_bin, "-o", "lstart=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            timeout=_START_TIME_PS_TIMEOUT,
        )
        # STRICT decode. A lossy one can alias two unreadable identities.
        return out.decode().strip() or None
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def process_start_time(pid: int) -> str | None:
    """Stable identity for WHEN *pid* started, or ``None`` when unreadable.

    An opaque token whose only contract is that it compares equal across gateway
    generations on the same host while the PID still names the same process
    object, and unequal once that PID has been recycled onto another. Units
    differ per platform and are deliberately not normalised -- nothing ever
    compares one host's value against another's, and no caller parses it.

    Callers use it as a PID-reuse guard before signalling, so an unreadable
    value must fail SAFE: ``None`` means "identity unconfirmed", which every
    caller treats as "do not kill".

    * **Linux** -- ``/proc/<pid>/stat`` field 22 (start time in clock ticks
      since boot): monotonic, locale-independent, and far finer than 1s, so
      same-second reuse cannot alias.
    * **Windows** -- the process creation ``FILETIME`` (100-ns units), read
      through a QUERY-ONLY handle. Terminate rights are deliberately NOT
      requested: this value is what decides whether a kill may happen at all, so
      demanding the right to kill in order to read it would refuse the guard for
      exactly the processes a caller must be most careful about.
    * **macOS / other POSIX** -- ``ps -o lstart=`` (1s resolution, locale/TZ
      formatted). Coarser, so a format or resolution drift can only make the
      guard decline to act, never act on the wrong process.
    """
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            # The comm field can contain spaces and parens; split after the
            # LAST ')' so a process named "(evil) 1 2 3" cannot shift the index.
            return stat.rsplit(")", 1)[1].split()[19]
        except (OSError, ValueError, IndexError):
            return None
    if IS_WINDOWS:
        # Opened and closed through the shared seams so this READ and the
        # identity-pinned TERMINATE below cannot drift in how they acquire or
        # release the handle -- the difference between the two is the handle's
        # LIFETIME, and that is easier to reason about with one acquisition site.
        handle = _open_process_query_handle(pid)
        if handle is None:
            return None
        try:
            identity = _windows_process_handle_identity(handle, await_exit_time=False)
        finally:
            _close_process_handle(handle)
        # (pid, creation_time, exit_time) -- only the creation half is an
        # identity; exit_time moves as the process dies. Asking for the creation
        # half alone keeps this read non-blocking, which is the contract
        # :func:`get_process_start_id` publishes to its event-loop callers.
        return str(identity[1]) if identity is not None else None
    return _process_lstart(pid)


def process_start_id_for_source(pid: int, source: ProcessIdentitySource) -> str | None:
    """Re-read *pid* with the same identity source used at capture time.

    There is deliberately no fallback between sources. ``WINDOWS`` reads
    process creation time through the query-only process handle; ``ATOMIC`` uses
    the native high-resolution start-ID reader, and ``LSTART`` uses ``ps``. If
    that source is now unavailable, the identity is inconclusive rather than
    compared against a token with a different encoding or resolution.
    """
    if source is ProcessIdentitySource.ATOMIC:
        return get_process_start_id(pid)
    if source is ProcessIdentitySource.WINDOWS:
        return process_start_time(pid)
    if source is ProcessIdentitySource.LSTART:
        return _process_lstart(pid)
    return None


#: (pid, token) cache for :func:`own_process_start_time`. Keyed by PID rather
#: than a bare value so a forked child re-reads its OWN identity instead of
#: inheriting the parent's — the OTEL SDK re-installs metric exporters in fork
#: children via ``os.register_at_fork``, so children genuinely export under
#: this token. Two threads racing the first read is benign: both compute the
#: same immutable tuple for the same process.
_OWN_START_TIME: tuple[int, str | None] | None = None


def _linux_boot_id() -> str | None:
    """The kernel's per-boot UUID, or ``None`` when unreadable."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def _own_identity_token(pid: int) -> str | None:
    """Reboot-unique start-time token for THIS process, or ``None``.

    The aggregator DISABLES its value-drop reset heuristic for any stream that
    carries a token, trusting one token = one OS process. A token that cannot
    honor that contract is therefore worse than no token — an aliased coarse
    token would merge two lifetimes AND mute the heuristic that catches the
    merge — so every degraded read returns ``None`` (no identity field, legacy
    heuristic applies) rather than a best-effort value:

    * **Linux** — ``/proc`` start ticks count from BOOT, so a post-reboot
      process can repeat an earlier boot's (PID, ticks) pair; metric shards
      outlive boots. The kernel's per-boot UUID makes the pair reboot-unique;
      without it, no token.
    * **macOS** — ``proc_pidinfo`` reports the absolute start instant at
      microsecond resolution, so a recycled PID cannot alias within the 1s
      window the ``ps -o lstart=`` probe cannot see past. Without ``libproc``,
      no token — the 1s probe is exactly such an aliasable coarse source.
    * **Windows** — the creation ``FILETIME`` (100ns units since 1601) is
      absolute, already reboot-unique and alias-proof.
    * **Other POSIX** — only the 1s ``ps`` probe exists: no token.
    """
    if sys.platform == "linux":
        ticks = process_start_time(pid)
        boot = _linux_boot_id()
        return f"{ticks}:{boot}" if ticks and boot else None
    if sys.platform == "darwin":
        return _darwin_process_start_microtime(pid)
    if IS_WINDOWS:
        return process_start_time(pid)
    return None


def own_process_start_time() -> str | None:
    """This process's own start-time identity, read once and cached.

    A module-scope cache of :func:`_own_identity_token` for the calling
    process. The cache is the contract, not an optimisation: every reader
    inside one process must observe the SAME token for the process lifetime,
    so a metrics provider rebuilt in-process (telemetry off/on) stamps records
    that stitch into one stream with those written before the rebuild — and a
    read that degrades mid-process (a ``libproc`` load failing on one call)
    must not flip the process between stamped and unstamped forms.

    Fail soft: an unreadable or alias-prone platform answer is cached as
    ``None`` for the process lifetime, so a consumer emits no identity at all
    rather than an identity that flaps between absent and present.
    """
    global _OWN_START_TIME
    pid = os.getpid()
    if _OWN_START_TIME is None or _OWN_START_TIME[0] != pid:
        _OWN_START_TIME = (pid, _own_identity_token(pid))
    return _OWN_START_TIME[1]


def process_thread_count(pid: int) -> int | None:
    """Thread count of *pid*, or ``None`` when it cannot be determined.

    Used to tell a *running* gateway (dozens of threads: the event loop, the
    executor pool, watchdogs, MCP stdio readers) apart from a **wedged fork** of
    one -- a child forked before ``exec`` inherits exactly one thread, the one
    that called ``fork()``, and never gains another. That single-thread signature
    is the decidable half of "this holder is an orphan, not a gateway".

    Linux: ``Threads:`` in ``/proc/<pid>/status``. Everywhere else: ``None``, so
    every caller must already handle "unknown" and degrade to a claim it can
    support. Deliberately no ``ps`` fallback for macOS -- shelling out from this
    low-level module would add an unrouted subprocess spawn (see
    ``test_spawn_audit``) to a purely diagnostic path.
    """
    if sys.platform != "linux":
        return None
    try:
        for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
            if ln.startswith("Threads:"):
                return int(ln.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def flock_owner_pid(path: str | os.PathLike) -> int | None:
    """PID recorded against an ``flock`` on *path*, via ``/proc/locks``.

    This is the pid that ACQUIRED the lock, which is not always a live process:
    an ``flock`` belongs to the open file description, so when the acquirer dies
    and a forked child still holds the inherited fd, the kernel keeps reporting
    the DEAD acquirer here. Verified on Linux 5.x -- an orphaned lock listed
    ``FLOCK ADVISORY WRITE <dead parent pid>`` while the surviving child held the
    fd. So a returned pid that is dead is positive evidence of an inherited fd,
    and no ``/proc`` surface names the inheritor: use
    :func:`pids_holding_file` for candidates and do not present them as owners.

    Returns ``None`` on non-Linux, when ``/proc/locks`` is unreadable, or when no
    ``FLOCK`` entry matches the file. Blocked waiters (``->`` rows) are skipped.

    Matching is on the full ``major:minor:inode`` triple that ``/proc/locks``
    prints, because inode numbers are only unique WITHIN a filesystem -- an
    unrelated flock on another device can share this inode's number, and
    accepting it would name a completely unrelated process. The device halves are
    hex (``%02x``), the inode decimal (``%lu``). On filesystems whose ``s_dev``
    differs from the ``st_dev`` that ``stat`` reports (btrfs subvolumes and
    overlayfs use anonymous devices) the triple will not match and this returns
    ``None``. That is the safe direction to fail: the caller degrades to its
    "holder could not be identified" wording instead of naming a wrong process.
    """
    if sys.platform != "linux":
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    want = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    try:
        # Streamed, not slurped: /proc/locks is unbounded (one row per lock
        # system-wide) and this runs on a startup failure path.
        with open("/proc/locks", encoding="utf-8") as handle:
            for row in handle:
                fields = row.split()
                # "23: FLOCK ADVISORY WRITE 39542 103:01:146872 0 EOF"; a blocked
                # waiter is "23: -> FLOCK ..." and owns nothing.
                if len(fields) < 6 or "->" in fields[:2] or fields[1] != "FLOCK":
                    continue
                try:
                    pid = int(fields[4])
                    major, minor, ino = fields[5].split(":")
                    found = (int(major, 16), int(minor, 16), int(ino))
                except (ValueError, IndexError):
                    continue
                if found == want:
                    return pid
    except OSError:
        return None
    return None


def parent_pid(pid: int) -> int | None:
    """PPID of *pid* from ``/proc/<pid>/stat``, or ``None`` if unknowable.

    Used to corroborate that a candidate process really is orphaned: a child
    reparented to init after its parent died reports ``1``. ``None`` means
    "unknown", which callers must not read as either answer.
    """
    if sys.platform != "linux":
        return None
    try:
        stat_data = Path(f"/proc/{pid}/stat").read_text()
        # comm (field 2) may contain spaces/parens -- parse after the LAST ')'
        close_paren = stat_data.rfind(")")
        if close_paren < 0:
            return None
        return int(stat_data[close_paren + 2 :].split()[1])
    except Exception:
        return None


def pids_holding_file(path: str | os.PathLike) -> list[int] | None:
    """PIDs with an open fd on *path*, matched by inode via ``/proc/*/fd``.

    These are CANDIDATE OPENERS, not lock owners. Any process may open the file
    without locking it, and an inherited ``flock`` has no live owner to identify
    (see :func:`flock_owner_pid`), so callers must not present a pid from here as
    the holder. Answering "who has this file open" is still the only way to reach
    the process that inherited a dead acquirer's descriptor.

    Matching is by ``(st_dev, st_ino)`` rather than by link target so a bind
    mount (a jailed gateway sees a different path for the same inode) still
    resolves. PIDs whose ``/proc`` entry we cannot read are skipped: the result
    is a best-effort lower bound.

    Returns ``None`` on non-Linux, where there is no ``/proc`` to walk.
    """
    if sys.platform != "linux":
        return None
    try:
        target = os.stat(path)
    except OSError:
        return None
    key = (target.st_dev, target.st_ino)
    holders: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            # Dead between listdir and here, or another user's process.
            continue
        for fd_name in fds:
            try:
                st = os.stat(f"{fd_dir}/{fd_name}")
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == key:
                holders.append(pid)
                break
    return holders


def _raise_taskkill_error(pid: int, rc: int, stderr: bytes) -> None:
    """Translate a Windows taskkill non-zero rc into ProcessLookupError /
    PermissionError / OSError so callers' POSIX-style ``except`` guards
    (``except (ProcessLookupError, OSError)``, ``except PermissionError``)
    fire on Windows too, matching the POSIX raise semantics.

    taskkill exit codes: 128 = process not found (rebadge as
    ProcessLookupError, POSIX analog of ESRCH); 1/5 = access denied
    (PermissionError, POSIX analog of EPERM); anything else = generic
    OSError with the stderr blob.
    """
    msg = (stderr or b"").decode("utf-8", "replace").strip() or f"taskkill rc={rc}"
    if rc == 128:
        raise ProcessLookupError(f"[taskkill rc=128] {msg}")
    if rc in (1, 5):
        raise PermissionError(f"[taskkill rc={rc}] {msg}")
    raise OSError(f"[taskkill rc={rc}] {msg}")


def kill_pid(pid: int, sig: int = SIGTERM) -> bool:
    """Send *sig* to *pid*. Returns True on success.

    POSIX: delegates to ``os.kill`` and **lets exceptions propagate**
    (``ProcessLookupError``, ``PermissionError``, ``OSError``) so callers
    can branch on them.
    Windows: uses ``taskkill /F`` and raises the same exception types on
    non-zero rc (mapped from taskkill's exit codes — see
    :func:`_raise_taskkill_error`) so ``except (ProcessLookupError,
    OSError)`` handlers written for POSIX fire uniformly. Returns True
    on success on both platforms.
    """
    if IS_POSIX:
        os.kill(pid, sig)
        return True
    taskkill_bin = trusted_system_bin("taskkill")
    if taskkill_bin is None:
        raise OSError("taskkill not found in the trusted system directories")
    try:
        r = subprocess.run(
            [taskkill_bin, "/F", "/PID", str(pid)],
            check=False,
            capture_output=True,
            timeout=5,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"taskkill invocation failed: {exc}") from exc
    if r.returncode != 0:
        _raise_taskkill_error(pid, r.returncode, r.stderr or r.stdout)
    return True


def kill_process_group(pgid: int, sig: int = SIGTERM) -> bool:
    """Signal every member of process group ``pgid``; False when none is left.

    The teardown for a group whose leader is already reaped, which
    :func:`kill_process_tree` cannot resolve from the leader's pid. Carries the
    same broadcast guard: ``pgid <= 1`` and this process's own group are refused.
    A denied signal also answers False, since that group is not the caller's.
    POSIX only; Windows has no process groups in this sense and answers False.
    """
    if not IS_POSIX:
        return False
    if type(pgid) is not int or pgid <= 1 or pgid == _OWN_PGID:
        raise ValueError(f"kill_process_group: refusing reserved or own pgid {pgid!r}")
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def kill_process_tree(pid: int, sig: int = SIGTERM) -> bool:
    """Kill *pid* and all descendants. Returns True on success.

    POSIX: ``os.killpg(os.getpgid(pid), sig)``; **lets exceptions
    propagate** (``ProcessLookupError`` if already dead, etc.).
    Windows: ``taskkill /T /F`` and raises the same exception types on
    non-zero rc (via :func:`_raise_taskkill_error`) so ``except
    (ProcessLookupError, OSError)`` handlers written for POSIX fire
    uniformly and callers' fallback / escalation branches execute on a
    genuine Windows failure (protected descendant, transient
    access-denied) instead of the shim silently returning False.
    Returns True on success on both platforms.

    POSIX broadcast guard: ``killpg(1, sig)`` is ``kill(-1, sig)`` in
    libc — a signal to EVERY process this uid owns (systemd --user
    manager, SSH, the gateway itself). A non-int pid (e.g. a mocked
    ``Popen``'s ``MagicMock`` pid coerces to 1 via ``__index__``),
    pid <= 1, pgid <= 1, or our own process group is therefore refused
    for the *group* signal and degrades to a pid-scoped ``os.kill``
    (or raises ``ValueError`` for a non-int pid).
    """
    if IS_POSIX:
        if type(pid) is not int or pid <= 1:
            raise ValueError(f"kill_process_tree: refusing non-int/reserved pid {pid!r}")
        pgid = os.getpgid(pid)
        if pgid <= 1 or pgid == _OWN_PGID:
            logger.error(
                "kill_process_tree: refusing broadcast/self pgid %d for pid %d; "
                "falling back to pid-scoped kill",
                pgid,
                pid,
            )
            os.kill(pid, sig)
            return True
        os.killpg(pgid, sig)
        return True
    taskkill_bin = trusted_system_bin("taskkill")
    if taskkill_bin is None:
        raise OSError("taskkill not found in the trusted system directories")
    try:
        r = subprocess.run(
            [taskkill_bin, "/T", "/F", "/PID", str(pid)],
            check=False,
            capture_output=True,
            timeout=5,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"taskkill /T invocation failed: {exc}") from exc
    if r.returncode != 0:
        _raise_taskkill_error(pid, r.returncode, r.stderr or r.stdout)
    return True


def _open_process_query_handle(pid: int) -> int | None:
    """Open a QUERY-ONLY Windows handle to *pid*, or ``None``.

    Terminate rights are deliberately NOT requested: the callers use this handle
    to decide whether a kill may happen at all, so demanding the right to kill in
    order to read the identity would refuse the guard for exactly the processes a
    caller must be most careful about.

    Returns ``None`` on every non-Windows platform, and on any failure -- an
    unopenable process is one whose identity cannot be confirmed, which every
    caller must treat as "do not kill".
    """
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception:
        return None
    return int(handle) if handle else None


def _close_process_handle(handle: int) -> None:
    """Release a handle from :func:`_open_process_query_handle`. Never raises."""
    if not IS_WINDOWS or type(handle) is not int or handle <= 0:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        logger.debug("CloseHandle failed for process handle %d", handle, exc_info=True)


def kill_process_tree_pinned(
    pid: int, expected_start_time: str, sig: int = SIGTERM, *, app_tracking: bool = False
) -> bool:
    """Drain *pid*'s Windows tree only after pinning its creation identity.

    Returns False without signalling when the original object cannot be opened
    or its creation time does not match. On Windows an exited root may still
    anchor surviving descendants; the exact-handle drain verifies their full
    lifetime chains and confirms exit, raising on unknown or incomplete cleanup.
    SIGTERM and SIGKILL both use Windows hard termination, as with taskkill /F.
    POSIX continues to delegate to kill_process_tree unchanged.
    """
    if not IS_WINDOWS:
        return kill_process_tree(pid, sig)
    try:
        key = (pid, int(expected_start_time))
    except (TypeError, ValueError):
        return False
    state = reserve_windows_tree_cleanup(key)
    with state.lock:
        if state.retired:
            return state.root_pid > 0
        if not state.handles:
            try:
                handle = open_process_termination_handle(pid, expected_start_time)
            except BaseException:
                release_windows_tree_reservation(state)
                raise
            if handle is None:
                release_windows_tree_reservation(state)
                return False
            state.root_pid = pid
            state.handles[pid] = handle
        state.retire_app_tracking |= app_tracking
        with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
            _PENDING_WINDOWS_TREE_CLEANUPS[key] = state
    drained, _ = _advance_owned_windows_tree(state)
    return drained


def kill_pid_pinned(pid: int, expected_start_time: str, sig: int = SIGTERM) -> bool:
    """Kill *pid* only while its verified identity is PINNED OPEN.

    Single-process variant of :func:`kill_process_tree_pinned` — same Windows
    guarantee (the query handle that verified the creation time stays open
    across the terminate, so the PID ``taskkill`` resolves cannot have been
    recycled between the check and the signal), delegating to :func:`kill_pid`
    instead of tearing down the tree. Returns ``False`` — without signalling —
    when the handle cannot be opened or the identity does not match; callers
    treat that as "identity unconfirmed, do not kill". On a match it delegates
    to :func:`kill_pid` and propagates its exceptions unchanged.

    POSIX delegates straight through: ``os.kill`` is issued in-process by the
    same interpreter that did the check and there is no handle to hold; the
    residual probe-to-signal window there is the pre-existing one callers
    mitigate by re-confirming identity before destructive escalation.
    """
    if not IS_WINDOWS:
        return kill_pid(pid, sig)
    handle = _open_process_query_handle(pid)
    if handle is None:
        return False
    try:
        identity = _windows_process_handle_identity(handle)
        # (pid, creation_time, exit_time) -- the creation half is the identity.
        if identity is None or str(identity[1]) != expected_start_time:
            return False
        # The handle stays open for the whole call: taskkill resolves the PID
        # while this process object is still referenced, so the PID cannot have
        # been recycled onto a different process in between.
        return kill_pid(pid, sig)
    finally:
        _close_process_handle(handle)


async def kill_pid_async(pid: int, sig: int = SIGTERM) -> bool:
    """Async variant of :func:`kill_pid` — offloads Windows ``taskkill`` off the loop.

    Windows kills spawn a ``taskkill.exe`` subprocess (``subprocess.run`` with a
    5s timeout) — a blocking spawn that stalls the asyncio event loop when
    called from an ``async def`` coroutine. Offload to
    :func:`kiro_crew.executors.subprocess_executor` (the same bounded pool the
    ACP client already uses for its ``ps``/``pgrep`` + ``os.close`` teardown
    work) so the loop keeps running while ``taskkill`` waits for the target to
    exit. POSIX ``os.kill`` is a non-blocking syscall — we still call
    :func:`kill_pid` inline (no executor hop) so the async signature is
    consistent across platforms AND existing test suites that monkeypatch
    :func:`kill_pid` continue to intercept the call. Raises the same exception
    types as :func:`kill_pid` (``ProcessLookupError`` / ``PermissionError`` /
    ``OSError``).
    """
    if IS_POSIX:
        # Inline dispatch to sync kill_pid: POSIX os.kill is non-blocking, and
        # keeping this in-process (rather than a to-thread hop) preserves the
        # exception frame + lets existing tests that patch kill_pid observe it.
        return kill_pid(pid, sig)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), kill_pid, pid, sig)


async def kill_process_tree_async(pid: int, sig: int = SIGTERM) -> bool:
    """Async variant of :func:`kill_process_tree` — offloads Windows ``taskkill /T``.

    See :func:`kill_pid_async` for the offload rationale. POSIX
    ``os.killpg`` is non-blocking so this dispatches inline to
    :func:`kill_process_tree`; the Windows branch spawns ``taskkill /T /F``
    off the loop via :func:`kiro_crew.executors.subprocess_executor`. Raises
    the same exceptions as :func:`kill_process_tree`.
    """
    if IS_POSIX:
        return kill_process_tree(pid, sig)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), kill_process_tree, pid, sig)


#: Ceiling on waiting for a killed process tree. A descendant that ignores the
#: signal must not turn cleanup into a hang while the caller is already
#: handling a timeout or a cancellation — often on the shutdown path, where an
#: unbounded reap would wedge the whole teardown.
REAP_TIMEOUT_SECS: float = 10


def _shares_own_process_group(pid: int) -> bool:
    """True when *pid* runs in the gateway's OWN process group.

    Such a child was spawned without ``start_new_session``, so it has no tree
    of its own to signal — see :func:`kill_and_reap`, whose group kill this
    gates. Fail-closed (``False``) on every probe failure: the pid may be gone
    or unreadable, and the tree kill it guards is itself best-effort and
    protected by :func:`kill_process_tree`'s own broadcast/self-group guard.

    This is a named seam on purpose. The probe reads the LIVE process table,
    so a test handing :func:`kill_and_reap` a synthetic pid was at the mercy
    of whichever real process happened to own that pid: when it landed inside
    the runner's own group the skip fired and the expected tree kill never
    happened. The rootdir ``conftest`` pins this one function instead of the
    shared ``_OWN_PGID`` — pinning that would also disarm
    :func:`kill_process_tree`'s self-group refusal, the guard that keeps a
    test from broadcasting a signal to the whole pytest run.
    """

    if not IS_POSIX:
        return False
    try:
        return os.getpgid(pid) == _OWN_PGID
    except Exception:
        return False


async def kill_and_reap(proc: asyncio.subprocess.Process, *, timeout: float | None = None) -> None:
    """Kill *proc* AND its descendants, then wait for it under a bound.

    The shared cleanup for a PIPE-stdio child whose ``communicate()`` was
    abandoned by ``asyncio.wait_for`` — used on BOTH the timeout and the
    cancellation path. Cancellation matters as much as timeout: a gateway
    shutdown cancels the owning task, and without this the child keeps
    running after the process that started it is gone.

    The whole TREE is signalled, not just the direct child. A spawned command
    is often a shell line (``curl … | sh``, ``pip … | tee log``), so killing
    only the shell leaves the pipeline members running and can leave
    ``communicate()`` waiting on pipes those survivors still hold. A child
    sharing the caller's own process group (spawned without
    ``start_new_session``) has no tree of its own to signal — the group kill
    is skipped for it and the pid-scoped ``kill()`` below covers it, instead
    of tripping :func:`kill_process_tree`'s broadcast guard on every routine
    timeout.

    The reap goes through ``communicate()`` rather than ``wait()`` so the
    pipes are drained: ``wait_for`` already cancelled the original
    ``communicate()``, and a killed child blocked writing into a full pipe
    would make a bare ``wait()`` hang the calling task forever. The reap is
    bounded by *timeout* (default :data:`REAP_TIMEOUT_SECS`). Both the kill
    and the reap are best-effort, since the caller is already handling a
    timeout or a cancellation and must not have it masked by a cleanup error.

    The whole sequence runs in a shielded inner task: a (repeat) cancellation
    of the caller landing mid-cleanup must not abandon the kill or leave the
    child un-reaped — the cancellation is absorbed until cleanup finishes and
    then re-delivered once.
    """

    async def _cleanup() -> None:
        # Bare-name lookup so a test can pin the probe (see
        # ``_shares_own_process_group``) without reaching into ``os``.
        if not _shares_own_process_group(proc.pid):
            # Bare-name lookup resolves through this module's namespace at
            # call time, so tests patching ``kiro_crew.platform_compat.
            # kill_process_tree_async`` still intercept the tree kill.
            with contextlib.suppress(Exception):
                await kill_process_tree_async(proc.pid, SIGKILL)
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(
                proc.communicate(),
                timeout=REAP_TIMEOUT_SECS if timeout is None else timeout,
            )

    cleanup = asyncio.ensure_future(_cleanup())
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            # Either the CALLER was (re-)cancelled while the shield kept the
            # cleanup running, or the cleanup itself ended cancelled. Absorb
            # until the cleanup has genuinely finished, then re-deliver.
            cancelled = True
            if cleanup.done():
                break
    if cancelled:
        raise asyncio.CancelledError


async def descendant_termination_handles_async(
    pid: int,
    retained_handles: Mapping[int, int] | None = None,
    root_handle: int | None = None,
) -> dict[int, int]:
    """Async variant of :func:`descendant_termination_handles`."""

    if not IS_WINDOWS:
        return {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        subprocess_executor(),
        descendant_termination_handles,
        pid,
        dict(retained_handles or {}),
        root_handle,
    )


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


def fchmod_safe(fd: int, mode: int) -> None:
    """Apply ``mode`` to ``fd``. Logs warning on failure.
    No-op on Windows (no POSIX perms).
    """
    if IS_POSIX:
        try:
            os.fchmod(fd, mode)
        except OSError:
            logger.warning("Cannot set permissions on fd %d", fd)


def chmod_safe(path: str | os.PathLike, mode: int) -> None:
    """Apply ``mode`` to ``path``. Logs warning on failure.
    No-op on Windows.
    """
    if IS_POSIX:
        try:
            os.chmod(path, mode)
        except OSError:
            logger.warning("Cannot set permissions on %s", path)


def _clear_readonly_and_retry(func: Any, path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` error hook: drop the read-only bit, then retry once.

    Windows checks the read-only ATTRIBUTE on the file being deleted, whereas
    POSIX consults the parent directory's write bit. So a mode-``444`` file — of
    which a git checkout is full, since loose objects are written read-only —
    cannot be unlinked on Windows even when its directory is writable.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        logger.warning("Cannot remove %s", path)


def rmtree_force(path: str | os.PathLike) -> bool:
    """Remove a directory tree, defeating Windows read-only files. Never raises.

    Returns True when *path* is gone afterwards.

    ``shutil.rmtree(..., ignore_errors=True)`` is the usual spelling and is WRONG
    for any tree that may contain a git checkout: on Windows the read-only loose
    objects under ``.git/objects`` refuse to unlink, ``ignore_errors`` swallows
    every one of those failures, and the caller reports success over a tree that
    is still on disk — so the project name stays taken and the next create
    answers 409.

    The return value is what lets a caller tell a real deletion from a partial
    one; the boolean is derived from the filesystem rather than from the hook,
    because a surviving file is the only thing that actually matters.
    """
    # `onexc` replaced `onerror` in 3.12 and the old name warns; this project
    # still supports 3.9+, so pick by capability rather than by version number.
    kwarg = "onexc" if sys.version_info >= (3, 12) else "onerror"
    try:
        if kwarg == "onerror":  # pragma: no cover - exercised on Python < 3.12

            def _legacy(func: Any, target: str, exc_info: Any) -> None:
                _clear_readonly_and_retry(func, target, exc_info[1])

            shutil.rmtree(path, onerror=_legacy)
        else:
            shutil.rmtree(path, onexc=_clear_readonly_and_retry)  # type: ignore[call-arg]
    except FileNotFoundError:
        # A missing ROOT is success. A nested entry can disappear during rmtree while
        # the root survives, especially through the Python <3.12 onerror path.
        return not os.path.lexists(path)
    except OSError:
        logger.warning("Cannot remove %s", path)
        return False
    return not os.path.lexists(path)


def symlink_or_junction(target: str | os.PathLike, link: str | os.PathLike) -> None:
    """Create a directory link at *link* pointing to *target*.

    POSIX: a plain ``os.symlink``.

    Windows: ``os.symlink`` needs SeCreateSymbolicLinkPrivilege — held only by
    an elevated process or one running with Developer Mode on — so it raises
    ``OSError WinError 1314`` for the ordinary non-admin user, silently breaking
    every feature that links a directory into place (app skills, etc.). A
    directory JUNCTION needs no privilege, is followed transparently by reads
    and by ``os.path.realpath`` / ``Path.resolve()`` (so app-root containment
    checks still hold), and is the standard no-elevation substitute. Fall back
    to it, and only if the symlink attempt fails, so the POSIX-identical path is
    unchanged where symlinks are permitted.

    ``target`` must be an existing directory on Windows (junctions are
    directory-only). Raises if neither a symlink nor a junction can be made.
    """
    if IS_POSIX:
        os.symlink(str(target), str(link))
        return
    try:
        # target_is_directory=True is required on Windows: a directory link made
        # without it is a FILE-type symlink pointing at a directory, which is not
        # traversable. Ignored on POSIX. This helper only ever links directories.
        os.symlink(str(target), str(link), target_is_directory=True)
    except OSError:
        # No symlink privilege (the common non-admin case) — use a junction,
        # which requires none. _winapi.CreateJunction exists on all supported
        # CPython builds on Windows.
        import _winapi

        # _winapi.CreateJunction is Windows-only; typeshed omits it on the POSIX
        # stub, so ignore the attr error mypy raises when checking on Linux.
        _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


# os.path.isjunction is 3.12+; fall back to the reparse-tag check below on the
# 3.10/3.11 interpreters this project still supports, or the junction guard is a
# silent no-op there. Constants mirror CPython's own isjunction.
_ISJUNCTION = getattr(os.path, "isjunction", None)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def _is_junction_fallback(path: str | os.PathLike) -> bool:
    """``os.path.isjunction`` for Python 3.10/3.11, which lack it.

    A junction is a reparse point (``FILE_ATTRIBUTE_REPARSE_POINT``) whose tag is
    ``IO_REPARSE_TAG_MOUNT_POINT``. Both fields are Windows-only additions to
    ``os.stat_result``, so their absence off Windows makes this False — correct,
    since junctions do not exist there. ``follow_symlinks=False``: the question
    is what THIS name is, not what it points at.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError, TypeError):
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    if not attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return getattr(info, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


def strip_extended_length_prefix(path: Path) -> Path:
    r"""*path* without Windows' extended-length prefix; unchanged elsewhere.

    ``Path.resolve()`` on a file that another thread is replacing at that exact
    moment comes back as ``\\?\C:\...``: ``ntpath.realpath`` drops the prefix
    only after re-checking the stripped spelling, and that re-check fails when
    the file has just been swapped out. The directory resolved separately comes
    back plain, so a containment comparison reads the prefix alone as an escape.

    The fold is LEXICAL and must stay that way: resolving again here could bless
    a redirect, which is the thing the caller is trying to detect. Both
    extended spellings are handled -- ``\\?\UNC\host\share`` becomes the
    ordinary ``\\host\share`` -- so both sides of a comparison are spelled the
    same way whatever ``realpath`` returned.
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return type(path)("\\\\" + text[8:])
    if text.startswith("\\\\?\\"):
        return type(path)(text[4:])
    return path


def is_link_or_junction(path: str | os.PathLike) -> bool:
    """True if *path* is a symlink OR (on Windows) a directory junction.

    ``os.path.islink`` returns False for a junction, so a caller that only
    checks ``islink`` would treat a junction as a real directory and
    ``rmtree`` THROUGH it, destroying the target's contents. Pair with
    :func:`unlink_link_or_junction` to remove one safely.
    """
    if os.path.islink(path):
        return True
    if _ISJUNCTION is not None:
        try:
            return bool(_ISJUNCTION(path))
        except (OSError, ValueError):
            return False
    return _is_junction_fallback(path)


def first_linked_ancestor(path: str | os.PathLike) -> str | None:
    r"""First ANCESTOR of *path* that is a symlink/junction, or None.

    :func:`is_link_or_junction` tests one path, so a caller that checks only
    the path it was handed still resolves through a linked PARENT. That gap is
    not cosmetic on Windows: an ancestor link whose target is ``\\host\share``
    turns the first innocent-looking ``is_dir()`` on a LOCAL-looking path into
    an outbound SMB connection that authenticates as this process. A lexical
    UNC screen cannot catch it, because the path being probed is not itself
    UNC-shaped -- only the link's target is.

    Ancestors are tested ROOT-FIRST and the walk stops at the first hit. That
    order is the safety property, not a detail: each ``lstat`` runs only after
    every ancestor above it is known not to be a link, so the probe itself
    never traverses one. Returns the offending ancestor for logging; callers
    deciding whether to REJECT should not put it in a user-facing message,
    since which ancestor is a link is filesystem layout the caller supplied a
    path to guess at.

    The leaf is deliberately excluded -- pair this with
    :func:`is_link_or_junction` on the path itself.
    """
    for ancestor in reversed(pathlib.Path(os.fspath(path)).parents):
        if is_link_or_junction(ancestor):
            return str(ancestor)
    return None


def unlink_link_or_junction(path: str | os.PathLike) -> None:
    """Remove a symlink or directory junction WITHOUT touching its target.

    A symlink is removed with ``unlink``; a Windows junction is a directory
    reparse point removed with ``rmdir`` (which unlinks the junction itself,
    never the target it points at).
    """
    if os.path.islink(path):
        os.unlink(path)
        return
    is_junction = _ISJUNCTION(path) if _ISJUNCTION is not None else _is_junction_fallback(path)
    if is_junction:
        os.rmdir(path)
        return
    # Neither — let the caller's own logic handle a real file/dir.
    os.unlink(path)


#: ``CreateFileW`` arguments for :func:`pin_directory`. ``BACKUP_SEMANTICS`` is
#: what lets a directory be opened at all; ``OPEN_REPARSE_POINT`` opens the
#: reparse point ITSELF instead of following it, so a junction planted at the
#: name is seen for what it is rather than silently traversed. The share mode
#: deliberately omits ``FILE_SHARE_DELETE``: that omission is the pin.
_WIN_GENERIC_READ = 0x80000000
_WIN_FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
_WIN_OPEN_EXISTING = 3
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WIN_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WIN_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


def pin_directory(path: str | os.PathLike) -> int:
    """Open *path* as a directory and return a descriptor that PINS it.

    For code that must let a child process write to ``<dir>/<name>`` by path:
    the string is re-resolved at the child's open, so a same-UID watcher that
    renames ``<dir>`` away and plants a link at its name between our check and
    that open redirects the write. Holding the directory open closes the
    window differently on each platform:

    * Windows: the handle is opened without ``FILE_SHARE_DELETE``, and a
      directory with such a handle open can be neither renamed nor deleted --
      nor can any directory above it -- for as long as the handle lives. The
      open itself refuses to follow a reparse point, so a junction already
      sitting at the name fails here instead of being pinned in its target's
      place.
    * POSIX: ``O_DIRECTORY | O_NOFOLLOW`` refuses a symlink or a file at the
      name, and the returned descriptor is usable as ``dir_fd`` so the caller's
      own opens resolve against the directory it inspected. Holding it does not
      block a rename (POSIX has no such lock); callers rely on the sandbox mask
      for that and use the descriptor for their own opens.

    Refuses anything that is not a real directory: a file or a Windows reparse
    point raises ``NotADirectoryError``; a POSIX symlink fails with whichever
    of ``ENOTDIR`` / ``ELOOP`` the kernel reports for ``O_DIRECTORY |
    O_NOFOLLOW``. Release with ``os.close``.
    """
    if IS_POSIX:
        return os.open(
            os.fspath(path),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )

    fd = _win_open_without_following(path)
    try:
        attrs = getattr(os.fstat(fd), "st_file_attributes", 0)
        if not attrs & _WIN_FILE_ATTRIBUTE_DIRECTORY or attrs & _WIN_FILE_ATTRIBUTE_REPARSE_POINT:
            raise NotADirectoryError(errno.ENOTDIR, "not a real directory", os.fspath(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _win_open_without_following(path: str | os.PathLike) -> int:
    """``CreateFileW`` *path* for reading, opening a reparse point INSTEAD of following it.

    Shared by :func:`pin_directory` and :func:`open_file_no_reparse` so the two do
    not carry separate copies of the same security-critical flags. What each of
    them then asserts about the descriptor differs; how the object is reached must
    not.

    ``OPEN_REPARSE_POINT`` is the whole point: a junction or symlink at the name is
    opened AS ITSELF, so the caller sees what is really there and the target is
    never touched. That is what makes the refusal atomic rather than a check
    followed by an open -- and on Windows the difference is not academic, because
    resolving a reparse point aimed at a UNC share is itself an outbound SMB
    authentication. ``BACKUP_SEMANTICS`` is what allows a directory to be opened at
    all and is harmless on a file. The share mode omits ``FILE_SHARE_DELETE``: while
    this descriptor lives, the object cannot be renamed or deleted -- and for a
    directory, neither can anything above it.

    The handle is wrapped in a CRT descriptor so ``os.fstat`` can read the
    attributes of what was actually opened and ``os.close`` can release it.

    ``O_BINARY`` is part of that wrapping, not a detail. A CRT descriptor in TEXT
    mode translates CRLF and stops at the first ``0x1A``, and a caller reading
    with a raw ``os.read`` gets that translation: on a body of 55 bytes holding
    one ``0x1A``, such a descriptor yields 12. ``os.fdopen(fd, "rb")`` hides the
    difference because ``io.FileIO`` sets the mode itself, so only a raw-read
    caller is exposed -- which is precisely the caller that copies media files.
    Naming the flag here makes the descriptor's contract the same for both.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        os.fspath(path),
        _WIN_GENERIC_READ,
        _WIN_FILE_SHARE_READ_WRITE,
        None,
        _WIN_OPEN_EXISTING,
        _WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    return msvcrt.open_osfhandle(  # type: ignore[attr-defined]
        handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
    )


def open_file_no_reparse(path: str | os.PathLike, *, nonblocking: bool = False) -> int:
    """Open a regular FILE for reading, refusing a reparse point at the final name.

    The leaf counterpart to :func:`pin_directory`. ``pin_directory`` freezes the
    ancestors so the path cannot be re-pointed underneath a caller; this one settles
    the last component, and it has to do so in the SAME operation that opens it --
    an ``is_symlink()`` (or ``lstat``) check followed by ``os.open`` is a
    check-to-open window, and an adversary that can plant the link chooses when.

    * POSIX: ``O_NOFOLLOW`` already refuses a link at the final component; this is
      exactly the open the callers were doing, named.
    * Windows: there is no ``O_NOFOLLOW`` -- ``getattr(os, "O_NOFOLLOW", 0)`` is 0,
      so ``os.open`` FOLLOWS a reparse point at the name. ``CreateFileW`` with
      ``FILE_FLAG_OPEN_REPARSE_POINT`` opens the reparse point itself instead, and
      the attribute read off the resulting descriptor is therefore a fact about what
      was opened, not a prediction about what a later open will find. Refused with
      ``ELOOP``, matching what POSIX reports for the same shape.

    Refuses a directory with ``IsADirectoryError`` (POSIX reports ``EISDIR`` from the
    read, Windows from ``os.open``; the two are made to agree here). Release the
    descriptor with ``os.close``.

    ``nonblocking`` adds ``O_NONBLOCK`` on POSIX so a caller can reject a FIFO
    with ``fstat`` before an open waits for a writer. Regular file reads are
    unaffected. Windows has no POSIX FIFO open; its handle checks stay the same.
    """
    if IS_POSIX:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if nonblocking:
            flags |= getattr(os, "O_NONBLOCK", 0)
        return os.open(os.fspath(path), flags)

    fd = _win_open_without_following(path)
    try:
        attrs = getattr(os.fstat(fd), "st_file_attributes", 0)
        if attrs & _WIN_FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno.ELOOP, "reparse point at the final component", os.fspath(path))
        if attrs & _WIN_FILE_ATTRIBUTE_DIRECTORY:
            raise IsADirectoryError(errno.EISDIR, "is a directory", os.fspath(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


_WIN_FILE_SHARE_READ_WRITE_DELETE = 0x00000001 | 0x00000002 | 0x00000004


def open_log_file_for_tail(path: str | os.PathLike) -> int:
    """Open a binary read fd without blocking the log writer's rename/rotation.

    Windows CRT opens omit FILE_SHARE_DELETE. This separate log-only helper
    permits rotation even during a read; security pinning helpers must not.
    The caller owns the returned descriptor and must close it.
    """
    if IS_POSIX:
        return os.open(os.fspath(path), os.O_RDONLY)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        os.fspath(path),
        _WIN_GENERIC_READ,
        _WIN_FILE_SHARE_READ_WRITE_DELETE,
        None,
        _WIN_OPEN_EXISTING,
        0,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        return msvcrt.open_osfhandle(  # type: ignore[attr-defined]
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        # Ownership transfers only when the CRT descriptor is created.
        kernel32.CloseHandle(handle)
        raise


# Well-known SID for the file's *owner* (implicit). Under a self-relative DACL
# with inheritance stripped, S-1-3-4 grants access to whoever currently owns
# the file. See:
# https://learn.microsoft.com/en-us/windows/win32/secauthz/well-known-sids
_OWNER_RIGHTS_SID = "S-1-3-4"


_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_token_sid() -> str | None:
    """The invoking user's SID read from this process's own access token.

    Preferred over ``whoami`` because it spawns nothing: the SID is already in
    the process token, so this cannot time out under load, cannot be defeated
    by a stripped PATH or a locked-down host, and is safe to call on the event
    loop. Returns ``None`` on any failure so the caller can fall back.
    """
    if IS_POSIX:
        return None
    try:
        return _process_token_sid_unguarded()
    except Exception:  # noqa: BLE001 - best-effort: the caller falls back
        logger.debug("_process_token_sid failed", exc_info=True)
        return None


def _process_token_sid_unguarded(pid: int | None = None) -> str | None:
    """Body of :func:`_process_token_sid`; may raise.

    ``pid`` selects whose token to read: ``None`` means this process (via the
    ``GetCurrentProcess`` pseudo-handle), any other value opens that process
    with ``PROCESS_QUERY_LIMITED_INFORMATION`` -- the least right that still
    permits ``OpenProcessToken``, and one a user always holds over their own
    processes without elevation.
    """

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        # AttributeError: ctypes has no WinDLL off Windows. Reachable because
        # tests exercise the Windows branch from Linux by patching IS_POSIX.
        return None

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    # Every prototype is declared and every argument below is passed as a
    # ctypes instance rather than a Python int. Leaving either to the default
    # lets ctypes convert a pointer-sized value through a C int, which either
    # truncates it silently or raises OverflowError depending on the call --
    # both observed on Windows, neither reproducible on Linux.
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    # Hand the pseudo-handle over as a HANDLE instance rather than the int
    # ctypes hands back for a c_void_p restype: converting that int through the
    # declared argtype raises OverflowError on Windows because the value is
    # pointer-sized and unsigned.
    #
    # own_handle tracks whether this is a real handle we must close. The
    # GetCurrentProcess pseudo-handle must NOT be closed.
    own_handle = pid is not None
    if pid is None:
        process = wintypes.HANDLE(kernel32.GetCurrentProcess())
    else:
        process = wintypes.HANDLE(
            kernel32.OpenProcess(
                wintypes.DWORD(_PROCESS_QUERY_LIMITED_INFORMATION),
                wintypes.BOOL(False),
                wintypes.DWORD(int(pid)),
            )
        )
        if not process.value:
            return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            process, wintypes.DWORD(_TOKEN_QUERY), ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD()
            # First call sizes the buffer and is expected to fail.
            advapi32.GetTokenInformation(
                token,
                ctypes.c_int(_TOKEN_USER_CLASS),
                None,
                wintypes.DWORD(0),
                ctypes.byref(size),
            )
            if size.value == 0:
                return None
            buf = (ctypes.c_byte * size.value)()
            if not advapi32.GetTokenInformation(
                token,
                ctypes.c_int(_TOKEN_USER_CLASS),
                ctypes.cast(buf, ctypes.c_void_p),
                size,
                ctypes.byref(size),
            ):
                return None
            user = ctypes.cast(buf, ctypes.POINTER(_TokenUser)).contents
            out = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(user.User.Sid), ctypes.byref(out)
            ):
                return None
            try:
                sid = out.value
            finally:
                kernel32.LocalFree(out)
        finally:
            kernel32.CloseHandle(token)
    finally:
        if own_handle:
            kernel32.CloseHandle(process)
    if not sid or not sid.startswith("S-1-"):
        return None
    return sid


def process_owner_sid(pid: int) -> str | None:
    """The SID of the user owning *pid*, as a string, or ``None``.

    Windows' answer to :func:`process_owner_uid`, which returns ``None`` there
    because Windows has no uid. Reads the target process's access token
    directly -- no subprocess, no WMI round trip -- so it is safe to call on the
    event loop.

    This is what lets a Windows peer-principal check work at *connect* time.
    The obvious alternative, ``ImpersonateNamedPipeClient``, cannot:
    per Microsoft's documentation it impersonates "the security context of the
    last message read from the pipe", so before the first read there is no
    context to adopt and the call fails (or yields an anonymous token that
    ``OpenThreadToken`` then refuses). Reading the peer process's own token has
    no such ordering requirement, and it never borrows the peer's token onto one
    of our threads.

    PID reuse is not exploitable here. The window between learning the peer PID
    and opening it is tiny, and either outcome is safe: if the PID has been
    recycled to a process owned by *another* user the comparison reports a
    mismatch and the caller denies; if it was recycled to another process owned
    by *us* then the principal genuinely is us, which is the only question this
    function answers. Ownership -- not process identity -- is the assertion.

    Returns ``None`` on POSIX and on any failure, so callers must treat ``None``
    as "unverifiable" rather than as a match.
    """
    if IS_POSIX:
        return None
    try:
        return _process_token_sid_unguarded(int(pid))
    except Exception:  # noqa: BLE001 - best-effort: the caller fails closed
        logger.debug("process_owner_sid(%s) failed", pid, exc_info=True)
        return None


#: Memo for :func:`current_user_sid`. Only ever holds a token-derived bare SID.
_TOKEN_SID_CACHE: list[str] = []


def current_user_sid() -> str | None:
    """Return the invoking user's bare SID (``S-1-5-...``), or ``None``.

    Read from this process's own access token and nothing else -- there is no
    subprocess fallback anywhere in this path. Every caller runs on the event
    loop: the gatewayd admission check, the client-side server check, the pipe
    DACL builder (once per pipe instance, so on the accept path), and now the
    owner-only lockdown itself. A ``whoami`` fallback would stall any of them for
    seconds at a time on a host where the token read fails, which is why this
    function refuses instead.

    Failing closed is correct for every caller: each treats ``None`` as
    "principal unverifiable" and refuses the connection, which degrades to a
    per-session MCP server rather than admitting an unattributable peer.
    :func:`restrict_to_owner` likewise raises rather than applying a
    half-configured DACL.

    SDDL and the Win32 security APIs want the bare SID, which is what this
    returns. Memoised: the SID is constant for the process lifetime and this is
    on a hot path. Returns ``None`` on POSIX and on any lookup failure.
    """
    if _TOKEN_SID_CACHE:
        return _TOKEN_SID_CACHE[0]
    raw = _process_token_sid()
    if not raw:
        return None
    sid = raw.lstrip("*") or None
    if sid:
        _TOKEN_SID_CACHE.append(sid)
    return sid


def make_owner_only_dir(path: str | os.PathLike) -> None:
    """Create *path* (with parents) and make it readable only by this user.

    ``mkdir(mode=...)`` alone is not enough on either platform: POSIX masks the
    mode with the umask and ignores it entirely for a directory that already
    exists, and Windows derives access from the DACL rather than the mode bits,
    so the mode argument is inert there. Both cases matter for the same reason --
    a directory created before the owner-only guarantee existed, or created on
    Windows at all, would silently stay readable.

    ``0o700`` and not :func:`restrict_to_owner` on POSIX: that helper applies
    ``0o600``, correct for a secret-bearing file and wrong for a directory,
    which needs the execute bit to be traversable at all. On Windows the split
    is the inverse of inert: ``restrict_to_owner``'s grants are not inheritable
    (correct for a file, where the flags mean nothing), so routing a directory
    through it left every file created inside on the creating token's default
    DACL. Both platforms therefore go through
    :func:`restrict_dir_to_owner`, the directory-shaped twin.

    Only newly created children are covered. A file that already exists inside
    the directory keeps its own DACL — see :func:`restrict_dir_to_owner` for
    why a tightened parent does not fix one.

    Best-effort on the tightening step: the directory is still created, and the
    caller decides whether an un-tightened directory is fatal.
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        restrict_dir_to_owner(p)
    except OSError:
        logger.warning("could not restrict directory %s to owner-only", p, exc_info=True)


def local_user_id() -> int:
    """A stable integer identifying the invoking user on this host.

    POSIX: ``os.getuid()``. Windows has no uid, so this is a CRC-32 of the
    user's SID -- an arbitrary but stable and collision-resistant-enough
    integer for the one thing the value is used for: partitioning a cache or a
    pool so two different users can never share an entry.

    An integer rather than the SID string because the consumer
    (``mcp_gateway.PoolKey``) type-checks this dimension strictly and refuses to
    coerce -- ``bool("false")`` is ``True`` and ``int()`` on a bool passes
    silently, so a wire value of the wrong type could land a peer in the wrong
    trust partition. Keeping the type identical across platforms keeps that
    check meaningful.

    Returns ``0`` when the SID cannot be resolved. That is a partition
    collapse, not a privilege change: the gateway's endpoint is already
    per-user (owner-only DACL) and its daemon runs per user, so two users
    cannot reach the same pool regardless.
    """
    if IS_POSIX:
        return os.getuid()
    sid = current_user_sid()
    if not sid:
        return 0
    return zlib.crc32(sid.encode("utf-8"))


def stat_writable_by_current_user(st: os.stat_result) -> bool:
    """Could a process running as this account write the file *st* describes?

    Answered from the mode bits of an ALREADY-STATTED object rather than a path, so a
    caller that has an open handle gets an answer about the object it actually read
    instead of about whatever the name resolves to a moment later.

    Used to refuse an agent-writable governance source
    (``platform/policy_distribution.py``): a distribution source the account Kiro Crew
    runs as can rewrite is one an agent subprocess can rewrite, because it runs as the
    same uid.

    **POSIX only, and off POSIX it answers ``True`` — unknown counts as writable.**
    Windows permissions are an ACL, not three mode triples, so ``st_mode``'s group/other
    bits carry no usable answer there and ``st_uid``/``st_gid`` are synthesised. The
    honest answer is "cannot tell", and for this predicate that has to round to
    ``True``: the caller refuses a writable source, so a ``False`` here does not abstain,
    it ASSERTS the source is safe and admits every Windows ``file://`` source unchecked —
    including one an agent just planted. ``True`` costs a Windows operator the
    ``file://`` channel (``https://`` is unaffected) until the DACL can be read, which
    belongs with the other ``_icacls`` work rather than here. It is also the safe
    direction for a future caller who inverts the test to decide where to WRITE.

    Lives in this module because ``os.getuid`` / ``os.getgroups`` do not exist on
    Windows, and the POSIX shims are this module's job.
    """
    if not IS_POSIX:
        return True
    # Root writes anything, whatever the mode says. Checked first because for a
    # privileged process every remaining test below is moot.
    if 0 in (os.getuid(), os.geteuid()):
        return True
    if st.st_mode & stat.S_IWOTH:
        return True
    # OWNERSHIP alone, not the write bit. An owner may `chmod` its own file, so a `0444`
    # file this account owns is one this account can make writable and then rewrite —
    # which is the whole move the threat model describes, since the agent subprocess runs
    # as the same uid. Requiring `S_IWUSR` here accepted exactly the source an agent could
    # take over with one `chmod`. The same reasoning covers a directory in the ancestor
    # walk: owning it means being able to unlink and recreate what is inside.
    #
    # Real AND effective. The kernel checks the effective pair, but this predicate answers
    # "could this account write it", and a process holding a real id can regain it.
    if st.st_uid in (os.getuid(), os.geteuid()):
        return True
    # Group ownership does NOT imply the same power — only the owner and root may chmod —
    # so here the write bit is the question, and `os.getgroups()` is the SUPPLEMENTARY
    # list: POSIX leaves it unspecified whether the effective gid appears in it. It
    # usually does, because `initgroups` puts it there at login, but a process that
    # reached its gid through `setegid`, or one in a container built without that step,
    # has a primary group the supplementary list never mentions. Testing membership alone
    # therefore called a group-writable file we CAN replace safe.
    gids = {os.getgid(), os.getegid(), *os.getgroups()}
    if st.st_mode & stat.S_IWGRP and st.st_gid in gids:
        return True
    return False


#: Whether ``os.access`` accepts ``effective_ids`` here — it needs ``faccessat``. Resolved
#: once, at import: it is a property of the platform, not of a call, and computing it from
#: ``os.access``'s own identity per call would silently disable the ACL arm for any caller
#: that had substituted the function.
_ACCESS_HONOURS_EFFECTIVE_IDS = os.access in os.supports_effective_ids


def path_writable_by_current_user(path: str | os.PathLike) -> bool:
    """Could this account replace the file at *path* — by any route?

    Checks the file AND every ancestor directory, because file mode alone is the wrong
    question: a ``0444`` file inside a directory this account can write is replaceable
    (unlink and recreate, or rename the parent aside), so an agent could publish a
    read-only file of its own choosing and pass a leaf-only check.

    Walks upward to the root and stops at the first writable component, so the answer is
    "there exists a way in" rather than "the leaf looks fine". Two chains are walked, the path
    as WRITTEN and the path as RESOLVED, because a source reached through a symlink is
    re-pointable by anyone who can write the LINK's parent — which the resolved chain never
    visits. A component that cannot be
    statted is skipped rather than treated as writable: an unreadable ancestor is not
    evidence of write access, and failing closed on it would refuse legitimate sources
    under directories this account cannot enumerate.

    Each component is tested TWICE: against the mode bits, and against the kernel's own
    answer via ``os.access(..., effective_ids=True)``. The second is not redundant, it is
    the only one that sees a **POSIX ACL**. A named-user entry (``user:me:w`` on a file
    owned by someone else) does not appear in ``st_mode`` at all — the group bits show the
    ACL *mask*, not that entry — so a mode-only check reports "not writable" for a source
    this account can in fact rewrite, which is the whole failure this predicate exists to
    catch. ``faccessat(AT_EACCESS)`` evaluates the full ACL, so it answers correctly. The
    two are OR'd because each sees something the other cannot: ``os.access`` answers about
    the *effective* ids only, while the mode check also covers the real pair.

    POSIX only, and ``True`` off POSIX, for the reason
    :func:`stat_writable_by_current_user` gives: a source whose write permissions cannot
    be read is not a source that has been shown to be safe.
    """
    if not IS_POSIX:
        return True
    # BOTH chains: the path as WRITTEN and the path as RESOLVED. Resolving first and walking
    # only the target was the gap — a source reached through a symlink was judged entirely by
    # the (root-owned, read-only) file at the end of it, while the link itself sat in a
    # directory this account could write. Re-pointing a symlink needs no permission on the
    # link and none on the target: it needs write on the link's PARENT, which only the lexical
    # chain visits. A symlink's own mode bits are meaningless on Linux (0777 and ignored), so
    # nothing is judged by them; what matters is the directories, and both chains contribute
    # some the other does not.
    starts = [Path(path).absolute()]
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = starts[0]
    if resolved != starts[0]:
        starts.append(resolved)
    seen: set[Path] = set()
    for start in starts:
        current = start
        while current not in seen:
            seen.add(current)
            try:
                if stat_writable_by_current_user(os.stat(current)):
                    return True
                if _ACCESS_HONOURS_EFFECTIVE_IDS and os.access(
                    current, os.W_OK, effective_ids=True
                ):
                    return True
            except OSError:
                pass
            if current.parent == current:
                break
            current = current.parent
    return False


def restrict_to_owner(path: str | os.PathLike) -> None:
    """Fail-loud owner-only lockdown of a secret-bearing file.

    POSIX: ``os.chmod(path, 0o600)`` — identical semantics to a raw call,
    including the fail-loud ``OSError`` propagation the security-sensitive
    callers rely on to reach their warn-and-continue handlers.

    Windows: strip inheritance and apply an owner-only DACL in-process via
    :func:`windows_acl.apply_owner_only`. S-1-3-4 (Owner Rights) covers the
    file's current owner; the invoking-user grant covers the caller by explicit
    SID, so a file created by another principal (elevated first-run, backup
    restore, SYSTEM-context service) remains readable by the caller that is
    trying to lock it down — otherwise the current user would be denied their
    own token signing key on the next read and every issued auth cookie /
    refresh token would be invalidated on each restart. When the SID cannot be
    resolved from the process token we raise ``OSError`` BEFORE applying
    anything: an Owner-Rights-only DACL would recreate the exact
    ownership-lockout regression the dual grant exists to prevent, so we
    refuse to apply a half-configured lockdown. Any failure raises
    ``OSError`` so callers hit the same warn-and-continue path they use on
    POSIX.

    A caller that runs INLINE ON THE ASYNCIO EVENT LOOP and so cannot afford the
    unbounded SMB round-trip a DACL write to a UNC or mapped-drive path costs must
    ask :func:`windows_acl.volume_is_local` FIRST and skip the lockdown itself.
    That decision is deliberately not a parameter here: by the time this function
    is reached the caller has already done whatever filesystem work it took to get
    here, so a refusal at this depth would come after the cost it was meant to
    avoid. ``config/loader.py``'s ``write_config_atomically`` is the one such
    caller today.
    """
    if IS_POSIX:
        os.chmod(path, 0o600)
        return
    # Misuse guard: this helper is FILE-shaped. Its grants carry no (OI)(CI),
    # so handing it a directory tightens the directory itself and leaves every
    # file created inside on the creating token's default DACL -- the exact
    # defect restrict_dir_to_owner exists to close. Warn rather than raise:
    # the ACE still applies to the named object, so the lockdown is partial
    # rather than absent, and turning a partial protection into a runtime
    # OSError would be the worse outcome. The argv tests cannot see this from
    # the call site, so the check lives here.
    try:
        if Path(path).is_dir():
            # The path is deliberately NOT logged. In this codebase a path can
            # itself be the secret -- mcp_gateway/apps.py notes that its spool
            # FILENAMES are live capability tokens -- so naming it here would be
            # clear-text logging of sensitive information (CodeQL flagged exactly
            # that). logging already records module/function/lineno, which is
            # what locates the offending caller.
            logger.warning(
                "restrict_to_owner was called on a directory; its grants are not "
                "inheritable, so files created inside will not be owner-only. "
                "Use restrict_dir_to_owner for a directory."
            )
    except OSError:
        pass
    _apply_owner_only_dacl(path, inherit=False)


def restrict_dir_to_owner(path: str | os.PathLike) -> None:
    """Fail-loud owner-only lockdown of a DIRECTORY, inherited by its children.

    The directory twin of :func:`restrict_to_owner`, and separate from it
    because the two shapes genuinely differ on both platforms:

    POSIX: ``0o700`` rather than ``0o600`` — a directory needs the execute bit
    to be traversable at all, so the file helper's mode would make the
    directory useless.

    Windows: the grants carry ``(OI)(CI)`` so they propagate to files and
    subdirectories created inside. ``restrict_to_owner``'s grants deliberately
    do not, because those flags are meaningless on a file; applying the
    file-shaped helper to a directory is what left every file created inside an
    "owner-only" directory on the creating token's default DACL.

    Note the limit: inheritance governs what gets CREATED from here on. A file
    that already exists inside the directory keeps its own DACL, and Windows
    grants *Bypass Traverse Checking* to Everyone by default, so a permissive
    pre-existing file stays reachable through a tightened parent. Repairing an
    existing install needs a per-file pass over the known names; this helper is
    the guarantee for new files, not a retrofit.

    Fail-loud like :func:`restrict_to_owner`: any failure raises ``OSError`` so
    callers reach their warn-and-continue handlers.

    On Windows the DACL is applied only when the directory's own descriptor does
    not already match, because that write is an O(descendants) propagation (see
    :func:`_apply_owner_only_dacl`). RECORDED DECISION: two states leave the
    directory itself correct while a descendant does not, and neither is
    detectable without the walk this avoids -- a propagation interrupted part way
    through, and a child moved in on the same NTFS volume, since a move preserves
    the child's ACL rather than inheriting the destination's. Measured on a local
    NTFS volume, the propagation DOES rewrite such a child's INHERITED entries, so
    skipping the write also stops healing a moved-in child whose foreign grant was
    inherited at its source; a grant written EXPLICITLY on the child survives the
    propagation and was never healed by it. Repairing either is a non-goal here:
    it would cost the propagation on every launch, and a repair path that can
    afford it belongs with the caller that needs one.
    """
    if IS_POSIX:
        # Semgrep's insecure-file-permissions rule reads 0o700 as "widely
        # permissive" and recommends 0o644, which is backwards for a DIRECTORY
        # holding secrets: 0o644 drops owner-execute (making the directory
        # untraversable) and ADDS world-read -- the exact exposure this helper
        # exists to close. 0o700 is the restrictive mode here, so the finding is
        # suppressed on the line below. Same reasoning as cloud/launch_job.py.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
        os.chmod(path, 0o700)
        return
    _apply_owner_only_dacl(path, inherit=True)


def path_volume_is_remote(path: str | os.PathLike) -> bool | None:
    """Is *path* on a NETWORK volume? True, False for local, None for unknown.

    The Windows half of "which kind of filesystem holds this file", for a caller
    that already answers the question from ``/proc/mounts`` or ``statfs``
    elsewhere: Windows exposes no mount table, so the volume ROOT's drive type
    is the source (:func:`windows_acl.volume_is_remote`, ``GetDriveTypeW``),
    which reports a UNC root and a mapped network drive alike as remote.

    None -- never False -- for every case where nothing was established: off
    Windows, where the caller has its own mount-table source and must not read
    this as "local"; a volume the OS reports as ``DRIVE_UNKNOWN`` or
    ``DRIVE_NO_ROOT_DIR``; and a failed query. Root-only, so it costs no SMB
    round trip and is safe for a path that does not exist yet.
    """
    if not IS_WINDOWS:
        return None
    try:
        return windows_acl.volume_is_remote(path)
    except (windows_acl.AclUnavailable, OSError, ValueError):
        logger.debug("could not classify the volume holding a path", exc_info=True)
        return None


def _apply_owner_only_dacl(path: str | os.PathLike, *, inherit: bool) -> None:
    """Apply an owner-only DACL to *path* in-process. Windows-only.

    Shared by :func:`restrict_to_owner` (``inherit=False``, file shape) and
    :func:`restrict_dir_to_owner` (``inherit=True``, directory shape). The only
    difference between the two is whether the grants are inheritable, so they are
    one function: an owner-only DACL that two call paths could drift apart on is
    the defect this consolidation exists to prevent.

    The descriptor is built through ``advapi32`` directly rather than by shelling
    out to ``icacls /inheritance:r /grant:r ...``, so no "must not run on the
    event loop" constraint falls on the callers: measured on a local NTFS
    volume, the subprocess costs 313 ms per call and this costs 0.24 ms. Callers
    that already offload it are still free to -- a filesystem call can block on
    a slow volume, so offloading remains good practice -- but it is not
    mandatory, and a caller on the loop does not park the gateway for a third of
    a second per secret written.

    That 0.24 ms is PER OBJECT, and with ``inherit=True`` the write reaches more
    than one: Windows propagates an inheritable ACE to every descendant, so on a
    directory the call is O(descendants). Measured on a local NTFS volume: 86 ms
    on a 337-object tree and **2.94 s on a 12358-object one**, linear at 0.238 ms
    per object. Quote the per-object figure as the cost of a directory call and it
    understates that by four orders of magnitude, which is what the probe below
    exists to bound.

    Resolve the invoking user's SID BEFORE writing anything, and resolve it
    WITHOUT the possibility of a spawn: :func:`current_user_sid` reads the
    process's own access token and nothing else. There is deliberately no
    ``whoami`` fallback -- it would put a blocking spawn back on the event loop
    on any host where the token read fails, defeating the whole point of not
    shelling out.

    If the SID cannot be resolved we CANNOT safely apply the DACL: an
    Owner-Rights-only descriptor (S-1-3-4 alone) would lock the current user out
    of their own file whenever the file was created by a different principal
    (elevated first-run, SYSTEM-context service, backup-restored tarball
    preserving foreign ownership -- the exact scenarios the dual grant exists to
    prevent). Fail loud with ``OSError``, the same shape callers already handle,
    so the security-warning path fires instead of silently re-introducing the
    ownership-lockout regression. Note the consequence of the token-only rule:
    on a host whose token read fails we refuse rather than spawning
    ``whoami``. That is the safe direction -- a caller that must not fail passes
    ``restrict_on_error="warn"`` and gets a warning instead of a stall.
    """
    user_sid = current_user_sid()
    if user_sid is None:
        raise OSError(
            f"{'restrict_dir_to_owner' if inherit else 'restrict_to_owner'}: "
            "cannot resolve current user SID from this process's access token; "
            "refusing to apply Owner-Rights-only DACL (would lock non-owner "
            f"users out of {path!s} — see current_user_sid docstring)."
        )
    sids = (_OWNER_RIGHTS_SID,) if user_sid == _OWNER_RIGHTS_SID else (_OWNER_RIGHTS_SID, user_sid)
    try:
        # Probe before writing. On a DIRECTORY the write carries inheritable ACEs,
        # which makes Windows propagate them to every descendant, so the cost is
        # O(descendants) -- measured 0.238 ms per object, i.e. 2.94 s on a
        # 12358-object data home. Reading this object's own descriptor is O(1), so
        # an unchanged DACL costs a constant check instead of a full re-propagation
        # on every boot. `vector_memory.init()` applies this to the whole data home
        # on every gateway start, so that saving is paid on every launch.
        #
        # TWO STATES THE PROBE CANNOT SEE, both of which leave the parent matching
        # while a descendant does not:
        #   1. A propagation interrupted part way (the process dies inside
        #      SetNamedSecurityInfoW). The parent is committed, some children are
        #      not, and the parent alone reads as correct from then on.
        #   2. A child MOVED in on the same NTFS volume. A move preserves the
        #      child's own ACL rather than inheriting the destination's. Measured:
        #      the propagation DOES rewrite that child's INHERITED entries, so
        #      skipping it also stops healing a moved-in child whose foreign grant
        #      was inherited at its source (Everyone:(I)(R) -> replaced by the
        #      parent's grants). A grant written EXPLICITLY on the child
        #      (Everyone:(R)) survives the propagation and was never healed by it.
        # RECORDED DECISION: neither is repaired here. Detecting either one needs
        # the descendant walk this removes, and the alternative is an
        # O(descendants) write on every launch. A caller that must repair a drifted
        # subtree needs a maintenance path of its own; this is the boot path.
        #
        # The probe answers False on any doubt (read failure, NULL or unprotected
        # DACL, unexpected ACE shape), so a wrong answer costs a redundant write
        # rather than a skipped lockdown. It is wrapped anyway: the fallback must be
        # "write", and a probe that somehow raises must not be able to turn a
        # lockdown into an exception on a path that would otherwise be fixed.
        try:
            already_locked = windows_acl.owner_only_dacl_matches(path, inherit=inherit, sids=sids)
        except Exception:
            already_locked = False
        if already_locked:
            return
        windows_acl.apply_owner_only(path, inherit=inherit, sids=sids)
    except (windows_acl.AclWriteFailed, windows_acl.AclUnavailable) as exc:
        # Translated to OSError so both platforms raise the same type: every
        # caller's handler is written against the POSIX chmod's OSError.
        raise OSError(f"owner-only DACL could not be applied to {path}: {exc}") from exc


# Hook-script extensions treated as runnable on Windows (where there is no
# POSIX execute bit). A hook is a small script KiroCrew shells out to; on
# Windows its runnability is decided by extension + interpreter at exec time,
# not a filesystem bit.
_WINDOWS_RUNNABLE_HOOK_SUFFIXES = (".sh", ".ps1", ".cmd", ".bat", ".py", ".exe")


def is_executable_file(
    path: str | os.PathLike,
    *,
    platform_name: str | None = None,
) -> bool:
    """Should this file be treated as a runnable hook/script for *this* platform?

    POSIX: the file must carry an execute bit (``os.access(X_OK)``) — unchanged
    behavior, so a ``chmod -x`` still disables a hook.

    Windows: there is NO execute bit (every file reports the same mode), so a
    POSIX X_OK check would reject EVERY hook and silently disable the whole
    kiro-hooks autoimport (observed: no preToolUse hook ever registered). On
    Windows we instead accept a regular file whose extension is a known script
    type (``.sh``/``.ps1``/``.cmd``/``.bat``/``.py``/``.exe``) — runnability is
    determined when KiroCrew actually invokes it, not by a meaningless bit.
    ``platform_name`` lets cross-platform discovery apply the target platform's
    rules instead of the host's. A Windows host cannot represent POSIX execute
    bits, so an existing regular file is accepted for an explicit POSIX target.
    """
    try:
        if not os.path.isfile(path):
            return False
        target_is_windows = IS_WINDOWS if platform_name is None else platform_name == "win32"
        if target_is_windows:
            suffix = os.path.splitext(str(path))[1].lower()
            return suffix in _WINDOWS_RUNNABLE_HOOK_SUFFIXES
        return not IS_POSIX or os.access(path, os.X_OK)
    except OSError:
        return False


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
    need *an* interpreter to re-exec KiroCrew itself should prefer
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
# Resource limits
# ---------------------------------------------------------------------------

#: ``task_info`` flavor selector for ``mach_task_basic_info``
#: (``<mach/task_info.h>``). Chosen over the legacy ``TASK_BASIC_INFO`` because
#: its sizes are 64-bit, so a footprint above 4 GiB is not truncated.
_MACH_TASK_BASIC_INFO = 20


class _MachTimeValue(ctypes.Structure):
    """``time_value_t`` (``<mach/time_value.h>``).

    Never read; present only so the fields after it in
    :class:`_MachTaskBasicInfo` land at the offsets the kernel writes them to.
    """

    _fields_ = [("seconds", ctypes.c_int32), ("microseconds", ctypes.c_int32)]


class _MachTaskBasicInfo(ctypes.Structure):
    """``mach_task_basic_info`` (``<mach/task_info.h>``), in kernel order.

    ``resident_size`` is the task's CURRENT resident footprint in bytes and
    falls when pages are released; ``resident_size_max`` is the high-water mark
    that never falls. Reading the wrong one of the two is exactly the bug this
    layout exists to avoid, so both are named rather than indexed.

    Module scope is load-bearing: ``ctypes.POINTER(T)`` memoises T in a
    module-level dict inside ctypes that is never evicted, so declaring this
    inside the probe would pin a fresh pair of type objects on every call — and
    this probe is polled by the dashboard's system-metrics endpoint.
    """

    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_max", ctypes.c_uint64),
        ("user_time", _MachTimeValue),
        ("system_time", _MachTimeValue),
        ("policy", ctypes.c_int),
        ("suspend_count", ctypes.c_int),
    ]


#: ``task_info`` takes and returns a count in ``natural_t``-sized elements
#: (``MACH_TASK_BASIC_INFO_COUNT``). Derived from the layout so it cannot go
#: stale if a field is added above.
_MACH_TASK_BASIC_INFO_COUNT = ctypes.sizeof(_MachTaskBasicInfo) // ctypes.sizeof(ctypes.c_int)


def _scale_ru_maxrss(ru_maxrss: int) -> int:
    """``ru_maxrss`` -> bytes. macOS reports bytes; Linux and other POSIX KiB.

    The unit differs by platform with nothing in the value to tell them apart,
    so every reader of ``ru_maxrss`` goes through here.
    """
    return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024


def _ru_maxrss_bytes() -> int | None:
    """Peak (high-water) RSS in bytes from ``getrusage``, or None on failure.

    POSIX only. This is a **peak**, not a live reading: ``ru_maxrss`` never
    decreases for the life of the process.
    """
    try:
        return _scale_ru_maxrss(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError, AttributeError):
        return None


def _linux_current_rss_bytes() -> int | None:
    """Current RSS in bytes from ``/proc/self/statm``, or None if unreadable.

    Field 1 of ``statm`` is the resident page count — the same quantity
    ``/proc/self/status``'s ``VmRSS`` and ``ps -o rss=`` report, so the
    dashboard's figure reconciles with what an operator measures by hand.
    """
    try:
        fields = Path("/proc/self/statm").read_text().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _macos_current_rss_bytes() -> int | None:
    """Current RSS in bytes via Mach ``task_info``, or None on any failure.

    ``proc_rss_bytes_for_pid`` has no ctypes-only route for an ARBITRARY pid
    (it needs a task port it cannot obtain), but ``mach_task_self()`` hands out
    a port for THIS task unconditionally, so the self-only reading below is
    always available — no subprocess, which matters because the macOS app
    sandbox can deny spawning ``ps``.

    Returns ``resident_size`` (what ``ps -o rss=`` reports), not
    ``phys_footprint``: every other platform branch here reports RSS, and the
    payload field it feeds is named for RSS. Activity Monitor's "Memory" column
    is the phys_footprint variant and will read somewhat differently; that is a
    separate accounting question from the peak-vs-current bug.
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    except OSError:
        return None  # not macOS / libSystem unavailable
    try:
        libc.mach_task_self.restype = ctypes.c_uint
        libc.task_info.restype = ctypes.c_int
        libc.task_info.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(_MachTaskBasicInfo),
            ctypes.POINTER(ctypes.c_uint),
        ]
        info = _MachTaskBasicInfo()
        count = ctypes.c_uint(_MACH_TASK_BASIC_INFO_COUNT)
        # mach_task_self() returns a port name owned by the task itself, not a
        # fresh send right, so unlike mach_host_self() it must NOT be deallocated.
        kern_return = libc.task_info(
            libc.mach_task_self(),
            _MACH_TASK_BASIC_INFO,
            ctypes.byref(info),
            ctypes.byref(count),
        )
    except (AttributeError, OSError, ValueError):
        return None
    if kern_return != 0:  # non-zero kern_return_t -> failure
        return None
    return int(info.resident_size)


def _windows_memory_counters() -> "_ProcessMemoryCounters | None":
    """psapi ``PROCESS_MEMORY_COUNTERS`` for this process, or None on failure."""
    try:

        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and TRUNCATES the
        # pseudo-handle, so GetProcessMemoryInfo fails and this returned 0 for
        # every process — silently disabling the watchdog's RSS-recycle ceiling.
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return counters
        return None
    except Exception:
        return None


def proc_rss_bytes() -> int:
    """Return this process's CURRENT resident set size in bytes, or 0 on failure.

    "Current" is the contract, not an implementation detail: this feeds an
    operator-facing live memory figure, so it must FALL when the gateway
    releases memory and must reconcile with ``ps -o rss=``.

    - Linux: ``/proc/self/statm`` resident pages.
    - macOS: Mach ``task_info(MACH_TASK_BASIC_INFO).resident_size``.
    - Windows: ``GetProcessMemoryInfo().WorkingSetSize``.
    - Last resort on POSIX only: ``getrusage(RUSAGE_SELF).ru_maxrss``, which is
      a **peak** that never decreases. It is here so an unreadable ``/proc`` or
      an unavailable ``libSystem`` still yields an order-of-magnitude number
      rather than 0, and it over-reports by construction — see
      :func:`proc_peak_rss_bytes` for the peak as a deliberate reading.
    """
    if IS_POSIX:
        current = (
            _macos_current_rss_bytes() if sys.platform == "darwin" else _linux_current_rss_bytes()
        )
        if current is not None:
            return current
        return _ru_maxrss_bytes() or 0
    counters = _windows_memory_counters()
    return 0 if counters is None else int(counters.WorkingSetSize)


HEAP_TRIM_INTERVAL_SECONDS = 10 * 60.0
HEAP_TRIM_RSS_THRESHOLD_BYTES = 1536 * 1024 * 1024
HEAP_TRIM_LOG_THRESHOLD_BYTES = 16 * 1024 * 1024
# Must remain below the heartbeat interval.  A queued or wedged default-executor
# worker forfeits this maintenance pass instead of starving the liveness beat.
HEAP_TRIM_TIMEOUT_SECONDS = 2.0


def _malloc_trim() -> bool:
    """Ask glibc to return wholly-free heap pages, or no-op elsewhere."""
    try:
        libc = ctypes.CDLL(None)
        getattr(libc, "gnu_get_libc_version")
        trim = libc.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return bool(trim(0))
    except (AttributeError, OSError):
        return False


def trim_heap_if_needed(
    *,
    rss_reader: Callable[[], int | None] | None = None,
    trimmer: Callable[[], bool] | None = None,
) -> int:
    """Return bytes released from a large Linux gateway heap.

    A high threshold avoids allocator-wide work on healthy gateways. This must
    use Linux's current RSS directly: :func:`proc_rss_bytes` deliberately falls
    back to peak RSS when procfs is unavailable, which would turn one historic
    spike into repeated trim attempts. Unsupported libc and probe failures are
    harmless because reclamation is optional.
    """
    if not IS_LINUX:
        return 0
    try:
        read_rss = rss_reader or _linux_current_rss_bytes
        before = read_rss()
        if before is None:
            return 0
        if before < HEAP_TRIM_RSS_THRESHOLD_BYTES:
            return 0
        if not (trimmer or _malloc_trim)():
            return 0
        after = read_rss()
        if after is None:
            return 0
    except Exception:  # noqa: BLE001 - optional maintenance must not stop the heartbeat
        return 0
    return max(0, before - after)


class HeapTrimMaintainer:
    """Self-gate bounded, best-effort heap reclamation for the gateway.

    The heartbeat calls :meth:`maybe_trim` on every tick. Cadence and in-flight
    ownership live here so the heartbeat stays cadence-free. A timed-out worker
    may continue running because Python cannot cancel native work already in a
    thread; ``_inflight`` prevents another from being submitted until that
    worker returns. If cancellation wins before the worker starts, maintenance
    remains disabled for this object, which is the safe failure mode.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        trim: Callable[[], int] = trim_heap_if_needed,
    ) -> None:
        self._clock = clock
        self._trim = trim
        self._next_trim = clock() + HEAP_TRIM_INTERVAL_SECONDS
        self._inflight = False

    async def maybe_trim(self) -> int:
        """Return bytes released, or zero when skipped, timed out, or failed."""
        try:
            if not IS_LINUX:
                return 0
            now = self._clock()
            if now < self._next_trim or self._inflight:
                return 0
            self._next_trim = now + HEAP_TRIM_INTERVAL_SECONDS
            self._inflight = True
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._run_trim),
                    timeout=HEAP_TRIM_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.debug("gateway heap trim timed out; maintenance pass skipped")
                return 0
        except Exception:  # noqa: BLE001 - maintenance must not stop the heartbeat
            logger.debug("gateway heap trim failed", exc_info=True)
            return 0

    def _run_trim(self) -> int:
        """Worker-thread wrapper that releases the single in-flight slot."""
        try:
            return self._trim()
        finally:
            self._inflight = False


def proc_peak_rss_bytes() -> int:
    """Return this process's PEAK resident set size in bytes, or 0 on failure.

    The high-water mark since the process started: it never decreases, which is
    what makes it useful for diagnosing a transient spike that a live reading
    has already forgotten — and useless as the live reading itself. POSIX reads
    ``getrusage(RUSAGE_SELF).ru_maxrss``; Windows reads
    ``GetProcessMemoryInfo().PeakWorkingSetSize``.
    """
    if IS_POSIX:
        return _ru_maxrss_bytes() or 0
    counters = _windows_memory_counters()
    return 0 if counters is None else int(counters.PeakWorkingSetSize)


# Per-process fd directories, in preference order: /proc/self/fd (Linux),
# /dev/fd (macOS/BSD; also present on Linux as a symlink to the former).
_FD_DIRS = ("/proc/self/fd", "/dev/fd")


def count_open_fds() -> int | None:
    """Return this process's open file descriptor count, or None if unavailable.

    The one shared probe behind both the ``kirocrew.process.open_fds`` gauge
    (``metrics/process_gauges.py``) and gatewayd's zombie-diagnostic
    ``fd_count`` snapshot field, so the two figures cannot drift apart.

    - POSIX: entry count of ``/proc/self/fd`` (Linux) or ``/dev/fd``
      (macOS/BSD), minus one because enumerating the directory opens one fd
      itself (the directory handle) — callers want the steady state.
    - Windows: ``GetProcessHandleCount`` — kernel HANDLEs, not fds, so the
      semantics are platform-dependent (callers document this). Returned raw:
      the query opens no extra handle, so no correction applies.

    Returns None when no probe works; each caller maps its own sentinel.
    """
    for fd_dir in _FD_DIRS:
        try:
            return max(0, len(os.listdir(fd_dir)) - 1)
        except OSError:
            continue
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and TRUNCATES the
        # pseudo-handle (see _windows_memory_counters).
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        handle_count = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(handle_count)):
            return int(handle_count.value)
        return None
    except Exception:
        return None


def proc_rss_bytes_for_pid(pid: int) -> int | None:
    """Resident set size (bytes) of an ARBITRARY *pid*, or None if unavailable.

    Unlike :func:`proc_rss_bytes` (self only), this measures another process so
    the watchdog can sum a spawned agent's whole tree. Linux reads
    ``/proc/<pid>/statm``; Windows opens the PID and calls
    ``GetProcessMemoryInfo``; macOS has no ctypes-only per-pid path, so it
    returns None and the caller keeps its ``ps`` route.
    """

    if sys.platform == "linux":
        try:
            fields = Path(f"/proc/{pid}/statm").read_text().split()
            # statm resident pages * page size.
            return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


# --- /proc process-subtree sampling ----------------------------------------
#
# ONE walk for its two callers, ``mcp_gateway.pool`` and ``subagent``: a
# per-caller BFS over ``/proc/<pid>/task/<tid>/children`` with its own ``256``
# ceiling would let a fix to either policy reach only one surface.
# :func:`proc_subtree_sample` is the
# single entry point for BOTH, and the helpers below are the per-process reads it
# is built from -- module-private, because no caller outside this module wants a
# single read on its own. Pure stdlib: on a host without ``/proc`` every access
# raises ``OSError`` and each reading degrades to its own sentinel.
#
# NOT the only way this repository walks a process tree, and deliberately so.
# ``session_pid._build_child_map`` sums a session's tree from a full ``/proc``
# scan of every process's ``stat`` ``PPid`` field, precisely because the
# ``children`` file this walk reads needs ``CONFIG_PROC_CHILDREN`` and is
# documented as reliable only for frozen tasks -- for a live task it can return
# an incomplete child set and silently drop a descendant subtree from the sum.
# That trade is the right one there (an under-counted tree would make the RSS
# watchdog no-op) and the wrong one here: these two callers sample per backend
# and per live agent on a timer, where a whole-machine scan per sample is the
# larger cost, and an under-count degrades a displayed number rather than
# disabling a protection. Reconsidering the method for these two surfaces is a
# behaviour change to figures users already read, not part of this
# consolidation -- but it is now ONE place to reconsider instead of two.

#: Upper bound on processes walked in one subtree sample. A real tree is tiny
#: (a launcher plus a handful of workers); the cap only guards against a
#: pathological or looping ``/proc`` graph.
#:
#: It bounds THIS WALK's work; it is not a display ceiling for a count, and a
#: surface that already enumerates its own tree does not adopt it. Truncating a
#: displayed count at this number would hand the card a plain integer no
#: consumer can tell from a complete one, where ``procs``/``matched`` reserve
#: ``None`` for "unmeasurable". A walk that needs bounding elsewhere wants a
#: budget that yields ``None``, not a silent truncation.
_SUBTREE_MAX_PROCS = 256


def _proc_status_rss_kb(pid: int) -> int:
    """RSS (KiB) of a single *pid* from ``/proc/<pid>/status``, or -1.

    Reads ``VmRSS``, so the figure matches ``ps -o rss=`` for that one process.
    Distinct from :func:`proc_rss_bytes_for_pid`, which reads ``statm`` pages and
    has a Windows path: this one is the Linux subtree walk's per-process read and
    keeps ``-1`` as its "unreadable" sentinel rather than ``None``.
    """
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def _parse_ppid(stat: bytes) -> "int | None":
    """The parent pid from raw ``/proc/<pid>/stat`` bytes, or None on a parse error.

    Splits after the final ``)`` for the same reason :func:`_parse_cpu_jiffies`
    does: ``comm`` may contain spaces and parens. ppid is field 4 (1-indexed),
    index 1 of the post-comm tokens.
    """
    try:
        rparen = stat.rindex(b")")
        return int(stat[rparen + 2 :].split()[1])
    except (ValueError, IndexError):
        return None


def proc_child_map() -> "dict[int, list[int]] | None":
    """Every live process's children, from ONE pass over ``/proc``. Linux only.

    For the caller that needs the subtrees of MANY roots at once. Asking the
    kernel per root (:func:`_proc_children`) costs one read per THREAD of every
    process visited, so a runtime carrying twenty threads is twenty reads and a
    sampler walking forty roots pays that on each of them. A parent map is one
    read per process on the host and answers every root from memory. Measured on
    a 1713-process host sampling 40 session trees of 734 processes in total:
    11296 children reads taking 432ms against 1717 ``stat`` reads taking 70ms,
    both routes returning the same 40 trees.

    The two routes agree for a process whose parent is alive, by construction:
    ``task/<tid>/children`` is the kernel's child list and ``stat``'s ppid is its
    inverse. Where they part, this map is the more complete of the two -- the
    child list is documented as reliable only for a frozen task, so a live tree
    can come back short through it. A process reparented after its parent died is
    outside both: its ppid becomes 1 and no surviving parent lists it.

    ``None`` -- off Linux, and when ``/proc`` cannot be listed -- means "walk the
    roots yourself", never "this host has no processes": an empty map would read
    as every tree being the root alone.

    Two host-wide snapshots already exist and neither is used here, for a
    measured reason each:

    * :func:`_posix_process_parent_map` spawns ``ps -Ao pid=,ppid=``. Its edges
      agree with this map exactly (measured on a 1743-process host: 335 parents
      in both, zero whose child sets differ), but it costs 122-129ms against
      66-86ms for this pass, and it puts a subprocess spawn on a 5-second
      browser poll. ``_get_rss_tree_mb``'s own note records the same choice for
      the same reason: the Linux branch reads ``/proc`` directly and never
      spawns.
    * :func:`parent_pid` parses one pid's ppid out of the same ``stat`` line, but
      through ``read_text``, so a process whose ``comm`` is not valid UTF-8 --
      any process may set its own name to arbitrary bytes with
      ``prctl(PR_SET_NAME)`` -- raises and is reported as unknown. In a map that
      would drop that process AND every descendant behind it from a caller's
      tree, silently. :func:`_parse_ppid` reads bytes, so it answers. The
      difference is pinned by a test.

    Blocking: one read per process on the host. Executor thread, never the loop.
    """
    if not IS_LINUX:
        return None
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    children: dict[int, list[int]] = {}
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read()
        except OSError:
            # Exited between the listing and the open: it is in no live tree.
            continue
        ppid = _parse_ppid(raw)
        if ppid is not None:
            children.setdefault(ppid, []).append(int(name))
    return children


def proc_cpu_jiffies_for_pids(pids: "list[int]") -> int:
    """Sum utime+stime (clock ticks) over pids already walked.

    The counterpart of :func:`proc_subtree_sample` for a caller that ALREADY
    holds the subtree, so the tree is not enumerated a second time to reach a
    total over the same processes. Each pid is read exactly as the walker reads
    it (:func:`_proc_cpu_jiffies`), so the figure is the walker's figure over
    that set and not a second definition of it.
    """
    return sum(_proc_cpu_jiffies(p) for p in pids)


def _proc_children(pid: int) -> list[int]:
    """Direct child PIDs of *pid* via ``/proc/<pid>/task/<tid>/children``.

    Uses the kernel-provided children list (``CONFIG_PROC_CHILDREN``), so no
    ``pgrep``/full-table scan. Returns ``[]`` if the file is unavailable.

    Per-root: it reads one ``children`` file per THREAD of *pid*. A caller that
    needs many roots' trees in one pass should build
    :func:`proc_child_map` instead.
    """
    kids: list[int] = []
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return kids
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children", encoding="ascii") as fh:
                kids.extend(int(tok) for tok in fh.read().split())
        except (OSError, ValueError):
            continue
    return kids


def _parse_cpu_jiffies(stat: bytes) -> int:
    """Sum utime+stime (clock ticks) from raw ``/proc/<pid>/stat`` bytes.

    Splits after the final ``)`` so a ``comm`` containing spaces/parens is
    handled. utime/stime are fields 14/15 (1-indexed) → indices 11/12 of the
    post-comm tokens. Returns 0 on any parse error.
    """
    try:
        rparen = stat.rindex(b")")
        fields = stat[rparen + 2 :].split()
        return int(fields[11]) + int(fields[12])
    except (ValueError, IndexError):
        return 0


def _proc_cpu_jiffies(pid: int) -> int:
    """utime+stime (clock ticks) for a single pid, 0 on error."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return _parse_cpu_jiffies(fh.read())
    except OSError:
        return 0


def proc_cpu_nanos_for_pid(pid: int) -> int | None:
    """Total (user+system) CPU time of *pid* in NANOSECONDS, or None.

    The per-pid, cross-platform counterpart to :func:`proc_cpu_seconds`, which
    can only measure the CALLING process. A caller samples this twice and
    consumes the DELTA as evidence that a process is doing work; ``None`` means
    no per-pid counter is readable on this host, which is evidence of neither
    work nor death.

    - Linux: ``_proc_cpu_jiffies`` scaled by ``SC_CLK_TCK``. Never None — an
      unreadable pid reads 0 there, which a delta correctly sees as flat.
    - macOS: ``libproc.proc_pidinfo(PROC_PIDTASKINFO)``, already nanoseconds.
    - Windows: ``GetProcessTimes`` kernel+user (100-ns units) over a query-only
      handle.
    - Any other platform: None.

    Root pid ONLY — deliberately no subtree walk here: this probe runs on a read
    loop's cadence and Windows has no child enumeration cheap enough for that. A
    caller that needs the subtree total uses :func:`proc_subtree_sample` on Linux,
    or walks :func:`darwin_child_pids` and sums this per pid on macOS (the
    liveness oracle's darwin backend does exactly that).
    """
    if type(pid) is not int or pid <= 0:
        return None
    if IS_LINUX:
        try:
            ticks = os.sysconf("SC_CLK_TCK") or 100
        except (AttributeError, OSError, ValueError):
            ticks = 100
        return _proc_cpu_jiffies(pid) * (1_000_000_000 // ticks)
    if IS_MACOS:
        return _darwin_process_cpu_nanos(pid)
    if not IS_WINDOWS:
        return None
    handle = _open_process_query_handle(pid)
    if handle is None:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            wintypes.HANDLE(handle),
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None

        def _hundred_ns(value: "wintypes.FILETIME") -> int:
            return (value.dwHighDateTime << 32) | value.dwLowDateTime

        return (_hundred_ns(kernel) + _hundred_ns(user)) * 100
    except Exception:
        return None
    finally:
        _close_process_handle(handle)


class SubtreeSample(NamedTuple):
    """Every reading one subtree walk can produce, from ONE frontier.

    Each field keeps its own sentinel, because the readings are unmeasurable in
    different ways and collapsing any of them into zero is a bug class in its own
    right:

    * ``rss_kb`` — summed KiB, or ``-1`` when the root pid's own status is
      unreadable (it is gone, or the host has no ``/proc``).
    * ``jiffies`` — summed utime+stime clock ticks; an unreadable pid contributes
      0, since a *delta* of jiffies is what a caller consumes.
    * ``procs`` / ``matched`` — how many processes the subtree carries, and how
      many of their command lines contain one of the caller's ``needles``.
      ``None`` means UNMEASURABLE, never zero: rendering "0 processes" for a live
      tree would be a lie, so a surface renders ``None`` as an em dash instead.
    """

    rss_kb: int
    jiffies: int
    procs: Optional[int]
    matched: Optional[int]


def proc_subtree_sample(
    pid: Optional[int],
    *,
    rss: bool = True,
    jiffies: bool = True,
    counts: bool = False,
    needles: tuple[str, ...] = (),
) -> SubtreeSample:
    """Walk *pid*'s process subtree ONCE and return every requested reading.

    A tracked process is frequently a thin launcher whose real memory lives in a
    child, so every reading describes the whole subtree rather than the root pid
    alone.

    The point of one pass is not only the fewer ``/proc`` reads: separate readers
    run at separate instants, so a process that exits between them is counted by
    one and missed by another. Reading every metric off a single frontier is what
    makes "the same set of processes" true of the *result* and not merely of the
    walk rules.

    ``rss`` / ``jiffies`` / ``counts`` let a caller skip the per-process reads it
    does not want, so sharing this walk costs each caller what its own walk cost:
    a CPU-only caller pays no ``status`` read, and an RSS-only caller pays no
    ``stat`` read. A skipped reading comes back as its own sentinel. When nothing
    remains to accumulate — RSS unreadable at the root, no jiffies, nothing
    countable — the descendants are not walked at all.

    ``counts`` is Linux-only (it matches command lines via
    :func:`process_matches`) and yields ``(None, None)`` elsewhere.

    Coverage caveat for a new caller: the walk reads
    ``/proc/<pid>/task/<tid>/children``, which needs ``CONFIG_PROC_CHILDREN`` and
    is documented as reliable only for frozen tasks, so a live tree can come back
    short. That is acceptable for the periodic per-process figures the two
    callers display; a reading that must not under-count (the RSS watchdog's
    recycle decision) uses ``session_pid._build_child_map`` instead, which pays a
    full ``/proc`` scan for completeness.

    Blocking: reads a handful of ``/proc`` entries per process in the subtree, so
    it belongs on an executor thread, never on the event loop.
    """
    if not pid:
        return SubtreeSample(-1, 0, None, None)
    # The counts share RSS's liveness probe: a root pid whose own status cannot
    # be read has nothing to attribute, so there is nothing to count either.
    own_rss = _proc_status_rss_kb(pid) if (rss or counts) else -1
    countable = counts and IS_LINUX and own_rss >= 0
    rss_total = own_rss if (rss and own_rss >= 0) else -1
    total_jiffies = _proc_cpu_jiffies(pid) if jiffies else 0
    procs = 1
    matched = 1 if countable and process_matches(pid, needles) else 0
    if rss_total < 0 and not jiffies and not countable:
        # Nothing a descendant could add — do not pay for the walk.
        return SubtreeSample(-1, total_jiffies, None, None)
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                if rss_total >= 0:
                    kb = _proc_status_rss_kb(child)
                    if kb > 0:
                        rss_total += kb
                if jiffies:
                    total_jiffies += _proc_cpu_jiffies(child)
                if countable:
                    procs += 1
                    if process_matches(child, needles):
                        matched += 1
                nxt.append(child)
        frontier = nxt
    if not countable:
        return SubtreeSample(rss_total, total_jiffies, None, None)
    return SubtreeSample(rss_total, total_jiffies, procs, matched)


def proc_rss_tree_mb_for_pid(pid: int) -> float | None:
    """Sum RSS (MiB) of *pid* and its LINEAGE-VALIDATED Windows descendants.

    Windows-only; returns None on other platforms (callers keep their /proc or
    ps route). The naive way to sum a Windows tree — walk Toolhelp's
    ``th32ParentProcessID`` map — is unsafe for a kill/health decision: that
    field is never cleared when a parent dies and Windows recycles PIDs
    aggressively, so a raw walk sums unrelated subtrees rooted at a recycled
    PID. This reuses :func:`descendant_termination_handles`, which validates
    every parent->child edge against exact creation/exit times across two
    snapshots, so only genuine descendants are counted. RSS that cannot be read
    for a given descendant (another session / higher integrity) is skipped, but
    the root itself always contributes, so the result is never a phantom-low
    tree total attached to a recycled root.

    Returns None if even the root's RSS is unavailable, matching the "unknown,
    do not judge" contract the RSS staleness probe relies on.
    """

    if not IS_WINDOWS:
        return None
    if type(pid) is not int or pid <= 1:
        return None
    root_handle = _open_process_termination_handle(pid)
    if root_handle is None:
        # Cannot even anchor the root — fall back to the single-process read so a
        # readable self still yields a number rather than a spurious None.
        rss = proc_rss_bytes_for_pid(pid)
        return None if rss is None else rss / (1024 * 1024)
    descendants: dict[int, int] = {}
    try:
        identity = _windows_process_handle_identity(root_handle)
        if identity is None or identity[0] != pid:
            rss = proc_rss_bytes_for_pid(pid)
            return None if rss is None else rss / (1024 * 1024)
        try:
            descendants = descendant_termination_handles(pid, root_handle=root_handle)
        except Exception:
            # Enumeration failed (transient snapshot race): measure the root
            # alone rather than an unvalidated tree.
            descendants = {}
        total_bytes = 0
        found = False
        for member in (pid, *descendants):
            member_rss = proc_rss_bytes_for_pid(member)
            if member_rss is not None:
                total_bytes += member_rss
                found = True
        return total_bytes / (1024 * 1024) if found else None
    finally:
        for handle in descendants.values():
            close_process_handle(handle)
        close_process_handle(root_handle)


def proc_cpu_seconds() -> float:
    """Return total (user+system) CPU seconds consumed by this process, or 0.0.

    POSIX: ``resource.getrusage`` user+system times.
    Windows: ``GetProcessTimes`` kernel+user times (100-ns units).
    """
    if IS_POSIX:
        try:

            ru = resource.getrusage(resource.RUSAGE_SELF)
            return ru.ru_utime + ru.ru_stime
        except (ImportError, OSError, ValueError):
            return 0.0
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and truncates the
        # pseudo-handle, so GetProcessTimes fails and this reads 0.0 (mirrors the
        # proc_rss_bytes fix — same truncation, same cause).
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation = wintypes.FILETIME()
        exit_ = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(creation),
            ctypes.byref(exit_),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return 0.0

        def _to_secs(ft: "wintypes.FILETIME") -> float:
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

        return _to_secs(kernel) + _to_secs(user)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# System-wide metrics (Windows). POSIX callers read /proc or sysctl directly;
# Windows has neither, so route through Win32 via ctypes.
# ---------------------------------------------------------------------------

# Prev-sample state for the Windows system-CPU delta (GetSystemTimes).
_prev_win_sys_cpu: dict[str, float] = {"idle": 0.0, "total": 0.0}


def system_memory() -> "tuple[int, int] | None":
    """Return (total_bytes, available_bytes) of physical RAM on Windows.

    Uses ``GlobalMemoryStatusEx``. Returns ``None`` on non-Windows (POSIX
    callers read ``/proc/meminfo`` or ``sysctl hw.memsize`` themselves) or on
    any failure, so the caller can fall back.
    """
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
        return None
    except Exception:
        return None


def system_cpu_percent() -> "float | None":
    """Return system-wide CPU utilization percent since the previous call.

    Windows only, via ``GetSystemTimes`` (idle/kernel/user FILETIMEs; the
    kernel time INCLUDES idle). Returns ``None`` on non-Windows, the first
    (pre-delta) sample, or failure. Stateful — keeps the previous sample in a
    module global, so callers should poll it periodically.
    """
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None

        def _ticks(ft: "wintypes.FILETIME") -> float:
            return float((ft.dwHighDateTime << 32) | ft.dwLowDateTime)

        idle_t = _ticks(idle)
        # kernel already includes idle, so kernel+user is the full busy+idle.
        total_t = _ticks(kernel) + _ticks(user)
        prev_idle = _prev_win_sys_cpu["idle"]
        prev_total = _prev_win_sys_cpu["total"]
        _prev_win_sys_cpu["idle"] = idle_t
        _prev_win_sys_cpu["total"] = total_t
        if prev_total <= 0:
            return None  # first sample, no delta yet
        dtotal = total_t - prev_total
        if dtotal <= 0:
            return None
        busy = dtotal - (idle_t - prev_idle)
        return min(100.0, max(0.0, round(busy / dtotal * 100.0, 1)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Available physical memory, on every platform
#
# "How much RAM can a new process take without pushing this machine into swap"
# has a different answer, and a different interface, on each OS: Linux publishes
# the number outright, macOS publishes page counters and leaves the composition
# to the caller, Windows has a Win32 call. A caller that reads only one of them
# does not get a conservative answer on the others -- it gets NO answer, which
# is why this lives here rather than at each call site.
# ---------------------------------------------------------------------------

_MIB_BYTES = 1024 * 1024

#: ``natural_t`` is 32-bit on macOS, including on Apple silicon.
_NATURAL_T = ctypes.c_uint

#: ``host_statistics64`` flavor selector for ``vm_statistics64_data_t``.
_HOST_VM_INFO64 = 4


class _VMStatistics64(ctypes.Structure):
    """``vm_statistics64_data_t`` (``<mach/vm_statistics.h>``), in kernel order.

    Declared in full even though few fields are read, so the element count handed
    to ``host_statistics64`` is exact and the trailing fields land at the offsets
    the kernel writes them to.

    Module scope is load-bearing: ``ctypes.POINTER(T)`` memoises T in a
    module-level dict inside ctypes that is never evicted, so declaring this
    inside the probe would pin a fresh pair of type objects on every call.
    """

    _fields_ = [
        ("free_count", _NATURAL_T),
        ("active_count", _NATURAL_T),
        ("inactive_count", _NATURAL_T),
        ("wire_count", _NATURAL_T),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", _NATURAL_T),
        ("speculative_count", _NATURAL_T),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", _NATURAL_T),
        ("throttled_count", _NATURAL_T),
        ("external_page_count", _NATURAL_T),
        ("internal_page_count", _NATURAL_T),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


#: How many ``natural_t``-sized elements the kernel must report having filled
#: before ``external_page_count`` holds anything. ``host_statistics64`` writes
#: the count back, and an older kernel that predates the field leaves it zero --
#: indistinguishable from "no file-backed pages" unless the count is checked.
#: Derived from the layout so it cannot go stale if a field is added above.
_EXTERNAL_PAGE_COUNT_ELEMENTS = (
    _VMStatistics64.external_page_count.offset + _VMStatistics64.external_page_count.size
) // ctypes.sizeof(ctypes.c_int)


def macos_vm_statistics() -> "tuple[_VMStatistics64, int] | None":
    """Mach ``host_statistics64(HOST_VM_INFO64)``, or ``None`` on any failure.

    Returns the filled struct and the element count the kernel wrote back, which
    a caller needs to know whether the trailing (later-revision) fields are
    meaningful. macOS-only; returns ``None`` everywhere else.

    Reads in-process through ``ctypes``/``libSystem`` -- **no subprocess**. That
    is not merely faster: the macOS app sandbox can deny spawning ``vm_stat`` or
    ``sysctl``, and this probe runs on the gateway event loop.
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    except OSError:
        return None  # not macOS / libSystem unavailable

    try:
        libc.mach_host_self.restype = ctypes.c_uint
        libc.mach_task_self.restype = ctypes.c_uint
        libc.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libc.host_statistics64.restype = ctypes.c_int
        libc.host_statistics64.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(_VMStatistics64),
            ctypes.POINTER(ctypes.c_uint),
        ]
        host_port = libc.mach_host_self()
        try:
            stats = _VMStatistics64()
            count = ctypes.c_uint(ctypes.sizeof(_VMStatistics64) // ctypes.sizeof(ctypes.c_int))
            kern_return = libc.host_statistics64(
                host_port,
                _HOST_VM_INFO64,
                ctypes.byref(stats),
                ctypes.byref(count),
            )
        finally:
            # Release the send right from mach_host_self so the port reference is
            # not leaked per probe. Guarded so a missing symbol still returns
            # None cleanly below rather than raising out of a memory reading.
            try:
                libc.mach_port_deallocate(libc.mach_task_self(), host_port)
            except (AttributeError, OSError, ValueError):
                pass
    except (AttributeError, OSError, ValueError):
        return None
    if kern_return != 0:  # non-zero kern_return_t -> failure
        return None
    return stats, int(count.value)


def _macos_available_mib() -> int:
    """RAM in MiB a new process can take on macOS without swapping, or 0.

    macOS publishes no ``MemAvailable``; it publishes page counters, and which
    ones count as available is a decision. Each term here is one:

    * ``free_count`` ALREADY INCLUDES ``speculative_count`` -- Darwin's own
      ``vm_stat`` prints ``free_count - speculative_count`` as its "Pages free"
      line. Adding speculative on top double-counts it, which inflates the
      reading on exactly the loaded machine where it must not.
    * ``purgeable_count`` is volatile memory the kernel may drop outright, with
      no I/O, so it is genuinely available.
    * ``inactive_count`` is NOT all reclaimable: it mixes clean file-backed pages
      with DIRTY ANONYMOUS pages that cannot be handed over without compressing
      or swapping them. ``HOST_VM_INFO64`` publishes no inactive-AND-file
      counter, so the intersection is not computable -- but
      ``min(inactive, external_page_count)`` is an upper bound on the file-backed
      share, and it is strictly tighter than ``inactive``. That tightening is
      what stops a browser's gigabytes of inactive anonymous memory reading as
      free.

    Compressed pages are occupied, so the compressor counts are excluded.

    ``0`` means UNKNOWN, and callers skip an unknown reading rather than treating
    it as zero memory. A read that SUCCEEDED but computed nothing therefore
    returns 0 too: a host with no free, purgeable or file-backed pages at all is
    not a reading anyone should act on.
    """
    probe = macos_vm_statistics()
    if probe is None:
        return 0
    stats, filled = probe
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 0
    if page_size <= 0:
        return 0
    inactive = int(stats.inactive_count)
    if filled >= _EXTERNAL_PAGE_COUNT_ELEMENTS:
        inactive = min(inactive, int(stats.external_page_count))
    pages = int(stats.free_count) + int(stats.purgeable_count) + inactive
    if pages <= 0:
        return 0
    # max(1, ...) only after the >0 check above, so a real but sub-MiB reading
    # stays distinguishable from "unknown".
    return max(1, pages * page_size // _MIB_BYTES)


def _linux_available_mib() -> int:
    """``MemAvailable`` in MiB, or 0 when ``/proc/meminfo`` cannot be read.

    The kernel's own estimate of what a new allocation can use without swapping.
    It counts reclaimable page cache, which ``MemFree`` and ``SC_AVPHYS_PAGES``
    both omit -- on a host that has read any files those understate badly (they
    match ``MemFree`` exactly, measured 43,574 MiB against ``MemAvailable``'s
    74,768 MiB on the same idle host).
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("MemAvailable:"):
                    continue
                # "MemAvailable:   107374182 kB" -- the unit is always kB.
                return int(line.split()[1]) * 1024 // _MIB_BYTES
    except (OSError, IndexError, ValueError):
        return 0
    return 0


def host_total_mib() -> int:
    """Total physical RAM in MiB, or 0 when it cannot be determined.

    POSIX ``sysconf`` first, then the Win32 reading -- the same order
    ``sandbox._default_max_memory_mb`` uses, and for the same reason: ``os.sysconf``
    does not EXIST on Windows, so a probe written against it alone does not return a
    conservative number there, it returns nothing. Paired with
    :func:`host_available_mib` so both halves of a memory budget answer on every
    platform; a budget with only one of them silently stops bounding anything.
    """
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return pages * page_size // _MIB_BYTES
    except (AttributeError, OSError, ValueError):
        pass
    mem = system_memory()  # GlobalMemoryStatusEx: (total, available)
    return (mem[0] // _MIB_BYTES) if mem else 0


def host_available_mib() -> int:
    """RAM in MiB actually free for a new process right now, or 0 when unknown.

    **MiB, not GiB, and the unit is load-bearing.** In GiB every reading under
    1 GiB truncates to ``0``, which is also this function's "could not
    determine" answer -- so on the starved host the reading exists to protect,
    860 MiB free would read as "unknown" and the bound built on it would vanish.

    ``0`` is returned only when the platform genuinely cannot be read, so a
    caller can distinguish "no headroom" from "no reading" and fail open on the
    latter.
    """
    if IS_LINUX:
        return _linux_available_mib()
    if IS_MACOS:
        return _macos_available_mib()
    if IS_WINDOWS:
        mem = system_memory()  # GlobalMemoryStatusEx: (total, available)
        return (mem[1] // _MIB_BYTES) if mem else 0
    return 0


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


# ---------------------------------------------------------------------------
# Windows Job objects — the cgroup-v2-scope analogue
# ---------------------------------------------------------------------------
# On Linux, ``sandbox.cgroup_scope_argv`` bounds an agent subprocess AND all its
# descendants as one cgroup (``TasksMax`` = fork-bomb ceiling, ``MemoryMax`` =
# RSS-balloon ceiling). That wrapper is a no-op on Windows (there is no systemd)
# and logs a one-time loud SECURITY warning, so Windows had NO fork-bomb and NO
# memory ceiling on the agent tree at all.
#
# A Job object is the native equivalent: limits apply to every process in the
# job, and descendants of a job member join the job automatically. Unlike the
# cgroup path this cannot be expressed as an argv prefix — there is no wrapper
# binary to prepend — so it is applied to an already-spawned pid instead. See the
# race note in :func:`apply_job_limits` for why that pid must be SUSPENDED.
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000) is deliberately NOT set. It would
# terminate the agent tree as soon as the last job handle closed, changing
# process LIFECYCLE (a gateway exit would kill running agents) rather than merely
# adding a resource ceiling. Omitting it also means the handle need not be held:
# a job object stays alive while processes are assigned to it, so the limits keep
# being enforced after CloseHandle. That makes this fire-and-forget, with no
# handle registry and no teardown semantics to get wrong.
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # JobObjectExtendedLimitInformation


def apply_job_limits(pid: int, *, max_procs: int, max_memory_bytes: int) -> bool:
    """Bound *pid* and its descendants with a Windows Job object.

    The Windows analogue of ``sandbox.cgroup_scope_argv``:

    ==============================  ====================================
    cgroup v2                       Job object
    ==============================  ====================================
    ``TasksMax`` (fork bomb)        ``ActiveProcessLimit``
    ``MemoryMax`` (RSS balloon)     ``JobMemoryLimit``
    ==============================  ====================================

    The memory row is a true equivalent; the process row is NOT one-for-one.
    ``TasksMax`` counts tasks — every thread — while ``ActiveProcessLimit``
    counts processes, so the same numeric budget is a LOOSER bound here: a
    tree of N processes holds at least N tasks and usually many more. It still
    bounds a fork bomb, which is the control's purpose, but do not read the two
    limits as equal strictness, and do not "fix" the gap by scaling the number
    without deciding what a process budget should be — the units differ, so
    there is no arithmetic conversion between them.

    Enforcement is by DENIAL, matching the cgroup tier's practical behavior:
    once ``ActiveProcessLimit`` is reached the member's ``CreateProcess`` calls
    fail with ``ERROR_NOT_ENOUGH_QUOTA``, and an allocation past
    ``JobMemoryLimit`` fails, rather than the tree being killed outright.
    Nothing about process lifetime changes (see the ``KILL_ON_JOB_CLOSE`` note
    above).

    Returns ``True`` when the limits were applied. Returns ``False`` — never
    raises — on POSIX (where ``cgroup_scope_argv`` owns this), on a non-positive
    limit, or on any Win32 failure; the caller treats that as "no ceiling
    enforced" exactly as it already treats the cgroup probe failing.

    Race-free ONLY when paired with :data:`CREATE_SUSPENDED`. Job membership
    covers a member's FUTURE descendants but not ones it already spawned, so
    assigning a *running* child leaves a window in which it could have forked
    something that escapes the job. Callers therefore create the child with
    ``creationflags |= CREATE_SUSPENDED`` — a suspended process has executed no
    instructions and so provably has no descendants — call this, then
    :func:`resume_process_main_thread`. That closes the window by construction
    rather than merely making it small. This function still works on an
    already-running pid (the ceiling then applies from that moment on); the
    suspended handshake is what makes it airtight.
    """
    if IS_POSIX:
        return False
    if max_procs <= 0 or max_memory_bytes <= 0:
        logger.debug(
            "apply_job_limits: skipping non-positive limits (procs=%s, mem=%s)",
            max_procs,
            max_memory_bytes,
        )
        return False
    job = None
    proc_handle = None
    kernel32 = None
    # pragma: no cover below — the ctypes plumbing is Windows-only, and the
    # Windows CI shards run with --no-cov (only the Ubuntu 3.12 shards measure
    # coverage), so these statements are unmeasurable ANYWHERE rather than
    # merely untested. Charging them to the denominator understates the file's
    # real rate, the same reasoning setup.cfg records for the CI-deselected
    # suites it omits. Everything above stays measured: the POSIX early-out is
    # exercised by test_windows_job_limits.py's ungated inertness tests, and the
    # Windows behavior itself is asserted against the live kernel there.
    try:  # pragma: no cover
        _PROCESS_SET_QUOTA = 0x0100  # noqa: N806 — Windows API constant
        _PROCESS_TERMINATE = 0x0001  # noqa: N806 — AssignProcessToJobObject needs it
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # Anonymous job (NULL name): nothing else can open it by name.
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning(
                "SECURITY: CreateJobObject failed (err=%s); fork-bomb / memory-DoS "
                "ceilings are NOT enforced for pid %d",
                _windows_last_error(),
                pid,
            )
            return False

        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_ACTIVE_PROCESS | _JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        info.BasicLimitInformation.ActiveProcessLimit = max_procs
        info.JobMemoryLimit = max_memory_bytes
        if not kernel32.SetInformationJobObject(
            job,
            _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            logger.warning(
                "SECURITY: SetInformationJobObject failed (err=%s); ceilings NOT "
                "enforced for pid %d",
                _windows_last_error(),
                pid,
            )
            return False

        proc_handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc_handle:
            logger.warning(
                "SECURITY: OpenProcess(SET_QUOTA|TERMINATE) failed for pid %d (err=%s); "
                "ceilings NOT enforced",
                pid,
                _windows_last_error(),
            )
            return False
        if not kernel32.AssignProcessToJobObject(job, proc_handle):
            logger.warning(
                "SECURITY: AssignProcessToJobObject failed for pid %d (err=%s); "
                "ceilings NOT enforced",
                pid,
                _windows_last_error(),
            )
            return False
        logger.info(
            "Job object ceilings applied to pid %d (max_procs=%d, max_mem=%dMB)",
            pid,
            max_procs,
            max_memory_bytes // (1024 * 1024),
        )
        return True
    except Exception:
        logger.warning("apply_job_limits failed for pid %s", pid, exc_info=True)
        return False
    finally:
        # Safe to close BOTH handles: without KILL_ON_JOB_CLOSE the job object
        # outlives our handle for as long as processes remain assigned, so the
        # limits stay in force. Leaking these would be a per-spawn handle leak in
        # a long-lived gateway.
        for handle in (proc_handle, job):
            if handle and kernel32 is not None:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    logger.debug("CloseHandle failed", exc_info=True)


_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_INVALID_HANDLE_VALUE = -1
# ResumeThread returns the thread's PREVIOUS suspend count, or (DWORD)-1 on
# failure. Compared as an unsigned 32-bit value because the restype is DWORD.
_RESUME_THREAD_FAILED = 0xFFFFFFFF


def resume_process_main_thread(pid: int) -> bool:
    """Resume every suspended thread of *pid*. Returns True iff one was resumed.

    The other half of race-free Job object assignment. A child spawned with
    :data:`CREATE_SUSPENDED` has executed no instructions, so
    :func:`apply_job_limits` can put it in a job knowing no descendant escaped;
    this then lets it run.

    kernel32 has no ``ResumeProcess``, so the main thread has to be reached by
    ID: snapshot the system thread list
    (``CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD)``), select entries whose
    ``th32OwnerProcessID`` matches, and ``ResumeThread`` each. A freshly created
    suspended process has exactly one thread, but every match is resumed rather
    than only the first — resuming a thread that is not suspended is a harmless
    no-op (its suspend count is already 0), whereas guessing wrong about which
    thread is "main" would leave the process wedged forever.

    Returns ``False`` — never raises — on POSIX (nothing is ever suspended there)
    or on any Win32 failure. A ``False`` return is SERIOUS for the caller: the
    child is alive but frozen, and the only safe response is to kill it rather
    than let a suspended process masquerade as a running agent. See
    ``acp.client.finish_suspended_spawn``, which implements that policy.
    """
    if IS_POSIX:
        return False
    snapshot = None
    kernel32 = None
    # Windows-only ctypes plumbing, unmeasurable on every runner — see the note
    # in :func:`apply_job_limits`.
    try:  # pragma: no cover
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            logger.error(
                "resume_process_main_thread: thread snapshot failed for pid %d (err=%s)",
                pid,
                _windows_last_error(),
            )
            return False
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        resumed = 0
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if kernel32.ResumeThread(thread) != _RESUME_THREAD_FAILED:
                            resumed += 1
                        else:
                            logger.error(
                                "ResumeThread failed for tid %d of pid %d (err=%s)",
                                entry.th32ThreadID,
                                pid,
                                _windows_last_error(),
                            )
                    finally:
                        kernel32.CloseHandle(thread)
                else:
                    logger.error(
                        "OpenThread(SUSPEND_RESUME) failed for tid %d of pid %d (err=%s)",
                        entry.th32ThreadID,
                        pid,
                        _windows_last_error(),
                    )
            # Thread32Next overwrites the entry, dwSize included, so it must be
            # reset before every call or the next iteration fails.
            entry.dwSize = ctypes.sizeof(_ThreadEntry32)
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if not resumed:
            logger.error("resume_process_main_thread: no threads resumed for pid %d", pid)
        return resumed > 0
    except Exception:
        logger.error("resume_process_main_thread failed for pid %s", pid, exc_info=True)
        return False
    finally:
        if snapshot and snapshot != _INVALID_HANDLE_VALUE and kernel32 is not None:
            try:
                kernel32.CloseHandle(snapshot)
            except Exception:
                logger.debug("CloseHandle(snapshot) failed", exc_info=True)


def is_readonly_filesystem(path: Path) -> bool:
    """Confirm a Linux readonly mount; absence or probe failure grants nothing."""
    if sys.platform != "linux":
        return False
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def ensure_owner_rwx_dirs(root: str | os.PathLike) -> None:
    """OR owner read/write/execute onto *root* and every directory below it.

    ``shutil.copytree`` preserves source modes verbatim. A copy made from a
    hardened install source can therefore lack owner write or execute (for
    example, mode ``0o455``). Creating a file needs a writable AND searchable
    parent, so callers that write a marker into a fresh copy run this first.

    Only directories are touched, and only by adding ``S_IRWXU``. Copied files
    remain exactly as shipped, so a file-mode customization still diverges
    skill fingerprints. On Windows ``os.chmod`` honours only the read-only
    flag: adding owner write clears it, while the extra owner bits are a no-op.
    Never raises: a directory this cannot repair surfaces as the original
    ``PermissionError`` at the caller's write site, which every caller handles.
    """

    def _add_owner_rwx(entry: str) -> None:
        try:
            mode = os.lstat(entry).st_mode
            if stat.S_ISDIR(mode) and mode & stat.S_IRWXU != stat.S_IRWXU:
                os.chmod(entry, stat.S_IMODE(mode) | stat.S_IRWXU)
        except OSError:
            logger.debug("could not add owner rwx to %s", entry, exc_info=True)

    top = os.fspath(root)
    if is_link_or_junction(top):
        return
    _add_owner_rwx(top)
    for dirpath, dirnames, _filenames in os.walk(top):
        for dname in list(dirnames):
            entry = os.path.join(dirpath, dname)
            if is_link_or_junction(entry):
                # os.walk(followlinks=False) skips POSIX symlinks, but a
                # Windows junction lstats as a plain directory and WOULD be
                # descended -- and chmodded THROUGH, touching its target
                # tree. Prune both so the walk never leaves *root*.
                dirnames.remove(dname)
                continue
            _add_owner_rwx(entry)


def windows_tree_cleanup_pending(pid: int, start: str | None) -> bool:
    """A pending pin outranks numeric liveness, including a dead root PID."""
    if not IS_WINDOWS:
        return False
    try:
        key = (pid, int(start)) if start is not None else None
    except (TypeError, ValueError):
        return False
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if key is None:
            return any(state.root_pid == pid for state in _PENDING_WINDOWS_TREE_CLEANUPS.values())
        return key in _PENDING_WINDOWS_TREE_CLEANUPS


async def _create_windows_subprocess_owned(
    owner: _PendingWindowsTreeCleanup, program: str, *args: str, limit: int = 2**16, **kwargs: Any
) -> asyncio.subprocess.Process:
    """Run CPython's subprocess implementation with per-call creation capture.

    Only this call sees the substituted native API and transport constructors.
    The interpreter's functions, globals, event loop and Popen stay untouched.
    Capture happens at CreateProcess return, BEFORE _close_pipe_fds, Handle
    publication, thread-handle close, proactor registration or pipe connection.
    """
    # Private Windows APIs have no Linux typeshed declarations. Runtime identity
    # checks below refuse a different transport implementation before creation.
    windows_events: Any = importlib.import_module("asyncio.windows_events")
    windows_utils: Any = importlib.import_module("asyncio.windows_utils")
    loop = asyncio.get_running_loop()
    make_transport = getattr(getattr(loop, "_make_subprocess_transport"), "__func__", None)
    subprocess_exec = getattr(loop.subprocess_exec, "__func__", None)
    if (
        make_transport is not windows_events.ProactorEventLoop._make_subprocess_transport
        or subprocess_exec is not asyncio.BaseEventLoop.subprocess_exec
    ):
        raise RuntimeError(
            "Windows session start refused: this interpreter does not expose the CPython "
            "Proactor subprocess path the tree-cleanup capture binds to; run the gateway on "
            f"a measured interpreter ({_WINDOWS_CAPTURE_MEASURED_VERSIONS})"
        )
    execute_child = getattr(subprocess.Popen, "_execute_child")
    start_transport = windows_events._WindowsSubprocessTransport._start
    if (
        not {"_winapi", "CreateProcess", "Handle"}.issubset(execute_child.__code__.co_names)
        or not {"windows_utils", "Popen"}.issubset(start_transport.__code__.co_names)
        or "_WindowsSubprocessTransport" not in make_transport.__code__.co_names
    ):
        # Same reasoning as the quarantine refusal: an operator meeting this sees a
        # failed session start and no context, and the remedy is not guessable from
        # the shape names. The interpreter is the whole cause, so it is the remedy.
        raise RuntimeError(
            "Windows session start refused: this interpreter's private subprocess bindings "
            "differ from the shapes the tree-cleanup capture is measured against; run the "
            f"gateway on a measured interpreter ({_WINDOWS_CAPTURE_MEASURED_VERSIONS})"
        )
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if owner not in _WINDOWS_TREE_ADMISSIONS or owner.retired or owner.handles:
            raise RuntimeError("Windows subprocess requires an unused cleanup reservation")

    def clone(function: Any, **bindings: Any) -> Any:
        cloned = types.FunctionType(
            function.__code__,
            dict(function.__globals__, **bindings),
            function.__name__,
            function.__defaults__,
            function.__closure__,
        )
        cloned.__kwdefaults__ = function.__kwdefaults__
        return cloned

    native = getattr(subprocess, "_winapi")
    handle_type = getattr(subprocess, "Handle")

    class CreationAPI:
        def __init__(self) -> None:
            self.thread_pin: Any = None

        def __getattr__(self, name: str) -> Any:
            return getattr(native, name)

        def CloseHandle(self, value: int) -> None:
            if self.thread_pin is not None and int(self.thread_pin) == value:
                self.thread_pin.Close()
                self.thread_pin = None
            else:
                native.CloseHandle(value)

        def CreateProcess(self, *values: Any, **options: Any) -> Any:
            result = native.CreateProcess(*values, **options)
            hp, ht, pid, _tid = result
            # Record the raw exact process value before wrapping handles: even
            # a later wrapper/setup exception must leave the reservation owed.
            owner.root_pid = pid
            owner.key = (pid, 0)
            owner.handles[pid] = hp
            # If pipe-fd cleanup raises before CPython closes ht, this local
            # reference-counted pin closes it when the failed launch unwinds.
            self.thread_pin = handle_type(ht)
            # Keep the reference-counted object itself. Popen receives THIS
            # object below, not a second Handle owning the same native value.
            pin = handle_type(hp)
            owner.raw_root_pin = pin
            return result

    def shared_handle(value: int) -> Any:
        if owner.raw_root_pin is not None and int(owner.raw_root_pin) == value:
            return owner.raw_root_pin
        return handle_type(value)

    class OwnedPopen(windows_utils.Popen):
        _execute_child = clone(
            getattr(subprocess.Popen, "_execute_child"), _winapi=CreationAPI(), Handle=shared_handle
        )

    class OwnedTransport(windows_events._WindowsSubprocessTransport):
        _start = clone(
            windows_events._WindowsSubprocessTransport._start,
            windows_utils=types.SimpleNamespace(Popen=OwnedPopen),
        )

    class SpawnLoop:
        def __getattr__(self, name: str) -> Any:
            return getattr(loop, name)

        _make_subprocess_transport = staticmethod(
            types.MethodType(
                clone(make_transport, _WindowsSubprocessTransport=OwnedTransport),
                loop,
            )
        )

    def protocol_factory() -> Any:
        return aio_subprocess.SubprocessStreamProtocol(limit=limit, loop=loop)

    transport, protocol = await subprocess_exec(
        SpawnLoop(), protocol_factory, program, *args, **kwargs
    )
    return aio_subprocess.Process(transport, protocol, loop)
