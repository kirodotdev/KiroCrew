"""Notification bridge: the bus's egress seam to chat transports.

The bus's single delivery sink becomes a composite (:func:`composite_sink`): the
local sink (log, badge, WebSocket, Web Push, disk) runs first and synchronously,
exactly as before, and only then is the note handed to :class:`BridgeDispatcher`,
which decides which chat transports receive it and schedules their deliveries as
tracked background tasks. The dashboard never waits on a transport.

Routing comes from each source channel's settings
(``notifications.settings.ChannelSettings``): ``deliver_to`` names the transports,
``deliver_min_priority`` the floor a note's EFFECTIVE priority (after the user's
overrides and mutes) must clear. A silenced note never routes, whatever the floor.

Transports plug in as :class:`BridgeSink` objects. This module ships none: a
routed transport with no registered sink is skipped.

Loop safety: nothing here publishes to the notification bus. The dispatcher
holds no reference to it and only ever calls its sinks, so a bridged delivery
can never become a new note (``test_notification_bridge.py`` pins it).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Transport ids ``deliver_to`` may name. Validated against the KNOWN set, not the
#: connected one, so a stored route survives a transport that is down for a while.
BRIDGE_TRANSPORTS: tuple[str, ...] = ("slack", "discord", "telegram", "webex", "wecom")

#: ``deliver_min_priority`` values, tightest first. ``all`` also routes passive notes.
DELIVER_MIN_PRIORITIES: tuple[str, ...] = ("critical", "default", "all")

#: The floor a route gets when ``deliver_to`` is set without one.
DEFAULT_DELIVER_MIN_PRIORITY = "critical"

#: The note priorities each floor lets through.
_FLOOR_ADMITS: dict[str, frozenset[str]] = {
    "critical": frozenset({"critical"}),
    "default": frozenset({"critical", "default"}),
    "all": frozenset({"critical", "default", "passive"}),
}


class BridgeSink(Protocol):
    """One chat transport's delivery of a bridged note to the owner."""

    #: The transport id this sink serves, one of :data:`BRIDGE_TRANSPORTS`.
    transport: str

    async def deliver(self, note: Mapping[str, Any]) -> None:
        """Deliver *note*. Raising is logged; it never reaches the bus or the dashboard."""
        ...


@dataclass(frozen=True)
class DeliveryRule:
    """A channel's bridge route: the transports, and the priority floor."""

    transports: tuple[str, ...] = ()
    min_priority: str = DEFAULT_DELIVER_MIN_PRIORITY

    def admits(self, priority: Any) -> bool:
        """Whether a note of *priority* clears this rule's floor."""
        return priority in _FLOOR_ADMITS.get(self.min_priority, frozenset())


def rule_from_settings(entry: Mapping[str, Any] | None) -> DeliveryRule:
    """The route a stored settings entry describes; no route when it names none.

    Lenient on read, like ``ChannelSettings.apply``: an unknown transport or floor in
    a hand-edited file is ignored rather than raised. The settings writer refuses
    them on the way in.
    """
    if not entry:
        return DeliveryRule()
    raw = entry.get("deliver_to")
    if not isinstance(raw, list):
        return DeliveryRule()
    transports = tuple(dict.fromkeys(t for t in raw if t in BRIDGE_TRANSPORTS))
    floor = entry.get("deliver_min_priority")
    if floor not in DELIVER_MIN_PRIORITIES:
        floor = DEFAULT_DELIVER_MIN_PRIORITY
    return DeliveryRule(transports=transports, min_priority=floor)


def composite_sink(
    local: Callable[[dict[str, Any]], None],
    bridge: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any]], None]:
    """The bus sink: *local* first and synchronously, then *bridge*.

    A local failure propagates as it always has, and the bridge is not reached.
    A bridge failure is logged and swallowed, so it can never fail the producer's
    ``push`` or the local delivery that already happened.
    """

    def _sink(note: dict[str, Any]) -> None:
        local(note)
        try:
            bridge(note)
        except Exception:
            logger.warning("Notification bridge could not schedule a delivery", exc_info=True)

    return _sink


class BridgeDispatcher:
    """Match delivered notes against channel routes and fan them out to sinks.

    *settings* returns the gateway's ``ChannelSettings`` (a provider, because the
    dispatcher is built before the settings object). Deliveries run as tasks this
    dispatcher holds until they finish, so the loop never drops one mid-flight.
    """

    def __init__(self, settings: Callable[[], Any]) -> None:
        self._settings = settings
        self._sinks: dict[str, BridgeSink] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def register_sink(self, sink: BridgeSink) -> None:
        """Install *sink* for its transport, replacing any earlier one."""
        if sink.transport not in BRIDGE_TRANSPORTS:
            raise ValueError(f"unknown bridge transport {sink.transport!r}")
        self._sinks[sink.transport] = sink

    def rule_for(self, channel: Any) -> DeliveryRule:
        """The route stored for *channel*; no route when settings cannot be read."""
        if not isinstance(channel, str) or not channel:
            return DeliveryRule()
        try:
            entry = self._settings().get(channel)
        except Exception:
            logger.warning("Notification bridge could not read channel settings", exc_info=True)
            return DeliveryRule()
        return rule_from_settings(entry)

    def routes(self, note: Mapping[str, Any]) -> tuple[str, ...]:
        """The transports *note* routes to, after the local sink applied the user's settings."""
        if note.get("silenced"):
            return ()
        rule = self.rule_for(note.get("channel"))
        if not rule.transports or not rule.admits(note.get("priority")):
            return ()
        return rule.transports

    def schedule(self, note: Mapping[str, Any], *, serving_loop: Any = None) -> None:
        """Hand *note* to its routed sinks without waiting for them.

        Called right after the local sink. On the event loop the route is matched
        and the deliveries start as a tracked task. On a worker thread the local
        sink queued its own delivery on *serving_loop*; this queues the bridge
        behind it, so the route is matched only after the user's settings were
        applied to the note.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._start(loop, note)
            return
        if serving_loop is None or serving_loop.is_closed() or not serving_loop.is_running():
            # shortcut: with no running loop there is nothing to run a delivery on (a
            # CLI-only producer, a test); such notes are not bridged. Upgrade if a
            # loop-less gateway path ever needs to bridge.
            return
        try:
            serving_loop.call_soon_threadsafe(self._start, serving_loop, note)
        except RuntimeError:
            return  # The loop closed between the check and the call.

    def _start(self, loop: asyncio.AbstractEventLoop, note: Mapping[str, Any]) -> None:
        transports = self.routes(note)
        if not transports:
            return
        for transport in transports:
            sink = self._sinks.get(transport)
            if sink is None:
                logger.debug("No bridge sink for %s; skipping", transport)
                continue
            # One task per transport, so a slow one never holds up the rest, and a
            # private copy each: a sink can neither see later edits to the stored
            # note nor change what the dashboard or another sink holds.
            task = loop.create_task(self._deliver_one(sink, copy.deepcopy(dict(note))))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _deliver_one(sink: BridgeSink, note: dict[str, Any]) -> None:
        try:
            await sink.deliver(note)
        except Exception:
            # A transport's failure never reaches the dashboard or the other sinks.
            logger.warning(
                "Notification bridge delivery to %s failed", sink.transport, exc_info=True
            )

    async def drain(self) -> None:
        """Wait for every delivery started so far (shutdown and tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
