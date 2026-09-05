"""The streaming marker filter must stay linear on bracket-heavy output.

A held run that looks like a marker is deliberately NOT released at the first ``]`` —
a label may legitimately contain one — so it keeps growing until the newline. Every
``]`` in between re-tests the whole accumulated run, and the case-insensitive head
folded that entire run to lower case to do it. One line of agent prose carrying many
closing brackets therefore cost O(n^2), which is reachable from ordinary model output
rather than only from a crafted payload.

The head test only ever needs the FIRST few characters, so the work per ``]`` must not
grow with the hold.
"""

import time

import pytest

from kiro_crew.constants import MARKER_PREFIXES
from kiro_crew.slack.handler import (
    _filter_options_brackets,
    _is_marker_candidate,
    _marker_head_needle,
)


class _CountingStr(str):
    """A hold that records every full-run copy taken of it."""

    def __new__(cls, value: str) -> "_CountingStr":
        obj = super().__new__(cls, value)
        obj.lowered = []
        return obj

    def lower(self) -> str:  # type: ignore[override]
        self.lowered.append(len(self))
        return str.lower(self)


class TestMarkerFilterStaysLinear:
    def test_head_test_does_not_copy_the_whole_hold(self):
        """The bound, asserted deterministically rather than by the clock.

        A timing assertion alone would pass under a fast machine and a slow bug, so the
        real invariant is pinned directly: no step of the head test may take a copy
        proportional to the hold.
        """
        hold = _CountingStr("[OPTIONS: " + "x]" * 5_000)
        assert _is_marker_candidate(hold) is True
        assert hold.lowered == [], (
            "the head test copied the whole %d-char hold %r time(s); it must look at no "
            "more than the head" % (len(hold), len(hold.lowered))
        )

    def test_needle_haystack_pair_is_bounded_by_the_needle(self):
        for prefix in MARKER_PREFIXES:
            needle, haystack = _marker_head_needle(prefix, "[OPTIONS: " + "y]" * 4_000)
            assert len(haystack) <= max(
                len(needle), len(prefix) + 1
            ), "%s: haystack is %d chars for a %d-char needle" % (
                prefix,
                len(haystack),
                len(needle),
            )

    def test_scales_linearly_on_a_bracket_heavy_candidate_line(self):
        """Doubling the closers must not quadruple the work.

        Measured against the SAME path at half size rather than a fixed wall-clock
        budget, so a slow or loaded machine moves both terms together.
        """

        def run(closers: int) -> float:
            text = "[OPTIONS: " + ("x]" * closers) + "\n"
            best = min(_time_once(text) for _ in range(3))
            return best

        small = run(8_000)
        large = run(16_000)
        if small <= 0:
            pytest.skip("timer resolution too coarse to compare")
        assert large / small < 2.8, (
            "doubling the closers multiplied the work by %.2f, which is quadratic rather "
            "than linear (%.4fs -> %.4fs)" % (large / small, small, large)
        )

    @pytest.mark.parametrize(
        "text,expect_suppressed",
        [
            ("[OPTIONS: a | b]\n", True),
            ("[options: a | b]\n", False),
            ("[OPTION-ACTIONS: close=Done]\n", True),
            ("[option-actions: close=Done]\n", True),
            ("[OPTION-ACTIONS: close=Done (see [1])]\n", True),
            ("[foo] ordinary prose\n", False),
        ],
    )
    def test_behaviour_is_unchanged_by_the_bound(self, text, expect_suppressed):
        """The per-head casing rule survives the optimisation.

        ``OPTIONS`` is case-SENSITIVE and ``OPTION-ACTIONS`` is not, so a lowercase
        ``[options:`` must still be released: holding more than the batch parser strips
        is what produced the raw pop-in when the final message replaced the stream.
        """
        _, released = _filter_options_brackets(text, "", "")
        if expect_suppressed:
            assert "OPTIONS:" not in released.upper() or "ordinary" in released
        else:
            assert text.strip().split("\n")[0][:9].lower() in released.lower()


def _time_once(text: str) -> float:
    start = time.perf_counter()
    _filter_options_brackets(text, "", "")
    return time.perf_counter() - start
