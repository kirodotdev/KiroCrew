"""Which dashboard sessions may keep KAS "approve for me" on, and which calls it may answer.

With the option on, KAS sends each call it would approve itself as a permission
request carrying its verdict (``AcpEvent.kas_auto_approval``). That request takes
Crew's usual permission path, deny floor first; at the end of it, where Trust or
YOLO would approve, the call is approved without a card only while
:func:`owner_allows` holds for its session. The same answer, asked through
:func:`kiro_crew.acp.kas_approve_for_me.register_owner_check` when a session opens
and before each turn, decides whether the option stays on at all. It is yes only
while all of these hold:

* a person turned approve-for-me on for that session from this dashboard (the
  key is in :func:`approve_for_me_keys`; a gateway restart forgets it, so a
  resumed session starts off again);
* the ``approval_modes`` policy permits it (:func:`approve_for_me_permitted`);
* every slot on that session is one a person drives from the dashboard
  (:func:`kiro_crew.dashboard.deny_override.slot_is_owners`: not app-owned,
  unattended, cron, channel-born, channel-linked or mirrored to Slack), and a
  running turn on it is one the owner sent with no channel text in it
  (:func:`kiro_crew.dashboard.deny_override.turn_is_owners`): the same lines the
  deny-floor override draws;
* no governance profile could bind the session's calls (its surface's, or the
  task bind of a slot's agent: :func:`profile_binds`);
* no slot on that session holds Trust, Reads or a reaching YOLO grant, under
  which Crew answers the calls itself;
* no Dashboard card's turn runs on it: a card turn keeps its human-approval
  floor, so its calls take Crew's permission path.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

from kiro_crew.agent_sdk import kas_approve_for_me
from kiro_crew.approval_mode_policy import APPROVE_FOR_ME_MODE, session_mode_permitted
from kiro_crew.dashboard import card_send
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.deny_override import slot_is_owners, turn_is_owners

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

_KEYS_ATTR = "_approve_for_me_keys"


def approve_for_me_keys(state: DashboardState) -> set[str]:
    """The session keys a person turned approve-for-me on for, on *state*."""
    keys = getattr(state, _KEYS_ATTR, None)
    if not isinstance(keys, set):
        keys = set()
        setattr(state, _KEYS_ATTR, keys)
    return keys


def approve_for_me_permitted() -> bool:
    """Whether the ``approval_modes`` policy lets a session hold approve-for-me."""
    return session_mode_permitted(APPROVE_FOR_ME_MODE)


def offered_on(slot: object) -> bool:
    """Whether approve-for-me may be offered on *slot* at all: a slot the owner drives."""
    return slot_is_owners(slot) and not getattr(slot, "_slack_channel", "")


def profile_binds(session_key: str, agent: str = "") -> bool:
    """Whether a governance profile could judge *session_key*'s calls (fail closed).

    The surface's bind, and the task bind of *agent* (the slot's agent) when given.
    A profile bound there may narrow or deny the mode, so approve-for-me stays off.
    """
    try:
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        return resolve_active_scope(session_key, agent=agent) is not None
    except Exception:
        logger.warning("could not resolve the session's governance profile", exc_info=True)
        return True


def owner_allows(state: DashboardState, session_key: str) -> bool:
    """Whether *session_key*'s approve-for-me may stay on. Memory reads only."""
    if not approve_for_me_permitted():
        return False
    sessions = state.sessions
    fold = sessions._fold_key if sessions is not None else (lambda key: key)
    target = fold(session_key)
    keys = approve_for_me_keys(state)
    if session_key not in keys and target not in keys:
        return False
    if card_send.card_turn_running(session_key) or card_send.card_turn_running(target):
        return False
    if profile_binds(session_key):
        return False
    yolo = state.is_yolo_active()
    matched = False
    for slot in list(state._slots.values()):
        if fold(effective_session_key(slot)) != target:
            continue
        matched = True
        if not offered_on(slot):
            return False
        agent = getattr(slot, "agent", "")
        if isinstance(agent, str) and agent and profile_binds(session_key, agent):
            return False
        if getattr(slot, "running", False) and not turn_is_owners(slot):
            return False
        if slot._trust or slot._trust_reads or slot._trust_scope:
            return False
        if yolo and state.yolo_reaches(slot):
            return False
    return matched


def reads_on(state: DashboardState, session_key: str) -> bool:
    """What the picker shows: on only on an engine that checks auto-approvals with
    Crew and while the owner allows it; never on otherwise."""
    return kas_approve_for_me.engine_supported() and owner_allows(state, session_key)


def install(state: DashboardState) -> None:
    """Answer KAS's owner check from *state* for this gateway's life."""
    kas_approve_for_me.register_owner_check(functools.partial(owner_allows, state))
    # Whether the engine sends its verdict to Crew, read off the loop.
    kas_approve_for_me.refresh_engine_support_soon()
