"""A turn killed at high context is compacted before its replay is sent.

The tool-stall watchdog kills the agent and the runner replays the interrupted
turn verbatim. Replaying an oversized prompt is what stalled in the first
place, so the next replay stalls too, and the kill/replay cycle repeats every
stall window. The death handler leaves the dying turn's last context reading on
the slot (the replay's own session meter starts empty), and the dispatch prep
compacts on it before the prompt is rebuilt.

These tests pin that guard: it fires at or above the session's own threshold,
declines below it, consumes the reading exactly once, and never lets a failed
or missing compaction block the replay it exists to make cheaper.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPACTION_STATUS
from kiro_crew.dashboard import chat_runner


class _Provider:
    """A provider that records ``/compact`` and reports one terminal status."""

    def __init__(self, status: str | None = "completed") -> None:
        self.calls: list[str] = []
        self.status = status

    def stream_command(self, command: str):
        self.calls.append(command)

        async def _events():
            if self.status is not None:
                yield SimpleNamespace(kind=EVENT_COMPACTION_STATUS, text=self.status)

        return _events()

    async def wait_for_compaction(self, timeout: float = 0.0):
        return {"type": self.status}


def _turn(
    pct: float | None,
    *,
    threshold: float = 40.0,
    provider: _Provider | None = None,
    compacting: bool = False,
):
    provider = provider or _Provider()
    slot = SimpleNamespace(_replay_compact_pct=pct)
    sessions = SimpleNamespace(
        effective_autocompact_pct=lambda _key: threshold,
        is_compacting=lambda _key: compacting,
        compact_wait_budget_secs=lambda: 5.0,
    )
    return (
        SimpleNamespace(
            slot=slot,
            state=SimpleNamespace(sessions=sessions),
            session_key="chat-1",
            client=provider,
        ),
        provider,
    )


@pytest.mark.asyncio
async def test_compacts_at_or_above_the_session_threshold():
    turn, provider = _turn(63.0, threshold=40.0)

    await chat_runner._compact_replay_before_prompt(turn)

    assert provider.calls == ["/compact"]
    # Consumed: a later, unrelated turn must not compact on the same reading.
    assert turn.slot._replay_compact_pct is None


@pytest.mark.asyncio
async def test_declines_below_the_threshold_but_still_consumes_the_reading():
    turn, provider = _turn(20.0, threshold=40.0)

    await chat_runner._compact_replay_before_prompt(turn)

    assert provider.calls == []
    assert turn.slot._replay_compact_pct is None


@pytest.mark.asyncio
async def test_no_recorded_reading_is_a_no_op():
    turn, provider = _turn(None)

    await chat_runner._compact_replay_before_prompt(turn)

    assert provider.calls == []


@pytest.mark.asyncio
async def test_skips_while_another_compaction_is_in_progress():
    turn, provider = _turn(80.0, compacting=True)

    await chat_runner._compact_replay_before_prompt(turn)

    assert provider.calls == []


@pytest.mark.asyncio
async def test_failed_compaction_does_not_block_the_replay():
    turn, provider = _turn(80.0, provider=_Provider(status="failed"))

    await chat_runner._compact_replay_before_prompt(turn)

    assert provider.calls == ["/compact"]
    assert turn.slot._replay_compact_pct is None
