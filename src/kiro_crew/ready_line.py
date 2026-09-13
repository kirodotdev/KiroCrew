"""Emit the ``KIROCREW_READY:`` line on a private duplicate of stdout.

Background loaders (the vendored llama-cpp ``suppress_stdout_stderr`` during
embedding-model load) ``dup2`` ``/dev/null`` over fd 1/2 for the duration of
the load. That runs on a worker thread, so a ``print()`` on the main thread
that lands inside that window is silently swallowed and a supervising parent
(``--json-ready`` harness, the Kiro CLI sidecar supervisor) never sees READY.

The fix is to ``dup()`` the *original* stdout before any loader can start and
write the READY line to that private descriptor: ``dup2`` replaces what fd 1
points at, but the duplicate keeps pointing at the real pipe.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def capture_stdout_fd() -> int | None:
    """Return a private dup of the current stdout fd, or ``None`` if unavailable."""
    try:
        return os.dup(sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        return None


def format_ready_line(payload: dict[str, Any]) -> str:
    return f"KIROCREW_READY:{json.dumps(payload)}\n"


def emit_ready_line(payload: dict[str, Any], out_fd: int | None) -> None:
    """Write the READY line to ``out_fd`` (closing it), falling back to ``print``.

    ``out_fd`` should come from :func:`capture_stdout_fd` taken *before* any
    stdout-suppressing loader could run, so the line survives a concurrent
    ``dup2(devnull, 1)`` window.
    """
    line = format_ready_line(payload)
    if out_fd is not None:
        data = line.encode("utf-8")
        try:
            view = memoryview(data)
            while view:
                n = os.write(out_fd, view)
                view = view[n:]
            return
        except OSError:
            pass
        finally:
            try:
                os.close(out_fd)
            except OSError:
                pass
    print(line, end="", flush=True)
