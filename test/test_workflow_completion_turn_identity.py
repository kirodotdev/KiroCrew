from __future__ import annotations

import asyncio
from unittest.mock import patch

from kiro_crew.dashboard.chat_turn.queue_drain import _queued_workflow_run_id
from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot


class _State:
    def __init__(self) -> None:
        self._background_tasks: set[asyncio.Task] = set()


class _Slot:
    def __init__(self, *, running: bool) -> None:
        self.running = running
        self.key = "dashboard:chat-1"
        self.task = None
        self.queue: list[tuple[str, dict]] = []

    def append(self, *_args) -> None:
        pass

    def queue_append(self, prompt: str, *, meta: dict) -> None:
        self.queue.append((prompt, meta))


def test_immediate_workflow_completion_turn_receives_run_id() -> None:
    async def exercise() -> str:
        state = _State()
        slot = _Slot(running=False)
        received: list[str] = []

        async def run_chat(_state, _slot, _prompt, *, _workflow_run_id: str = "") -> None:
            received.append(_workflow_run_id)

        started = _ChatSlot.enqueue_or_run_prompt(
            slot,
            "workflow finished",
            run_chat,
            state,
            workflow_run_id="run-immediate",
        )
        await asyncio.sleep(0)
        assert started is True
        return received[0]

    assert asyncio.run(exercise()) == "run-immediate"


def test_queued_workflow_completion_retains_run_id_for_drain() -> None:
    slot = _Slot(running=True)
    state = _State()
    with (
        patch("kiro_crew.dashboard.chat_delivery.start_queue_persist"),
        patch(
            "kiro_crew.dashboard.session_control.containment_meta",
            return_value={"containment": "captured"},
        ),
    ):
        started = _ChatSlot.enqueue_or_run_prompt(
            slot,
            "workflow finished",
            lambda *_args, **_kwargs: None,
            state,
            workflow_run_id="run-queued",
        )

    assert started is False
    assert _queued_workflow_run_id({"meta": slot.queue[0][1]}) == "run-queued"
    assert slot.queue[0][1]["containment"] == "captured"
