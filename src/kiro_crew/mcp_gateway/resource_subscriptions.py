"""MCP resource subscriptions a session keeps after its kiro-cli exits.

``rfc-mcp-resource-subscriptions.md`` §4.1-§4.4, the holder limits of §4.6 and
§4.7. :class:`ResourceSubscriptionService` owns one
:class:`~kiro_crew.mcp_gateway.resource_holder.ResourceHolder` per (session,
server) with at least one subscription, the durable record of every one of them
(:mod:`~kiro_crew.mcp_gateway.resource_subscription_store`), and the two caps:
:data:`MAX_SUBSCRIPTIONS_PER_SESSION` and
``mcp_gateway.resource_subscriptions.max_pinned_backends``.

What a holder observes leaves as a :class:`ResourceEvent` through an
:data:`EventSink`: one ``updated`` per ``notifications/resources/updated``, one
``may_have_changed`` per URI after any gap, and ``paused`` with the reason. A
sink is called from the holder's read loop, so it must return at once; an
:class:`EventStream` is the asynchronous consumer side, and
:class:`RecordingSink` the test double. Turning events into wakes is Phase 3 and
not here.

Everything is behind ``mcp_gateway.resource_subscriptions.enabled``: off, a
subscribe is refused and stored subscriptions are not resumed, but removal on
session close still applies, so a session closed while the switch was off does
not keep a record that comes back when it is turned on.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional, Protocol, TypeVar

from kiro_crew.mcp_gateway.backend_leases import RESOURCE_URI_MAX_LEN
from kiro_crew.mcp_gateway.backend_wire import _log_safe_identifier, _tool_call_error_text
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.resource_holder import (
    HANDSHAKE_TIMEOUT_SECS,
    PAUSED_NOT_SUBSCRIBABLE,
    PING_INTERVAL_SECS,
    RECONNECT_DELAY_SECS,
    HolderNotLive,
    HolderPaused,
    HolderSpec,
    ResourceHolder,
    describe_session,
)
from kiro_crew.mcp_gateway.resource_subscription_store import (
    RETAINED_CONTAINER_MAX_BYTES,
    STORE_DIR_LEAF,
    STORE_LEAF,
    HolderRecord,
    ResourceSubscriptionStore,
    StoreUnreadable,
    SubscriptionMeta,
    serialized_container_size,
)

logger = logging.getLogger(__name__)

#: §4.6: subscriptions one session may hold, across all its servers. A URI
#: whose re-subscribe was refused still counts: it is the session's until removed.
MAX_SUBSCRIPTIONS_PER_SESSION = 20

#: Sessions whose records could not be removed from the store when they closed
#: (§4.7). Process-wide, so a broker restart's new service sees them: a start
#: removes them again before restoring anything, and restores none of their
#: records while that still fails.
_UNRETIRED: set[str] = set()

EVENT_UPDATED = "updated"
EVENT_MAY_HAVE_CHANGED = "may_have_changed"
EVENT_PAUSED = "paused"
EVENT_REMOVED = "removed"
EVENT_STREAM_OVERFLOW = "stream_overflow"

#: How many events an :class:`EventStream` keeps for a consumer that is behind.
EVENT_STREAM_CAPACITY = 1024


def _log_safe_exc_info(
    exc: BaseException,
) -> tuple[type[BaseException], BaseException, Optional[TracebackType]]:
    """A traceback whose exception text is safe for a gateway log."""
    safe_text = _tool_call_error_text({"error": str(exc)}) or "?"
    safe_exc = RuntimeError(f"{type(exc).__name__}: {safe_text}")
    return type(safe_exc), safe_exc, exc.__traceback__


@dataclass(frozen=True)
class ResourceEvent:
    """One thing a holder observed for one (session, server, uri)."""

    kind: str
    session_key: str
    server: str
    uri: str
    #: Why, for ``paused``; empty otherwise.
    reason: str = ""
    #: For ``paused``, whether this event posts the pause notice. A holder pause
    #: emits one event per URI and sets it only on the first, so it posts one notice.
    notify: bool = True


#: Receives every :class:`ResourceEvent`. Called from a holder's read loop: it
#: must not block and must not await.
EventSink = Callable[[ResourceEvent], None]
#: Start-time question "did the user close this session?". Async when the
#: answer needs a disk read, which must stay off the event loop.
SessionClosedCheck = Callable[[str], "bool | Awaitable[bool]"]


class RecordingSink:
    """Test double for :data:`EventSink`: keeps every event, in order."""

    def __init__(self) -> None:
        self.events: list[ResourceEvent] = []

    def __call__(self, event: ResourceEvent) -> None:
        self.events.append(event)

    def of(self, kind: str, uri: Optional[str] = None) -> list[ResourceEvent]:
        return [e for e in self.events if e.kind == kind and (uri is None or e.uri == uri)]


class EventStream:
    """An :data:`EventSink` read as an async iterator.

    Wake events coalesce by (session, server, URI) in first-arrival order, with
    ``may_have_changed`` dominating ``updated``. A pause removes an earlier wake
    for its key because the dispatcher would cancel that window. The bounded
    queue still evicts its oldest event on overflow, but an ordered overflow
    marker makes the dispatcher regenerate a conservative wake for every held
    subscription, so no distinct key disappears silently.
    """

    def __init__(self, capacity: int = EVENT_STREAM_CAPACITY) -> None:
        self._events: deque[ResourceEvent] = deque()
        self._capacity = capacity
        self._ready = asyncio.Event()
        self._overflow_at: Optional[int] = None
        self.dropped = 0
        self.coalesced = 0

    @staticmethod
    def _key(event: ResourceEvent) -> tuple[str, str, str]:
        return event.session_key, event.server, event.uri

    def __call__(self, event: ResourceEvent) -> None:
        key = self._key(event)
        if event.kind in (EVENT_UPDATED, EVENT_MAY_HAVE_CHANGED):
            for index, queued in enumerate(self._events):
                if (
                    queued.kind in (EVENT_UPDATED, EVENT_MAY_HAVE_CHANGED)
                    and self._key(queued) == key
                ):
                    if event.kind == EVENT_MAY_HAVE_CHANGED and queued.kind != event.kind:
                        self._events[index] = event
                    self.coalesced += 1
                    return
        elif event.kind == EVENT_PAUSED:
            kept: deque[ResourceEvent] = deque()
            removed_before_overflow = 0
            for index, queued in enumerate(self._events):
                if (
                    queued.kind in (EVENT_UPDATED, EVENT_MAY_HAVE_CHANGED)
                    and self._key(queued) == key
                ):
                    if self._overflow_at is not None and index < self._overflow_at:
                        removed_before_overflow += 1
                    self.coalesced += 1
                    continue
                kept.append(queued)
            self._events = kept
            if self._overflow_at is not None:
                self._overflow_at -= removed_before_overflow

        if self._capacity <= 0:
            self.dropped += 1
            if self._overflow_at is None:
                self._overflow_at = 0
            self._ready.set()
            return
        overflow_started = False
        if len(self._events) >= self._capacity:
            self._events.popleft()
            self.dropped += 1
            if self._overflow_at is None:
                overflow_started = True
            elif self._overflow_at > 0:
                self._overflow_at -= 1
        self._events.append(event)
        if overflow_started:
            self._overflow_at = len(self._events)
        self._ready.set()

    async def next(self) -> ResourceEvent:
        while not self._events:
            self._ready.clear()
            await self._ready.wait()
        event = self._events.popleft()
        if self._overflow_at is not None and self._overflow_at > 0:
            self._overflow_at -= 1
        return event

    async def __aiter__(self) -> AsyncIterator[ResourceEvent]:
        while True:
            if self._overflow_at == 0:
                self._overflow_at = None
                yield ResourceEvent(EVENT_STREAM_OVERFLOW, "", "", "")
                continue
            yield await self.next()


#: :attr:`SubscribeRefused.code` values: the refusals of RFC §4.9.3 this service
#: decides, plus two about the gateway or the store rather than the subscribe.
REFUSED_DISABLED = "disabled"
REFUSED_SERVER_NOT_ATTACHED = "server_not_attached"
REFUSED_NOT_SUBSCRIBABLE = PAUSED_NOT_SUBSCRIBABLE
REFUSED_SESSION_LIMIT = "session_limit"
REFUSED_PINNED_BACKENDS = "pinned_backends"
REFUSED_URI_TOO_LONG = "uri_too_long"
REFUSED_GATEWAY_UNAVAILABLE = "gateway_unavailable"
REFUSED_STORE_UNAVAILABLE = "store_unavailable"


class SubscribeRefused(Exception):
    """A subscribe was not attempted; the message says why and ``code`` names it."""

    def __init__(self, reason: str, code: str = REFUSED_GATEWAY_UNAVAILABLE) -> None:
        super().__init__(reason)
        self.code = code


def new_subscription_id() -> str:
    """A fresh id a session names one subscription by (§4.9.3)."""
    return "rsub_" + uuid.uuid4().hex[:16]


class Settings(Protocol):
    """What the service reads from ``mcp_gateway.resource_subscriptions``."""

    enabled: bool
    max_pinned_backends: int


def pin_id(record: HolderRecord) -> str:
    """The backend a holder for *record* keeps resident (§4.6).

    Holders on one pool key share one backend, so they pin it once. A stub the
    daemon served privately -- no ``poolable``, or a hazard retreat -- means the
    holder gets a backend of its own as well.
    """
    if not record.private_backend and record.register.get("poolable") is True:
        try:
            return "pool:" + PoolKey.from_register(record.register).stable_hash()
        except ValueError:
            pass
    return f"private:{record.session_key}\x00{record.server}"


def default_store() -> ResourceSubscriptionStore:
    from kiro_crew.config.paths import data_home  # config imports stay call-time

    return ResourceSubscriptionStore(data_home() / STORE_DIR_LEAF / STORE_LEAF)


def _copy_record(record: HolderRecord) -> HolderRecord:
    copied = HolderRecord.from_json(record.to_json())
    if copied is None:  # pragma: no cover - HolderRecord always serializes to its own schema
        raise ValueError("could not copy a resource subscription record")
    return copied


_MutationResult = TypeVar("_MutationResult")


async def _finish_mutation(mutation: Awaitable[_MutationResult]) -> _MutationResult:
    """Finish *mutation* before delivering any caller cancellation.

    A store write runs in a thread and cannot be cancelled. The mutation includes
    the memory publish or compensation paired with that write, so every repeated
    cancellation waits until both sides agree and then reaches the caller.
    """
    task = asyncio.ensure_future(mutation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class _Listener:
    """Routes one holder's reports to the service, ignoring a superseded holder."""

    def __init__(self, service: "ResourceSubscriptionService", key: tuple[str, str]) -> None:
        self._service = service
        self._key = key
        self.holder: Optional[ResourceHolder] = None

    def _current(self) -> bool:
        return self.holder is not None and self._service._holders.get(self._key) is self.holder

    def on_update(self, uri: str) -> None:
        if self._current():
            self._service._event_arrived(EVENT_UPDATED, self._key, uri)

    def on_gap(self, uris: list[str]) -> None:
        if self._current():
            for uri in uris:
                self._service._event_arrived(EVENT_MAY_HAVE_CHANGED, self._key, uri)

    async def on_paused(self, reason: str, uris: list[str]) -> bool:
        if not self._current() or self.holder is None:
            return False
        return await self._service._holder_paused(self._key, reason, uris, holder=self.holder)

    async def on_refused(self, uri: str, reason: str) -> bool:
        if not self._current() or self.holder is None:
            return False
        return await self._service._uri_refused(self._key, uri, reason, holder=self.holder)


#: Every client-facing route answers within one of these budgets, the wait for
#: ``_lock`` included, whatever a backend does. Each stays strictly below the
#: timeout its caller gives up at, so a caller is never told a change failed
#: while that change goes on to commit. ``BUDGET_MARGIN_SECS`` is the margin a
#: contract test holds them to, against the caller timeouts read with
#: :mod:`inspect`.

#: A subscribe's whole server-side budget. Below the 150 s
#: ``mcp_tools.resources._SUBSCRIBE_TIMEOUT_S`` that ``resource_subscribe`` posts with.
SUBSCRIBE_BUDGET_SECS = 120.0

#: Of a subscribe's budget, what is kept back for the store commit: every holder
#: wait before it is bounded by what is left above this, so the record of a
#: subscribe the server accepted still lands inside the budget.
SUBSCRIBE_COMMIT_RESERVE_SECS = 15.0

#: A removal's whole server-side budget. Below the 30 s default timeout of
#: ``mcp_core._post``, which ``resource_unsubscribe`` posts with, and below the
#: 30 s ``cli_resource_subs._TIMEOUT_SECS`` the operator route answers.
UNSUBSCRIBE_BUDGET_SECS = 20.0

#: How far under its caller's timeout a budget must stay.
BUDGET_MARGIN_SECS = 5.0

#: A holder call after a commit is given at least this long, so the holder always
#: reaches the step where it forgets the URI. A wait of zero would not start it,
#: and it would re-subscribe a subscription that is already removed.
HOLDER_CALL_FLOOR_SECS = 0.1


class _Budget:
    """How long one call may take in all, as a deadline on the running loop."""

    def __init__(self, total: float) -> None:
        self.total = total
        self._deadline = asyncio.get_running_loop().time() + total

    def remaining(self) -> float:
        """Seconds left; zero or less once the deadline has passed."""
        return self._deadline - asyncio.get_running_loop().time()

    def allowance(self, what: str, *, reserve: float = 0.0) -> float:
        """Seconds *what* may take, keeping *reserve* for what follows it.

        Raises :class:`SubscribeRefused` rather than start *what* with nothing
        left, so no step of a change runs once the caller has given up on it.
        """
        left = self.remaining() - reserve
        if left <= 0:
            raise SubscribeRefused(
                f"this call reached its bound of {self.total:g}s before {what}; try again",
                REFUSED_GATEWAY_UNAVAILABLE,
            )
        return left

    def holder_call(self) -> float:
        """What is left for a holder call after a commit, never zero."""
        return max(self.remaining(), HOLDER_CALL_FLOOR_SECS)


def _release_if_acquired(lock: asyncio.Lock, acquire: "asyncio.Future[bool]") -> None:
    """Release *lock* for an acquire that won its race after its wait had ended."""
    if not acquire.cancelled() and acquire.exception() is None and acquire.result():
        lock.release()


@asynccontextmanager
async def _locked_within(
    lock: asyncio.Lock,
    budget: _Budget,
    what: str,
    *,
    reserve: float = 0.0,
) -> AsyncIterator[None]:
    """Hold *lock*, taken inside *budget*, or refuse before anything moves.

    The acquire is a task of its own, so a wait that ends at the bound or is
    cancelled cannot leave the lock held: the task is cancelled, and the one
    race where it was granted the lock anyway releases it from a done callback.
    """
    bound = budget.allowance(f"the {what} lock", reserve=reserve)
    acquire: "asyncio.Future[bool]" = asyncio.ensure_future(lock.acquire())
    try:
        await asyncio.wait_for(asyncio.shield(acquire), bound)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        acquire.add_done_callback(lambda done: _release_if_acquired(lock, done))
        acquire.cancel()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SubscribeRefused(
            f"another resource subscription change is still in progress, so this {what} "
            f"did not start within {budget.total:g}s; try again",
            REFUSED_GATEWAY_UNAVAILABLE,
        ) from None
    try:
        yield
    finally:
        lock.release()


def _holder_failure(exc: BaseException) -> str:
    """Why a holder or gateway call failed, for a refusal reason. Never empty."""
    if isinstance(exc, asyncio.TimeoutError):
        return "it did not answer in time"
    return str(exc) or type(exc).__name__


#: Holder closes a removal left running at its bound, held so nothing collects
#: them. The holder is out of ``_holders`` before the wait, so each is already
#: detached from the service.
_LATE_HOLDER_CLOSES: set["asyncio.Future[None]"] = set()


async def _holder_call_within(call: Awaitable[None], bound: float, what: str) -> None:
    """Await *call* for at most *bound*, cancelling it rather than overrun.

    A cancelled unsubscribe ends the holder's session, exactly as one that timed
    out on the wire does, so what the server still holds goes with the connection.
    """
    try:
        await asyncio.wait_for(call, bound)
    except asyncio.TimeoutError:
        logger.warning(
            "resource subscriptions: %s did not finish inside the answer's bound",
            _log_safe_identifier(what),
        )


class ResourceSubscriptionService:
    """The holders, their record and their caps, for every session."""

    def __init__(
        self,
        *,
        store: ResourceSubscriptionStore,
        socket_path: Callable[[], Optional[Path]],
        sink: EventSink,
        settings: Callable[[], Settings],
        session_is_closed: SessionClosedCheck = lambda _session_key: False,
        ping_interval_secs: float = PING_INTERVAL_SECS,
        reconnect_delay_secs: float = RECONNECT_DELAY_SECS,
        ready_timeout_secs: float = HANDSHAKE_TIMEOUT_SECS,
        probe_timeout_secs: float = HANDSHAKE_TIMEOUT_SECS,
        subscribe_budget_secs: float = SUBSCRIBE_BUDGET_SECS,
        subscribe_commit_reserve_secs: float = SUBSCRIBE_COMMIT_RESERVE_SECS,
        unsubscribe_budget_secs: float = UNSUBSCRIBE_BUDGET_SECS,
    ) -> None:
        self._store = store
        self._socket_path = socket_path
        self._sink = sink
        self._settings = settings
        self._session_is_closed = session_is_closed
        self._ping_interval = ping_interval_secs
        self._reconnect_delay = reconnect_delay_secs
        self._ready_timeout = ready_timeout_secs
        self._probe_timeout = probe_timeout_secs
        self._subscribe_budget = subscribe_budget_secs
        self._commit_reserve = subscribe_commit_reserve_secs
        self._unsubscribe_budget = unsubscribe_budget_secs
        self._records: dict[tuple[str, str], HolderRecord] = {}
        self._holders: dict[tuple[str, str], ResourceHolder] = {}
        self._lock = asyncio.Lock()
        #: Held around every change to ``_records``: read the current record,
        #: write the store, publish. Never held across a holder wait, so a
        #: holder's own transition can take it while a method holding
        #: ``_lock`` waits for that holder, and neither overwrites the other.
        self._write_lock = asyncio.Lock()
        #: Set when the store could not be read at start: nothing is written
        #: until it can be, so one bad byte cannot erase every subscription.
        self._unreadable = ""
        #: Closed services own a stale snapshot and may never write it again.
        self._closed = False
        #: True only after the startup snapshot has been loaded and published.
        self._started = False
        #: (session, server, uri) of a subscribe the server accepted and this
        #: service has not saved yet, and the event kind that arrived for it
        #: meanwhile ("" for none). That event is published only once the save
        #: lands, and discarded when it fails. One entry at most: ``subscribe``
        #: holds ``_lock`` across it.
        self._unsaved: dict[tuple[str, str, str], str] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Load records and restore only the backend pins admitted by today's cap.

        A closed session whose removal failed earlier is removed again first, and
        its records are not restored while that still fails. All of it holds
        ``_lock``, so a retire or subscribe that arrives meanwhile acts on what
        this start loaded, not on a store it is about to overwrite from memory.
        """
        self._started = False
        await _finish_mutation(self._start())
        self._started = True

    async def _closed_at_start(self, session_key: str) -> bool:
        """Ask *session_is_closed* once about *session_key*, awaiting it when it is async."""
        verdict = self._session_is_closed(session_key)
        if inspect.isawaitable(verdict):
            return bool(await verdict)
        return bool(verdict)

    async def _start(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("a closed resource subscription service cannot start")
            try:
                records = await asyncio.to_thread(self._store.load)
            except StoreUnreadable as exc:
                self._unreadable = str(exc)
                logger.error(
                    "resource subscriptions: store unreadable, holding nothing: %s",
                    _tool_call_error_text({"error": str(exc)}) or "?",
                    exc_info=_log_safe_exc_info(exc),
                )
                return
            retiring_sessions = set(_UNRETIRED)
            for session_key in sorted({record.session_key for record in records}):
                if await self._closed_at_start(session_key):
                    retiring_sessions.add(session_key)
            for session_key in sorted(retiring_sessions):
                try:
                    await asyncio.to_thread(self._store.remove, session_key)
                except (OSError, StoreUnreadable) as exc:
                    _UNRETIRED.add(session_key)
                    logger.error(
                        "resource subscriptions: could not remove closed session %s at start: %s",
                        _log_safe_identifier(session_key),
                        _tool_call_error_text({"error": str(exc)}) or "?",
                        exc_info=_log_safe_exc_info(exc),
                    )
                else:
                    _UNRETIRED.discard(session_key)
            records = [r for r in records if r.session_key not in retiring_sessions]
            settings = self._settings()
            admitted: set[str] = set()
            prepared: list[HolderRecord] = []
            for loaded in records:
                record = loaded
                if settings.enabled and not record.paused_reason and record.uris:
                    record_pin = pin_id(record)
                    if record_pin in admitted or len(admitted) < settings.max_pinned_backends:
                        admitted.add(record_pin)
                    else:
                        record = _copy_record(record)
                        record.paused_reason = (
                            f"restoring {record.server!r} would pin more than "
                            "mcp_gateway.resource_subscriptions.max_pinned_backends allows "
                            f"({settings.max_pinned_backends})"
                        )
                        await asyncio.to_thread(self._store.put, record)
                prepared.append(record)
            for record in prepared:
                self._records[record.key] = record
                if (
                    settings.enabled
                    and not record.paused_reason
                    and record.uris
                    and pin_id(record) in admitted
                ):
                    self._start_holder(record, uris=record.uris, resumed=True)

    async def close(self) -> None:
        """Drop every holder and make this service's record snapshot read-only."""
        async with self._lock:
            async with self._write_lock:
                self._closed = True
                self._started = False
                holders = list(self._holders.values())
                self._holders.clear()
        for holder in holders:
            await holder.close()

    # -- reads ---------------------------------------------------------------

    @property
    def sink(self) -> EventSink:
        return self._sink

    @property
    def started(self) -> bool:
        """Whether the startup snapshot is published and operator writes are safe."""
        return self._started

    @property
    def store_unreadable(self) -> bool:
        """Whether the startup store read failed, so :meth:`records` holds none of the store's."""
        return bool(self._unreadable)

    def records(self, session_key: str = "") -> list[HolderRecord]:
        return [
            r for r in self._records.values() if not session_key or r.session_key == session_key
        ]

    def pinned_backends(self) -> set[str]:
        """Backends live holders keep resident; a paused holder pins nothing."""
        return {pin_id(self._records[key]) for key in self._holders if key in self._records}

    def holder(self, session_key: str, server: str) -> Optional[ResourceHolder]:
        return self._holders.get((session_key, server))

    def subscription(self, session_key: str, server: str, uri: str) -> Optional[SubscriptionMeta]:
        """The id, instruction and wake count of one subscription, if it is held."""
        record = self._records.get((session_key, server))
        if record is None or (uri not in record.uris and uri not in record.paused_uris):
            return None
        return record.subscriptions.get(uri)

    def paused(self, session_key: str, server: str, uri: str) -> bool:
        """Whether the holder or this URI is paused, excluding the daily wake budget."""
        record = self._records.get((session_key, server))
        return record is not None and bool(record.paused_reason or uri in record.paused_uris)

    def find_subscription(
        self, session_key: str, subscription_id: str
    ) -> Optional[tuple[HolderRecord, str]]:
        """The record and URI *subscription_id* names, only among *session_key*'s own."""
        for record in self.records(session_key):
            for uri, meta in record.subscriptions.items():
                if meta.id == subscription_id and (uri in record.uris or uri in record.paused_uris):
                    return record, uri
        return None

    # -- wake budget (§4.9.2) ------------------------------------------------

    async def charge_wake(
        self,
        session_key: str,
        server: str,
        uri: str,
        subscription_id: str,
        *,
        day: str,
        limit: int,
    ) -> Optional[int]:
        """Count one wake on UTC *day* before delivery; return the day's count.

        ``None`` means the expected subscription is gone or the service is closed.
        Zero means its daily budget is exhausted. The deciding check and increment
        are serialized, and a counted charge is stored before it is published.
        """

        def _charge(meta: SubscriptionMeta) -> bool:
            if meta.id != subscription_id:
                return False
            used = meta.wakes if meta.wake_day == day else 0
            if used >= limit:
                return False
            if meta.wake_day != day:
                meta.wake_day, meta.wakes = day, 0
            meta.wakes += 1
            return True

        meta, changed = await self._update_meta_if((session_key, server), uri, _charge)
        if meta is None or meta.id != subscription_id:
            return None
        return meta.wakes if changed else 0

    async def refund_wake(
        self, session_key: str, server: str, uri: str, subscription_id: str, *, day: str
    ) -> None:
        """Refund a wake charged to the expected subscription on UTC *day*."""

        def _refund(meta: SubscriptionMeta) -> bool:
            if meta.id != subscription_id or meta.wake_day != day or meta.wakes == 0:
                return False
            meta.wakes -= 1
            return True

        await self._update_meta_if((session_key, server), uri, _refund)

    async def note_budget_reported(
        self, session_key: str, server: str, uri: str, subscription_id: str, *, day: str
    ) -> bool:
        """Record the expected subscription's budget pause for *day* once."""

        def _note(meta: SubscriptionMeta) -> bool:
            if meta.id != subscription_id or meta.budget_reported_day == day:
                return False
            meta.budget_reported_day = day
            return True

        meta, changed = await self._update_meta_if((session_key, server), uri, _note)
        return meta is not None and meta.id == subscription_id and changed

    async def _update_meta(
        self,
        key: tuple[str, str],
        uri: str,
        change: Callable[[SubscriptionMeta], None],
    ) -> Optional[SubscriptionMeta]:
        def _apply(meta: SubscriptionMeta) -> bool:
            change(meta)
            return True

        meta, _ = await self._update_meta_if(key, uri, _apply)
        return meta

    async def _update_meta_if(
        self,
        key: tuple[str, str],
        uri: str,
        change: Callable[[SubscriptionMeta], bool],
    ) -> tuple[Optional[SubscriptionMeta], bool]:
        return await _finish_mutation(self._update_meta_committed(key, uri, change))

    async def _update_meta_committed(
        self,
        key: tuple[str, str],
        uri: str,
        change: Callable[[SubscriptionMeta], bool],
    ) -> tuple[Optional[SubscriptionMeta], bool]:
        async with self._write_lock:
            if self._closed:
                return None, False
            record = self._records.get(key)
            if record is None or uri not in record.subscriptions:
                return None, False
            proposed = _copy_record(record)
            meta = proposed.subscriptions[uri]
            if not change(meta):
                return meta, False
            await asyncio.to_thread(self._store.put, proposed)
            self._records[key] = proposed
            return meta, True

    # -- writes --------------------------------------------------------------

    async def subscribe(
        self, session_key: str, server: str, uri: str, *, instruction: str = ""
    ) -> dict[str, Any]:
        """Subscribe *session_key* to *uri* on *server*; the server's answer.

        *instruction* is what the subscription's wakes carry (§4.9.2). The same
        (session, server, URI) again keeps its id and wake count and replaces
        the instruction (§4.9.3).

        Raises :class:`SubscribeRefused` with the reason when a cap, the switch,
        the store or the gateway rules the subscribe out before it reaches the
        server. A server that refuses answers with its own JSON-RPC error.

        The whole call is bounded by :data:`SUBSCRIBE_BUDGET_SECS`, the wait for
        ``_lock`` included, so it always answers before the caller gives up and
        nothing commits after that. Out of budget it takes the same refusal and
        cleanup paths as a holder that never connected: no record, no holder.
        """
        budget = _Budget(self._subscribe_budget)
        if self._closed:
            raise SubscribeRefused(
                "the resource subscription service is closed", REFUSED_GATEWAY_UNAVAILABLE
            )
        settings = self._settings()
        if not settings.enabled:
            raise SubscribeRefused(
                "resource subscriptions are off (mcp_gateway.resource_subscriptions.enabled)",
                REFUSED_DISABLED,
            )
        if self._unreadable:
            raise SubscribeRefused(
                f"the subscription store is unreadable: {self._unreadable}",
                REFUSED_STORE_UNAVAILABLE,
            )
        if not uri:
            raise SubscribeRefused("a subscribe needs a URI", REFUSED_URI_TOO_LONG)
        if len(uri) > RESOURCE_URI_MAX_LEN:
            raise SubscribeRefused(
                f"a resource URI may be at most {RESOURCE_URI_MAX_LEN} characters; "
                f"this one has {len(uri)}",
                REFUSED_URI_TOO_LONG,
            )
        key = (session_key, server)
        async with _locked_within(self._lock, budget, "subscribe"):
            if self._closed:
                raise SubscribeRefused(
                    "the resource subscription service is closed", REFUSED_GATEWAY_UNAVAILABLE
                )
            if session_key in _UNRETIRED:
                await self._settle_unretired(session_key, budget)
            existing = self._records.get(key)
            if (
                self._session_total(session_key, besides=(server, uri))
                >= MAX_SUBSCRIPTIONS_PER_SESSION
            ):
                raise SubscribeRefused(
                    f"this session already holds {MAX_SUBSCRIPTIONS_PER_SESSION} resource "
                    "subscriptions, the most one session may hold",
                    REFUSED_SESSION_LIMIT,
                )
            holder = self._holders.get(key)
            # The unpersisted record this call installs for a holder it starts;
            # put back only while it is still the current one.
            installed: Optional[HolderRecord] = None
            if holder is None:
                bound = budget.allowance("describe-session", reserve=self._commit_reserve)
                try:
                    record = await asyncio.wait_for(self._describe(session_key, server), bound)
                except asyncio.TimeoutError as exc:
                    raise SubscribeRefused(
                        "the MCP gateway did not answer describe-session: "
                        f"{_holder_failure(exc)}",
                        REFUSED_GATEWAY_UNAVAILABLE,
                    ) from exc
                self._check_pin_cap(record, settings.max_pinned_backends)
                carried = list(existing.uris) + list(existing.paused_uris) if existing else []
                # The new record is current while its holder connects, so what
                # the holder reports lands on it; a failed start puts back the
                # record it replaced, which still names every carried URI.
                record.uris = list(carried)
                if existing is not None:
                    record.paused_reason = existing.paused_reason
                    record.paused_uris = dict(existing.paused_uris)
                    record.subscriptions = dict(existing.subscriptions)
                async with _locked_within(
                    self._write_lock,
                    budget,
                    "subscribe install",
                    reserve=self._commit_reserve,
                ):
                    self._records[key] = record
                installed = record
                holder = self._start_holder(
                    record, uris=carried, resumed=bool(carried), awaiting_subscribe=True
                )
                try:
                    await holder.wait_ready(
                        min(
                            self._ready_timeout,
                            budget.allowance("the holder connected", reserve=self._commit_reserve),
                        )
                    )
                except asyncio.CancelledError:
                    await self._drop_installed_holder(key, holder, installed, existing)
                    raise
                except SubscribeRefused:
                    await self._drop_installed_holder_within(
                        key, holder, installed, existing, budget
                    )
                    raise
                except HolderPaused as paused:
                    await self._drop_installed_holder_within(
                        key, holder, installed, existing, budget
                    )
                    raise SubscribeRefused(
                        str(paused), paused.code or REFUSED_GATEWAY_UNAVAILABLE
                    ) from paused
                installed.uris = holder.uris
            was_subscribed = uri in holder.uris
            unsaved = (session_key, server, uri)
            if not was_subscribed:
                self._unsaved[unsaved] = ""
            saved = False
            try:
                try:
                    bound = budget.allowance(
                        "the server answered the subscribe", reserve=self._commit_reserve
                    )
                    reply = await asyncio.wait_for(holder.subscribe(uri), bound)
                except asyncio.CancelledError:
                    if installed is not None:
                        await self._drop_installed_holder(key, holder, installed, existing)
                    raise
                except SubscribeRefused:
                    if installed is not None:
                        await self._settle_started_holder(key, holder, installed, existing, budget)
                    raise
                except (HolderNotLive, OSError, ConnectionError, asyncio.TimeoutError) as exc:
                    if installed is not None:
                        await self._settle_started_holder(key, holder, installed, existing, budget)
                    raise SubscribeRefused(
                        f"the gateway connection for {server!r} is not live: "
                        f"{_holder_failure(exc)}",
                        REFUSED_GATEWAY_UNAVAILABLE,
                    ) from exc
                if "error" in reply:
                    if installed is not None:
                        await self._settle_started_holder(key, holder, installed, existing, budget)
                    return reply

                async def commit_subscription() -> None:
                    nonlocal saved
                    try:
                        async with _locked_within(
                            self._write_lock,
                            budget,
                            "subscribe commit",
                            reserve=self._commit_reserve,
                        ):
                            if self._closed:
                                raise SubscribeRefused(
                                    "the resource subscription service is closed",
                                    REFUSED_GATEWAY_UNAVAILABLE,
                                )
                            if self._holders.get(key) is not holder:
                                # The holder paused after the server accepted: its
                                # pause wrote the record, which stays as it wrote it.
                                pause_record = self._records.get(key)
                                reason = (pause_record.paused_reason if pause_record else "") or (
                                    holder.paused_reason
                                )
                                raise SubscribeRefused(
                                    f"the holder for {server!r} paused: {reason or 'it stopped'}",
                                    holder.paused_code or REFUSED_GATEWAY_UNAVAILABLE,
                                )
                            # Built from the record current NOW: a holder transition
                            # that landed during the waits above is kept, not overwritten.
                            current = self._records.get(key)
                            if current is None:
                                raise SubscribeRefused(
                                    f"the subscription for {server!r} was removed",
                                    REFUSED_GATEWAY_UNAVAILABLE,
                                )
                            proposed = _copy_record(current)
                            if installed is not None:
                                proposed.paused_reason = ""
                                for held_uri in holder.uris:
                                    proposed.paused_uris.pop(held_uri, None)
                            proposed.paused_uris.pop(uri, None)
                            proposed.uris = [
                                u for u in holder.uris if u == uri or u not in proposed.paused_uris
                            ]
                            if uri not in proposed.uris:
                                proposed.uris.append(uri)
                            kept = proposed.subscriptions.get(uri)
                            proposed.subscriptions[uri] = SubscriptionMeta(
                                id=kept.id if kept is not None else new_subscription_id(),
                                instruction=instruction,
                                wake_day=kept.wake_day if kept is not None else "",
                                wakes=kept.wakes if kept is not None else 0,
                                budget_reported_day=(
                                    kept.budget_reported_day if kept is not None else ""
                                ),
                            )
                            budget.allowance("the durable subscribe commit")
                            try:
                                await asyncio.to_thread(self._store.put, proposed)
                            except (OSError, StoreUnreadable) as exc:
                                if installed is not None and current is installed:
                                    self._restore_record(key, existing)
                                raise SubscribeRefused(
                                    f"could not persist the subscription for {server!r}: {exc}",
                                    REFUSED_STORE_UNAVAILABLE,
                                ) from exc
                            self._records[key] = proposed
                            saved = True
                    except SubscribeRefused:
                        if installed is not None:
                            await self._drop_installed_holder_within(
                                key, holder, installed, existing, budget
                            )
                        elif not was_subscribed:
                            await _holder_call_within(
                                holder.unsubscribe(uri),
                                budget.holder_call(),
                                f"the refused subscribe cleanup for {server}",
                            )
                        raise

                await _finish_mutation(commit_subscription())
                return reply
            finally:
                # An event held back while the subscribe was unsaved goes out
                # once the save landed, and never when it did not.
                held = self._unsaved.pop(unsaved, "")
                if held and saved:
                    self._emit(held, key, uri)

    async def unsubscribe(
        self,
        session_key: str,
        server: str,
        uri: str,
        *,
        expected_subscription_id: Optional[str] = None,
    ) -> bool:
        """Drop one subscription if its optional expected id still names it.

        ``False`` means nothing was removed: the id names nothing current, the
        service is closed, or the store could not take the removal. Nothing in
        memory, on disk or on the holder moved, so the caller may retry.

        Raises :class:`SubscribeRefused` with ``gateway_unavailable`` when the
        removal could not start inside :data:`UNSUBSCRIBE_BUDGET_SECS` because
        another subscription change holds ``_lock`` or ``_write_lock``. Nothing
        moved then either.
        """
        return await _finish_mutation(
            self._unsubscribe(
                session_key,
                server,
                uri,
                expected_subscription_id=expected_subscription_id,
            )
        )

    async def _unsubscribe(
        self,
        session_key: str,
        server: str,
        uri: str,
        *,
        expected_subscription_id: Optional[str] = None,
    ) -> bool:
        key = (session_key, server)
        budget = _Budget(self._unsubscribe_budget)
        async with _locked_within(self._lock, budget, "removal"):
            if self._closed:
                return False
            async with _locked_within(self._write_lock, budget, "removal commit"):
                record = self._records.get(key)
                if record is None or (uri not in record.uris and uri not in record.paused_uris):
                    return False
                meta = record.subscriptions.get(uri)
                if expected_subscription_id is not None and (
                    meta is None or meta.id != expected_subscription_id
                ):
                    return False
                proposed = _copy_record(record)
                if uri in proposed.uris:
                    proposed.uris.remove(uri)
                proposed.paused_uris.pop(uri, None)
                proposed.subscriptions.pop(uri, None)
                holder = self._holders.get(key)
                budget.allowance("the durable removal commit")
                try:
                    if proposed.uris or proposed.paused_uris:
                        await asyncio.to_thread(self._store.put, proposed)
                        self._records[key] = proposed
                    else:
                        await asyncio.to_thread(self._store.remove, session_key, server)
                        self._records.pop(key, None)
                except (OSError, StoreUnreadable) as exc:
                    # A removal the store cannot take keeps the subscription whole:
                    # memory, holder and event are left as they were, and the
                    # route's removal refusal names it gateway_unavailable.
                    logger.error(
                        "resource subscriptions: the removal of a subscription for %s "
                        "did not commit: %s",
                        _log_safe_identifier(session_key),
                        _tool_call_error_text({"error": str(exc)}) or "?",
                        exc_info=_log_safe_exc_info(exc),
                    )
                    return False
            self._emit(EVENT_REMOVED, key, uri)
            if holder is None:
                return True
            # The removal is committed, so the answer reports it whatever the
            # holder does next; the round trip may not hold that answer past the
            # budget. Cancelled at the bound, the holder ends its session and
            # re-subscribes only what its own URIs still name.
            if proposed.uris:
                await _holder_call_within(
                    holder.unsubscribe(uri), budget.holder_call(), f"the unsubscribe of {server}"
                )
            else:
                # Nothing live is left for it to hold; it would still pin its backend.
                await self._drop_holder_within(key, holder, budget)
            return True

    async def remove_session(
        self, session_key: str, *, keep: Optional[Callable[[str], bool]] = None
    ) -> int:
        """§4.7: every holder and record of *session_key*; how many records went.

        The holders and the in-memory records go even when the store cannot be
        written: the session is closed either way. The store error is then
        raised, and the session is remembered in :data:`_UNRETIRED` so no start
        restores its records. *keep* rechecks a deferred retirement once the
        store write lock is held.
        """
        return await _finish_mutation(self._remove_session(session_key, keep))

    async def _remove_session(self, session_key: str, keep: Optional[Callable[[str], bool]]) -> int:
        async with self._lock:
            if self._closed:
                return 0
            return await self._remove_session_locked(session_key, keep)

    async def remove_sessions_with_stems(
        self, trashed: set[str], keep: Optional[Callable[[str], bool]] = None
    ) -> int:
        return await _finish_mutation(self._remove_sessions_with_stems(trashed, keep))

    async def _remove_sessions_with_stems(
        self, trashed: set[str], keep: Optional[Callable[[str], bool]]
    ) -> int:
        """§4.7 for the trash and delete: :meth:`remove_session` for every session
        whose transcript stems are in *trashed*, unless *keep* names it.

        Takes ``_lock``, so it waits for a :meth:`start` that is still loading
        and matches against what that start loaded. A store error for one
        session is logged and the rest are still removed.
        """
        from kiro_crew.history_files import transcript_stems  # history imports stay call-time

        async with self._lock:
            if self._closed:
                return 0
            if self._unreadable:
                logger.error(
                    "resource subscriptions: the store is unreadable, so the trashed sessions "
                    "could not be checked for subscriptions: %s",
                    _tool_call_error_text({"error": self._unreadable}) or "?",
                )
                return 0
            sessions = {k[0] for k in self._records} | {k[0] for k in self._holders}
            removed = 0
            for session_key in sorted(sessions):
                if not trashed.intersection(transcript_stems(session_key)):
                    continue
                try:
                    removed += await self._remove_session_locked(session_key, keep)
                except (OSError, StoreUnreadable) as exc:
                    logger.error(
                        "resource subscriptions: could not retire %s: %s",
                        _log_safe_identifier(session_key),
                        _tool_call_error_text({"error": str(exc)}) or "?",
                        exc_info=_log_safe_exc_info(exc),
                    )
            return removed

    async def _remove_session_locked(
        self, session_key: str, keep: Optional[Callable[[str], bool]]
    ) -> int:
        """:meth:`remove_session`'s body; the caller holds ``_lock``.

        *keep* is asked once ``_write_lock`` is held: a holder's store write can
        hold that lock while the session is reopened.
        """
        failure: Optional[BaseException] = None
        removed = 0
        async with self._write_lock:
            if self._closed or (keep is not None and keep(session_key)):
                return 0
            keys = [k for k in self._records if k[0] == session_key]
            keys += [k for k in self._holders if k[0] == session_key and k not in keys]
            removed_subscriptions = [
                (record.key, uri)
                for record in self._records.values()
                if record.session_key == session_key
                for uri in set(record.uris) | set(record.paused_uris)
            ]
            try:
                removed = await asyncio.to_thread(self._store.remove, session_key)
            except (OSError, StoreUnreadable) as exc:
                failure = exc
                _UNRETIRED.add(session_key)
            else:
                _UNRETIRED.discard(session_key)
            holders = [(k, self._holders.get(k)) for k in keys]
            for key in keys:
                self._records.pop(key, None)
        for key, uri in removed_subscriptions:
            self._emit(EVENT_REMOVED, key, uri)
        for key, holder in holders:
            if holder is not None:
                await self._drop_holder(key, holder)
        if failure is not None:
            raise failure
        return max(removed, len(keys))

    # -- internals -----------------------------------------------------------

    def _session_total(self, session_key: str, *, besides: tuple[str, str]) -> int:
        """Subscriptions *session_key* holds, not counting (server, uri) *besides*."""
        total = 0
        for record in self.records(session_key):
            held = set(record.uris) | set(record.paused_uris)
            if record.server == besides[0]:
                held.discard(besides[1])
            total += len(held)
        return total

    async def _settle_unretired(self, session_key: str, budget: _Budget) -> None:
        """Finish the failed removal of *session_key* before it subscribes again.

        Otherwise the next start would either drop the new subscription with the
        old ones, or restore the old ones with it.
        """
        await _finish_mutation(self._settle_unretired_committed(session_key, budget))

    async def _settle_unretired_committed(self, session_key: str, budget: _Budget) -> None:
        async with _locked_within(
            self._write_lock,
            budget,
            "subscribe cleanup",
            reserve=self._commit_reserve,
        ):
            budget.allowance("the earlier subscription removal")
            try:
                await asyncio.to_thread(self._store.remove, session_key)
            except (OSError, StoreUnreadable) as exc:
                raise SubscribeRefused(
                    f"this session's earlier subscriptions could not be removed: {exc}",
                    REFUSED_STORE_UNAVAILABLE,
                ) from exc
            _UNRETIRED.discard(session_key)

    def _restore_record(self, key: tuple[str, str], previous: Optional[HolderRecord]) -> None:
        if previous is None:
            self._records.pop(key, None)
        else:
            self._records[key] = previous

    def _undo_install(
        self,
        key: tuple[str, str],
        installed: HolderRecord,
        previous: Optional[HolderRecord],
    ) -> None:
        """Put *previous* back unless a holder transition replaced *installed* durably."""
        if self._records.get(key) is installed:
            self._restore_record(key, previous)

    async def _drop_installed_holder(
        self,
        key: tuple[str, str],
        holder: ResourceHolder,
        installed: HolderRecord,
        previous: Optional[HolderRecord],
    ) -> None:
        """Detach a failed holder, restore its record, then finish closing it."""
        self._forget_holder(key, holder)
        self._undo_install(key, installed, previous)
        await holder.close()

    async def _drop_installed_holder_within(
        self,
        key: tuple[str, str],
        holder: ResourceHolder,
        installed: HolderRecord,
        previous: Optional[HolderRecord],
        budget: _Budget,
    ) -> None:
        """Restore a failed install before its detached holder closes within *budget*."""
        self._forget_holder(key, holder)
        self._undo_install(key, installed, previous)
        await self._drop_holder_within(key, holder, budget)

    async def _settle_started_holder(
        self,
        key: tuple[str, str],
        holder: ResourceHolder,
        installed: HolderRecord,
        previous: Optional[HolderRecord],
        budget: _Budget,
    ) -> None:
        await _finish_mutation(
            self._settle_started_holder_committed(key, holder, installed, previous, budget)
        )

    async def _settle_started_holder_committed(
        self,
        key: tuple[str, str],
        holder: ResourceHolder,
        installed: HolderRecord,
        previous: Optional[HolderRecord],
        budget: _Budget,
    ) -> None:
        """Keep a started holder only after recording the carried subscriptions."""
        if self._holders.get(key) is not holder:
            self._undo_install(key, installed, previous)
            return
        try:
            async with _locked_within(self._write_lock, budget, "refused subscribe cleanup"):
                if self._holders.get(key) is not holder:
                    self._undo_install(key, installed, previous)
                    return
                held = holder.uris
                current = self._records.get(key)
                if not self._closed and held and current is not None:
                    proposed = _copy_record(current)
                    proposed.paused_reason = ""
                    for uri in held:
                        proposed.paused_uris.pop(uri, None)
                    proposed.uris = held
                    budget.allowance("the durable refused subscribe cleanup")
                    try:
                        await asyncio.to_thread(self._store.put, proposed)
                    except (OSError, StoreUnreadable) as exc:
                        logger.error(
                            "resource subscriptions: could not record the carried subscriptions: %s",
                            _tool_call_error_text({"error": str(exc)}) or "?",
                            exc_info=_log_safe_exc_info(exc),
                        )
                    else:
                        self._records[key] = proposed
                        return
        except SubscribeRefused:
            pass
        await self._drop_installed_holder_within(key, holder, installed, previous, budget)

    def _check_pin_cap(self, record: HolderRecord, cap: int) -> None:
        pinned = self.pinned_backends()
        if pin_id(record) in pinned:
            return
        if len(pinned) >= cap:
            raise SubscribeRefused(
                f"holding {record.server!r} would pin {len(pinned) + 1} MCP backends; "
                f"mcp_gateway.resource_subscriptions.max_pinned_backends allows {cap}",
                REFUSED_PINNED_BACKENDS,
            )

    async def _describe(self, session_key: str, server: str) -> HolderRecord:
        path = self._socket_path()
        if path is None:
            raise SubscribeRefused("the MCP gateway is not running", REFUSED_GATEWAY_UNAVAILABLE)
        try:
            reply = await describe_session(path, session_key, server)
        except (OSError, ConnectionError, asyncio.TimeoutError, ValueError) as exc:
            raise SubscribeRefused(
                f"the MCP gateway did not answer describe-session: {_holder_failure(exc)}",
                REFUSED_GATEWAY_UNAVAILABLE,
            ) from exc
        if reply.get("type") == "session-undescribed":
            raise SubscribeRefused(
                str(reply.get("reason") or "describe-session found no live stub"),
                REFUSED_SERVER_NOT_ATTACHED,
            )
        if reply.get("type") != "session-described":
            raise SubscribeRefused(
                str(reply.get("reason") or f"describe-session answered {reply!r}"),
                REFUSED_GATEWAY_UNAVAILABLE,
            )
        register, initialize = reply.get("register"), reply.get("initialize")
        if not isinstance(register, dict) or not isinstance(initialize, dict):
            raise SubscribeRefused(
                "describe-session answered without a Register and an initialize",
                REFUSED_GATEWAY_UNAVAILABLE,
            )
        for field_name, container in (("Register", register), ("initialize", initialize)):
            size = serialized_container_size(container)
            if size > RETAINED_CONTAINER_MAX_BYTES:
                # The daemon answers the same over-bound container with
                # session-undescribed, which maps to server_not_attached.
                raise SubscribeRefused(
                    f"describe-session {field_name} exceeds the retained-container limit "
                    f"of {RETAINED_CONTAINER_MAX_BYTES} bytes ({size})",
                    REFUSED_SERVER_NOT_ATTACHED,
                )
        return HolderRecord(
            session_key=session_key,
            channel_id=str(reply.get("channel_id") or ""),
            server=server,
            register=register,
            initialize=initialize,
            private_backend=reply.get("private_backend") is True,
        )

    def _start_holder(
        self,
        record: HolderRecord,
        *,
        uris: Iterable[str],
        resumed: bool,
        awaiting_subscribe: bool = False,
    ) -> ResourceHolder:
        listener = _Listener(self, record.key)
        holder = ResourceHolder(
            HolderSpec(
                session_key=record.session_key,
                channel_id=record.channel_id,
                server=record.server,
                register=record.register,
                initialize=record.initialize,
            ),
            self._socket_path,
            listener,
            uris=tuple(uris),
            resumed=resumed,
            awaiting_subscribe=awaiting_subscribe,
            ping_interval_secs=self._ping_interval,
            reconnect_delay_secs=self._reconnect_delay,
            probe_timeout_secs=self._probe_timeout,
        )
        listener.holder = holder
        self._holders[record.key] = holder
        holder.start()
        return holder

    async def _drop_holder(self, key: tuple[str, str], holder: ResourceHolder) -> None:
        if self._holders.get(key) is holder:
            del self._holders[key]
        await holder.close()

    async def _drop_holder_within(
        self, key: tuple[str, str], holder: ResourceHolder, budget: _Budget
    ) -> None:
        """:meth:`_drop_holder` without holding its caller's answer past *budget*.

        The holder leaves ``_holders`` first, so it is detached before the wait.
        A close still running at the bound runs on, referenced from
        :data:`_LATE_HOLDER_CLOSES` so nothing collects it while it does.
        """
        if self._holders.get(key) is holder:
            del self._holders[key]
        closing: "asyncio.Future[None]" = asyncio.ensure_future(holder.close())
        _LATE_HOLDER_CLOSES.add(closing)
        closing.add_done_callback(_LATE_HOLDER_CLOSES.discard)
        try:
            await asyncio.wait_for(asyncio.shield(closing), budget.holder_call())
        except asyncio.TimeoutError:
            logger.warning(
                "resource subscriptions: a holder of %s is still closing after its "
                "removal was answered",
                _log_safe_identifier(key[0]),
            )

    def _event_arrived(self, kind: str, key: tuple[str, str], uri: str) -> None:
        """Publish *kind* for *uri*, or hold it while its subscribe is not saved yet.

        Held events collapse into one. A "may have changed" outranks an update,
        because it also says updates may have been lost.
        """
        if self._closed:
            return
        unsaved = (key[0], key[1], uri)
        if unsaved in self._unsaved:
            if self._unsaved[unsaved] != EVENT_MAY_HAVE_CHANGED:
                self._unsaved[unsaved] = kind
            return
        record = self._records.get(key)
        if record is None or (uri not in record.uris and uri not in record.paused_uris):
            return
        self._emit(kind, key, uri)

    def _emit(
        self, kind: str, key: tuple[str, str], uri: str, reason: str = "", *, notify: bool = True
    ) -> None:
        try:
            self._sink(ResourceEvent(kind, key[0], key[1], uri, reason, notify=notify))
        except Exception as exc:  # noqa: BLE001 - a sink fault must not stop a holder's read loop
            logger.error(
                "resource subscriptions: event sink raised: %s",
                _tool_call_error_text({"error": str(exc)}) or "?",
                exc_info=_log_safe_exc_info(exc),
            )

    async def _holder_paused(
        self,
        key: tuple[str, str],
        reason: str,
        uris: list[str],
        *,
        holder: Optional[ResourceHolder] = None,
    ) -> bool:
        return await _finish_mutation(self._commit_pause(key, reason, uris, holder))

    async def _commit_pause(
        self,
        key: tuple[str, str],
        reason: str,
        uris: list[str],
        holder: Optional[ResourceHolder],
    ) -> bool:
        async with self._write_lock:
            if self._closed:
                return False
            if holder is not None and self._holders.get(key) is not holder:
                return False
            record = self._records.get(key)
            if record is None:
                return False
            # *uris* was read before this queued on the lock; one unsubscribed
            # meanwhile stays gone.
            uris = [uri for uri in uris if uri in record.uris]
            if not uris:
                # Nothing stored to pause; the reason still reaches a subscribe
                # that is waiting on this holder.
                if holder is not None:
                    holder.paused_reason = reason
                self._forget_holder(key, holder)
                return True
            proposed = _copy_record(record)
            proposed.uris = list(uris)
            proposed.paused_reason = reason
            try:
                await asyncio.to_thread(self._store.put, proposed)
            except (OSError, StoreUnreadable) as exc:
                logger.error(
                    "resource subscriptions: could not record a holder pause: %s",
                    _tool_call_error_text({"error": str(exc)}) or "?",
                    exc_info=_log_safe_exc_info(exc),
                )
                return False
            self._records[key] = proposed
            self._forget_holder(key, holder)
        for index, uri in enumerate(uris):
            self._emit(EVENT_PAUSED, key, uri, reason, notify=index == 0)
        return True

    def _forget_holder(self, key: tuple[str, str], holder: Optional[ResourceHolder]) -> None:
        """Drop *key*'s holder entry only while it is still *holder*."""
        if holder is None or self._holders.get(key) is holder:
            self._holders.pop(key, None)

    async def _uri_refused(
        self,
        key: tuple[str, str],
        uri: str,
        reason: str,
        *,
        holder: Optional[ResourceHolder] = None,
    ) -> bool:
        return await _finish_mutation(self._commit_refusal(key, uri, reason, holder))

    async def _commit_refusal(
        self,
        key: tuple[str, str],
        uri: str,
        reason: str,
        holder: Optional[ResourceHolder],
    ) -> bool:
        async with self._write_lock:
            if self._closed:
                return False
            if holder is not None and self._holders.get(key) is not holder:
                return False
            record = self._records.get(key)
            if record is None:
                return False
            if uri not in record.uris and uri not in record.paused_uris:
                # Unsubscribed while the refusal queued: nothing is left to pause.
                return True
            proposed = _copy_record(record)
            if uri in proposed.uris:
                proposed.uris.remove(uri)
            proposed.paused_uris[uri] = reason
            try:
                await asyncio.to_thread(self._store.put, proposed)
            except (OSError, StoreUnreadable) as exc:
                logger.error(
                    "resource subscriptions: could not record a refused URI: %s",
                    _tool_call_error_text({"error": str(exc)}) or "?",
                    exc_info=_log_safe_exc_info(exc),
                )
                return False
            self._records[key] = proposed
        self._emit(EVENT_PAUSED, key, uri, reason)
        return True


# -- process-wide handle ------------------------------------------------------

_ACTIVE: Optional[ResourceSubscriptionService] = None


def set_active_service(service: Optional[ResourceSubscriptionService]) -> None:
    """Install the service the broker started, or clear it on broker stop."""
    global _ACTIVE
    _ACTIVE = service


def active_service() -> Optional[ResourceSubscriptionService]:
    return _ACTIVE


async def start_active_service(
    socket_path: Callable[[], Optional[Path]],
    settings: Callable[[], Settings],
    *,
    session_is_closed: SessionClosedCheck = lambda _session_key: False,
    backend_spawn_wait_secs: float = HANDSHAKE_TIMEOUT_SECS,
) -> ResourceSubscriptionService:
    """Start the process's service on the broker's socket, with an :class:`EventStream`.

    Phase 3 consumes the stream through ``active_service().sink``.
    """
    service = ResourceSubscriptionService(
        store=default_store(),
        socket_path=socket_path,
        sink=EventStream(),
        settings=settings,
        session_is_closed=session_is_closed,
        probe_timeout_secs=backend_spawn_wait_secs,
    )
    set_active_service(service)
    try:
        await service.start()
    except BaseException:
        set_active_service(None)
        await service.close()
        raise
    return service


async def stop_active_service() -> None:
    """Drop every holder on broker stop; the record stays for the next start."""
    service = _ACTIVE
    if service is not None:
        await service.close()
        if _ACTIVE is service:
            set_active_service(None)


async def retire_session(session_key: str, *, keep: Optional[Callable[[str], bool]] = None) -> int:
    """§4.7: drop *session_key*'s subscriptions, live service or not.

    Fail-soft: a session close must not fail on this. When the store cannot be
    written the holders and in-memory records still go, the error is logged, 0
    is returned, and the session is kept in :data:`_UNRETIRED` so no later start
    restores it.
    """
    if not session_key:
        return 0
    try:
        service = _ACTIVE
        if service is not None:
            return await service.remove_session(session_key, keep=keep)
        store = default_store()

        async def remove_stored_session() -> int:
            if keep is not None and keep(session_key):
                return 0
            try:
                removed = await asyncio.to_thread(store.remove, session_key)
            except (OSError, StoreUnreadable):
                _UNRETIRED.add(session_key)
                raise
            _UNRETIRED.discard(session_key)
            return removed

        return await _finish_mutation(remove_stored_session())
    except (OSError, StoreUnreadable) as exc:
        _UNRETIRED.add(session_key)
        logger.error(
            "resource subscriptions: could not retire %s: %s",
            _log_safe_identifier(session_key),
            _tool_call_error_text({"error": str(exc)}) or "?",
            exc_info=_log_safe_exc_info(exc),
        )
        return 0


async def retire_sessions_with_stems(
    stems: Iterable[str], *, keep: Optional[Callable[[str], bool]] = None
) -> int:
    """§4.7 for the trash and delete: drop sessions whose transcript stems went.

    *keep* names a session key to leave alone, such as one a live runtime owns.
    """
    from kiro_crew.history_files import transcript_stems  # history imports stay call-time

    trashed = set(stems)
    if not trashed:
        return 0
    service = _ACTIVE
    if service is not None:
        return await service.remove_sessions_with_stems(trashed, keep)
    try:
        store = default_store()
        records = await asyncio.to_thread(store.load)
    except (OSError, StoreUnreadable) as exc:
        logger.error(
            "resource subscriptions: could not read the store to prune trashed sessions: %s",
            _tool_call_error_text({"error": str(exc)}) or "?",
            exc_info=_log_safe_exc_info(exc),
        )
        return 0
    keys = {r.session_key for r in records if trashed.intersection(transcript_stems(r.session_key))}
    removed = 0
    for key in sorted(keys):
        removed += await retire_session(key, keep=keep)
    return removed


#: Bound on how long a session-ending route waits for :func:`retire_session` or
#: :func:`retire_sessions_with_stems`. A retire queues behind a subscribe that
#: is waiting up to ``HANDSHAKE_TIMEOUT_SECS`` for its holder, so the removal
#: keeps running past this; the route does not wait longer.
RETIRE_WAIT_SECS = 5.0


async def retire_within_bound(
    retire: Awaitable[int], what: str, *, timeout: Optional[float] = None
) -> None:
    """Await *retire* for at most :data:`RETIRE_WAIT_SECS` (or *timeout*), shielded.

    Never raises for time. A removal still running at the bound, or when the
    caller is cancelled, keeps running, and if it then fails, the failure is
    logged rather than left unretrieved. The task is referenced from the start
    and released when it ends, so it cannot be collected while it runs.
    """
    task = asyncio.ensure_future(retire)
    _LATE_RETIRES.add(task)
    abandoned = False
    logged = False

    def log_once(done: "asyncio.Future[int]") -> None:
        # At the timeout or cancellation boundary the task can finish with its
        # done callback still queued; both sides then see it abandoned and done.
        nonlocal logged
        if not logged:
            logged = True
            _log_late_retire_failure(done, what)

    def on_done(done: "asyncio.Future[int]") -> None:
        _LATE_RETIRES.discard(done)
        if abandoned:
            log_once(done)

    task.add_done_callback(on_done)
    try:
        await asyncio.wait_for(
            asyncio.shield(task), RETIRE_WAIT_SECS if timeout is None else timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "resource subscriptions of %s are still being removed",
            _log_safe_identifier(what),
        )
        abandoned = True
        if task.done():
            log_once(task)
    except asyncio.CancelledError:
        abandoned = True
        if task.done():
            log_once(task)
        raise


#: Removals :func:`retire_within_bound` started that are still running, held so
#: nothing collects them early.
_LATE_RETIRES: set["asyncio.Future[int]"] = set()


def _log_late_retire_failure(task: "asyncio.Future[int]", what: str) -> None:
    _LATE_RETIRES.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "resource subscriptions of %s: removal failed after the wait ended: %s",
            _log_safe_identifier(what),
            _tool_call_error_text({"error": str(exc)}) or "?",
            exc_info=_log_safe_exc_info(exc),
        )
