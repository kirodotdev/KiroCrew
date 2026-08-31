"""A resumed CHANNEL turn must retract the dashboard's option-marking instruction.

The two critical-rules blocks are runtime-selected, and the channel one omits the marker
rule. Omission is enough for a fresh channel session, which never saw the rule. It is NOT
enough for a session that STARTED on the dashboard and resumed on a channel: the restored
transcript still carries the dashboard instruction, and a channel sends a label as written,
so a marked label would put the prefix into the user's own message.

The retraction must not spell the marker grammar -- `test_context_critical_rules_assembly`
pins that the channel contract never teaches it.
"""

from __future__ import annotations

import inspect

from kiro_crew import context as ctx


def _resume_source() -> str:
    """The slim-resume branch, where the rules are re-anchored on a restored session."""
    src = inspect.getsource(ctx)
    anchor = "Re-anchor the critical rules"
    assert anchor in src, "the slim-resume re-anchor moved -- re-anchor this test"
    return src[src.index(anchor) : src.index(anchor) + 2200]


class TestTheChannelResumeRetractsTheMarker:
    def test_the_retraction_exists_on_the_resume_path(self):
        assert "does NOT apply here" in _resume_source()

    def test_it_is_gated_to_the_channel_variant(self):
        """A dashboard resume must keep the rule -- the retraction is channel-only.

        Keyed on the VARIANT rather than the assembled rules string: gating on the latter
        skipped the retraction for an agent opted out of crew context, whose restored history
        still carried the dashboard instruction.
        """
        body = _resume_source()
        assert "_resume_variant is _CRITICAL_RULES_CHANNEL" in body, body[:400]

    def test_it_names_the_reason_the_prefix_is_unsafe_here(self):
        body = _resume_source()
        assert "sends a label as written" in body

    def test_it_offers_the_surviving_alternative(self):
        """Retracting without an alternative would leave no way to steer at all."""
        assert "order the options best-first" in _resume_source()


class TestTheRetractionDoesNotTeachTheGrammar:
    def test_the_channel_block_still_excludes_the_marker(self):
        assert "(recommended)" not in ctx._CRITICAL_RULES_CHANNEL

    def test_the_retraction_text_does_not_spell_the_marker(self):
        body = _resume_source()
        start = body.index("This runtime does not use")
        retraction = body[start : body.index("]\\n", start)]
        assert "(recommended)" not in retraction, retraction

    def test_the_dashboard_block_still_carries_the_rule(self):
        assert "(recommended)" in ctx._CRITICAL_RULES
