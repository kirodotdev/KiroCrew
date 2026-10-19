"""The explicit-import chunk budget: a rolling-window ceiling on the chunks the explicit
import paths ingest.

``ImportChunkBudget`` books a worst-case reservation when an explicit import is admitted
and settles it to the real chunk count once the import lands; ``ImportChunkBudgetError``
is the refusal a caller surfaces when the window is spent; and ``_import_chunk_budget``
reads ``knowledge.import_chunk_budget`` from live config. ``IngestionPipeline`` holds one
budget per pipeline and enters it on every explicit ingest.

:mod:`kiro_crew.knowledge.ingestion` stays the import path and the patch surface for every
name here, and ``ImportChunkBudgetError`` reports that module. At import time this module
reads only :mod:`kiro_crew.knowledge.chunker`; the config read imports the loader when it runs,
and nothing here imports the facade.
"""

from __future__ import annotations

import time as _time

from .chunker import MAX_CHUNKS_PER_FILE

#: Rolling window (seconds) over which the explicit-import chunk budget is
#: totalled. 60s is a short window because the explicit paths are interactive
#: (a user adding files, an agent handing over a document), so the ceiling bounds
#: a burst of successive adds within about a minute rather than pacing a
#: long-running scan the way the watcher's per-sweep budget does. Inlined as the
#: single window: only ``ImportChunkBudget()`` is constructed in production.
_IMPORT_CHUNK_BUDGET_WINDOW_SECS = 60.0


class ImportChunkBudgetError(RuntimeError):
    """Raised when an explicit import would exceed ``knowledge.import_chunk_budget``.

    Carries the budget, the window and the count already spent in the window so a
    caller can surface WHY the import was deferred rather than failing opaquely --
    the whole point of refusing rather than silently truncating a deliberate
    import. The message is deterministic and ASCII so it is safe to surface to an
    agent tool result or a dashboard error.
    """

    def __init__(self, budget: int, window_secs: float, spent: int):
        self.budget = budget
        self.window_secs = window_secs
        self.spent = spent
        super().__init__(
            f"Knowledge import deferred: the explicit-import chunk budget "
            f"(knowledge.import_chunk_budget={budget} chunks per {window_secs:g}s) "
            f"is already spent ({spent} chunks reserved or ingested this window). "
            f"Retry after the window rolls over, ingest fewer files at once, or "
            f"raise knowledge.import_chunk_budget (0 removes the bound)."
        )


# Tracebacks and logs name it by the import path its callers use.
ImportChunkBudgetError.__module__ = "kiro_crew.knowledge.ingestion"


class ImportChunkBudget:
    """Rolling-window ceiling on chunks ingested through the EXPLICIT import paths.

    The watcher path (folder sweep) already has its own per-sweep and global
    chunk budgets; those are bounded by a scan (one pass over one source). The
    explicit routes -- single-file add, agent ``knowledge_add_document``, direct
    text ingest, remote sync -- are many independent calls in succession with no
    scan boundary, so there is nothing to total the cost across files. This is
    the equivalent ceiling for that path: a rolling 60-second window over which
    at most ``budget`` chunks may be ingested.

    ``budget=0`` disables it (unbounded). Per-file cost stays bounded by the
    chunker's 50-chunk cap independently; this bounds the CROSS-FILE total.

    The gate is enforced at CALL granularity, not chunk granularity: a file whose
    real cost pushes the window PAST ``budget`` still completes (its chunks are
    already produced), and the NEXT call is the one refused. So the effective
    bound is ``[budget, budget + MAX_CHUNKS_PER_FILE)`` -- a run of adds can end at
    most one file's worst case (50 chunks) above ``budget``, never unbounded. This
    is deliberate: the alternative -- rejecting a file mid-flight once its chunk
    count is known -- would waste the extraction already spent and is the
    silent-truncation failure this exists to avoid. Treat ``budget`` as a soft
    ceiling with a bounded, single-file overshoot, not a hard cap.

    Trip behaviour is REFUSE, not truncate: a single-file cap that silently
    dropped part of a user's deliberate import would be a worse failure than the
    cost it prevents. :meth:`reserve` raises :class:`ImportChunkBudgetError` at
    entry, before any chunking/extraction cost is incurred; an accepted file
    always runs to completion, its reservation settled to its real chunk count.

    Concurrency: the caller does ``reserve()`` (synchronous), then awaits the
    chunk/extract work, then ``settle()``. If N imports each only checked a
    running total they would all pass before any recorded, overrunning by
    concurrency x file. So ``reserve`` books a placeholder of the per-file
    maximum (:data:`~kiro_crew.knowledge.chunker.MAX_CHUNKS_PER_FILE`) INTO the
    window immediately, and that reservation is visible to every concurrent
    ``reserve`` across the await -- ``settle`` later reconciles it down to the
    real count. The reservation is an UPPER bound, so a burst is refused a little
    early rather than allowed to overrun; the worst residual error is one file's
    slack per in-flight import, never unbounded. Every caller is on the event
    loop and reserve/settle/release are synchronous and non-awaiting, so no lock
    is needed (unlike the embed limiter, whose refill straddles an await).
    """

    def __init__(self, budget: int = 0):
        self._budget = max(0, int(budget))
        self._window_secs = _IMPORT_CHUNK_BUDGET_WINDOW_SECS
        # (monotonic_ts, chunk_count, token) entries; pruned to the window on each
        # touch. token identifies a live reservation so settle/release can find it.
        self._events: list[tuple[float, int, int]] = []
        self._next_token = 0
        # Tokens already settled: a later release() of one is a no-op, which is
        # what makes a finally-release safe after a success-path settle.
        self._settled: set[int] = set()
        # Tokens reserved and not yet settled or released, i.e. imports still in
        # flight. Window pruning skips their events: an import slower than the
        # window would otherwise age out of its own reservation while still
        # running, and a concurrent reserve() would stop seeing it and admit
        # another import past the concurrency ceiling the placeholder enforces.
        self._open: set[int] = set()

    def set_budget(self, budget: int) -> None:
        """Update the ceiling live (config is read per-ingest, no restart)."""
        self._budget = max(0, int(budget))

    def _spent(self, now: float) -> int:
        """Chunks reserved-or-recorded within the trailing window.

        Prunes entries older than the window, EXCEPT an open reservation: an
        import still in flight holds its slot however long it runs, so a file
        slower than the window cannot age out of the ceiling it occupies.
        """
        cutoff = now - self._window_secs
        self._events = [
            (ts, n, t) for (ts, n, t) in self._events if ts >= cutoff or t in self._open
        ]
        return sum(n for (_, n, _t) in self._events)

    def reserve(self) -> int | None:
        """Refuse a new import when the window is at/over budget; else book a
        worst-case placeholder and return its token.

        Returns ``None`` when the budget is disabled (0) -- the caller then does
        not settle. Raises :class:`ImportChunkBudgetError` when the trailing
        window (including live reservations from concurrent in-flight imports) is
        already exhausted.

        The placeholder is the per-file maximum because the true chunk count is
        not known until the file is chunked, which happens after an await; booking
        the maximum now is what makes a concurrent ``reserve`` see this import and
        stops N simultaneous imports from each passing before any records.
        """
        if self._budget <= 0:
            return None
        now = _time.monotonic()
        spent = self._spent(now)
        if spent >= self._budget:
            raise ImportChunkBudgetError(self._budget, self._window_secs, spent)
        token = self._next_token
        self._next_token += 1
        self._events.append((now, MAX_CHUNKS_PER_FILE, token))
        self._open.add(token)
        return token

    def settle(self, token: int | None, chunk_count: int) -> None:
        """Reconcile a reservation to the real chunk count of an accepted import.

        No-op when ``token`` is ``None`` (budget was disabled at reserve time).
        The reservation timestamp is preserved so the window still expires the
        cost at the moment the import began, not when it finished. After a settle
        the token is CONSUMED, so a later :meth:`release` of it is a no-op -- that
        is what lets every caller put ``release`` in a ``finally`` and settle on
        the success path without double-counting.
        """
        if token is None:
            return
        self._settled.add(token)
        self._open.discard(token)
        n = max(0, int(chunk_count))
        for i, (ts, _placeholder, t) in enumerate(self._events):
            if t == token:
                self._events[i] = (ts, n, t)
                return

    def release(self, token: int | None) -> None:
        """Drop a still-open reservation, so a failed OR no-op import does not hold
        budget. A no-op when the token is ``None`` or was already settled -- so it
        is safe to call unconditionally in a ``finally`` after a possible settle.

        This is the fix for the reservation LEAK on the no-op success paths
        (content-hash-unchanged, dedup-refused): those branches return before any
        chunk work, so they never settle; a finally-release reclaims their
        placeholder. Without it, ten no-op re-ingests would each strand a 50-chunk
        placeholder and falsely refuse genuine imports at the default budget.
        """
        if token is None:
            return
        if token in self._settled:
            self._settled.discard(token)
            return
        self._open.discard(token)
        self._events = [(ts, n, t) for (ts, n, t) in self._events if t != token]


def _import_chunk_budget() -> int:
    # Read live so a config change takes effect without a restart, matching
    # _max_ingest_file_mb. Circular import guarded the same way.
    from kiro_crew.config.loader import KiroCrewConfig  # circular import

    try:
        return max(0, int(KiroCrewConfig.load().knowledge.import_chunk_budget))
    except Exception:
        return 0
