"""Tests for the OPTIONS card: one-click buttons and the static answered/expired forms."""

from __future__ import annotations

from conftest import assert_rejected_without_backtracking
from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_SEVERAL_ACTION,
    build_options_blocks,
    build_options_selected_blocks,
    extract_options,
)


class TestExtractOptions:
    def test_extracts_choices(self):
        text = "Pick one\n[OPTIONS: A | B | C]"
        cleaned, choices = extract_options(text)
        assert choices == ["A", "B", "C"]
        assert "[OPTIONS:" not in cleaned

    def test_keeps_text_before_and_after_marker(self):
        cleaned, choices = extract_options("Pick.\n[OPTIONS: A | B]\nAnytime.")
        assert cleaned == "Pick.\nAnytime."
        assert choices == ["A", "B"]

    def test_keeps_text_after_marker_without_prefix(self):
        cleaned, choices = extract_options("[OPTIONS: A | B]\nAnytime.")
        assert cleaned == "Anytime."
        assert choices == ["A", "B"]

    def test_keeps_text_before_marker_without_suffix(self):
        cleaned, choices = extract_options("Pick.\n[OPTIONS: A | B]")
        assert cleaned == "Pick."
        assert choices == ["A", "B"]

    def test_no_options_returns_empty(self):
        cleaned, choices = extract_options("Hello world")
        assert choices == []
        assert cleaned == "Hello world"

    def test_strips_whitespace_from_choices(self):
        _, choices = extract_options("[OPTIONS:  X |  Y  | Z ]")
        assert choices == ["X", "Y", "Z"]

    def test_bracket_inside_option_text_survives(self):
        # The closing ']' is anchored to end-of-line, so a literal ']' inside
        # an option (e.g. "Fix [x] logging") must not truncate the body.
        _, choices = extract_options("[OPTIONS: Fix [x] logging | Skip]")
        assert choices == ["Fix [x] logging", "Skip"]

    def test_body_does_not_span_newlines(self):
        # The MULTILINE body must stay single-line: an assistant message that
        # mentions "[OPTIONS:" mid-text on one line and has a LATER line ending
        # in "]" must NOT match across the newline (which would delete a
        # multi-line span from the visible text and emit bogus pills). The
        # tempered body excludes \n (``[^[\n]``) precisely so this cannot happen.
        cleaned, choices = extract_options("See [OPTIONS: in my notes\nsummary ]")
        assert choices == []
        assert cleaned == "See [OPTIONS: in my notes\nsummary ]"

    def test_unterminated_options_tag_is_not_redos(self):
        # Regression (py/polynomial-redos): a plain greedy ``.*`` body could
        # consume a ``[`` that ALSO starts the outer ``[OPTIONS:`` literal, so
        # over text with many ``[OPTIONS:`` prefixes ``search()`` re-explored the
        # body from each position — polynomial. The tempered body
        # ``(?:[^[\n]|\[(?!OPTIONS:))*`` forbids only a re-occurring ``[OPTIONS:``,
        # so the body is unambiguous (linear). Two adversarial inputs — a long
        # whitespace-padded unterminated tag, and many repeated ``[OPTIONS:``
        # prefixes (the real pump) — must both be rejected in CPU time linear in
        # the pump; see conftest.assert_rejected_without_backtracking for why this
        # is not a 1.0 s wall-clock bound.
        def reject(text: str) -> None:
            cleaned, choices = extract_options(text)
            # No terminating ']' → no match, input returned unchanged.
            assert choices == []
            assert cleaned == text

        assert_rejected_without_backtracking(reject, lambda n: "[OPTIONS:" + (" " * n) + "x")
        assert_rejected_without_backtracking(reject, lambda n: "[OPTIONS:" * n + "x")


def _buttons(blocks):
    return [
        el
        for blk in blocks
        if blk["type"] == "actions"
        for el in blk["elements"]
        if el["type"] == "button"
    ]


class TestBuildOptionsBlocks:
    def test_returns_plain_buttons_and_a_choose_several_menu(self):
        blocks = build_options_blocks(["A", "B"])
        elements = blocks[0]["elements"]
        assert [el["type"] for el in elements] == ["button", "button", "overflow"]
        assert [el["action_id"] for el in elements[:2]] == [
            f"{OPTIONS_ACTION_PREFIX}0",
            f"{OPTIONS_ACTION_PREFIX}1",
        ]
        assert all("style" not in el for el in elements[:2])
        assert elements[2]["action_id"] == OPTIONS_SEVERAL_ACTION

    def test_buttons_match_choices(self):
        buttons = _buttons(build_options_blocks(["Alpha", "Beta", "Gamma"]))
        assert [b["value"] for b in buttons] == ["Alpha", "Beta", "Gamma"]
        assert buttons[2]["text"]["text"] == "Gamma"

    def test_max_ten_choices(self):
        choices = [f"C{i}" for i in range(15)]
        blocks = build_options_blocks(choices)
        assert len(_buttons(blocks)) == 10
        # Overflow does not vanish: choices 11-15 degrade to a numbered
        # context block the user can answer by typing.
        overflow = next(b for b in blocks if b["type"] == "context")
        assert "11. C10" in overflow["elements"][0]["text"]
        assert "15. C14" in overflow["elements"][0]["text"]

    def test_truncates_long_choice_label_only(self):
        long = "x" * 100
        (button,) = _buttons(build_options_blocks([long]))
        assert len(button["text"]["text"]) <= 75
        assert button["value"] == long


class TestBuildOptionsSelectedBlocks:
    def test_single_int_index_backward_compat(self):
        blocks = build_options_selected_blocks(["A", "B", "C"], 1)
        text = blocks[0]["elements"][0]["text"]
        assert "*B*" in text
        assert "~A~" in text
        assert "~C~" in text

    def test_multiple_indices(self):
        blocks = build_options_selected_blocks(["A", "B", "C"], [0, 2])
        text = blocks[0]["elements"][0]["text"]
        assert "*A*" in text
        assert "~B~" in text
        assert "*C*" in text

    def test_returns_context_block(self):
        blocks = build_options_selected_blocks(["A", "B"], [0])
        assert blocks[0]["type"] == "context"

    def test_all_choices_present(self):
        choices = ["One", "Two", "Three"]
        blocks = build_options_selected_blocks(choices, [2])
        text = blocks[0]["elements"][0]["text"]
        for c in choices:
            assert c in text

    def test_max_ten_choices(self):
        choices = [f"Choice {i}" for i in range(12)]
        blocks = build_options_selected_blocks(choices, [0])
        text = blocks[0]["elements"][0]["text"]
        assert "Choice 10" not in text

    def test_empty_selection_all_strikethrough(self):
        blocks = build_options_selected_blocks(["A", "B"], [])
        text = blocks[0]["elements"][0]["text"]
        assert "~A~" in text
        assert "~B~" in text
