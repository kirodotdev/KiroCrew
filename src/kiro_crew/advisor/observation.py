"""Normalized, bounded advisor observation records and checkpoint batching.

Checkpoints are derived from facts the host owns -- finalized text segments,
completed tool results, and the turn terminal -- never from transcript
polling (edits, rewinds, compaction, and forks make JSONL polling unsafe;
see docs/system-specs/modules/advisor.md).

An :class:`AdvisorObserver` belongs to one parent session and one observation
epoch at a time. Records buffer until :meth:`AdvisorObserver.drain_update`
coalesces them into one ``in_progress=True`` :class:`ObservationUpdate`;
:meth:`AdvisorObserver.complete` emits exactly one ``in_progress=False``
final update per epoch. Lifecycle boundaries (reset, compaction, fork or
transfer, agent/model/workspace/provider switch) call
:meth:`AdvisorObserver.begin_epoch`: pending records and dedupe state never
cross an epoch boundary.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

#: Upper bound on one recorded tool payload or text segment, in characters.
#: Reviewer context is finite and the observer must stay cheap; longer
#: payloads are truncated and flagged rather than dropped.
OBSERVATION_PAYLOAD_MAX_CHARS = 4000

#: Appended to a clipped record so the reviewer reads it
#: as cut off rather than complete (tool results carry a ``truncated`` flag).
TRUNCATION_MARKER = " [... truncated]"

#: Hard cap on COMBINED records carried per epoch (category-balanced), so a
#: pathological turn cannot grow the reviewer prompt without bound.
EPOCH_MAX_RECORDS = 40

_epoch_counter = itertools.count(1)


def _next_epoch_id() -> int:
    return next(_epoch_counter)


@dataclass(frozen=True)
class ToolResultRecord:
    """One completed tool result, bounded and redacted at record time."""

    tool_name: str
    payload: str
    truncated: bool = False


@dataclass(frozen=True)
class ObservationUpdate:
    """One coalesced checkpoint delivered to the reviewer runtime."""

    parent_session_key: str
    turn_id: str
    epoch: int
    seq: int
    in_progress: bool
    segments: list[str] = field(default_factory=list)
    tool_results: list[ToolResultRecord] = field(default_factory=list)
    stop_reason: str | None = None
    synthetic: bool = False


class AdvisorObserver:
    """Buffers primary-session facts and batches them into updates.

    Not thread-safe: the owner drives it from the session's event loop, the
    same place the underlying facts (tool results, segment flushes, the
    terminal event) are produced.
    """

    def __init__(
        self,
        parent_session_key: str,
        turn_id: str,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self._parent_session_key = parent_session_key
        self._turn_id = turn_id
        self._redactor = redactor
        self._epoch = _next_epoch_id()
        self._seq = 0
        self._completed = False
        self._segments: list[str] = []
        self._tool_results: list[ToolResultRecord] = []
        self._dirty = False
        #: FIFO of sealed-but-undrained final updates: a fast successor turn
        #: completing before the pump drains must not overwrite its
        #: predecessor. Bounded -- a stalled pump cannot grow it unbounded.
        self._completed_updates: list[ObservationUpdate] = []

    # -- recording ---------------------------------------------------------

    def record_segment(self, text: str) -> None:
        """Record a finalized assistant text segment."""
        self._require_active()
        self._segments.append(self._bound_marked(text))
        self._trim()
        self._dirty = True

    def record_tool_result(self, tool_name: str, payload: str) -> None:
        """Record one completed tool result, bounded and redacted."""
        self._require_active()
        bounded, truncated = self._bound(payload)
        self._tool_results.append(
            ToolResultRecord(tool_name=tool_name, payload=bounded, truncated=truncated)
        )
        self._trim()
        self._dirty = True

    # -- epochs ------------------------------------------------------------

    def begin_epoch(self) -> None:
        """Start a new observation epoch at a lifecycle boundary.

        Pending records are discarded: a rewritten or replaced primary
        conversation must not leak stale evidence -- or dedupe suppression --
        into the new epoch.
        """
        self._epoch = _next_epoch_id()
        self._completed = False
        self._segments.clear()
        self._tool_results.clear()
        self._dirty = False
        self._completed_updates.clear()

    @property
    def epoch_completed(self) -> bool:
        """True once complete() has sealed the current epoch."""
        return self._completed

    def begin_turn(self) -> None:
        """Re-prime for a new turn at the same session.

        Like begin_epoch(), but an unconsumed completed update survives the
        re-prime so a pump scheduled for the previous turn can still take
        it. No-op unless the current epoch is sealed -- a double attach
        within one turn must not wipe live records.
        """
        if not self._completed:
            return
        pending = list(self._completed_updates)
        self.begin_epoch()
        self._completed_updates = pending

    def take_completed(self) -> ObservationUpdate | None:
        """The OLDEST held final update, exactly once (None when drained)."""
        if not self._completed_updates:
            return None
        return self._completed_updates.pop(0)

    # -- draining ----------------------------------------------------------

    def drain_update(self) -> ObservationUpdate | None:
        """One in-progress update carrying the epoch's cumulative evidence.

        Returns None unless NEW records arrived since the last emission: a
        checkpoint review needs the whole story so far (a slice-at-a-time
        view proved myopic against a live reviewer), but re-reviewing an
        unchanged story is pure spend.
        """
        if not self._dirty:
            return None
        return self._emit(in_progress=True)

    def complete(
        self, stop_reason: str | None = None, synthetic: bool = False
    ) -> ObservationUpdate | None:
        """Emit the single final update for this epoch.

        Idempotent: a second completion in the same epoch is a no-op, so a
        replayed terminal event cannot produce a duplicate final review.
        ``synthetic`` marks a host-fabricated terminal; consumers must not
        treat it as a genuine provider completion.
        """
        if self._completed:
            return None
        self._completed = True
        update = self._emit(in_progress=False, stop_reason=stop_reason, synthetic=synthetic)
        # Held for the asynchronous pump: the completion hook fires inside the
        # turn's event loop, while the review runs after it. Taken once.
        self._completed_updates.append(update)
        # Bound the queue: keep the newest few, drop the oldest beyond that --
        # a pump stalled for many turns should review recent work, not replay
        # ancient history.
        if len(self._completed_updates) > 4:
            del self._completed_updates[0]
        return update

    # -- internals ---------------------------------------------------------

    def _emit(
        self,
        in_progress: bool,
        stop_reason: str | None = None,
        synthetic: bool = False,
    ) -> ObservationUpdate:
        self._seq += 1
        update = ObservationUpdate(
            parent_session_key=self._parent_session_key,
            turn_id=self._turn_id,
            epoch=self._epoch,
            seq=self._seq,
            in_progress=in_progress,
            segments=list(self._segments),
            tool_results=list(self._tool_results),
            stop_reason=stop_reason,
            synthetic=synthetic,
        )
        self._dirty = False
        return update

    def _trim(self) -> None:
        """Keep the COMBINED record set within the epoch bound.

        The cap is a payload bound for the reviewer prompt, so it applies to
        the total -- two independently-full lists would double it.
        Retention is CATEGORY-BALANCED, not globally newest-first: records
        drop oldest-first from whichever list is longest, so one chatty
        record kind cannot evict every sample of the others.
        """
        lists: tuple[list, ...] = (
            self._segments,
            self._tool_results,
        )
        excess = sum(len(r) for r in lists) - EPOCH_MAX_RECORDS
        while excess > 0:
            # Drop from the longest list first: it holds the bulkiest history
            # and keeps the survivors balanced across record kinds.
            longest = max(lists, key=lambda r: len(r))
            drop = min(excess, max(1, len(longest) - EPOCH_MAX_RECORDS // 3))
            del longest[:drop]
            excess -= drop

    def _bound(self, text: str) -> tuple[str, bool]:
        if self._redactor is not None:
            text = self._redactor(text)
        if len(text) > OBSERVATION_PAYLOAD_MAX_CHARS:
            return text[:OBSERVATION_PAYLOAD_MAX_CHARS], True
        return text, False

    def _bound_marked(self, text: str) -> str:
        bounded, truncated = self._bound(text)
        return bounded + TRUNCATION_MARKER if truncated else bounded

    def _require_active(self) -> None:
        if self._completed:
            raise RuntimeError("observation epoch already completed; begin_epoch() first")
