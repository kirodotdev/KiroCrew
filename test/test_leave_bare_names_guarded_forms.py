"""The leave-bare instruction must name every form the guard actually keeps.

The producer rule and the strip guard are two halves of one contract. Where the guard keeps a
marker the instruction does not mention, an agent marks that label in good faith and the user
sees a raw prefix with no badge -- the instruction is the only thing steering the producer, so a
form missing from it is a form that reaches users unbadged.
"""

from __future__ import annotations

from kiro_crew.constants import _RESERVED_DISPATCH_SIGILS
from kiro_crew.context import _OPTIONS_RECOMMENDED_RULE


class TestLeaveBareNamesEveryGuardedForm:
    def test_the_rule_text_is_reachable(self):
        """Guards the guard: every absence below is measured on the real instruction."""
        assert "Leave those bare" in _OPTIONS_RECOMMENDED_RULE

    def test_every_reserved_sigil_appears_in_the_instruction(self):
        missing = [
            s for s in _RESERVED_DISPATCH_SIGILS if f"`{s}`" not in _OPTIONS_RECOMMENDED_RULE
        ]
        assert not missing, f"guard keeps these but the rule never names them: {missing}"

    def test_the_action_protocol_appears_in_the_instruction(self):
        """`action::` is guarded by its own byte comparison, not by a sigil."""
        assert "action::" in _OPTIONS_RECOMMENDED_RULE

    def test_the_instruction_does_not_invent_a_sigil_the_guard_ignores(self):
        """Negative control: naming an unguarded sigil would teach a false restriction."""
        assert "`#`" not in _OPTIONS_RECOMMENDED_RULE
        assert "`%`" not in _OPTIONS_RECOMMENDED_RULE
