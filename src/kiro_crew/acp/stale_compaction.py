"""Track terminal statuses from compactions whose ownership is ambiguous.

An idle ``/compact`` can end without a terminal status after Crew cancels and
briefly drains it. The harness can still send that status later. Every status
reader discards compaction statuses while unresolved ownership exists. Each
discarded terminal lowers the per-reader count by one.

Crew refuses another compaction on a provider until its count reaches zero.
This keeps an ambiguous status away from every Crew compaction result. Before
a provider entry point refuses, it consumes the owed statuses that already wait
on an idle reader, without blocking. A refused ``compact()`` sends nothing, and
the next ``wait_for_compaction`` reports one plain ``failed`` result, so a
caller does not treat the refusal as a broken session. A refused
``stream_command("/compact")`` raises ``AcpError``.

While a reader has a positive count, each provider stream invalidates its
essential delivery first, because the stream can consume a late ``completed``
after the receipt check. An ownerless status can belong to a co-tenant, but it
pays this handle's debt when the count is positive because the frame has no
session identifier. This can end the debt early. With a zero count, ownerless
frames keep their normal behavior. A reader whose process is replaced forgets
its debt, because the new process cannot send the old terminal. The count has
constant storage, has no time expiry, and retains no request data.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

UNRESOLVED_COMPACTION_ERROR = (
    "An earlier compaction on this session has not reported its result yet, so /compact is "
    "not available until it does."
)

_ATTR = "_unresolved_compaction_statuses"
_WARNING_ATTR = "_unresolved_compaction_warning_pending"
_DELIVERY_GENERATION_ATTR = "_stale_compaction_delivery_generation"
_DELIVERY_SEEN_ATTR = "_stale_compaction_delivery_seen"
_REINJECTION_SEEN_ATTR = "_stale_compaction_reinjection_seen"
_REFUSAL_ATTR = "_stale_compaction_refused"
_TERMINAL = ("completed", "failed")
#: Attributes a provider reaches its status reader through: ``AcpProvider``'s
#: ``_client`` (an ``AcpClient``, or the ``AcpSessionProvider`` the kiro path
#: swaps in) and ``AcpSessionProvider``'s ``_handle``.
_LINKS = ("_client", "client", "_handle")
_MAX_DEPTH = 3


def _unresolved(target: Any) -> int:
    value = getattr(target, _ATTR, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and value > 0:
        return max(1, int(value))
    return 0


def _set_unresolved(target: Any, count: int) -> None:
    setattr(target, _ATTR, count if count > 0 else None)
    if count <= 0:
        setattr(target, _WARNING_ATTR, None)


def discard_stale_compaction_status(target: Any, *, within_secs: float) -> None:
    """Record one unresolved terminal status on *target*.

    ``within_secs`` remains in the call contract because the idle coordinator
    supplies its compaction budget. Elapsed time cannot resolve ownership, so
    the count remains until a reader discards a terminal status.
    """
    del within_secs
    count = _unresolved(target)
    _set_unresolved(target, count + 1)
    if count == 0:
        setattr(target, _WARNING_ATTR, True)


def _readers(provider: Any) -> Iterator[Any]:
    """The ``AcpClient`` or ``AcpSessionHandle`` objects behind *provider*."""
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.session_handle import AcpSessionHandle

    seen: set[int] = set()
    frontier = [provider]
    for _ in range(_MAX_DEPTH + 1):
        nxt: list[Any] = []
        for obj in frontier:
            if obj is None or id(obj) in seen:
                continue
            seen.add(id(obj))
            if isinstance(obj, (AcpClient, AcpSessionHandle)):
                yield obj
                continue
            nxt.extend(getattr(obj, name, None) for name in _LINKS)
        frontier = nxt


def discard_provider_stale_compaction_status(provider: Any, *, within_secs: float) -> bool:
    """Record one unresolved terminal on each status reader behind *provider*."""
    marked = False
    for target in _readers(provider):
        discard_stale_compaction_status(target, within_secs=within_secs)
        marked = True
    return marked


def provider_has_unresolved_compaction_status(provider: Any) -> bool:
    """Return whether a status reader behind *provider* has unresolved ownership."""
    return any(_unresolved(target) > 0 for target in _readers(provider))


def take_provider_unresolved_warning(provider: Any) -> bool:
    """Return True once when a provider first refuses a debt's compaction."""
    readers = list(_readers(provider))
    warn = any(
        _unresolved(target) > 0 and getattr(target, _WARNING_ATTR, False) is True
        for target in readers
    )
    if warn:
        for target in readers:
            setattr(target, _WARNING_ATTR, None)
    return warn


def sync_provider_compaction_delivery(provider: Any, delivery: Any) -> bool:
    """Invalidate *delivery* before a stream when native history can have changed.

    A reader with a positive count can consume a late ``completed`` after this
    check, for example in the session handle's pre-turn drain, so every stream
    on that reader resends the complete envelope. A reader whose delivery
    generation differs from the one this provider last saw consumed a
    ``completed`` after an earlier stream started. Each dispatch also ends a
    refusal that no ``wait_for_compaction`` collected.
    """
    setattr(provider, _REFUSAL_ATTR, None)
    unresolved, changed = _note_reader_state(
        provider, _DELIVERY_SEEN_ATTR, list(_readers(provider))
    )
    if not (unresolved or changed):
        return False
    delivery.invalidate()
    return True


def take_provider_context_reinjection(provider: Any) -> bool:
    """Whether the next prompt on *provider* must carry the session's startup context.

    True while a reader behind *provider* owes a terminal: the reader can consume
    a late ``completed`` after the prompt is built, for example in the session
    handle's pre-turn drain. True once more after a reader dropped a
    ``completed`` this provider has not answered yet. A dropped status reaches
    no session, so the session's own reinjection flag never sees it. A provider
    with no ACP status reader answers False and is left untouched.
    """
    readers = list(_readers(provider))
    if not readers:
        return False
    unresolved, changed = _note_reader_state(provider, _REINJECTION_SEEN_ATTR, readers)
    return unresolved or changed


def _note_reader_state(provider: Any, seen_attr: str, readers: list[Any]) -> tuple[bool, bool]:
    """Whether a reader owes a terminal, and whether a delivery generation moved.

    The second answer compares each reader's generation with the one *provider*
    recorded under *seen_attr*, then records the current ones there.
    """
    current = tuple(
        (target, int(getattr(target, _DELIVERY_GENERATION_ATTR, 0) or 0)) for target in readers
    )
    seen = getattr(provider, seen_attr, ())
    if not isinstance(seen, tuple):
        seen = ()

    def _seen_generation(target: Any) -> int:
        for prior_target, generation in seen:
            if prior_target is target and isinstance(generation, int):
                return generation
        return 0

    unresolved = any(_unresolved(target) > 0 for target, _ in current)
    changed = any(generation != _seen_generation(target) for target, generation in current)
    setattr(provider, seen_attr, current)
    return unresolved, changed


def clear_stale_compaction_state(target: Any) -> None:
    """Forget the debt of a reader whose process was replaced.

    The replacement process cannot send the earlier process's terminal, so the
    count, the warning bit and the delivery generation start again. A cleared
    nonzero generation reads as a change, so each provider invalidates its
    delivery once more.
    """
    setattr(target, _ATTR, None)
    setattr(target, _WARNING_ATTR, None)
    setattr(target, _DELIVERY_GENERATION_ATTR, None)


def _reader_idle(target: Any) -> bool:
    """Whether no turn reads *target*, so its queued frames wait for no reader."""
    is_set = getattr(getattr(target, "_turn_done", None), "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _consume_queued_on_handle(target: Any) -> None:
    """Consume owed statuses in a session handle's queue, keeping other frames in order."""
    queue = getattr(target, "_queue", None)
    if not isinstance(queue, asyncio.Queue):
        return
    kept: list[Any] = []
    while True:
        try:
            frame = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if frame is not None and consume_stale_compaction_status(target, frame):
            continue
        kept.append(frame)
    for frame in kept:
        queue.put_nowait(frame)


def _consume_buffered_on_client(target: Any) -> None:
    """Consume owed statuses an ``AcpClient`` buffered, keeping other frames in order.

    The replay buffer goes first because the next reader reads it first. The
    notification list carries index floors, so each floor falls by the frames
    removed below it and still marks the same frame.
    """
    buffer = getattr(target, "_buffer", None)
    if buffer:
        kept = [frame for frame in buffer if not consume_stale_compaction_status(target, frame)]
        if len(kept) != len(buffer):
            buffer.clear()
            buffer.extend(kept)
    notes = getattr(target, "_mcp_notifications", None)
    if not isinstance(notes, list) or not notes:
        return
    floors = {
        name: int(getattr(target, name, 0) or 0)
        for name in ("_mcp_report_frame_floor", "_routing_answer_floor")
    }
    removed = dict.fromkeys(floors, 0)
    kept_notes: list[Any] = []
    for idx, frame in enumerate(notes):
        if consume_stale_compaction_status(target, frame):
            for name, floor in floors.items():
                removed[name] += idx < floor
            continue
        kept_notes.append(frame)
    if len(kept_notes) == len(notes):
        return
    notes[:] = kept_notes
    for name, floor in floors.items():
        if removed[name]:
            setattr(target, name, max(0, floor - removed[name]))


def provider_refuses_compaction(provider: Any) -> bool:
    """Return whether a compaction on *provider* must be refused.

    Owed statuses that already wait on an idle reader are consumed first,
    without blocking, so a terminal that arrived between turns ends its debt
    before the check. A reader with a turn in progress keeps its frames for
    that turn, which consumes them in order. Statuses that are still in the
    process pipe, or that a pending request holds, are not read here.
    """
    from kiro_crew.acp.session_handle import AcpSessionHandle

    for target in _readers(provider):
        if _unresolved(target) <= 0 or not _reader_idle(target):
            continue
        if isinstance(target, AcpSessionHandle):
            _consume_queued_on_handle(target)
        else:
            _consume_buffered_on_client(target)
    return provider_has_unresolved_compaction_status(provider)


def refuse_provider_compaction(provider: Any) -> bool:
    """Return True and hold one ``failed`` result when ``compact()`` must not run.

    The caller sends nothing. Its next ``wait_for_compaction`` returns the
    result from :func:`take_provider_compaction_refusal`, so a caller that
    follows ``compact()`` with that wait reports a plain failure.
    """
    refused = provider_refuses_compaction(provider)
    setattr(provider, _REFUSAL_ATTR, True if refused else None)
    if refused:
        logger.info("Refused /compact because an earlier compaction status is unresolved")
    return refused


def take_provider_compaction_refusal(provider: Any) -> dict[str, str] | None:
    """Return the held refusal result once, or None when ``compact()`` ran."""
    if getattr(provider, _REFUSAL_ATTR, None) is not True:
        return None
    setattr(provider, _REFUSAL_ATTR, None)
    return {"type": "failed", "summary": UNRESOLVED_COMPACTION_ERROR}


def _frame_status_type(frame: Any) -> str | None:
    if getattr(frame, "method", None) != "_kiro.dev/compaction/status":
        return None
    params = getattr(frame, "params", None)
    params = params if isinstance(params, dict) else {}
    status = params.get("status", {})
    return status.get("type", "") if isinstance(status, dict) else str(status)


def consume_stale_compaction_status(target: Any, frame: Any) -> bool:
    """Account for and discard an owed compaction status *frame*.

    A dropped ``completed`` still proves that native history changed, so it
    records a generation that each provider delivery layer consumes before its
    next stream. Only an owned completion resets the reader's context
    statistics and failure budget. A dropped ``failed`` changes no turn state.
    """
    status_type = _frame_status_type(frame)
    if status_type is None or not is_stale_compaction_status(target, status_type):
        return False
    if status_type == "completed":
        # Dispatch applies its ownership guard after this helper, so only owned
        # frames can reset session-local meter and failure state here.
        if getattr(frame, "fanout_no_owner", False) is not True:
            target._compaction_failed_at = None
            reset = getattr(
                getattr(target, "last_prompt_stats", None), "reset_after_compaction", None
            )
            if callable(reset):
                reset()
            # The summarized conversation dropped its pictures too, so a later
            # prompt naming one must send it again.
            forget = getattr(target, "_forget_sent_images", None)
            if callable(forget):
                forget()
        generation = int(getattr(target, _DELIVERY_GENERATION_ATTR, 0) or 0)
        setattr(target, _DELIVERY_GENERATION_ATTR, generation + 1)
    return True


def is_compact_command(command: str) -> bool:
    """Whether *command* is a ``/compact`` slash command, with or without arguments."""
    head = command.split(maxsplit=1)[:1]
    return head == ["/compact"]


def is_stale_compaction_status(target: Any, status_type: str) -> bool:
    """Whether a compaction status on *target* must be discarded.

    A normal reader discards every status while ownership is unresolved. Each
    discarded terminal resolves one abandoned compaction. Non-terminal statuses
    leave the count unchanged.
    """
    count = _unresolved(target)
    if count <= 0:
        return False
    remaining = count
    if status_type in _TERMINAL:
        remaining -= 1
        _set_unresolved(target, remaining)
    logger.info(
        "Dropped a late compaction status %r; %d terminal status(es) unresolved",
        status_type,
        remaining,
    )
    return True
