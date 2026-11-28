"""The review contract reads a ``Clears when:`` line in linear time.

A lazy ``(.+?)`` between two ``\\s*`` runs let a long whitespace run be split
between quantifiers in every way, so a design item whose ``Clears when:`` text
held one cost at least quadratic time: about a second at 20,000 spaces. The fix
yields the same stripped text the reader stores.
"""

from __future__ import annotations

import re

import pytest
from ai_review_workflows_helpers import _review_contract_module

from conftest import assert_rejected_without_backtracking

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(r"clears\s+when\s*:\s*(.+?)\s*$", re.IGNORECASE)


def _stored(pattern: re.Pattern[str], text: str) -> str:
    """What the reader keeps: the stripped capture, or empty with no match."""
    found = pattern.search(text)
    return found.group(1).strip() if found else ""


@pytest.mark.parametrize(
    "text",
    [
        "Clears when: the cap is removed",
        "Trigger: x\nOutcome: y\nClears when: the cap is removed\n",
        "clears when:the test pins it",
        "CLEARS  WHEN :  spaced out  \t",
        "Clears\nwhen:\n  the next line",
        "Clears when: x",
        "Clears when:\u00a0nbsp\u00a0",
        "Clears when:",
        "Clears when:   ",
        "Clears when: \n",
        "Clears when:\n\n",
        "Clears when: two\nlines",
        "Clears when: first\nClears when: second",
        "clears when: x" + " " * 64 + "y",
        "no such line here",
    ],
)
def test_the_fix_stores_exactly_what_the_old_grammar_did(text):
    contract = _review_contract_module()
    assert _stored(contract.CLEARS_WHEN_RE, text) == _stored(QUADRATIC_GRAMMAR, text)


def test_a_clears_when_line_with_a_long_space_run_is_read_in_linear_time():
    contract = _review_contract_module()

    def read(text: str) -> None:
        assert _stored(contract.CLEARS_WHEN_RE, text) == text[len("Clears when: ") :]

    assert_rejected_without_backtracking(read, lambda n: "Clears when: x" + " " * n + "y")
