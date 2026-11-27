"""The steering-marker grammar rejects an unclosed frame in linear time.

CodeQL ``py/polynomial-redos``: ``\\s*`` after the colon and the lazy payload group
could both take the same whitespace, so a frame that failed to close cost time
quadratic in its length.
"""

from __future__ import annotations

import re

import pytest

from conftest import assert_rejected_without_backtracking
from kiro_crew.messaging import driver

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(
    r"^\[STEERING\s+steer-[0-9a-f-]+(?:\s*:\s*(.*?))?\]$",
    re.IGNORECASE | re.DOTALL,
)


@pytest.mark.parametrize(
    "frame",
    [
        "[STEERING steer-4a2f]",
        "[STEERING steer-4a2f: pause]",
        "[STEERING steer-4a2f-9b1c: stop and re-plan]",
        "[STEERING STEER-4A2F: pause]",
        "[steering steer-4a2f]",
        "[STEERING  steer-4a2f  :  spaced  ]",
        "[STEERING steer-4a2f:]",
        "[STEERING steer-4a2f:\t\t]",
        "[STEERING steer-4a2f:\n  two\nlines \t]",
        "[STEERING steer-4a2f :\u00a0pause]",
        "[STEERING steer-4a2f: pause",
        "[STEERING steer-4a2f pause]",
        "[STEERING steer-: x]",
        "[STEERING\tsteer--:\t\t\tx",
    ],
)
def test_the_fix_accepts_and_reads_exactly_what_the_old_grammar_did(frame):
    old = QUADRATIC_GRAMMAR.match(frame)
    new = driver._STEER_MARKER_RE.match(frame)
    assert (old is None) == (new is None)
    if old is not None and new is not None:
        assert (new.group(1) or "").strip() == (old.group(1) or "").strip()


def test_an_unclosed_frame_padded_with_whitespace_is_rejected_in_linear_time():
    def reject(text: str) -> None:
        assert driver._STEER_MARKER_RE.match(text) is None

    assert_rejected_without_backtracking(reject, lambda n: "[steering\tsteer--:" + "\t" * n)
