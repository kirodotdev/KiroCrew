"""Which session-control calls may reach a session an app owns.

:func:`authorize_target` refuses every session an app owns (``slot._app``) with
``app_scoped_target``. The refusal is lifted for one call only when all three hold:

1. The app allows the verb the call needs, and the owner approved it
   (``apps/user_session_reach.py``, resolved off the loop by
   :func:`prewarm_app_reach` before each gated call).
2. The operation is one that verb covers (:data:`APP_REACH_OPERATIONS`).
3. The caller is one of the owner's own chats (:func:`app_reach_caller_refusal`).

Every other refusal still runs. The caller-side ``app_scoped_caller`` rule keeps an
app's session from reaching anything, the target's channel link and mirror still
refuse, and the ownership fence still bounds an agent-created or member caller to
the sessions it created, which an app's session never is.

Stop and close are not covered. A stop ends a turn the app started and may still be
waiting on, and a close archives a session the app still holds by key. Either one
changes the app's work rather than adding to it, and neither is needed to read a
session or send it a follow-up. Revive stays refused for the second reason
(``session_revive``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from kiro_crew.apps.user_session_reach import (
    app_allows_user_session_reach,
    prime_user_session_reach,
)
from kiro_crew.dashboard.session_control_parts.caller_access import (
    OWNER_CHAT_SLOT_PREFIX,
    _member_caller,
)
from kiro_crew.dashboard.session_control_parts.delivery_containment import _has_channel_mirror
from kiro_crew.dashboard.state import SlotOrigin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

#: ``authorize_target`` operation -> the ``reachableByUserSessions`` verb it needs.
APP_REACH_OPERATIONS: Mapping[str, str] = {
    "read": "read",
    "summary": "read",
    "send": "send",
}

#: The origins a person's own tab carries: ``USER``, or none on a slot restored from
#: a record written before origins were stored (``restore_drivers._PERSON_ORIGINS``).
_PERSON_ORIGINS = frozenset({"", SlotOrigin.USER})


async def prewarm_app_reach(state: "DashboardState") -> None:
    """Resolve, off the event loop, what the app of each live app session allows.

    :func:`app_admits_reach` runs inside the synchronous ``authorize_target`` and reads
    only these verdicts, so every read, summary and send path awaits this before its
    gate. Await it BEFORE ``prewarm_enabled_check``, which must stay the last
    suspension before the gate. A path that skips it fails closed.
    """
    slots = getattr(state, "_slots", None) or {}
    apps = {str(getattr(slot, "_app", "") or "") for slot in list(slots.values())}
    apps.discard("")
    if apps:
        await prime_user_session_reach(apps)


def app_admits_reach(slot: "_ChatSlot", operation: str) -> bool:
    """Whether the app that owns *slot* lets the owner's chats run *operation* on it.

    False for a slot no app owns, for an operation no verb covers, and for a verb
    the app does not allow now. It reads the verdict :func:`prewarm_app_reach`
    recorded and no file.
    """
    app = str(getattr(slot, "_app", "") or "")
    verb = APP_REACH_OPERATIONS.get(operation, "")
    if not app or not verb:
        return False
    return app_allows_user_session_reach(app, verb)


def app_reach_caller_refusal(state: "DashboardState", caller_key: str, caller: Any) -> str:
    """Why *caller* is not one of the owner's own chats, or ``""`` when it is.

    Positive identity, read from the caller's own slot: a dashboard chat tab
    (``chat-``) a person opened, so no ``_created_by``, no app, persistent memory,
    run on this host, with no channel or cron link, not channel-born, no outbound
    mirror, and not a crew member's session. The owner-DM exemption the caller-side
    channel refusals grant does not apply: an app's transcript read into a channel
    conversation would reach that channel's readers, and the app's opt-in names only
    the owner's chats. An unreadable mirror link counts as a mirror.
    """
    if not caller_key.startswith(OWNER_CHAT_SLOT_PREFIX):
        return "the caller is not a dashboard chat"
    if str(getattr(caller, "_origin", "") or "") not in _PERSON_ORIGINS:
        return "the caller was not opened by a person"
    if getattr(caller, "_created_by", ""):
        return "the caller was created by another session"
    if str(getattr(caller, "_app", "") or ""):
        return "the caller is app-scoped"
    if getattr(caller, "memory_mode", "persistent") != "persistent":
        return "the caller is incognito or temporary"
    if getattr(caller, "executor", "") == "remote":
        return "the caller runs on another host"
    if getattr(caller, "linked_session_key", "") or getattr(caller, "channel_origin", False):
        return "the caller is linked to a channel or a scheduled job"
    if _has_channel_mirror(state, caller):
        return "the caller is mirrored to a channel"
    if _member_caller(state, caller_key):
        return "the caller is a Bot's session"
    return ""


__all__ = [
    "APP_REACH_OPERATIONS",
    "app_admits_reach",
    "app_reach_caller_refusal",
    "prewarm_app_reach",
]
