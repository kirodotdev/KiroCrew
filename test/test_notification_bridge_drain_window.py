"""A shutdown drain must also wait for handoffs that have no task yet.

An off-loop producer does not create the fanout task itself: it asks the gateway loop to,
with ``call_soon_threadsafe``, and ``_spawn`` puts the task in ``_tasks`` only once that
callback runs on the loop thread. Between the two there is work the bridge owes and
``_tasks`` cannot show it, so a drain reading tasks alone returned immediately and the
transports closed under a note that had been accepted -- the note the user routed to chat
precisely because they were not watching the dashboard.

A persist-gated delivery has the same shape one step earlier: ``schedule`` is not called
until the note's durable write lands, so between arming that callback and the write
landing there is again work owed and no task to show it. Both producers therefore reserve
against the same counter, and both pin the same ordering -- the task is in ``_tasks``
before the reservation retires.

These pins live in their own file rather than beside the other bridge tests because
``test/test_notification_bridge.py`` is write-protected by policy in this environment and
was not reachable for an edit.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest import mock

from kiro_crew.notifications.bridge import BridgeDispatcher


class _RecordingSink:
    """Records what it was handed, mirroring the sink protocol the bridge expects."""

    def __init__(self, transport_id: str = "slack") -> None:
        self.transport_id = transport_id
        self.sent: list[str] = []

    async def send(self, text: str) -> str:
        self.sent.append(text)
        return "ts-1"


def _permit(*_args: Any, **_kwargs: Any) -> Any:
    return mock.Mock(permitted=True, rule="", layer="", reason="")


class DrainCoversQueuedHandoffsTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_drain_in_the_handoff_window_still_waits_for_the_note(self) -> None:
        """A drain between the enqueue and the callback must still wait.

        The window is entered deliberately rather than raced for: ``call_soon_threadsafe``
        queues its callback even when called ON the loop thread, and nothing is awaited
        between that call and the drain, so ``_tasks`` is provably empty while a note is
        owed. Both preconditions are asserted, because a test that yielded first would
        close the window and then pass against the very defect it names.
        """
        loop = asyncio.get_running_loop()
        sink = _RecordingSink()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: sink,
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: loop,
        )
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            d._hand_off_to_gateway_loop(
                {"channel": "system.agent", "priority": "critical", "title": "Shutdown note"}
            )
            self.assertEqual(d._tasks, set(), "the window requires no task to exist yet")
            self.assertEqual(d._owed, 1, "the handoff must be registered")
            await d.drain(timeout=5)
        self.assertEqual(len(sink.sent), 1)
        self.assertIn("Shutdown note", sink.sent[0])
        self.assertEqual(d._owed, 0)

    async def test_the_task_is_registered_before_the_handoff_is_cleared(self) -> None:
        """Order, not bookkeeping: clearing the count first would reopen the same window
        one instant later, and a drain reading between the two would again see nothing."""
        loop = asyncio.get_running_loop()
        sink = _RecordingSink()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: sink,
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: loop,
        )
        seen: list[tuple[int, int]] = []
        real_spawn = d._spawn

        def _watch(loop_arg: Any, note: Any) -> Any:
            task = real_spawn(loop_arg, note)
            seen.append((len(d._tasks), d._owed))
            return task

        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
            mock.patch.object(d, "_spawn", _watch),
        ):
            d._hand_off_to_gateway_loop({"channel": "system.agent", "priority": "critical"})
            await d.drain(timeout=5)

        # Sampled inside _spawn_handed_off, after _spawn returned and before the count
        # dropped: the task is already visible while the handoff is still counted, so no
        # instant exists at which a drain sees neither.
        self.assertEqual(seen, [(1, 1)])

    async def test_a_closed_loop_leaves_no_handoff_owed(self) -> None:
        """The registration is undone when the callback can never run.

        Otherwise the count never returns to zero and every later drain waits out its
        whole timeout on work that does not exist -- turning a lost note into a stalled
        shutdown, which is worse than what it replaced.
        """
        dead = asyncio.new_event_loop()
        dead.close()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: _RecordingSink(),
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: dead,
        )
        d._hand_off_to_gateway_loop({"channel": "system.agent", "priority": "critical"})
        self.assertEqual(d._owed, 0)
        await d.drain(timeout=5)

    async def test_a_drain_with_nothing_owed_returns_without_waiting(self) -> None:
        """The complement: the wait must not have become unconditional."""
        loop = asyncio.get_running_loop()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: _RecordingSink(),
            settings_reader=lambda _c: {},
            loop_provider=lambda: loop,
        )
        started = loop.time()
        await d.drain(timeout=5)
        self.assertLess(loop.time() - started, 1.0)


class DrainCoversPersistGatedDeliveriesTests(unittest.IsolatedAsyncioTestCase):
    """The second producer of owed-but-taskless work, found by review after the first.

    ``_schedule_bridge_after_persist`` does not call ``schedule``: it arms a done-callback
    on the delivery's persist future. Between arming and the write landing the delivery is
    in no task and no handoff, so a shutdown drain returned immediately and the transports
    closed on a note whose DM leg had not been sent. The dashboard copy survives; the DM
    the user routed away from the dashboard does not, and nothing replays it.
    """

    def _state(self, bridge: Any) -> Any:
        from kiro_crew.dashboard.state import DashboardState

        state = mock.Mock(spec=DashboardState)
        state.notification_bridge = bridge
        state._schedule_bridge_after_persist = (
            DashboardState._schedule_bridge_after_persist.__get__(state, DashboardState)
        )
        state._bridge_after_persist = DashboardState._bridge_after_persist
        return state

    async def test_a_drain_before_the_persist_lands_still_waits_for_the_note(self) -> None:
        """The window is entered deliberately: the future is left UNRESOLVED, so the
        callback provably has not run and ``_tasks`` is provably empty while the delivery
        is owed. Both are asserted, so the test cannot pass by having missed the window.
        """
        loop = asyncio.get_running_loop()
        sink = _RecordingSink()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: sink,
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: loop,
        )
        state = self._state(d)
        durability: asyncio.Future[bool] = loop.create_future()
        note = {"channel": "system.agent", "priority": "critical", "title": "Persisting note"}

        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
        ):
            state._schedule_bridge_after_persist(note, durability)
            self.assertEqual(d._tasks, set(), "the window requires no task to exist yet")
            self.assertEqual(d._owed, 1, "the persist-gated delivery must be reserved")

            # The write lands while the drain is already waiting, which is the real
            # shutdown shape: the drain must still be there to see the task appear.
            loop.call_soon(durability.set_result, True)
            await d.drain(timeout=5)

        self.assertEqual(len(sink.sent), 1)
        self.assertIn("Persisting note", sink.sent[0])
        self.assertEqual(d._owed, 0)

    async def test_a_withheld_leg_leaves_nothing_owed(self) -> None:
        """A failed persist withholds the DM on purpose, and that is a DECISION, not an
        outstanding obligation: the reservation must retire anyway, or every later drain
        waits out its whole timeout on a delivery that will never be sent."""
        loop = asyncio.get_running_loop()
        sink = _RecordingSink()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: sink,
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: loop,
        )
        state = self._state(d)
        durability: asyncio.Future[bool] = loop.create_future()
        state._schedule_bridge_after_persist({"channel": "system.agent"}, durability)
        self.assertEqual(d._owed, 1)

        durability.set_result(False)
        await asyncio.sleep(0)
        self.assertEqual(d._owed, 0, "a withheld leg must not leave a reservation behind")
        self.assertEqual(sink.sent, [], "a failed persist must not deliver the DM")

        started = loop.time()
        await d.drain(timeout=5)
        self.assertLess(loop.time() - started, 1.0, "drain must not wait on a retired leg")

    async def test_the_task_is_registered_before_the_reservation_is_retired(self) -> None:
        """Same ordering the handoff path pins, at the other producer: retiring first
        would reopen the window one instant later instead of closing it.

        The note carries a ``critical`` priority because ``schedule`` drops a note that
        does not clear the route's floor before any task exists -- with a priority absent
        this sampled ``(0, 1)`` and would have read as a broken ordering rather than as an
        undelivered note, so the sink is asserted too.
        """
        loop = asyncio.get_running_loop()
        sink = _RecordingSink()
        d = BridgeDispatcher(
            sink_resolver=lambda _t: sink,
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            loop_provider=lambda: loop,
        )
        state = self._state(d)
        seen: list[tuple[int, int]] = []
        real_release = d.release

        def _watch() -> None:
            seen.append((len(d._tasks), d._owed))
            real_release()

        durability: asyncio.Future[bool] = loop.create_future()
        note = {"channel": "system.agent", "priority": "critical", "title": "Ordered note"}
        with (
            mock.patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=_permit),
            mock.patch("kiro_crew.sel.sel", return_value=mock.Mock()),
            mock.patch.object(d, "release", _watch),
        ):
            state._schedule_bridge_after_persist(note, durability)
            durability.set_result(True)
            await d.drain(timeout=5)

        self.assertEqual(len(sink.sent), 1, "the note must actually have been delivered")
        # Sampled at the release call: the task is already in _tasks while the delivery is
        # still counted, so no instant exists at which a drain sees neither.
        self.assertEqual(seen, [(1, 1)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
