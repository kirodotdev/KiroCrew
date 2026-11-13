"""Point raw stdout/stderr at the desktop shell's log when the shell asks.

On Windows the desktop shell spawns the gateway with no inherited stdio, because
Node only adds ``CREATE_NO_WINDOW`` to a spawn whose stdio inherits no handle. A
gateway with a console of its own can show that console and take the focus.
Instead the shell names its launch log in :data:`DESKTOP_STDIO_LOG_ENV`, and the
entry points call :func:`redirect_desktop_stdio` early, so tracebacks and
child-process output still reach the same file the shell reads. Until then
stderr is a pipe the shell copies into that file; the re-point closes it.

Standard library only: this runs before the package's dependencies are known to
import.
"""

from __future__ import annotations

import os
import sys

DESKTOP_STDIO_LOG_ENV = "KIROCREW_DESKTOP_STDIO_LOG"


def redirect_desktop_stdio(fds: tuple[int, ...] = (1, 2)) -> None:
    """Re-point *fds* at the file named in :data:`DESKTOP_STDIO_LOG_ENV`, if set.

    The variable is removed first, so the processes this one spawns do not
    re-point their own descriptors. Best-effort: when the file cannot be opened
    the descriptors keep the null device the shell gave them.
    """
    path = os.environ.pop(DESKTOP_STDIO_LOG_ENV, "")
    if not path:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass  # a broken stream must not stop the start
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    try:
        raw_fd = os.open(path, flags, 0o600)
    except OSError:
        return
    try:
        for fd in fds:
            try:
                os.dup2(raw_fd, fd)
            except OSError:
                pass  # leave this descriptor on the null device
    finally:
        os.close(raw_fd)
