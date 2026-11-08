"""An idle dashboard tab for tests that drive a sub-agent completion through ``on_done``.

An idle tab queues the completion and asks the queue drain to start it
(``subagent_injection._route_subagent_to_tab``). These tests stub the parent's turn
by patching ``kiro_crew.dashboard.chat_runner._run_chat``, the name the drain reads, and many of them use a
``MagicMock`` tab. The real drain lives in ``dashboard.chat_turn.queue_drain`` and
reads far more slot state than such a tab has, so the autouse fixture here swaps it
for :func:`drain_head_as_turn`: it starts the queue's head as a turn
through that ``_run_chat`` and settles the owed delivery through the real
``_arm_queued_delivery_settlement``, as the drain does.

:func:`open_idle_tab` gives a ``MagicMock`` tab what the idle branch reads before it
asks: an ``asyncio.Lock`` (a bare mock's ``_lock`` is ``unittest.mock``'s own
``threading.RLock``), a real queue, and admission fields that are all open (a bare
mock attribute is truthy, which reads as a Stop or a close in flight).

Import the fixture by name, with ``# noqa: F401``, to register it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest


def open_idle_tab(slot: Any) -> Any:
    """Give a ``MagicMock`` tab an open admission state and a real queue."""
    slot._lock = asyncio.Lock()
    slot._queue = []
    slot._turn_admission_reserved = False
    slot.is_closing = False
    slot._last_turn_auth_required = False
    slot._stopping = False
    slot._stop_state = "idle"

    def _append(content: str, kind: str = "", meta: Any = None, **_kw: Any) -> str:
        queue_id = f"q{len(slot._queue)}-{id(content)}"
        slot._queue.append({"id": queue_id, "content": content, "kind": kind, "meta": meta})
        return queue_id

    slot.queue_append.side_effect = _append
    return slot


async def drain_head_as_turn(state: Any, slot: Any, **_kw: Any) -> bool:
    """Start the queue's head as the tab's turn, the way the drain does."""
    from kiro_crew.dashboard import chat_runner

    if not slot._queue:
        return False
    head = slot._queue.pop(0)
    content = head["content"]
    consumed: list[bool] = [False]

    def _note_consumed(value: bool = True) -> None:
        consumed[0] = value

    owes = bool(slot.owes_subagent_delivery([content]))
    kwargs: dict[str, Any] = {"_directive_user_origin": False, "_turn_actor": "subagent"}
    if owes:
        kwargs["_on_consumed"] = _note_consumed
    task = asyncio.ensure_future(chat_runner._run_chat(state, slot, content, **kwargs))
    slot.task = task

    def _clear(done: Any) -> None:
        if slot.task is done:
            slot.task = None

    task.add_done_callback(_clear)
    if owes:
        chat_runner._arm_queued_delivery_settlement(state, slot, task, [content], consumed)
    return True


@pytest.fixture(autouse=True)
def _idle_tab_drains_head_as_turn(_floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from kiro_crew.slack import gateway

    _floor_monkeypatch.setattr(gateway, "_start_next_queued_turn", drain_head_as_turn)
    yield
