"""Integer fields read from a parsed JSON request body.

``json.loads`` reads ``1e999`` and ``-1e999`` as infinite floats, and ``int`` of
an infinite float raises ``OverflowError``, a type the usual
``except (TypeError, ValueError)`` around a body field does not catch. So a
handler that means to answer a malformed field with its own 400 or its own
fallback raised out of the request instead.
"""

from __future__ import annotations


def body_int(value: object) -> int:
    """Convert a body field with ``int()``, raising ``ValueError`` for an infinite number.

    Every other input behaves exactly as ``int(value)``: a well-formed number or
    numeric string converts, ``None`` raises ``TypeError`` and a non-numeric
    string or NaN raises ``ValueError``. The caller keeps its own
    ``except (TypeError, ValueError)`` and the answer that clause already gives.
    """
    try:
        return int(value)  # type: ignore[call-overload]
    except OverflowError as exc:
        raise ValueError(f"not a finite number: {value!r}") from exc
