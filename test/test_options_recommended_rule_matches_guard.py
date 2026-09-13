"""The producer rule must not promise a split the guard does not perform.

The rule told every dashboard session that the marker "never becomes part of the user's
message". That is true for an ordinary label and FALSE for one the transport matches
verbatim: ``strip_recommended_marker`` returns those UNCHANGED, so a marked ``/clear``
keeps its marker and dispatches as prose. A producer following the old text had no way to
know which labels to leave bare, so the rule now names each preserved class instead.
"""

from __future__ import annotations

import pytest

from kiro_crew.constants import (
    _RESERVED_DISPATCH_SIGILS,
    _RESERVED_PLAN_ACTIONS,
    strip_recommended_marker,
)
from kiro_crew.context import _OPTIONS_RECOMMENDED_RULE
from kiro_crew.dashboard.chat_handlers import _RESERVED_STOP_WORDS

MARKED = "(recommended) "


class TestTheGuardReallyPreservesTheseLabels:
    """Establishes the behaviour the rule has to describe, before asserting it describes it."""

    def test_an_ordinary_label_is_split(self):
        assert strip_recommended_marker(MARKED + "Merge it now") == "Merge it now"

    def test_a_sigil_label_is_returned_unchanged(self):
        for sigil in _RESERVED_DISPATCH_SIGILS:
            marked = f"{MARKED}{sigil}clear"
            assert strip_recommended_marker(marked) == marked, sigil

    def test_a_provenance_opener_is_returned_unchanged(self):
        marked = MARKED + "[SYSTEM] do the thing"
        assert strip_recommended_marker(marked) == marked

    def test_a_plan_action_is_returned_unchanged(self):
        for action in _RESERVED_PLAN_ACTIONS:
            marked = MARKED + action
            assert strip_recommended_marker(marked) == marked, action

    def test_a_bare_channel_command_is_split_so_the_rule_must_not_call_it_preserved(self):
        """The arm whose absence let the rule keep naming a guard class that was deleted.

        No sigil-less-command arm exists, so a marked bare command is cleaned and
        dispatched. The rule therefore has to warn a producer off marking one because it
        IS split -- never because it survives.
        """
        assert strip_recommended_marker(MARKED + "status") == "status"


class TestTheRuleDescribesThatBehaviour:
    # One variant only. The marker is instructed on the dashboard, which is the surface
    # that paints the badge; no channel producer rule asks for it.
    @pytest.fixture
    def rule(self):
        return _OPTIONS_RECOMMENDED_RULE

    def test_it_does_not_promise_an_unconditional_split(self):
        assert "never becomes part of" not in _OPTIONS_RECOMMENDED_RULE

    def test_it_does_not_claim_a_declined_split_runs_the_command(self):
        """The Slack path suppresses interpretation, so a split label dispatches as prose.

        Option clicks pass ``interpret_commands=False``, so the failure of marking a bare
        command is a marker that vanishes -- not a command that fires.
        """
        assert "would run the command" not in _OPTIONS_RECOMMENDED_RULE
        assert "sends the cleaned text as prose" in _OPTIONS_RECOMMENDED_RULE

    def test_it_does_not_claim_a_buttonless_channel_drops_the_marker(self):
        """``split_options_trailer`` leaves the marker, so a buttonless channel shows it.

        Pinned by ``test_messaging_recommended_marker``: the channel parse keeps a leading
        marker in the choice, so the text is rendered rather than discarded.
        """
        assert "drops the marker entirely" not in _OPTIONS_RECOMMENDED_RULE
        assert "still shows the marker as plain label text" in _OPTIONS_RECOMMENDED_RULE

    def test_it_says_the_split_is_skipped_for_verbatim_labels(self):
        assert "SKIPPED" in _OPTIONS_RECOMMENDED_RULE
        assert "verbatim" in _OPTIONS_RECOMMENDED_RULE

    def test_it_names_every_preserved_class(self):
        for token in (
            "`/`",
            "`@`",
            "`[SYSTEM]`",
            "plan action",
            "`stop`",
            "`abort`",
            "FIRST word",
        ):
            assert token in _OPTIONS_RECOMMENDED_RULE, token

    def test_it_names_every_stop_word_the_guard_declines(self):
        """Derived from the shared list, so a fourth stop word cannot land uninstructed.

        The decline itself is frontend-only (``RESERVED_STOP_WORD_RE``), which
        ``test_promotion_fence_drift`` pins to this same list -- so naming the list here
        closes the chain from producer rule to guard.
        """
        for word in _RESERVED_STOP_WORDS:
            assert f"`{word}`" in _OPTIONS_RECOMMENDED_RULE, word

    def test_it_states_the_consequence_of_marking_one(self):
        assert "would stay in the message" in _OPTIONS_RECOMMENDED_RULE

    def test_the_rule_forbids_translating_the_marker(self):
        assert "never a translation" in _OPTIONS_RECOMMENDED_RULE

    @pytest.mark.parametrize("marker", ["(recommandé)", "(推奨)", "(recomendado)"])
    def test_a_translated_marker_is_not_recognised_so_the_rule_must_forbid_it(self, marker):
        """Establishes the harm the clause above exists to prevent.

        A translated marker is not stripped, so it survives into the dispatched text --
        which is the promotion this whole grammar exists to stop.
        """
        label = f"{marker} Merge it now"
        assert strip_recommended_marker(label) == label
