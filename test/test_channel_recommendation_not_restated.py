"""The channel seam leaves a drifted marker in the label and restates nothing.

This is the contract the spec now states, and it is a real decision rather than an
omission, so it needs a test that fails if a restatement is ever appended at this seam.
Two facts drive it: no channel producer rule asks for the marker, so one arriving here is
model drift and rendering it as text is base behaviour; and the two-value wrapper drops
the recommendation identity by contract, so the choice a line would name is not knowable
here.
"""

from __future__ import annotations

import pytest

from kiro_crew.messaging.renderer import split_options_trailer

MARKED = "Pick one.\n\n[OPTIONS: (recommended) Ship it | Hold off]"
UNMARKED = "Pick one.\n\n[OPTIONS: Ship it | Hold off]"


class TestTheMarkerRidesIntoTheDispatchedValue:
    def test_the_marker_survives_into_the_choice_as_plain_text(self):
        _, choices = split_options_trailer(MARKED)
        assert choices == ["(recommended) Ship it", "Hold off"]

    def test_an_unmarked_list_is_untouched(self):
        # Guards the guard: identical output either way would make the arm above vacuous.
        assert split_options_trailer(UNMARKED)[1] == ["Ship it", "Hold off"]


class TestNothingIsRestatedInItsPlace:
    def test_the_body_gains_no_line_when_a_marker_was_stripped(self):
        marked_body, _ = split_options_trailer(MARKED)
        unmarked_body, _ = split_options_trailer(UNMARKED)
        assert marked_body == unmarked_body == "Pick one."

    @pytest.mark.parametrize("token", ["Recommended", "recommended", "steer"])
    def test_the_body_names_no_recommendation(self, token):
        marked_body, _ = split_options_trailer(MARKED)
        assert token not in marked_body

    def test_the_withdrawn_symbol_stays_withdrawn(self):
        import kiro_crew.messaging.renderer as renderer

        assert not hasattr(renderer, "recommended_restatement")
        # Positive control: a symbol this module really does export, so the assertion
        # above is about the absent name rather than about a mistyped module reference.
        assert hasattr(renderer, "split_options_trailer")
