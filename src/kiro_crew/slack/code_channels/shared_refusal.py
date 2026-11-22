"""Code channels are the owner's: the refusals a shared user (``slack.shared_access``) meets.

Split out of :mod:`kiro_crew.slack.code_channels.routing`, which re-exports the names
its callers import from it, so that module stays their import path; the OPTIONS-answer
permit (:func:`options_answer_permitted`) moved here with them. Collaborators are read
off ``routing`` at call time, so a test that patches one there reaches these too,
except where a function's docstring names another module (``events``, ``handler``).
The two the Slack message router calls (:func:`shared_message_dropped`,
:func:`shared_stop_refused`) are imported from here by :mod:`kiro_crew.slack.events`.
"""

from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Any


def _r() -> ModuleType:
    """``routing``, read at call time (it re-exports this module, so not at import)."""
    from kiro_crew.slack.code_channels import routing

    return routing


def shared_user_refused_in(channel: str) -> bool:
    """Whether a shared user (``slack.shared_access``) is refused in *channel*.

    True in a bound code channel whether or not ``slack.code_channels`` is on: a
    binding kept while the feature is off still leads to the owner's session. Fails
    closed for a channel that could be a code channel when the store cannot be
    read. Reads the store from disk on first use, so async callers run it in a thread.
    """
    if not _r().recognition.could_be_code_channel(channel):
        return False
    try:
        bindings = _r().store()
        return (
            bindings.get_binding(channel) is not None
            # A revoked channel's anchor thread still leads to the owner's session.
            or bindings.was_revoked(channel)
            # A row that did not read back whole fences its channel, for them too.
            or bindings.is_quarantined(channel)
            or bindings.unreadable
        )
    except Exception:
        _r().logger.warning("code channel: shared-user binding lookup failed", exc_info=True)
        return True


async def shared_sender_dropped(sender_id: str, channel: str) -> bool:
    """Whether a message from a shared user in *channel* is dropped (and audited) at the
    code-channel gate: code channels are the owner's, with the feature on or off."""
    from kiro_crew.slack.handler import is_shared_listed

    if not (
        is_shared_listed(sender_id) and await asyncio.to_thread(shared_user_refused_in, channel)
    ):
        return False
    _r()._audit(
        sender_id,
        "slack.code_channel.message",
        "denied",
        f"channel={channel}",
        "code channel is owner-only",
    )
    return True


async def shared_message_dropped(sender_id: str, channel: str) -> bool:
    """Only for a sender the caller already judged a shared user: whether their message
    in *channel* is dropped (and audited) by the Slack message router, in a bound or
    revoked code channel (:func:`shared_user_refused_in`). Unlike
    :func:`shared_sender_dropped` it does not check the sender itself.

    Refused quietly, with the audit row only: a channel member posts many messages
    that are not for the bot, and a reply to each would be noise. The check is read
    off ``routing`` and ``sel`` off :mod:`kiro_crew.slack.events` (the router) at call
    time, so a patch of either reaches it.
    """
    from kiro_crew.slack import events

    if not await asyncio.to_thread(_r().shared_user_refused_in, channel):
        return False
    events.sel().log_api_access(
        caller=sender_id,
        operation="slack.message",
        outcome="denied",
        source="slack",
        resources=f"channel={channel}",
        error="code channel is owner-only",
    )
    return True


async def shared_stop_refused(
    orch: Any, sender_id: str, session_key: str, thread_root: str, channel: str
) -> bool:
    """Whether a shared user's ``!stop`` stops nothing: it would act on a code channel's
    session, either *session_key* or the session that owns the thread *thread_root*.

    False for anyone who is not a shared user. ``is_shared_listed`` is read off
    :mod:`kiro_crew.slack.events` (the Slack message router, the caller) and the session
    check off :mod:`kiro_crew.slack.handler`, both at call time, so a patch of either
    reaches them. A thread relinked while this checks is refused too: the next
    ``!stop`` is judged afresh.
    """
    from kiro_crew.slack import events, handler

    if not events.is_shared_listed(sender_id):
        return False
    stop_owner = orch.sessions.get_session_for_thread(thread_root)
    for key in {session_key, str(stop_owner or "")}:
        if await handler.shared_user_refused_for_session(sender_id, key, thread_root, channel):
            return True
    return bool(orch.sessions.get_session_for_thread(thread_root) != stop_owner)


async def shared_user_refused_here(slack: Any, channel: str) -> bool:
    """Whether a shared user is refused in *channel*: a bound or revoked code channel
    (:func:`shared_user_refused_in`) or an unbound one of ours."""
    if await asyncio.to_thread(shared_user_refused_in, channel):
        return True
    return await shared_user_refused_unbound(slack, channel)


async def shared_user_refused_unbound(slack: Any, channel: str) -> bool:
    """Whether *channel* is this bot's code channel that Slack's UI opened and no
    binding records yet, with Slack Code on: a shared user's typed message there is
    refused by :func:`inbound_gate`, so their card answer is too. A failed lookup
    is not read as ours, as there."""
    if not _r().code_channels_on(
        _r().live_config(None)
    ) or not _r().recognition.could_be_code_channel(channel):
        return False
    if await asyncio.to_thread(lambda: _r().store().get_binding(channel)) is not None:
        return False  # bound: shared_user_refused_in already answered
    client = _r().CodeChannelsClient.from_slack(slack)
    kind, _info = await _r().recognition.classify_channel(client, channel, _r()._self_user_id())
    return kind is _r().ChannelKind.OURS


def _effective_session_of(slot_key: str) -> str:
    """The session the live dashboard tab *slot_key* runs on, or "" with no such tab."""
    slot = getattr(_r()._dashboard_state(), "_slots", {}).get(slot_key) if slot_key else None
    if slot is None:
        return ""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    return str(effective_session_key(slot) or "")


def _quarantined_session_matches(
    bindings: Any, bare: str, slot_key: str, thread_ts: str, anchor_of_key: str
) -> bool:
    """Whether a quarantined binding row may lead to this session or thread.

    The row's session is told by its key, else by its anchor's live Slack link. A row
    whose session cannot be told either way fences every linked session (fails closed).
    """
    state = _r()._dashboard_state()
    for key, anchor in bindings.quarantined_identities():
        if anchor and anchor in (thread_ts, anchor_of_key):
            return True
        if key is None:
            continue  # a revoked row names no session: its anchor is all it fences
        linked = state.get_linked_slot(anchor) if anchor and state is not None else None
        linked_key = str(getattr(linked, "key", "") or "")
        if not (key or linked_key):
            return True
        if bare and (key in (bare, slot_key) or linked_key in (bare, slot_key)):
            return True
    return False


def shared_user_refused_for_slot(slot_key: str, thread_ts: str, *, _alias: bool = True) -> bool:
    """Whether a shared user is refused a linked thread that leads to a code channel's
    session: the slot is a bound channel's session or a helper agent one started, or
    the thread is a bound or revoked channel's anchor. Fails closed when the store
    cannot be read. Reads the store, so async callers run it in a thread.
    """
    # Callers hold a session in any of its spellings: the bare slot key a binding
    # stores, the ``dashboard:<slot>`` key the thread index and askers use, or a
    # channel-born slot's ``slack:<anchor>`` session.
    bare = slot_key[len("dashboard:") :] if slot_key.startswith("dashboard:") else slot_key
    # A live tab named *bare* may run another session (``linked_session_key``, e.g. a
    # workflow tab kept on its origin's session): that session is judged too.
    effective = _effective_session_of(bare) if _alias else ""
    if effective and effective not in (bare, slot_key):
        if shared_user_refused_for_slot(effective, "", _alias=False):  # one hop
            return True
    anchor_of_key = bare[len("slack:") :] if bare.startswith("slack:") else ""
    try:
        bindings = _r().store()
        for binding in bindings.bindings():  # loads the store first
            if (
                (bare and binding.session_key in (bare, slot_key))
                or (thread_ts and binding.anchor_ts == thread_ts)
                or (anchor_of_key and binding.anchor_ts == anchor_of_key)
            ):
                return True
        if bare and bindings.worker_conductor(bare):
            return True  # a helper agent a code channel's session started is its work
        keys = bindings.session_keys()
        if bare and (bare in keys or slot_key in keys):
            return True  # a quarantined row's or an earlier binding's session
        if _quarantined_session_matches(bindings, bare, slot_key, thread_ts, anchor_of_key):
            return True
        return (
            bindings.unreadable
            or bindings.revoked_anchor(thread_ts)
            or bindings.revoked_anchor(anchor_of_key)
        )
    except Exception:
        _r().logger.warning("code channel: shared-user slot lookup failed", exc_info=True)
        return True


def options_answer_permitted(channel: str, sender: str = "") -> bool:
    """A button or selection answered in *channel* may reach its session.

    True when *channel* is not a bound code channel (or code channels are
    off): the ordinary path's own rules apply there. In a bound code channel
    an answer is a turn like a typed message, so it obeys the same rule as
    :func:`~kiro_crew.slack.code_channels.routing.inbound_gate`: the channel
    and its origin must both still admit the channel's turns. A control
    posted before the channel or its origin went to ``review`` or ``off`` is
    refused (SEL-audited), never dispatched.
    So is an answer in a channel this gateway unbound because Slack stopped
    listing us there (:meth:`CodeChannelStore.was_revoked`). A failed lookup
    refuses too, since it cannot show the channel is unbound.
    """
    try:
        from kiro_crew.slack.handler import is_shared_listed

        cfg = _r().live_config(None)
        if is_shared_listed(sender) and _r().shared_user_refused_in(channel):
            # Code channels are the owner's (slack.shared_access), with the feature
            # on or off: an answer here would be a turn in the owner's session.
            origin, error = "", "code channel is owner-only"
        elif not _r().code_channels_on(cfg) or not _r().recognition.could_be_code_channel(channel):
            return True
        elif (binding := _r().store().get_binding(channel)) is None and _r().store().is_quarantined(
            channel
        ):
            # Its binding row does not read back whole: the channel is fenced, so
            # nothing in it -- a kickoff-thread control included -- reaches a session.
            origin, error = "", "code channel binding row unreadable"
        elif binding is None:
            if not _r().store().was_revoked(channel):
                return True
            # Unbound because Slack does not list us here: a control its
            # session posted earlier must not run a turn in that session.
            origin, error = "", "code channel no longer bound"
        else:
            origin = _r()._origin_of(channel, binding, None)
            if _r().channel_and_origin_permit(cfg, channel, origin):
                return True
            error = "origin activation does not admit a code channel"
    except Exception:
        _r().logger.warning("code channel: options permit lookup failed", exc_info=True)
        origin, error = "", "permit lookup failed"
    _r()._audit(
        sender, "slack.code_channel.options", "denied", f"channel={channel} origin={origin}", error
    )
    return False
