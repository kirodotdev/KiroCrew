"""Channel-side delivery for the host spawn-approval prompt — one seam, every channel.

The single host-wide ``SubagentManager`` owns ONE ``on_spawn_approval`` callback,
built in ``slack/gateway.py``. Before this seam that callback raced only a Slack
owner DM and the dashboard, so a spawn parented on a Telegram (or any other)
conversation reached no surface at all: post-#8914 the gate refuses it fast with
``no_approval_surface`` rather than parking to the reaper's deadline. The ideal
fix named on issue #2381 is to deliver the prompt to the ORIGINATING channel's
own inline keyboard, which each channel already renders for mid-run tool prompts
but which the host spawn gate cannot reach — the driver's per-turn ``decider`` is
wired to the main-agent tool ladder, not to this callback.

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
survived a restart would name a dispatcher that no longer exists. The registry
dies with the process; a channel re-registers on its next startup.

``messaging`` may not import a channel package, so the hook is a plain async
callable the channel supplies — the same inversion ``session_trust`` uses to let
a Slack widget write a grant a Telegram turn can read.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

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


def unregister_channel_delivery(channel: str) -> None:
    """Drop *channel*'s delivery hook (idempotent).

    Called on a channel's shutdown so the gate stops routing to a dispatcher that
    is going away. Absent-key safe: a channel that failed to start never
    registered, and its shutdown path still calls this.
    """
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
    registered for the session's channel or the hook itself returned ``None`` (it
    could not surface the prompt here). A hook that RAISES is contained and read as
    ``None``: a channel-delivery bug must degrade to the existing fallback, never
    turn a spawn the operator could still answer on Slack/dashboard into a hard
    failure.
    """
    hook = resolve_channel_delivery(parent_session_key)
    if hook is None:
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


def clear_channel_delivery_hooks() -> None:
    """Drop every registered hook. For test isolation and a full gateway teardown."""
    _HOOKS.clear()
