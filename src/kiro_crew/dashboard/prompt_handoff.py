"""The point where a turn's prompt is handed to the engine, and waiting for it.

A send starts helpers for its session that run on the shared background runtime:
the session title, a fork's name, topic tags. The first of them after a gateway
start has to start that runtime, and it holds the runtime's lock for the whole
start. A turn's admission takes the same lock for its account check, so a helper
that reaches the runtime first makes the turn a person is waiting on wait for a
process start that is not its own.

The ordering is decided here and nowhere else. The chat runner calls
:func:`prompt_handed_off` where a turn has passed admission, the session claim,
the prompt build and every dispatch gate and has opened its stream to the
engine. A helper awaits :func:`turn_handed_off` before it touches the background
runtime. That returns at once when no turn of the slot is on its way to the mark
(none was started, or the running one is past it), and when the turn's task ends
without reaching it (refused, failed, stopped), so no helper is left waiting for
a handoff that will not come.
"""

from __future__ import annotations

import asyncio
from typing import Any


def _turn_before_handoff(slot: Any) -> asyncio.Task[Any] | None:
    """The slot's running turn task while that turn has not reached the mark.

    A slot that publishes no task (a tagging caller's stand-in) has no turn.
    """
    task: asyncio.Task[Any] | None = getattr(slot, "task", None)
    if task is None or task.done() or slot._handoff_generation == slot._turn_generation:
        return None
    # A helper the turn itself awaits cannot wait for that turn.
    return None if task is asyncio.current_task() else task


async def turn_handed_off(slot: Any) -> None:
    """Wait until no turn of *slot* is on its way to the engine.

    Checked again after each wake: a turn that ends before its handoff may have
    passed the slot to a queued one, which is then the turn not to delay.
    """
    while (task := _turn_before_handoff(slot)) is not None:
        mark: asyncio.Future[None] | None = slot._handoff_mark
        if mark is None:
            mark = slot._handoff_mark = asyncio.get_running_loop().create_future()
        # ``wait`` neither cancels the turn nor re-raises how it ended.
        await asyncio.wait({mark, task}, return_when=asyncio.FIRST_COMPLETED)
        if not mark.done():
            # The turn ended first, and no handoff will resolve this mark. Resolve
            # it here, so every helper parked on it looks again with this one: a
            # helper that joined it while watching a queued successor would
            # otherwise stay on a mark the successor's handoff never resolves.
            mark.set_result(None)
            if slot._handoff_mark is mark:
                slot._handoff_mark = None


def prompt_handed_off(slot: Any) -> None:
    """The slot's running turn opened its stream to the engine: wake what waited."""
    slot._handoff_generation = slot._turn_generation
    mark: asyncio.Future[None] | None = slot._handoff_mark
    slot._handoff_mark = None
    if mark is not None and not mark.done():
        mark.set_result(None)
