"""A model-authored label must not reach the early slash-command dispatch.

`_run_chat` gates `@prompt` and `$skill` expansion on `_model_authored`, but the slash
dispatch above them ran regardless -- so a `[OPTIONS: /workflow deploy]` label clicked in a
linked Slack thread started a saved workflow. The fix neutralises `first_word` at DETECTION,
which is what makes every arm below it unreachable at once rather than one arm at a time.
"""

from __future__ import annotations

import inspect
import re

from kiro_crew.dashboard import chat_runner

SRC = inspect.getsource(chat_runner)
GUARD = 'first_word = "" if _model_authored else (message.split()[0] if message.strip() else "")'


class TestTheSlashDispatchIsGatedOnProvenance:
    def test_first_word_is_neutralised_for_a_model_authored_turn(self):
        assert GUARD in " ".join(SRC.split())

    def test_the_guard_sits_at_the_only_assignment(self):
        """One assignment means no arm can read an ungated value."""
        assigns = re.findall(r"^\s*first_word = ", SRC, re.M)
        assert len(assigns) == 1, f"{len(assigns)} assignments -- a second could bypass the gate"

    def test_every_command_arm_reads_the_gated_value(self):
        """Ordering is the invariant: an arm placed ABOVE the assignment would be ungated."""
        lines = SRC.splitlines()
        guard_at = next(i for i, ln in enumerate(lines) if ln.strip().startswith("first_word = "))
        arms = [i for i, ln in enumerate(lines) if re.search(r"first_word (==|in) ", ln)]
        assert arms, "positive control: the arms must exist to be ordered"
        assert min(arms) > guard_at, "a command arm reads first_word before the gate"

    def test_the_sibling_expansion_gates_still_exist(self):
        """The slash gate joins them; it does not replace them."""
        flat = " ".join(SRC.split())
        assert "_model_authored" in flat
        assert flat.count("_model_authored") >= 3
