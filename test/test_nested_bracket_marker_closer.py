"""A citation bracket inside an unfinished marker must not close it.

The batch path already applies this predicate (`_unclosed_marker_flags`); the streaming twin
took the first closer, so a citation inside an open head cancelled it and the remainder of the
line streamed to the user as answer text.
"""

from __future__ import annotations

from kiro_crew.constants import excise_marker_spans


class TestNestedBracketDoesNotCloseAnUnfinishedMarker:
    def test_unterminated_head_with_a_citation_drops_from_the_head_onward(self):
        """The exact shape GPT graded blocking: no closer of its own, so nothing survives it."""
        text = "Answer.\n[OPTION-ACTIONS: close=See [1] later"
        assert excise_marker_spans(text) == "Answer.\n"

    def test_the_trailing_prose_is_not_released(self):
        """`later` sat after the citation's `]`, which the first-closer scan took as the end."""
        assert " later" not in excise_marker_spans("[OPTION-ACTIONS: close=See [1] later")

    def test_a_terminated_head_containing_a_citation_excises_the_whole_span(self):
        """The marker DOES close here, so only the marker goes and the tail is kept."""
        text = "Answer.\n[OPTION-ACTIONS: close=See [1] now] tail"
        assert excise_marker_spans(text) == "Answer.\n tail"

    def test_a_citation_before_the_head_is_untouched(self):
        """A bracket that never opened a marker is ordinary text."""
        text = "See [1] for context.\n[OPTIONS: Alpha | Bravo]"
        assert excise_marker_spans(text) == "See [1] for context.\n"

    def test_two_nested_citations_still_need_the_marker_s_own_closer(self):
        text = "[OPTIONS: a [1] b [2] c] kept"
        assert excise_marker_spans(text) == " kept"

    def test_a_stray_closer_cannot_end_a_head_it_never_opened(self):
        """Depth cannot go below zero from text preceding the head."""
        text = "prose] more\n[OPTIONS: a [1] b"
        assert excise_marker_spans(text) == "prose] more\n"

    def test_the_seam_case_the_docstring_names_still_holds(self):
        """Excising the inner marker forms `[OPTIONS: b]` at the join; it must also go."""
        assert excise_marker_spans("[OPTI[OPTIONS: a]ONS: b]") == ""

    def test_linear_on_many_markers(self):
        """Each character is scanned by at most one closer walk, so this stays cheap."""
        text = "[OPTIONS: a [1] b] x " * 2000
        assert excise_marker_spans(text) == " x " * 2000
