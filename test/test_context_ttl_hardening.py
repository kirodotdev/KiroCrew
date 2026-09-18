"""A malformed pending-context TTL must not raise out of the expiry check.

``context_entry_expired`` did the arithmetic unguarded, so a ``maxAge`` that is not a
finite number raised ``TypeError`` out of every caller — and a NaN silently compared
False forever, making an unparseable entry immortal. The boundary validators guard the
live enqueue only; an entry rehydrated from an operator-editable metadata line never
passes through them.
"""

from __future__ import annotations

import math
import time

import pytest

#: Arbitrary-precision, so ``math.isfinite``'s float conversion raises rather than answering.
_HUGE_INT = 10**400


@pytest.mark.parametrize("bad", ["60", [1], True, float("nan"), float("inf")])
def test_context_entry_expired_never_raises_on_a_bad_max_age(bad):
    """Hardened at the arithmetic itself, so every caller is protected.

    A malformed value reports EXPIRED rather than "never expires": unparseable
    data must be pruned, not made immortal.
    """
    from kiro_crew.dashboard.state import context_entry_expired

    assert context_entry_expired({"content": "x", "maxAge": bad}, time.time()) is True


def test_context_entry_expired_never_raises_on_a_bad_injected_at():
    from kiro_crew.dashboard.state import context_entry_expired

    entry = {"content": "x", "maxAge": 60, "injectedAt": "nope"}
    assert context_entry_expired(entry, time.time()) is True


def test_finite_number_survives_an_arbitrary_precision_int():
    """`math.isfinite` raises OverflowError here; the guard must report False."""
    from kiro_crew.dashboard.state import _finite_number

    with pytest.raises(OverflowError):
        math.isfinite(_HUGE_INT)  # the defect this pins, still live in the stdlib call
    assert _finite_number(_HUGE_INT) is False
    assert _finite_number(-_HUGE_INT) is False


def test_context_entry_expired_survives_an_arbitrary_precision_ttl():
    from kiro_crew.dashboard.state import context_entry_expired

    assert context_entry_expired({"content": "x", "maxAge": _HUGE_INT}, time.time()) is True
    entry = {"content": "x", "maxAge": 60, "injectedAt": _HUGE_INT}
    assert context_entry_expired(entry, time.time()) is True


def test_a_live_entry_is_still_not_expired():
    """Positive control: the hardening must not report EXPIRED for a well-formed entry."""
    from kiro_crew.dashboard.state import context_entry_expired

    now = time.time()
    assert context_entry_expired({"content": "x", "maxAge": 60, "injectedAt": now}, now) is False
    assert context_entry_expired({"content": "x", "maxAge": None}, now) is False
    assert (
        context_entry_expired({"content": "x", "maxAge": 1, "injectedAt": now - 300}, now) is True
    )
