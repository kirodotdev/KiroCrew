"""The process's active resource subscription service, and retiring a session through it.

``rfc-mcp-resource-subscriptions.md`` §4.7. The broker installs the one
:class:`~kiro_crew.mcp_gateway.resource_subscriptions.ResourceSubscriptionService`
(:func:`start_active_service`, :func:`stop_active_service`); a session close, trash or
delete drops that session's subscriptions through it, or straight from the store when
no service runs (:func:`retire_session`, :func:`retire_sessions_with_stems`), within
:data:`RETIRE_WAIT_SECS` (:func:`retire_within_bound`). ``_ACTIVE`` lives here with every
function that rebinds or reads it, so one binding serves the whole family.

An owner of :mod:`kiro_crew.mcp_gateway.resource_subscriptions`, which stays the import
path and the patch surface. What this module reads from that facade (the service class,
``default_store``, ``_UNRETIRED`` and the mutation helpers) it looks up at call time,
so a patch of the facade reaches it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, Optional

from kiro_crew.mcp_gateway.backend_wire import _log_safe_identifier, _tool_call_error_text
from kiro_crew.mcp_gateway.resource_holder import HANDSHAKE_TIMEOUT_SECS
from kiro_crew.mcp_gateway.resource_subscription_store import StoreUnreadable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.mcp_gateway.resource_subscriptions import (
        EventSink,
        ResourceSubscriptionService,
        SessionClosedCheck,
        Settings,
    )

# Logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.mcp_gateway.resource_subscriptions")

_FACADE = "kiro_crew.mcp_gateway.resource_subscriptions"


def _facade() -> ModuleType:
    """The facade, read from ``sys.modules`` when called: the owner's patch target.

    Looked up, never imported: the facade imports this module, so it is loaded
    before any function here runs.
    """
    return sys.modules[_FACADE]


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
    sink: Callable[[ResourceSubscriptionService], EventSink],
    *,
    session_is_closed: SessionClosedCheck = lambda _session_key: False,
    backends_shared: Callable[[], bool] = lambda: True,
    backend_spawn_wait_secs: float = HANDSHAKE_TIMEOUT_SECS,
) -> ResourceSubscriptionService:
    """Start the process's service on the broker's socket.

    *sink* is called with the service before it starts and answers the
    :data:`EventSink` every event of that service goes to, so a sink that needs
    the service (the wake dispatcher) receives the first event a restore emits.
    """
    rs = _facade()
    service = rs.ResourceSubscriptionService(
        store=rs.default_store(),
        socket_path=socket_path,
        sink=lambda _event: None,
        settings=settings,
        session_is_closed=session_is_closed,
        backends_shared=backends_shared,
        probe_timeout_secs=backend_spawn_wait_secs,
    )
    # Bound before anything can emit: nothing does until ``start``.
    service._sink = sink(service)
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
    rs = _facade()
    try:
        service = _ACTIVE
        if service is not None:
            removed = await service.remove_session(session_key, keep=keep)
            if removed or not service.closed:
                return removed
            # A closed service never writes the store, so retire the record
            # directly or the next start would restore it.
        store = rs.default_store()

        async def remove_stored_session() -> int:
            if keep is not None and keep(session_key):
                return 0
            try:
                removed = await asyncio.to_thread(store.remove, session_key)
            except (OSError, StoreUnreadable):
                rs._UNRETIRED.add(session_key)
                raise
            rs._UNRETIRED.discard(session_key)
            return removed

        removed = await rs._finish_mutation(remove_stored_session())
        started_meanwhile = _ACTIVE
        if started_meanwhile is not None and started_meanwhile is not service:
            # A service installed while the store removal ran may have loaded the
            # record before it committed, and its start would restore it. Its
            # remove_session waits on that start's lock, then drops what it loaded.
            # Both counts are the same records, so the larger one is the answer.
            removed = max(removed, await started_meanwhile.remove_session(session_key, keep=keep))
        return removed
    except (OSError, StoreUnreadable) as exc:
        rs._UNRETIRED.add(session_key)
        logger.error(
            "resource subscriptions: could not retire %s: %s",
            _log_safe_identifier(session_key),
            _tool_call_error_text({"error": str(exc)}) or "?",
            exc_info=rs._log_safe_exc_info(exc),
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
        removed = await service.remove_sessions_with_stems(trashed, keep)
        if removed or not service.closed:
            return removed
    rs = _facade()
    try:
        store = rs.default_store()
        records = await asyncio.to_thread(store.load)
    except (OSError, StoreUnreadable) as exc:
        logger.error(
            "resource subscriptions: could not read the store to prune trashed sessions: %s",
            _tool_call_error_text({"error": str(exc)}) or "?",
            exc_info=rs._log_safe_exc_info(exc),
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
            exc_info=_facade()._log_safe_exc_info(exc),
        )
