"""Code channels are the owner's: the refusals a shared user (``slack.shared_access``) meets.

The one exception is a shared user in the code channel they opened
(:func:`opener_admitted`): it admits them like the owner, except for the controls
that stay the owner's (Trust this channel, the approval-mode and agent pickers,
``!`` commands, the helper-agent Turn-on, the channel's slash commands, Archive).

Split out of :mod:`kiro_crew.slack.code_channels.routing`, which re-exports the names
its callers import from it, so that module stays their import path; the OPTIONS-answer
permit (:func:`options_answer_permitted`) moved here with them. Collaborators are read
off ``routing`` at call time, so a test that patches one there reaches these too,
except where a function's docstring names another module (``events``, ``handler``).
The two the Slack message router calls (:func:`shared_message_dropped`,
:func:`shared_stop_refused`) are imported from here by :mod:`kiro_crew.slack.events`.

A refused shared user is told why, privately (:func:`tell_owner_only`): at most once
per person per channel per :data:`NOTICE_WINDOW_SECS` for their messages, since a
channel member posts many messages that are not for the bot.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

#: What a shared user is told, privately, when a code channel refuses them.
OWNER_ONLY_TEXT = (
    "⛔ Only Kiro Crew's owner or the person who asked me to open this code channel "
    "can work in it, so I didn't act on that. You can still ask me in a regular "
    "channel or a DM, and I'll open a code channel of your own when the work needs one."
)
#: One notice per person per channel in this window, for their messages.
NOTICE_WINDOW_SECS = 600.0
_NOTICED_MAX = 1024
_noticed: dict[tuple[str, str], float] = {}

#: What the sender of the message ``routing.dispatch`` is handling typed, before file
#: ingestion and voice transcription rewrote it; None when the caller gave none.
TYPED_TEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "code_channel_typed_text", default=None
)


def typed_text(clean_text: str) -> str:
    """The words a shared user's command is judged on, as the normal path judges
    ``spool_text``: what they typed, not *clean_text*, whose ingested form leads with
    an attachment's absolute temp path and carries file contents."""
    typed = TYPED_TEXT.get()
    return clean_text if typed is None else typed


def _r() -> ModuleType:
    """``routing``, read at call time (it re-exports this module, so not at import)."""
    from kiro_crew.slack.code_channels import routing

    return routing


def _notice_due(user: str, channel: str) -> bool:
    """Whether *user* may be told again in *channel*; records the notice when so."""
    now = time.monotonic()
    key = (user, channel)
    last = _noticed.pop(key, None)
    if last is not None and now - last < NOTICE_WINDOW_SECS:
        _noticed[key] = last
        return False
    while len(_noticed) >= _NOTICED_MAX:
        del _noticed[next(iter(_noticed))]  # the oldest notice
    _noticed[key] = now
    return True


async def tell_owner_only(
    slack: Any, channel: str, user: str, thread_ts: str | None = None, *, throttle: bool = True
) -> None:
    """Tell the shared user *user*, in an ephemeral only they see, that code channels
    are the owner's. *throttle* off answers every call (a button press is always meant
    for the bot). Never raises: the refusal stands whether or not the notice lands.
    """
    if slack is None or not channel or not user:
        return
    if throttle and not _notice_due(user, channel):
        return
    try:
        await slack.post_ephemeral(channel, user, OWNER_ONLY_TEXT, thread_ts=thread_ts)
    except Exception:
        _r().logger.debug("code channel: owner-only notice failed", exc_info=True)


def opener_admitted(binding: Any, sender: str) -> bool:
    """Whether *sender* is the shared user who opened *binding*'s channel, and still
    one: the one person besides the owner a code channel admits.

    Asked of the person now (``shared_access.still_confirmed``: on the live list, and
    Slack's last answer confirmed them), never of an earlier admission, so removing
    them from ``slack.shared_access`` cuts them off at once. It does not expire with
    the confirmation's cache: the channel's session is judged mid-turn too, where
    nothing asks Slack again. A binding with no opener (the owner's channel, one
    Slack's UI opened, or a row written before openers were recorded) admits nobody.
    """
    opener = getattr(binding, "opener", "")
    if not (sender and isinstance(opener, str) and opener == sender):
        return False
    from kiro_crew.slack import handler, shared_access

    return shared_access.still_confirmed(
        handler.slack_cfg(), sender, owner=bool(handler.is_owner(sender))
    )


def opener_of_request(sender: str) -> str | None:
    """The opener a code channel *sender* asks for would record: ``""`` for the owner
    (or anyone who is not a shared user), *sender* for a confirmed shared user, and
    None for a listed one Slack does not confirm, whose request opens nothing."""
    from kiro_crew.slack.handler import is_shared_listed, is_shared_user

    if not is_shared_listed(sender):
        return ""
    return sender if is_shared_user(sender) else None


def opener_answers_prompt(channel: str, slot_key: str, session_key: str, sender: str) -> bool:
    """Whether *sender* may answer a tool prompt the slot *slot_key* asked on the
    session *session_key*, on a card in *channel*: the code channel bound there is that
    very slot, its turn ran on the channel's own session (a slot relinked to another
    session asks for that session, which is not the opener's), and *sender* is its
    opener (:func:`opener_admitted`) acting in it. Trust is never theirs (the caller
    keeps it the owner's). Fails closed on a fenced or unreadable store. Reads the
    store, so async callers run it in a thread.
    """
    from kiro_crew.dashboard.chat_utils import session_key_for

    if not (channel and slot_key and session_key and sender) or shared_user_refused_in(
        channel, sender=sender
    ):
        return False
    try:
        binding = _r().store().get_binding(channel)
    except Exception:
        _r().logger.warning("code channel: opener prompt lookup failed", exc_info=True)
        return False
    bare = slot_key[len("dashboard:") :] if slot_key.startswith("dashboard:") else slot_key
    return (
        binding is not None
        and bool(bare)
        and binding.session_key == bare
        and session_key == session_key_for(binding.session_key)
    )


def opening_for(channel: str, sender: str) -> bool:
    """Whether this process is opening *channel* right now for *sender*'s request, and
    *sender* is still a confirmed shared user.

    The opener is admitted from their request's route until the channel is revoked;
    between ``agents.conversations.create`` and the binding write there is no binding
    yet, so their message in the new channel is let through to wait for the binding
    (``routing._wait_for_creation``) and is judged on the stored binding at the
    route's commit point (:func:`opener_still_admitted`). Once the creation is over
    with no binding (a failed bind), the route whose saved destination is *channel*
    still names its opener, so their retry adopts the channel as theirs. Another
    person, or a channel no route of ours opened, admits nobody.
    The channel is matched by the in-memory ``routing._opening`` entry, set the
    moment Slack names the new channel, never by the route's saved destination,
    whose write may not have landed (or may have failed) yet.
    """
    r = _r()
    sid = r._opening.get(channel, "")
    if sid and sid in r._creating:
        route = r.store().get_route(sid)
    else:
        # The creation is over and left no binding (its bind failed, and the opener
        # was told to retry): the route that recorded this channel still names them.
        route = r.store().route_to(channel)
    return (
        route is not None
        and bool(route.opener)
        and route.opener == sender
        and opener_of_request(sender) == sender
    )


async def opener_still_admitted(
    binding: Any, sender: str, inner: Callable[[], Awaitable[bool]] | None
) -> bool:
    """The commit-point gate for a shared user's message: *inner* (a command's own
    fences) first, then, with nothing awaited after it, the channel is still bound to
    this very session and *sender* is still its opener (:func:`opener_admitted`), so
    a removal or rebind during the route's awaits appends nothing."""
    if inner is not None and not await inner():
        return False
    current = _r().store().get_binding(binding.channel_id)
    return (
        current is not None
        and current.session_key == binding.session_key
        and opener_admitted(current, sender)
    )


def shared_stop_still_admitted(sender: str, session_key: str, thread_ts: str, channel: str) -> bool:
    """Whether a shared user's Stop of *session_key* may run NOW, with no await: still
    admitted (``is_shared_user``), and, in a code channel, still its opener on the
    binding stored now. True for anyone who is not a shared user. Reads only the
    store the earlier checks loaded."""
    from kiro_crew.slack.handler import is_shared_listed, is_shared_user

    if not is_shared_listed(sender):
        return True
    return bool(is_shared_user(sender)) and not (
        shared_user_refused_in(channel, sender=sender)
        or shared_user_refused_for_slot(session_key, thread_ts, sender=sender, channel=channel)
    )


def shared_user_refused_in(channel: str, thread_ts: str = "", *, sender: str = "") -> bool:
    """Whether a shared user (``slack.shared_access``) is refused in *channel*.

    True in a bound code channel whether or not ``slack.code_channels`` is on: a
    binding kept while the feature is off still leads to the owner's session. The
    exception is *sender* in the channel they opened (:func:`opener_admitted`);
    keyword-only, so a positional second argument is always *thread_ts*.
    Fails closed for a channel that could be a code channel when the store cannot be
    read. Reads the store from disk on first use, so async callers run it in a thread.

    With *thread_ts*, also True in that thread when it is a Conductor thread --
    live, ended, pruned into ``revoked`` or quarantined -- in any channel or DM,
    whatever the switch says, and when the store cannot be read.
    """
    if thread_ts and _conductor_thread_refused(channel, thread_ts):
        return True
    if not _r().recognition.could_be_code_channel(channel):
        return False
    try:
        bindings = _r().store()
        binding = bindings.get_binding(channel)
        # A row that did not read back whole fences its channel, for them too.
        if bindings.unreadable or bindings.is_quarantined(channel):
            return True
        # A revoked channel's anchor thread still leads to the owner's session.
        revoked = bindings.was_revoked(channel)
        if binding is None or revoked:
            return binding is not None or revoked
        return not opener_admitted(binding, sender)
    except Exception:
        _r().logger.warning("code channel: shared-user binding lookup failed", exc_info=True)
        return True


def _conductor_thread_refused(channel: str, thread_ts: str) -> bool:
    """Whether *thread_ts* in *channel* is (or was) a Conductor thread; fails closed."""
    try:
        current = _r().store()
        return (
            current.get_thread_binding(channel, thread_ts) is not None
            or current.is_thread_quarantined(channel, thread_ts)
            or current.revoked_anchor(thread_ts)
            or current.unreadable
        )
    except Exception:
        _r().logger.warning("conductor thread: shared-user lookup failed", exc_info=True)
        return True


async def shared_command_dropped(
    orch: Any, event: dict[str, Any], clean_text: str, sender_id: str
) -> bool:
    """Whether a DIRECTED message is a shared user's ``/`` command or privacy modifier,
    refused (audited, and they are told) as in every thread (``shared_command_refused``):
    the opener's channel admits their words, never the session's own commands
    (``/agent``, ``/model``, ...). Judged on what they typed (:func:`typed_text`)."""
    from kiro_crew.slack.shared_access import SHARED_OWNER_ONLY_TEXT, shared_command_refused

    if not shared_command_refused(sender_id, typed_text(clean_text)):
        return False
    r = _r()
    channel = str(event.get("channel") or "")
    r._audit(sender_id, "slack.code_channel.message", "denied", f"channel={channel}", "command")
    try:
        await r._slack(orch).post_ephemeral(channel, sender_id, SHARED_OWNER_ONLY_TEXT)
    except Exception:
        r.logger.debug("code channel: owner-only command notice failed", exc_info=True)
    return True


async def shared_sender_dropped(sender_id: str, channel: str) -> bool:
    """Whether a message from a shared user in *channel* is dropped (and audited) at the
    code-channel gate: code channels are the owner's and their openers', with the
    feature on or off."""
    from kiro_crew.slack.handler import is_shared_listed

    if not (
        is_shared_listed(sender_id)
        and await asyncio.to_thread(shared_user_refused_in, channel, sender=sender_id)
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


async def shared_message_dropped(
    sender_id: str, channel: str, slack: Any, thread_ts: str | None = None
) -> bool:
    """Only for a sender the caller already judged a shared user: whether their message
    in *channel* is dropped (and audited) by the Slack message router, in a bound or
    revoked code channel (:func:`shared_user_refused_in`). Unlike
    :func:`shared_sender_dropped` it does not check the sender itself.

    Every drop is audited; the sender is told why through *slack* (the client the
    router read the message with), throttled by :func:`tell_owner_only`: a channel
    member posts many messages that are not for the bot, and a reply to each would
    be noise. The check is read off ``routing`` and ``sel`` off
    :mod:`kiro_crew.slack.events` (the router) at call time, so a patch of either
    reaches it.
    """
    from kiro_crew.slack import events

    if not await asyncio.to_thread(_r().shared_user_refused_in, channel, sender=sender_id):
        return False
    events.sel().log_api_access(
        caller=sender_id,
        operation="slack.message",
        outcome="denied",
        source="slack",
        resources=f"channel={channel}",
        error="code channel is owner-only",
    )
    await tell_owner_only(slack, channel, sender_id, thread_ts)
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


async def shared_user_refused_here(slack: Any, channel: str, sender: str = "") -> bool:
    """Whether a shared user is refused in *channel*: a bound or revoked code channel
    (:func:`shared_user_refused_in`, which admits *sender* in the channel they
    opened) or an unbound one of ours."""
    if await asyncio.to_thread(shared_user_refused_in, channel, sender=sender):
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


def _runs_its_own_session(binding: Any) -> bool:
    """Whether *binding*'s live tab, if open, still runs the channel's own session,
    as :func:`.binding.channel_session` requires: a ``linked_session_key`` read back
    onto it names another session, which is not the opener's to reach."""
    from kiro_crew.dashboard.chat_utils import session_key_for

    live = _effective_session_of(binding.session_key)
    return not live or live == session_key_for(binding.session_key)


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


def shared_user_refused_for_slot(
    slot_key: str, thread_ts: str, *, sender: str = "", channel: str = "", _alias: bool = True
) -> bool:
    """Whether a shared user is refused a linked thread that leads to a code channel's
    or a Conductor thread's session: the slot is a bound channel's or thread's session
    or a helper agent one started, or the thread is a bound or revoked anchor. Fails closed when the store
    cannot be read. Reads the store, so async callers run it in a thread.

    The one exception is *sender* acting in *channel*, the code channel they opened
    (:func:`opener_admitted`), reaching that channel's own session or anchor. It is
    tied to the channel the act happens in, so a thread elsewhere linked or relinked
    to that session, a helper agent it started, or any other channel's session
    stays refused.
    """
    # Callers hold a session in any of its spellings: the bare slot key a binding
    # stores, the ``dashboard:<slot>`` key the thread index and askers use, or a
    # channel-born slot's ``slack:<anchor>`` session.
    bare = slot_key[len("dashboard:") :] if slot_key.startswith("dashboard:") else slot_key
    # A live tab named *bare* may run another session (``linked_session_key``, e.g. a
    # workflow tab kept on its origin's session): that session is judged too.
    effective = _effective_session_of(bare) if _alias else ""
    if effective and effective not in (bare, slot_key):
        # One hop, under the same rule.
        if shared_user_refused_for_slot(
            effective, "", sender=sender, channel=channel, _alias=False
        ):
            return True
    anchor_of_key = bare[len("slack:") :] if bare.startswith("slack:") else ""
    try:
        bindings = _r().store()
        admitted = ""  # the opener's own channel's session, when that is what matched
        # Code channels and Conductor threads alike (loads the store first). A
        # Conductor thread records no opener, so it admits no shared user.
        for binding in (*bindings.bindings(), *bindings.thread_bindings()):
            if (
                (bare and binding.session_key in (bare, slot_key))
                or (thread_ts and binding.anchor_ts == thread_ts)
                or (anchor_of_key and binding.anchor_ts == anchor_of_key)
            ):
                if (
                    channel
                    and binding.channel_id == channel
                    and opener_admitted(binding, sender)
                    and _runs_its_own_session(binding)
                ):
                    admitted = binding.session_key
                    continue
                return True
        if bare and bindings.worker_conductor(bare):
            return True  # a helper agent a code channel's session started is its work
        keys = bindings.session_keys() - {admitted}
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
        if is_shared_listed(sender) and _r().shared_user_refused_in(channel, sender=sender):
            # Code channels are the owner's and their openers' (slack.shared_access),
            # with the feature on or off: an answer here would be a turn in a session
            # this person may not drive.
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
