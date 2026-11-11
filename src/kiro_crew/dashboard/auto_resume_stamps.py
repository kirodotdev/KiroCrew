"""The persisted automatic-resume budget list, validated the way the codec reads it.

``auto_resume_stamps`` on the metadata line holds the wall-clock stamps of the
automatic resumes a chat spent (``restart_resume``). The line is a writable file, so
the list is re-checked field by field on every restore. This module holds only
that validation and its bounds, apart from the dispatcher: the codec reads it for
every restored slot, with the switch on or off, and must not pull the dispatcher
in behind it (AUTOSDE ``no-new-work-on-gateway-boot-path``). It sits beside the
codec's facade rather than among its owners, which import nothing of each other.
"""

from __future__ import annotations

import math
import time

#: The persisted stamp list is bounded well above any limit the config allows
#: (20), so a list longer than this is not something this gateway wrote.
MAX_STAMPS = 64

#: A stamp this far in the future is not a clock skew this gateway produced.
_FUTURE_SLACK_SECS = 300.0


def read_persisted_stamps(raw: object, *, now: float | None = None) -> tuple[list[float], bool]:
    """Validate a metadata-line ``auto_resume_stamps`` value: ``(stamps, valid)``.

    The metadata line is a writable file, so every element is re-checked. Anything
    that is not a list of finite, non-negative, non-future floats within
    :data:`MAX_STAMPS` -- including an integer too large to be a float, which
    ``float()`` refuses with ``OverflowError`` -- reads as a FULL list:
    ``MAX_STAMPS`` copies of *now*, which makes the budget refuse the automation
    for one window from now (the fail-closed direction for an automatic
    dispatcher; a person's send or Resume clears it sooner) and never touches the
    manual Resume. Never raises on a bad value: this runs inside the slot restore,
    where an exception would turn a tampered line into a chat that cannot be
    opened. ``valid`` is False for the bad case, so the reader can say which chat
    and why.
    """
    if raw is None:
        return [], True
    current = time.time() if now is None else now
    full = [current] * MAX_STAMPS
    if not isinstance(raw, list) or len(raw) > MAX_STAMPS:
        return full, False
    stamps: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return full, False
        try:
            value = float(item)
        except (OverflowError, ValueError):
            return full, False
        if not math.isfinite(value) or value < 0 or value > current + _FUTURE_SLACK_SECS:
            return full, False
        stamps.append(value)
    return stamps, True
