"""The channel-resume retraction is owed by the RESTORED HISTORY, not by the rules block.

A slim resume re-anchors the critical rules, and the channel variant deliberately omits the
option-marking rule. Omission is not retraction: the restored transcript still carries the
dashboard instruction, so a note saying it does not apply here has to ship. That note was
chained onto the rules string, so an agent opted out of crew context got an empty string and
no retraction at all -- while still resuming with the instruction in its history.
"""

from __future__ import annotations

import inspect

from kiro_crew import context


def _flat() -> str:
    """Source with whitespace collapsed, so a formatter's line breaks cannot matter."""
    return " ".join(inspect.getsource(context).split())


class TestTheRetractionIsNotGatedOnTheRulesBlock:
    def test_the_variant_is_resolved_independently_of_the_opt_out(self):
        flat = _flat()
        assert "_resume_variant = _critical_rules_for(session_key, runtime_source)" in flat
        assert (
            '_resume_rules = _resume_variant if _agent_includes_crew_context(agent) else ""' in flat
        )

    def test_the_retraction_keys_on_the_variant_not_the_rules_string(self):
        flat = _flat()
        assert "if _resume_variant is _CRITICAL_RULES_CHANNEL:" in flat
        # The defect: `_resume_rules and ...` made an opted-out agent skip the retraction.
        assert "if _resume_rules and _resume_rules is _CRITICAL_RULES_CHANNEL:" not in flat

    def test_the_retraction_text_still_ships(self):
        assert "does NOT apply here" in _flat()

    def test_the_retraction_never_spells_the_marker(self):
        """`_CRITICAL_RULES_CHANNEL` must never carry the literal, so nor may its retraction."""
        start = _flat().index("does NOT apply here")
        window = _flat()[start - 400 : start + 400]
        assert "(recommended)" not in window
