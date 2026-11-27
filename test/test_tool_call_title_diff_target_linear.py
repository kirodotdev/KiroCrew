"""The tool-call title reads a diff's ``+++`` target line in linear time.

A lazy ``(.+?)`` beside a trailing ``\\s*$`` rescanned the whitespace after the
path once per path character, so a diff whose ``+++`` line held a long run of
spaces cost time quadratic in its length: about a second at 20,000 spaces. The
fix keeps every match, span and capture identical.
"""

from __future__ import annotations

import re

import pytest

from conftest import assert_rejected_without_backtracking
from kiro_crew import tool_call_title

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$", re.MULTILINE)


def _observed(pattern: re.Pattern[str], text: str):
    found = pattern.search(text)
    return None if found is None else (found.span(), found.groups())


@pytest.mark.parametrize(
    "text",
    [
        "--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n",
        "+++ src/x.py",
        "+++ b/src/x.py  \t\r\n",
        "+++ /dev/null\n",
        "+++  leading space.py",
        "+++ b/a b.py",
        "+++ b/\u00a0x\u00a0",
        "+++ b/",
        "+++ b/\n",
        "+++ ",
        "+++   ",
        "+++ \n+++ b/next.py",
        "+++ b/x\n\n",
        "no diff here",
        "+++ b/x" + " " * 64 + "y",
    ],
)
def test_the_fix_matches_exactly_what_the_old_grammar_did(text):
    assert _observed(tool_call_title._DIFF_TARGET_RE, text) == _observed(QUADRATIC_GRAMMAR, text)


def test_a_target_line_with_a_long_space_run_is_read_in_linear_time():
    def read(text: str) -> None:
        found = tool_call_title._DIFF_TARGET_RE.search(text)
        assert found is not None and found.group(1) == text[len("+++ b/") :]

    assert_rejected_without_backtracking(read, lambda n: "+++ b/x" + " " * n + "y")
