"""Channel-side delivery for the host spawn-approval prompt — one seam, every channel.

The single host-wide ``SubagentManager`` owns ONE ``on_spawn_approval`` callback,
built in ``slack/gateway.py``, whose own surfaces are a Slack owner DM and the
dashboard. Neither one can be answered by the person who started a spawn from a
Telegram (or any other) channel conversation, and the gate refuses such a spawn
fast with ``no_approval_surface`` rather than parking it to the reaper's
deadline. The prompt belongs on the ORIGINATING channel's own inline keyboard,
which each channel already renders for mid-run tool prompts but which the host
spawn gate cannot reach by itself — the driver's per-turn ``decider`` is wired to
the main-agent tool ladder, not to this callback.

This module is that reach: a process-global registry a channel dispatcher
registers a delivery hook into, keyed by the channel namespace it owns
(``telegram``, ``slack``, …). The spawn-approval callback consults it FIRST,
given the spawn's ``parent_session_key``, and:

* a hook returning ``True``/``False`` is the user's in-channel decision — run or
  reject — and the callback returns it verbatim;
* a hook returning ``None`` means "this channel is registered but could not
  surface the prompt for THIS session" (an unroutable key, a send that failed),
  so the callback falls through to the existing Slack-DM/dashboard path exactly
  as before;
* no hook registered for the session's channel is the same fall-through.

Why a registry keyed by channel namespace rather than by ``parent_session_key``:
a channel runs one dispatcher for the whole process, and the session's channel is
already recoverable from its key prefix (``messaging.link.channel_namespace_of``),
which is the authoritative classifier the rest of the system uses. Keying by the
full session key would need the gate to know every live session, which is exactly
the coupling the seam exists to avoid.

In memory only, deliberately, and modelled on ``messaging/session_trust.py``: a
delivery hook is a live object on a running dispatcher, so a registration that
survived a restart would name a dispatcher that is already gone. The registry
dies with the process; a channel re-registers on its next startup.

``messaging`` may not import a channel package, so the hook is a plain async
callable the channel supplies — the same inversion ``session_trust`` uses to let
a Slack widget write a grant a Telegram turn can read.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.link import channel_namespace_of

logger = logging.getLogger(__name__)

#: A channel's spawn-approval delivery hook.
#:
#: ``async def(request_id, description, parent_session_key) -> bool | None`` — the
#: SAME three arguments the host ``SpawnApprovalCallback`` receives, so a channel
#: can post its prompt and await the press without the seam reshaping anything.
#: ``True``/``False`` is the user's decision; ``None`` means "not surfaced here,
#: fall through" (see the module docstring).
SpawnApprovalDeliveryHook = Callable[[str, str, str], Awaitable["bool | None"]]

#: Channel namespace (``telegram``, ``slack``, …) -> its live delivery hook. One
#: dispatcher per channel per process, so a namespace maps to at most one hook.
#: In memory only: a hook is a live object on a running dispatcher.
_HOOKS: dict[str, SpawnApprovalDeliveryHook] = {}


def register_channel_delivery(channel: str, hook: SpawnApprovalDeliveryHook) -> None:
    """Register *hook* as the spawn-approval delivery surface for *channel*.

    *channel* is a channel namespace (``telegram``), the same token
    :func:`channel_namespace_of` returns for that channel's session keys — that is
    what lets :func:`resolve_channel_delivery` find the hook from a bare
    ``parent_session_key``. Idempotent by construction: a channel that restarts
    replaces its own prior hook, so a stale registration can never shadow the live
    dispatcher.
    """
    if not channel:
        return
    _HOOKS[channel] = hook


def unregister_channel_delivery(
    channel: str, hook: "SpawnApprovalDeliveryHook | None" = None
) -> None:
    """Drop *channel*'s delivery hook (idempotent).

    Called on a channel's shutdown so the gate stops routing to a dispatcher that
    is going away. Absent-key safe: a channel that failed to start never
    registered, and its shutdown path still calls this.

    *hook* is the registration the caller believes it owns, and supplying it makes
    this a COMPARE-AND-DROP. Two dispatcher lifetimes can overlap: a restart
    registers the replacement hook (``register_channel_delivery`` replaces the
    slot) before the OLD client's close callback runs, and an unconditional pop
    there removes the LIVE replacement — leaving the channel with no delivery
    surface at all, which the gate reads as "fall through to Slack/dashboard" for
    every later spawn until the next restart. Comparing first makes a stale close
    a no-op. Equality rather than identity because the registered hook is a bound
    method: each ``dispatcher.deliver_spawn_approval`` attribute access mints a new
    object, and two of them compare equal exactly when they name the same method on
    the same dispatcher, which is the question being asked here.

    Omitted, the drop stays unconditional for a caller holding no handle on its own
    registration.
    """
    if hook is not None and _HOOKS.get(channel) != hook:
        return
    _HOOKS.pop(channel, None)


def resolve_channel_delivery(parent_session_key: str) -> "SpawnApprovalDeliveryHook | None":
    """The delivery hook whose channel owns *parent_session_key*, or ``None``.

    ``None`` when the key is unowned (the CLI spawns with no parent), sits in a
    non-channel namespace (``dashboard:``, ``cron:``, ``subagent:``), or its
    channel has registered no hook. The caller treats every ``None`` the same way:
    fall through to the Slack-DM/dashboard path.
    """
    channel = channel_namespace_of(parent_session_key)
    if not channel:
        return None
    return _HOOKS.get(channel)


async def deliver_spawn_approval(
    request_id: str, description: str, parent_session_key: str
) -> "bool | None":
    """Offer the spawn prompt to the originating channel; ``None`` = fall through.

    Consulted FIRST by the host spawn-approval callback. Returns the channel's
    ``True``/``False`` decision when a hook answered, or ``None`` when no hook is
    registered for the session's channel, the operator's ``channels`` governance
    ceiling denies that channel, or the hook itself returned ``None`` (it could
    not surface the prompt here). A hook that RAISES is contained and read as
    ``None``: a channel-delivery bug must degrade to the existing fallback, never
    turn a spawn the operator could still answer on Slack/dashboard into a hard
    failure.

    The ceiling is checked HERE rather than inside each hook, because every hook
    posts an interactive prompt whose answering press arrives INBOUND on the same
    channel, and a denied channel drops that press (only an explicit reject is
    exempt on a channel's callback path). A prompt posted under a deny is
    unanswerable, so its deny-by-default wait elapses and the host gate reads the
    elapsed wait as a refusal the operator never made. One check at the routing
    layer that already resolves the channel gates every present and future hook;
    a copy inside each dispatcher would be the same authority duplicated per
    implementation, and the next hook written without it reopens the hole.

    It runs AFTER hook resolution, so a key in a non-channel namespace
    (``dashboard:``, ``cron:``) or a ``unified`` DM bucket — neither of which
    names a governed channel — falls through without asking the profile store
    about a channel type that does not exist.

    The check before the prompt covers only half the window, because the gate holds
    a spawn for as long as its approval takes and a deny can land while the prompt
    is already pending. :func:`unpressed_wait_answer` is the other half: a hook
    whose wait elapsed with no press asks it what that means, and gets ``None``
    rather than ``False`` once the channel is denied.
    """
    hook = resolve_channel_delivery(parent_session_key)
    if hook is None:
        return None
    channel = channel_namespace_of(parent_session_key)
    if not await channel_inbound_permitted(channel):
        logger.info(
            "Spawn-approval channel delivery skipped on %s for %s; the channel is "
            "denied by channels governance policy, so the prompt would be "
            "unanswerable there",
            channel,
            request_id,
        )
        return None
    try:
        return await hook(request_id, description, parent_session_key)
    except Exception:
        # The surface, not the key: the channel is what an operator acts on, and a
        # raw session key can carry the peer's platform id. The request id is named
        # too so this seam-level warning matches the granularity of the
        # dispatcher-level one (which also logs the ``rid``) when both fire.
        logger.warning(
            "Spawn-approval channel delivery failed on %s for %s; falling through "
            "to the Slack/dashboard path",
            channel_namespace_of(parent_session_key) or "unknown",
            request_id,
            exc_info=True,
        )
        return None


async def unpressed_wait_answer(channel: str, request_id: str) -> "bool | None":
    """What a hook answers when its deny-by-default wait elapsed UNPRESSED.

    ``False`` — a real deny-by-default the operator declined to answer — while
    *channel* is still permitted, and ``None`` (fall through) once the operator's
    ``channels`` ceiling denies it.

    :func:`deliver_spawn_approval` consults the ceiling BEFORE a hook posts, so a
    channel already denied is never prompted. This is the rest of that window: the
    gate holds a spawn for as long as its approval takes, so a deny can land while
    the prompt is pending. From that moment the channel's callback path drops the
    answering press — every press except an explicit reject — so the prompt is no
    longer answerable, the wait elapses, and a bare ``False`` would hand the host
    gate a refusal the operator never made. A deny withholds the channel; it does
    not vote in the operator's name.

    The split of duties is why this is a seam function a hook calls rather than
    something either side does alone. Only the hook can know its wait elapsed with
    no press: the decision arrives as a bool, and the cause behind it
    (``last_deny_cause``, see :data:`~kiro_crew.messaging.driver.ApprovalDecider`)
    is the channel decider's own. Only the seam should decide what that fact MEANS,
    because that reading is the ceiling's authority and belongs in one place for
    every channel rather than copied per dispatcher.

    A hook that answers a bare ``False`` without calling this keeps today's
    behaviour. That is the conservative direction on purpose: the seam cannot tell
    such a ``False`` apart from a press, and reading a press as unpressed would
    turn an operator's explicit reject into a fall-through that re-offers the spawn
    they just refused.
    """
    if await channel_inbound_permitted(channel):
        return False
    logger.info(
        "Spawn-approval prompt on %s for %s went unanswered and the channel is now "
        "denied by channels governance policy; falling through to the "
        "Slack/dashboard path rather than reporting a deny the operator never made",
        channel,
        request_id,
    )
    return None


def clear_channel_delivery_hooks() -> None:
    """Drop every registered hook. For test isolation and a full gateway teardown."""
    _HOOKS.clear()
