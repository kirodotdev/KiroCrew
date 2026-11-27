"""The hook-script header grammar reads a long header line in linear time.

A lazy value ``(\\S.*?)`` beside a trailing ``\\s*$`` rescanned the whitespace run
after every value character, so a header line holding a long run of spaces cost
time quadratic in its length: about a second at 20,000 spaces. The fix keeps the
captured key and value byte-identical.
"""

from __future__ import annotations

import re

import pytest

from conftest import assert_rejected_without_backtracking
from kiro_crew.agent_materialization import kiro_hooks

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(r"^\s*#\s*(event|matcher)\s*:\s*(\S.*?)\s*$", re.IGNORECASE)


@pytest.mark.parametrize(
    "line",
    [
        "# event: preToolUse\n",
        "# event: preToolUse",
        "#event:stop\n",
        "  # Matcher :  execute_bash  \n",
        "# matcher: fs_write fs_read\t\r\n",
        "# event: a\n",
        "# event:\u00a0spaced\u00a0value\u00a0\n",
        "# event: \n",
        "# event:\n",
        "# event:   \t \n",
        "# event preToolUse\n",
        "# other: x\n",
        "#!/bin/sh\n",
        "# event: two\nlines\n",
        "# event: x" + " " * 64 + "y\n",
    ],
)
def test_the_fix_captures_exactly_what_the_old_grammar_did(line):
    old = QUADRATIC_GRAMMAR.match(line)
    new = kiro_hooks._HOOK_HEADER_RE.match(line)
    assert (old is None) == (new is None)
    if old is not None and new is not None:
        assert new.groups() == old.groups()


def test_a_header_value_with_a_long_interior_space_run_is_read_in_linear_time():
    def read(text: str) -> None:
        match = kiro_hooks._HOOK_HEADER_RE.match(text)
        assert match is not None and match.group(2) == text[len("# event: ") : -1]

    assert_rejected_without_backtracking(read, lambda n: "# event: x" + " " * n + "y\n")
