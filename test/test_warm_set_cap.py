"""Tests for the warm-set cap policy (``kiro_crew.instances.warm_set``).

The cap decides how many remote-crew panes stay mounted. It matters because
eviction is indistinguishable from a disconnect at the pane -- the iframe is
unmounted and the remote SPA cold-boots on the next click -- so a cap below the
number of crews in use reads to the user as a connection that flaps on tab
switch.
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

    def test_automatic_matches_the_registered_count(self):
        # The whole point: every configured crew keeps its pane, so switching
        # between them never evicts one and never looks like a disconnect.
        for registered in (1, 2, 3, 4, 7):
            assert resolve_warm_set_cap(WARM_SET_CAP_AUTO, registered) == registered

    def test_automatic_is_bounded_by_the_ceiling(self):
        # A large fleet must not mount an unbounded number of dashboard SPAs in
        # one renderer, so eviction resumes past the ceiling.
        assert (
            resolve_warm_set_cap(WARM_SET_CAP_AUTO, WARM_SET_CAP_AUTO_CEILING + 5)
            == WARM_SET_CAP_AUTO_CEILING
        )

    def test_automatic_never_resolves_below_one(self):
        # The active pane is always warm, so 0 is a cap the viewport cannot
        # honour -- it would evict the pane the user is looking at.
        assert resolve_warm_set_cap(WARM_SET_CAP_AUTO, 0) == 1

    def test_a_negative_registered_count_is_not_trusted(self):
        assert resolve_warm_set_cap(WARM_SET_CAP_AUTO, -2) == 1


class TestAdmitsEveryRegisteredCrew:
    """The regression this policy exists to prevent.

    Auto used to resolve from the LIVE CONNECTED count, which made the cap race
    tunnel startup. With four crews configured and the fourth still connecting
    when the dashboard polled, the cap came back 3, the viewport evicted a pane
    to honour it, and one crew looked broken -- a different one each restart,
    since it depended on which tunnel finished last.
    """

    def test_a_crew_still_connecting_does_not_shrink_the_cap(self):
        # Four registered, however many happen to be up at this instant.
        assert resolve_warm_set_cap(WARM_SET_CAP_AUTO, 4) == 4

    def test_adding_a_crew_widens_the_cap_without_touching_config(self):
        # Otherwise the operator has to remember to raise the cap alongside, and
        # forgetting reintroduces the eviction that looks like a random failure.
        assert resolve_warm_set_cap(WARM_SET_CAP_AUTO, 5) > resolve_warm_set_cap(
            WARM_SET_CAP_AUTO, 4
        )

    def test_a_registered_but_never_connected_crew_still_gets_a_slot(self):
        # A crew is registered by configuring it, not by connecting it. Budgeting
        # only for live tunnels is precisely what made the cap arrive one short.
        assert resolve_warm_set_cap(WARM_SET_CAP_AUTO, 2) == 2


class TestExplicit:
    def test_an_explicit_cap_wins_over_the_registered_count(self):
        assert resolve_warm_set_cap(3, 9) == 3

    def test_an_explicit_cap_below_the_registered_count_is_not_widened(self):
        # A deliberately tight cap is the only knob that bounds renderer cost;
        # silently widening it would defeat the operator's own trade.
        assert resolve_warm_set_cap(2, 4) == 2

    def test_an_explicit_cap_above_the_ceiling_is_not_clamped(self):
        # The ceiling only bounds the automatic mode. Naming a number IS the
        # budget decision, so it is honoured verbatim.
        assert (
            resolve_warm_set_cap(WARM_SET_CAP_AUTO_CEILING + 8, 30) == WARM_SET_CAP_AUTO_CEILING + 8
        )

    def test_an_explicit_one_is_honoured(self):
        assert resolve_warm_set_cap(1, 6) == 1
