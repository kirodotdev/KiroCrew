"""Out-of-process symlink resolver for the sensitive-path gate.

The SOURCE of this module is what runs: :mod:`kiro_crew.security.pathres_client`
reads this file once at import time (gateway boot, before any agent turn) and
starts the helper as ``python -I -c <that source>``. The file on disk is never
re-read for a spawn, so an edit to it -- in an editable install the working tree
is what an agent edits -- changes nothing until the gateway restarts, exactly
like every other product source file. It must import NOTHING from ``kiro_crew``
so it starts in a few milliseconds and carries none of the gateway's threads.

Why a separate PROCESS rather than the ``mc-pathres`` thread pool alone: the
resolver's job -- ``os.path.realpath`` and ``Path.resolve`` -- is pure Python
that issues one ``lstat`` and one ``readlink`` per path component, and every one
of those syscalls releases and then re-acquires the GIL. Measured on a 64-core
Linux host: resolving the ~60 keystone anchors takes 2 ms in an idle
interpreter and 2000-4200 ms when ONE other thread is CPU-bound, because each
of the ~400 re-acquisitions waits a full switch interval (5 ms). The gateway
runs 100+ threads, so inside it that convoy was the common case, the 2 s
resolve budget expired on a healthy local disk, and every gate refused ordinary
project paths as "sensitive" for the cooldown window (57 stalls in one day on
one host, each preceded by an event-loop-blocked warning). A helper process has
its own GIL: the caller pays ONE pipe round-trip per request instead of
hundreds of GIL handoffs, and a helper genuinely wedged on a dead mount can be
KILLED -- which a timed-out thread cannot be -- so the pool worker waiting on
it is freed rather than pinned for the life of the process.

Protocol: newline-delimited JSON on stdin/stdout, one object per line.
Request ``{"i": <int>, "p": <path>}`` answers ``{"i": <int>, "r": [<realpath>|null,
<resolve>|null]}``; request ``{"i": <int>, "p": [<path>, ...]}`` answers
``{"i": <int>, "r": [[<realpath>|null, <resolve>|null], ...]}`` in order. The list
form exists for the root anchors the gate re-resolves on EVERY call (``$HOME`` and
the override roots): measured on Windows CI, one test file made ~80,000 single
requests, seven per gate call, and each pipe round-trip there costs a scheduler
tick, so the round-trips, not the resolutions, were the time. Both sides use
``ensure_ascii`` so a path carrying surrogate-escaped bytes survives the pipe
unchanged. Any malformed request line ends the process (the client treats EOF
as a transport fault), so a wedged or corrupted helper never answers the wrong
request.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resolve_one(path: str) -> list[str | None]:
    """``[realpath, resolve]`` for *path*; ``None`` where that spelling raised."""
    try:
        real: str | None = os.path.realpath(path)
    except (OSError, ValueError):
        real = None
    try:
        resolved: str | None = str(Path(path).resolve())
    except (OSError, ValueError, RuntimeError):
        resolved = None
    return [real, resolved]


def serve(stdin, stdout) -> None:
    """Answer requests until EOF or a malformed line."""
    for raw in stdin:
        try:
            request = json.loads(raw)
            paths = request["p"]
            if isinstance(paths, str):
                answer: object = _resolve_one(paths)
            elif isinstance(paths, list) and all(isinstance(p, str) for p in paths):
                answer = [_resolve_one(p) for p in paths]
            else:
                return
            reply = {"i": request["i"], "r": answer}
        except (ValueError, KeyError, TypeError):
            return
        stdout.write(json.dumps(reply, ensure_ascii=True))
        stdout.write("\n")
        stdout.flush()


if __name__ == "__main__":
    # ``surrogateescape`` on stdin mirrors what the client sends; the JSON is ASCII
    # either way, so this only matters for a hand-driven session.
    serve(
        open(sys.stdin.fileno(), "r", encoding="ascii", errors="surrogateescape"),
        open(sys.stdout.fileno(), "w", encoding="ascii", errors="surrogateescape"),
    )
