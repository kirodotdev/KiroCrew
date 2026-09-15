"""The relation among the marker head SPELLINGS, pinned at this tip.

``constants`` carries three head alternations that deliberately differ, and until now
the source only ASSERTED they stay in step. Each row below is a measured property, so a
change to one spelling that forgets another turns red here instead of silently changing
how a shipped ``[OPTIONS:]`` marker parses. The three, and why they are not one:
* :data:`_MARKER_BODY_TEMPER` -- what a tempered BODY refuses to cross. Carries the
  singular ``OPTION:`` because the frontend's own head is ``OPTION(S)?:`` and its pair
  guard refuses the singular too, so accepting it here would render a pill whose label
  holds a raw protocol head the frontend declines to parse.
* :data:`_MARKER_HEAD_ALT` -- what counts AS a head. Feeds the line tail's sibling
  lookahead, the trailer head scan and the mid-prose strip. The singular is NOT here:
  it would be stripped from speech and previews and every shipped marker would reparse.
* :data:`_MARKER_SUPPRESSION_HEAD_RE` -- the UNCLOSED-head scan. Widest, and
  case-insensitive: a narrower scan accepts an action the frontend refuses.
"""

import re

from kiro_crew.constants import (
    _CASE_INSENSITIVE_MARKER_HEADS,
    _MARKER_BODY_TEMPER,
    _MARKER_HEAD_ALT,
    _MARKER_HEADS,
    _MARKER_SUPPRESSION_HEAD_RE,
)

TEMPER_RE = re.compile(_MARKER_BODY_TEMPER)
HEAD_ALT_RE = re.compile(_MARKER_HEAD_ALT)

SINGULAR_HEAD = "OPTION:"


def _refuses(candidate: str) -> bool:
    """Whether the temper's negative lookahead declines *candidate*."""
    return TEMPER_RE.match(candidate) is None


class TestTheTemperRefusesEveryHeadPlusTheSingular:
    def test_every_declared_head_is_refused(self):
        for head in _MARKER_HEADS:
            assert _refuses(head), head

    def test_the_singular_content_head_is_refused_for_frontend_parity(self):
        assert _refuses(SINGULAR_HEAD)

    def test_positive_control_ordinary_label_text_is_not_refused(self):
        for allowed in ("hello", "OPTIONX:", "x] | Skip", "arr[0"):
            assert not _refuses(allowed), allowed


class TestOnlyDeclaredHeadsCountAsHeads:
    def test_head_alt_matches_every_declared_head(self):
        for head in _MARKER_HEADS:
            assert HEAD_ALT_RE.match(head), head

    def test_head_alt_does_not_admit_the_singular(self):
        # The temper refuses it, but it is not a head: admitting it here would
        # reach the line tail, the trailer scan and the mid-prose strip.
        assert HEAD_ALT_RE.match(SINGULAR_HEAD) is None
        assert _refuses(SINGULAR_HEAD)

    def test_the_case_insensitive_set_is_a_subset_of_the_declared_heads(self):
        assert _CASE_INSENSITIVE_MARKER_HEADS <= set(_MARKER_HEADS)

    def test_head_alt_honours_the_per_head_casing_rule(self):
        for head in _MARKER_HEADS:
            insensitive = head in _CASE_INSENSITIVE_MARKER_HEADS
            assert bool(HEAD_ALT_RE.match(head.lower())) is insensitive, head


class TestTheSuppressionScanIsTheWidestSpelling:
    def test_it_carries_every_declared_head(self):
        for head in _MARKER_HEADS:
            assert _MARKER_SUPPRESSION_HEAD_RE.match(f"[{head}"), head

    def test_it_carries_the_singular_too(self):
        assert _MARKER_SUPPRESSION_HEAD_RE.match(f"[{SINGULAR_HEAD}")

    def test_it_is_case_insensitive(self):
        assert _MARKER_SUPPRESSION_HEAD_RE.flags & re.IGNORECASE
        for head in (*_MARKER_HEADS, SINGULAR_HEAD):
            assert _MARKER_SUPPRESSION_HEAD_RE.match(f"[{head.lower()}"), head

    def test_positive_control_a_non_head_is_not_matched(self):
        assert _MARKER_SUPPRESSION_HEAD_RE.match("[OPTIONX:") is None


class TestTheTemperIsCaseSensitiveOnTheContentHead:
    """The content head's case rule diverges from the frontend DELIBERATELY.

    Widening it would change how every shipped ``[OPTIONS:]`` marker parses on
    every streamed channel message, so the divergence is a called-out follow-up
    rather than part of this change. Pinned so a later widening is a decision
    somebody makes on purpose.
    """

    def test_a_lowercase_content_head_is_not_refused(self):
        assert not _refuses("options:")
        assert not _refuses("option:")

    def test_the_action_head_follows_its_own_declared_rule(self):
        # Case-SENSITIVE in the temper's own spelling. Safe only because two
        # independent rescues cover it -- both pinned in the riders file.
        assert "OPTION-ACTIONS:" in _CASE_INSENSITIVE_MARKER_HEADS
        assert _refuses("OPTION-ACTIONS:")
