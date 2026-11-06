"""The provider-banner matchers compare fixed text exactly, with no regular expression.

The banner is ``You have <count> weighted tokens left``: its fixed parts are compared
exactly and the count is checked digit by digit. Ordinary assistant text that uses
similar words is kept.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp._dispatch_parts import tool_result_events
from kiro_crew.dashboard import chat_utils_turn_text as turn_text

BANNER = "You have 8154 weighted tokens left"


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        (BANNER, ""),
        (BANNER + ".", ""),
        ("  You have 8,154 weighted tokens left!\n\nDone.", "Done."),
        ("You have 12,345,678 weighted tokens left\r\n  Next", "Next"),
        (BANNER + "Here is the answer.", "Here is the answer."),
        ("\tYou have 0 weighted tokens left \t", ""),
    ],
)
def test_the_exact_banner_is_stripped(text: str, rest: str) -> None:
    assert turn_text.strip_provider_budget_banner(text) == (rest, True)


@pytest.mark.parametrize(
    "text",
    [
        "you have 8154 weighted tokens left",  # case differs
        "You have 8154 weighted tokens leftover",  # lowercase continuation
        "You have 8154 weighted tokens left, so here is more.",
        "You have 8154  weighted tokens left",  # two spaces
        "You have 1234,567 weighted tokens left",  # bad grouping
        "You have 8,154, weighted tokens left",
        "You have \u0668\u0661\u0665\u0664 weighted tokens left",  # non-ASCII digits
        "You have many weighted tokens left",
        "You have 8154 tokens left",
        "The tool said: You have 8154 weighted tokens left",  # not leading
        "> You have 8154 weighted tokens left",  # quoted
        "You have 8154 weighted tokens left.\r Done",  # a bare CR is no line break
        "I counted. You have 8154 weighted tokens left in the plan.",
    ],
)
def test_similar_assistant_text_is_kept(text: str) -> None:
    assert turn_text.strip_provider_budget_banner(text) == (text, False)


@pytest.mark.parametrize(
    ("message", "mentions"),
    [
        ("How many TOKENS do I have?", True),
        ("what is the\n  context   window here", True),
        ("check the quota.", True),
        ("context-window", False),
        ("tokenize this", False),
        ("my_budget_file", False),
        ("\u0130budget", False),  # a letter glued to the word
        ("please continue", False),
    ],
)
def test_the_topic_gate_compares_whole_words(message: str, mentions: bool) -> None:
    assert turn_text.mentions_provider_budget(message) is mentions


@pytest.mark.parametrize(
    ("status", "is_code"),
    [
        ("0", True),
        ("-1", True),
        ("exit status: 1", True),
        ("Exit Code 127", True),
        ("exit_code:2", True),
        ("1234", False),
        ("exit status:  1", False),
        ("exit 1", False),
        ("done 1", False),
    ],
)
def test_an_exit_code_status_is_read_without_a_pattern(status: str, is_code: bool) -> None:
    assert tool_result_events._is_exit_code_text(status) is is_code


def test_json_escapes_decode_by_exact_reading() -> None:
    decode = turn_text._decode_json_unicode_escapes
    assert decode("\\u0668\\u0661") == "\u0668\u0661"
    assert decode("\\\\u0038") == "8"
    assert decode("\\ud83d\\ude00") == "\U0001f600"
    assert decode("\\ud83d alone") == "\\ud83d alone"
    assert decode("\\u00zz") == "\\u00zz"


def test_the_banner_matchers_use_no_regex() -> None:
    for fn in (
        turn_text.strip_provider_budget_banner,
        turn_text.mentions_provider_budget,
        turn_text._drop_digit_grouping,
        turn_text._banner_count,
        turn_text._carries_count,
        turn_text._names_whole_number,
        turn_text._decode_json_unicode_escapes,
        tool_result_events._is_exit_code_text,
        tool_result_events._is_run_status,
    ):
        assert "re" not in fn.__code__.co_names, fn.__name__


def test_a_long_undecodable_backslash_run_decodes_in_linear_time(monkeypatch) -> None:
    """Each failed start must not rescan the run: 200,000 backslashes before an
    invalid escape stay as written after ONE escape read, not one per backslash
    (which made the decode quadratic)."""
    reads = []
    real = turn_text._escape_hex_at

    def _counted(text: str, pos: int):
        reads.append(pos)
        return real(text, pos)

    monkeypatch.setattr(turn_text, "_escape_hex_at", _counted)
    text = "\\" * 200_000 + "uZZZZ"
    assert turn_text._decode_json_unicode_escapes(text) == text
    assert reads == [0]
    assert turn_text._decode_json_unicode_escapes("\\\\\\x \\u0038") == "\\\\\\x 8"
