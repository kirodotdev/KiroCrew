"""``sweep_check`` reads a sweep entry's field line in linear time.

A lazy ``(.*?)`` beside a trailing ``\\s*$`` rescanned the whitespace after the
value once per value character, so a field line holding a long run of spaces
cost time quadratic in its length: about a second at 20,000 spaces. The fix
keeps every match, span and capture identical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from conftest import assert_rejected_without_backtracking

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/sweep_check.py"
)
FIELD_RE = load_skill_script("sweep_check_field_linear_under_test", SCRIPT).FIELD_RE

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(r"^(class|search|risk|resolution|sites|branches):[ \t]*(.*?)\s*$")


def _observed(pattern: re.Pattern[str], line: str):
    found = pattern.match(line)
    return None if found is None else (found.span(), found.groups())


@pytest.mark.parametrize(
    "line",
    [
        "class: lazy capture beside a trailing whitespace run",
        "search: git grep -nE 'x'  ",
        "risk:low",
        "sites:",
        "branches:   ",
        "resolution:\t\t",
        "class: a  b\t\r",
        "class:\x0bvertical tab first",
        "class:\u00a0spaced\u00a0",
        "class: x\ny",
        "class: x\n",
        "other: x",
        "class: x" + " " * 64 + "y",
    ],
)
def test_the_fix_matches_exactly_what_the_old_grammar_did(line):
    assert _observed(FIELD_RE, line) == _observed(QUADRATIC_GRAMMAR, line)


def test_a_field_line_with_a_long_space_run_is_read_in_linear_time():
    def read(text: str) -> None:
        found = FIELD_RE.match(text)
        assert found is not None and found.group(2) == text[len("class: ") :]

    assert_rejected_without_backtracking(read, lambda n: "class: x" + " " * n + "y")
