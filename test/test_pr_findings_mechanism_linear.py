"""``pr_findings`` reads a disposition's ``mechanism:`` line in linear time.

A lazy ``(.+?)`` beside a trailing ``\\s*$`` rescanned the whitespace after the
text once per character, so a PR comment whose ``mechanism:`` line held a long
run of spaces cost time quadratic in its length: about a second at 20,000
spaces. The fix keeps every match, span and capture identical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from conftest import assert_rejected_without_backtracking

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/pr_findings.py"
)
MECHANISM_RE = load_skill_script("pr_findings_mechanism_linear_under_test", SCRIPT).MECHANISM_RE

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(r"^mechanism:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)


def _observed(pattern: re.Pattern[str], body: str):
    return [(found.span(), found.groups()) for found in pattern.finditer(body)]


@pytest.mark.parametrize(
    "body",
    [
        "mechanism: a retry loop around the fetch",
        "> fixed\nself-added: yes\nMechanism: cache the lookup  \nmore text",
        "mechanism:no space",
        "mechanism: a  b\t\r\nmechanism: second",
        "mechanism:\u00a0spaced\u00a0",
        "mechanism:",
        "mechanism:   ",
        "mechanism: \n",
        "mechanism:\n\nnext paragraph",
        "mechanism:  \n  \n",
        "mechanism: x\n\nmechanism: y\n",
        "not a mechanism: line",
        "mechanism: x" + " " * 64 + "y",
    ],
)
def test_the_fix_matches_exactly_what_the_old_grammar_did(body):
    assert _observed(MECHANISM_RE, body) == _observed(QUADRATIC_GRAMMAR, body)


def test_a_mechanism_line_with_a_long_space_run_is_read_in_linear_time():
    def read(text: str) -> None:
        assert MECHANISM_RE.findall(text) == [text[len("mechanism: ") :]]

    assert_rejected_without_backtracking(read, lambda n: "mechanism: x" + " " * n + "y")
