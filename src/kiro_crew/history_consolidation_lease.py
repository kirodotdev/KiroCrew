"""Cross-process exclusion for one consolidation pass: the session's lease.

``HistoryConsolidator._running`` is an in-memory set. It excludes a second pass over
one session inside ONE process and says nothing about another one, and ``kirocrew
consolidate`` IS another process, running against the same transcripts while the
gateway's idle sweep is live. Both would snapshot the same span, spend a provider turn
on it, and write the history entry, preferences and lessons that turn produced. The
marker write is idempotent, so nothing detects it afterwards: the duplicate is the spend
and the doubled memory, not a corrupted offset.

A pass therefore claims a durable lease on the session's metadata line
(:meth:`_acquire_consolidation_lease`) before its authoritative snapshot, keeps a
monotonic heartbeat on it while it runs (:meth:`_heartbeat_consolidation_lease`), and
releases it on every exit (:meth:`_release_consolidation_lease`). Every durable write the
pass makes after that is fenced on the acquisition token under the transcript lock
(:meth:`_lease_fence`, :meth:`_lease_is_current`), so a holder whose renewals fell silent
past :data:`_CONSOLIDATION_LEASE_CEILING_SECS` while a peer claimed the span cannot
publish it, advance its marker or charge its retry budget a second time.

Each function here whose first parameter is ``self`` is a ``HistoryConsolidator`` method:
the class binds it under the same name (``history_consolidation.py``).

Instance fields read: ``_log``.

Part of the ``kiro_crew.history_consolidation`` family: that module is the import path and
the patch surface; this owner imports only ``history_consolidation_span``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import time as _time
import uuid
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.history_consolidation_span import AttemptedSpan, _LeaseLostMidRun

if TYPE_CHECKING:
    from kiro_crew.history_consolidation import HistoryConsolidator

# A consolidation lease is honoured only while its holder still looks alive, and
# never longer than this SINCE ITS LAST HEARTBEAT. The liveness test is exact
# where the platform can answer it, so the common failure — a process that
# crashed or was killed mid-pass — frees the session on the very next attempt
# rather than waiting this out. The ceiling covers what liveness cannot: a
# holder whose start identity cannot be read (so a recycled PID can read as
# the original) and a holder that is alive but wedged forever. It is not a timeout
# for the pass: expiry allows a peer to claim it; a subsequent renewal detects
# that claim and cancels the old holder.
#
# There is no maximum pass duration: renewal keeps a working pass excluded
# while the ceiling bounds a holder that stops renewing.
_CONSOLIDATION_LEASE_CEILING_SECS = 3600.0

# How often the holder proves it is still working. Well under the ceiling, so a
# renewal can be missed several times over — a starved event loop, a slow
# metadata write — before a peer is allowed to conclude the holder is wedged.
_CONSOLIDATION_LEASE_RENEW_SECS = 300.0

# Lease fields on the session's metadata line. The token identifies one
# ACQUISITION (so a second attempt from the same process is still excluded, and
# a release cannot clear a lease that has since been taken by someone else); the
# pid and start-time identity together identify the HOLDER, which is what the
# liveness test needs. Deliberately NOT part of _CONSOLIDATION_META_KEYS: those
# are dropped when a span is marked consolidated, and the lease is still held at
# that point — the memory, lesson and history writes come after it.
_LEASE_TOKEN = "consolidation_lease_token"
_LEASE_PID = "consolidation_lease_pid"
_LEASE_START = "consolidation_lease_start"
# Last heartbeat on the cross-process monotonic clock, refreshed for as long as
# the holder is working, so the ceiling above measures silence rather than
# duration and a wall-clock step cannot expire a live holder.
_LEASE_MONOTONIC = "consolidation_lease_monotonic"
_LEASE_INCARNATION = "consolidation_lease_incarnation"


def _lease_holder_is_live(meta: dict, *, now: float) -> bool:
    """True when the consolidation lease recorded in *meta* still excludes us.

    A lease with no pid, or with no readable monotonic heartbeat, is not a
    lease: every writer stamps both, so such a record is unreadable or
    half-written, and it must not wedge a session forever. It reads as free.

    The holder is identified by PID plus
    :func:`~kiro_crew.platform_compat.get_process_start_id`, which is stable for
    a process's lifetime and differs across a PID reuse, so a dead holder whose
    number has been handed to something else does not keep excluding peers. That
    lookup is in-process on every platform it answers on (a ``/proc`` read on
    Linux, ``libproc`` on macOS), so this is safe to call from the gateway loop.
    ``None`` means "cannot tell" for any unreadable process on any platform,
    including Windows, where readable processes have a FILETIME identity.
    An unknown identity is never a mismatch: use pid-exists plus the ceiling.

    ``now`` is the cross-process monotonic clock, independent of wall-clock
    corrections. A reboot-unique incarnation detects a reused PID/start pair
    across boots.
    """
    try:
        pid = int(meta.get(_LEASE_PID, 0) or 0)
        heartbeat = float(meta[_LEASE_MONOTONIC])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if pid <= 0 or not math.isfinite(heartbeat):
        return False
    if not platform_compat.pid_exists(pid):
        return False
    stored_start = meta.get(_LEASE_START)
    live_start = platform_compat.get_process_start_id(pid)
    if stored_start and live_start and str(stored_start) != str(live_start):
        return False
    stored_incarnation = meta.get(_LEASE_INCARNATION)
    live_incarnation = platform_compat.process_incarnation_id(pid)
    if stored_incarnation and live_incarnation and stored_incarnation != live_incarnation:
        return False
    # A future monotonic stamp is incomparable (for example after a reboot
    # with unavailable identity). Do not infer expiry from it.
    return now - heartbeat < _CONSOLIDATION_LEASE_CEILING_SECS


def _acquire_consolidation_lease(self: HistoryConsolidator, key: str) -> str | None:
    """Claim the session's consolidation lease, or ``None`` if a peer holds it.

    Compare-and-set through ``update_metadata_if``, whose guard runs under the
    same cross-process ``flock`` the write does, so two processes reaching
    here together cannot both pass it. The lease lives on the metadata line
    beside the retry accounting rather than in a lock file of its own: it is
    state about a span, it has to survive a restart to be worth taking, and
    the line is already read under that lock on every pass.

    The flock is held only for the compare-and-set, never across the LLM
    turn. Holding it for the pass would block appends — the same lock every
    write to this session takes — and freeze the conversation behind a
    provider call. That is why the lease is a durable record rather than a
    held lock, and why it needs the liveness test in
    :func:`_lease_holder_is_live` to survive a holder that dies mid-pass.

    ``require_existing`` keeps the claim from recreating a deleted session;
    ``require_write`` refuses a write the projection skipped (a legacy
    row-first line), so a claim that is not on disk is never granted.

    Blocking file IO — call it off the event loop.
    """
    token = uuid.uuid4().hex
    pid = os.getpid()
    fields = {
        _LEASE_TOKEN: token,
        _LEASE_PID: pid,
        _LEASE_MONOTONIC: _time.monotonic(),
        _LEASE_INCARNATION: platform_compat.process_incarnation_id(pid),
        _LEASE_START: platform_compat.get_process_start_id(pid),
    }

    def _free(meta: dict) -> bool:
        return not _lease_holder_is_live(meta, now=_time.monotonic())

    try:
        if self._log.update_metadata_if(
            key, fields, _free, require_existing=True, require_write=True
        ):
            return token
    except Exception:
        # Refuse the lease on persistence failure: without a durable claim,
        # another process can consolidate the same span concurrently.
        self._logger.warning(
            "Could not take the consolidation lease for %s; refusing consolidation",
            key,
            exc_info=True,
        )
    return None


def _renew_consolidation_lease(self: HistoryConsolidator, key: str, token: str) -> bool | None:
    """Refresh our heartbeat; False means lease loss, None means retryable IO.

    Blocking file IO — call it off the event loop.
    """
    checked = False

    def _ours(meta: dict) -> bool:
        nonlocal checked
        checked = True
        return meta.get(_LEASE_TOKEN) == token

    try:
        renewed = self._log.update_metadata_if(
            key, {_LEASE_MONOTONIC: _time.monotonic()}, _ours, require_write=True
        )
        if renewed:
            return True
        # An unreadable metadata line skips the guard. It proves no token
        # mismatch, so the heartbeat must keep trying rather than stop.
        if checked:
            return False
        self._logger.warning("Consolidation lease metadata unreadable for %s; retrying", key)
    except (OSError, UnicodeError):
        self._logger.warning(
            "Consolidation lease renewal failed for %s; retrying", key, exc_info=True
        )
    return None


async def _heartbeat_consolidation_lease(
    self: HistoryConsolidator,
    key: str,
    token: str,
    on_lost: Callable[[], None] | None = None,
) -> None:
    """Retry transient renewals; stop the owning pass when its lease is lost."""
    while True:
        await asyncio.sleep(_CONSOLIDATION_LEASE_RENEW_SECS)
        try:
            renewed = await asyncio.to_thread(self._renew_consolidation_lease, key, token)
        except Exception:
            self._logger.exception("Consolidation heartbeat failed for %s; aborting pass", key)
            if on_lost is not None:
                on_lost()
            raise
        if renewed is False:
            self._logger.warning("Consolidation lease lost for %s; aborting pass", key)
            if on_lost is not None:
                on_lost()
            return


def _release_consolidation_lease(self: HistoryConsolidator, key: str, token: str) -> None:
    """Drop the lease, but only while *token* is still the one on disk.

    Guarded on the token so a pass that overran the ceiling and had its lease
    taken by a peer cannot clear the peer's claim on its way out.

    Blocking file IO — call it off the event loop.
    """
    try:
        self._log.update_metadata_if(
            key,
            {
                _LEASE_TOKEN: None,
                _LEASE_PID: 0,
                _LEASE_START: None,
                _LEASE_MONOTONIC: None,
                _LEASE_INCARNATION: None,
            },
            lambda meta: meta.get(_LEASE_TOKEN) == token,
            require_write=True,
        )
    except Exception:
        # The ceiling and the liveness test both release it eventually, so a
        # failed release costs a delay, not a wedged session.
        self._logger.warning("Could not release the consolidation lease for %s", key, exc_info=True)


def _lease_is_current(self: HistoryConsolidator, key: str, token: str) -> bool:
    """True while *token* is still the lease on *key*'s metadata line.

    Call INSIDE a transcript-lock hold. Holding the lock is what makes this
    read and the write it guards one decision: a takeover cannot land between
    them, so a stale holder is refused rather than racing the new holder's
    publication of the same span.

    Reads the LINE, not this process's cached view of it. A takeover is a
    peer write, and a cached entry whose stat identity still matched would
    hand the holder its own token back — the same hazard ``update_metadata_if``
    drops the cache for before running its guard. An unreadable line cannot
    vouch for the token either, so it is refused.

    Blocking file IO — call it off the event loop.
    """
    self._log._meta_cache.pop(key, None)
    metadata, readable = self._log.get_metadata_status(key)
    if not readable:
        self._logger.warning(
            "Consolidation lease for %s is unreadable; refusing the write it guards", key
        )
        return False
    return metadata.get(_LEASE_TOKEN) == token


@contextlib.contextmanager
def _lease_fence(self: HistoryConsolidator, key: str, lease: str | None) -> Iterator[None]:
    """Hold the transcript lock and refuse unless *lease* is still ours.

    The marker and the failure accounting are durable claims on the span like
    any publication, so they are fenced on the same acquisition token: a pass
    that overran the ceiling must not retire messages, or charge attempts
    against a span, that the peer which replaced it now owns.

    It does not go through ``publication_hold``: that hold re-validates the
    transcript's privacy contract, and a restricted line is not these writes'
    contract (the marker claims only that the span was read — see the
    restricted-mid-run arm of ``_consolidate``). The transcript lock alone is
    what the check and the write need, and it is the same reentrant lock
    ``mark_consolidated`` and ``record_consolidation_failure`` take for
    themselves. ``lease`` ``None`` (no lease held) is not fenced.

    Blocking file IO — use it off the event loop.
    """
    if lease is None:
        yield
        return
    # Circular import: kiro_crew.history re-exports the consolidation family.
    from kiro_crew.history import transcript_lock_stems

    with self._log.locked_stems(transcript_lock_stems(key)):
        if not self._lease_is_current(key, lease):
            raise _LeaseLostMidRun(key)
        yield


def _advance_history_marker_now(self: HistoryConsolidator, key: str, span: AttemptedSpan) -> None:
    """Make durable the progress a finished history span represents.

    A NON-FINAL sub-slice (its prompt advanced the sub-offset but did not reach
    the end of the head, so ``prompted`` is still at ``offset``) advances the
    durable sub-offset; every other span advances ``last_consolidated`` to
    ``prompted`` and clears the sub-offset. Unfenced: callers hold
    :meth:`_lease_fence`.

    Blocking file IO — call it off the event loop.
    """
    if span.sub_prompted > span.sub_offset and span.prompted <= span.offset:
        self._log.advance_consolidation_sub_offset(
            key, span.offset, span.sub_prompted, span.generation
        )
        return
    self._log.mark_consolidated(key, span.prompted, span.generation)


def _advance_history_marker_fenced(
    self: HistoryConsolidator, key: str, span: AttemptedSpan, lease: str | None
) -> None:
    """:meth:`_advance_history_marker_now` under the lease fence.

    Blocking file IO — call it off the event loop.
    """
    with self._lease_fence(key, lease):
        self._advance_history_marker_now(key, span)


def _mark_consolidated_fenced(
    self: HistoryConsolidator, key: str, offset: int, generation: int | None, lease: str | None
) -> None:
    """Advance the consolidated marker to *offset* under the lease fence.

    For the marker writes that do not come from an :class:`AttemptedSpan` (an
    empty span, a replayed member receipt, a retired head).

    Blocking file IO — call it off the event loop.
    """
    with self._lease_fence(key, lease):
        self._log.mark_consolidated(key, offset, generation)
