"""Steps that build a dashboard turn's prompt around the user's message.

The turn keeps the order the steps run in, the ContextBuilder call and the
context-composition record; each step here reads what it is handed and returns
what it adds, so the order stays readable in ``_run_chat``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        _ChatSlot,
        logger,
    )


async def _session_replay_history(
    state: DashboardState,
    context_builder: ContextBuilder,
    slot: _ChatSlot,
    client: Any,
    session_key: str,
    *,
    _context_is_new: bool,
    _provider_has_history: bool,
    _current_replay_message: dict | None,
) -> str | None:
    """The provider-agnostic history replay a cold-start turn carries, or ``""``.

    Built off the loop from the conversation log plus the slot's live tail, unless
    the provider resumed its own native session or an explicit conversation reset
    asked for a cold start without history.
    """
    compressed: str | None = ""
    # Provider-agnostic session replay: Kiro Crew's conversation_log
    # is the canonical history source. Skip only when the provider
    # successfully resumed its own native session (same provider,
    # full-fidelity history already loaded via ACP session/load).
    if _context_is_new and not _provider_has_history:
        # Consumed HERE rather than before the branch, so only a real cold
        # start can spend the flag: a warm turn that never rebuilds history
        # must not burn the one chance the reset asked for.
        if state.sessions.consume_replay_suppression(session_key):
            # Suppression arm of the FRESH first-turn history debt: a reset asked
            # to forget, so there is no history to deliver and nothing to re-queue.
            # Pay the debt NOW, unconditionally — even if this turn then ends
            # pre-token, the re-queue must not replay the conversation the reset
            # dropped. (The build arm's debt is settled by the finally in
            # ``_run_chat``, gated on ``_first_turn_history_assembled`` and a
            # durably-kept turn, because its replay only reaches the provider when
            # the stream starts.)
            state.sessions.consume_first_turn_history_owed(session_key)
            logger.info(
                "Session replay suppressed by an explicit conversation reset: %s",
                session_key,
            )
            compressed = ""
        else:
            from kiro_crew.context import (  # circular: context -> chat
                build_session_replay,
                window_for_provider_client,
            )

            # Merge the disk transcript and a frozen live-window tail
            # before one budget pass. Exclude this request by identity,
            # whether or not the periodic flush has persisted it yet.
            compressed = (
                await asyncio.to_thread(
                    build_session_replay,
                    context_builder.conversation_log,
                    session_key,
                    pending_messages=list(slot.messages),
                    current_message=_current_replay_message,
                    model_window=window_for_provider_client(client),
                )
                or ""
            )
            logger.info(
                "Session replay: key=%s result=%s",
                session_key,
                f"{len(compressed)} chars" if compressed else "None (no history)",
            )
    return compressed


async def _scrub_dashboard_prefix(full_message: str, _trusted_prompt_tail: str | None) -> str:
    """*full_message* with every structural marker in its dashboard-added prefix neutralized.

    ContextBuilder's own prompt is the trusted tail; everything the dashboard
    prepended after it is scrubbed in one off-loop pass. A prompt whose tail is no
    longer ContextBuilder's is scrubbed whole, failing safe.
    """
    from kiro_crew.context import (  # circular: context -> dashboard.chat
        _neutralize_structural_markers,
    )

    if _trusted_prompt_tail is None:
        full_message = await asyncio.to_thread(
            _neutralize_structural_markers,
            full_message,
        )
    elif full_message.endswith(_trusted_prompt_tail):
        prefix_end = len(full_message) - len(_trusted_prompt_tail)
        if prefix_end:
            safe_prefix = await asyncio.to_thread(
                _neutralize_structural_markers,
                full_message[:prefix_end],
            )
            full_message = safe_prefix + _trusted_prompt_tail
    else:
        # Every post-builder mutation ``_run_chat`` makes before this scrub is
        # documented as a pure prepend. If that invariant changes, fail safe by removing all
        # structural authority rather than preserving an unknown copy.
        logger.warning(
            "prompt boundary: ContextBuilder prompt is no longer the "
            "final prompt tail; neutralizing every structural candidate"
        )
        full_message = await asyncio.to_thread(
            _neutralize_structural_markers,
            full_message,
        )
    return full_message


async def _checklist_resync(
    slot: _ChatSlot,
    full_message: str,
    *,
    _context_is_new: bool,
    _provider_has_history: bool,
    _todo_sync_rendered: tuple[tuple[str, str, bool], ...],
    _todo_recovery_carried: bool,
) -> tuple[str, tuple[tuple[str, str, bool], ...], bool]:
    """Prefix the checklist the agent must re-learn, and what the turn now owes.

    A cold start the provider did not resume gets the whole recovery block; a warm
    turn gets only the rows the person ticked since the agent's last snapshot.
    Returns the prompt and the turn's ``(sync_rendered, recovery_carried)`` debt,
    settled on the provider's first event.
    """
    # The cold-start `is_new` observation is a one-shot the session claim
    # consumes, so a turn that builds the recovery block but dies before
    # the provider's first event (a pre-dispatch Stop, an expired
    # non-persistent session) would never deliver it and later turns,
    # seeing is_new=False, would omit it — leaving the agent's empty list
    # diverged for good. `slot.todo_recovery_pending` keeps the trigger
    # armed across such an abort; it is cleared on the first provider
    # event of ``_run_chat``'s stream, once delivery is confirmed.
    if (_context_is_new and not _provider_has_history) or slot.todo_recovery_pending:
        # The rebuild's `create` echoes an all-open list; pin the rows
        # already done so that echo cannot erase them if the turn dies
        # before the agent's `complete` calls.
        slot.pin_completed_todo_rows()
        _todo_resync = slot.todo_recovery_prompt()
        if _todo_resync:
            # Owed until the provider accepts it (first event), not here:
            # the dispatch gates in ``_run_chat`` can still abort before the prompt is sent.
            slot.mark_todo_recovery_pending()
            _todo_recovery_carried = True
    else:
        _todo_resync = slot.todo_sync_prompt()
        # Marked stated on the provider's first event, not here: the
        # dispatch gates in ``_run_chat`` can still abort before the prompt is sent.
        _todo_sync_rendered = slot.todo_sync_rendered if _todo_resync else ()
    if _todo_resync:
        full_message = f"{_todo_resync}\n\n{full_message}"
    return full_message, _todo_sync_rendered, _todo_recovery_carried
