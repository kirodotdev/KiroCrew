"""Durable per-install count of genuine user-initiated chat sessions.

Gates the session-pulse survey's "new user" window: the survey must not appear
until this install has started at least ``NEW_USER_SESSION_THRESHOLD`` genuine
user chats (``SlotOrigin.USER``). Counting only user-origin sessions -- not
cron, app, system, or restored-untagged slots -- keeps the gate a measure of
real human engagement, matching the only surface the survey ever shows on.

The count is persisted next to the other dashboard state files via
``config_dir()`` so it honors ``KIROCREW_HOME`` and survives restarts. The
in-memory ``_slot_counter`` in state.py cannot serve this: it resets to 0 on
every gateway boot and only reseeds past currently-live slots, so it is not a
lifetime count.

Both functions are best-effort and never raise into their callers: a survey
timing gate must not be able to break session creation or the eligibility
check. On any read/write trouble they log and fall back (read -> 0, which keeps
a new-ish install gated shut rather than surfacing the survey on bad data).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_FILE = "session_pulse_sessions.json"
_KEY = "user_sessions"

# Serializes the counter's read-modify-write. Needed because the increment is
# scheduled into an executor (see `increment_user_session_count_off_loop`), so it
# is no longer serialized by the event loop the way an inline call was.
_WRITE_LOCK = threading.Lock()

# Minimum genuine user-initiated chats before the survey may appear. A person
# below this is treated as a new user and never surveyed -- their rating would
# reflect a first impression, not real use.
NEW_USER_SESSION_THRESHOLD = 10


def _path() -> Path:
    return config_dir() / _FILE


def get_user_session_count() -> int:
    """Best-effort read of the durable user-session count (0 if unset/unreadable)."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except Exception:
        logger.warning("session-pulse session count unreadable; treating as 0", exc_info=True)
        return 0
    # Valid but non-object JSON (e.g. ``null``, ``5``, ``[]``) parses fine yet has
    # no ``.get`` -- guard it here so a hand-edited or truncated-then-valid file
    # can never raise into the session-creation path that calls this.
    if not isinstance(data, dict):
        return 0
    val = data.get(_KEY, 0)
    return val if isinstance(val, int) and val >= 0 else 0


def increment_user_session_count() -> int:
    """Increment the durable count by one and return the new value.

    Atomic write (temp file + ``os.replace``) so a crash mid-write cannot
    corrupt the file. On write failure this logs and returns the pre-increment
    value rather than raising into the session-creation path.

    The read-modify-write is held under ``_WRITE_LOCK``. Inline on the event loop
    that was free -- births are serialized by the loop -- but
    :func:`increment_user_session_count_off_loop` runs this in an executor, where
    two births could otherwise interleave their read and write and drop a count.
    Cross-PROCESS interleaving (two gateways sharing a home) is unchanged and
    still only guaranteed not to corrupt the file, not to be exact.
    """
    with _WRITE_LOCK:
        current = get_user_session_count()
        new_val = current + 1
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{_FILE}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({_KEY: new_val}, fh)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except Exception:
            logger.warning("failed to persist session-pulse session count", exc_info=True)
            return current
        return new_val


def increment_user_session_count_off_loop() -> None:
    """Count one user session without doing its disk I/O on the event loop.

    ``get_or_create_slot`` is synchronous and runs on the gateway loop for every
    request-layer slot birth -- a new chat tab, a fork, and the session-control
    create verb all reach it -- so doing this module's read, ``mkdir``, tempfile
    write and ``os.replace`` inline stalls the whole loop on slow or contended
    storage.

    Nothing reads the count until the survey's own eligibility check, and the
    increment is already best-effort (it swallows its own I/O errors and returns
    the pre-increment value), so a slot birth has no reason to wait for it.

    Deliberately fixed HERE rather than by making the caller async: the
    allocation inside ``get_or_create_slot`` must not suspend part-way, because
    callers depend on the slot being fully configured before anything else can
    observe it. Offloading the allocation would reintroduce exactly that window;
    offloading the I/O does not.

    Fire-and-forget by design: the future is not awaited, which is safe only
    because the callee cannot raise. A count lost to shutdown racing the executor
    is acceptable for a survey timing gate and matches the best-effort contract
    the module already documents.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop: a test, a CLI, or a background thread. Nothing to protect.
        increment_user_session_count()
        return
    loop.run_in_executor(None, increment_user_session_count)
