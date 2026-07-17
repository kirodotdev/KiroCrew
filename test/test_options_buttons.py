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
