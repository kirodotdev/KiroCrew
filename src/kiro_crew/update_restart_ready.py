"""The "update ready, restart to use it" state.

A running gateway is never restarted by an update (the decision entry
"updates never restart a running gateway" in ``docs/decisions``).
What the paths can do instead is leave a note here: an update was
staged beside the running install (by the scheduler, or by an approved in-app
wheel), or the running install's served assets are gone, and a person decides when
to restart. The dashboard reads the note through
the status frame (``update_restart_ready*``) and shows it on the update pill and
in the update dialog; the next user stop or restart, or a fresh start, is what
brings the new version in.

The note is process-global and in memory on purpose: a restart replaces the
process, and with it the reason the note exists.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: A new version was built and promoted beside the running install.
REASON_STAGED = "staged"
#: The served dashboard bundle vanished under the running process.
REASON_ASSETS_GONE = "assets_gone"


@dataclass(frozen=True)
class RestartReady:
    """What is waiting for a restart, and why."""

    reason: str
    version: str = ""
    #: A policy floor requires it: the dialog stays prominent (it cannot be skipped).
    mandatory: bool = False
    since: float = 0.0


_lock = threading.Lock()
_current: RestartReady | None = None


def mark(reason: str, *, version: str = "", mandatory: bool = False) -> bool:
    """Record that a restart would bring in an update. ``True`` when the note changed."""
    global _current
    note = RestartReady(reason, version, mandatory, time.time())
    with _lock:
        old = _current
        if old is not None and (old.reason, old.version, old.mandatory) == (
            reason,
            version,
            mandatory,
        ):
            return False
        _current = note
    logger.info(
        "Update ready, restart to use it: reason=%s version=%s mandatory=%s",
        reason,
        version or "-",
        mandatory,
    )
    return True


def clear(reason: str | None = None) -> bool:
    """Drop the note, or only a note of *reason*. ``True`` when one was dropped."""
    global _current
    with _lock:
        if _current is None or (reason is not None and _current.reason != reason):
            return False
        _current = None
    return True


def current() -> RestartReady | None:
    """The note, or ``None`` while nothing waits for a restart."""
    with _lock:
        return _current


def status_fields() -> dict[str, object]:
    """The status-frame fields: whether a restart brings in an update, which, and why."""
    note = current()
    return {
        "update_restart_ready": note is not None,
        "update_restart_ready_version": note.version if note else "",
        "update_restart_ready_reason": note.reason if note else "",
    }
