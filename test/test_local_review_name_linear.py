"""``local_review`` reads a workflow step's ``name:`` line in linear time.

A lazy ``(.+?)`` beside a trailing ``\\s*$`` rescanned the whitespace after the
name once per name character, so a long run of spaces inside a name cost time
quadratic in its length: about a second at 20,000 spaces. The fix keeps every
match, span and capture identical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from conftest import assert_rejected_without_backtracking

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/local_review.py"
)
NAME_RE = load_skill_script("local_review_name_linear_under_test", SCRIPT)._NAME_RE

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(r"^\s*(?:-\s+)?name:\s*(.+?)\s*$")


def _observed(pattern: re.Pattern[str], line: str):
    found = pattern.match(line)
    return None if found is None else (found.span(), found.groups())


@pytest.mark.parametrize(
    "line",
    [
        "- name: Run the review",
        "      - name: 'quoted step'  ",
        "name:Run",
        "  name: x",
        "- name: a  b\t",
        "- name:\u00a0spaced\u00a0",
        "- name:",
        "- name:   ",
        "- name: \t\r",
        "- name: two\nlines",
        "- name: x\n",
        "-name: dash without space",
        "  run: echo hi",
        "- name: x" + " " * 64 + "y",
    ],
)
def test_the_fix_matches_exactly_what_the_old_grammar_did(line):
    assert _observed(NAME_RE, line) == _observed(QUADRATIC_GRAMMAR, line)


def test_a_step_name_with_a_long_space_run_is_read_in_linear_time():
    def read(text: str) -> None:
        found = NAME_RE.match(text)
        assert found is not None and found.group(1) == text[len("- name: ") :]

    assert_rejected_without_backtracking(read, lambda n: "- name: x" + " " * n + "y")
