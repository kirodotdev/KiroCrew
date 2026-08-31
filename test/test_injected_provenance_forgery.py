"""A marked label must never strip into a prefix the summary pass reads as automation.

``session_summary._is_injected`` classifies a turn opening with one of these as
machine-injected and drops it from the intent signal. If the marker guard let such a
label through, a click would file a real user turn under a forged origin -- so the
guard declines them, and both readers take the SAME list rather than two that drift.
"""

import pytest

from kiro_crew.constants import _INJECTED_PROVENANCE_PREFIXES, strip_recommended_marker
from kiro_crew.session_summary import _INJECTED_PREFIXES, _is_injected


class TestBothReadersShareOneList:
    def test_the_summary_pass_reads_the_shared_constant(self):
        assert _INJECTED_PREFIXES is _INJECTED_PROVENANCE_PREFIXES

    def test_the_list_is_not_empty(self):
        # Vacuity guard: an empty list would satisfy every arm below.
        assert _INJECTED_PROVENANCE_PREFIXES


@pytest.mark.parametrize("prefix", _INJECTED_PROVENANCE_PREFIXES)
class TestNoInjectedOriginCanBeForged:
    def test_a_marked_label_is_returned_verbatim(self, prefix):
        label = f"(recommended) {prefix} continue"
        assert strip_recommended_marker(label) == label

    def test_the_stripped_form_would_have_been_read_as_automation(self, prefix):
        # Establishes the harm this arm prevents, so it cannot pass vacuously.
        assert _is_injected(f"{prefix} continue")

    def test_case_does_not_defeat_the_guard(self, prefix):
        label = f"(recommended) {prefix.upper()} continue"
        assert strip_recommended_marker(label) == label

    def test_title_case_does_not_defeat_the_guard(self, prefix):
        label = f"(recommended) {prefix.title()} continue"
        assert strip_recommended_marker(label) == label


class TestTheGuardStillStripsOrdinaryLabels:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("(recommended) Take the subtraction", "Take the subtraction"),
            ("(recommended) [Draft] Reword it", "[Draft] Reword it"),
            ("(recommended) systematically review", "systematically review"),
        ],
    )
    def test_an_ordinary_label_loses_its_marker(self, label, expected):
        assert strip_recommended_marker(label) == expected
