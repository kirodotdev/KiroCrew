"""A completion parked behind its parent's live turn stays owned until the park ends.

A result ``spawn_sub_agents`` returned inline is not delivered until the turn that
received it lands, so the gateway parks its completion (``_park_inline``) until
that turn's release settles it (``_settle_inline`` -> ``_finish_parked``). While
it is parked, the manager's completion ledger counts it, so an agent's goal stop
issued from that same turn is refused as a terminal child report delivery. The
park's count ends on every path the park ends on: its route takes the
completion (a tab route then holds its own), settles it, or drops it for a
retired parent, the tab closed meanwhile, the turn died, or the routing was
cancelled. The next turn's stop is applied.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.chat_utils_completion import _attached_verdict
from kiro_crew.parent_turns import ParentTurnLedger
from kiro_crew.subagent import SubagentInfo, SubagentManager

KEY = "telegram:chat-1"
CHILD = "inline-child"
_REFUSED = "terminal child report delivery"
_HANG_CEILING_S = 30


def _stop_verdict(manager: SubagentManager) -> str | None:
    return _attached_verdict(manager, [], 0, False, None, KEY, "autonudge_stop")


def _setup() -> tuple:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    manager = SubagentManager(sessions=MagicMock(), ctx_builder=None)  # type: ignore[arg-type]
    manager.settle_queued_delivery = AsyncMock()  # type: ignore[method-assign]
    ledger = ParentTurnLedger()
    sessions = MagicMock()
    sessions.parent_turns = ledger
    sessions.get_thread = MagicMock(return_value=None)
    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = sessions
    gateway.subagent_mgr = manager
    gateway.dashboard_state = None
    gateway._cron_injecting = {}
    gateway._inline_parked = {}
    gateway._inline_tasks = set()
    ledger.on_released = gateway._settle_inline
    info = SubagentInfo(id=CHILD, task="t", parent_session_key=KEY)
    info.done = True
    info.result = "the child's report"
    token = ledger.open_attempt(KEY, object())
    ledger.hold_marks(token, [CHILD])
    info._collected_inline = True
    info._inline_state = "pending"
    info._inline_token = token
    manager.get = MagicMock(side_effect={CHILD: info}.get)  # type: ignore[method-assign]
    return gateway, manager, ledger, info, token


def _park(gateway, info, token, route: str) -> None:
    gateway._park_inline(
        token,
        info,
        route=route,
        announce="[Subagent completion event] the child finished",
        body="the child's report",
        title="inline-child finished",
        carried=(CHILD,),
        parent_key=KEY,
    )


async def _settled(gateway) -> None:
    tasks = tuple(gateway._inline_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), _HANG_CEILING_S)


@pytest.mark.asyncio
async def test_a_stop_in_the_turn_that_received_the_result_inline_waits_for_its_release():
    """The channel route parks the completion; the same turn's stop is refused.

    The turn lands and releases, the parked completion is acknowledged with no
    second turn, and a stop on the next turn is applied.
    """
    gateway, manager, ledger, info, token = _setup()

    await gateway._inject_subagent_into_channel_parent(
        info,
        announce="[Subagent completion event] the child finished",
        body="the child's report",
        title="inline-child finished",
        carried=(CHILD,),
        parent_key=KEY,
    )

    assert gateway._inline_parked, "the completion did not park under the live turn"
    assert _stop_verdict(manager) == _REFUSED

    ledger.note_landed(token)
    ledger.settle(token)
    await _settled(gateway)

    assert gateway._inline_parked == {}
    assert info._inline_state == "confirmed"
    assert _stop_verdict(manager) is None
    assert manager._retained_completion_owners == {}


def _routes(gateway, manager, end: str) -> None:
    """Wire the route the parked completion takes at release, per *end*."""
    gateway._inject_subagent_into_channel_parent = AsyncMock()
    gateway._finish_subagent_cron_or_notify = AsyncMock()
    if end == "tab route takes it":

        async def _tab_takes_it(completion, *, counted):
            # As ``_route_subagent_to_tab`` does once it accepts the completion.
            manager.retain_completion_delivery(KEY, completion.info.id)

        gateway._route_parked_in_tab = _tab_takes_it
    elif end == "tab closed":
        gateway._parent_tab = AsyncMock(return_value=("", None, f"evt:{KEY}"))
    elif end == "retired parent":
        manager.suppress_retired_delivery = MagicMock(return_value=True)  # type: ignore[method-assign]
    elif end == "routing cancelled":
        gateway._inject_subagent_into_channel_parent = AsyncMock(
            side_effect=lambda *_a, **_k: asyncio.Event().wait()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end",
    [
        "tab route takes it",
        "route settles it",
        "retired parent",
        "tab closed",
        "turn died",
        "routing cancelled",
    ],
)
async def test_the_park_releases_its_count_on_every_end(end):
    gateway, manager, ledger, info, token = _setup()
    route = "tab" if end in ("tab route takes it", "tab closed") else "channel"
    _park(gateway, info, token, route)
    _routes(gateway, manager, end)

    # Owned while parked.
    assert manager.terminal_delivery_inflight_for(KEY) is True
    assert _stop_verdict(manager) == _REFUSED

    if end != "turn died":
        ledger.note_landed(token)
    ledger.settle(token)
    if end == "routing cancelled":
        (routing,) = gateway._inline_tasks
        await asyncio.sleep(0)
        routing.cancel()
    await _settled(gateway)

    assert gateway._inline_parked == {}
    if end == "tab route takes it":
        # The tab's own count is all that is left; it ends with that delivery.
        assert manager._retained_completion_owners == {KEY: {CHILD: 1}}
        manager.release_completion_delivery(KEY, CHILD)
    assert manager.terminal_delivery_inflight_for(KEY) is False
    assert _stop_verdict(manager) is None
    if end == "tab closed":
        gateway._inject_subagent_into_channel_parent.assert_awaited_once()
