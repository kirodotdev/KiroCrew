"""``pre_push_gate`` checks a ``Gate proof:`` line in linear time.

A lazy ``skipped`` capture beside a trailing ``\\s*$`` rescanned the whitespace
after it once per character, so a PR body whose proof line held a long run of
spaces cost time quadratic in its length: about a second at 20,000 spaces. The
fix keeps every match, span and capture identical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from conftest import assert_rejected_without_backtracking

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/pre_push_gate.py"
)
PROOF_LINE = load_skill_script("pre_push_gate_proof_line_linear_under_test", SCRIPT).PROOF_LINE

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(
    r"^Gate proof:\s*tree\s+(?P<tree>[0-9a-f]{12});\s*ran\s+(?P<ran>\d+);"
    r"\s*skipped\s+(?P<skipped>.+?)\s*$"
)

HEAD = "Gate proof: tree 0123456789ab; ran 4; skipped "


def _observed(pattern: re.Pattern[str], line: str):
    found = pattern.match(line)
    return None if found is None else (found.span(), found.groupdict())


@pytest.mark.parametrize(
    "line",
    [
        HEAD + "none",
        HEAD + "tool-missing:uv, platform:windows",
        "Gate proof:tree 0123456789ab;ran 4;skipped none",
        HEAD + "a  b\t",
        HEAD + "\u00a0x\u00a0",
        HEAD.rstrip(),
        HEAD + "  ",
        HEAD + "\t\r",
        HEAD + "x\ny",
        HEAD + "x\n",
        "Gate proof: tree 0123456789AB; ran 4; skipped none",
        "Gate proof: tree 0123456789ab; ran x; skipped none",
        HEAD + "x" + " " * 64 + "y",
    ],
)
def test_the_fix_matches_exactly_what_the_old_grammar_did(line):
    assert _observed(PROOF_LINE, line) == _observed(QUADRATIC_GRAMMAR, line)


def test_a_proof_line_with_a_long_space_run_is_checked_in_linear_time():
    def read(text: str) -> None:
        found = PROOF_LINE.match(text)
        assert found is not None and found.group("skipped") == text[len(HEAD) :]

    assert_rejected_without_backtracking(read, lambda n: HEAD + "x" + " " * n + "y")
