"""``!trust``: put one Slack thread's session on Trust from the thread itself.

The approval card's "Trust session" button grants the same thing, but only once a
tool has already stopped to ask, and only in a DM. ``!trust`` lets the owner grant
it up front, in any thread run from Slack. The dispatcher in
:mod:`kiro_crew.slack.handler_runtime.commands` admits the owner and a confirmed
shared user (``slack.shared_access``) here, and nobody else.

The owner's grant is the in-memory half of the shared ``messaging.session_trust``
one, which the Slack turn drivers read. The session's approval policy is left alone
on purpose: a spawned sub-agent inherits that policy whoever sent the turn, so
writing it would run a shared user's sub-agents without asking. Sub-agents of a
trusted thread ask. It is per session and lasts until ``!trust off``, a restart, or
a ``permission_mode`` deny of ``trust``. It does not weaken the PreToolUse gate:
the deny-list, sensitive paths and the governance ceiling still refuse a tool.
A shared user's turn in a trusted thread still asks (see the turn drivers).

A shared user's ``!trust`` is a narrower grant, for that sender on that session
(``session_trust.add_trusted_sender``): only their own turns stop asking. It covers
neither the owner's turns nor another shared user's, writes no approval policy, and
never answers a helper agent's call, so sub-agents ask. It is bound to the session
as it runs when the grant is made (:func:`session_binding`: that session's
provider and its recorded agent), so it needs a running session
and ends when that session is replaced, whatever replaced it: ``!ta``, ``!bot``,
``/agent``, a switch on the dashboard, a reset, an idle close, a restart. A
compaction, a cleared conversation and a fresh conversation on the live process
keep that session, its provider and its agent, so the grant holds across them, as
the owner's does: none changes who sends or which agent runs.
It also ends with their own ``!trust off``, with
the owner's ``!trust off`` in the thread, when the thread joins a Bot's chat, when
the harness switches the running session's agent in place, and when they leave
``slack.shared_access`` (:func:`drop_unlisted_senders`, :func:`sender_trusted`). Their ``off`` and ``status`` reach a grant of theirs in
any thread, the refused kinds below included.

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
import weakref
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
LINKED_TEXT = "This thread runs another session. The owner sets Trust in that session's picker."
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
    "This thread has its own approval setting. The owner sets it with `/kirocrew approval trust`."
)
USAGE_TEXT = "Usage: `!trust` (on), `!trust off`, or `!trust status`."
NOT_ALLOWED_TEXT = "⛔ Not authorized."
NO_SESSION_TEXT = "Nothing is running here yet. Send a message first, then type `!trust`."
OWN_ON_TEXT = (
    "🔓 Your messages here are on *Trust*: their tools run without asking.\n"
    "Helper agents still ask. Blocked tools stay blocked. Type `!trust off` to stop."
)
OWN_ALREADY_ON_TEXT = "ℹ Your messages here are already on *Trust*. Type `!trust off` to stop."
OWN_OFF_TEXT = "🔒 Trust is off for your messages here. Tools ask again."
OWN_ALREADY_OFF_TEXT = "ℹ Your messages here are not on Trust. Type `!trust` to turn it on."
OWN_STATUS_ON_TEXT = "Your messages here are on *Trust*. Type `!trust off` to stop."
OWN_STATUS_OFF_TEXT = "Your messages here are not on Trust. Type `!trust` to turn it on."
SENDERS_TEXT = (
    "\nShared users with Trust for their own messages here: {count}. "
    "`!trust off` ends theirs too."
)

_ON_WORDS = frozenset({"", "on"})
_OFF_WORDS = frozenset({"off"})
_STATUS_WORDS = frozenset({"status"})
#: The stream event an agent harness sends when it switched the session's agent in
#: place (the ACP layer's ``EVENT_AGENT_SWITCHED``; a test pins the two together).
_AGENT_SWITCHED_EVENT = "agent_switched"


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


def _senders(session_key: str = "") -> str:
    """The senders holding a grant on *session_key* (on any session when empty), for
    an audit row: a drop names who lost a grant, not only how many."""
    from kiro_crew.messaging.session_trust import _trusted_senders

    # Copies: a policy deny clears the map from another thread while this reads.
    rows = (
        [_trusted_senders.get(session_key, {})] if session_key else list(_trusted_senders.values())
    )
    return ",".join(sorted({sender for row in rows for sender in list(row)}))


class SessionBinding:
    """The running session a sender's grant is made on: its provider and its agent.

    Two bindings are equal only while both name the same live provider object and
    the same recorded agent. The provider is held weakly, so a session that has been
    replaced equals nothing, a later one included, and a grant keeps no dead
    provider alive.
    """

    __slots__ = ("_provider", "agent")
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, provider: Any, agent: str) -> None:
        self._provider = weakref.ref(provider)
        self.agent = agent

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SessionBinding):
            return NotImplemented
        mine = self._provider()
        return mine is not None and mine is other._provider() and self.agent == other.agent

    def __repr__(self) -> str:
        return f"{self.agent}@{id(self._provider()):x}"


def session_binding(sessions: Any, session_key: str) -> SessionBinding | None:
    """What a sender's grant on *session_key* is bound to: the session as it runs now.

    The provider object the session registry holds for the key, and that session's
    recorded agent. A session keeps its provider for as long as it runs and gets a
    new one whenever it is replaced, whatever replaced it, so a grant made under
    this value covers calls on that one running session on that one agent, and no
    write to a thread's, a channel's or the default agent can bring it back.
    ``None`` when no session is running or the registry cannot be read: nothing to
    bind to.
    """
    try:
        if sessions is None or sessions.has_session(session_key) is not True:
            return None
        provider, agent = sessions.get_provider(session_key), sessions.get_agent(session_key)
        if provider is None or not isinstance(agent, str):
            return None
        return SessionBinding(provider, agent)
    except Exception:
        logger.warning("!trust: the session registry could not be read; asking", exc_info=True)
        return None


def holds_grant(session_key: str, user: str) -> bool:
    """Whether *user* holds a grant of their own on *session_key*, in force or not."""
    from kiro_crew.messaging.session_trust import _trusted_senders

    return user in _trusted_senders.get(session_key, ())


def _is_turns_own_call(event: Any) -> bool:
    """Whether the permission request *event* is the turn's own call, not a helper
    agent's: a child session's and a native sub-agent's calls arrive on the parent
    turn's stream. Only a backend that marks its children's calls can tell them
    apart (``child_calls_marked``); one that does not answers no for every call."""
    return (
        getattr(event, "child_calls_marked", False) is True
        and not getattr(event, "sub_session_id", "")
        and not getattr(event, "native_child_call", False)
    )


def sender_trusted(sessions: Any, session_key: str, user: str, event: Any) -> bool:
    """Whether the permission request *event* of shared user *user*'s own turn on
    *session_key* skips the prompt.

    The turn drivers ask this at each permission request of a shared user's turn.
    The grant (``session_trust.add_trusted_sender``) never covers a helper agent's
    call (:func:`_is_turns_own_call`). It counts only on the session it was made on
    (:func:`session_binding`, read from *sessions* now) and while the person is
    still a shared user, read live, so taking them off ``slack.shared_access`` ends
    it mid-turn; a grant that fails either of those two tests is dropped.
    """
    if not _is_turns_own_call(event):
        return False
    from kiro_crew.messaging.session_trust import (
        is_sender_trusted,
        remove_trusted_sender,
        sender_binding,
    )

    if not holds_grant(session_key, user):
        return False
    from kiro_crew.slack import handler, shared_access

    resources = f"session={session_key} sender={user} mode=normal"
    if not shared_access.still_confirmed(
        handler.slack_cfg(), user, owner=bool(handler.is_owner(user))
    ):
        remove_trusted_sender(session_key, user)
        _audit(user, "allowed", f"{resources} reason=left_shared_access")
        return False
    binding = session_binding(sessions, session_key)
    granted = sender_binding(session_key, user)
    if granted is None:
        # Gone since the read above: a policy deny clears grants from another thread.
        return False
    if binding is None or granted != binding:
        remove_trusted_sender(session_key, user)
        _audit(user, "allowed", f"{resources} reason=session_replaced")
        return False
    return is_sender_trusted(session_key, user, binding)


def end_sender_grants(session_key: str, reason: str) -> int:
    """Drop every sender's grant on *session_key* and audit who lost one; how many."""
    from kiro_crew.messaging.session_trust import drop_trusted_senders

    senders = _senders(session_key)
    dropped = drop_trusted_senders(session_key)
    if dropped:
        _audit(
            "config",
            "allowed",
            f"session={session_key} senders={senders} mode=normal reason={reason}",
        )
    return dropped


def end_grants_on_turn_event(session_key: str, event: Any) -> None:
    """Drop the sender grants on *session_key* when *event* says the agent harness
    switched the running session's agent in place.

    Such a switch keeps the session's provider, so :func:`session_binding` alone
    would still match. The native turn loop passes every event of the turn here, so
    the grant ends before a later call of the same turn asks; the transport path's
    ``TurnDriver`` does the same drop itself.
    """
    if getattr(event, "kind", "") == _AGENT_SWITCHED_EVENT:
        end_sender_grants(session_key, "agent_switched")


def drop_unlisted_senders(cfg: Any) -> int:
    """Drop the grant of every sender *cfg* does not list; how many were dropped.

    The gateway calls it with each Slack config it applies, so a person taken off
    ``slack.shared_access`` (or the switch turned off) loses their grants then, and
    being listed again later does not bring them back.
    """
    from kiro_crew.messaging.session_trust import keep_trusted_senders
    from kiro_crew.slack import shared_access

    gone = ",".join(s for s in _senders().split(",") if s and not shared_access.listed(cfg, s))
    dropped = keep_trusted_senders(lambda sender: shared_access.listed(cfg, sender))
    if dropped:
        _audit("config", "allowed", f"senders={gone} mode=normal reason=left_shared_access")
    return dropped


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
    from kiro_crew.messaging.session_trust import (
        _trusted_sessions,
        drop_trusted_senders,
        remove_trusted_session,
    )

    # The owner's off is the thread's: every shared user's own grant goes with it.
    held = _senders(session_key)
    senders = drop_trusted_senders(session_key)
    resources = f"session={session_key} mode=normal" + (f" senders={held}" if senders else "")
    # A policy refresh may mask a grant without removing it.
    if session_key not in _trusted_sessions:
        tab_trust = _tab_holds_trust(state, session_key)
        _audit(
            user,
            "allowed",
            f"{resources} result={'set' if senders else 'unchanged'}"
            + (" tab_trust=held" if tab_trust else ""),
        )
        if tab_trust:
            return TAB_STILL_TRUSTED_TEXT
        return OFF_TEXT if senders else ALREADY_OFF_TEXT
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


#: Why :func:`_grant_own` made no grant, as the audit row names it.
_POLICY_REFUSAL = "mode_disabled_by_policy"


def _grant_own(sessions: Any, session_key: str, user: str, binding: SessionBinding | None) -> str:
    """Make *user*'s own-turns grant on *session_key* under *binding*; ``""`` when made.

    Otherwise why not, as the audit names it. Nothing is awaited, and everything is
    asked here: a person taken off the list meanwhile has no grant for a later
    config to drop, and a session replaced since *binding* was read is not the one
    the grant was asked for.
    """
    from kiro_crew.messaging import session_trust
    from kiro_crew.slack import handler

    if binding is None:
        return "no running session"
    if not handler.is_shared_user(user) or session_binding(sessions, session_key) != binding:
        return "changed during the request"
    try:
        session_trust.add_trusted_sender(session_key, user, binding)
    except PermissionError:
        return _POLICY_REFUSAL
    return ""


async def _own_turns(word: str, sessions: Any, session_key: str, user: str) -> str:
    """A shared user's ``!trust``: their own turns on *session_key*, nothing else.

    The same two policy gates as the owner's grant, and the same audit row with the
    shared user as its caller. ``off`` reads the raw grant, as the owner's does, so
    a grant a policy refresh is masking can still be removed; ``status`` reports a
    grant made on a session that has been replaced as off, and a masked one as turned
    off by policy: neither covers a turn.
    """
    from kiro_crew.messaging import session_trust
    from kiro_crew.safety_override import approval_mode_permitted

    resources = f"session={session_key} sender={user}"
    if word in _OFF_WORDS:
        removed = session_trust.remove_trusted_sender(session_key, user)
        _audit(
            user, "allowed", f"{resources} mode=normal result={'set' if removed else 'unchanged'}"
        )
        return OWN_OFF_TEXT if removed else OWN_ALREADY_OFF_TEXT
    binding = session_binding(sessions, session_key)
    granted = session_trust.sender_binding(session_key, user)
    if granted is not None and (binding is None or granted != binding):
        session_trust.remove_trusted_sender(session_key, user)
        _audit(user, "allowed", f"{resources} mode=normal reason=session_replaced")
    held = holds_grant(session_key, user)
    if word in _STATUS_WORDS:
        if not held:
            return OWN_STATUS_OFF_TEXT
        # A grant the policy is masking runs nothing: say so, not "on".
        return OWN_STATUS_ON_TEXT if session_trust.trust_permitted() else POLICY_TEXT
    resources += " mode=trust"
    if binding is None:
        _audit(user, "denied", resources, "no running session")
        return NO_SESSION_TEXT
    permitted = session_trust.trust_permitted() and await asyncio.to_thread(
        approval_mode_permitted, "trust"
    )
    if not permitted:
        _audit(user, "denied", resources, "mode_disabled_by_policy")
        return POLICY_TEXT
    if held:
        _audit(user, "allowed", f"{resources} result=unchanged")
        return OWN_ALREADY_ON_TEXT
    # Asked again, with no await before the grant: the checks above waited.
    refused = _grant_own(sessions, session_key, user, binding)
    if refused:
        _audit(user, "denied", resources, refused)
        return POLICY_TEXT if refused == _POLICY_REFUSAL else NOT_ALLOWED_TEXT
    _audit(user, "allowed", f"{resources} agent={binding.agent} result=set")
    return OWN_ON_TEXT


def grant_from_card(
    state: Any,
    sessions: Any,
    session_key: str,
    channel: str,
    reply_ts: str,
    user: str,
    pressed_on: SessionBinding | None = None,
) -> bool:
    """A shared user's own-turns grant, made by a card's Trust button; whether it was made.

    The grant their ``!trust`` makes (:func:`_grant_own`), where :func:`handle` lets a
    shared user make one: not in a thread that owns its approval mode, not in a
    channel a Channel Bot or Channel Conductor answers, not in a thread that runs a
    dashboard chat's or a cron's session. Never awaits, so the caller grants with
    nothing between its own checks and its answer; it reads the code-channel store,
    which the caller loads off the event loop first. *pressed_on* is the
    :func:`session_binding` a caller read at the press, when it awaited since: a
    session replaced meanwhile gets no grant. ``None`` reads the session now.
    """
    from kiro_crew.slack import channel_handler

    if (
        _has_own_approval_setting(channel, reply_ts)
        or channel_handler.handler_refusal(channel)
        or _is_linked(state, session_key, reply_ts)
    ):
        return False
    binding = session_binding(sessions, session_key) if pressed_on is None else pressed_on
    # Read back: a policy deny from another thread can clear the grant just made.
    return not _grant_own(sessions, session_key, user, binding) and holds_grant(session_key, user)


async def handle(
    arg: str,
    *,
    state: Any,
    session_key: str,
    channel: str,
    reply_ts: str,
    user: str,
    shared: bool = False,
    sessions: Any = None,
) -> str:
    """``!trust [on|off|status]``; the reply to post.

    *shared* is the caller's "not the owner": such a sender must be a confirmed
    shared user, and gets the grant for their own turns only (:func:`_own_turns`),
    bound to the session *sessions* holds for the thread.
    """
    if shared:
        from kiro_crew.slack import handler

        if not handler.is_shared_user(user):
            _audit(user, "denied", f"session={session_key}", "unauthorized sender")
            return NOT_ALLOWED_TEXT
    word = arg.strip().lower()
    if word not in _ON_WORDS | _OFF_WORDS | _STATUS_WORDS:
        return USAGE_TEXT
    if shared and word not in _ON_WORDS and holds_grant(session_key, user):
        # Their own grant, however it was made, can be seen and removed in any thread.
        return await _own_turns(word, sessions, session_key, user)
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
    if shared:
        return await _own_turns(word, sessions, session_key, user)
    if word in _STATUS_WORDS:
        from kiro_crew.messaging.session_trust import _trusted_senders, _trusted_sessions

        senders = len(_trusted_senders.get(session_key, ()))
        note = SENDERS_TEXT.format(count=senders) if senders else ""
        if session_key in _trusted_sessions:
            return STATUS_ON_TEXT + note
        tab_trust = _tab_holds_trust(state, session_key)
        return (STATUS_TAB_TRUSTED_TEXT if tab_trust else STATUS_OFF_TEXT) + note
    if word in _OFF_WORDS:
        return revoke(state, session_key, user)
    return await grant(session_key, user)
