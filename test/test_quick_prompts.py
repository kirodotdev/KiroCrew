"""``/plain`` and the quick-prompt macro layer.

A quick prompt replaces the user's turn with an instruction before the model
reads it. Three things make that risky enough to pin here rather than trust:

* it fires on a LEADING token, so anything that widens the match (a longer word,
  a mid-sentence mention, a quoted log line) silently hijacks an ordinary turn;
* it must not shadow a kiro-cli slash command, which is forwarded to the harness
  and never reaches the expansion at all;
* it replaces the turn wholesale, so the user-text span the caller measured
  before the swap describes bytes that no longer exist.

The build_message tests drive the REAL assembly rather than the expander alone,
because the value of putting the macro in ``build_message`` is precisely that
every surface inherits it.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

from kiro_crew.context import ContextBuilder
from kiro_crew.context_blocks import attributable_user_chars
from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    is_harness_slash_command,
    user_text_span,
)
from kiro_crew.memory import MemoryStore
from kiro_crew.quick_prompts import QUICK_PROMPTS, expand_quick_prompt
from kiro_crew.skills import SkillsLoader

_REPO = Path(__file__).resolve().parents[1]
_MENU_TSX = _REPO / "website" / "src" / "components" / "SlashCommandMenu.tsx"
_EN_MANUAL = _REPO / "website" / "src" / "i18n" / "locales" / "en.manual.json"


def _make_builder(tmp_path):
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


def _flat(text: str) -> str:
    """Collapse whitespace so a phrase assertion survives a prose reflow.

    The templates are hard-wrapped for readability, so any asserted phrase long
    enough to be worth asserting straddles a newline. Matching on the flattened
    form pins the RULE being present, not the column it happens to break at.
    """
    return re.sub(r"\s+", " ", text)


class TestExpandsOnlyALeadingToken:
    def test_bare_token_expands(self):
        out = expand_quick_prompt("/plain")
        assert out is not None
        assert "[QUICK PROMPT /plain]" in out

    def test_leading_whitespace_still_expands(self):
        assert expand_quick_prompt("   /plain") is not None
        assert expand_quick_prompt("\t/plain  ") is not None

    def test_mid_sentence_mention_is_ordinary_text(self):
        """The word has to OPEN the message. Otherwise every turn that discusses
        the feature would silently become an invocation of it."""
        assert expand_quick_prompt("please /plain this for me") is None
        assert expand_quick_prompt("the /plain macro is documented where?") is None

    def test_quoted_inside_a_pasted_block_is_ordinary_text(self):
        assert expand_quick_prompt("here is the log:\n/plain\nnot a command") is None

    def test_longer_word_sharing_the_prefix_does_not_match(self):
        """``/plainly`` must not read as ``/plain`` + argument ``ly``."""
        assert expand_quick_prompt("/plainly explain this") is None
        assert expand_quick_prompt("/plain-text") is None

    def test_unregistered_token_is_ordinary_text(self):
        assert expand_quick_prompt("/nosuchmacro hello") is None

    def test_empty_and_whitespace_are_ordinary_text(self):
        assert expand_quick_prompt("") is None
        assert expand_quick_prompt("   \n ") is None

    def test_case_insensitive(self):
        """A phone keyboard autocapitalises the first letter of a message."""
        out = expand_quick_prompt("/Plain")
        assert out is not None
        assert "[QUICK PROMPT /plain]" in out


class TestZeroArgIsThePrimaryForm:
    def test_bare_token_asks_for_the_whole_line_not_the_last_turn(self):
        out = expand_quick_prompt("/plain")
        assert out is not None
        assert "WHOLE line of work" in _flat(out)
        assert "not the last turn" in _flat(out)

    def test_bare_token_asks_for_the_latest_understanding(self):
        out = expand_quick_prompt("/plain")
        assert out is not None
        assert "LATEST understanding" in _flat(out)
        assert "superseded" in out

    def test_bare_token_does_not_carry_the_with_arg_body(self):
        out = expand_quick_prompt("/plain")
        assert out is not None
        assert "Explain the following" not in out
        assert "{arg}" not in out


class TestArgumentForm:
    def test_argument_is_carried_verbatim(self):
        out = expand_quick_prompt("/plain why did the rebase turn CI red")
        assert out is not None
        assert "why did the rebase turn CI red" in out
        assert "Explain the following" in out

    def test_multiline_argument_is_carried(self):
        out = expand_quick_prompt("/plain\nwhy is this slot refusing to resume?")
        assert out is not None
        assert "why is this slot refusing to resume?" in out

    def test_braces_in_the_argument_do_not_raise(self):
        """The argument is the user's own text and may contain braces — a JSON
        blob, an f-string, a Rust generic. A ``format()``-based template would
        raise and lose the turn."""
        arg = '{"slot": "main", "state": {"nested": true}} and List<Map<K,V>>'
        out = expand_quick_prompt("/plain " + arg)
        assert out is not None
        assert arg in out

    def test_placeholder_is_fully_substituted(self):
        out = expand_quick_prompt("/plain the slot lifecycle")
        assert out is not None
        assert "{arg}" not in out


class TestStyleContractIsAlwaysPresent:
    def test_both_forms_carry_the_line_format(self):
        """The original contract said "3 to 5 sentences of plain prose", which
        produced exactly the wall of text `/plain` exists to avoid. The format is
        now lines, and that is the part most worth pinning."""
        for text in ("/plain", "/plain the retry path"):
            flat = _flat(expand_quick_prompt(text) or "")
            assert "3 to 5 LINES" in flat, text
            assert "one thought per line" in flat, text
            assert "Not one paragraph" in flat, text
            assert "thirty words" in flat, text

    def test_both_forms_follow_the_readers_language_and_habits(self):
        """An English-only instruction block pushes the answer toward English and
        drops the reader's own conventions, so both are stated explicitly."""
        for text in ("/plain", "/plain the retry path"):
            flat = _flat(expand_quick_prompt(text) or "")
            assert "language the reader is using" in flat, text
            assert "answer in Chinese" in flat, text
            assert "corrections they have taught you" in flat, text
            # The reader's own preferences outrank the shape rules.
            assert "win over anything here" in flat, text

    def test_both_forms_carry_the_bans_and_the_closing_decision(self):
        for text in ("/plain", "/plain the retry path"):
            flat = _flat(expand_quick_prompt(text) or "")
            assert "analogy" in flat, text
            assert "The LAST line is the decision" in flat, text
            assert "reader's own language" in flat, text
            assert "Add a diagram only when" in flat, text
            assert "do NOT make the next answer longer" in flat, text


class TestDoesNotShadowRealCommands:
    def test_tokens_are_disjoint_from_kiro_cli_slash_commands(self):
        """A token also present in ``_SLASH_COMMANDS`` would be forwarded to the
        harness via ``stream_command`` and never reach this expansion, so the
        macro would look silently broken rather than fail loudly."""
        assert not set(QUICK_PROMPTS) & set(_SLASH_COMMANDS)

    def test_tokens_are_disjoint_from_blocked_commands(self):
        assert not set(QUICK_PROMPTS) & set(_BLOCKED_SLASH_COMMANDS)

    def test_registered_slash_commands_are_left_alone(self):
        for cmd in sorted(_SLASH_COMMANDS):
            assert expand_quick_prompt(cmd) is None, cmd
            assert expand_quick_prompt(f"{cmd} some argument") is None, cmd

    def test_tokens_are_disjoint_from_every_channel_alias_set(self):
        """Each channel parses its OWN commands BEFORE the shared funnel.

        ``/new``, ``/compact``, ``/help`` and friends are handled inside
        ``<channel>/commands.py`` and never reach ``build_message``, so a token
        colliding with one would expand on some surfaces and not others -- the
        per-surface drift this layer exists to end, and invisible without this test.

        Collected by REFLECTION rather than a hand-written list: a channel that
        gains a new ``_*_ALIASES`` set is covered the day it lands, which a literal
        list would not be.
        """
        collected: dict[str, set[str]] = {}
        for mod_name in (
            "kiro_crew.telegram.commands",
            "kiro_crew.discord.commands",
            "kiro_crew.teams.commands",
            "kiro_crew.imessage.commands",
        ):
            mod = importlib.import_module(mod_name)
            aliases: set[str] = set()
            for attr in dir(mod):
                if not attr.endswith("_ALIASES"):
                    continue
                value = getattr(mod, attr)
                if isinstance(value, (set, frozenset)):
                    aliases |= {v for v in value if isinstance(v, str)}
            assert aliases, f"{mod_name} exposed no *_ALIASES -- has it been renamed?"
            collected[mod_name] = aliases

        for mod_name, aliases in collected.items():
            clash = set(QUICK_PROMPTS) & aliases
            assert not clash, f"{mod_name} already handles {sorted(clash)}"


class TestNotForwardedToTheHarness:
    """Under the ``claude_code`` provider ANY leading slash is forwarded to the
    harness as a command. A quick prompt must be excluded, or the token silently
    does nothing on that provider while working everywhere else."""

    def test_quick_prompt_is_not_a_harness_command_on_cc(self):
        for token in QUICK_PROMPTS:
            assert is_harness_slash_command(token, cc_provider=True) is False, token

    def test_quick_prompt_is_not_a_harness_command_on_other_providers(self):
        for token in QUICK_PROMPTS:
            assert is_harness_slash_command(token, cc_provider=False) is False, token

    def test_unknown_slash_is_still_forwarded_on_cc(self):
        """The catch-all itself must survive -- the exception is scoped to the
        registry, not a blanket hole in cc command routing."""
        assert is_harness_slash_command("/init", cc_provider=True) is True
        assert is_harness_slash_command("/security-review", cc_provider=True) is True

    def test_real_slash_commands_are_forwarded_on_every_provider(self):
        for cmd in sorted(_SLASH_COMMANDS):
            assert is_harness_slash_command(cmd, cc_provider=False) is True, cmd
            assert is_harness_slash_command(cmd, cc_provider=True) is True, cmd

    def test_ordinary_text_is_not_a_command(self):
        assert is_harness_slash_command("hello", cc_provider=True) is False
        assert is_harness_slash_command("", cc_provider=True) is False


class TestEveryTokenIsReachableFromTheComposer:
    """A quick prompt nobody can discover is dead weight. These read the
    frontend so a backend-only addition fails here instead of shipping a token
    with no menu row and no description."""

    def test_menu_lists_every_token(self):
        source = _MENU_TSX.read_text(encoding="utf-8")
        names = re.search(r"const FRONTEND_COMMAND_NAMES = \[(.*?)\]", source, re.S)
        assert names, "FRONTEND_COMMAND_NAMES not found in SlashCommandMenu.tsx"
        listed = set(re.findall(r"'([^']+)'", names.group(1)))
        assert set(QUICK_PROMPTS) <= listed

    def test_menu_maps_every_token_to_a_description_key(self):
        source = _MENU_TSX.read_text(encoding="utf-8")
        catalog = json.loads(_EN_MANUAL.read_text(encoding="utf-8"))
        for token in QUICK_PROMPTS:
            match = re.search(rf"'{re.escape(token)}': '([^']+)'", source)
            assert match, f"{token} has no COMMAND_DESC_KEY entry"
            node: object = catalog
            for segment in match.group(1).split("."):
                assert isinstance(node, dict) and segment in node, match.group(1)
                node = node[segment]
            assert isinstance(node, str) and node.strip()


class TestSpanLocatesTheTokenRatherThanAttributingIt:
    """The regression that made `/plain` stop expanding on the dashboard entirely.

    Two different questions share a shape: WHERE the user's text sits, and HOW MUCH
    of the turn is theirs. A quick prompt credits them zero, so deriving the span
    from `attributable_user_chars` produced an EMPTY range -- and that range is what
    the matcher reads, so the token never expanded. Every unit test still passed,
    because they hand `build_message` a truthful range directly and never go through
    the caller that computed it.
    """

    def test_quick_prompt_span_covers_the_typed_token(self):
        start, end = user_text_span(0, len("/plain"), quick_prompt=True, prompt_expanded=True)
        assert (start, end) == (0, len("/plain"))
        assert end > start, "an empty span cannot contain a token to match"

    def test_quick_prompt_span_survives_a_prefix_offset(self):
        start, end = user_text_span(40, len("/plain x"), quick_prompt=True, prompt_expanded=True)
        assert (start, end) == (40, 48)

    def test_attribution_still_credits_the_user_nothing(self):
        """The span widening must not undo the attribution rule -- they are separate
        mechanisms now, and this pins that the second one is untouched."""
        assert attributable_user_chars(len("/plain"), prompt_expanded=True) == 0

    def test_prompt_mention_span_stays_empty(self):
        """An `@prompt` turn was already replaced before this point, so its typed
        text is gone from the message and the empty span is correct there."""
        assert user_text_span(0, 12, quick_prompt=False, prompt_expanded=True) == (0, 0)

    def test_ordinary_turn_span_is_the_typed_text(self):
        assert user_text_span(7, 20, quick_prompt=False, prompt_expanded=False) == (7, 27)

    def test_the_span_a_quick_prompt_reports_actually_expands(self):
        """End to end over the two pieces that disagreed: take the span the caller
        would pass and feed that exact slice to the matcher."""
        prefix = "[Memory - pending]\nfact.\n\n"
        typed = "/plain"
        start, end = user_text_span(
            len(prefix), len(typed), quick_prompt=True, prompt_expanded=True
        )
        assert expand_quick_prompt((prefix + typed)[start:end]) is not None


class TestBuildMessageAppliesTheMacro:
    def test_turn_text_is_replaced_in_the_built_prompt(self, tmp_path):
        builder = _make_builder(tmp_path)
        msg, _ = builder.build_message("/plain", is_new_session=False)
        assert "[QUICK PROMPT /plain]" in msg
        assert "WHOLE line of work" in _flat(msg)

    def test_ordinary_turn_is_untouched(self, tmp_path):
        builder = _make_builder(tmp_path)
        typed = "explain the slot lifecycle"
        msg, _ = builder.build_message(typed, is_new_session=False)
        assert typed in msg
        assert "QUICK PROMPT" not in msg

    def test_user_span_is_empty_because_the_turn_is_injected_instruction(self, tmp_path):
        """The caller measured ``(0, 6)`` for ``/plain``; after expansion the turn is
        generated instruction and the six characters are gone.

        Neither the stale bounds NOR the full replacement is right: clamping would
        attribute the instruction's first six characters to the user, and claiming
        the whole span would report ~1.8k characters of generated text as their
        typing and underreport Crew-added context. The user's span is empty, which
        is the rule ``attributable_user_chars`` already states for ``@prompt``.
        """
        builder = _make_builder(tmp_path)
        typed = "/plain"
        span: list[int] = []
        msg, _ = builder.build_message(
            typed,
            is_new_session=False,
            user_text_range=(0, len(typed)),
            user_span_out=span,
        )
        assert len(span) == 2
        assert span[0] == span[1], "a replacing expansion attributes nothing to the user"
        assert msg[span[0] : span[1]] == ""
        # And the expansion really did land, so this is not an empty span for the
        # boring reason that nothing happened.
        assert "[QUICK PROMPT /plain]" in msg

    def test_user_span_is_empty_for_the_argument_form_too(self, tmp_path):
        """Under-crediting the argument is deliberate: it matches the ``@prompt``
        rule, and erring toward attributing LESS to the user can never over-credit
        the breakdown."""
        builder = _make_builder(tmp_path)
        typed = "/plain why is CI red"
        span: list[int] = []
        msg, _ = builder.build_message(
            typed,
            is_new_session=False,
            user_text_range=(0, len(typed)),
            user_span_out=span,
        )
        assert span[0] == span[1]
        assert "why is CI red" in msg

    def test_an_ordinary_turn_still_reports_its_real_span(self, tmp_path):
        """The empty-span rule must be scoped to quick prompts, not a hole in
        attribution for every turn."""
        builder = _make_builder(tmp_path)
        typed = "explain the retry path"
        span: list[int] = []
        msg, _ = builder.build_message(
            typed,
            is_new_session=False,
            user_text_range=(0, len(typed)),
            user_span_out=span,
        )
        assert msg[span[0] : span[1]] == typed

    def test_argument_survives_into_the_built_prompt(self, tmp_path):
        builder = _make_builder(tmp_path)
        msg, _ = builder.build_message(
            "/plain why is the gateway refusing to resume",
            is_new_session=False,
        )
        assert "why is the gateway refusing to resume" in msg


class TestPrefixedTurnStillExpands:
    """A dashboard turn can arrive with an ENVELOPE prefixed to it -- a drained
    memory block, a compaction notice -- which is exactly what ``user_text_range``
    exists to describe. Anchoring the match on the whole turn silently sent the
    literal token to the model, so these pin the slice-scoped match."""

    PREFIX = "[Memory - pending]\nremembered fact.\n\n"

    def _build(self, tmp_path, typed, prefix=None):
        prefix = self.PREFIX if prefix is None else prefix
        builder = _make_builder(tmp_path)
        span: list[int] = []
        msg, _ = builder.build_message(
            prefix + typed,
            is_new_session=False,
            user_text_range=(len(prefix), len(prefix) + len(typed)),
            user_span_out=span,
        )
        return msg, span

    def test_prefixed_token_expands(self, tmp_path):
        msg, _ = self._build(tmp_path, "/plain")
        assert "[QUICK PROMPT /plain]" in msg
        assert "WHOLE line of work" in _flat(msg)

    def test_the_prefix_is_preserved_ahead_of_the_expansion(self, tmp_path):
        msg, _ = self._build(tmp_path, "/plain")
        assert "remembered fact." in msg
        assert msg.index("remembered fact.") < msg.index("[QUICK PROMPT /plain]")

    def test_prefixed_argument_form_expands_and_carries_the_argument(self, tmp_path):
        msg, _ = self._build(tmp_path, "/plain why did the rebase go red")
        assert "[QUICK PROMPT /plain]" in msg
        assert "why did the rebase go red" in msg

    def test_span_is_empty_and_sits_where_the_users_slice_began(self, tmp_path):
        msg, span = self._build(tmp_path, "/plain")
        assert span[0] == span[1], "a replacing expansion attributes nothing"
        # Anchored at the splice point, i.e. after the prefix -- not at offset 0,
        # which would place the (empty) user span inside Crew-added context.
        assert span[0] >= msg.index("remembered fact.")

    def test_a_prefixed_ordinary_turn_still_reports_its_real_span(self, tmp_path):
        msg, span = self._build(tmp_path, "explain the retry path")
        assert msg[span[0] : span[1]] == "explain the retry path"

    def test_a_prefixed_near_miss_does_not_expand(self, tmp_path):
        msg, span = self._build(tmp_path, "/plainly explain the retry path")
        assert "QUICK PROMPT" not in msg
        assert msg[span[0] : span[1]] == "/plainly explain the retry path"

    def test_a_token_inside_the_PREFIX_does_not_expand(self, tmp_path):
        """The envelope is Crew-added text. A `/plain` quoted inside it is not an
        invocation, and treating it as one would let injected context fire the
        macro."""
        msg, _ = self._build(
            tmp_path,
            "what happened here",
            prefix="[Memory - pending]\nthe user typed /plain earlier.\n\n",
        )
        assert "QUICK PROMPT" not in msg
