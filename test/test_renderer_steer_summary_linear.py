"""The Discord and Telegram steer-chip summary rejects an unclosed frame in linear time.

Both renderers take a frame and its summary from ``messaging.steer_ack``. Against
the original summary grammar, kept below as the oracle, the shared reader must find
the same frames and the same summary text, and an unclosed frame padded with
whitespace must cost it linear time to reject.
"""

from __future__ import annotations

import re

import pytest

from conftest import assert_rejected_without_backtracking
from kiro_crew.messaging.steer_ack import find_steering_acks

#: The renderers' summary grammar before the shared reader, kept only as the oracle.
QUADRATIC_GRAMMAR = re.compile(r"\[STEERING\s+steer-[0-9a-f-]+\s*:\s*([^\]]*)\]", re.IGNORECASE)

FRAMES = [
    "[STEERING steer-4a2f: pause]",
    "[STEERING steer-4a2f-9b1c: stop and re-plan]",
    "[STEERING STEER-4A2F: pause]",
    "[steering steer-4a2f: pause]",
    "[STEERING  steer-4a2f  :  spaced  ]",
    "[STEERING steer-4a2f:]",
    "[STEERING steer-4a2f: ]",
    "[STEERING steer-4a2f:\t\t]",
    "[STEERING steer-4a2f:\n  two\nlines \t]",
    "[STEERING steer-4a2f :\u00a0pause]",
    "[STEERING steer-4a2f: pause] tail ]",
    "[STEERING steer-4a2f: pause",
    "[STEERING steer-4a2f pause]",
    "[STEERING steer-: x]",
    "[STEERING\tsteer--:\t\t\tx",
]


@pytest.mark.parametrize("frame", FRAMES)
def test_the_shared_reader_captures_what_the_old_grammar_did(frame):
    old = QUADRATIC_GRAMMAR.match(frame)
    acks = find_steering_acks(frame, unbalanced=False)
    if old is None:
        assert acks == []
    else:
        (ack,) = acks
        assert frame[ack.start : ack.end] == old.group(0)
        assert ack.summary.lstrip() == old.group(1)


def test_an_unclosed_frame_padded_with_whitespace_is_rejected_in_linear_time():
    def reject(text: str) -> None:
        assert find_steering_acks(text, unbalanced=False) == []

    assert_rejected_without_backtracking(reject, lambda n: "[steering\tsteer--:" + "\t" * n)
