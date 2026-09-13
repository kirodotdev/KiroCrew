"""The strip guard is bound to what actually dispatches, not to a mirrored list.

The fence suites next door pin list against list: ``_RESERVED_DISPATCH_SIGILS`` here
equals ``RESERVED_DISPATCH_SIGILS`` there. That is parity, and parity is blind in the
one direction that matters -- a dispatch prefix introduced at the dispatch site updates
neither list, both lists still agree, every parity test stays green, and
``(recommended) <new-command>`` strips into something that runs.

So these tests interrogate the dispatch entry points themselves and require the guard to
decline whatever they claim. The binding runs dispatch -> guard, which is the direction a
new prefix arrives from. Before this, the only thing pointing from the dispatch site back
at the guard was a comment above ``is_harness_slash_command``.
"""

from __future__ import annotations

import pytest

from kiro_crew.constants import strip_recommended_marker
from kiro_crew.dashboard.chat_utils import _SLASH_COMMANDS, is_harness_slash_command

MARKED = "(recommended) "


class TestTheGuardDeclinesWhateverTheHarnessDispatches:
    def test_the_command_set_is_not_empty(self):
        """A vacuous loop below would pass while asserting nothing."""
        assert _SLASH_COMMANDS

    @pytest.mark.parametrize("word", sorted(_SLASH_COMMANDS))
    def test_a_dispatched_command_word_is_returned_unchanged(self, word: str):
        """Ask the dispatcher, then require the guard to agree.

        A command added to the dispatch set that the guard cannot see -- one carrying no
        sigil the guard knows -- fails here, which is the case list-to-list parity cannot
        detect.
        """
        assert is_harness_slash_command(word, cc_provider=False), word
        marked = MARKED + word
        assert strip_recommended_marker(marked) == marked, word

    def test_any_leading_slash_dispatches_under_that_provider_and_is_declined(self):
        """Under ``claude_code`` the harness owns its set, so ANY leading slash forwards."""
        for word in ("/unknown-to-us", "/x", "/deploy-prod"):
            assert is_harness_slash_command(word, cc_provider=True), word
            marked = MARKED + word
            assert strip_recommended_marker(marked) == marked, word

    def test_a_mention_is_declined_because_the_resolver_substitutes_its_content(self):
        """``_resolve_prompt_mention`` replaces an ``@name`` message with a stored prompt."""
        marked = MARKED + "@deploy"
        assert strip_recommended_marker(marked) == marked

    def test_an_ordinary_word_is_not_dispatched_and_is_split(self):
        """The negative control: without this the guard could decline everything and pass."""
        assert not is_harness_slash_command("Merge", cc_provider=False)
        assert strip_recommended_marker(MARKED + "Merge it now") == "Merge it now"
