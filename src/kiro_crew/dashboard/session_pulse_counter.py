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

import json
import logging
import os
import tempfile
from pathlib import Path

from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_FILE = "session_pulse_sessions.json"
_KEY = "user_sessions"

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
    """
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
