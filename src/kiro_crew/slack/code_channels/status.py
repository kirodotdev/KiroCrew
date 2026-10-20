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
#: What the bound session's last turn end left showing, until its next turn
#: starts: the workspace's worker status keeps a ``suspended`` conductor suspended.
_after_turn: dict[str, str] = {}


def after_turn(channel_id: str) -> str:
    """What the conductor's last turn end showed (``""`` while a turn runs or none ended)."""
    return _after_turn.get(channel_id, "")


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


#: Channels whose session Slack's own Stop ended: no agents.* call until the next
#: turn starts (code channel lifecycle: Slack updates the status itself).
_quiet: set[str] = set()


def quiet_after_stop(channel_id: str) -> None:
    """Slack's Stop button stopped *channel_id*: write nothing until a new turn."""
    _quiet.add(channel_id)


def is_quiet(channel_id: str) -> bool:
    """Whether Slack's own Stop silenced *channel_id* and no turn has started since."""
    return channel_id in _quiet


async def set_status(channel_id: str, status: str) -> bool:
    """Move the channel's session to *status*; False when nothing was sent."""
    if channel_id in _quiet:
        return False
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
    _after_turn.pop(binding.channel_id, None)
    _quiet.discard(binding.channel_id)
    await set_status(binding.channel_id, SESSION_STATUS_PROCESSING)


async def _on_end(binding: Binding, _event: hooks.TurnEvent) -> None:
    _approvals.pop(binding.session_key, None)
    shown = _held.pop(binding.channel_id, SESSION_STATUS_ACTIVE)
    if len(_after_turn) > 4096:
        _after_turn.clear()
    _after_turn[binding.channel_id] = shown
    await set_status(binding.channel_id, shown)


# ── A tool approval waiting on a person: ``suspended`` ────────────────────

#: Approvals pending per bound session key, by request id: one answered late
#: (after its turn ended) or registered twice cannot skew the count.
_approvals: dict[str, set[str]] = {}
_approval_tasks: set[Any] = set()


def _channel_of(session_key: str) -> Binding | None:
    """The live binding whose session is *session_key* (in-memory read; None if none)."""
    from kiro_crew.slack.code_channels.store import store

    bindings = store().loaded_bindings()
    for binding in bindings or ():
        if binding.session_key == session_key and not binding.archived_at:
            return binding
    return None


def note_approval(session_key: str, *, pending: bool, request_id: str = "") -> None:
    """A tool approval was raised (*pending*) or answered on *session_key*.

    Slack's lifecycle says a session that cannot go on until a person acts --
    a tool approval among them -- is ``suspended``, not ``processing``
    (`agents.sessions.setStatus`_). So the channel shows ``suspended`` while any
    approval waits, and ``processing`` again once the last one is answered and
    the turn is still running. Synchronous and cheap for a session no code
    channel is bound to; never raises.

    .. _agents.sessions.setStatus: https://docs.slack.dev/reference/methods/agents.sessions.setStatus#status-values
    """
    try:
        if not _gate():
            return
        binding = _channel_of(session_key)
        if binding is None:
            return
        waiting = _approvals.setdefault(session_key, set())
        before = len(waiting)
        if pending:
            waiting.add(request_id)
        elif request_id in waiting:
            waiting.discard(request_id)
        else:
            return  # not one this turn raised: an earlier turn's, answered late
        if not waiting:
            _approvals.pop(session_key, None)
        if pending and before == 0:
            status = "suspended"
        elif not pending and not waiting:
            status = SESSION_STATUS_PROCESSING
        else:
            return
        import asyncio

        task = asyncio.ensure_future(_apply_approval_status(binding, status))
        _approval_tasks.add(task)
        task.add_done_callback(_approval_tasks.discard)
    except Exception:
        logger.debug("code channel approval status failed", exc_info=True)


async def _apply_approval_status(binding: Binding, status: str) -> None:
    if status == SESSION_STATUS_PROCESSING and not _turn_running(binding):
        return  # the turn already ended: its end hook set the status
    if _approvals.get(binding.session_key) and status != "suspended":
        return  # another approval was raised meanwhile
    await set_status(binding.channel_id, status)


def _turn_running(binding: Binding) -> bool:
    try:
        from kiro_crew.slack.code_channels.binding import bound_slot
        from kiro_crew.slack.handler import get_dashboard_state

        slot = bound_slot(get_dashboard_state(), binding)
        return bool(slot is not None and getattr(slot, "running", False))
    except Exception:
        return False


def install(slack_provider: Callable[[], Any]) -> None:
    """Subscribe the status hooks; *slack_provider* returns the live Slack client."""
    set_slack_provider(slack_provider)
    hooks.on_turn_start(_on_start)
    hooks.on_turn_end(_on_end)
