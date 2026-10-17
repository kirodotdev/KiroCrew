"""The agent surface of a code channel: what the bound agent and its people can do.

Three parts, each in its own module, wired here:

* :mod:`.tools` -- the MCP tools the bound session calls (views, plan canvas,
  context bar, rename, status, wrap-up), every one PINNED to the calling
  session's own channel: the channel is resolved from the caller's verified
  session identity, never from an argument.
* :mod:`.commands` -- the channel's runtime slash commands (``/summarize``,
  ``/wrap-up``, and ``/create-pr`` when the session can open a pull request),
  registered with ``agents.conversations.setCommands`` and delivered over
  Socket Mode.
* :mod:`.wrapup` -- the wrap-up summary with its Archive button, and the
  archive that follows a click.

Everything here is inert while the code-channel gate
(:func:`.binding.code_channels_on`) is shut: the hooks fire only for a bound
channel with the feature on, the slash and button handlers answer "not mine",
and the tools refuse.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.sel import sel
from kiro_crew.slack.code_channels.surface_client import SurfaceClient
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)

_slack_provider: Callable[[], Any] | None = None
_INSTALLED = False


def install(slack_provider: Callable[[], Any]) -> None:
    """Subscribe the command and wrap-up hooks (idempotent; called by the core)."""
    global _INSTALLED, _slack_provider
    _slack_provider = slack_provider
    if _INSTALLED:
        return
    from kiro_crew.slack.code_channels import commands, hooks, wrapup

    hooks.on_turn_start(commands.on_turn_start)
    # First: a /wrap-up arriving while a turn starts must see that turn as earlier.
    hooks.on_turn_start(wrapup.on_turn_start, first=True)
    hooks.on_turn_end(wrapup.on_turn_end)
    _INSTALLED = True


def set_slack_provider(provider: Callable[[], Any] | None) -> None:
    """Where the live Slack client comes from (set by :func:`install`; tests)."""
    global _slack_provider
    _slack_provider = provider


def slack() -> Any | None:
    """The live Slack client, or None before Slack connects."""
    try:
        return _slack_provider() if _slack_provider is not None else None
    except Exception:
        return None


def client() -> SurfaceClient | None:
    """The surface's ``agents.*`` client over the live Slack client."""
    live = slack()
    found = SurfaceClient.from_slack(live) if live is not None else None
    return found if isinstance(found, SurfaceClient) else None


def dashboard_state() -> Any | None:
    from kiro_crew.slack.handler_runtime.access import get_dashboard_state

    return get_dashboard_state()


def audit(caller: str, operation: str, outcome: str, resources: str, error: str = "") -> None:
    """One SEL row per decision; a failed write never changes the decision."""
    try:
        sel().log_api_access(
            caller=caller or "unknown",
            operation=operation,
            outcome=outcome,
            source="slack",
            resources=resources,
            error=error,
        )
    except Exception:
        logger.debug("code channel surface SEL write failed", exc_info=True)


def caller_slot_key(state: Any, session_key: str) -> str:
    """The dashboard slot the verified *session_key* belongs to ("" when none)."""
    if state is None or not session_key:
        return ""
    from kiro_crew.dashboard.session_control_parts.caller_access import caller_slot_key as _resolve

    try:
        return _resolve(state, session_key)
    except Exception:
        logger.debug("caller slot resolution failed", exc_info=True)
        return ""


async def binding_for_slot(state: Any, slot_key: str) -> Binding | None:
    """The code channel bound to *slot_key*, when that binding still names it.

    A binding is the caller's only when its session key IS the slot AND the
    channel's Slack link still resolves to that slot: a channel whose session was
    closed and replaced is bound to the replacement, never to the old slot.
    """
    if not slot_key:
        return None
    from kiro_crew.slack.code_channels.binding import bound_slot

    for binding in await asyncio.to_thread(store().bindings):
        if binding.session_key != slot_key:
            continue
        slot = bound_slot(state, binding)
        if slot is None or getattr(slot, "key", "") != slot_key:
            continue
        linked = getattr(slot, "_slack_channel", None)
        if isinstance(linked, str) and linked and linked != binding.channel_id:
            continue  # the anchor ts belongs to some other channel's link
        return binding
    return None


#: Channels this process archived, fenced before (and regardless of) the stamp.
_archived_here: set[str] = set()


def fence_archived(channel_id: str) -> None:
    """Treat *channel_id* as archived for the rest of this process."""
    _archived_here.add(channel_id)


def unfence(channel_id: str) -> None:
    """The stamp is written: the stored binding is the answer again (an unarchive clears it)."""
    _archived_here.discard(channel_id)


async def is_archived(binding: Binding) -> bool:
    """Whether the channel is archived: fenced here, or stamped on the stored binding."""
    if binding.channel_id in _archived_here:
        return True
    current = await asyncio.to_thread(store().get_binding, binding.channel_id)
    return bool((current or binding).archived_at)


def workspace_admitted(team_id: str) -> bool:
    """The per-message workspace gate every inbound Slack path applies (fail closed)."""
    if not team_id:
        return False
    try:
        from kiro_crew.platform import current_context

        return bool(current_context().slack_gate.check_message_origin(team_id))
    except Exception:
        logger.debug("workspace origin check failed", exc_info=True)
        return False


async def outbound_permitted() -> bool:
    """Whether the ``channels`` policy lets this gateway write to Slack."""
    from kiro_crew.messaging.identity import channel_outbound_permitted

    return await channel_outbound_permitted("slack")


def channel_stopped(channel_id: str) -> bool:
    """Whether Slack's Stop silenced a turn of *channel_id* that is still unwinding.

    Slack's rule after ``agent_session_stopped``: no further posts and no
    ``agents.*`` calls for that context.
    """
    try:
        from kiro_crew.slack.agent_sessions import fence

        live = getattr(fence, "_LIVE", ())
        return any(s.channel == channel_id and s.stopped for s in list(live))
    except Exception:
        return False


def push_slots(state: Any) -> None:
    push = getattr(state, "push_slots_update", None)
    if callable(push):
        try:
            push()
        except Exception:
            logger.debug("slots push failed", exc_info=True)
