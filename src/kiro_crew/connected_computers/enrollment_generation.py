"""Bounded enrollment identity for generation-scoped remote cleanup."""

from __future__ import annotations

from typing import Any, Final

#: Matches the maximum decimal width carried by connector credentials.
MAX_GENERATION: Final[int] = 10**18 - 1


def enrollment_generation(value: Any) -> int:
    """Return a valid wire/storage generation, or zero for an unscoped legacy request."""
    return value if type(value) is int and 0 < value <= MAX_GENERATION else 0
