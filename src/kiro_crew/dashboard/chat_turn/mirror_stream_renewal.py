"""The dashboard mirror's Slack step list: moving it, and renewing it before Slack's limit.

A mirrored turn draws its tool steps as task cards on one ``chat.startStream``
stream in the linked thread (``tool_rows._on_tool_call``). Slack ends a stream 10
minutes after it opens, however often it is appended to, and turns the step still
running into an error (``kiro_crew.slack.stream_window``). The two Slack-native
paths renew at their next append, and their 30 s elapsed-time refresh is that
append during a long tool. The mirror draws a card only when a tool call starts,
so one tool that runs past the limit gets no append at all. The mirror therefore
renews on a timer of its own: :func:`_arm_mirror_stream_renewal` starts one task
per turn when its first stream opens, which wakes ``STREAM_RENEW_AFTER_S`` after
the live stream opened and moves it (:func:`_move_mirror_stream`), closing the
running step "continues below" on the old message and opening it again on the
fresh one. A tool call's own move, after a newer message in the thread, gives
the timer a younger stream to wait on.

The stream, its running step and its age are one turn's state with three
writers: the tool-call arm, this timer and the turn's teardown
(``session_reset._turn_release_session``). Each holds ``turn._mirror_stream_lock``
while it reads and changes them, and reads them afresh off the turn once it holds
it: the timer runs on a task of its own. A renewal is shielded, and teardown first cancels
the timer and then waits on the lock (:func:`_stop_mirror_stream_renewal`), so a
renewal under way finishes and teardown closes the stream it opened, never the
one it already closed. Teardown's ``finally`` cancels the timer once more
(:func:`_cancel_mirror_stream_renewal`): a cancellation that reaches teardown
before that wait would otherwise leave it renewing a finished turn's stream.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils import slack_mirror_is_paused
from kiro_crew.slack.stream_window import (
    CONTINUES_BELOW,
    STREAM_RENEW_AFTER_S,
    messages_noted,
    stream_clock,
    stream_due_for_renewal,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import logger
    from kiro_crew.dashboard.chat_turn.recipient import (
        cross_surface_withheld,
        slack_publication_withheld,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn


def _note_mirror_stream_opened(turn: _ChatTurn, noted: int | None) -> None:
    """Record when the turn's ``_mirror_stream_ts``, just set, opened: now, at *noted*.

    *noted* is the ``messages_noted()`` count read before Slack was asked. An empty
    ts means Slack gave none: the turn then has no live stream, nothing to move and
    nothing to renew.
    """
    opened = bool(turn._mirror_stream_ts)
    turn._mirror_stream_messages_noted = noted if opened else None
    turn._mirror_stream_started_at = stream_clock() if opened else 0.0


def _mirror_stream_due(turn: _ChatTurn) -> bool:
    """True when the mirror's live stream is old enough to renew before Slack ends it."""
    return stream_due_for_renewal(turn._mirror_stream_started_at, stream_clock())


async def _move_mirror_stream(
    slack_client: Any,
    channel: str,
    thread_ts: str,
    owner_id: str | None,
    stream_ts: str,
    running: tuple[str, str],
    *,
    continues: bool = False,
) -> tuple[str, int | None]:
    """Close the mirror's step list *stream_ts* and open a fresh one at the thread's end.

    *running* is the id and title of the step still open on it, closed where it
    was drawn so the old message ends complete. With *continues* the step is still
    running: it is closed with the "continues below" title and opened again on the
    fresh stream. Without it the next tool call is starting, so the step is done.
    Returns the new stream's ts and the ``messages_noted()`` count it opened at,
    or ``("", None)`` when Slack gave none: the turn then mirrors no more steps,
    as when its first stream is refused. Best-effort and never raises: the step
    list is decoration.
    """
    task_id, title = running
    try:
        if task_id:
            closing = title + CONTINUES_BELOW if continues else title
            await slack_client.append_task(channel, stream_ts, task_id, closing, "complete")
        await slack_client.stop_stream(channel, stream_ts)
    except Exception:
        logger.debug("Mirror task stream close failed before a move", exc_info=True)
    noted = messages_noted()  # before the request, as for the turn's first stream
    try:
        new_ts = await slack_client.start_stream(channel, thread_ts, user_id=owner_id) or ""
    except Exception:
        logger.debug("Mirror task stream start failed after a move", exc_info=True)
        new_ts = ""
    if new_ts and continues and task_id:
        try:
            await slack_client.append_task(channel, new_ts, task_id, title, "in_progress")
        except Exception:
            logger.debug("Mirror running step failed on a renewed stream", exc_info=True)
    return new_ts, (noted if new_ts else None)


def _arm_mirror_stream_renewal(turn: _ChatTurn) -> None:
    """Start the turn's renewal timer once its first live stream has opened."""
    if turn._mirror_stream_renewal is None and turn._mirror_stream_started_at:
        # Held on the turn until teardown stops it.
        turn._mirror_stream_renewal = asyncio.ensure_future(_renew_mirror_stream_when_due(turn))


async def _renew_mirror_stream_when_due(turn: _ChatTurn) -> None:
    """Renew the mirror's stream each time it reaches ``STREAM_RENEW_AFTER_S``.

    Wakes at the live stream's renewal age, which a tool call's move pushes back.
    Ends once the turn has no live stream (refused, or closed by teardown) or a
    renewal did not open one.
    """
    while turn._mirror_stream_started_at:
        due_at = turn._mirror_stream_started_at + STREAM_RENEW_AFTER_S
        await asyncio.sleep(max(0.0, due_at - stream_clock()))
        if _mirror_stream_due(turn) and not await asyncio.shield(_renew_mirror_stream(turn)):
            return


async def _renew_mirror_stream(turn: _ChatTurn) -> bool:
    """Move the mirror's step list to a fresh stream if it is still due.

    False when the timer should stop: Slack gave no new stream, or the turn may
    not publish to the thread now (the mirror is paused, the reply is withheld, a
    reconnect holds no client). Renewing re-posts the running step's title, so a
    turn that may not publish leaves the stream for Slack to end, as it did before
    renewal existed. Never raises.
    """
    state = turn.state
    async with turn._mirror_stream_lock:
        if not _mirror_stream_due(turn):
            # A tool call moved it meanwhile, or teardown closed it.
            return True
        chan = turn._mirror_chan
        slack_client = state.slack_client
        try:
            if (
                not chan
                or not slack_client
                or slack_mirror_is_paused(state, turn.session_key)
                or cross_surface_withheld(state, turn.slot)
                or slack_publication_withheld(state, turn.slot, chan, turn._mirror_thread)
            ):
                return False
            _mirror_stream_ts, noted = await _move_mirror_stream(
                slack_client,
                chan,
                turn._mirror_post_ts or "",
                state.owner_id or None,
                turn._mirror_stream_ts,
                (turn._mirror_active_task, turn._mirror_active_task_title),
                continues=True,
            )
            turn._mirror_stream_ts = _mirror_stream_ts
        except Exception:
            logger.debug("Mirror task stream renewal failed", exc_info=True)
            return False
        _note_mirror_stream_opened(turn, noted)
        if not _mirror_stream_ts:
            _mirror_active_task = ""
            turn._mirror_active_task = _mirror_active_task
        return bool(_mirror_stream_ts)


async def _stop_mirror_stream_renewal(turn: _ChatTurn) -> None:
    """Stop the renewal timer for good, after any renewal under way has finished.

    Teardown calls this before it closes the stream, then reads the stream and its
    running step afresh: a renewal may have replaced them.
    """
    renewal = turn._mirror_stream_renewal
    if renewal is None:
        # No stream ever opened, so nothing was armed and nothing is due.
        return
    turn._mirror_stream_renewal = None
    renewal.cancel()
    # ``wait`` never raises the timer's own outcome; only this task's own
    # cancellation can leave here.
    await asyncio.wait([renewal])
    async with turn._mirror_stream_lock:
        # No renewal can be due after this, so one shielded but not yet started
        # finds nothing to do.
        turn._mirror_stream_started_at = 0.0


def _cancel_mirror_stream_renewal(turn: _ChatTurn) -> None:
    """Cancel the renewal timer without waiting: teardown's backstop on every exit.

    A cancellation delivered to teardown before :func:`_stop_mirror_stream_renewal`
    runs skips it, and the timer, holding the turn, would renew a finished turn's
    stream every ``STREAM_RENEW_AFTER_S`` for good. Synchronous, so a further
    cancellation cannot skip it too.
    """
    renewal = turn._mirror_stream_renewal
    turn._mirror_stream_renewal = None
    turn._mirror_stream_started_at = 0.0
    if renewal is not None:
        renewal.cancel()
