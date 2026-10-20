"""One child process of an update: its environment, its process group, its bounded output.

The scrubbed environment a build child runs in, the spawn that gives it its own process
group with the lock descriptor and anonymous output files, the timeout and cancel kill,
and the redacted failure tail of a build step.

:mod:`kiro_crew.platform.wheel_engine` is the import path and the patch surface: callers
import these names from it and tests patch them there, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries such a patch to this module. This module never
imports the facade.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from typing import Callable

from kiro_crew.platform.wheel_engine_contract import (
    _NO_CONTEXT,
    WheelUpdateError,
    _BuildContext,
    _raise_if_cancelled,
)
from kiro_crew.platform_compat import IS_POSIX, kill_popen_tree
from kiro_crew.subprocess_utf8 import utf8_stdout

#: How much of a child's stderr is kept: its TAIL, where pip and venv put the
#: error. Written to an anonymous file rather than a pipe, so a chatty child can
#: neither fill the caller's memory nor block on a full pipe.
_STDERR_TAIL_BYTES = 64 * 1024
#: How much of a child's stdout is read when it is wanted (a version string, a
#: ``pip check`` report).
_STDOUT_MAX_BYTES = 64 * 1024
#: How much of a failed build step's redacted stderr goes into its message.
_ERROR_DETAIL_CHARS = 2000
#: Ceiling on failure text an operator is shown in one line (a dashboard step, an
#: ERROR log line, an audit record, a probe's detail). Shared with
#: :mod:`kiro_crew.platform.wheel_apply`.
FAILURE_TEXT_CHARS = 500
#: How often a running child is polled for exit. Its own kill needs no poll:
#: a timeout or a cancel kills the group at once; this bounds only how soon the
#: kill (or an ordinary exit) is noticed.
_REAP_POLL_SECS = 0.05

#: Loader variables the gateway's own process may need its children to keep:
#: a base interpreter built with a shared libpython finds it through them. The
#: INJECTION variables (``LD_PRELOAD``, ``DYLD_INSERT_LIBRARIES``) stay removed.
_KEPT_LOADER_PATHS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")


def _build_child_env(trusted: bool) -> dict[str, str]:
    """The environment a child of the apply runs with.

    *trusted* (a child started inside the gateway): the update commands'
    scrubbed environment (``PATH`` narrowed to the trusted system directories,
    the interpreter and loader injection variables and exported shell functions
    removed), keeping only the library search paths the gateway itself was
    started with (:data:`_KEPT_LOADER_PATHS`). Otherwise (``kirocrew update`` in
    the operator's shell) the full environment, so the operator's toolchain and
    credential helpers reach pip. Interpreter children run ``-I`` either way, so
    an inherited ``PYTHONPATH`` never reaches pip's dependency resolution.
    """
    if not trusted:
        return dict(os.environ)
    from kiro_crew.platform.update_provider import _trusted_path_env

    env = _trusted_path_env()
    if env is None:
        raise WheelUpdateError("no trusted system PATH to run the update's build steps with")
    for name in _KEPT_LOADER_PATHS:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _spawn_build_child(
    argv: list[str],
    timeout: float,
    step: str,
    *,
    ctx: _BuildContext = _NO_CONTEXT,
    cwd: str = "/",
    want_stdout: bool = False,
    umask: int = -1,
    stdin_data: bytes | None = None,
    extra_fds: tuple[int, ...] = (),
) -> tuple[int, bytes, bytes]:
    """Run one child of the apply to completion; return ``(rc, stdout, stderr tail)``.

    Every child of the apply comes through here. It runs in its own session, so
    its whole process group can be killed (``python -m venv`` runs ``ensurepip``
    as a grandchild), with the environment :func:`_build_child_env` gives it,
    with the update lock's descriptor (:attr:`_BuildContext.lock_fd`), and with
    no pipes: stdout and stderr go to anonymous files. *stdin_data*, when
    given, reaches the child through an anonymous file too, and *extra_fds*
    are inherited beside the lock's descriptor (openssl reads its key and
    signature from them as ``/dev/fd/N``).

    The group is killed when *timeout* passes, when the cancel is set
    (synchronously, inside :meth:`ApplyCancel.set`), and when this call is
    interrupted, and the child is reaped before this returns or raises. A
    killed child never reports a status. The exit is polled under the same lock
    the kill takes, so a kill is only ever signalled while the leader is
    unreaped: its pid, which names the group, cannot have been reused.

    Raises :class:`WheelUpdateError` naming *step* when the child cannot start
    or times out, and :class:`WheelUpdateCancelled` on cancel.
    """
    _raise_if_cancelled(ctx.cancel)
    env = _build_child_env(ctx.trusted_env)
    pass_fds = ((ctx.lock_fd,) if ctx.lock_fd is not None else ()) + tuple(extra_fds)
    with (
        tempfile.TemporaryFile() as infile,
        tempfile.TemporaryFile() as outfile,
        tempfile.TemporaryFile() as errfile,
    ):
        if stdin_data is not None:
            infile.write(stdin_data)
            infile.seek(0)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=infile if stdin_data is not None else subprocess.DEVNULL,
                stdout=outfile if want_stdout else subprocess.DEVNULL,
                stderr=errfile,
                cwd=cwd,
                env=env,
                umask=umask,
                start_new_session=IS_POSIX,
                pass_fds=pass_fds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WheelUpdateError(f"{step} could not run: {exc}") from exc
        guard = threading.Lock()
        killed_for: list[str] = []

        def _kill(reason: str) -> None:
            with guard:
                if proc.returncode is None:
                    killed_for.append(reason)
                    kill_popen_tree(proc)

        undo: Callable[[], None] | None = None
        try:
            if ctx.cancel is not None:
                undo = ctx.cancel.on_set(lambda: _kill("cancel"))
            deadline = time.monotonic() + timeout
            while True:
                with guard:
                    if proc.poll() is not None:
                        break
                if time.monotonic() >= deadline:
                    _kill("timeout")
                time.sleep(_REAP_POLL_SECS)
        except BaseException as exc:
            _kill("interrupted")
            proc.wait()
            if isinstance(exc, (OSError, subprocess.SubprocessError)):
                raise WheelUpdateError(f"{step} failed while running: {exc}") from exc
            raise
        finally:
            if undo is not None:
                undo()
        if killed_for and killed_for[0] == "cancel":
            _raise_if_cancelled(ctx.cancel)
        if killed_for:
            raise WheelUpdateError(f"{step} timed out after {timeout:.0f}s")
        outfile.seek(0)
        out = outfile.read(_STDOUT_MAX_BYTES)
        errfile.seek(0, os.SEEK_END)
        errfile.seek(max(0, errfile.tell() - _STDERR_TAIL_BYTES))
        return proc.returncode, out, errfile.read()


def _redacted_detail(raw: bytes, limit: int = _ERROR_DETAIL_CHARS) -> str:
    """A child's output for an error message: redacted IN FULL, then cut to its tail.

    The order is the point. Cutting first can split a credential, and half a
    token does not match the redactors' patterns, so the surviving fragment
    would reach the log and the dashboard verbatim.
    """
    from kiro_crew.platform.context import redact_log_via_context

    return redact_log_via_context(utf8_stdout(raw).strip())[-limit:]


def _run(argv: list[str], timeout: float, step: str, *, ctx: _BuildContext = _NO_CONTEXT) -> None:
    """Run one build step; raise :class:`WheelUpdateError` with its redacted tail on failure.

    Build steps write into the shadow tree, so they run under the owner-only
    build umask (see :data:`_BUILD_UMASK`).
    """
    rc, _out, err = _spawn_build_child(argv, timeout, step, ctx=ctx, umask=_BUILD_UMASK)
    if rc != 0:
        detail = _redacted_detail(err)
        raise WheelUpdateError(f"{step} exited {rc}" + (f": {detail}" if detail else ""))


#: Owner-only umask for the venv/pip build children (POSIX; -1 = leave unchanged).
#: ``kirocrew service install`` refuses to attach the AppArmor unprivileged-userns
#: profile to a launcher that is group- or world-writable (service/apparmor.py), and
#: ``python -m venv``/pip create ``bin/`` under the process umask — so a permissive
#: umask (``002``, common on shared dev hosts) would otherwise birth the tree ``0775``
#: and the profile install would refuse, recurring on every update. ``subprocess``
#: applies ``umask`` in the child between fork and exec (thread-safe, unlike a
#: ``preexec_fn`` in this threaded gateway), so bin/ and the launcher are born
#: owner-only with no window and the profile attaches.
_BUILD_UMASK = 0o077 if IS_POSIX else -1
