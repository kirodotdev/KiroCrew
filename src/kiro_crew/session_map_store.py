"""The session map's critical section and its file: the lock, the load, the flush and the write.

``_MAP_LOCK`` serializes every structural access to the map, ``_guarded`` holds it for one
call, and ``batched_save`` holds it across a block that writes once on the way out. The file
is read once, by ``_load`` from the constructor; every mutation after that reaches it through
``_save``, inline off the event loop or as a debounced deferred flush on it, whose snapshot is
serialized under the lock and written in a worker thread. ``flush``, ``aflush`` and ``aclose``
are the durability points, and ``_write_payload`` is the one tmp+rename write, ordered by a
per-snapshot ticket. The deferred flush task itself, ``SessionMap._flush_async``, is defined in
:mod:`kiro_crew.session_map`, which is where tests patch the debounce it waits out. The class
docstring's threading contract is the rule set this code keeps; new work on the map's lock,
load, flush or file write goes here.

Each function here whose first parameter is ``self`` is a ``SessionMap`` method: the class
binds it by name, so it reads and writes the map's own state on the instance, and a patch of
the method on the class or on an instance reaches every caller.

Instance fields written: ``_batch_depth``, ``_batch_dirty``, ``_data``, ``_dirty``,
``_flush_task``, ``_snapshot_seq``, ``_thread_to_session``, ``_written_seq``.

Instance fields read: ``_io_lock``, ``_path``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.messaging.link import ChannelLink, canonical_key

if TYPE_CHECKING:
    from kiro_crew.session_map import SessionMap


# Serializes every structural access to the map. MODULE-level, not per-instance,
# because the instances are not the unit of exclusion: the read-only call sites
# build their own throwaway ``SessionMap()`` (``handlers/session_storage.py``,
# ``slack/handler.py``) while ``SessionManager`` holds the long-lived one, and
# all of them resolve the same file. A per-instance lock would leave a throwaway
# reader iterating its map while the live writer rewrites the file — the exact
# pair this exists to order.
#
# REENTRANT because the public surface composes: ``set_mirror_link`` reaches
# ``clear_mirror_link`` -> ``clear_slack_link`` -> ``_save`` -> ``_write``, and
# ``batched_save`` blocks nest. A plain Lock would deadlock the first such call.
_MAP_LOCK = threading.RLock()

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _guarded(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Hold :data:`_MAP_LOCK` for the whole call.

    Applied to every :class:`SessionMap` method that mutates the map, iterates
    it, or reads several fields as one unit. Single-key probes are deliberately
    left undecorated — see the class docstring's threading contract for why that
    boundary is where it is, and ``test_session_map_locking.py`` for the ratchet
    that keeps a new mutator from being added without it.
    """

    @functools.wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with _MAP_LOCK:
            return fn(*args, **kwargs)

    return wrapper


@contextmanager
def batched_save(self: SessionMap) -> Iterator[None]:
    """One critical section and one write for every mutation in this block.

    A mutation rewrites the WHOLE map, so a caller making several related
    mutations pays that cost once per operation rather than once per
    sequence — and on the event loop each write is a stall every task
    shares. Nesting is counted, and the write happens on the way out even if
    the block raises, so a partial sequence is never left only in memory.

    The block also holds :data:`_MAP_LOCK` throughout, which is what makes
    the sequence ATOMIC and not merely coalesced: without it another
    thread's write could land between two of these mutations and then be
    dropped by this batch's whole-map rewrite.

    MUST NOT be held across an ``await``. The lock is reentrant per THREAD,
    so it does not exclude a second coroutine on the same loop — an await
    inside the block lets one interleave exactly as it would have without
    any lock, and meanwhile a worker thread blocked on the lock waits for
    the await to finish. ``test_session_map_locking.py`` ratchets this
    against the whole tree, so the rule is checked rather than trusted.
    """
    with _MAP_LOCK:
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0 and self._batch_dirty:
                self._batch_dirty = False
                # The batch's inline write below IS the flush for anything
                # deferred before the block started, so consume the dirty
                # mark too — otherwise an already-scheduled flush task would
                # wake and rewrite the identical state.
                self._dirty = False
                self._write()


def _load(self: SessionMap) -> None:
    """Populate this instance from the map file.

    DELIBERATELY NOT ``@_guarded``. It runs from ``__init__`` only, on an
    instance no other thread can reach yet, so the lock would protect
    nothing here — and it would do harm: reading and parsing the file is the
    one unbounded I/O step in this class, and a `SessionMap()` built on a
    worker thread (``handlers/session_storage._build_index`` runs under
    ``asyncio.to_thread``) would hold the lock across it, so a loop-side
    ``set_slack_link`` would block on a worker's disk read and stall every
    gateway task with it. The two shared effects it does have —
    ``_rebuild_thread_index`` and the migration ``_write`` — take the lock
    themselves, and both are bounded (see the class threading contract).
    """
    self._thread_to_session = {}
    if self._path.exists():
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            self._data = {}
            return
        if not isinstance(raw, dict):
            self._data = {}
            return
        migrated = False
        new_data: dict[str, dict] = {}
        for key, val in raw.items():
            if isinstance(val, str):
                # Backward compat: plain string → new dict format
                entry: dict = {
                    "sid": val,
                    "slack_thread_ts": None,
                    "slack_channel_id": None,
                }
                migrated = True
            elif isinstance(val, dict) and "sid" in val:
                entry = val
            else:
                continue  # skip corrupt entries
            # v1c: namespace bare Slack thread_ts keys → slack:<thread>,
            # preserving sid. Keep the raw thread_ts inside the entry so the
            # reverse index + challenge-redirect resume path are unaffected,
            # and populate the Layer-3 own-channel link.
            canon = canonical_key(key)
            # Scrub a pre-release corruption signature: a ``dashboard:`` key
            # carrying no ``slack_thread_ts`` but a ``discord:``
            # ``slack_channel_id`` is an impossible pair. It is left by an
            # early Discord session-resume build that ran the legacy
            # Slack-only ``set_channel`` path on a cold resumed dashboard
            # session; ``!unlink`` removes the Discord mirror but cannot
            # clear these separate legacy fields, so a later resume attempt
            # sees a phantom Slack binding. A genuine Slack link always has a
            # thread timestamp, so scrub only this exact signature.
            legacy_channel = entry.get("slack_channel_id")
            if (
                canon.startswith("dashboard:")
                and not entry.get("slack_thread_ts")
                and isinstance(legacy_channel, str)
                and legacy_channel.startswith("discord:")
            ):
                entry.pop("slack_thread_ts", None)
                entry.pop("slack_channel_id", None)
                migrated = True
            if canon != key:
                migrated = True
                if not entry.get("slack_thread_ts"):
                    entry["slack_thread_ts"] = key
                if "link" not in entry:
                    entry["link"] = ChannelLink(
                        channel_type="slack",
                        channel_id=entry.get("slack_channel_id"),
                        thread_id=key,
                    ).to_dict()
            existing = new_data.get(canon)
            if existing is not None:
                # Collision (e.g. partially-migrated file): never clobber a
                # live session. Overwrite ONLY when the existing entry has no
                # sid and the incoming one does; otherwise keep existing.
                # Order-independent — if both have a sid, the first-seen wins
                # deterministically rather than depending on dict iteration.
                if not (entry.get("sid") and not existing.get("sid")):
                    continue
            new_data[canon] = entry
        self._data = new_data
        self._rebuild_thread_index()
        if migrated:
            # Inline, not deferred: this is a one-time legacy-format repair
            # running from __init__ on an instance no task should capture,
            # and readers of the FILE (a fresh instance, a cotenant scan)
            # rely on the migrated shape being durable once construction
            # returns. It costs nothing on the steady state — an already
            # migrated map never takes this branch.
            self._write()
    else:
        self._data = {}


@_guarded
def _save(self: SessionMap) -> None:
    """Record that the file is owed a write, and arrange for one.

    Three contexts, three behaviours:

    - inside a :meth:`batched_save` block: mark the batch dirty; the block
      exit writes once for the whole sequence (unchanged).
    - on a thread running an event loop: mark dirty and schedule ONE
      debounced flush task. The task serializes under the lock and does the
      disk write in a worker thread, so the loop never pays the write
      inline. A mutation landing while a flush is in flight
      re-marks dirty, and the task loops until it observes a clean map, so
      a trailing mutation is never dropped.
    - no running loop (CLI, tests, worker threads): write inline on the
      caller's thread. Worker threads may block on disk; the loop is the
      only caller that must not.
    """
    if self._batch_depth:
        self._batch_dirty = True
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        self._write()
        return
    self._dirty = True
    task = self._flush_task
    # ``done()`` covers a task that exited exceptionally without clearing
    # itself; the loop-identity check covers a stale task bound to a loop
    # that has stopped running (tests create a loop per test) — such a task can
    # never wake, so it must not suppress scheduling on the live loop.
    if task is None or task.done() or task.get_loop() is not loop:
        self._flush_task = loop.create_task(self._flush_async())


@_guarded
def _take_pending_snapshot(self: SessionMap) -> tuple[str, int] | None:
    """Atomically claim the pending flush: serialize now, or report clean.

    Returns ``(payload, seq)`` when a write is owed, consuming the dirty
    mark. Returns ``None`` — and retires ``_flush_task`` in the SAME
    critical section — when the map is clean: clearing the task while still
    holding the lock is what makes "task exists" a reliable reason for
    :meth:`_save` to skip scheduling, with no window where a new mutation
    sees a task that has already decided to exit. Retirement is
    identity-checked so a superseded task (one whose loop went stale and
    was replaced by :meth:`_save`) cannot un-register its replacement.
    """
    if not self._dirty:
        if self._flush_task is asyncio.current_task():
            self._flush_task = None
        return None
    self._dirty = False
    return self._serialize()


@_guarded
def _serialize(self: SessionMap) -> tuple[str, int]:
    """Snapshot ``_data`` as an immutable JSON string with a ticket.

    The string is the ONLY thing that crosses the thread boundary —
    ``_data`` itself never does. The ticket orders this snapshot against
    every other snapshot so :meth:`_write_payload` can refuse to land a
    stale one over a newer one.
    """
    self._snapshot_seq += 1
    return json.dumps(self._data), self._snapshot_seq


@_guarded
def _restore_dirty(self: SessionMap) -> None:
    """A claimed snapshot never landed: re-owe it from any thread."""
    self._dirty = True
    try:
        current = asyncio.current_task()
    except RuntimeError:  # ``flush`` is allowed on a worker thread.
        current = None
    if current is not None and self._flush_task is current:
        self._flush_task = None


@_guarded
def _claim_for_flush(self: SessionMap) -> tuple[str, int] | None:
    """Claim pending state for a durability write, or report nothing owed.

    The shared predicate behind :meth:`flush` and :meth:`aflush`. Progress-
    based, not just the dirty mark: a flush task that has already CLAIMED a
    snapshot (consuming the mark) but whose worker-thread write has not
    landed leaves ``_snapshot_seq`` ahead of ``_written_seq``, and treating
    that as clean would hand the caller a stale file. A stale read of
    ``_written_seq`` (it advances under ``_io_lock``, not this lock) only
    errs toward one redundant write, never toward skipping a needed one.
    """
    if self._batch_depth:
        return None
    if not self._dirty and self._snapshot_seq <= self._written_seq:
        return None
    self._dirty = False
    return self._serialize()


@_guarded
def flush(self: SessionMap) -> None:
    """Force any pending deferred write to disk, synchronously.

    The deterministic durability point for SYNC contexts (no running loop,
    worker threads, tests): after this returns, the file reflects every
    mutation made so far (a no-op inside a :meth:`batched_save` block,
    whose exit already writes). The write queues behind any in-flight
    worker write on ``_io_lock`` and lands with a newer ticket, so an
    in-flight payload can never regress it. On the event loop prefer
    :meth:`aflush`: this method blocks its calling thread on disk I/O.
    """
    snapshot = self._claim_for_flush()
    if snapshot is None:
        return
    payload, seq = snapshot
    try:
        self._write_payload(payload, seq)
    except Exception:
        self._restore_dirty()
        raise


async def aflush(self: SessionMap) -> None:
    """Awaitable durability point: land pending state without blocking the loop.

    Same progress-based contract as :meth:`flush`, but the disk write runs
    in a worker thread, so a shutdown path awaiting this stays cancellable
    and a wedged filesystem cannot hold the loop. Cancellation mid-write
    re-owes the state (erring toward one redundant write — the orphaned
    thread write may still land, ordered by its ticket) and re-raises so
    the caller's deadline stays honest.
    """
    snapshot = self._claim_for_flush()
    if snapshot is None:
        return
    payload, seq = snapshot
    try:
        await asyncio.to_thread(self._write_payload, payload, seq)
    except asyncio.CancelledError:
        self._restore_dirty()
        raise
    except Exception:
        self._restore_dirty()
        raise


async def aclose(self: SessionMap) -> None:
    """Retire deferred flush tasks and durably land every owed snapshot.

    Cancelling and awaiting the registered task handles both ends of the
    debounce lifecycle: a task cancelled before its first step leaves the
    dirty mark untouched, while a task cancelled after claiming a snapshot
    restores that mark in ``_flush_async``. ``aflush`` then queues a newer
    snapshot behind any worker-thread write that cancellation could not
    stop. Repeat if a mutation registered a replacement task while the
    durability write was in flight, and return only with no task retained.
    """
    while True:
        with _MAP_LOCK:
            task = self._flush_task
        if task is not None:
            task.cancel()
            try:
                await asyncio.gather(task, return_exceptions=True)
            finally:
                # A coroutine cancelled before its first step cannot run
                # _restore_dirty(), so retire its registration here. The
                # identity guard preserves a replacement scheduled by a
                # concurrent mutation.
                with _MAP_LOCK:
                    if self._flush_task is task:
                        self._flush_task = None

        await self.aflush()
        with _MAP_LOCK:
            if self._flush_task is None:
                return


@_guarded
def _write(self: SessionMap) -> None:
    payload, seq = self._serialize()
    self._write_payload(payload, seq)


def _write_payload(self: SessionMap, payload: str, seq: int) -> None:
    """Land one serialized snapshot atomically; refuse to regress.

    Runs on whatever thread the caller chose — the loop-side inline path
    under ``_MAP_LOCK``, or a worker thread WITHOUT it (holding the map
    lock across disk I/O in a thread would make a loop-side mutation wait
    on the disk, the very stall the deferral removes). ``_io_lock`` orders
    concurrent writers, and the ticket check drops a payload older than one
    already on disk, so a slow in-flight flush cannot overwrite a newer
    forced write. Both are PER-INSTANCE: they order this instance's own
    writers against each other, not two instances sharing the file — that
    remains covered by the class contract's rule 3 (a throwaway
    ``SessionMap()`` is read-only, only the live map writes). tmp+rename
    keeps a crash from ever leaving a truncated file: the worst case is a
    well-formed OLDER map.
    """
    with self._io_lock:
        if seq <= self._written_seq:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
            replace_with_retry(tmp_path, str(self._path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        self._written_seq = seq
