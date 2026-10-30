"""The channel's native status, driven by the session's turns.

``processing`` shows Slack's "Working..." loader (and its Stop button, which
reaches us as ``agent_session_stopped``); ``active`` means ready for the next
prompt. Every write goes through the agent-sessions driver
(:class:`~kiro_crew.slack.agent_sessions.status.AgentSessionStatus`), the one
``agents.sessions.setStatus`` writer, addressing the whole channel with
``thread_ts=None``. It sends nothing for a channel this process never put into
``processing``, and it takes Slack's refusals to the shared capability latch.

The driver used here is its own instance, gated on code channels instead of
on thread agent sessions: an install can run code channels with thread agent
sessions switched off, and the channel must still show its status. A code
channel's turn runs outside a Slack turn scope, so the driver arms no keepalive
for it and Slack's own one-hour timeout bounds a ``processing`` it was sent.

Installed as ordinary :mod:`.hooks` subscribers by :func:`install`. A status
write that fails is logged by the driver and ignored: the status is decoration
on a turn that runs either way.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.slack.agent_sessions.status import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_PROCESSING,
    AgentSessionStatus,
)
from kiro_crew.slack.code_channels import hooks
from kiro_crew.slack.code_channels.store import Binding

logger = logging.getLogger(__name__)

#: Where the driver is attached on the live Slack client.
_ATTR = "code_channel_status"

_slack_provider: Callable[[], Any] | None = None
#: A status the agent chose for after its turn (``suspended``: it asked a question
#: and waits on the people in the channel); the turn end shows it instead of
#: ``active``. Cleared when the next turn starts.
_held: dict[str, str] = {}


def hold_after_turn(channel_id: str, status: str | None) -> None:
    """Show *status* when the current turn ends (``None`` = the usual ``active``)."""
    if status:
        _held[channel_id] = status
    else:
        _held.pop(channel_id, None)


def _gate() -> bool:
    from kiro_crew.slack.code_channels.binding import code_channels_on, live_config

    return code_channels_on(live_config())


def driver_for(slack: Any) -> AgentSessionStatus | None:
    """The code-channel status driver on *slack*, created on first use."""
    if slack is None or not hasattr(slack, "_web"):
        return None
    driver = getattr(slack, _ATTR, None)
    if not isinstance(driver, AgentSessionStatus):
        driver = AgentSessionStatus(slack, gate=_gate)
        setattr(slack, _ATTR, driver)
    return driver


def set_slack_provider(provider: Callable[[], Any] | None) -> None:
    """Where status writes find the live Slack client (set by :func:`install`, tests)."""
    global _slack_provider
    _slack_provider = provider


def _driver() -> AgentSessionStatus | None:
    return driver_for(_slack_provider()) if _slack_provider is not None else None


async def set_status(channel_id: str, status: str) -> bool:
    """Move the channel's session to *status*; False when nothing was sent."""
    driver = _driver()
    if driver is None:
        return False
    return await driver.set_status(channel_id, None, status)


async def settle_after_stop(channel_id: str) -> None:
    """Move a stopped channel to ``active`` (Slack never clears it after Stop)."""
    driver = _driver()
    if driver is not None:
        await driver.settle_after_stop(channel_id, None)


async def _on_start(binding: Binding, _event: hooks.TurnEvent) -> None:
    _held.pop(binding.channel_id, None)
    await set_status(binding.channel_id, SESSION_STATUS_PROCESSING)


async def _on_end(binding: Binding, _event: hooks.TurnEvent) -> None:
    await set_status(binding.channel_id, _held.pop(binding.channel_id, SESSION_STATUS_ACTIVE))


def install(slack_provider: Callable[[], Any]) -> None:
    """Subscribe the status hooks; *slack_provider* returns the live Slack client."""
    set_slack_provider(slack_provider)
    hooks.on_turn_start(_on_start)
    hooks.on_turn_end(_on_end)
