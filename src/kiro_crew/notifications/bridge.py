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

Transports plug in as :class:`BridgeSink` objects (``bridge_slack.py`` is the
first); a routed transport with no registered sink is skipped. Each delivery is
governance-vetted, rate-limited per transport, redacted, and SEL-audited by the
dispatcher itself (``bridge_egress.py``), so a sink only renders and sends.

Loop safety: nothing here publishes to the notification bus. The dispatcher
holds no reference to it and only ever calls its sinks, so a bridged delivery
can never become a new note (``test_notification_bridge.py`` pins it).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from kiro_crew.notifications.bridge_egress import audit_delivery, redact_note, vet_delivery
from kiro_crew.notifications.rate_limit import AppRateLimiter

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


class BridgeTransportUnavailable(Exception):
    """A sink's transport is not connected; the delivery is skipped and audited."""


class BridgeSink(Protocol):
    """One chat transport's delivery of a bridged note to the owner."""

    #: The transport id this sink serves, one of :data:`BRIDGE_TRANSPORTS`.
    transport: str

    async def deliver(self, note: Mapping[str, Any]) -> None:
        """Deliver an already vetted and redacted *note*.

        Raise :class:`BridgeTransportUnavailable` when the transport is not
        connected. Any other raise is audited as an error and logged; it never
        reaches the bus or the dashboard.
        """
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
    bridge: Callable[[dict[str, Any], str], None],
) -> Callable[[dict[str, Any]], None]:
    """The bus sink: *local* first and synchronously, then *bridge*.

    *bridge* also gets the note's ``publisher_session``, read before *local* runs:
    the local sink pops that delivery-only field, and the bridge vets and
    attributes each delivery on it. A local failure propagates as it always has, and the bridge is not reached.
    A bridge failure is logged and swallowed, so it can never fail the producer's
    ``push`` or the local delivery that already happened.
    """

    def _sink(note: dict[str, Any]) -> None:
        publisher = note.get("publisher_session")
        local(note)
        try:
            bridge(note, publisher if isinstance(publisher, str) else "")
        except Exception:
            logger.warning("Notification bridge could not schedule a delivery", exc_info=True)

    return _sink


class BridgeDispatcher:
    """Match delivered notes against channel routes and fan them out to sinks.

    *settings* returns the gateway's ``ChannelSettings`` (a provider, because the
    dispatcher is built before the settings object). Deliveries run as tasks this
    dispatcher holds until they finish, so the loop never drops one mid-flight.
    """

    def __init__(
        self,
        settings: Callable[[], Any],
        subjects: Callable[[Mapping[str, Any], str], tuple[str, ...]] | None = None,
        committed: Callable[[Mapping[str, Any]], Awaitable[bool] | None] | None = None,
    ) -> None:
        self._settings = settings
        # The local sink's still-pending write for a note, or None once it is done:
        # the gateway hands back a keyed replacement's commit, which can fail.
        self._committed = committed or (lambda note: None)
        # Maps a note and its publisher to the session keys governance vets it for.
        # The gateway resolves slot keys to the session their turns run on; with no
        # resolver the publisher is taken as given.
        self._subjects = subjects or (lambda note, publisher: (publisher,) if publisher else ())
        # One token bucket per transport (the app-ingress budget's shape), so a
        # channel routed at the ``all`` floor cannot flood a chat surface.
        self._limiter = AppRateLimiter()
        self._sinks: dict[str, BridgeSink] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def register_sink(self, sink: BridgeSink) -> None:
        """Install *sink* for its transport, replacing any earlier one."""
        if sink.transport not in BRIDGE_TRANSPORTS:
            raise ValueError(f"unknown bridge transport {sink.transport!r}")
        self._sinks[sink.transport] = sink

    @property
    def transports(self) -> tuple[str, ...]:
        """The transports a route can deliver to: those with a registered sink.

        In :data:`BRIDGE_TRANSPORTS` order. A route may still NAME a transport with no
        sink (it is skipped at delivery), so the Settings UI offers only these.
        """
        return tuple(t for t in BRIDGE_TRANSPORTS if t in self._sinks)

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

    def schedule(
        self, note: Mapping[str, Any], *, serving_loop: Any = None, publisher: str = ""
    ) -> None:
        """Hand *note* to its routed sinks without waiting for them.

        *publisher* is the session that published the note, if any: governance is
        vetted for it as well as the host, and the delivery is attributed to it.

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
            self._start(loop, note, publisher)
            return
        if serving_loop is None or serving_loop.is_closed() or not serving_loop.is_running():
            # shortcut: with no running loop there is nothing to run a delivery on (a
            # CLI-only producer, a test); such notes are not bridged. Upgrade if a
            # loop-less gateway path ever needs to bridge.
            return
        try:
            serving_loop.call_soon_threadsafe(self._start, serving_loop, note, publisher)
        except RuntimeError:
            return  # The loop closed between the check and the call.

    def _start(
        self, loop: asyncio.AbstractEventLoop, note: Mapping[str, Any], publisher: str = ""
    ) -> None:
        try:
            commit = self._committed(note)
        except Exception:
            logger.warning("Notification bridge could not read a note's commit", exc_info=True)
            return
        if commit is None:
            self._fan_out(loop, note, publisher)
            return
        # The local sink's write is still in flight (a keyed replacement): bridge
        # only what the dashboard actually committed, so a refused update never
        # reaches a chat app and a retried publish never sends a second copy.
        task = loop.create_task(self._fan_out_once_committed(loop, commit, note, publisher))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fan_out_once_committed(
        self,
        loop: asyncio.AbstractEventLoop,
        commit: Awaitable[bool],
        note: Mapping[str, Any],
        publisher: str,
    ) -> None:
        try:
            committed = await commit
        except Exception:
            # A failed write committed nothing; nothing is bridged.
            return
        if committed:
            self._fan_out(loop, note, publisher)

    def _fan_out(
        self, loop: asyncio.AbstractEventLoop, note: Mapping[str, Any], publisher: str
    ) -> None:
        transports = self.routes(note)
        if not transports:
            return
        try:
            subjects = tuple(self._subjects(note, publisher))
        except Exception:
            # Not knowing who a note speaks for must not send it as the host alone.
            logger.warning("Notification bridge could not resolve a note's sessions", exc_info=True)
            return
        for transport in transports:
            sink = self._sinks.get(transport)
            if sink is None:
                logger.debug("No bridge sink for %s; skipping", transport)
                continue
            # One task per transport, so a slow one never holds up the rest, and a
            # private copy each: a sink can neither see later edits to the stored
            # note nor change what the dashboard or another sink holds.
            task = loop.create_task(self._deliver_one(sink, copy.deepcopy(dict(note)), subjects))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _deliver_one(
        self, sink: BridgeSink, note: dict[str, Any], subjects: tuple[str, ...]
    ) -> None:
        transport = sink.transport
        publisher = subjects[0] if subjects else ""
        try:
            denied = await asyncio.to_thread(vet_delivery, transport, note, subjects)
        except Exception:
            denied = "governance evaluation failed; denying (fail-closed)"
        if denied:
            audit_delivery(transport, note, "denied", publisher=publisher, error=denied)
            return
        if not self._limiter.allow(transport):
            audit_delivery(transport, note, "rate_limited", publisher=publisher)
            return
        try:
            await sink.deliver(redact_note(note))
        except BridgeTransportUnavailable as exc:
            # Not connected: an audited no-op. Nothing was sent, so the budget is kept.
            self._limiter.refund(transport)
            audit_delivery(transport, note, "skipped", publisher=publisher, error=str(exc))
            return
        except Exception as exc:
            # A transport's failure never reaches the dashboard or the other sinks. The
            # token is kept: a failed call may still have posted.
            audit_delivery(transport, note, "error", publisher=publisher, error=type(exc).__name__)
            logger.warning("Notification bridge delivery to %s failed", transport, exc_info=True)
            return
        audit_delivery(transport, note, "completed", publisher=publisher)

    async def drain(self) -> None:
        """Wait for every delivery started so far (shutdown and tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
