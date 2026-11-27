"""``!trust``: put one Slack thread's session on Trust from the thread itself.

The approval card's "Trust session" button grants the same thing, but only once a
tool has already stopped to ask, and only in a DM. ``!trust`` lets the owner grant
it up front, in any thread run from Slack. The dispatcher in
:mod:`kiro_crew.slack.handler_runtime.commands` keeps every ``!`` command but a
few owner-only, so nobody else reaches this module.

The grant is the in-memory half of the shared ``messaging.session_trust`` one, which
the Slack turn drivers read. The session's approval policy is left alone on purpose:
a spawned sub-agent inherits that policy whoever sent the turn, so writing it would
run a shared user's sub-agents without asking. Sub-agents of a trusted thread ask.
It is per session and lasts until ``!trust off``, a restart, or a
``permission_mode`` deny of ``trust``. It does not weaken the PreToolUse gate:
the deny-list, sensitive paths and the governance ceiling still refuse a tool.
A shared user's turn in a trusted thread still asks (see the turn drivers).

Two kinds of thread are pointed elsewhere instead of written around:

* a Slack Code channel or Conductor thread has its own owner gates and its own
  command (``/kirocrew approval trust``);
* a thread linked to a dashboard chat or a cron runs its turns on that session,
  whose Trust has no per-sender rung: a shared user's message there would run
  without asking, so the owner sets it in that session's own picker. ``!trust``
  grants nothing in a channel that a Channel Bot or Channel Conductor answers,
  for the same reason: that chat runs every thread there.

A channel-born dashboard tab runs on the thread's own session yet is not a linked
thread here, and the card's Trust button also raises that tab's Trust. ``!trust
off`` leaves that tab's Trust alone and reports that its tools and helper agents
may still run without asking; ``!trust`` never raises it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_OPERATION = "slack.thread_trust"

ON_TEXT = (
    "🔓 This thread is on *Trust*: its tools run without asking.\n"
    "Blocked tools stay blocked. Type `!trust off` to stop."
)
ALREADY_ON_TEXT = "ℹ This thread is already on *Trust*. Type `!trust off` to stop."
OFF_TEXT = "🔒 Trust is off for this thread. Tools ask again."
ALREADY_OFF_TEXT = "ℹ This thread is not on Trust. Type `!trust` to turn it on."
STATUS_ON_TEXT = "This thread is on *Trust*. Type `!trust off` to stop."
STATUS_OFF_TEXT = "This thread is not on Trust. Type `!trust` to turn it on."
POLICY_TEXT = "🔒 Trust is turned off by your organization's policy."
REVOKE_PARTIAL_TEXT = (
    "⚠ Trust is off for this thread's own tools, but its helper agents may still run "
    "without asking. Type `!stop` to end the running work."
)
LINKED_TEXT = "This thread runs another session. Set Trust in that session's picker instead."
TAB_STILL_TRUSTED_TEXT = (
    "🔒 Trust is off for messages sent in this thread, but its dashboard tab is still on "
    "*Trust*, so its tools and helper agents may run without asking. "
    "Set that tab to Normal in its approval picker."
)
STATUS_TAB_TRUSTED_TEXT = (
    "This thread has no Trust grant, but its dashboard tab is on *Trust*. "
    "Set that tab to Normal in its approval picker."
)
CODE_CHANNEL_TEXT = (
    "This thread has its own approval setting. Use `/kirocrew approval trust` instead."
)
USAGE_TEXT = "Usage: `!trust` (on), `!trust off`, or `!trust status`."

_ON_WORDS = frozenset({"", "on"})
_OFF_WORDS = frozenset({"off"})
_STATUS_WORDS = frozenset({"status"})


def _audit(user: str, outcome: str, resources: str, error: str = "") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=user or "unknown",
            operation=AUDIT_OPERATION,
            outcome=outcome,
            source="slack",
            resources=resources,
            error=error,
        )
    except Exception:
        logger.warning("SEL audit failed for !trust", exc_info=True)


def _has_own_approval_setting(channel: str, reply_ts: str) -> bool:
    """A bound Code channel, or a Conductor thread: both own their approval mode.

    A Conductor thread whose row could not be read back counts too: it is fenced,
    and its session must not be raised from here.
    """
    from kiro_crew.slack.code_channels.store import store

    st = store()
    return (
        st.get_binding(channel) is not None
        or st.get_thread_binding(channel, reply_ts) is not None
        or st.is_thread_quarantined(channel, reply_ts)
    )


def _is_linked(state: Any, session_key: str, reply_ts: str) -> bool:
    """Whether the thread's turns run on a dashboard chat, open or closed."""
    from kiro_crew.slack.thread_parent import is_slack_born

    if not is_slack_born(session_key):
        return True
    return state is not None and state.get_linked_slot(reply_ts) is not None


def is_trusted(session_key: str) -> bool:
    """Whether the thread's session holds the Trust grant."""
    from kiro_crew.messaging.session_trust import is_session_trusted

    return is_session_trusted(session_key)


def _tab_holds_trust(state: Any, session_key: str) -> bool:
    """Whether a live tab holds Trust, even while policy masks it."""
    slots = getattr(state, "_slots", None) if state is not None else None
    if not slots:
        return False
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    return any(
        getattr(slot, "_trust_held", False) is True and effective_session_key(slot) == session_key
        for slot in list(slots.values())
    )


async def grant(session_key: str, user: str) -> str:
    """Put *session_key* on Trust; the reply to post."""
    from kiro_crew.messaging.session_trust import add_trusted_session, trust_permitted
    from kiro_crew.safety_override import approval_mode_permitted

    resources = f"session={session_key} mode=trust"
    if not trust_permitted() or not await asyncio.to_thread(approval_mode_permitted, "trust"):
        _audit(user, "denied", resources, "mode_disabled_by_policy")
        return POLICY_TEXT
    if is_trusted(session_key):
        _audit(user, "allowed", f"{resources} result=unchanged")
        return ALREADY_ON_TEXT
    try:
        # No session manager: the in-memory half only (see the module docstring).
        add_trusted_session(session_key, strict=True)
    except PermissionError:
        _audit(user, "denied", resources, "mode_disabled_by_policy")
        return POLICY_TEXT
    _audit(user, "allowed", f"{resources} result=set")
    return ON_TEXT


def revoke(state: Any, session_key: str, user: str) -> str:
    """Remove the thread grant without changing its dashboard tab's Trust."""
    from kiro_crew.messaging.session_trust import _trusted_sessions, remove_trusted_session

    resources = f"session={session_key} mode=normal"
    # A policy refresh may mask a grant without removing it.
    if session_key not in _trusted_sessions:
        tab_trust = _tab_holds_trust(state, session_key)
        _audit(
            user,
            "allowed",
            f"{resources} result=unchanged" + (" tab_trust=held" if tab_trust else ""),
        )
        return TAB_STILL_TRUSTED_TEXT if tab_trust else ALREADY_OFF_TEXT
    try:
        # Resets the policy too when the grant came from the card's Trust button.
        remove_trusted_session(session_key)
    except Exception:
        # The in-memory half is gone; the policy a sub-agent reads may not be.
        logger.warning("!trust: approval policy reset failed", exc_info=True)
        _audit(user, "error", resources, "policy_reset_failed")
        return REVOKE_PARTIAL_TEXT
    tab_trust = _tab_holds_trust(state, session_key)
    _audit(user, "allowed", f"{resources} result=set" + (" tab_trust=held" if tab_trust else ""))
    return TAB_STILL_TRUSTED_TEXT if tab_trust else OFF_TEXT


async def handle(
    arg: str,
    *,
    state: Any,
    session_key: str,
    channel: str,
    reply_ts: str,
    user: str,
) -> str:
    """``!trust [on|off|status]`` from the owner; the reply to post."""
    word = arg.strip().lower()
    if word not in _ON_WORDS | _OFF_WORDS | _STATUS_WORDS:
        return USAGE_TEXT
    if await asyncio.to_thread(_has_own_approval_setting, channel, reply_ts):
        _audit(user, "denied", f"session={session_key}", "code_channel_owns_mode")
        return CODE_CHANNEL_TEXT
    from kiro_crew.slack import channel_handler

    # A Channel Bot's or Channel Conductor's chat runs this thread's next message, so
    # a grant on the thread's own session would report Trust on while every tool still
    # asks. ``off`` and ``status`` still reach a grant made before the channel had one.
    handled = word in _ON_WORDS and bool(channel_handler.handler_refusal(channel))
    if handled or _is_linked(state, session_key, reply_ts):
        _audit(user, "denied", f"session={session_key}", "linked_dashboard_chat")
        return LINKED_TEXT
    if word in _STATUS_WORDS:
        from kiro_crew.messaging.session_trust import _trusted_sessions

        if session_key in _trusted_sessions:
            return STATUS_ON_TEXT
        return STATUS_TAB_TRUSTED_TEXT if _tab_holds_trust(state, session_key) else STATUS_OFF_TEXT
    if word in _OFF_WORDS:
        return revoke(state, session_key, user)
    return await grant(session_key, user)
