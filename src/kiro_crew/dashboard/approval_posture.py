"""The decisions on whether a slot's trust passes to what it starts, and the one
turn that takes none of it."""

from __future__ import annotations

import threading
from typing import Any

#: The ``_trust_scope`` prefix of a hub conductor's Trust carried to its bound worker
#: on this crew (rfc-conductor-remote-dispatch §4). It stands in for Trust, so the
#: ceiling's ``trust`` verdict masks it wherever it is honoured.
REMOTE_TRUST_SCOPE_PREFIX = "remote-dispatch:"


def remote_scope_denied(scope: str) -> bool:
    """Whether *scope* is a remote Trust grant the ceiling's ``trust`` verdict denies.

    The one mask every reader of such a grant applies: the approval path, the
    slots poll's Trust label and the hub's ``trust_active``. A memory read.
    """
    if not scope.startswith(REMOTE_TRUST_SCOPE_PREFIX):
        return False
    from kiro_crew.approval_mode_policy import session_mode_permitted

    return not session_mode_permitted("trust")


_lock = threading.Lock()
#: Session key -> the slot whose turn is running on it, and how many of its turns
#: are (a ``/prompts get`` turn nests). In-process; read by the sub-agent
#: spawn and policy paths, which hold a session key and no slot.
_running_bot_turns: dict[str, tuple[Any, int]] = {}


def teammate_bot_turn(slot: Any) -> bool:
    """Whether the turn on *slot* is a team Crew teammate's on a Bot's thread.

    Such a turn takes none of the owner's approvals: slot trust, trust-reads, a
    scoped grant, trusted patterns, YOLO and session deny overrides are the owner's,
    so every call asks. ``_turn_teammate_sent`` is set by the turn's open from who
    started it (``attribution.teammate_started``) and by a teammate's steer; read per
    request, so a grant the owner adds mid-turn is withheld as well.
    """
    return getattr(slot, "mode", "") == "member" and (
        getattr(slot, "_turn_teammate_sent", False) is True
    )


def trust_reaches_subagents(slot: Any) -> bool:
    """Whether *slot*'s ``_trust`` covers the subagents it spawns.

    Any ``_trust`` does, a Crew Mode card trust ("Trust this crew member",
    ``_trust_card_scoped``) included: trusting a worker trusts the subagents it
    spawns, so its ``spawn_run`` and their tools are not asked on the conductor's
    card. Every writer of a slot's stored ``approval_policy`` (what the spawn gate
    and each subagent read) goes through this; the Slack/background parent-trust
    gate counts the card trust through ``_slot_is_trusted`` like any slot trust. A
    revoke clears ``_trust`` and writes ``""``, so a child spawned after it asks
    again, and one already running loses its ``parent_policy=auto`` grant on its next
    tool request (``subagent_manager.turn_session.parent_trust_revoked``).
    """
    return bool(getattr(slot, "_trust", False)) and not teammate_bot_turn(slot)


def trust_carries(slot: Any) -> bool:
    """Whether *slot*'s ``_trust`` passes to the new sessions, forks and workflows it starts.

    It does for a trust a person set on the slot itself. It does not for a Crew Mode
    card trust (``_trust_card_scoped``): that covers the crew member and the subagents
    it spawns (:func:`trust_reaches_subagents`), not other sessions it opens, which
    are born asking on the conductor's card. Every creator posture goes through this.
    """
    return trust_reaches_subagents(slot) and (
        getattr(slot, "_trust_card_scoped", False) is not True
    )


class bot_turn:
    """Register *slot*'s turn on *session_key* for :func:`teammate_turn_running`."""

    def __init__(self, session_key: str, slot: Any) -> None:
        self._key = session_key
        self._slot = slot

    def __enter__(self) -> None:
        with _lock:
            _, count = _running_bot_turns.get(self._key, (self._slot, 0))
            _running_bot_turns[self._key] = (self._slot, count + 1)

    def __exit__(self, *_exc: object) -> None:
        with _lock:
            slot, count = _running_bot_turns.get(self._key, (self._slot, 0))
            if count > 1:
                _running_bot_turns[self._key] = (slot, count - 1)
                return
            _running_bot_turns.pop(self._key, None)
        # The outermost turn has ended: the teammate mark described it, and an idle
        # thread runs no one's turn, so the owner's grants apply to it again (a Trust
        # pressed now reaches the thread's sub-agents). Every turn's open sets it anew.
        if getattr(self._slot, "_turn_teammate_sent", False) is True:
            self._slot._turn_teammate_sent = False


def teammate_turn_running(session_key: str) -> bool:
    """Whether a teammate's turn on a Bot's thread is running on *session_key* now.

    Read live off the slot, so a teammate's steer into the turn counts too.
    ``card_send.card_turn_running`` includes it, so every card-turn refusal (the
    spawn gate, the delegation routes, the cron tools, the lost-wave report, the
    arrival mark) holds such a turn as well. The self-write hold reads the teammate
    turn alone (``card_send.teammate_turn_seen``).
    """
    with _lock:
        entry = _running_bot_turns.get(session_key)
    return entry is not None and teammate_bot_turn(entry[0])
