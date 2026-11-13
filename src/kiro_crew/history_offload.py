"""Keep session-file mutations off the event loop.

The on-loop persistence discipline ``ConversationLog._locked`` enforces (strict in dev
and under the opt-in knob, a throttled warning otherwise), and the off-loop dispatchers
an on-loop caller persists through.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import time as _time
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: Moved code logs under the facade's own logger name.
logger = logging.getLogger("kiro_crew.history")


class OnLoopPersistError(AssertionError):
    """A session-file mutation entered :meth:`ConversationLog._locked` directly
    on the event loop, violating the off-loop persistence discipline.

    No session-JSONL mutator may run on the event loop (see the ``_locked``
    contract and ``docs/system-specs/modules/history.md``): on-loop callers use
    the ``*_off_loop`` helpers here, ``dashboard.chat_persistence.save_slot_off_loop``
    or ``asyncio.to_thread``. A raw on-loop call works while the flock is
    uncontended, but under real concurrency its single non-blocking acquire
    raises :class:`HistoryLockTimeout` and a best-effort ``try/except`` swallows
    it as silent transcript loss.

    So under strict enforcement (:func:`_on_loop_persist_strict`) an on-loop entry
    raises this, failing tests instead of losing data. In production it is logged
    (throttled) and proceeds via the non-blocking acquire. Tests that exercise the
    on-loop primitive on purpose wrap it in :func:`allow_on_loop_persist`.
    """


# When True (default in production), on-loop entry into ``_locked`` is a logged
# warning + single non-blocking acquire. When strict enforcement is active it
# raises :class:`OnLoopPersistError` instead. A ``ContextVar`` (async-safe, no
# cross-task bleed) lets the low-level primitive tests opt a single call out.
_allow_on_loop_persist: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kirocrew_allow_on_loop_persist", default=False
)

# Throttle the production on-loop warning so a mis-wired hot path cannot flood
# the log; the diagnostic still fires once per window per process.
_ON_LOOP_WARN_INTERVAL_S = 60.0
_on_loop_warn_last: float = 0.0


@contextlib.contextmanager
def allow_on_loop_persist() -> Iterator[None]:
    """Suppress the strict on-loop persistence assertion for the current context.

    Reserved for tests (and any genuinely-vetted call-site) that intentionally
    drive a session mutator directly on the event loop to exercise the
    low-level ``_locked`` primitive — e.g. the HistoryLockTimeout fail-fast
    path. Production code must NEVER use this: it must offload instead.
    """
    token = _allow_on_loop_persist.set(True)
    try:
        yield
    finally:
        _allow_on_loop_persist.reset(token)


def _on_loop_persist_strict() -> bool:
    """Whether an on-loop ``_locked`` entry should RAISE (vs. warn-and-proceed).

    On when ``KIROCREW_STRICT_ON_LOOP_PERSIST`` is truthy (the knob enforcement
    tests and CI flip) or ``KIROCREW_DEV_MODE`` is truthy (dev gateways, where all
    persistence is already offloaded, so a raise is a real new path). An explicit
    falsy ``KIROCREW_STRICT_ON_LOOP_PERSIST`` wins over dev mode, keeping the
    production fallback testable. Not auto-on under pytest: the async harness
    calls some mutators on the loop as a convenience. The default is the
    production warn-and-proceed, so shipping adds no new hard failure.
    """
    explicit = os.environ.get("KIROCREW_STRICT_ON_LOOP_PERSIST", "").strip().lower()
    if explicit in _ON_LOOP_TRUTHY:
        return True
    if explicit in _ON_LOOP_FALSY:
        return False
    return os.environ.get("KIROCREW_DEV_MODE", "").strip().lower() in _ON_LOOP_TRUTHY


_ON_LOOP_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ON_LOOP_FALSY = frozenset({"0", "false", "no", "off"})


def on_loop_persist_strict() -> bool:
    """Public alias of :func:`_on_loop_persist_strict` for other modules.

    The strictness knob (``KIROCREW_STRICT_ON_LOOP_PERSIST`` /
    ``KIROCREW_DEV_MODE``) governs the on-loop persistence discipline for every
    store, not just this module's conversation log; consumers that enforce the
    same offload rule on their own SQLite databases (e.g. the auto_research
    campaigns DB) read the shared setting through this alias instead of
    importing a private name.
    """
    return _on_loop_persist_strict()


def _check_on_loop_persist_discipline(key: str) -> None:
    """Enforce (strict) or diagnose (production) an on-loop ``_locked`` entry.

    Called at the top of :meth:`ConversationLog._locked`. If there is no running
    event loop the caller is already off-loop (worker thread / CLI / subagent /
    cron) and this is a no-op — the common, correct case. On the loop it either
    raises :class:`OnLoopPersistError` (strict) or logs a throttled warning
    (production) so the un-offloaded call-site is never silent.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # off-loop: the sanctioned path — nothing to flag
    if _allow_on_loop_persist.get():
        return  # explicitly-vetted low-level primitive exercise (tests)
    if _on_loop_persist_strict():
        raise OnLoopPersistError(
            f"session mutation for {key!r} entered _locked on the event loop; "
            f"on-loop callers MUST offload (history.append_off_loop / "
            f"append_if_absent_off_loop / append_rows_if_absent_off_loop / "
            f"update_metadata_off_loop, dashboard.chat_persistence."
            f"save_slot_off_loop, or asyncio.to_thread) so the write takes the "
            f"patient off-loop acquire path — a raw on-loop mutation loses data "
            f"under real contention (HistoryLockTimeout swallowed as silent "
            f"transcript loss). Wrap in history.allow_on_loop_persist() only to "
            f"test the low-level primitive itself."
        )
    global _on_loop_warn_last
    now = _time.monotonic()
    if now - _on_loop_warn_last >= _ON_LOOP_WARN_INTERVAL_S:
        _on_loop_warn_last = now
        logger.warning(
            "history: session mutation for %s ran _locked ON the event loop "
            "without offloading; this drops the write under real contention "
            "(HistoryLockTimeout swallowed by best-effort callers). Route it "
            "through append_off_loop/save_slot_off_loop/asyncio.to_thread.",
            key,
            stack_info=True,
        )


def append_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    role: str,
    content: str,
    *,
    agent: str | None = None,
) -> None:
    """Persist a message without blocking (or fail-fast-dropping on) the loop.

    On the event loop :meth:`ConversationLog.append` makes one non-blocking flock
    attempt and raises :class:`HistoryLockTimeout` on any holder, so a direct
    call writes on the loop and drops the row under benign contention. On a
    running loop this dispatches to a worker thread (``run_in_executor``) for the
    patient acquire; off the loop it appends inline. Best-effort: the message is
    already in the in-memory slot, so a failure is logged, not raised.
    """

    def _do() -> None:
        conversation_log.append(key, role, content, agent=agent)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:  # noqa: BLE001 - best-effort durable copy
            logger.warning("append_off_loop: inline append failed key=%s", key, exc_info=True)
        return

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning("append_off_loop: offloaded append failed key=%s: %r", key, exc)

    loop.run_in_executor(None, _do).add_done_callback(_report)


def append_rows_if_absent_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    rows: "Sequence[tuple[str, str, str, str | None]]",
    *,
    agent: str | None = None,
    row_meta: "Sequence[dict[str, Any] | None] | None" = None,
) -> Any:
    """Persist SEVERAL rows of one turn as one indivisible off-loop write.

    :func:`append_if_absent_off_loop` sends each row as its own executor task, so
    a prompt+result pair could land out of order or half-written, which no
    ``ts`` ordering repairs. This runs the group in ONE task holding
    :meth:`ConversationLog.atomic_appends`; ``_locked`` is reentrant per key per
    thread, so the per-row locks reuse the hold.

    *rows* is an ordered sequence of ``(role, content, cls, mid)``, appended in
    order. Each row keeps ``append_if_absent``'s idempotence, so a row the slot
    save already wrote is skipped alone. ``row_meta``, when given, aligns with
    *rows* by index and adds display fields to that row's ``meta`` (see
    :meth:`ConversationLog.append`).

    Returns the executor future, or None when written inline (no running loop).
    Best-effort like its siblings.
    """

    def _do() -> None:
        with conversation_log.atomic_appends(key):
            for i, (role, content, cls, mid) in enumerate(rows):
                extra = row_meta[i] if row_meta and i < len(row_meta) else None
                conversation_log.append_if_absent(
                    key, role, content, agent=agent, cls=cls, mid=mid, extra_meta=extra
                )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:  # noqa: BLE001 - best-effort durable copy
            logger.warning(
                "append_rows_if_absent_off_loop: inline append failed key=%s",
                key,
                exc_info=True,
            )
        return None

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "append_rows_if_absent_off_loop: offloaded append failed key=%s: %r",
                key,
                exc,
            )

    fut = loop.run_in_executor(None, _do)
    fut.add_done_callback(_report)
    return fut


def append_if_absent_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    role: str,
    content: str,
    *,
    agent: str | None = None,
    cls: str = "",
    mid: str | None = None,
) -> Any:
    """Idempotent, loop-safe variant of :func:`append_off_loop`.

    Returns the executor future, or None when written inline (no running loop).
    A caller holding the ONLY durable copy must await it: scheduling is not
    durability.

    Used by the workflow-result and cron-result injectors, which also add the
    message to the in-memory slot, so the periodic slot save may persist it
    first. :meth:`ConversationLog.append_if_absent` checks the disk under the
    lock, so the second write is a no-op. Best-effort: failures are logged.
    """

    def _do() -> None:
        conversation_log.append_if_absent(key, role, content, agent=agent, cls=cls, mid=mid)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:  # noqa: BLE001 - best-effort durable copy
            logger.warning(
                "append_if_absent_off_loop: inline append failed key=%s",
                key,
                exc_info=True,
            )
        return None

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "append_if_absent_off_loop: offloaded append failed key=%s: %r",
                key,
                exc,
            )

    fut = loop.run_in_executor(None, _do)
    fut.add_done_callback(_report)
    # Hand the future BACK: a caller holding the only durable copy awaits this
    # to turn "scheduled" into "on disk". Dropping it here made the barrier a
    # no-op on every running-loop path, i.e. every real gateway path.
    return fut


def update_metadata_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    fields: dict,
) -> None:
    """Merge session metadata without running the flock/fd ops on the loop.

    :meth:`ConversationLog.update_metadata` enters ``_locked``, whose
    ``os.open``, ``flock`` and ``os.close`` are ``blocking: true`` under the
    ``no-blocking-call-on-event-loop`` rule: a wedged cross-process peer could
    freeze chat, WebSockets and heartbeat until the watchdog restarts.

    For *synchronous* helpers that may run on the loop thread (slot rehydration,
    startup restore). Async handlers should prefer
    ``await asyncio.to_thread(conversation_log.update_metadata, ...)`` so the
    write is ordered against the response. On a running loop this dispatches to
    a worker thread; off it, it runs inline. Best-effort: failures are logged.
    """

    def _do() -> None:
        conversation_log.update_metadata(key, fields)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:  # noqa: BLE001 - best-effort metadata backfill
            logger.warning(
                "update_metadata_off_loop: inline update failed key=%s",
                key,
                exc_info=True,
            )
        return

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "update_metadata_off_loop: offloaded update failed key=%s: %r",
                key,
                exc,
            )

    loop.run_in_executor(None, _do).add_done_callback(_report)
