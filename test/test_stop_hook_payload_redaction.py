"""A Stop hook reads the finished answer redacted in full, whatever the owner's setting.

A hook is a sink no output seam lists, so the turn's chat seam opened with the setting
off must not hand it the raw answer.
"""

from __future__ import annotations

import pytest
import turn_harness
from turn_harness import TurnScript, run_turn

from kiro_crew import redaction_seams
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.hooks import HOOK_EVENT_STOP
from kiro_crew.testing.synthetic_credentials import github_classic


@pytest.mark.asyncio
@pytest.mark.parametrize("off", [False, True])
async def test_the_stop_hook_reads_the_answer_redacted(monkeypatch, off):
    token = github_classic(seed=413)
    fired: list[tuple[object, tuple, dict]] = []

    async def record(self, event, *args, **kwargs):
        fired.append((event, args, kwargs))
        return []

    monkeypatch.setattr(turn_harness._NoHooks, "fire", record)
    monkeypatch.setattr(redaction_seams, "_redaction_off", off)
    reply = AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"pushed with {token}")
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    await run_turn(TurnScript(events=[reply, done]))

    stops = [call for call in fired if call[0] == HOOK_EVENT_STOP]
    assert stops, [call[0] for call in fired]
    assert all(token not in repr(call) for call in stops)
    assert any("pushed with" in repr(call) for call in stops)
