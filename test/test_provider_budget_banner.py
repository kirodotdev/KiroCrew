"""Regression tests for provider-only token-budget banners in dashboard chat.

Stable discovery path for the provider-budget-banner suite. This module holds
the narrow classifier layer -- ``strip_provider_budget_banner``,
``classify_provider_budget_banner`` and the echo-evidence predicates. The
end-to-end turn behaviour lives in the sibling ``test_provider_budget_banner_*``
modules, which share wiring through ``_provider_budget_banner_helpers``.
"""

from __future__ import annotations

import pytest


class TestProviderBudgetBannerClassifier:
    """A backend-only context-budget reminder must not become chat content."""

    @pytest.mark.parametrize(
        ("raw", "expected", "removed"),
        [
            # The four shapes observed in a real provider transcript: two
            # banner-only messages, and two banners fused to the reply's first
            # word with no separator.
            ("You have 8136 weighted tokens left", "", True),
            ("You have 3762 weighted tokens left", "", True),
            (
                "You have 1461 weighted tokens leftTwo checks remain",
                "Two checks remain",
                True,
            ),
            (
                "You have 2811 weighted tokens leftThe summary follows",
                "The summary follows",
                True,
            ),
            # Plain decimal counts have no grouping requirement.
            ("You have 0 weighted tokens left", "", True),
            ("You have 9 weighted tokens left", "", True),
            ("You have 999 weighted tokens left", "", True),
            ("You have 1000 weighted tokens left", "", True),
            ("You have 1234567 weighted tokens left", "", True),
            # Grouped counts require one to three leading digits and exact
            # three-digit groups after every comma.
            ("You have 1,000 weighted tokens left", "", True),
            ("You have 12,345 weighted tokens left", "", True),
            ("You have 123,456 weighted tokens left", "", True),
            ("You have 1,234,567 weighted tokens left", "", True),
            ("  You have 8,154 weighted tokens left.\nFinal answer", "Final answer", True),
            # The glued form needs an uppercase letter directly after "left".
            # A lowercase continuation is a different word, punctuation before
            # the glued text is not the observed shape, and whitespace before a
            # word is ordinary prose.
            (
                "You have 1461 weighted tokens lefttwo tasks remain",
                "You have 1461 weighted tokens lefttwo tasks remain",
                False,
            ),
            (
                "You have 1461 weighted tokens leftover from the run",
                "You have 1461 weighted tokens leftover from the run",
                False,
            ),
            (
                "You have 1461 weighted tokens left.Done",
                "You have 1461 weighted tokens left.Done",
                False,
            ),
            (
                "You have 1461 weighted tokens left Two tasks remain",
                "You have 1461 weighted tokens left Two tasks remain",
                False,
            ),
            # Malformed grouping is ordinary model text, not provider metadata.
            ("You have 1,,2 weighted tokens left", "You have 1,,2 weighted tokens left", False),
            ("You have ,123 weighted tokens left", "You have ,123 weighted tokens left", False),
            ("You have 1,00 weighted tokens left", "You have 1,00 weighted tokens left", False),
            ("You have 12,34 weighted tokens left", "You have 12,34 weighted tokens left", False),
            (
                "You have 1234,567 weighted tokens left",
                "You have 1234,567 weighted tokens left",
                False,
            ),
            (
                "You have 1,2345 weighted tokens left",
                "You have 1,2345 weighted tokens left",
                False,
            ),
            (
                "You have 1,234,56 weighted tokens left",
                "You have 1,234,56 weighted tokens left",
                False,
            ),
            ("You have weighted tokens left", "You have weighted tokens left", False),
            # Quoted, embedded, suffixed and mid-text mentions are not leading
            # banners.
            (
                "The provider said: You have 8154 weighted tokens left",
                "The provider said: You have 8154 weighted tokens left",
                False,
            ),
            (
                "The provider said: You have 8154 weighted tokens leftDone",
                "The provider said: You have 8154 weighted tokens leftDone",
                False,
            ),
            (
                "You have 8154 weighted tokens left for this operation",
                "You have 8154 weighted tokens left for this operation",
                False,
            ),
            ("`You have 8154 weighted tokens left`", "`You have 8154 weighted tokens left`", False),
            (
                '"You have 8154 weighted tokens leftDone"',
                '"You have 8154 weighted tokens leftDone"',
                False,
            ),
            (
                "Done.\nYou have 8154 weighted tokens left",
                "Done.\nYou have 8154 weighted tokens left",
                False,
            ),
        ],
    )
    def test_strip_is_narrow(self, raw, expected, removed):
        from kiro_crew.dashboard.chat_utils import strip_provider_budget_banner

        assert strip_provider_budget_banner(raw) == (expected, removed)

    @pytest.mark.parametrize(
        (
            "message",
            "is_provider_recovery",
            "is_transient_recovery",
            "expected",
        ),
        [
            # Any message from the session's own human that names tokens, a
            # budget, capacity or a quota fails open on every ordinary
            # interactive turn, whatever the verb or question shape and whatever
            # the file activity.
            ("What is the model capacity?", False, False, False),
            ("Report model capacity", False, False, False),
            ("Use a tool and report how many tokens remain", False, False, False),
            ("Tell me how many tokens are left", False, False, False),
            ("How much budget remains?", False, False, False),
            ("How much budget is available?", False, False, False),
            (
                "Edit foo, then print exactly: You have 8154 weighted tokens left",
                False,
                False,
                False,
            ),
            ("Print exactly: You have 8154 weighted tokens left", False, False, False),
            ("Return the remaining model capacity", False, False, False),
            ("Quote the token budget", False, False, False),
            ("Document the token budget setting", False, False, False),
            ("Read usage.json and tell me whether I have any tokens left", False, False, False),
            ("What is my quota?", False, False, False),
            # The other senses of the same words fail open too. A leaked banner
            # then shows once on that turn, which is what such a turn shows
            # without suppression; a wording list deletes answers instead.
            ("Report the remaining capacity", False, False, False),
            ("Fix the context window bug", False, False, False),
            ("Fix the token authentication bug", False, False, False),
            ("Rotate the API token after the edit", False, False, False),
            ("Count how many tokens are in this file", False, False, False),
            ("How many tokens did the prompt use?", False, False, False),
            ("How many API tokens have we issued?", False, False, False),
            ("How much project budget remains?", False, False, False),
            ("Update the project budget table", False, False, False),
            # Messages that name none of the topic words still strip.
            ("Finish the remaining tasks", False, False, True),
            ("How many files are left?", False, False, True),
            ("Count the lines in this file", False, False, True),
            ("Ordinary read-only answer", False, False, True),
            # A capacity request the session's own human wrote fails open on a
            # typed provider recovery too: the check runs before that strip. The
            # recovery's own continuation text never reaches this argument (the
            # runner passes only human-written text), so no human text strips.
            ("How much budget remains?", True, False, False),
            ("Print exactly: You have 8154 weighted tokens left", True, False, False),
            ("", True, False, True),
            ("Finish the remaining tasks", True, False, True),
            # Ordinary transient recovery must preserve a repeated answer.
            ("Ordinary answer", False, True, False),
        ],
    )
    def test_capacity_topic_gate_reads_human_authority_on_every_turn_kind(
        self,
        message,
        is_provider_recovery,
        is_transient_recovery,
        expected,
    ):
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        classified, artifact = classify_provider_budget_banner(
            exact,
            message,
            is_provider_recovery=is_provider_recovery,
            is_transient_recovery=is_transient_recovery,
            # Give ordinary non-capacity rows independent artifact evidence;
            # explicit capacity wording must still fail open against it.
            prior_visible_output=expected,
        )
        assert artifact is expected
        assert classified == ("" if expected else exact)

    @pytest.mark.parametrize(
        ("message", "kept"),
        [
            # Remaining-token wordings in every shape a person uses: subject or
            # none, modifiers, the ability form, a qualifier before "tokens", the
            # inverted auxiliary, a quantity phrase, the yes/no form, wrapped or
            # double-spaced. The gate reads the topic word, so the shape does
            # not matter.
            ("Read usage.json and tell me how many tokens are remaining", True),
            ("Tell me how many tokens I have left", True),
            ("How many tokens do I still have left?", True),
            ("How many tokens are now left over?", True),
            ("Read usage.json and tell me how many tokens I can still use", True),
            ("How many tokens may you still consume?", True),
            ("How much budget can I still use?", True),
            ("Read usage.json and tell me how many more tokens can I use?", True),
            ("How many tokens have I got left?", True),
            ("Read usage.json and tell me how many tokens\nare left", True),
            ("How many  tokens are\tremaining?", True),
            ("Read usage.json and tell me the number of tokens I have left", True),
            ("What number of tokens remain?", True),
            ("Read usage.json and tell me whether I have any tokens left", True),
            ("Do I have any budget left?", True),
            ("Is my quota exhausted?", True),
            # The other senses of the words fail open as well: a leaked banner
            # shows once on such a turn, as it does without suppression.
            ("Count how many tokens are in this file", True),
            ("How many tokens did the prompt use?", True),
            ("How many API tokens have we issued?", True),
            ("Report the number of tokens per request", True),
            ("How much project budget remains?", True),
            # Only a message that names none of the topic words keeps the strip.
            ("Finish the remaining tasks", False),
            ("How many files are left?", False),
            ("Read usage.json and summarize it", False),
            ("Count the lines in this file", False),
            ("What is the number of open pull requests?", False),
        ],
    )
    def test_remaining_token_questions_keep_a_banner_only_tail_after_visible_output(
        self, message, kept
    ):
        """The capacity gate must win against the ordinary-tail evidence.

        With prior visible output a banner-only tail is otherwise metadata, so
        this is the row where a missed wording loses the user's answer.
        """
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        classified, artifact = classify_provider_budget_banner(
            exact,
            message,
            is_provider_recovery=False,
            is_transient_recovery=False,
            prior_visible_output=True,
        )
        assert artifact is (not kept)
        assert classified == (exact if kept else "")

    @pytest.mark.parametrize(
        ("is_provider_recovery", "echo_evidence_complete", "expected_text", "artifact"),
        [
            # Ordinary tail after visible output: whole evidence strips, a cut
            # result keeps the tail as an echo the check could not rule out.
            (False, True, "", True),
            (False, False, "You have 8154 weighted tokens left", False),
            # Typed recovery owns the prefix by provenance, but evidence it could
            # not read cannot rule an echo out there either: a cut result keeps
            # the text on a recovery turn too.
            (True, True, "", True),
            (True, False, "You have 8154 weighted tokens left", False),
        ],
        ids=["ordinary-whole", "ordinary-cut", "recovery-whole", "recovery-cut"],
    )
    def test_a_cut_tool_result_keeps_an_ordinary_banner_only_tail_visible(
        self, is_provider_recovery, echo_evidence_complete, expected_text, artifact
    ):
        """A result the transport cut cannot prove the number absent, on any turn kind."""
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        assert classify_provider_budget_banner(
            exact,
            "Read response.txt and repeat its last line verbatim",
            is_provider_recovery=is_provider_recovery,
            is_transient_recovery=False,
            prior_visible_output=True,
            # The visible head of the result lacks the phrase either way.
            tool_results=["print('hello')\n"],
            echo_evidence_complete=echo_evidence_complete,
        ) == (expected_text, artifact)

    @pytest.mark.parametrize(
        (
            "text",
            "message",
            "provider_recovery",
            "transient_recovery",
            "prior_visible",
            "expected_text",
            "artifact",
        ),
        [
            # Exact banner-only ordinary output is ambiguous. Preserve it even
            # when the user used an unforeseen echo verb rather than adding that
            # verb to a wording allowlist.
            (
                "You have 8154 weighted tokens left",
                "Echo the next model line verbatim",
                False,
                False,
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            # A prior visible answer gives independent evidence that the exact
            # final tail is provider metadata.
            (
                "You have 8154 weighted tokens left",
                "finish the task",
                False,
                False,
                True,
                "",
                True,
            ),
            # A line boundary proves the banner grammar, not who authored it.
            # Ordinary output therefore preserves the exact echoed answer.
            (
                "You have 8154 weighted tokens left\nFinal answer",
                "finish the task",
                False,
                False,
                False,
                "You have 8154 weighted tokens left\nFinal answer",
                False,
            ),
            # The glued shape is recognized grammar too, and the same authority
            # rule applies: an ordinary turn keeps the bytes it cannot attribute.
            (
                "You have 1461 weighted tokens leftTwo checks remain",
                "finish the task",
                False,
                False,
                True,
                "You have 1461 weighted tokens leftTwo checks remain",
                False,
            ),
            # Typed provider recovery is the causal opposite: the controller
            # minted ownership after observing a prior provider artifact, so it
            # may remove only the banner prefix and preserve the semantic tail,
            # with a line break or glued to the first word.
            (
                "You have 8154 weighted tokens left\nFinal answer",
                "",
                True,
                False,
                False,
                "Final answer",
                True,
            ),
            (
                "You have 2811 weighted tokens leftThe summary follows",
                "",
                True,
                False,
                False,
                "The summary follows",
                True,
            ),
            # Capacity requests, quoted prose, transient recovery, and near
            # misses stay visible.
            (
                "You have 8154 weighted tokens left",
                "What is the model capacity?",
                False,
                False,
                True,
                "You have 8154 weighted tokens left",
                False,
            ),
            (
                "`You have 8154 weighted tokens left`",
                "quote the line",
                False,
                False,
                True,
                "`You have 8154 weighted tokens left`",
                False,
            ),
            (
                "You have 8154 weighted tokens left",
                "",
                False,
                True,
                True,
                "You have 8154 weighted tokens left",
                False,
            ),
            (
                "You have 1461 weighted tokens leftTwo checks remain",
                "",
                False,
                True,
                False,
                "You have 1461 weighted tokens leftTwo checks remain",
                False,
            ),
            (
                "You have 8154 weighted tokens left for this operation",
                "finish the task",
                False,
                False,
                True,
                "You have 8154 weighted tokens left for this operation",
                False,
            ),
            # An explicit capacity echo stays visible with no other evidence.
            (
                "You have 8154 weighted tokens left",
                "Print exactly: You have 8154 weighted tokens left",
                False,
                False,
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            # Typed provider recovery is host-created specifically to recover a
            # prior provider artifact, so a repeated exact banner remains owned.
            (
                "You have 8154 weighted tokens left",
                "",
                True,
                False,
                False,
                "",
                True,
            ),
            # Regeneration context is not artifact provenance either.
            (
                "You have 8154 weighted tokens left",
                "regenerate",
                False,
                False,
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
        ],
    )
    def test_classifier_uses_content_and_structural_provenance(
        self,
        text,
        message,
        provider_recovery,
        transient_recovery,
        prior_visible,
        expected_text,
        artifact,
    ):
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        assert classify_provider_budget_banner(
            text,
            message,
            is_provider_recovery=provider_recovery,
            is_transient_recovery=transient_recovery,
            prior_visible_output=prior_visible,
        ) == (expected_text, artifact)

    @pytest.mark.parametrize(
        ("text", "tool_results", "provider_recovery", "expected_text", "artifact"),
        [
            # A tool result this turn delivered carries the banner's phrase, so
            # the tail may be the echo the user asked for: it stays visible.
            (
                "You have 8154 weighted tokens left",
                ["You have 8154 weighted tokens left"],
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            # The phrase anywhere in a result counts, with the echo's own trailing
            # period and a grouped count.
            (
                "You have 8,154 weighted tokens left.",
                ["notes\nYou have 8,154 weighted tokens left\nmore notes"],
                False,
                "You have 8,154 weighted tokens left.",
                False,
            ),
            # The check runs before the provider-recovery strip as well.
            (
                "You have 8154 weighted tokens left",
                ["You have 8154 weighted tokens left"],
                True,
                "You have 8154 weighted tokens left",
                False,
            ),
            # The echo is the same count in the same words, not the same
            # spelling: a result that groups the digits, or the model's reply
            # that drops the grouping the result had, is the phrase the person
            # may have asked to see reformatted; so is a result that wraps it.
            (
                "You have 8154 weighted tokens left",
                ["last line: You have 8,154 weighted tokens left"],
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            (
                "You have 8,154 weighted tokens left",
                ["You have 8154 weighted tokens left"],
                False,
                "You have 8,154 weighted tokens left",
                False,
            ),
            (
                "You have 8154 weighted tokens left",
                ["You have 8154 weighted\ntokens left"],
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            # The evidence is the number: a result in another case, another
            # wording, or holding only the figure (a usage file) is what a reply
            # in the banner's shape may be echoing or reformatting.
            (
                "You have 8154 weighted tokens left",
                ["you have 8154 weighted tokens left"],
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            (
                "You have 8154 weighted tokens left",
                ["remaining weighted tokens: 8,154"],
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            (
                "You have 8154 weighted tokens left",
                ['{"remaining": 8154}'],
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            # A different count, however grouped, is not this banner's echo, and
            # a number that merely contains the digits is not the number.
            (
                "You have 8154 weighted tokens left",
                ["You have 9,154 weighted tokens left"],
                False,
                "",
                True,
            ),
            ("You have 8154 weighted tokens left", ["id 18154, 8154.25 ms"], False, "", True),
            ("You have 8154 weighted tokens left", ["81540 rows"], False, "", True),
            # A different count is not this banner's echo, and a result without
            # the phrase is no evidence: the tail after narration is stripped.
            (
                "You have 8154 weighted tokens left",
                ["You have 0 weighted tokens left"],
                False,
                "",
                True,
            ),
            ("You have 8154 weighted tokens left", ["file contents"], False, "", True),
            ("You have 8154 weighted tokens left", [], False, "", True),
        ],
    )
    def test_banner_phrase_in_a_tool_result_is_an_echo_candidate(
        self, text, tool_results, provider_recovery, expected_text, artifact
    ):
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        assert classify_provider_budget_banner(
            text,
            "finish the task",
            is_provider_recovery=provider_recovery,
            is_transient_recovery=False,
            prior_visible_output=True,
            tool_results=tool_results,
        ) == (expected_text, artifact)

    @pytest.mark.parametrize(
        ("transcript_texts", "kept"),
        [
            # An earlier answer in this conversation carrying the banner's count
            # is what a request to repeat the previous answer echoes.
            (["You have 8154 weighted tokens left"], True),
            (["Earlier I said you had 8,154 weighted tokens left."], True),
            # A different count, or no earlier answer, is no evidence.
            (["You have 9154 weighted tokens left"], False),
            ([], False),
        ],
    )
    def test_an_earlier_answer_carrying_the_count_is_echo_evidence(self, transcript_texts, kept):
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        classified, artifact = classify_provider_budget_banner(
            exact,
            "Check README, then repeat your previous answer exactly",
            is_provider_recovery=False,
            is_transient_recovery=False,
            prior_visible_output=True,
            tool_results=["# README\nno figures here"],
            transcript_texts=transcript_texts,
        )
        assert artifact is (not kept)
        assert classified == (exact if kept else "")

    @pytest.mark.parametrize(
        ("message", "kept"),
        [
            # The human's own words carry the count: a translation request in
            # another language names no topic word the gate knows, but the number
            # is the same.
            ("Translate « Il vous reste 8154 jetons pondérés » into English.", True),
            ("Traduce «Te quedan 8154 fichas ponderadas» al inglés.", True),
            ("Print exactly the figure 8154 in a sentence about what is left", True),
            # Words without the count, in any language, are no evidence.
            ("Translate « Bonjour tout le monde » into English.", False),
            ("Print exactly the figure 9154 in a sentence about what is left", False),
        ],
        ids=["french", "spanish", "english-count", "french-no-count", "other-count"],
    )
    def test_the_count_in_the_humans_own_words_is_echo_evidence(self, message, kept):
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        classified, artifact = classify_provider_budget_banner(
            exact,
            message,
            is_provider_recovery=False,
            is_transient_recovery=False,
            prior_visible_output=True,
            tool_results=["# README\nno figures here"],
            transcript_texts=[],
        )
        assert artifact is (not kept)
        assert classified == (exact if kept else "")

    def test_carries_count_folds_unicode_digits_but_keeps_the_whole_number_boundary(self):
        """A figure the reply echoes can be written in another script's digits.

        Arabic-Indic, Devanagari and fullwidth spellings of 8154 all name the
        same whole number, so each protects an ASCII banner. The per-digit fold
        does not loosen the boundary: a longer or fractional non-ASCII number
        still does not name 8154.
        """
        from kiro_crew.dashboard.chat_utils import _carries_count

        assert _carries_count("remaining \u0668\u0661\u0665\u0664 tokens", "8154") is True
        assert _carries_count("\u096e\u0967\u096b\u096a left", "8154") is True
        assert _carries_count("\uff18\uff11\uff15\uff14", "8154") is True
        # 18154 and 8154.2, written in Arabic-Indic digits, are different numbers.
        assert _carries_count("\u0661\u0668\u0661\u0665\u0664", "8154") is False
        assert _carries_count("\u0668\u0661\u0665\u0664.\u0662", "8154") is False

    @pytest.mark.parametrize(
        ("evidence_kind", "evidence"),
        [
            # Arabic-Indic 8154 in a tool result this turn, or in an earlier row.
            ("tool_results", "status: \u0668\u0661\u0665\u0664 left"),
            ("transcript_texts", "\u0633\u0627\u0628\u0642\u0627: \u0668\u0661\u0665\u0664"),
        ],
        ids=["arabic-indic-tool-result", "arabic-indic-earlier-row"],
    )
    def test_a_count_in_non_ascii_digits_is_echo_evidence(self, evidence_kind, evidence):
        """A figure the reply translates into ASCII still names the same number.

        The number is folded to one digit alphabet before the comparison, so a
        banner repeating a count the model read in Arabic-Indic digits is a
        visible echo, not a provider artifact. The asker names no topic word, so
        only the number itself is the evidence here.
        """
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        kwargs: dict = {"tool_results": [], "transcript_texts": []}
        kwargs[evidence_kind] = [evidence]
        classified, artifact = classify_provider_budget_banner(
            exact,
            "translate the status line into English",
            is_provider_recovery=False,
            is_transient_recovery=False,
            prior_visible_output=True,
            **kwargs,
        )
        assert (classified, artifact) == (exact, False)
