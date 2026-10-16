"""The delivery acknowledgement (``_report_delivered``) on inline-collected results.

An owed memory-wait expiry report is cleared only on a route's positive
acknowledgement. A result a blocking ``spawn_sub_agents`` call already returned
inline was delivered by that return, so consuming it acknowledges. A result the
call is still collecting is HELD: it has not reached the parent, so the hold
acknowledges nothing, and the report stays owed until the held result is
delivered. These tests drive the gateway's completion route and the registry's
release through the same seams as ``test_spawn_sub_agents_inline_collection``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_spawn_sub_agents_inline_collection import (
    CRON_PARENT,
    SLACK_PARENT,
    _assert_parent_untouched,
    _drain,
    _gateway,
    _info,
    _mark,
    _stream,
)

from kiro_crew.subagent_inline_collection import InlineCollections


def _owed_batches(mgr: Any) -> list[list[tuple[str, bool]]]:
    """Each settle the registry handed the manager, as ``(agent_id, report_owed)``."""
    return [
        [(d.agent_id, d.report_owed) for d in call.args[0]]
        for call in mgr.settle_queued_delivery.await_args_list
    ]


async def _release_and_drain(orch: Any, mgr: Any, registry: InlineCollections, parent: str) -> list:
    """End the call without returning ``a1`` and wait for the registry's delivery."""
    await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": parent})
    tasks = list(registry._tasks)
    await _drain(orch)
    return [task.result() for task in tasks]


class TestConsumingAReturnedResultAcknowledgesIt:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent", [CRON_PARENT, SLACK_PARENT])
    async def test_a_consumed_member_is_marked_delivered(self, parent: str) -> None:
        """The call returned ``a1`` before its completion arrived: the
        completion is consumed, never injected, and its record says the report
        reached the parent, so an owed report is cleared on it."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(parent, "a1", 600, call="c1")
        await _mark(orch, mgr, {"ids": ["a1"], "released": ["a1"], "parent_session": parent})
        info = _info("a1", parent)
        info._report_owed = True
        with _stream() as stream:
            await on_done(info)
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        assert info._report_delivered is True
        assert info._delivery_queued is False


class TestAHeldResultIsNotAcknowledgedUntilItIsDelivered:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent", [CRON_PARENT, SLACK_PARENT])
    async def test_a_held_member_is_not_marked_delivered(self, parent: str) -> None:
        """Still being collected: held, undelivered, and not acknowledged, so
        the terminal report's done-callback leaves an owed report owed."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(parent, "a1", 600, call="c1")
        info = _info("a1", parent)
        info._report_owed = True
        with _stream() as stream:
            await on_done(info)
            _assert_parent_untouched(orch, stream)
        assert info._delivery_queued is True
        assert info._report_delivered is False
        mgr.settle_queued_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_held_member_the_call_returns_settles_its_owed_report(self) -> None:
        """The call's written response named the held member: that response is
        the delivery, and its owed report is handed to the settle that clears it."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600, call="c1")
        info = _info("a1", CRON_PARENT)
        info.error = "memory wait expired"  # a memory-wait expiry: no folder, an owed report
        info._report_owed = True
        with _stream() as stream:
            await on_done(info)
            await _mark(
                orch, mgr, {"ids": ["a1"], "released": ["a1"], "parent_session": CRON_PARENT}
            )
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        assert _owed_batches(mgr) == [[("a1", True)]]

    @pytest.mark.asyncio
    async def test_a_held_member_delivered_by_the_route_is_acknowledged_then_cleared(
        self,
    ) -> None:
        """The call ended without returning it: the result takes the ordinary
        cron route on its ticket, the injection lands and acknowledges it, and
        only then is the owed report handed to the settle."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        tickets: list[Any] = []
        route = mgr._on_done

        async def _recording_route(ticket: Any) -> Any:
            tickets.append(ticket)
            return await route(ticket)

        mgr._on_done = _recording_route
        registry.reserve(CRON_PARENT, "a1", 600, call="c1")
        info = _info("a1", CRON_PARENT)
        info.error = "memory wait expired"  # a memory-wait expiry: no folder, an owed report
        info._report_owed = True
        with _stream() as stream:
            await on_done(info)
            assert info._report_delivered is False
            outcomes = await _release_and_drain(orch, mgr, registry, CRON_PARENT)
            stream.assert_awaited()
        (ticket,) = tickets
        assert ticket is not info
        assert ticket._report_delivered is True
        assert outcomes == ["delivered"]
        assert _owed_batches(mgr) == [[("a1", True)]]

    @pytest.mark.asyncio
    async def test_a_route_that_returns_without_acknowledging_leaves_the_report_owed(
        self,
    ) -> None:
        """A route that returns with no failure flag and no acknowledgement (a
        route added later that does not say it delivered) is not a delivery:
        the owed report is not cleared, so the next start replays it."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)

        async def _silent_route(_ticket: Any) -> None:
            await asyncio.sleep(0)

        registry.reserve(CRON_PARENT, "a1", 600, call="c1")
        info = _info("a1", CRON_PARENT)
        info.error = "memory wait expired"  # a memory-wait expiry: no folder, an owed report
        info._report_owed = True
        with _stream():
            await on_done(info)
            mgr._on_done = _silent_route
            outcomes = await _release_and_drain(orch, mgr, registry, CRON_PARENT)
        assert outcomes == ["undelivered"]
        mgr.settle_queued_delivery.assert_not_awaited()


def test_a_redelivery_ticket_starts_with_no_route_flag_of_its_own():
    """The ticket's flags decide that one delivery, so none is inherited from
    the live record, the acknowledgement included."""
    from kiro_crew.subagent_inline_collection import _ticket

    info = _info("a1", CRON_PARENT)
    info._report_delivered = True
    info._delivery_queued = True
    info._report_undelivered = True
    ticket = _ticket(info)
    assert (ticket._report_delivered, ticket._delivery_queued, ticket._report_undelivered) == (
        False,
        False,
        False,
    )
    assert (info._report_delivered, info._delivery_queued, info._report_undelivered) == (
        True,
        True,
        True,
    )
