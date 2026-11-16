"""Turns the agent starts on its own, on a session of the shared runtime.

Claude Code can start a turn after the dashboard's turn has ended: a
``run_in_background`` command exits and it answers the task notification, or a
background agent asks for a permission. On a per-chat ``AcpClient`` the idle
reader sees that turn (``client_transport._install_idle_reader``). On the shared
runtime every frame of a session already lands in its queue, so nothing has to
read stdout: the queue tells its handle about each frame it takes
(:class:`AgentTurnQueue`), and a frame that starts a turn while no turn reads
calls the chat's callback. The chat then reads that turn with
:func:`stream_unsolicited`, which sends no prompt and runs the user turn's own
loop (``_run_turn``), so permission cards, tool rows and the turn's terminal are
the user turn's own.

Only a host with per-session state builds its queues this way
(``HarnessAdapter.per_session_state``, claude): every other session keeps a
plain ``asyncio.Queue`` and never reaches this module (harness-parity H13).

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none (its three fields are class defaults on the handle).

Instance fields read: ``_abandoned_prompt_id``, ``_awaited_responses``, ``_queue``,
``_runtime``, ``_turn_done``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING

from kiro_crew.acp.client_transport import _AGENT_STARTED_REQUEST_ID, _starts_agent_turn
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The handle's own logger, as every owner module of it logs.
logger = logging.getLogger("kiro_crew.acp.session_handle")

#: The request id an agent-started turn reads under: the per-chat client's, which
#: no response can match (the runtime's ids start at 1 and only grow).
AGENT_STARTED_REQUEST_ID = _AGENT_STARTED_REQUEST_ID


class AgentTurnQueue(asyncio.Queue):  # type: ignore[type-arg]
    """A session queue that tells its handle about each frame it takes.

    ``asyncio.Queue.put`` ends in ``put_nowait``, so this one override sees every
    frame however the runtime queued it. The hook runs after the frame is queued,
    so a turn it starts finds the frame already there.
    """

    def __init__(self) -> None:
        super().__init__()
        self.on_frame: Callable[[JsonRpcMessage], None] | None = None
        #: Whether a frame queued now can open a turn its handle reads; None while
        #: unwatched. The runtime asks before routing a child's request here
        #: between turns, so one no turn will read is answered fail-closed instead.
        self.opens_turn: Callable[[], bool] | None = None

    def put_nowait(self, item: JsonRpcMessage | None) -> None:
        super().put_nowait(item)
        hook = self.on_frame
        if hook is not None and item is not None:
            hook(item)


def watch_agent_turns(self: AcpSessionHandle, callback: Callable[[], None] | None) -> bool:
    """Call *callback* when the agent starts a turn on its own; ``None`` stops it.

    False on a session whose queue is a plain ``asyncio.Queue``: its host starts
    no such turn, so nothing is registered.
    """
    queue = self._queue
    if not isinstance(queue, AgentTurnQueue):
        return False
    self._on_agent_turn = callback
    queue.on_frame = None if callback is None else self._note_queued_frame
    # A prompt whose reader left before its terminal (``_abandoned_prompt_id``)
    # opens no turn until the next prompt closes it (``_note_queued_frame``,
    # ``_rearm_agent_turn``), so nothing would read a frame queued meanwhile.
    queue.opens_turn = None if callback is None else (lambda: self._abandoned_prompt_id is None)
    return True


def _note_queued_frame(self: AcpSessionHandle, msg: JsonRpcMessage) -> None:
    """Mark the turn *msg* starts while no turn or request reads this queue.

    Runs synchronously on the reader loop, so the callback is scheduled, not
    called: the reader must not wait on the chat's turn admission. Nothing is
    marked while a prompt's reader left before its terminal
    (``_abandoned_prompt_id``): its late frames are that prompt's tail, which the
    next turn's drain discards, as :func:`_rearm_agent_turn` holds.
    """
    if (
        self._agent_turn_start is not None
        or not self._turn_done.is_set()
        or self._awaited_responses
        or self._abandoned_prompt_id is not None
        or not _starts_agent_turn(msg, str(getattr(self._runtime, "acp_backend", "") or ""))
    ):
        return
    self._agent_turn_start = msg
    callback = self._on_agent_turn
    if callback is not None:
        asyncio.get_running_loop().call_soon(_call_agent_turn_callback, callback)


def _call_agent_turn_callback(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:
        logger.exception("on_agent_turn callback failed")


def agent_turn_pending(self: AcpSessionHandle) -> bool:
    """Whether a turn the agent started waits in this queue, unread."""
    return self._agent_turn_start is not None


def _take_agent_turn_frames(self: AcpSessionHandle, stale: JsonRpcMessage | None) -> list:
    """The frames from a pending agent-started turn's first frame on, if *stale* is it.

    Called by the pre-turn drain for each frame it takes. On the first frame of a
    pending turn it takes the rest of the queue too and ends the hand-off, so the
    turn starting now reads those frames as its own instead of dropping them, as
    a prompt on the per-chat client does. Empty for any other frame.
    """
    start = self._agent_turn_start
    if start is None or stale is not start:
        return []
    self._agent_turn_start = None
    kept: list[JsonRpcMessage | None] = [stale]
    while True:
        try:
            kept.append(self._queue.get_nowait())
        except asyncio.QueueEmpty:
            return kept


def _rearm_agent_turn(self: AcpSessionHandle) -> None:
    """After a turn ends, look again at what is still queued for a turn the agent began.

    A second cycle Claude Code starts while a turn reads arrives with no hand-off
    (``_note_queued_frame`` skips frames while a turn runs), and the turn that just
    ended stopped at its own cycle's end. Taking the queue and putting it back, in
    order, shows each frame to the hook again now that no turn reads, so the first
    one that starts a turn marks it and calls the chat back. Synchronous: no frame
    can arrive between the take and the put.
    """
    queue = self._queue
    if (
        not isinstance(queue, AgentTurnQueue)
        or queue.on_frame is None
        or self._agent_turn_start is not None
        # A turn whose reader left early: its own frames are still arriving, and
        # the next prompt closes it (``_close_abandoned_turn``).
        or self._abandoned_prompt_id is not None
    ):
        return
    held: list[JsonRpcMessage | None] = []
    while True:
        try:
            held.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    for frame in held:
        queue.put_nowait(frame)


async def stream_unsolicited(
    self: AcpSessionHandle, timeout: float | None = None
) -> AsyncGenerator[AcpEvent, None]:
    """Yield the events of the turn the agent started on its own, sending nothing.

    Yields nothing when no such turn is pending or a prompt took its frames.
    """
    if self._agent_turn_start is None:
        return
    async for event in self._run_turn(_no_request, timeout, agent_started=True):
        yield event


async def _no_request() -> tuple[str, dict]:
    """The request an agent-started turn would send: none (``_run_turn`` skips it)."""
    raise AssertionError("an agent-started turn sends no request")
