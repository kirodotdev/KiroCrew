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

    The offload invariant (see the ``_locked`` contract and
    ``docs/system-specs/modules/history.md``) is that NO session-JSONL mutator
    runs on the gateway event loop: on-loop callers route through
    ``append_off_loop`` / ``append_if_absent_off_loop`` /
    ``append_rows_if_absent_off_loop`` / ``update_metadata_off_loop`` (here), or
    ``dashboard.chat_persistence.save_slot_off_loop`` (or ``asyncio.to_thread``),
    all of which dispatch
    the mutation to a worker thread so ``_locked`` runs OFF the loop and takes
    the patient acquire path. A raw on-loop mutator call works in every low-
    traffic test and use (the flock is uncontended) and only loses data under
    real concurrency — where the on-loop path makes a single non-blocking
    acquire, raises :class:`HistoryLockTimeout`, and the caller's best-effort
    ``try/except`` swallows it as silent transcript loss.

    Because that failure is invisible in CI, ``_locked`` fails LOUD instead:
    when strict enforcement is active (:func:`_on_loop_persist_strict` — on under
    ``KIROCREW_STRICT_ON_LOOP_PERSIST`` or ``KIROCREW_DEV_MODE``) any on-loop
    entry raises this error so an un-offloaded call-site fails tests rather than
    losing data in production. In production (strict off) the same on-loop entry
    is instead logged loudly (throttled) and proceeds via the single non-blocking
    safety-net acquire. Tests that deliberately exercise the low-level on-loop
    primitive wrap the call in :func:`allow_on_loop_persist`.
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

    Strict enforcement turns the convention-only offload discipline into a hard,
    test-catchable failure. It is on when EITHER:

    - ``KIROCREW_STRICT_ON_LOOP_PERSIST`` is truthy (explicit opt-in — the knob
      the discipline-enforcement tests flip, and a CI/dev harness can export to
      make an un-offloaded call-site fail instead of ship), or
    - ``KIROCREW_DEV_MODE`` is truthy (developer gateway runs — where all
      persistence is already offloaded, so a raise means a real newly-added
      un-offloaded path, surfaced immediately rather than lost under contention).

    A truthy ``KIROCREW_STRICT_ON_LOOP_PERSIST`` wins; an explicit falsy value
    force-disables even under dev-mode (so the production-fallback path stays
    testable). It is deliberately NOT auto-on under bare pytest: the suite's own
    async harness calls several mutators directly on the loop as a convenience
    (not a production path), so auto-strict would flag harness code, not drift.
    The default (no flag) is the production behavior — a loud throttled warning
    plus the non-blocking safety-net acquire — so shipping never introduces a new
    hard failure in the field.
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

    The lock-backed :meth:`ConversationLog.append` acquires a cross-process
    flock and writes to disk. On the gateway event loop that primitive makes a
    single non-blocking acquire attempt and raises :class:`HistoryLockTimeout`
    on *any* concurrent holder (see the ``_locked`` contract), so calling it
    directly from an async context both risks a disk write on the loop and drops
    the write under benign contention.

    This helper routes around both problems: on a running asyncio loop the
    append is dispatched to a worker thread (``run_in_executor``) so it takes the
    patient off-loop acquire path and never stalls the loop; off the loop it
    appends inline. Persistence is best-effort — the caller has already reflected
    the message in the in-memory slot, so a lock timeout or I/O error only skips
    the durable replay copy and is logged rather than raised.
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

    :func:`append_if_absent_off_loop` dispatches each row as its own executor
    task, so a caller writing a prompt+result PAIR hands two worker threads two
    independent writes: they can land out of order, and one can fail while the
    other succeeds. The transcript then replays a run whose rows are reversed or
    half-present, and no timestamp ordering repairs it because each row's ``ts``
    is correct on its own.

    This routes the whole group through ONE task holding
    :meth:`ConversationLog.atomic_appends`, whose contract names this hazard as
    the companion a multi-append caller needs precisely BECAUSE it moved the
    write off the loop. ``_locked`` is reentrant per key per thread, so the
    per-row locks inside ``append_if_absent`` reuse the hold rather than
    deadlocking on it.

    *rows* is an ordered sequence of ``(role, content, cls, mid)``; they are
    appended in that order. Each row keeps ``append_if_absent``'s idempotence,
    so a row the periodic slot save already serialized is skipped individually
    without dropping its siblings. ``row_meta``, when given, is aligned with
    *rows* by index and adds display fields to that row's ``meta``
    (see :meth:`ConversationLog.append`).

    Returns the executor future, or None when the write already happened inline
    (no running loop). Best-effort like its siblings: a lock timeout or I/O
    error only skips the durable replay copy the slot already carries.
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

    Returns the executor future for the scheduled write, or None when the write
    already happened inline (no running loop). A caller holding the ONLY durable
    copy of something must await that future: scheduling is not durability.

    Routes :meth:`ConversationLog.append_if_absent` — which atomically skips a
    message already persisted under the same session lock — off the event loop
    exactly like :func:`append_off_loop`. Used by the workflow-result and
    cron-result injectors, which ALSO reflect the message in the in-memory slot
    (``slot.append``); the periodic slot save can therefore serialize the same
    message before this durable copy runs. The plain ``append`` would then
    double-write it; ``append_if_absent`` performs the disk check under the
    lock so the second write is a no-op. Best-effort — a lock timeout / I/O
    error only skips the durable replay copy (the slot already carries it) and
    is logged rather than raised.
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

    Mirrors :func:`append_off_loop`, but for the lock-backed
    :meth:`ConversationLog.update_metadata`. That method enters ``_locked``,
    which ``os.open``\\ s the sidecar, takes a cross-process ``flock`` and
    ``os.close``\\ s the fd. Those primitives are ``blocking: true`` under the
    ``no-blocking-call-on-event-loop`` rule: a wedged cross-process peer can
    stall the acquire (or, mid-release, the close) and freeze chat, WebSockets,
    and heartbeat until the watchdog restarts the gateway. This helper keeps
    them off the loop.

    Use it from *synchronous* helpers that may run on the event-loop thread
    (e.g. slot rehydration / startup restore) where ``await`` is not available.
    Async handlers that can ``await`` should prefer
    ``await asyncio.to_thread(conversation_log.update_metadata, ...)`` so the
    write is ordered against the response.

    On a running loop the update is dispatched to a worker thread (which takes
    ``_locked``'s patient off-loop acquire path); off the loop it runs inline.
    Persistence is best-effort — the mutation is metadata backfill the caller
    has already reflected in memory, so a lock timeout or I/O error is logged
    rather than raised.
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
