"""Tests for the warm-set cap policy (``kiro_crew.instances.warm_set``).

The cap decides how many remote Crew panes, SSH and relay together, stay warm.
The dashboard keeps that many of the most recently opened Crews warm.
"""

from __future__ import annotations

from kiro_crew.instances.constants import (
    DEFAULT_WARM_SET_CAP,
    WARM_SET_CAP_AUTO,
    WARM_SET_CAP_AUTO_CEILING,
)
from kiro_crew.instances.warm_set import resolve_warm_set_cap


class TestAutomatic:
    def test_the_shipped_default_is_automatic(self):
        assert DEFAULT_WARM_SET_CAP == WARM_SET_CAP_AUTO == 0

    def test_automatic_keeps_five_warm(self):
        assert WARM_SET_CAP_AUTO_CEILING == 5
        assert resolve_warm_set_cap(WARM_SET_CAP_AUTO) == 5


class TestExplicit:
    def test_an_explicit_cap_below_automatic_is_not_widened(self):
        # A deliberately tight cap is the only knob that bounds renderer cost;
        # silently widening it would defeat the operator's own trade.
        assert resolve_warm_set_cap(2) == 2

    def test_an_explicit_cap_above_automatic_is_not_clamped(self):
        # Naming a number IS the budget decision, so it is honoured verbatim.
        assert resolve_warm_set_cap(WARM_SET_CAP_AUTO_CEILING + 8) == WARM_SET_CAP_AUTO_CEILING + 8

    def test_an_explicit_one_is_honoured(self):
        assert resolve_warm_set_cap(1) == 1
