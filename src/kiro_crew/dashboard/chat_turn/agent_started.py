"""A turn the agent starts on its own between a chat's prompts: its watch and its start.

On a backend in ``ACP_BACKENDS_AGENT_STARTED_TURNS`` (claude) the agent can start a
turn after the dashboard's turn has ended, for example when a ``run_in_background``
command it launched exits and Claude Code answers the task notification, or a
background agent asks for a permission. The client's idle reader sees that turn and
calls back. This module registers that callback for a chat slot
(:func:`_watch_agent_turns`) and, when it fires, claims the slot the way the queue
drain does and starts a ``_run_chat(agent_started=True)`` turn
(:func:`_on_agent_turn`). That turn sends nothing and reads the session's pending
turn through the same event loop a user turn uses, so its tool rows, permission
cards, hooks, SEL audit, redaction and persistence are the user turn's own.

Design record: ``docs/request-for-change/rfc-agent-started-turns.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk import watch_agent_turns

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_runner import (
        EVENT_ELICITATION_REQUEST,
        EVENT_PERMISSION_REQUEST,
        DashboardState,
        _ChatSlot,
        _run_chat,
        effective_session_key,
        logger,
        spawn_guarded_turn,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

#: The crew-log actor of a turn the agent started (``crew_log.emit_turn.ACTORS``).
AGENT_STARTED_ACTOR = "agent"
#: The assistant row meta key that says who started the turn the row answers.
AGENT_ORIGIN_META_KEY = "origin"


def agent_origin_meta() -> dict[str, str]:
    """``meta.origin`` of an assistant row a turn the agent started wrote.

    A fixed value: nothing the adapter sent reaches a stored field through it.
    """
    return {"kind": AGENT_STARTED_ACTOR}


def is_agent_started_row(row: object) -> bool:
    """Whether *row* is a reply a turn the agent started wrote (:func:`agent_origin_meta`)."""
    meta = row.get("meta") if isinstance(row, dict) else None
    origin = meta.get(AGENT_ORIGIN_META_KEY) if isinstance(meta, dict) else None
    return isinstance(origin, dict) and origin.get("kind") == AGENT_STARTED_ACTOR


def _agent_turn_still_pending(client: Any) -> bool:
    """Whether *client*'s session still holds a turn the agent started, unread."""
    probe = getattr(client, "agent_turn_pending", None)
    if not callable(probe):
        return False
    try:
        return probe() is True
    except Exception:
        logger.debug("agent_turn_pending probe failed", exc_info=True)
        return False


async def _close_agent_turn_stream(turn: _ChatTurn) -> None:
    """Close the agent-started turn's event stream, whatever ended the turn.

    The client holds the next prompt back while that stream is open, so a turn
    that leaves its loop early (a Stop, a raise, a ``break``) must not leave it
    for the garbage collector to close.
    """
    stream = turn._agent_turn_stream
    if stream is None:
        return
    turn._agent_turn_stream = None
    try:
        await stream.aclose()
    except Exception:
        logger.debug("closing the agent-started turn stream failed", exc_info=True)


async def _send_owed_agent_cancel(turn: _ChatTurn, client: Any, event: Any) -> None:
    """Send the Stop pressed while the agent-started turn was being prepared.

    That Stop found no turn to cancel (the agent's cycle had no open stream), so
    it is sent once the stream yields its first event, *event*, when the client
    holds the turn. The cycle then ends on its own end condition within the
    cancel grace window, as a Stop during the turn does (RFC section 7).
    """
    turn._agent_cancel_owed = False
    turn._agent_cancel_event = event
    try:
        await client.cancel()
    except Exception:
        logger.debug("cancelling the agent-started turn failed", exc_info=True)


def _ask_the_owed_cancel_answered(turn: _ChatTurn, event: Any) -> bool:
    """Whether *event* is the permission or MCP elicitation ask the owed cancel answered.

    The cancel is sent at the stream's first event, and ``cancel_session`` answers
    every ask the client holds open as cancelled, which is that event's ask when it
    is one: a card for it would wait on a person for an ask already closed and hold
    the turn until its window ran out. The stream builds the next event, and
    registers its ask, only once this turn reads on, so a later ask is still open
    and takes the normal card path, where a person or the approval window answers
    it and the answer is audited.
    """
    return event is turn._agent_cancel_event and getattr(event, "kind", None) in (
        EVENT_PERMISSION_REQUEST,
        EVENT_ELICITATION_REQUEST,
    )


def _watch_agent_turns(
    state: DashboardState,
    slot: _ChatSlot,
    client: Any,
    loaded_cfg: KiroCrewConfig | None,
) -> None:
    """Have *slot* run the turns *client*'s agent starts between its prompts.

    Called by every turn once it holds its session, so ``agent.agent_started_turns``
    applies from the next turn: off, or a config that did not load, registers
    nothing and the client's idle reader never starts, which is main's behaviour.
    A session whose backend starts no such turn registers nothing either
    (``agent_sdk.watch_agent_turns`` answers False), and nor does a session
    another provider serves.
    """
    try:
        enabled = loaded_cfg is not None and loaded_cfg.agent.agent_started_turns is True
        if enabled:
            watch_agent_turns(client, lambda: _on_agent_turn(state, slot, client))
        else:
            watch_agent_turns(client, None)
    except Exception:
        logger.debug("registering the agent-started turn watch failed", exc_info=True)


def _on_agent_turn(state: DashboardState, slot: _ChatSlot, provider: Any) -> None:
    """Start the turn *provider*'s agent began on its own, on *slot*.

    Runs synchronously on the event loop, so the slot is claimed (``slot.task``)
    before anything else can start a turn on it. Nothing starts when the slot was
    closed or replaced, when its session now runs another process, or when a
    prompt already read the turn as its own. While another turn holds the slot
    (the previous turn still ending, or a user turn waiting for its session), the
    start is retried when that turn's task ends: a user turn that read the frames
    by then leaves nothing pending, and the retry starts nothing.
    """
    try:
        if state._slots.get(slot.key) is not slot:
            return
        if state.sessions.get_provider(effective_session_key(slot)) is not provider:
            return
        if not _agent_turn_still_pending(provider):
            return
        task = slot.task
        if task is not None and not task.done():
            task.add_done_callback(lambda _task: _on_agent_turn(state, slot, provider))
            return
        if slot.running:
            # Admitted but not yet started: that turn reads the frames as its own.
            return
        # Imported here, as the queue drain does.
        from kiro_crew.shared_crew.turn_gate import independent_turn

        # Called from the client's idle reader, whose context is the finished
        # turn's: without ``independent_turn`` this turn would run as that
        # turn's already-settled admitted work instead of being admitted itself.
        # Protected, so a message sent meanwhile queues behind it rather than
        # cancelling it (the client refuses a steer into this turn).
        with independent_turn():
            task = spawn_guarded_turn(
                state,
                slot,
                _run_chat(
                    state,
                    slot,
                    "",
                    _agent_started=True,
                    _turn_actor=AGENT_STARTED_ACTOR,
                    _synthetic_payload=True,
                    _responsive_protected=True,
                ),
            )
        slot.task = task
        state.push_slots_update()
    except Exception:
        logger.warning("Could not start the agent-started turn on slot %s", slot.key, exc_info=True)
