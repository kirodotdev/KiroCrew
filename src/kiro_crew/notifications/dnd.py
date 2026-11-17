"""Gateway-wide timed Do Not Disturb.

One end time for the whole gateway, stored in
``~/.kiro/crew/notification_dnd.json`` as ``{"dnd_until": <epoch seconds>}``.
While the clock is before it, the delivery sink treats every note that is not
urgent like a muted channel's: it stays in history but is stamped
``silenced`` and forced ``passive``, so no badge, sound, banner, Web Push or
chat bridge delivery is made for it. Nothing is held back to replay later.

Do Not Disturb ends on its own: :meth:`DoNotDisturb.until` compares the stored
end time with the clock on every read, so no timer runs and nothing is written
when it runs out.

An end time more than :data:`MAX_DND_SECS` ahead is treated as off. The
dashboard never offers one that long, so such a value comes from a clock that
was moved back after Do Not Disturb started, or from a hand edit. Reading it as
off lets notifications through rather than hiding them for days. A file that
cannot be read or holds anything but a finite number is read as off too.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger(__name__)

_FILENAME = "notification_dnd.json"

#: Longest Do Not Disturb the gateway accepts or honours. The longest dashboard
#: choice, "until 8:00", is under 24 hours; one more hour covers a daylight
#: saving change inside that span.
MAX_DND_SECS = 25 * 3600


class DndError(ValueError):
    """An end time the gateway refuses (not a number, past, or too far ahead)."""


def _path():
    return config_dir() / _FILENAME


def active_until(raw: Any, now: float) -> float | None:
    """*raw* as a running end time at *now*, or ``None`` when it does not suppress."""
    if not _is_finite_number(raw):
        return None
    if raw <= now or raw > now + MAX_DND_SECS:
        return None
    return float(raw)


def _read() -> Any:
    path = _path()
    if not path.exists():
        return None
    try:
        data = loads_user_json(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read %s; Do Not Disturb is off", path, exc_info=True)
        return None
    raw = data.get("dnd_until") if isinstance(data, dict) else None
    if raw is not None and not _is_finite_number(raw):
        logger.warning("%s holds a dnd_until that is not a number (%r); read as off", path, raw)
    return raw


def _is_finite_number(raw: Any) -> bool:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return False
    try:
        return math.isfinite(raw)
    except OverflowError:
        # An int too large for a float (JSON keeps every digit) is no end time.
        return False


class DoNotDisturb:
    """The gateway's one Do Not Disturb end time; owned by ``DashboardState``."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._raw: Any = _read()

    def until(self) -> float | None:
        """The running end time, or ``None`` when Do Not Disturb is off."""
        return active_until(self._raw, self._clock())

    def suppresses(self) -> bool:
        """Whether a note that is not urgent is silenced now."""
        return self.until() is not None

    def set(self, until: Any) -> float | None:
        """Start Do Not Disturb until *until* (epoch seconds), or end it with ``None``.

        Persists before memory changes, so a failed write leaves the old state.
        Raises :class:`DndError` for an end time that is not a finite number, is
        not in the future, or is more than :data:`MAX_DND_SECS` ahead.
        """
        now = self._clock()
        if until is not None:
            if not _is_finite_number(until):
                raise DndError("until must be a number of epoch seconds or null")
            if until <= now:
                raise DndError("until must be in the future")
            if until > now + MAX_DND_SECS:
                raise DndError(f"until must be at most {MAX_DND_SECS // 3600} hours ahead")
            until = float(until)
        with self._lock:
            atomic_write(_path(), json.dumps({"dnd_until": until}))
            self._raw = until
        return until
