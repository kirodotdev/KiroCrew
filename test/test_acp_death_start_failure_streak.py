"""A shared runtime dying during a session start and the auto-nudge start streak.

``_mark_dead`` fails every pending request with an untagged ``AcpRuntimeDead``, so
a ``session/new`` cut short by the death reaches the chat runner without the
``session_start_failed`` tag that ``_note_cycle_start_failure`` counts. No loop on
that process -- the one whose start was interrupted or a sibling -- has its
start-failure streak advanced by the death.
"""

import asyncio

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _await_pending, _make_runtime, _start_reader, _stop_reader

from kiro_crew import autonudge as _an
from kiro_crew.acp.runtime import AcpRuntimeDead
from kiro_crew.acp.transport_errors import AcpProcessDied
from kiro_crew.dashboard.chat_turn.recovery import _note_cycle_start_failure
from kiro_crew.testing.wait import await_until

SLOT = "chat-1-987"


@pytest.mark.asyncio
async def test_a_runtime_death_during_session_new_advances_no_start_streak(monkeypatch):
    """Drives the real raise: ``session/new`` is in flight when stdout hits EOF.

    The exception the start raises is the one ``_mark_dead`` built, read off the
    interrupted ``create_session`` itself rather than constructed here, so a raise
    site that began tagging a death would redden this -- as would the helper
    counting a death without the tag. The ``AcpProcessDied`` form is the one the
    provider's ``_translate_dead`` hands callers that only catch ``AcpError``; it
    is checked too because the chat runner meets the death in that shape.
    """
    reported: list[str] = []

    class _Svc:
        _inflight_adds: set = set()

        def notify_cycle_start_failed(self, slot_key: str) -> None:
            reported.append(slot_key)

        def stop(self) -> None:
            return None

    monkeypatch.setattr(_an, "_INSTANCE", _Svc(), raising=False)

    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    start = asyncio.create_task(rt.create_session(mcp_servers=[]))
    try:
        await _await_pending(rt)
        reader.feed_eof()  # empty readline -> process exited -> _mark_dead
        with pytest.raises(AcpRuntimeDead) as info:
            await await_until(start, what="the session/new cut short by the death")
    finally:
        await _stop_reader(task)

    death = info.value
    assert rt._dead is True
    assert getattr(death, "session_start_failed", False) is False

    _note_cycle_start_failure(SLOT, death, self_wake=True)
    _note_cycle_start_failure(SLOT, AcpProcessDied(str(death)), self_wake=True)

    assert reported == []
