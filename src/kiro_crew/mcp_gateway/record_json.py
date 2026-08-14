"""Coercion for values read out of the gateway's on-disk record files.

The hazard ledger and the verdict cache are both JSON files in the runtime
directory, and both are loaded during gateway startup. Neither is a protocol
message, so nothing upstream validates them: anything that can write the
runtime directory decides what these loaders parse. A loader that raises
therefore takes the daemon down before it binds its socket, and a loader that
accepts a non-finite number stores a timestamp no comparison can order.

Both files reach the same values through the same shapes, so the coercion lives
here once rather than in each loader.
"""

from __future__ import annotations

import math
from typing import Any


def finite_float(value: Any, fallback: float = 0.0) -> float:
    """*value* as a finite float, or *fallback* if it cannot be one.

    ``isinstance(value, (int, float))`` is not sufficient to make ``float()``
    safe. JSON parses a bare number with no decimal point into ``int``, which
    has no size limit, and ``float()`` of a large enough one raises
    ``OverflowError`` — an ``ArithmeticError``, so a guard spelled
    ``except (TypeError, ValueError)`` does not catch it.

    Non-finite results are rejected for the same reason, one step later: the
    string form of that number converts to ``inf`` without raising, and an
    ``inf`` timestamp is never older than anything, so an entry carrying one
    would never expire.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    try:
        as_float = float(value)
    except (OverflowError, ValueError):
        return fallback
    return as_float if math.isfinite(as_float) else fallback
