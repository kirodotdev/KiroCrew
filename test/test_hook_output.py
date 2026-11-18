"""``bounded_hook_output`` (issue #38593): the shared helper every PostToolUse
call site (ACP, dashboard, subagent, messaging) uses to bound a hook's
``tool_response.output`` and report whether it was cut."""

from __future__ import annotations

from kiro_crew.hook_output import HOOK_OUTPUT_CHAR_CAP, bounded_hook_output


def test_the_cap_is_2000():
    assert HOOK_OUTPUT_CHAR_CAP == 2000


def test_a_long_output_is_cut_and_the_cut_is_reported():
    text = "x" * 7901
    result = bounded_hook_output(text)
    assert result == {
        "output": "x" * 2000,
        "truncated": True,
        "output_length": 7901,
    }


def test_a_short_output_arrives_unchanged_and_untruncated():
    text = "y" * 1500
    result = bounded_hook_output(text)
    assert result == {
        "output": text,
        "truncated": False,
        "output_length": 1500,
    }


def test_exactly_at_the_cap_is_not_truncated():
    text = "z" * 2000
    result = bounded_hook_output(text)
    assert result["truncated"] is False
    assert result["output_length"] == 2000
    assert result["output"] == text


def test_empty_output():
    result = bounded_hook_output("")
    assert result == {"output": "", "truncated": False, "output_length": 0}


def test_a_custom_limit_is_honoured():
    result = bounded_hook_output("a" * 10, limit=5)
    assert result == {"output": "aaaaa", "truncated": True, "output_length": 10}
