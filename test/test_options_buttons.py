"""Tests for OPTIONS multi-select checkboxes + Send button."""

from __future__ import annotations

from kiro_crew.slack.format import (
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
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
        # prefixes (the real pump) — must both return promptly.
        import time

        for evil in (
            "[OPTIONS:" + (" " * 200_000) + "x",
            "[OPTIONS:" * 100_000 + "x",
        ):
            start = time.perf_counter()
            cleaned, choices = extract_options(evil)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"extract_options took {elapsed:.2f}s (possible ReDoS)"
            # No terminating ']' → no match, input returned unchanged.
            assert choices == []
            assert cleaned == evil


class TestBuildOptionsBlocks:
    def test_returns_checkboxes_and_send_button(self):
        blocks = build_options_blocks(["A", "B"])
        actions = blocks[0]
        assert actions["type"] == "actions"
        elements = actions["elements"]
        assert elements[0]["type"] == "checkboxes"
        assert elements[0]["action_id"] == OPTIONS_CHECKBOXES_ACTION
        assert elements[1]["type"] == "button"
        assert elements[1]["action_id"] == OPTIONS_SUBMIT_ACTION
        assert elements[1]["style"] == "primary"

    def test_checkbox_options_match_choices(self):
        blocks = build_options_blocks(["Alpha", "Beta", "Gamma"])
        opts = blocks[0]["elements"][0]["options"]
        assert len(opts) == 3
        assert opts[0]["value"] == "Alpha"
        assert opts[2]["text"]["text"] == "Gamma"

    def test_max_ten_choices(self):
        choices = [f"C{i}" for i in range(15)]
        blocks = build_options_blocks(choices)
        opts = blocks[0]["elements"][0]["options"]
        assert len(opts) == 10

    def test_truncates_long_choice_text(self):
        long = "x" * 100
        blocks = build_options_blocks([long])
        text = blocks[0]["elements"][0]["options"][0]["text"]["text"]
        assert len(text) <= 75

    def test_send_button_text(self):
        blocks = build_options_blocks(["A"])
        btn = blocks[0]["elements"][1]
        assert btn["text"]["text"] == "Send"


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
