"""A slim resume that never lands re-arms the re-injection for the next turn.

The resume's prompt carried the lessons and steering a compacted restored
history may have lost; the next turn is warm, so without the re-arm they are
gone for the rest of the session.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _Stop(Exception):
    pass


def _turn(*, slim: bool, landed: bool = False, is_slash: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        _replay_accepted_this_turn=False,
        _responsive_recovery=[],
        _responsive_turn=SimpleNamespace(recovery=[]),
        _terminal_synthetic=False,
        session_key="dashboard:s1",
        state=MagicMock(),
        _admission_slot=None,
        _drained_subagent_notices=[],
        _needs_reinjection=False,
        _member_session_start_pending=False,
        _slim_resume_pending=slim,
        is_slash=is_slash,
        _turn_landed=landed,
    )


def _rearmed(turn: SimpleNamespace) -> bool:
    seen: dict[str, bool] = {}

    def _record(_sessions, _key, *, consumed, landed):
        seen["rearmed"] = consumed and not landed
        raise _Stop

    # The settle's own globals: another test may have re-imported the module.
    settle = importlib.import_module(
        "kiro_crew.dashboard.chat_turn.prompt_assembly"
    )._turn_settle_debts
    with patch.dict(settle.__globals__, {"rearm_reinjection": _record}):
        with pytest.raises(_Stop):
            asyncio.run(settle(turn))
    return seen["rearmed"]


@pytest.mark.parametrize(
    "slim,landed,is_slash,expected",
    [
        (True, False, False, True),  # slim resume that never landed
        (True, True, False, False),  # slim resume that landed its blocks
        (True, True, True, True),  # slim resume spent on a slash command
        (False, False, False, False),  # warm or fresh turn owes nothing
    ],
)
def test_a_slim_resume_that_did_not_deliver_re_arms(slim, landed, is_slash, expected):
    assert _rearmed(_turn(slim=slim, landed=landed, is_slash=is_slash)) is expected
