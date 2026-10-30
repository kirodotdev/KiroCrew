"""Start a slot in a configured approval posture, without a person's press.

Two places give a slot nobody has touched yet the approval mode its owner chose
ahead of time: a crewmate's thread (the profile's ``approval_mode``) and a Slack
code channel's session (``slack.code_channel_defaults.approval_mode``). Both go
through :func:`seed_slot_approval`, which writes what the dashboard picker's
Reads / Trust write in ``apply_approval_mode`` -- ``_trust`` / ``_trust_reads`` on
every slot sharing the session, the session's approval policy -- and the same
``mode_change:<mode>`` SEL record, with three differences that make it a seed
rather than a person's choice:

* it never touches the process-wide safety override (YOLO): seeding a new
  session is not the documented action that ends an operator's grant;
* it never replaces a posture the slot already holds (a grant, a scope);
* it sweeps no pending approvals: a slot being seeded has none.

Trust is in-memory like every slot's: a restart returns the slot to asking, and
whoever seeded it decides whether to seed again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The modes a seed applies. ``normal`` (ask) is the starting posture already,
#: and ``yolo`` is process-global, never a property of one slot.
SEEDABLE_MODES = frozenset({"trust_reads", "trust"})


def _default_sel() -> Any:
    from kiro_crew.sel import sel

    return sel()


def seed_slot_approval(
    state: Any,
    slot: Any,
    mode: str,
    *,
    caller: str,
    sel: Callable[[], Any] = _default_sel,
) -> bool:
    """Put *slot* (and every slot sharing its session) on *mode*.

    Returns True when the posture was applied. A mode outside
    :data:`SEEDABLE_MODES`, or a slot that already holds any trust, is left
    exactly as it is. *caller* names the seeder in the SEL record; *sel* is the
    SEL accessor (a caller whose tests patch its own passes that).
    """
    if mode not in SEEDABLE_MODES:
        return False
    if slot._trust or slot._trust_reads or slot._trust_scope:
        return False
    from kiro_crew.dashboard.chat_utils import effective_session_key

    key = effective_session_key(slot)
    for sharing in list(state._slots.values()):
        if effective_session_key(sharing) == key:
            if mode == "trust":
                sharing._trust = True
            else:
                sharing._trust_reads = True
    # A no-op until the slot's ACP session exists; the runner assigns the policy
    # from ``_trust`` when it creates or resumes one.
    sessions = getattr(state, "sessions", None)
    if sessions is not None:
        sessions.set_approval_policy(key, "auto" if mode == "trust" else "")
    try:
        sel().log_api_access(
            caller=caller,
            operation=f"mode_change:{mode}",
            outcome="enabled",
            resources=slot.key,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.warning("SEL audit failed for approval seed by %s", caller, exc_info=True)
    return True
