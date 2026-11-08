"""``messaging.channel_fair_admission``: a channel turn behind fair admission."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew.messaging import channel_fair_admission as cfa
from kiro_crew.messaging.dispatch_contract import Verdict
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.shared_crew import turn_gate


class _Renderer:
    def __init__(self, fail: bool = False) -> None:
        self.chunks: list[str] = []
        self.done = False
        self._fail = fail

    async def on_text_chunk(self, text: str) -> None:  # type: ignore[override]
        if self._fail:
            raise RuntimeError("send failed")
        self.chunks.append(text)

    async def on_done(self) -> None:  # type: ignore[override]
        self.done = True


# A virtual subclass: the note only needs the two methods it calls.
Renderer.register(_Renderer)


def _turns():
    return SimpleNamespace(
        channel="telegram",
        _outcome=Mock(side_effect=lambda verdict, monitor: (verdict, monitor)),
        _run=AsyncMock(return_value="ran"),
    )


def _refusing(monkeypatch):
    async def run_channel_turn(*, session_key, text, automation, run, refused):
        return await refused("crew_queue_full")

    monkeypatch.setattr(turn_gate, "run_channel_turn", run_channel_turn)


@pytest.mark.asyncio
async def test_an_admitted_turn_runs(monkeypatch):
    async def run_channel_turn(*, session_key, text, automation, run, refused):
        assert session_key == "telegram:1" and automation is False
        return await run()

    monkeypatch.setattr(turn_gate, "run_channel_turn", run_channel_turn)
    turns = _turns()
    asker = SimpleNamespace(session_key="telegram:1")
    assert await cfa.run_admitted(turns, asker, "hi", _Renderer()) == "ran"


@pytest.mark.asyncio
async def test_a_refused_inbound_turn_tells_the_sender(monkeypatch):
    _refusing(monkeypatch)
    renderer = _Renderer()
    outcome = await cfa.run_admitted(
        _turns(), SimpleNamespace(session_key="telegram:1"), "hi", renderer
    )
    assert outcome == (Verdict.BUSY, None)
    assert renderer.chunks and renderer.done


@pytest.mark.asyncio
async def test_a_refused_wake_answers_busy_silently(monkeypatch):
    _refusing(monkeypatch)
    renderer = _Renderer()
    monitor = object()
    outcome = await cfa.run_admitted(
        _turns(), SimpleNamespace(session_key="telegram:1"), "hi", renderer, monitor=monitor
    )
    assert outcome == (Verdict.BUSY, monitor) and renderer.chunks == []


@pytest.mark.asyncio
async def test_the_note_respects_mute_and_lazy_renderers():
    renderer = _Renderer()
    await cfa.note_capacity_refusal("telegram", renderer, lambda: True, "crew_queue_full")
    assert renderer.chunks == []

    async def source(_streaming: bool) -> _Renderer:
        return renderer

    await cfa.note_capacity_refusal("telegram", source, None, "crew_queue_full")
    assert renderer.chunks and renderer.done


@pytest.mark.asyncio
async def test_a_failed_note_is_best_effort():
    await cfa.note_capacity_refusal("telegram", _Renderer(fail=True), None, "crew_queue_full")
