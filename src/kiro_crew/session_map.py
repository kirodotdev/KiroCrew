"""Persistent session-to-kiro-cli mapping.

Stores ``session_map.json`` mapping session keys to kiro-cli session IDs,
with channel thread linkage (Slack legacy fields; other channels use the
generic ChannelLink mirror map) for bidirectional sync.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ParamSpec, TypeVar

from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
from kiro_crew.config.paths import config_dir, kiro_sessions_dir
from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_UNSPECIFIED,
    UNBIND_REASONS,
    ChannelLink,
    canonical_key,
    is_channel_session_key,
    legacy_dashboard_mirror_key,
)
from kiro_crew.sel import _infer_source, sel

logger = logging.getLogger(__name__)

# Public because another instance's map is read by file path, not through this
# class: :func:`kiro_crew.session_storage.cotenant_sids` opens the map belonging
# to a pod that shares the replay store. A second literal there would be a silent
# hazard rather than a duplicate — a rename would turn that read into "no file",
# which reads as "that instance owns nothing" and withdraws the protection.
SESSION_MAP_FILENAME = "session_map.json"

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home" and
# issue #874; dashboard/handlers/usage.py is the reference implementation.
_KIRO_SESSIONS_DIR: Path | None = None


def _kiro_sessions_dir() -> Path:
    """kiro-cli sessions directory, resolved against the live data home."""
    return _KIRO_SESSIONS_DIR if _KIRO_SESSIONS_DIR is not None else kiro_sessions_dir()


# Per-conversation flag recording a refusal of automatic origin mirroring. Named
# here rather than at the caller because it is an ON-DISK contract: the map
# persists it, so renaming the literal would silently re-enable mirroring for
# every conversation that had already turned it off.
MIRROR_OPT_OUT_FLAG = "mirror_opt_out"

# Flags that are durable SETTINGS rather than session-scoped state, and so keep
# their entry alive through :meth:`SessionMap.prune`. Membership is opt-in
# BECAUSE immortality has a cost: an entry that prune can never collect is a row
# the map carries forever, and every mutation rewrites the whole map. A flag
# describing one session (Slack's ``temporary`` / ``incognito`` threads) must
# stay collectable — one leaked row per such thread would grow without bound.
_DURABLE_FLAGS = frozenset({MIRROR_OPT_OUT_FLAG})

# How long a deferred flush waits before serializing, so a burst of mutations
# (a subagent wave calling ``set`` once per spawn) collapses into one write
# instead of one write per mutation. Small on purpose: the window is also how
# much recent state a crash can lose (the file stays a well-formed OLDER map —
# see ``_write_payload``'s tmp+rename), and durability-critical points force
# the write through :meth:`SessionMap.flush` rather than waiting it out.
_FLUSH_DEBOUNCE_SECS = 0.05


def _has_durable_flag(entry: dict) -> bool:
    """True iff *entry* carries a flag that must outlive its native session."""
    flags = entry.get("flags")
    if not isinstance(flags, dict):
        return False
    return any(flags.get(name) for name in _DURABLE_FLAGS)


def _survives_prune(entry: dict) -> bool:
    """True iff *entry* holds state that must outlive its native session.

    The ONE predicate behind every stale branch of :meth:`SessionMap.prune`, so
    they cannot disagree about what a missing session file is allowed to take
    with it. Two kinds of state qualify: a durable flag (a per-conversation
    setting) and a channel binding — a Slack thread or a ``mirror`` — which is
    the identity that routes a conversation back to its channel. Prune may clear
    a stale ``sid`` on such an entry, but never discards the entry itself.
    """
    return bool(_has_durable_flag(entry) or entry.get("slack_thread_ts") or entry.get("mirror"))


# The callable shape a lost-binding announcement is delivered through:
# ``(session_key, link, reason)``.
UnbindListener = Callable[[str, ChannelLink, str], None]

# Announces a lost inbound binding to the channel that lost it. Registered by the
# gateway, because reaching a channel means resolving a transport and SessionMap is
# the synchronous store every surface sits on. MODULE-level for the same reason as
# :data:`_MAP_LOCK`: a clearing call site may hold a throwaway ``SessionMap()``, and
# a per-instance listener would leave those removals unannounced.
_UNBIND_LISTENER: UnbindListener | None = None


def _normalize_unbind_reason(reason: str) -> str:
    """Constrain *reason* to the audited vocabulary.

    The runtime guard behind :data:`~kiro_crew.messaging.link.UNBIND_REASONS`. An
    unexpected value would add an unbounded SEL dimension and reach the notice's
    phrasing map as a miss, so it is recorded as ``unspecified`` and the WARNING
    names the call site that needs threading.
    """
    if reason in UNBIND_REASONS:
        return reason
    logger.warning(
        "unknown inbound-unbind reason %r; recording as %r",
        reason,
        UNBIND_REASON_UNSPECIFIED,
    )
    return UNBIND_REASON_UNSPECIFIED


def set_unbind_listener(callback: UnbindListener | None) -> None:
    """Register (or clear, with None) the sink for inbound-binding removals.

    Invoked as ``callback(session_key, link, reason)`` after the binding is gone
    and persisted, so the callback observes a committed removal. Best-effort by
    contract: it is called inside the map lock on a synchronous path, so it must
    not block, and an exception it raises is swallowed — a broken notifier cannot
    fail the unlink that provoked it. Suppression by reason belongs to the
    callback, since only it knows what the user has already been told.
    """
    global _UNBIND_LISTENER
    _UNBIND_LISTENER = callback


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


class ConversationOwnershipConflict(RuntimeError):
    """Raised when a second session tries to bind a conversation already held.

    One conversation belongs to at most one session. That is not a policy
    preference — it is what makes an inbound reply routable at all. The inbound
    resolver (``find_mirror_sessions(link, inbound_only=True)`` behind
    ``DiscordSessionResume.resumed_session``) refuses to choose between two
    candidates and returns ``None``, and "no owner" and "two owners" land on the
    same ``None``. So a duplicate binding does not send the reply to the wrong
    session, it sends it to NO session: the message silently starts a fresh
    conversation, which is the very bug an inbound binding exists to prevent.

    Enforced on the WRITER, deliberately. Readers stay permissive and keep
    reporting every owner they find, so a map written before this check existed
    still makes the resolver fail closed and still lets in-channel conflict
    detection see the duplicate. Callers translate this into their own surface's
    refusal — an HTTP 409, or the channel's own "run `!unlink` first" — never a
    500 or a generic failure, which would send the user off to retry a command
    that is working exactly as intended.
    """


class SessionMap:
    """Persistent mapping of session_key → kiro-cli session ID.

    Stored as ``~/.kiro/crew/session_map.json``. Atomic write via tmp+rename.
    Only used for long-lived conversational sessions (channel DMs, dashboard).
    Stateless sessions (cron, subagent, taskrunner) are excluded.

    Each entry is a dict with keys: ``sid``, ``slack_thread_ts``, ``slack_channel_id``.
    A reverse index ``_thread_to_session`` maps Slack thread_ts → session_key
    for bidirectional sync lookups.

    THREADING CONTRACT
    ------------------
    Every mutation rewrites the WHOLE map from ``_data``, so a read-modify-write
    is only atomic if nothing else touches the structure in between. Three rules
    hold that together; the first two are enforced, the third cannot be:

    1. **Any thread may call this class.** :data:`_MAP_LOCK` (module-level,
       reentrant) guards every method that mutates the map, iterates it, or
       reads several fields as one unit. Single-key probes (``get_cwd``,
       ``get_flag``, ``get_session_for_thread``, …) are lock-free on purpose:
       one ``dict.get`` cannot observe a half-applied write, and taking the lock
       for them would put the map's cheapest reads — including the one on every
       inbound Slack reply — behind its most expensive write. What that costs is
       a rule for the WRITERS: a structure a lock-free reader looks at is
       replaced by rebinding a finished copy, never cleared and refilled in
       place (see :meth:`_rebuild_thread_index`).

       Because a caller can now WAIT, what may be held under the lock is
       bounded: **no guarded method reads or parses the map file.** The longest
       hold is one whole-map write (0.77 ms at today's size) plus, in ``get`` and
       ``prune``, a session-file stat per entry — costs the event loop already
       paid inline before this lock existed. Loading a file into a fresh
       instance is the one unbounded step, and :meth:`_load` is deliberately
       unguarded so a worker-thread construction cannot stall the loop behind
       its disk read.

    2. **A multi-mutation sequence must say so.** Two locked calls are two
       critical sections, and another thread's write can land between them and
       be lost by the second one's whole-map rewrite. :meth:`batched_save` holds
       the lock across the block, making the sequence one critical section AND
       one write. Related mutations belong inside it — never merely adjacent.
       It MUST NOT be held across an ``await``: the lock is per-thread
       reentrant, so an await inside a batch lets another coroutine on the same
       loop walk straight into the block, while a worker thread waiting on the
       lock stalls until the await returns. ``test_session_map_locking.py``
       ratchets this against the whole tree.

    3. **Writes go through the LIVE map.** The lock orders access to the
       structure; it cannot reconcile two instances that loaded ``_data``
       independently, because the loser's rewrite is a whole-file write of a
       stale snapshot. A throwaway ``SessionMap()`` is therefore READ-ONLY —
       see ``session_transfer._join_layer_b`` for what a detached write costs.

    On the event loop, a mutation does not pay the disk write inline: it marks
    the map dirty and a debounced flush task serializes UNDER the lock into an
    immutable payload, then writes tmp+rename in a worker thread — ``_data``
    never crosses the thread boundary, and the lock is never held across the
    await (the ratchet in ``test_session_map_locking.py`` checks that). Off the
    loop (CLI, tests, worker threads) writes stay inline and synchronous.
    :meth:`flush` (sync) and :meth:`aflush` (awaited) are the deterministic
    durability points; losing a pending flush leaves a well-formed OLDER file,
    never a truncated one. The lock is what
    makes the offload safe at all — before it, offloading a single write made
    the map racy instead of non-blocking.

    SCOPE: the lock is in-PROCESS. Two gateways writing one map file would still
    lose updates, and nothing here changes that — the data-home singleton is what
    keeps that from happening. ``history.ConversationLog._locked`` is the
    cross-process, off-loop-enforcing primitive; this is deliberately not it.
    """

    def __init__(self) -> None:
        self._path = config_dir() / SESSION_MAP_FILENAME
        self._data: dict[str, dict] = {}  # key → {"sid", "slack_thread_ts", "slack_channel_id"}
        self._thread_to_session: dict[str, str] = {}  # slack_thread_ts → session_key
        self._batch_depth = 0
        self._batch_dirty = False
        # Strong references to in-flight audit writes, so an executor Future is not
        # collected before its result is consumed. Discarded on completion.
        self._audit_futures: set["asyncio.Future[None]"] = set()
        # Deferred-flush state (all mutated under _MAP_LOCK). ``_dirty`` records
        # that in-memory state is ahead of the file; ``_flush_task`` is the one
        # loop task owed for it. ``_snapshot_seq`` tickets each serialized
        # snapshot and ``_written_seq`` (guarded by ``_io_lock``, not _MAP_LOCK)
        # refuses a STALE payload: an in-flight thread write racing a forced
        # inline write must not land an older map over a newer one. Keeping the
        # worker's write off _MAP_LOCK is what keeps loop-side MUTATIONS from
        # waiting on its disk I/O; the inline write paths (batch exit, flush,
        # load-time migration) do queue behind it on _io_lock — they already
        # paid an inline write before deferral existed, so that wait replaces
        # like with like.
        self._dirty = False
        self._flush_task: asyncio.Task[None] | None = None
        self._snapshot_seq = 0
        self._written_seq = 0
        self._io_lock = threading.Lock()
        self._load()

    @contextmanager
    def batched_save(self) -> Iterator[None]:
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

    def _load(self) -> None:
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
    def _rebuild_thread_index(self) -> None:
        """Rebuild _thread_to_session from current _data.

        Two entries can claim the same ``slack_thread_ts``: a dashboard session
        that created the thread via send-to-Slack, and a ``slack:<ts>`` session
        forked by an inbound reply that ignored the existing binding. A plain
        last-write-wins pass resolves the thread by dict order, which is file
        order -- so the fork usually wins and the thread keeps routing to the
        wrong session even after the fork bug is fixed.

        Break that tie in favour of the session that does NOT derive its key from
        this thread. A ``slack:<ts>`` key whose ts IS the thread is the fork (or
        a self-link, which is a no-op rewrite); any other key holds the real
        conversation. This heals maps corrupted before the fix, on load, with no
        migration pass.

        Built into a FRESH dict and rebound at the end, never cleared in place.
        ``get_session_for_thread`` reads this index without the lock (one keyed
        lookup, the hot path of every inbound Slack reply), so a clear-then-refill
        would give it a window in which the thread it is resolving has no owner —
        and "no owner" is not a delay there, it is a brand-new conversation
        forked off the user's reply. A rebind is atomic under the GIL, so that
        reader sees either the whole old index or the whole new one.
        """
        rebuilt: dict[str, str] = {}
        derived: dict[str, str] = {}
        for key, entry in self._data.items():
            ts = entry.get("slack_thread_ts")
            if not ts or not isinstance(ts, str):
                # A hand-edited or legacy file can hold a non-string ts. The old
                # index only ever used it as a dict key, so it survived; the
                # tie-break below calls str.endswith, which would raise
                # TypeError here and take gateway startup down with it.
                continue
            if is_channel_session_key(key) and key.endswith(ts):
                # Self-derived: only usable if nothing else claims the thread.
                derived.setdefault(ts, key)
                continue
            rebuilt[ts] = key
        for ts, key in derived.items():
            rebuilt.setdefault(ts, key)
        self._thread_to_session = rebuilt

    @_guarded
    def _save(self) -> None:
        """Record that the file is owed a write, and arrange for one.

        Three contexts, three behaviours:

        - inside a :meth:`batched_save` block: mark the batch dirty; the block
          exit writes once for the whole sequence (unchanged).
        - on a thread running an event loop: mark dirty and schedule ONE
          debounced flush task. The task serializes under the lock and does the
          disk write in a worker thread, so the loop never pays the write
          inline (issue #2405). A mutation landing while a flush is in flight
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
        # that no longer runs (tests create a loop per test) — such a task can
        # never wake, so it must not suppress scheduling on the live loop.
        if task is None or task.done() or task.get_loop() is not loop:
            self._flush_task = loop.create_task(self._flush_async())

    @_guarded
    def _take_pending_snapshot(self) -> tuple[str, int] | None:
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
    def _serialize(self) -> tuple[str, int]:
        """Snapshot ``_data`` as an immutable JSON string with a ticket.

        The string is the ONLY thing that crosses the thread boundary —
        ``_data`` itself never does. The ticket orders this snapshot against
        every other snapshot so :meth:`_write_payload` can refuse to land a
        stale one over a newer one.
        """
        self._snapshot_seq += 1
        return json.dumps(self._data), self._snapshot_seq

    async def _flush_async(self) -> None:
        """Debounce, serialize under the lock, write off the loop; repeat.

        Loops because coalescing must not lose the last write: a mutation that
        lands while ``to_thread`` is writing re-marks dirty, and the next
        iteration picks it up. Exits only via :meth:`_take_pending_snapshot`
        observing a clean map (which retires the task under the lock).

        The lock is NEVER held across an await — serialization happens inside
        the guarded helpers, the write happens off-lock in a worker thread.
        Cancellation re-owes any claimed-but-unwritten snapshot and does NOT
        write (a cancellation handler that did disk I/O on the loop would hold
        up the very teardown that cancelled it): pending state is landed by the
        shutdown path awaiting :meth:`aflush`, or rescheduled by the next
        mutation. A task cancelled before its first step never runs this body
        at all — the same two nets cover it, because nothing here consumed the
        dirty mark. A write failure restores the mark and retires the task so
        a later mutation retries.
        """
        try:
            while True:
                await asyncio.sleep(_FLUSH_DEBOUNCE_SECS)
                snapshot = self._take_pending_snapshot()
                if snapshot is None:
                    return
                payload, seq = snapshot
                await asyncio.to_thread(self._write_payload, payload, seq)
        except asyncio.CancelledError:
            self._restore_dirty()
            raise
        except Exception:
            logger.exception("Deferred session-map flush failed; will retry on next mutation")
            self._restore_dirty()

    @_guarded
    def _restore_dirty(self) -> None:
        """A claimed snapshot never landed: re-owe the flush, retire the task."""
        self._dirty = True
        if self._flush_task is asyncio.current_task():
            self._flush_task = None

    @_guarded
    def _claim_for_flush(self) -> tuple[str, int] | None:
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
    def flush(self) -> None:
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
        self._write_payload(payload, seq)

    async def aflush(self) -> None:
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

    @_guarded
    def _write(self) -> None:
        payload, seq = self._serialize()
        self._write_payload(payload, seq)

    def _write_payload(self, payload: str, seq: int) -> None:
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
                os.replace(tmp_path, str(self._path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._written_seq = seq

    def _resolve_alias(self, key: str) -> "tuple[str, dict | None]":
        """Resolve *key* through the map's alias folds; return (matched_key, entry).

        The ONE place the key-alias rules live, shared by the pruning
        :meth:`get` and the read-only :meth:`has_hint` so they cannot drift:
        1. exact key;
        2. bidirectional bare <-> ``slack:`` shim (a not-yet-updated caller
           may pass a bare thread_ts; resolve to the namespaced entry);
        3. dashboard history round-trip (``dashboard:dashboard_X`` -> the
           original ``dashboard:X`` written via ``_safe_key``).
        Pure lookup: no disk I/O, no pruning, no save.
        """
        entry = self._data.get(key)
        if not entry:
            canon = canonical_key(key)
            if canon != key:
                entry = self._data.get(canon)
                if entry:
                    key = canon
        if not entry and key.startswith("dashboard:dashboard_"):
            canonical = "dashboard:" + key[len("dashboard:dashboard_") :]
            entry = self._data.get(canonical)
            if entry:
                key = canonical
        return key, entry

    @_guarded
    def get(self, key: str) -> str | None:
        """Return kiro-cli session ID if mapping exists and .json file is present.

        Handles the dashboard history key round-trip: the original session key
        ``dashboard:chat-1-xxx`` becomes ``dashboard_chat-1-xxx`` on disk (via
        ``_safe_key``), and when resumed from history the slot name becomes
        ``dashboard_chat-1-xxx``, producing session key
        ``dashboard:dashboard_chat-1-xxx``.  We try the canonical form too.
        """
        matched_key, entry = self._resolve_alias(key)
        if not entry:
            return None
        sid = entry["sid"]
        # Only kiro-cli keeps transcripts at a flat path this process can stat.
        # For every other backend the sid's validity is decided by session/load
        # itself, so skip the file check rather than pruning a live session.
        # An absent label means kiro-cli, so normalize before comparing.
        if (entry.get("provider") or PROVIDER_LABEL_DEFAULT) != PROVIDER_LABEL_DEFAULT:
            return sid
        sessions_dir = _kiro_sessions_dir()
        if sid and (sessions_dir / f"{sid}.json").exists():
            jsonl = sessions_dir / f"{sid}.jsonl"
            try:
                jsonl_size = jsonl.stat().st_size
            except FileNotFoundError:
                jsonl_size = 0
            if jsonl_size < 10:
                logger.info("Session %s has empty JSONL — pruning stale entry for %s", sid, key)
                self._repair_or_remove_stale(matched_key)
                return None
            return sid
        if sid:
            self._repair_or_remove_stale(matched_key)
        return None

    @_guarded
    def _repair_or_remove_stale(self, key: str) -> None:
        """Drop a stale entry, or clear only its ``sid`` when state must outlive it.

        Asks :func:`_survives_prune`, the same predicate :meth:`prune` uses, so
        the two stale paths cannot disagree: an entry carrying a channel binding
        or a durable flag keeps the entry and loses only the dead ``sid``. The
        binding is the conversation's identity — deleting it here would strand the
        channel. No inbound-unbind audit or notice fires on the repair branch,
        because no binding was removed; the removal branch reaches an entry that
        holds none.
        """
        entry = self._data.get(key)
        if entry is not None and _survives_prune(entry):
            if entry.get("sid"):
                entry["sid"] = ""
                self._save()
            return
        self._remove_entry(key, reason=UNBIND_REASON_ENTRY_DELETED)

    def has_hint(self, key: str) -> bool:
        """Read-only, in-memory probe: does an entry exist for *key*?

        Unlike :meth:`get`, this never touches disk and never mutates the map
        (no stale-entry pruning, no ``_save``), so it is safe to call from the
        event loop and carries no cross-thread hazard. It can return a false
        positive for an entry whose session files are gone — callers that act
        on the hint must treat the pruning :meth:`get` inside the actual
        resume path as the authority and tolerate the resume falling back.
        Alias folding is shared with :meth:`get` via ``_resolve_alias``.
        """
        return self._resolve_alias(key)[1] is not None

    @staticmethod
    def _inbound_binding(entry: dict) -> ChannelLink | None:
        """The inbound resume binding *entry* holds, or None when it holds none.

        The single definition of "losing this strands a conversation", so every
        removal path announces the same thing. An entry flagged inbound whose
        ``mirror`` is missing or unparsable routes nothing already, so it is no loss.
        """
        if not entry.get("mirror_accepts_inbound"):
            return None
        raw = entry.get("mirror")
        if not isinstance(raw, dict):
            return None
        try:
            return ChannelLink.from_dict(raw)
        except (TypeError, ValueError):
            return None

    def _note_inbound_unbind(self, key: str, link: ChannelLink, reason: str) -> None:
        """Audit and announce the removal of one inbound resume binding.

        The choke point every removal path funnels through, so a binding cannot
        die traceless: the SEL event is the durable record (lifecycle logs are
        INFO and a production gateway logs WARNING and above), and the listener is
        what reaches the channel that just lost its way back. Called after the
        removal is persisted, so the audit describes something that happened, and
        both legs are best-effort — a broken sink or notifier must not turn an
        unlink, or a teardown reaching here from a ``finally``, into a raise.

        The reason is normalized HERE rather than trusted from the caller: this is
        the only place that sees every removal.
        """
        reason = _normalize_unbind_reason(reason)
        target = f"{link.channel_type}:{link.channel_id or ''}"
        if link.thread_id:
            target = f"{target}/{link.thread_id}"
        self._emit_unbind_audit(key, target, reason)
        listener = _UNBIND_LISTENER
        if listener is None:
            return
        try:
            listener(key, link, reason)
        except Exception:
            logger.warning("inbound-unbind listener failed for %s", key, exc_info=True)

    def _emit_unbind_audit(self, key: str, target: str, reason: str) -> None:
        """Write the removal's SEL event without blocking a caller on the loop.

        ``sel()`` does real filesystem work on its first call — it resolves the
        data home, creates the log and mints the HMAC trust root — and every write
        appends. This is reached from synchronous ``SessionMap`` calls a coroutine
        makes inline, so doing it here stalls the gateway for the disk I/O. The
        WHOLE closure, ``sel()`` included, therefore runs in the running loop's
        executor, with the Future retained and its result consumed so a failure
        logs instead of vanishing. Off the loop there is nothing to protect, so it
        runs inline — same single event either way, no thread per event.
        """

        def _emit() -> None:
            sel().log_api_access(
                caller="kirocrew",
                operation="session.inbound_unbind",
                outcome="success",
                # ``_infer_source`` is the canonical key->surface classifier, so
                # this event's surface cannot drift from the rest of the trail.
                source=_infer_source(key),
                resources=f"{key} -> {target} ({reason})",
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                _emit()
            except Exception:
                logger.warning("inbound-unbind audit failed for %s", key, exc_info=True)
            return
        future = loop.run_in_executor(None, _emit)
        self._audit_futures.add(future)

        def _consume(done: "asyncio.Future[None]") -> None:
            self._audit_futures.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                logger.warning("inbound-unbind audit failed for %s: %s", key, exc)

        future.add_done_callback(_consume)

    @_guarded
    def _remove_entry(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> None:
        """Remove an entry and update reverse index.

        A dying entry takes any inbound binding it held with it, so the removal
        is announced here rather than at each caller — this is the only path by
        which a whole entry leaves the map.
        """
        entry = self._data.pop(key, None)
        if not entry:
            return
        inbound = self._inbound_binding(entry)
        ts = entry.get("slack_thread_ts")
        if ts and self._thread_to_session.get(ts) == key:
            del self._thread_to_session[ts]
        self._save()
        if inbound is not None:
            self._note_inbound_unbind(key, inbound, reason)

    @_guarded
    def set(self, key: str, sid: str, *, provider: str = "", cwd: str = "") -> None:
        """Save mapping and persist to disk, preserving existing slack fields."""
        key = canonical_key(key)
        existing = self._data.get(key)
        if existing:
            existing["sid"] = sid
            if provider:
                existing["provider"] = provider
            if cwd:
                existing["cwd"] = cwd
        else:
            entry: dict = {"sid": sid, "slack_thread_ts": None, "slack_channel_id": None}
            if provider:
                entry["provider"] = provider
            if cwd:
                entry["cwd"] = cwd
            self._data[key] = entry
        self._save()

    def get_cwd(self, key: str) -> str:
        """Return the stored CWD for *key*, or '' if not set."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return ""
        return entry.get("cwd", "")

    def get_provider(self, key: str) -> str:
        """Return the stored provider for *key* (e.g. 'acp', 'claude_code'), or ''."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return ""
        return entry.get("provider", "")

    @_guarded
    def clear_sid(self, key: str) -> None:
        """Clear the stored session ID without removing the entry.

        Used on provider switch (the SID is incompatible with the new
        provider) and by the poisoned-conversation discard. The cleared sid
        is stashed as ``discarded_sid`` so the operation is diagnosable and
        manually reversible — the native conversation still exists on disk;
        only the pointer to it is dropped.
        """
        entry = self._data.get(canonical_key(key))
        if entry and entry.get("sid"):
            entry["discarded_sid"] = entry["sid"]
            entry["sid"] = ""
            self._save()

    def get_discarded_sid(self, key: str) -> str:
        """Return the last sid dropped by :meth:`clear_sid`, or ''."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return ""
        return entry.get("discarded_sid", "")

    @_guarded
    def delete(self, key: str, *, reason: str = UNBIND_REASON_ENTRY_DELETED) -> None:
        """Remove mapping and persist.

        The catch-all reason names the shape of the removal rather than its
        motive: a caller that knows why (a session teardown, a recycle) passes
        its own so the audit says which one happened.
        """
        self._remove_entry(canonical_key(key), reason=reason)

    @_guarded
    def prune(self) -> int:
        """Remove entries whose session files no longer exist.

        An entry carrying a DURABLE flag or a channel binding is never deleted,
        and when its ``sid`` has gone stale the ``sid`` is cleared instead —
        :func:`_survives_prune` is the single predicate both stale branches ask,
        so neither can start discarding what the other keeps. A durable flag is a
        per-conversation SETTING, not session state: it can be written before the
        conversation has ever run a turn (``/unlink`` as the very first message
        leaves no ``sid``, no thread and no mirror), and it must outlive the
        native session the conversation happened to be using. A channel binding
        (``slack_thread_ts``, ``mirror``) is the conversation's identity: it is
        what routes an inbound channel message back to this session. Deleting the
        entry either way would silently revert user state at the next restart —
        the setting returns to the default they had just turned off, or the next
        message from the channel opens a fresh session instead of resuming the
        one it is bound to.

        Session-SCOPED flags (a temporary or incognito thread) are deliberately
        NOT durable: they describe one session, so keeping their entries alive
        would leak a never-collected row per such thread and grow the map — which
        every mutation rewrites — without bound.

        Returns the number of entries removed; a ``sid``-only reset is a repair,
        not a removal, so it is not counted.
        """
        sessions_dir = _kiro_sessions_dir()
        stale: list[str] = []
        repaired = False
        for key, entry in self._data.items():
            # Only kiro-cli's transcripts are stat-able here; other backends
            # own their own storage. An absent label means kiro-cli.
            if (entry.get("provider") or PROVIDER_LABEL_DEFAULT) != PROVIDER_LABEL_DEFAULT:
                continue
            sid = entry.get("sid")
            survives = _survives_prune(entry)
            if sid and not (sessions_dir / f"{sid}.json").exists():
                if survives:
                    entry["sid"] = ""
                    repaired = True
                else:
                    stale.append(key)
            elif not sid and not survives:
                stale.append(key)
        for k in stale:
            del self._data[k]
        if stale:
            self._rebuild_thread_index()
            self._save()
            logger.info("Pruned %d stale session map entries", len(stale))
        elif repaired:
            # A sid-only reset still has to reach disk, or the next startup sees
            # the same stale sid and repairs it again forever.
            self._save()
        return len(stale)

    @_guarded
    def mapped_sids_by_key(self) -> dict[str, str]:
        """Session key to kiro-cli session ID, for every entry that has one.

        A session ID present here is one Kiro Crew can still resume. Callers that
        account for or reclaim disk space need both halves of this relation: the
        IDs to exclude from deletion, and the key each ID belongs to so a
        session's transcript can be paired with its replay log. Returning the
        mapping rather than only the ID set is what lets such a caller reclaim a
        session whole instead of leaving one half behind.
        """
        return {
            key: sid
            for key, entry in self._data.items()
            if isinstance(sid := entry.get("sid"), str) and sid
        }

    @staticmethod
    def _is_self_derived(key: str, thread_ts: str) -> bool:
        """True when *key* derives from *thread_ts* itself (``slack:<ts>``).

        Mirrors the predicate in ``_rebuild_thread_index``: a self-derived
        claimant is the fork (or a self-link) and holds a contested thread
        only when nothing else claims it.
        """
        return is_channel_session_key(key) and key.endswith(thread_ts)

    @_guarded
    def _evict_rival_claimants(self, key: str, thread_ts: str) -> list[str]:
        """Clear the Slack link fields of every OTHER entry claiming *thread_ts*.

        Returns the evicted keys. Only link fields are cleared (entry and
        ``sid`` preserved, mirroring ``clear_slack_link``); the caller owns the
        reverse-index write and the ``_save()``. A self-derived claimant — a
        channel key whose ts IS the thread — never evicts: the load-time
        tie-break in ``_rebuild_thread_index`` awards a contested thread to the
        non-derived session, and stripping that owner's fields here would leave
        the heal with nothing to restore.
        """
        if self._is_self_derived(key, thread_ts):
            return []
        evicted: list[str] = []
        for other_key, other_entry in self._data.items():
            if other_key != key and other_entry.get("slack_thread_ts") == thread_ts:
                other_entry.pop("slack_thread_ts", None)
                other_entry.pop("slack_channel_id", None)
                evicted.append(other_key)
        if evicted:
            logger.info(
                "Slack thread %s reassigned to session %s from %s",
                thread_ts,
                key,
                ", ".join(evicted),
            )
        return evicted

    @_guarded
    def set_slack_link(self, key: str, thread_ts: str, channel_id: str | None) -> None:
        """Link a session to a Slack thread. Creates entry if needed.

        A thread has at most one owner: a non-derived claim evicts every other
        entry claiming *thread_ts* (see :meth:`_evict_rival_claimants`), and the
        eviction lands in the same ``_save()`` as the new claim so a crash
        between the two cannot persist a two-owner map. A self-derived claim
        never evicts — the load-time tie-break resolves that contest instead.
        An empty *thread_ts* is the clear sentinel and neither evicts anyone
        nor enters the reverse index.

        Establishing a link also drops any ``slack_paused`` marker, so a mute
        never outlives the binding it was set on — otherwise it would re-mute
        the next link, which the user never paused. That drop is scoped to a
        REBIND (different ts or channel). Identical coordinates are the SAME
        binding and keep the mute, because this method is not only called by an
        explicit connect: the Slack inbound path re-writes the same ts/channel on
        every turn as its thread registry (``slack/handler.py``,
        ``slack/transport_dispatch.py``), so clearing here unconditionally let a
        single inbound message — or a cold start's ``set_channel`` — silently
        un-disconnect a thread the user had muted, and later dashboard turns
        resumed delivering to it. Connecting does not rely on this: the dashboard
        row lifts a mute through :meth:`set_slack_paused`, not by re-linking.
        """
        key = canonical_key(key)
        entry = self._data.get(key)
        evicted = self._evict_rival_claimants(key, thread_ts) if thread_ts else []
        if entry:
            if (
                entry.get("slack_thread_ts") == thread_ts
                and entry.get("slack_channel_id") == channel_id
            ):
                if thread_ts:
                    if evicted:
                        self._thread_to_session[thread_ts] = key
                    else:
                        self._thread_to_session.setdefault(thread_ts, key)
                # Only the eviction is a mutation on this branch now. The pause
                # is deliberately NOT touched here — see the docstring.
                if evicted:
                    self._save()
                return
            # REBIND: the mute belonged to the binding being replaced, so it goes
            # with it rather than carrying onto a thread the user never muted.
            entry.pop("slack_paused", None)
            old_ts = entry.get("slack_thread_ts")
            if old_ts and old_ts != thread_ts:
                self._thread_to_session.pop(old_ts, None)
            entry["slack_thread_ts"] = thread_ts
            entry["slack_channel_id"] = channel_id
        else:
            self._data[key] = {
                "sid": "",
                "slack_thread_ts": thread_ts,
                "slack_channel_id": channel_id,
            }
        if thread_ts:
            # Same policy as the tie-break: a self-derived claim never
            # displaces a live owner from the reverse index — it routes the
            # thread only when nothing else claims it. A non-derived claim
            # (which just swept its rivals) takes the index outright.
            if self._is_self_derived(key, thread_ts):
                self._thread_to_session.setdefault(thread_ts, key)
            else:
                self._thread_to_session[thread_ts] = key
        self._save()

    @_guarded
    def get_slack_link(self, key: str) -> tuple[str | None, str | None]:
        """Return (thread_ts, channel_id) for a session."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return None, None
        return entry.get("slack_thread_ts"), entry.get("slack_channel_id")

    @_guarded
    def clear_slack_link(self, key: str) -> bool:
        """Remove the Slack link from a session, keeping the session itself.

        Clears only ``slack_thread_ts`` + ``slack_channel_id`` (preserves
        ``sid`` and the entry) and evicts the ``_thread_to_session`` reverse
        index so a later Slack reply in the old thread does not re-route to
        this session and silently re-engage mirroring. Returns True iff a link
        was present (only then is ``_save()`` called).
        """
        entry = self._data.get(canonical_key(key))
        if not entry:
            return False
        old_ts = entry.get("slack_thread_ts")
        had_link = bool(old_ts or entry.get("slack_channel_id"))
        if old_ts and self._thread_to_session.get(old_ts) == key:
            del self._thread_to_session[old_ts]
        entry.pop("slack_thread_ts", None)
        entry.pop("slack_channel_id", None)
        # The mute dies with the binding it muted. A marker left behind would
        # silently re-mute whatever link the user establishes next.
        was_paused = entry.pop("slack_paused", None) is not None
        if had_link or was_paused:
            self._save()
        return had_link

    @_guarded
    def set_slack_paused(self, key: str, paused: bool) -> bool:
        """Mute (or unmute) a linked Slack thread; return the PREVIOUS state.

        A mute is not an unlink. The thread binding, both coordinate fields and
        the ``_thread_to_session`` reverse index all survive, so a reply in the
        muted thread still resolves to THIS session rather than forking a new
        one; only outbound turn mirroring stops.

        Stored as a presence flag (``True`` or absent, never ``False``) on the
        same entry and under the same key resolution the link accessors use, so
        the flag cannot land on a spelling they do not read. That is what makes
        "a pause never outlives its binding" hold by construction instead of by
        bookkeeping in every caller.
        """
        entry = self._data.get(canonical_key(key))
        if not entry:
            return False
        was_paused = entry.get("slack_paused") is True
        if paused and not was_paused:
            entry["slack_paused"] = True
            self._save()
        elif not paused and was_paused:
            del entry["slack_paused"]
            self._save()
        return was_paused

    @_guarded
    def is_slack_paused(self, key: str) -> bool:
        """True iff this session's Slack link is muted AND still linked.

        The flag is only meaningful next to a live binding: a marker with no
        link is stale by definition, and reporting it would make an unlinked
        session render as a muted one.
        """
        entry = self._data.get(canonical_key(key))
        if not entry:
            return False
        if not (entry.get("slack_thread_ts") or entry.get("slack_channel_id")):
            return False
        return entry.get("slack_paused") is True

    def get_session_for_thread(self, thread_ts: str) -> str | None:
        """Return the session key linked to a Slack thread_ts, or None."""
        return self._thread_to_session.get(thread_ts)

    # ── Channel-neutral outbound mirror binding (generalizes Slack linking) ──
    # ``set/get/clear_slack_link`` above are the Slack-specific backend of this
    # API: they own the dedicated ``slack_thread_ts`` / ``slack_channel_id``
    # fields and the ``_thread_to_session`` reverse index that powers Slack's
    # inbound leg. The trio below exposes the SAME binding channel-neutrally as
    # a ``ChannelLink`` so the dashboard turn path can deliver a reply to any
    # proactive-capable channel via ``Transport.send_message`` without
    # special-casing Slack. Slack routes back through the dedicated fields;
    # every other channel stores a ``ChannelLink`` under ``mirror``.

    @_guarded
    def set_mirror_link(
        self,
        key: str,
        link: ChannelLink | None,
        *,
        accepts_inbound: bool = False,
        reason: str = UNBIND_REASON_UNSPECIFIED,
    ) -> None:
        """Bind (or clear, when *link* is None) a session's mirror target.

        ``accepts_inbound`` marks a non-Slack mirror as a session-resume binding:
        messages arriving from that exact channel location may be routed back to
        *key*. Ordinary dashboard mirrors remain outbound-only. Slack keeps its
        dedicated reverse index and therefore ignores this flag.

        ``reason`` describes the removal this call performs, if any: a None
        *link*, or an overwrite that displaces an inbound binding — rebinding to
        another location, or to the same one as outbound-only, both end a session
        resume as thoroughly as an unlink does.

        Raises :class:`ConversationOwnershipConflict` when another session already
        holds this exact location AND the conversation is inbound-committed —
        either this claim is inbound-capable, or an occupant already is. See
        :meth:`mirror_claim_blockers`: exclusivity exists to keep inbound routing
        unambiguous, so it is scoped to that, and two outbound-only mirrors on one
        conversation stay as permitted as they were before this rule.
        """
        if link is None:
            self.clear_mirror_link(key, reason=reason)
            return
        if link.channel_type == SLACK_NAMESPACE:
            self.set_slack_link(key, link.thread_id or "", link.channel_id)
            return
        key = canonical_key(key)
        rivals = self.mirror_claim_blockers(key, link, accepts_inbound=accepts_inbound)
        if rivals:
            raise ConversationOwnershipConflict(
                f"{link.channel_type} conversation is already held by "
                f"{len(rivals)} other session(s)"
            )
        entry = self._ensure_entry(key)
        displaced = self._inbound_binding(entry)
        entry["mirror"] = link.to_dict()
        if accepts_inbound:
            entry["mirror_accepts_inbound"] = True
        else:
            entry.pop("mirror_accepts_inbound", None)
        # Binding is how the user reconnects, so it lifts any mute. Same reason
        # as the Slack path: a marker outliving its binding re-mutes the next one.
        entry.pop("mirror_paused", None)
        self._save()
        if displaced is not None and (displaced != link or not accepts_inbound):
            self._note_inbound_unbind(key, displaced, reason)

    @_guarded
    def mirror_claim_blockers(
        self,
        key: str,
        link: ChannelLink,
        *,
        accepts_inbound: bool = False,
    ) -> list[str]:
        """Sessions that must stop *key* from binding *link*, or ``[]`` if it is free.

        The single definition of "this conversation is taken", shared by the
        atomic check inside :meth:`set_mirror_link` and by the dashboard
        endpoint's precheck. They must ask the same question with the same
        arguments: a backstop whose idea of "occupied" differs from the precheck
        it backs looks like coverage while diverging from it in exactly the cases
        that matter.

        Exclusivity is owed to INBOUND routing, so it is scoped to it. A
        conversation is exclusive when it is inbound-committed — either this claim
        is inbound-capable, or an existing occupant already is. That is precisely
        when a second binding does harm: the inbound resolver refuses to choose
        between two candidates and returns ``None``, and "no owner" and "two
        owners" are the same ``None`` to it, so the reply silently starts a fresh
        session instead of resuming.

        Two outbound-only mirrors on one conversation are left alone. They are
        merely noisy — both write out, nobody reads back — and refusing them
        would reach transports that cannot resume at all, whose in-channel link
        handlers do not translate this refusal because they can never provoke it.

        The occupancy scan itself is UNFILTERED once the conversation is
        inbound-committed: an outbound-only occupant counts. Narrowing that too
        would let an in-channel ``!link`` land a second binding on a conversation
        the dashboard is resuming through, which is the collision that strands the
        reply.

        A rebind by the SAME session is not a rivalry, and three spellings can all
        mean "me": the canonical key, the row the binding actually lives on today
        (a pre-unification ``dashboard:`` row, which only ``_mirror_key`` can
        identify), and any other spelling that canonicalizes to this key. Deriving
        the legacy name unconditionally instead of asking ``_mirror_key`` would
        excuse a row that is NOT this session's binding, letting a genuine
        duplicate persist.

        CONTRACT FOR A NEW TRANSPORT: declaring
        ``TransportCapabilities.supports_session_resume`` makes its conversations
        inbound-committable, and therefore makes this refusal reachable from that
        transport's own in-channel link handler. Translate it there into a
        followable message, as ``discord/transport_dispatch.py`` does — an uncaught
        raise inside a channel handler is a dropped task and a silent no-reply.
        """
        key = canonical_key(key)
        selves = {key, self._mirror_key(key)}
        rivals = [
            other
            for other in self.find_mirror_sessions(link)
            if other not in selves and canonical_key(other) != key
        ]
        if not rivals:
            return []
        if accepts_inbound or any(
            (self._data.get(other) or {}).get("mirror_accepts_inbound") for other in rivals
        ):
            return rivals
        return []

    @_guarded
    def _mirror_key(self, key: str) -> str:
        """The key a session's mirror binding is actually stored under.

        A channel conversation's binding belongs on its own session key — the key
        its dashboard turns run under. Bindings written before that unification
        sit on the sanitized ``dashboard:`` spelling
        (:func:`~kiro_crew.messaging.link.legacy_dashboard_mirror_key`); when
        that row holds the only binding it is still the live one, so reads and
        clears resolve to it instead of silently dropping a link the user set. A
        binding on the canonical key always wins, so a rebind supersedes the
        legacy row rather than being shadowed by it.
        """
        canon = canonical_key(key)
        entry = self._data.get(canon)
        if entry and entry.get("mirror") is not None:
            return canon
        if is_channel_session_key(canon):
            legacy = self._data.get(legacy_dashboard_mirror_key(canon))
            if legacy and legacy.get("mirror") is not None:
                return legacy_dashboard_mirror_key(canon)
        return canon

    @_guarded
    def get_mirror_link(self, key: str) -> ChannelLink | None:
        """Return a session's outbound mirror target as a channel-neutral link.

        Reads the explicit ``mirror`` binding first; for a legacy Slack session
        that only carries ``slack_thread_ts`` / ``slack_channel_id`` it
        synthesizes the equivalent Slack ``ChannelLink`` so callers never have
        to special-case Slack. Returns None when the session mirrors nowhere.
        """
        entry = self._data.get(self._mirror_key(key))
        if not entry:
            return None
        raw = entry.get("mirror")
        if raw:
            return ChannelLink.from_dict(raw)
        ts = entry.get("slack_thread_ts")
        ch = entry.get("slack_channel_id")
        if ts or ch:
            return ChannelLink(channel_type=SLACK_NAMESPACE, channel_id=ch, thread_id=ts)
        return None

    def mirror_accepts_inbound(self, key: str) -> bool:
        """True iff this session's mirror is a session-RESUME binding.

        The read counterpart of ``set_mirror_link(accepts_inbound=True)``.
        ``get_mirror_link`` deliberately returns a plain ``ChannelLink``, which
        cannot carry the flag, so a caller that needs to tell a two-way resume
        from an outbound-only mirror (e.g. the dashboard's link projection, which
        must not offer a one-way mirror the affordances of a resumed session)
        asks here. Slack is excluded: it routes inbound through its own
        ``_thread_to_session`` index and never sets this marker.
        """
        entry = self._data.get(canonical_key(key))
        return bool(entry and entry.get("mirror_accepts_inbound"))

    @_guarded
    def find_mirror_sessions(
        self,
        link: ChannelLink,
        *,
        inbound_only: bool = False,
    ) -> list[str]:
        """Return sessions bound to an exact non-Slack mirror location.

        The list form makes duplicate/corrupt bindings explicit so callers can
        fail closed rather than routing an inbound message to an arbitrary
        session. With ``inbound_only=True``, ordinary outbound-only dashboard
        mirrors are excluded.
        """
        matches: list[str] = []
        for key, entry in self._data.items():
            raw = entry.get("mirror")
            if not isinstance(raw, dict):
                continue
            if inbound_only and not entry.get("mirror_accepts_inbound"):
                continue
            try:
                candidate = ChannelLink.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if candidate == link:
                matches.append(key)
        return matches

    @_guarded
    def clear_mirror_links_at(
        self, link: ChannelLink, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> list[str]:
        """Clear EVERY session whose mirror targets an exact non-Slack location.

        The write counterpart of :meth:`find_mirror_sessions`. An in-channel
        unlink means "nothing mirrors into this conversation anymore", and the
        bindings that occupy a location are matched by VALUE there — so a row
        stranded under a key spelling the conversation no longer uses (a rotated
        DM generation, a pre-unification ``dashboard:`` row) or a dashboard
        session mirroring into the conversation still blocks it while being
        unreachable by any key-addressed :meth:`clear_mirror_link`. Clearing by
        location closes that gap and doubles as the repair path for duplicate
        bindings, which the inbound resolver deliberately refuses to pick from.

        Returns the cleared session keys (empty when the location was free).
        Slack mirrors live in their own reverse index and are out of scope,
        exactly as in :meth:`find_mirror_sessions`.
        """
        cleared: list[str] = []
        lost: list[tuple[str, ChannelLink]] = []
        for key in self.find_mirror_sessions(link):
            entry = self._data.get(key)
            if entry is None:  # pragma: no cover - keys come from _data itself
                continue
            inbound = self._inbound_binding(entry)
            entry.pop("mirror", None)
            entry.pop("mirror_accepts_inbound", None)
            entry.pop("mirror_paused", None)
            cleared.append(key)
            if inbound is not None:
                lost.append((key, inbound))
        if cleared:
            self._save()
        # A sweep can clear several sessions at one location; each one lost its
        # own way back, so each is audited and announced separately.
        for key, inbound in lost:
            self._note_inbound_unbind(key, inbound, reason)
        return cleared

    @_guarded
    def clear_mirror_link(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> bool:
        """Remove a session's outbound mirror binding; return True iff one existed.

        A non-Slack ``mirror`` field is dropped directly; a Slack binding is
        cleared via ``clear_slack_link`` so its reverse index is evicted too.
        Resolves through :meth:`_mirror_key` so an unlink reaches a binding still
        held under the legacy spelling — otherwise a mirror that reads as live
        could not be turned off.
        """
        mkey = self._mirror_key(key)
        entry = self._data.get(mkey)
        if not entry:
            return False
        if entry.get("mirror") is not None:
            inbound = self._inbound_binding(entry)
            entry.pop("mirror", None)
            entry.pop("mirror_accepts_inbound", None)
            entry.pop("mirror_paused", None)
            self._save()
            if inbound is not None:
                self._note_inbound_unbind(mkey, inbound, reason)
            return True
        if entry.get("slack_thread_ts") or entry.get("slack_channel_id"):
            return self.clear_slack_link(mkey)
        return False

    @_guarded
    def set_mirror_paused(self, key: str, paused: bool, *, origin: bool = False) -> bool:
        """Mute (or unmute) a non-Slack delivery for *key*; return the PREVIOUS state.

        Two DISTINCT non-Slack deliveries can exist on one session, so they get
        two distinct flags rather than sharing one:

        * ``mirror_paused`` — the explicit ``mirror`` binding (``origin=False``).
        * ``origin_paused`` — the conversation the session was BORN in
          (``origin=True``), which reaches the dashboard as a separate row
          derived from the legacy namespaced ``slack_channel_id``.

        A session born in Discord that also mirrors to Telegram renders both
        rows, and a single scalar made disconnecting one silently disconnect the
        other. Keying the flag to the row's source is what makes the two
        independent; the caller says which row it is acting on, because only the
        caller knows.
        """
        field = "origin_paused" if origin else "mirror_paused"
        # The ORIGIN flag belongs to the SESSION and is keyed canonically;
        # ``_mirror_key`` is reserved for the MIRROR flag, which belongs to the
        # binding. That distinction is load-bearing, not stylistic: _mirror_key
        # migrates between the canonical row and the legacy ``dashboard:``
        # spelling depending on where a mirror binding currently lives, so writing
        # the origin flag through it strands the pause the moment a canonical
        # mirror is added -- the lookup moves rows, the flag does not, and the
        # conversation the user muted silently resumes delivering.
        entry = self._data.get(canonical_key(key) if origin else self._mirror_key(key))
        if not entry:
            return False
        was_paused = entry.get(field) is True
        if paused and not was_paused:
            entry[field] = True
            self._save()
        elif not paused and was_paused:
            del entry[field]
            self._save()
        return was_paused

    @_guarded
    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        """True iff the named non-Slack delivery for *key* is muted.

        A mute is only meaningful next to something that can actually deliver, so
        a flag with nothing behind it reads as not-paused rather than reporting a
        session that mirrors nowhere as merely quiet. What counts as "something"
        differs per flag, which is why the existence check is not shared:

        * ``origin=True`` requires the session to be channel-BORN, and is read
          from the CANONICAL row -- the session's own -- never through
          ``_mirror_key``. That conversation is permanent, so the flag cannot be
          orphaned by its target disappearing; it CAN be orphaned by the lookup
          moving, which is what keying it to the mirror binding used to do.
        * ``origin=False`` requires an explicit ``mirror`` dict, and follows the
          binding through ``_mirror_key``.
        """
        canon = canonical_key(key)
        entry = self._data.get(canon if origin else self._mirror_key(key))
        if not entry:
            return False
        if origin:
            if not is_channel_session_key(canon):
                return False
            return entry.get("origin_paused") is True
        if not isinstance(entry.get("mirror"), dict):
            return False
        return entry.get("mirror_paused") is True

    @_guarded
    def max_generation(self, bucket: str) -> int:
        """Return the highest persisted DM generation for a session *bucket*.

        The bucket is the generation-0 key (e.g.
        ``telegram:<agent>:direct:<user>``); generations persist as ``{bucket}``
        (gen 0) and ``{bucket}:gen{N}``. Returns the max ``N`` with a persisted
        entry, or -1 when the bucket has none. Channels seed their in-memory
        generation counter from this so ``/new`` and idle/daily reset advance
        past any generation left on disk (restart-safe) instead of colliding
        with a stale session and resuming it.
        """
        bucket = canonical_key(bucket)
        best = 0 if bucket in self._data else -1
        prefix = f"{bucket}:gen"
        for key in self._data:
            if key.startswith(prefix):
                suffix = key[len(prefix) :]
                if suffix.isdigit():
                    best = max(best, int(suffix))
        return best

    @_guarded
    def find_key_by_sid(self, session_id: str) -> str | None:
        """Find the session map key for a given kiro-cli session ID."""
        for k, entry in self._data.items():
            sid = entry.get("sid") if isinstance(entry, dict) else entry
            if sid == session_id:
                return k
        return None

    @_guarded
    def channel_key_for_stem(self, stem: str) -> str:
        """The real channel session key whose transcript filename is *stem*.

        ``history._safe_key`` folds every ``:`` in a session key to ``_`` to
        build the filename, and that fold is NOT reversible: given
        ``discord_kirocrew_direct_123`` there is no way to tell which
        underscores were colons, and an agent name may legitimately contain
        one. This map holds the unfolded keys, so it is the only authority.

        Returns ``""`` when no mapped session matches, which callers must treat
        as "leave it unbound" rather than guessing — binding a tab to a
        wrongly-spelled key would answer the user from a session the channel
        never sees.
        """
        if not stem:
            return ""
        from kiro_crew.history import _safe_key

        for k in self._data:
            if is_channel_session_key(k) and _safe_key(k) == stem:
                return k
        return ""

    def get_link(self, key: str) -> ChannelLink | None:
        """Return the session's OWN inbound-channel link, or None.

        Distinct from the dashboard->Slack *mirror* binding, which stays
        behind ``get/set_slack_link`` (guardrail G3).
        """
        entry = self._data.get(canonical_key(key))
        if not entry:
            return None
        raw = entry.get("link")
        return ChannelLink.from_dict(raw) if raw else None

    @_guarded
    def set_link(self, key: str, link: ChannelLink) -> None:
        """Set the session's OWN inbound-channel link. Creates entry if needed."""
        key = canonical_key(key)
        entry = self._data.get(key)
        if entry:
            entry["link"] = link.to_dict()
        else:
            self._data[key] = {
                "sid": "",
                "slack_thread_ts": None,
                "slack_channel_id": None,
                "link": link.to_dict(),
            }
        self._save()

    # --- v1c-B: per-conversation state on the session entry ---------------
    # Durable backing for per-thread state (``temporary``, ``incognito``,
    # ``agents``, ``projects``). Storing it on the session entry makes it
    # survive gateway restarts and ties its lifetime to the session (pruned
    # with the entry) rather than an ad-hoc bounded LRU in module-global dicts.

    @_guarded
    def _ensure_entry(self, key: str) -> dict:
        """Return the entry for *key*, creating a blank one if absent."""
        entry = self._data.get(key)
        if entry is None:
            entry = {"sid": "", "slack_thread_ts": None, "slack_channel_id": None}
            self._data[key] = entry
        return entry

    @_guarded
    def set_flag(self, key: str, flag: str, value: bool) -> None:
        """Set or clear a boolean per-conversation flag (e.g. ``temporary``).

        Flags are stored under an ``flags`` sub-dict on the entry. Clearing the
        last flag removes the sub-dict so empty state does not accrete on disk.
        Idempotent: writing an unchanged value still persists (cheap) so callers
        need not pre-check.
        """
        key = canonical_key(key)
        if value:
            entry = self._ensure_entry(key)
        else:
            # Clearing a flag on a key that was never stored is a no-op — don't
            # materialize a blank entry (would accrete empty state on disk).
            existing = self._data.get(key)
            if not existing:
                return
            entry = existing
        flags = entry.get("flags") or {}
        if value:
            flags[flag] = True
        else:
            flags.pop(flag, None)
        if flags:
            entry["flags"] = flags
        else:
            entry.pop("flags", None)
        self._save()

    def get_flag(self, key: str, flag: str) -> bool:
        """Return the value of a per-conversation boolean *flag* (default False)."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return False
        flags = entry.get("flags")
        return bool(flags and flags.get(flag))

    @_guarded
    def set_agent_override(self, key: str, agent: str | None) -> None:
        """Set (or clear, when *agent* is falsy) the per-thread agent override."""
        key = canonical_key(key)
        if agent:
            self._ensure_entry(key)["agent_override"] = agent
        else:
            entry = self._data.get(key)
            if not entry or "agent_override" not in entry:
                return
            entry.pop("agent_override", None)
        self._save()

    def get_agent_override(self, key: str) -> str | None:
        """Return the per-thread agent override for *key*, or None."""
        entry = self._data.get(canonical_key(key))
        return entry.get("agent_override") if entry else None

    @_guarded
    def set_project_override(self, key: str, project: str | None) -> None:
        """Set (or clear, when *project* is falsy) the per-thread project dir."""
        key = canonical_key(key)
        if project:
            self._ensure_entry(key)["project_override"] = project
        else:
            entry = self._data.get(key)
            if not entry or "project_override" not in entry:
                return
            entry.pop("project_override", None)
        self._save()

    def get_project_override(self, key: str) -> str | None:
        """Return the per-thread project-dir override for *key*, or None."""
        entry = self._data.get(canonical_key(key))
        return entry.get("project_override") if entry else None
