"""The MAB suite reads a numbered fact line in linear time.

A lazy ``(.+?)`` beside a trailing ``\\s*$`` rescanned the whitespace after the
fact once per fact character, so a context line holding a long run of spaces
cost time quadratic in its length: about a second at 20,000 spaces. The fix
keeps every match, span and capture identical.
"""

from __future__ import annotations

import re

import pytest

from conftest import assert_rejected_without_backtracking
from kiro_crew.eval.bench.suites import mab

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")


def _observed(pattern: re.Pattern[str], line: str):
    found = pattern.match(line)
    return None if found is None else (found.span(), found.groups())


@pytest.mark.parametrize(
    "line",
    [
        "1. Alice lives in Paris.",
        "  12. Bob moved to Rome.  ",
        "3.\tTabbed fact",
        "4. a  b\t\r",
        "5.\u00a0spaced\u00a0",
        "6.",
        "7. ",
        "8.   ",
        "9. \t\r",
        "10. two\nlines",
        "11. x\n",
        "12.no space",
        "not a fact",
        "13. x" + " " * 64 + "y",
    ],
)
def test_the_fix_matches_exactly_what_the_old_grammar_did(line):
    assert _observed(mab._FACT, line) == _observed(QUADRATIC_GRAMMAR, line)


def test_a_fact_line_with_a_long_space_run_is_read_in_linear_time():
    def read(text: str) -> None:
        found = mab._FACT.match(text)
        assert found is not None and found.group(2) == text[len("1. ") :]

    assert_rejected_without_backtracking(read, lambda n: "1. x" + " " * n + "y")
