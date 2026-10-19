"""A dashboard turn's entry state: the Stop signal it reads live, the session's Stop count and
the directive provenance a channel steer can narrow."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_turn.recovery import _session_stop_generation_for


def _session_stop_generation(turn) -> int:
    """The session manager's Stop count for this turn's session key."""
    session_key = turn.session_key
    sessions = turn.sessions
    return _session_stop_generation_for(sessions, session_key)


def _directive_producer_is_channel(turn) -> bool:
    """The turn's directive provenance, NARROWED by any channel steer admitted into it.

    ``_directive_channel_origin`` is the opener's provenance and is fixed for
    the turn. A channel human's steer admitted mid-turn
    (``steer_into_running_turn(channel_origin=True)``) sets the slot's
    ``_turn_channel_narrowed`` at admission, before its RPC, and the flag holds
    for the remainder of the turn -- so a directive the model emits after
    channel text reached it is filed as channel-created, never with more
    authority than a channel-origin turn's directive carries. The one reader
    for both directive-application sites, so they cannot disagree.
    """
    _directive_channel_origin = turn._directive_channel_origin
    slot = turn.slot
    return _directive_channel_origin or getattr(slot, "_turn_channel_narrowed", False) is True


def _stop_pressed(turn) -> bool:
    """The user's Stop signal for this turn, read LIVE at the call site.

    True when a stop is in flight OR a monotonic stop generation moved since
    entry -- a Stop that pressed and already resolved back to idle is invisible
    to ``_stopping`` but not to the counters. Two counters are read: the slot's
    own (moved by the dashboard Stop handler) and the session manager's for
    this turn's session key (moved by ``stop_turn`` on any surface). A
    channel-born slot runs its turns on the channel's session, so a stop
    issued from that channel never touches the slot's state -- only the
    session-scoped count sees it. The two end-of-turn continuation gates
    (Stop-hook and refusal recovery) read THIS, never the backend's wire
    stop reason: a backend that aborts a policy-denied turn (codex answers its
    only reject option, ``cancel``, that way) reports ``cancelled`` with no
    Stop pressed, and the continuation is owed there. A function rather than a
    value so no gate can consume a snapshot taken before an await -- the Stop
    hook and the credential-hint lookup both suspend between the turn's end
    and the queue write.
    """
    _responsive_owner = turn._responsive_owner
    _responsive_turn = turn._responsive_turn
    _session_stop_gen_at_entry = turn._session_stop_gen_at_entry
    _stop_gen_at_entry = turn._stop_gen_at_entry
    slot = turn.slot
    return (
        bool(getattr(slot, "_stopping", False))
        or getattr(slot, "_stop_generation", _stop_gen_at_entry) != _stop_gen_at_entry
        or _session_stop_generation(turn) != _session_stop_gen_at_entry
        or (_responsive_turn.owner is _responsive_owner and _responsive_turn.interrupted)
    )
