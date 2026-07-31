"""Post-exec shim: apply a child's resource limits, then ``exec`` the real command.

Spawned as::

    <sys.executable> -I -S -c <this file's source> [--rlimits=SPEC] [--oom-bias] -- argv...

and replaces itself with ``argv`` via ``execv``, so the PID, process group,
session, inherited fds, and exit status the caller observes are all the child's
own -- this is a shim, not a supervisor: it never forks and never waits.

Why this exists
---------------
Handing the same ``setrlimit`` calls to ``preexec_fn`` instead makes CPython
``fork()`` the multi-GB, ~118-thread gateway and run Python bytecode in the
child before ``exec``. A lock another thread held at fork time can never be
released there, so the child can deadlock before reaching ``exec`` -- and when it
does, two things follow that a per-command timeout cannot reach:

* ``subprocess.Popen._execute_child`` blocks in an unbounded
  ``os.read(errpipe_read, ...)`` waiting for the child to exec or die. For
  ``asyncio.create_subprocess_exec`` that read happens on the event loop thread,
  with no ``await`` point, so the whole gateway stops.
* ``_posixsubprocess``'s ``child_exec()`` runs ``_close_open_fds()`` *after*
  ``preexec_fn``, so a child wedged in ``preexec_fn`` still holds a duplicate of
  every parent fd -- including the dashboard's listening socket, which then
  survives the gateway it outlived.

Running the limits here instead removes the whole class: the process is
single-threaded post-exec, and with ``preexec_fn=None`` the fork child executes
only async-signal-safe C. Limits set here are inherited by the exec'd image and
all of its descendants, so coverage is unchanged.

Kept stdlib-only and import-light on purpose: it is executed as an immutable
source string captured by the gateway at import time, never imported from the
(agent-writable) package directory at spawn time. ``-S`` is part of that fence --
it skips ``site``, so a ``sitecustomize`` dropped into site-packages cannot run
ahead of this code -- and it also halves interpreter startup.
"""

from __future__ import annotations

import os
import sys

try:
    import resource as _resource
except ImportError:  # pragma: no cover - Windows has no POSIX rlimits
    _resource = None  # type: ignore[assignment]

_RLIMIT_FLAG = "--rlimits="
_OOM_BIAS_FLAG = "--oom-bias"
_ARGV_SEPARATOR = "--"
# Shell convention for "command found but could not be executed", so a caller
# that only sees the exit status can still tell an exec failure from the
# command's own nonzero exit.
_EXEC_FAILED = 127
# Matches sandbox.session_host_preexec: raise NOFILE to the inherited hard cap,
# or to this floor when the kernel reports no ceiling at all.
_UNLIMITED_NOFILE_FLOOR = 65536


def _parse_rlimits(spec: str) -> list[tuple[int, int | None]]:
    """Resolve ``RLIMIT_NAME:value`` pairs into ``(rlimit id, value)`` tuples.

    ``value`` is ``None`` for the literal token ``hard``, which means "raise the
    soft limit to the inherited hard limit" rather than a numeric request.
    Unknown names and unparseable values are skipped rather than failing the
    spawn, matching ``security.apply_resource_limits``.

    Called BEFORE :func:`_apply_rlimits` so every allocation this parse needs
    happens while the process still has its inherited budget.
    """
    if _resource is None or not spec:
        return []
    parsed: list[tuple[int, int | None]] = []
    for item in spec.split(","):
        name, _, raw = item.partition(":")
        res_id = getattr(_resource, name, None)
        if not isinstance(res_id, int):
            continue
        if raw == "hard":
            parsed.append((res_id, None))
            continue
        try:
            parsed.append((res_id, int(raw)))
        except ValueError:
            continue
    return parsed


def _apply_rlimits(pairs: list[tuple[int, int | None]]) -> None:
    """Apply pre-parsed limits to this process.

    Mirrors ``security.apply_resource_limits``: clamp a numeric request DOWN to
    the inherited hard limit (never try to raise a ceiling), set soft AND hard so
    the child cannot lift its own cap back up, and swallow per-limit failures so
    an rlimit this platform lacks never blocks the spawn. The ``hard`` token is
    the one case that raises the SOFT limit -- it leaves the hard limit alone, so
    a trusted session host gets headroom without losing its ceiling.
    """
    if _resource is None:
        return
    res = _resource
    for res_id, requested in pairs:
        try:
            soft, hard = res.getrlimit(res_id)
            if requested is None:
                if hard == res.RLIM_INFINITY:
                    res.setrlimit(res_id, (max(soft, _UNLIMITED_NOFILE_FLOOR), hard))
                else:
                    res.setrlimit(res_id, (hard, hard))
                continue
            if hard != res.RLIM_INFINITY:
                requested = min(requested, hard)
            res.setrlimit(res_id, (requested, requested))
        except (ValueError, OSError):
            continue


def _bias_oom_score() -> None:
    """Bias the kernel OOM killer toward this process tree (``oom_score_adj``=1000).

    So a memory-ballooning tool is killed before the cgroup ``memory.max``
    ceiling takes out the whole agent scope. Inherited across ``exec`` and by
    descendants. Linux-only, unprivileged, best-effort -- never raises.

    Requested explicitly with ``--oom-bias`` rather than implied by the presence
    of limits, because the callers do not agree: the tool and build preexec paths
    this replaces biased every child, while the session-host path never did, and
    an interactive terminal must not be a preferred kill target at all.
    """
    if sys.platform != "linux":
        return
    try:
        fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY)
        try:
            os.write(fd, b"1000")
        finally:
            os.close(fd)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Parse the shim's own options, then ``exec`` the command after ``--``.

    Returns an exit code only on a usage or exec failure; on success it does not
    return at all.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    spec = ""
    want_oom_bias = False
    while args and args[0] != _ARGV_SEPARATOR:
        item = args.pop(0)
        if item.startswith(_RLIMIT_FLAG):
            spec = item[len(_RLIMIT_FLAG) :]
        elif item == _OOM_BIAS_FLAG:
            want_oom_bias = True
        else:
            # Fail closed. A stray token here means the caller and this shim
            # disagree about the argv contract, and guessing which side the
            # command starts on could exec the wrong thing.
            sys.stderr.write(f"spawn shim: unknown option {item!r}\n")
            return _EXEC_FAILED
    if not args:
        sys.stderr.write(f"spawn shim: missing {_ARGV_SEPARATOR!r} argv separator\n")
        return _EXEC_FAILED
    args.pop(0)  # the separator itself
    if not args:
        sys.stderr.write("spawn shim: no command to execute\n")
        return _EXEC_FAILED

    # Everything that allocates happens before the limits go on, so a tight
    # RLIMIT_AS cannot make the shim fail between setrlimit and execv. Encoding
    # argv here leaves execv with only its own C-level argument array to build.
    pairs = _parse_rlimits(spec)
    try:
        encoded = [os.fsencode(item) for item in args]
    except (UnicodeEncodeError, ValueError):
        sys.stderr.write("spawn shim: command argv is not encodable\n")
        return _EXEC_FAILED

    _apply_rlimits(pairs)
    if want_oom_bias:
        _bias_oom_score()
    try:
        # execv, not execve: the environment this process was given IS the
        # environment the caller built for the command, and passing it through
        # untouched avoids rebuilding the whole mapping under the new limits.
        # No PATH search -- the caller resolves argv[0] so a missing command
        # surfaces as FileNotFoundError at the spawn, as it did without a shim.
        os.execv(encoded[0], encoded)
    except OSError as exc:
        sys.stderr.write(f"spawn shim: cannot execute {args[0]!r}: {exc.strerror}\n")
        return _EXEC_FAILED
    return _EXEC_FAILED  # pragma: no cover - execv does not return on success


if __name__ == "__main__":  # pragma: no cover - exercised as a spawned process
    sys.exit(main())
