"""The strip guard must fence an embedded ``$name`` token, as the expander resolves one.

The expander matches skill tokens ANYWHERE in a message, so a guard that only inspects the first
character cannot see ``Run the $deploy skill``: the marker strips clean, the label dispatches, and
a local skill body loads without the user having seen the text.
"""

from __future__ import annotations

from kiro_crew.constants import _EMBEDDED_SKILL_TOKEN_RE, strip_recommended_marker
from kiro_crew.skills import _DOLLAR_SKILL_PATTERN


class TestEmbeddedSkillTokenIsFenced:
    def test_the_marker_survives_an_embedded_token(self):
        label = "(recommended) Run the $deploy skill"
        assert strip_recommended_marker(label) == label

    def test_the_marker_survives_a_trailing_token(self):
        label = "(recommended) Deploy with $prod-release"
        assert strip_recommended_marker(label) == label

    def test_an_ordinary_label_still_strips(self):
        """Positive control: the fence must not swallow every label."""
        assert (
            strip_recommended_marker("(recommended) Run the tests again") == "Run the tests again"
        )

    def test_the_fence_accepts_what_the_expander_accepts(self):
        """The two patterns must agree, or the fence is narrower than the thing it fences."""
        for probe in ("$deploy", "Run the $deploy skill", "$a", "$40", "mid$word"):
            assert bool(_EMBEDDED_SKILL_TOKEN_RE.search(probe)) == bool(
                _DOLLAR_SKILL_PATTERN.search(probe)
            ), probe

    def test_a_doubled_sigil_is_not_a_token_on_either_side(self):
        assert not _EMBEDDED_SKILL_TOKEN_RE.search("$$deploy")
        assert not _DOLLAR_SKILL_PATTERN.search("$$deploy")
