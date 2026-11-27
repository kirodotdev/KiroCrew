"""Background flush of a detached meter provider.

The config watcher's telemetry applier detaches the live provider and normally
returns at once: a dashboard settings write waits for that applier, and a
provider flush joins every reader's export (a network POST per OTLP
destination, up to its 30 s deadline). So the applier hands the flush to a
daemon thread here. If starting it fails, the caller must arrange an off-thread
fallback. Started threads are tracked only so the test suite can wait for them
(:func:`join_all`); production never joins them, so an exit while one runs loses
that provider's last export, the cost ``_flush_detached_provider`` already accepts.
"""

from __future__ import annotations

import time
from threading import Lock, Thread
from typing import Any, Callable

_lock = Lock()
_threads: "list[Thread]" = []


def flush_in_background(doomed: Any, flush: "Callable[[Any], None]") -> bool:
    """Start a daemon flush without waiting; never call ``flush`` inline.

    Return True when started or ``doomed`` is None, False if starting raises
    RuntimeError. The caller owns an off-thread fallback. *flush* must never raise.
    """
    if doomed is None:
        return True
    thread = Thread(target=flush, args=(doomed,), name="kirocrew-telemetry-flush", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        return False
    with _lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
        _threads.append(thread)
    return True


def join_all(timeout: float) -> bool:
    """Wait up to *timeout* seconds for every flush started here; True when none is left."""
    deadline = time.monotonic() + timeout
    with _lock:
        pending = list(_threads)
    for thread in pending:
        thread.join(max(0.0, deadline - time.monotonic()))
    with _lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
        return not _threads
