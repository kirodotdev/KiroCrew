"""Turn hooks: what other code-channel features subscribe to.

A code channel runs in a dashboard session (see :mod:`.binding`). Each turn of
that session -- whether it was typed in Slack or in the dashboard -- fires
:func:`fire_turn_start` before the model runs and :func:`fire_turn_end` after it
settles. Subscribers get the channel's :class:`~.store.Binding` and a
:class:`TurnEvent`; the built-in subscriber sets the channel's native status
("Working..." while processing, ready when active). The workspace feature (diff
view, context bar) subscribes the same way::

    from kiro_crew.slack.code_channels import hooks

    async def on_end(binding, event):  # Binding, TurnEvent
        ...

    hooks.on_turn_end(on_end)

Contract:

* Hooks are awaited in registration order, each under a short timeout, and
  every exception is caught and logged: a subscriber can never fail, stall or
  reorder a turn.
* Hooks fire only for a BOUND channel while ``slack.code_channels`` is on.
  With the toggle off they never fire.
* ``fire_turn_end`` fires once per ``fire_turn_start``, on every exit path
  (success, error, Stop), with ``outcome`` naming which.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kiro_crew.slack.code_channels.store import Binding

logger = logging.getLogger(__name__)

HOOK_TIMEOUT_SECS = 10.0

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_STOPPED = "stopped"


@dataclass(frozen=True)
class TurnEvent:
    """One turn of a code channel's session.

    ``source`` is ``"slack"`` for a turn a Slack message started and
    ``"dashboard"`` for one started in the dashboard. ``outcome`` is empty on
    start and one of ``ok`` / ``error`` / ``stopped`` on end.
    """

    channel_id: str
    session_key: str
    source: str = "dashboard"
    outcome: str = ""


Hook = Callable[[Binding, TurnEvent], Awaitable[None]]

_START_HOOKS: list[Hook] = []
_END_HOOKS: list[Hook] = []


def on_turn_start(hook: Hook, *, first: bool = False) -> Hook:
    """Subscribe *hook* to turn starts (idempotent). Usable as a decorator.

    *first* runs it before every hook already registered, for a hook that must
    observe the start before any other one awaits.
    """
    if hook not in _START_HOOKS:
        if first:
            _START_HOOKS.insert(0, hook)
        else:
            _START_HOOKS.append(hook)
    return hook


def on_turn_end(hook: Hook) -> Hook:
    """Subscribe *hook* to turn ends (idempotent). Usable as a decorator."""
    if hook not in _END_HOOKS:
        _END_HOOKS.append(hook)
    return hook


def remove_hook(hook: Hook) -> None:
    """Unsubscribe *hook* from both lists (tests, reloads)."""
    for hooks in (_START_HOOKS, _END_HOOKS):
        while hook in hooks:
            hooks.remove(hook)


async def _run(hooks: list[Hook], binding: Binding, event: TurnEvent) -> None:
    for hook in list(hooks):
        try:
            await asyncio.wait_for(hook(binding, event), timeout=HOOK_TIMEOUT_SECS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "code channel hook %s failed for %s",
                getattr(hook, "__qualname__", hook),
                binding.channel_id,
                exc_info=True,
            )


async def fire_turn_start(binding: Binding, event: TurnEvent) -> None:
    await _run(_START_HOOKS, binding, event)


async def fire_turn_end(binding: Binding, event: TurnEvent) -> None:
    await _run(_END_HOOKS, binding, event)
