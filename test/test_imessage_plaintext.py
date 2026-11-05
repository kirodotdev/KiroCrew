"""Tests for kiro_crew.imessage.plaintext (markdown flattening + splitting)."""

from __future__ import annotations

from kiro_crew.imessage.plaintext import chunk_plaintext, to_plaintext
from kiro_crew.messaging.split import split_markdown_safe


class TestCodeBlocksPassThroughVerbatim:
    def test_fence_contents_keep_indentation_and_blank_lines(self) -> None:
        out = to_plaintext("intro\n\n```python\ndef f():\n\n    return **1**\n```\n")
        # The body is what a user copies out of the message, so `**1**` must NOT
        # be de-emphasised and the blank line must survive.
        assert "def f():\n\n    return **1**" in out
        # The delimiter itself is noise on a surface that renders no markup.
        assert "```" not in out

    def test_tilde_fence_is_honoured(self) -> None:
        out = to_plaintext("~~~\n# not a heading\n~~~")
        assert out == "# not a heading"

    def test_a_different_delimiter_inside_a_fence_is_content(self) -> None:
        out = to_plaintext("```\n~~~\nstill code\n```")
        assert out == "~~~\nstill code"

    def test_an_unclosed_fence_does_not_swallow_flattening_forever(self) -> None:
        # Everything after the opener is code; nothing is dropped.
        out = to_plaintext("```\n**kept**")
        assert out == "**kept**"


class TestInlineMarkupIsFlattened:
    def test_emphasis_variants(self) -> None:
        assert to_plaintext("**b** *i* __b__ _i_ ***bi*** ~~s~~") == "b i b i bi s"

    def test_inline_code_loses_its_backticks(self) -> None:
        assert to_plaintext("run `kirocrew status` now") == "run kirocrew status now"

    def test_heading_marker_is_dropped(self) -> None:
        assert to_plaintext("### Title\nbody") == "Title\nbody"

    def test_link_keeps_the_url_because_imessage_makes_it_tappable(self) -> None:
        assert to_plaintext("see [docs](https://x.dev/a)") == "see docs (https://x.dev/a)"

    def test_a_link_whose_label_is_its_url_is_not_duplicated(self) -> None:
        assert to_plaintext("[https://x.dev](https://x.dev)") == "https://x.dev"

    def test_image_alt_is_kept_and_the_bang_is_consumed(self) -> None:
        assert to_plaintext("![a chart](u)") == "a chart (u)"

    def test_bullets_become_a_real_bullet_character(self) -> None:
        assert to_plaintext("- one\n* two\n+ three") == "• one\n• two\n• three"

    def test_ordered_list_is_normalized_to_dot_form(self) -> None:
        assert to_plaintext("1) one\n2. two") == "1. one\n2. two"

    def test_blockquote_and_rule_markers_go_away(self) -> None:
        assert to_plaintext("> quoted\n\n---\n\ntail") == "quoted\n\ntail"

    def test_blank_runs_collapse_to_one_paragraph_break(self) -> None:
        assert to_plaintext("a\n\n\n\n\nb") == "a\n\nb"

    def test_empty_input_stays_empty(self) -> None:
        assert to_plaintext("") == ""


class TestVerbatimSpansKeepTheirMarkers:
    """Inline code and URLs are what a user copies or taps, so emphasis
    flattening must not reach inside them."""

    def test_inline_code_keeps_underscores_and_asterisks(self) -> None:
        assert to_plaintext("Call `__init__` first") == "Call __init__ first"
        assert to_plaintext("Use `**kwargs**` here") == "Use **kwargs** here"

    def test_a_link_target_keeps_its_underscores(self) -> None:
        assert (
            to_plaintext("[docs](https://e.com/__init__.py)") == "docs (https://e.com/__init__.py)"
        )
        assert to_plaintext("[https://e.com/_x_](https://e.com/_x_)") == "https://e.com/_x_"

    def test_a_bare_url_keeps_its_underscores(self) -> None:
        assert to_plaintext("see https://e.com/a/__x__/b") == "see https://e.com/a/__x__/b"

    def test_emphasis_around_a_verbatim_span_still_goes(self) -> None:
        assert to_plaintext("**run `a_b_c` now**") == "run a_b_c now"
        assert to_plaintext("[**docs**](https://e.com/_x_)") == "docs (https://e.com/_x_)"

    def test_emphasis_around_a_bare_url_still_goes(self) -> None:
        assert to_plaintext("**See https://e.com/docs**") == "See https://e.com/docs"
        assert to_plaintext("*https://e.com*") == "https://e.com"
        assert to_plaintext("_https://e.com_") == "https://e.com"
        assert to_plaintext("~~https://old.com~~") == "https://old.com"
        assert to_plaintext("**see https://e.com/a__x__b**") == "see https://e.com/a__x__b"
        # Punctuation after the closing marker does not pull it into the URL.
        assert to_plaintext("**See https://e.com/docs**.") == "See https://e.com/docs."
        assert (
            to_plaintext("Docs: **https://e.com/x**, then go") == "Docs: https://e.com/x, then go"
        )
        assert to_plaintext("(*https://e.com/a_b*)") == "(https://e.com/a_b)"
        assert to_plaintext("see https://e.com/docs.") == "see https://e.com/docs."

    def test_a_closing_marker_after_a_url_pairs_whatever_follows_it(self) -> None:
        assert to_plaintext("请访问 **https://x.com**。") == "请访问 https://x.com。"
        assert to_plaintext("**https://x.com**—next") == "https://x.com—next"
        assert to_plaintext("**https://x.com**'s page") == "https://x.com's page"

    def test_trailing_url_underscores_stay_in_the_url(self) -> None:
        url = "https://github.com/facebook/react/tree/main/packages/react/src/__tests__"
        assert to_plaintext(f"{url} and __bold__") == f"{url} and bold"
        assert to_plaintext(f"**see** {url}") == f"see {url}"

    def test_a_bare_url_keeps_a_balanced_closing_parenthesis(self) -> None:
        url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        assert to_plaintext(f"read {url} now") == f"read {url} now"
        assert to_plaintext(f"**{url}**.") == f"{url}."

    def test_a_link_target_holding_inline_code_is_restored(self) -> None:
        assert to_plaintext("[a](`b`)") == "a (b)"
        assert to_plaintext("[run](`x_y`) now") == "run (x_y) now"

    def test_a_line_holding_a_nul_is_still_flattened(self) -> None:
        assert to_plaintext("a\x00b **c**") == "a\x00b c"


class TestFenceDelimiterLength:
    """A fence is closed only by a run at least as long as its opener.

    Normalising the opener to three backticks made an inner ``` line close a
    ```` block, and everything after it was markdown-flattened -- the user was
    shown corrupted code with no indication anything had happened.
    """

    def test_a_shorter_run_inside_a_long_fence_is_content(self) -> None:
        text = "````\ncode with ``` inside\nstill code *not* italic\n````"
        out = to_plaintext(text)
        assert "```" in out
        assert "*not*" in out, "content after the inner run was flattened"

    def test_a_longer_run_closes_a_short_fence(self) -> None:
        text = "```\ncode\n````\n*after*"
        out = to_plaintext(text)
        assert "after" in out and "*after*" not in out

    def test_an_equal_run_still_closes(self) -> None:
        text = "```\ncode\n```\n*after*"
        out = to_plaintext(text)
        assert "code" in out and "*after*" not in out

    def test_tilde_and_backtick_do_not_close_each_other(self) -> None:
        text = "~~~\ncode ``` here\n~~~\n*after*"
        out = to_plaintext(text)
        assert "```" in out and "*after*" not in out

    def test_leading_code_indentation_survives_flattening(self) -> None:
        # A bare .strip() on the joined output ate the first line's indentation.
        text = "```\n    indented\ncode\n```"
        assert "    indented" in to_plaintext(text)


class TestChunkBoundariesPreserveIndentation:
    def test_a_continuation_line_keeps_its_indentation(self) -> None:
        # The corruption case: a code line falling on a chunk boundary lost its
        # leading whitespace, because the split lstripped the remainder.
        text = "x" * 20 + "\n" + "    indented continuation"
        chunks = chunk_plaintext(text, 24)
        assert len(chunks) > 1
        assert chunks[1].startswith("    indented"), chunks

    def test_trailing_whitespace_is_still_trimmed(self) -> None:
        # Asymmetric by design: the trailing side is never semantic here.
        assert chunk_plaintext("aaaa bbbb cccc", 11) == ["aaaa bbbb", "cccc"]


class TestChunkBoundaries:
    def test_empty_and_short_text(self) -> None:
        assert chunk_plaintext("", 10) == []
        assert chunk_plaintext("short", 10) == ["short"]

    def test_a_non_positive_limit_still_delivers_the_message(self) -> None:
        # A broken limit must not silently drop the answer.
        assert chunk_plaintext("body", 0) == ["body"]

    def test_paragraph_break_is_preferred_over_a_line_break(self) -> None:
        text = "one\ntwo\n\nthree"
        chunks = chunk_plaintext(text, 12)
        assert chunks == ["one\ntwo", "three"]

    def test_line_break_is_preferred_over_a_space(self) -> None:
        chunks = chunk_plaintext("aaa bbb\nccc", 9)
        assert chunks == ["aaa bbb", "ccc"]

    def test_word_boundary_is_used_when_no_line_break_fits(self) -> None:
        chunks = chunk_plaintext("aaaa bbbb cccc", 10)
        assert chunks == ["aaaa bbbb", "cccc"]

    def test_every_chunk_respects_the_limit(self) -> None:
        text = " ".join(["word"] * 200)
        for chunk in chunk_plaintext(text, 40):
            assert len(chunk) <= 40

    def test_nothing_is_lost_across_a_word_split(self) -> None:
        text = " ".join(f"w{i}" for i in range(200))
        assert " ".join(chunk_plaintext(text, 37)).split() == text.split()

    def test_a_paragraph_break_halfway_in_is_still_taken(self) -> None:
        text = "a" * 6 + "\n\n" + "b" * 8
        assert chunk_plaintext(text, 12) == ["a" * 6, "b" * 8]

    def test_a_line_break_a_quarter_in_is_still_taken(self) -> None:
        text = "a" * 3 + "\n" + "b" * 10
        assert chunk_plaintext(text, 12) == ["a" * 3, "b" * 10]

    def test_a_space_a_quarter_in_is_still_taken(self) -> None:
        text = "a" * 3 + " " + "b" * 10
        assert chunk_plaintext(text, 12) == ["a" * 3, "b" * 10]

    def test_a_long_answer_reaches_the_floor(self) -> None:
        text = "x" * 9000
        assert [len(c) for c in chunk_plaintext(text, 4000)] == [4000, 4000, 1000]

    def test_a_cut_through_a_long_word_keeps_every_character(self) -> None:
        text = "Hi\n\n" + "x" * 30
        assert "".join(chunk_plaintext(text, 20)).replace("\n", "") == "Hi" + "x" * 30

    def test_cjk_without_spaces_falls_back_to_a_hard_cut(self) -> None:
        text = "一二三四五六七八九十"
        chunks = chunk_plaintext(text, 4)
        assert chunks == ["一二三四", "五六七八", "九十"]
        assert "".join(chunks) == text


class TestGraphemeClustersSurviveAHardCut:
    def test_a_flag_is_never_split_in_half(self) -> None:
        # Two regional indicators = one flag. A cut between them renders as two
        # stray letters on both sides.
        text = "🇦🇺🇳🇿🇯🇵"
        chunks = chunk_plaintext(text, 3)
        assert "".join(chunks) == text
        for chunk in chunks:
            assert len(chunk) % 2 == 0

    def test_a_skin_tone_modifier_stays_with_its_base(self) -> None:
        text = "👍🏽👍🏽"
        chunks = chunk_plaintext(text, 3)
        assert "".join(chunks) == text
        for chunk in chunks:
            assert not chunk.startswith("\U0001f3fd")

    def test_a_zwj_sequence_is_not_cut_at_the_joiner(self) -> None:
        family = "👨\u200d👩\u200d👧"
        chunks = chunk_plaintext(family + family, 4)
        assert "".join(chunks) == family + family
        for chunk in chunks:
            assert not chunk.startswith("\u200d")
            assert not chunk.endswith("\u200d")

    def test_a_combining_accent_stays_with_its_letter(self) -> None:
        text = "e\u0301" * 5
        chunks = chunk_plaintext(text, 3)
        assert "".join(chunks) == text
        for chunk in chunks:
            assert not chunk.startswith("\u0301")

    def test_one_grapheme_longer_than_the_limit_is_emitted_whole(self) -> None:
        # No safe boundary exists below the limit; emitting it whole is the only
        # non-corrupting option, and it must not spin on a zero-width cut.
        family = "👨\u200d👩\u200d👧\u200d👦"
        chunks = chunk_plaintext(family + "tail", 2)
        assert "".join(chunks) == family + "tail"


class TestAnEarlyBreakDoesNotStrandTheBudget:
    def test_a_short_greeting_paragraph_does_not_cost_a_message(self) -> None:
        chunks = chunk_plaintext("Hi\n\n" + "x" * 6000, 4000)
        assert [len(c) for c in chunks] == [4000, 2004]
        assert chunks[0].startswith("Hi\n\nxxx")

    def test_a_short_greeting_before_wrapped_prose_does_not_cost_a_message(self) -> None:
        chunks = chunk_plaintext("Hi\n\n" + "word " * 1200, 4000)
        assert len(chunks) == 2
        assert chunks[0].startswith("Hi\n\nword")

    def test_an_early_line_break_falls_through_to_a_later_space(self) -> None:
        text = "Title\n" + "aa " * 10
        assert chunk_plaintext(text, 28) == ["Title\naa aa aa aa aa aa aa", "aa aa aa"]

    def test_an_early_space_falls_through_to_a_hard_cut(self) -> None:
        text = "a " + "b" * 30
        assert chunk_plaintext(text, 20) == ["a " + "b" * 18, "b" * 12]

    def test_an_early_paragraph_falls_through_to_a_later_line_break(self) -> None:
        text = "a\n\n" + "b" * 12 + "\n" + "c" * 10
        assert chunk_plaintext(text, 20) == ["a\n\n" + "b" * 12, "c" * 10]

    def test_a_long_url_behind_a_short_lead_in_moves_whole(self) -> None:
        # iMessage makes a bare URL tappable; a URL cut in two opens nothing.
        url = "https://example.com/" + "a" * 48
        chunks = chunk_plaintext("Open: " + url, 70)
        assert chunks == ["Open:", url]

    def test_a_word_longer_than_any_chunk_fills_the_budget(self) -> None:
        chunks = chunk_plaintext("Open: " + "a" * 90, 70)
        assert [len(c) for c in chunks] == [70, 26]

    def test_a_paragraph_between_a_quarter_and_halfway_matches_the_shared_splitter(
        self,
    ) -> None:
        # A paragraph break that misses its own floor still counts as a line
        # break, in split_markdown_safe's ladder and here.
        text = "w " * 550 + "\n\n" + "word " * 1000
        ours = chunk_plaintext(text, 4000)
        shared = split_markdown_safe(text, 4000)
        assert len(ours) == len(shared) == 3
        assert ours[0] == shared[0]
