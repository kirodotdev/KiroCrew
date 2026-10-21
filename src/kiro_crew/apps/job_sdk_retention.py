"""Run-record retention: the completion-order counter and the per-kind sweep.

A kind registered with ``retention`` keeps its newest terminal records and evicts
the rest after each terminal write; ``live`` keeps a terminal run that is still
using its resources, and ``on_evict`` is told about each run dropped. Newest is
decided by ``JobRun.seq``, a per-SDK completion counter this module mints and
seeds above the highest ``seq`` on disk, so the order holds across a restart.
``JobSDK`` binds ``stamp_completion_seq``, ``ensure_seq_seeded``, ``is_closed`` and
``sweep_retention`` as its own members.

Served through :mod:`kiro_crew.apps.job_sdk`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

#: The Job SDK logs under its facade's name, so one logger covers the whole family.
logger = logging.getLogger("kiro_crew.apps.job_sdk")

#: A kind's liveness predicate: given a terminal run, is it still using its
#: resources? Retention asks it before evicting a terminal run, so a run that is
#: terminal by status but still writing its results (Code Review Sage's
#: ``posting`` phase) is kept rather than deleted out from under its own writer.
JobLive = Callable[[Any], bool]

#: A kind's eviction callback: called with a run the moment retention drops it,
#: BEFORE its record file is removed, so the app can release whatever it holds
#: for that run (an on-disk subtree, a cache entry).
JobOnEvict = Callable[[Any], None]

#: No cap: a kind registered without ``retention`` keeps every record. A cap is
#: opt-in per kind because the right bound is the consumer's to choose, and a
#: silent default would evict a consumer's records on an upgrade it never asked
#: for.
_NO_RETENTION = 0

#: The exclusive bound on any ``seq``: a signed 64-bit integer, far above the
#: wall clock in nanoseconds for centuries. ``max_seq`` ignores a disk value at
#: or above it and ``raise_to`` refuses one, so a planted record cannot drive the
#: counter past what ``json.dumps`` will write.
_MAX_SEQ = 1 << 63

#: The highest floor a seed accepts: half of ``_MAX_SEQ``, still far above the
#: wall clock in nanoseconds, leaving room to count up without reaching the bound.
_SEED_CEILING = _MAX_SEQ >> 1


class _SeqCounter:
    """One SDK's strictly increasing COMPLETION counter.

    Minted onto each run's ``seq`` at its terminal write so retention orders runs
    newest-FINISHED-first without the second-resolution timestamps, where a burst
    of fast runs shares one stamp. Each value is at least the wall clock in
    nanoseconds and above the previous value, so a run finishing after a restart
    outranks every record an earlier process wrote even when the seed saw
    nothing. The seed (``raise_to``, above the highest ``seq`` in the SDK's own
    store) keeps that order if the wall clock stepped back across the restart.
    One counter per SDK, never shared between apps: a value one app's store
    holds can steer only that app's own order.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def next(self) -> int:
        with self._lock:
            self._value = max(self._value + 1, time.time_ns())
            return self._value

    def raise_to(self, floor: int) -> None:
        """Raise the counter to at least ``floor``, never lower it.

        A floor at or above ``_SEED_CEILING`` is refused, so the counter always
        keeps headroom below ``_MAX_SEQ`` to count up from.
        """
        if not 0 <= floor < _SEED_CEILING:
            return
        with self._lock:
            if floor > self._value:
                self._value = floor


def ensure_seq_seeded(self: Any) -> None:
    """Seed this SDK's counter past the highest ``seq`` in its store, once.

    Deferred to the first completion or ``reconcile`` (both off the event loop)
    rather than run in the constructor, which runs on the loop. Unconditional,
    not gated on the kind capping: an uncapped kind's records carry completion
    seqs too, and a kind that later opts in sorts by them. Never raises, and is
    not retried: a seed that fails or misses records leaves the counter on its
    wall-clock floor, which already orders a post-restart run above every
    record the earlier process wrote.
    """
    if self._seq_seeded:
        return
    with self._seq_seed_lock:
        if self._seq_seeded:
            return
        try:
            self._seq_counter.raise_to(self._store.max_seq())
        except Exception:  # noqa: BLE001 - the seed runs in the terminal finally
            logger.exception("App %s could not seed the job seq counter", self._app_name)
        self._seq_seeded = True


def stamp_completion_seq(self: Any, run: Any) -> None:
    """Mint ``run``'s completion-order ``seq``; never raises.

    Called in the terminal ``finally`` before the terminal write, so a failure
    here must not skip that write or the live/dedupe bookkeeping after it.
    """
    try:
        ensure_seq_seeded(self)
        run.seq = self._seq_counter.next()
    except Exception:  # noqa: BLE001 - must not skip the terminal write
        logger.exception("App %s could not stamp a seq on run %s", self._app_name, run.run_id)


def is_closed(self: Any) -> bool:
    """Whether disable has closed this SDK, read under the lock that sets it."""
    with self._lock:
        return bool(self._closed)


def sweep_retention(self: Any, runner: Any, kind: str) -> None:
    """Evict ``kind``'s terminal records past its cap; never raises.

    Runs on the terminal thread after the run's record is durable, so a sweep
    failure is logged and cannot lose the run's own terminal state.
    """
    try:
        _sweep(self, runner, kind)
    except Exception:  # noqa: BLE001 - a sweep failure must not escape the terminal finally
        logger.exception("App %s retention sweep for kind %s failed", self._app_name, kind)


def _sweep(self: Any, runner: Any, kind: str) -> None:
    """Evict this kind's terminal records past its cap. Writes nothing new.

    No-ops when the kind has no cap (``retention == 0``), which is every
    consumer that did not opt in -- so an app that never asks for retention
    keeps every record exactly as before.

    What a run is weighed as is decided here, in order:

    * a run still LIVE in this process is never evicted and never counted --
      it is excluded before the cap is applied, reading the same live-table
      snapshot ``cancel`` and ``reconcile`` consult, so a run whose worker is
      still executing cannot have its record deleted;
    * a terminal run the kind's ``live`` predicate claims is kept past the
      cap and does not count toward it -- the terminal-but-still-writing case
      (Sage's ``posting``). It becomes evictable on a later sweep once
      ``live`` goes False;
    * the remaining terminal records are ordered newest-first and the ones
      past ``retention`` are evicted: the kind's ``on_evict`` is called with
      each, then its file is removed.

    A non-terminal record of another process (a foreign run not yet
    reconciled) is left alone: this is retention, not reconciliation, and
    deleting an unresolved run would strand it. Reads the store once, through
    the bounded no-follow ``_iter_runs_nolink`` scan rather than ``iter_runs``:
    the directory is app-writable, so a planted link or oversized file is
    skipped -- neither counted nor evicted -- instead of hanging the terminal
    thread on an unbounded read.

    The sweep runs only when a run of this kind finishes, so a run whose
    ``live`` predicate clears after the kind's last completion stays on disk
    until the next completion of that kind: the store is bounded at the cap
    plus the runs still live, not released the moment ``live`` goes False.
    """
    cap = runner.retention
    if cap <= _NO_RETENTION:
        return
    # Serialize the whole sweep for this SDK. Two runs of one capped kind can
    # finish on their own threads at once; without this lock both sweeps read
    # the store before either deletes and compute the same evictable tail, so
    # ``on_evict`` fires twice on one run. Held across the store scan and the
    # deletes so the second sweep sees a store the first already pruned. Lock
    # order is always ``_sweep_lock`` then ``self._lock`` (the live-table
    # snapshot, the closed check and each delete take ``self._lock`` briefly
    # inside it), and nothing holding ``self._lock`` takes ``_sweep_lock``,
    # so the two cannot deadlock. ``on_evict`` runs under neither
    # ``self._lock`` nor any other SDK lock but this one.
    with self._sweep_lock:
        # A disabled SDK sweeps nothing. A worker that outlived disable's
        # join deadline still reaches here when it returns; its cap and
        # ``on_evict`` are the disabled instance's, its live table was
        # cleared by the disable, and the directory may already hold a
        # re-enabled replacement SDK's records.
        if is_closed(self):
            return
        _, live_ids = self.cancelling_and_live_ids()
        terminal: list[Any] = []
        for r in self._store._iter_runs_nolink():
            if r.kind != kind or not r.is_terminal:
                continue
            if r.run_id in live_ids:
                # A record terminal on disk but whose worker is still in the
                # live table (its terminal write landed before the live pop).
                # Spared and uncounted: finishing, not finished-and-old.
                continue
            terminal.append(r)

        # A LIVE terminal run is kept and does not consume a slot, so a burst
        # of mid-delivery runs cannot push a just-finished one off the cap.
        # The predicate is the app's; a raising one must not strand the sweep,
        # so it is treated as "live" (keep) -- eviction is the destructive
        # side, and the safe answer under doubt is not to evict.
        def _is_live(r: Any) -> bool:
            if runner.live is None:
                return False
            try:
                return bool(runner.live(r))
            except Exception:  # noqa: BLE001 - a bad predicate must not strand the sweep
                logger.exception(
                    "App %s retention live-predicate for kind %s raised on run %s; "
                    "keeping the run",
                    self._app_name,
                    kind,
                    r.run_id,
                )
                return True

        evictable = [r for r in terminal if not _is_live(r)]
        if len(evictable) <= cap:
            return
        # Newest-first by the monotonic completion order, NOT the wall clock:
        # a burst of fast runs shares one second-resolution stamp, so a
        # timestamp sort ties and could evict the just-finished run. ``seq``
        # is minted at each run's terminal write and never ties; a record from
        # an older build carries seq 0 and so sorts oldest, which is correct.
        # ``updated_at`` is the stable secondary key so the order is total
        # even among seq-0 records.
        evictable.sort(key=_order_key, reverse=True)
        for r in evictable[cap:]:
            # ``on_evict`` fires AT MOST once per run id. The id is a
            # TOMBSTONE held only while deletion is still outstanding: it
            # suppresses a second callback if a ``remove`` returns False and
            # leaves the record for a later sweep to re-encounter. Recorded
            # before the callback so a raising callback still counts as fired
            # -- it is the app's to make idempotent, and re-calling a raising
            # release helps nothing.
            if r.run_id not in self._evicted_ids:
                if is_closed(self):
                    return
                self._evicted_ids.add(r.run_id)
                if runner.on_evict is not None:
                    try:
                        runner.on_evict(r)
                    except Exception:  # noqa: BLE001 - a callback failure is logged, not fatal
                        logger.exception(
                            "App %s retention on_evict for kind %s raised on run %s; "
                            "removing the record anyway",
                            self._app_name,
                            kind,
                            r.run_id,
                        )
            # The delete and the closed check share ``self._lock``, the lock
            # ``remove_all_async`` sets ``_closed`` under, so once disable has
            # closed this SDK no sweep of it deletes another file -- the
            # directory may already belong to the replacement SDK a re-enable
            # builds, and this sweep's cap, callback and live table are the
            # disabled instance's. A callback that fired just before the close
            # leaves its record to ``remove_all``, which deletes every record.
            with self._lock:
                if self._closed:
                    return
                removed = self._store.remove(r.run_id)
            if removed:
                self._audit("job_evicted", r.run_id, "ok")
                # Deletion succeeded, so the record cannot return through
                # the sweep's scan (the sweep holds ``_sweep_lock`` across the
                # scan and the deletes). Drop the tombstone: keeping every
                # evicted id for the process's life would grow this set with
                # historical runs, the unbounded growth retention exists to
                # stop. The once-per-run guarantee still holds -- a gone file
                # is never re-encountered -- so the tombstone has no more work.
                self._evicted_ids.discard(r.run_id)


def _order_key(r: Any) -> tuple[int, str, str]:
    """Newest-first sort key; a ``seq`` outside ``[0, _MAX_SEQ)`` sorts as 0.

    The same range ``max_seq`` honours, so a forged out-of-range value can
    neither outrank a genuine run nor be read as newer than the seed allows.
    """
    seq = r.seq if 0 <= r.seq < _MAX_SEQ else 0
    return (seq, r.updated_at, r.created_at)
