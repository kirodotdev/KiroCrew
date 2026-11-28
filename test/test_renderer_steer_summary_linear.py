"""The Discord and Telegram steer-summary grammar rejects an unclosed frame in linear time.

``\\s*`` after the colon and the summary class ``[^\\]]*`` could both take the same
whitespace, so a frame that never closed cost time quadratic in its length: about
a second at 20,000 tabs. The fix keeps the captured summary byte-identical.
"""

from __future__ import annotations

import re

import pytest

from conftest import assert_rejected_without_backtracking
from kiro_crew.discord import renderer_text as discord_text
from kiro_crew.telegram import renderer_display as telegram_display

#: The grammar before the fix, kept only as the oracle the fix must agree with.
QUADRATIC_GRAMMAR = re.compile(r"\[STEERING\s+steer-[0-9a-f-]+\s*:\s*([^\]]*)\]", re.IGNORECASE)

SUMMARY_GRAMMARS = [
    pytest.param(discord_text._STEER_SUMMARY_RE, id="discord"),
    pytest.param(telegram_display._STEER_SUMMARY_RE, id="telegram"),
]

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
    "[STEERING steer-4a2f]",
    "[STEERING steer-: x]",
    "[STEERING\tsteer--:\t\t\tx",
]


@pytest.mark.parametrize("grammar", SUMMARY_GRAMMARS)
@pytest.mark.parametrize("frame", FRAMES)
def test_the_fix_captures_exactly_what_the_old_grammar_did(grammar, frame):
    old = QUADRATIC_GRAMMAR.match(frame)
    new = grammar.match(frame)
    assert (old is None) == (new is None)
    if old is not None and new is not None:
        assert new.group(0) == old.group(0)
        assert new.group(1) == old.group(1)


@pytest.mark.parametrize("grammar", SUMMARY_GRAMMARS)
def test_an_unclosed_frame_padded_with_whitespace_is_rejected_in_linear_time(grammar):
    def reject(text: str) -> None:
        assert grammar.match(text) is None

    assert_rejected_without_backtracking(reject, lambda n: "[steering\tsteer--:" + "\t" * n)
