"""Name what holds the consent-check flag when ``reset_for_testing`` gives up.

``metrics.provider._wait_for_in_flight_consent_worker`` raises once
``_check_in_flight`` outlives its bound. The flag alone cannot say whether a
worker thread is still running, and if so in which step (the config read, a
detached provider's flush, a recorder build), so the error carries the live
stack of every thread under the worker's name, or states that none is alive: a
flag set with no worker behind it is a different defect from a slow worker.
The provider calls it under ``_lock``, which the worker needs to clear the flag,
so a set flag there means its worker is still alive or absent.

Test seam only: it runs on the reset path between tests, never on a request.
"""

from __future__ import annotations

import sys
import threading
import traceback

#: The name ``provider._schedule_consent_check_locked`` gives its worker thread;
#: the module keeps no handle to it, so the name is how it is found.
CONSENT_THREAD_NAME = "kirocrew-telemetry-consent"


def describe_consent_workers() -> str:
    """Return the stack of each live consent worker, or say none is alive."""
    frames = sys._current_frames()
    parts: list[str] = []
    for thread in threading.enumerate():
        frame = frames.get(thread.ident) if thread.ident is not None else None
        if thread.name != CONSENT_THREAD_NAME or frame is None:
            continue
        stack = "".join(traceback.format_stack(frame))
        parts.append(f"{CONSENT_THREAD_NAME!r} (ident {thread.ident}) is at:\n{stack}")
    if not parts:
        return (
            f"no {CONSENT_THREAD_NAME!r} thread is alive, so the in-flight flag "
            "was left set with no worker to clear it"
        )
    return "\n".join(parts)
