"""Channel History Buffer — rolling window of recent messages per channel.

Captures all messages in group channels (not just @mentions) so that
when the agent is invoked, it has conversational context about what
was being discussed.

Non-observe channels are ephemeral / in-memory only.  Observe-mode
channels are persisted to disk as JSONL so history survives restarts.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import math
import os
import stat
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, pinned_parent_replace_supported
from kiro_crew.executors import channel_history_executor

logger = logging.getLogger(__name__)

# Defaults
_DEFAULT_MAX_ENTRIES = 50  # per channel
_DEFAULT_TTL_SECS = 300  # 5 minutes

# Observe-mode channels get a deeper buffer
OBSERVE_MAX_ENTRIES = 200
OBSERVE_TTL_SECS = 604800  # 1 week

# Slack chat.postMessage caps message text at 40,000 characters.
HISTORY_MAX_TEXT_CHARS = 40_000
HISTORY_MAX_ID_CHARS = 256


def _bounded_optional_id(value: str | None) -> str | None:
    return value[:HISTORY_MAX_ID_CHARS] if value is not None else None


#: ``O_NOFOLLOW`` refuses to open a symlink at all; ``O_DIRECTORY`` makes
#: "open this only if it is a directory" atomic with the open. Both are 0
#: (no-ops) where the platform lacks them (Windows).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

#: Whether the deferred append can pin its parent directory openat-style
#: (open the directory itself refusing links, then resolve the leaf RELATIVE
#: to that descriptor via ``dir_fd``). Same gate shape as the backup and
#: clone-setup writers, which use the identical pattern. Absent on Windows,
#: where the fallback is ``platform_compat.open_append_no_reparse`` — an open
#: that refuses to traverse a reparse point at the leaf.
_SUPPORTS_DIR_FD = (
    os.open in getattr(os, "supports_dir_fd", set()) and _O_NOFOLLOW != 0 and _O_DIRECTORY != 0
)

#: How many best-effort APPEND jobs may sit queued on the disk lane at once.
#: The queue is normally empty (a write is microseconds, messages arrive
#: seconds apart), so the cap only bites when storage has stalled. Terminal
#: ops (rewrite/unlink) do not consume these slots — they are coalesced to at
#: most one pending flush per channel, so they are bounded structurally.
_DISK_LANE_MAX_PENDING = 64

#: Terminal per-channel disk ops, coalesced last-writer-wins. A REWRITE
#: publishes the in-memory window as the whole file; an UNLINK removes the
#: file. A rewrite's content is snapshotted at SCHEDULE time on the deque's
#: mutator thread; because it replaces the whole file, its correctness does
#: not depend on queue ordering.
_OP_REWRITE = "rewrite"
_OP_UNLINK = "unlink"


@dataclass
class HistoryEntry:
    """A single message in the channel history buffer."""

    user: str
    text: str
    thread_ts: str | None = None
    msg_ts: str | None = None  # Slack message ts — identifies thread parent
    timestamp: float = field(default_factory=time.monotonic)
    wall_ts: float | None = None  # wall-clock time (observe-mode only, for persistence)


@dataclass
class _ObserveLoad:
    """Parsed lane result applied to the deque on its caller thread."""

    entries: deque[HistoryEntry]
    loaded_entries: int
    fields_sliced: bool


class ChannelHistory:
    """Per-channel rolling window of recent messages.

    Usage:
        history = ChannelHistory()
        history.push("C0ABC123", "alice", "The pipeline is broken")
        history.push("C0ABC123", "bob", "I see 5xx errors in us-west-2")

        # When @kirocrew is mentioned:
        context = history.context_for("C0ABC123")
        # → "[Recent channel messages for context:]\\n  alice (1m ago): ..."
    """

    def __init__(
        self,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        ttl_secs: int = _DEFAULT_TTL_SECS,
        observe_max_entries: int = OBSERVE_MAX_ENTRIES,
        observe_ttl_secs: int = OBSERVE_TTL_SECS,
        history_dir: Path | None = None,
    ) -> None:
        self._max_entries = max_entries
        self._ttl_secs = ttl_secs
        self._observe_max_entries = observe_max_entries
        self._observe_ttl_secs = observe_ttl_secs
        # Construction can run on the gateway event loop, so it stores only
        # the RAW configured path. The first history-lane operation resolves it,
        # creates/first-trusts the root, and publishes the canonical path plus
        # identity under this lock. Every later operation copies that pair
        # without resolving again.
        self._history_dir = history_dir
        self._history_root_lock = threading.Lock()
        self._history_root_identity: tuple[int, int] | None = None
        self._channels: dict[str, deque[HistoryEntry]] = {}
        self._observe_channels: set[str] = set()  # channels with deeper buffer
        # Invalidates a deferred load when observe mode is toggled again before
        # the lane returns its parsed snapshot.
        self._observe_generation: dict[str, int] = {}
        self._user_names: dict[str, str] = {}  # user_id -> display name cache
        # Appends since this channel's file was last compacted. The file is an
        # append log while the deque is a fixed window, so without this the two
        # diverge without bound between compactions.
        self._appends_since_compact: dict[str, int] = {}
        # Bounds the best-effort append queue (see _DISK_LANE_MAX_PENDING).
        # Released by the worker as each job finishes; acquired non-blocking
        # at submit, so the event-loop thread never waits on it.
        self._disk_lane_slots = threading.Semaphore(_DISK_LANE_MAX_PENDING)
        # Terminal-op reconciliation state (see _schedule_terminal). The lock
        # guards these dicts and window snapshots only — it is never held
        # across disk IO, so the event-loop thread blocks for microseconds at
        # most.
        self._terminal_lock = threading.Lock()
        # channel_id -> (generation, op, entry snapshot or None=unlink,
        # schedule-time (canonical root, identity), or None before first trust).
        # The generation is the _flush_epoch value at schedule time.
        self._pending_terminal: dict[
            str,
            tuple[
                int,
                str,
                list[HistoryEntry] | None,
                tuple[Path, tuple[int, int]] | None,
            ],
        ] = {}
        # Supersession counter: bumped when a terminal op is scheduled.
        self._flush_epoch: dict[str, int] = {}
        # Publish counter: advanced to a terminal op's generation only after
        # its disk IO SUCCEEDS. An append queued before a terminal op is
        # superseded (its entry is in that op's snapshot) only once the op
        # has actually published. A failed rewrite publishes nothing and
        # supersedes no queued append, but it arms _rewrite_failed_channels,
        # so queued and new appends skip disk until a later rewrite succeeds.
        # The deque retains their entries, and that successful rewrite persists
        # its snapshot.
        self._published_epoch: dict[str, int] = {}
        # A failed rewrite means the existing append log cannot currently be
        # bounded. Suppress new disk appends for that channel until a rewrite
        # succeeds; the in-memory deque remains authoritative meanwhile.
        self._rewrite_failed_channels: set[str] = set()
        # A failed load leaves the persisted file authoritative over history
        # that never reached the deque. Suppress every disk mutation — append
        # and window rewrite alike — until a later successful load folds that
        # complete file into memory; ``_append_to_disk`` retries the load once
        # per count window. The deque remains authoritative for new messages
        # received meanwhile.
        self._load_failed_channels: set[str] = set()
        # A deferred observe load has not yet been merged into the deque.
        # Compaction must not publish that incomplete window; remember the
        # suppressed request and its latest snapshot so the apply step can
        # republish exactly once without losing entries around the trigger.
        self._load_pending_channels: set[str] = set()
        self._load_pending_compactions: set[str] = set()
        self._load_pending_compaction_snapshots: dict[str, list[HistoryEntry]] = {}

    def set_user_name(self, user_id: str, name: str) -> None:
        """Cache a display name for a user ID."""
        if user_id and name:
            self._user_names[user_id] = name

    def set_observe_limits(self, max_entries: int, ttl_secs: int) -> None:
        """Apply live observe limits, resizing every active observe buffer."""
        previous_max_entries = self._observe_max_entries
        self._observe_max_entries = max_entries
        self._observe_ttl_secs = ttl_secs
        for channel_id in self._observe_channels:
            buf = self._channels.get(channel_id)
            if buf is not None and buf.maxlen != max_entries:
                buf = deque(buf, maxlen=max_entries)
                self._channels[channel_id] = buf
            # Republish the persisted file at a LOWER cap: count-triggered
            # compaction only fires on appends, so a channel that goes quiet
            # after the reduction would otherwise keep its file at the old,
            # larger bound. Only from a window that is a COMPLETE view of the
            # file: the worker skips ``wall_ts=None`` records (messages
            # received while observe was off) when it serializes a REWRITE,
            # and while the window still holds any such entry it is not that
            # view — the load merge appends them after the disk entries, so
            # they displace persisted records that are absent from the window, and
            # publishing its persistable slice would replace the file with a
            # strict subset of its own content (or, with no persistable entry
            # at all, serialize to content=None and UNLINK it: a failed
            # boot-time load's window, see set_observe). The observe file must
            # never lose records to a rewrite; the shrink waits for the next
            # complete-view rewrite instead — the count trigger, which sees an
            # all-new window within a cap of appends, or the next successful
            # load, which rebuilds the window at the new cap. The filtered
            # list is passed explicitly so _schedule_terminal's default
            # snapshot is never used here.
            entries = list(buf or ())
            persistable = [entry for entry in entries if entry.wall_ts is not None]
            complete_view = bool(persistable) and len(persistable) == len(entries)
            if complete_view and max_entries < previous_max_entries:
                self._compact(channel_id, rewrite_snapshot=persistable)

    def set_observe(self, channel_id: str) -> None:
        """Enable observe mode for a channel (deeper history buffer).

        If a history file exists on disk, loads it into memory.
        """
        with self._terminal_lock:
            self._observe_channels.add(channel_id)
            generation = self._observe_generation.get(channel_id, 0) + 1
            self._observe_generation[channel_id] = generation
            # Cancel any queued UNLINK from a prior unset_observe BEFORE the
            # load: observe is on again, so the file must survive. The cancel is
            # unconditional — it must not depend on the load succeeding, or a
            # transient read failure (fd exhaustion, mount blip) would leave the
            # stale unlink armed to delete a still-valid file. A snapshot the
            # worker has already popped is beyond reach; the superseding REWRITE
            # after a successful load covers that in-flight case.
            pending = self._pending_terminal.get(channel_id)
            if pending is not None and pending[1] == _OP_UNLINK:
                self._pending_terminal.pop(channel_id, None)

        # Upgrade existing buffer to larger capacity
        buf = self._channels.get(channel_id)
        if buf is not None and buf.maxlen != self._observe_max_entries:
            new_buf: deque[HistoryEntry] = deque(buf, maxlen=self._observe_max_entries)
            self._channels[channel_id] = new_buf

        # Resolve/trust and read on the history lane. A gateway-loop caller
        # returns immediately; a synchronous non-loop caller waits for that
        # same lane operation so the long-standing synchronous API stays intact.
        self._load_observe(channel_id)

    def unset_observe(self, channel_id: str) -> None:
        """Disable observe mode for a channel (revert to default buffer).

        Keeps the newest ``max_entries`` window in memory — an @mention right
        after observe-off still gets its ordinary channel context. Re-enable
        cannot duplicate what is retained: ``_load_observe`` dedupes the
        disk/memory merge by message identity.
        """
        with self._terminal_lock:
            self._observe_channels.discard(channel_id)
            self._observe_generation[channel_id] = self._observe_generation.get(channel_id, 0) + 1
            # This unlink is the user's deliberate removal, not publication of a
            # possibly incomplete window. Once requested, there is no file whose
            # unseen prefix needs failed-load protection.
            self._load_pending_channels.discard(channel_id)
            self._load_pending_compactions.discard(channel_id)
            self._load_pending_compaction_snapshots.pop(channel_id, None)
            self._load_failed_channels.discard(channel_id)
        # The file is removed below, so the count against it is meaningless; drop
        # it rather than let one int per channel ever observed outlive the file.
        self._appends_since_compact.pop(channel_id, None)
        buf = self._channels.get(channel_id)
        if buf is not None and buf.maxlen != self._max_entries:
            new_buf: deque[HistoryEntry] = deque(buf, maxlen=self._max_entries)
            self._channels[channel_id] = new_buf

        # Remove the persisted history file as a terminal op: it is coalesced
        # per channel and wins against queued or dropped writes.
        self._schedule_terminal(channel_id, _OP_UNLINK)

    def push(
        self,
        channel_id: str,
        user: str,
        text: str,
        thread_ts: str | None = None,
        msg_ts: str | None = None,
    ) -> None:
        """Record a message in the channel buffer.

        Called on every message event in the gateway, not just @mentions.
        Evicts stale entries (TTL) and oldest entries (capacity).
        """
        if not channel_id or not text:
            return

        is_observe = channel_id in self._observe_channels

        buf = self._channels.get(channel_id)
        if buf is None:
            maxlen = self._observe_max_entries if is_observe else self._max_entries
            buf = deque(maxlen=maxlen)
            self._channels[channel_id] = buf

        # Evict expired entries
        self._evict(buf, channel_id)

        # Build entry — observe channels use wall clock for persistence
        wall_ts = time.time() if is_observe else None
        entry = HistoryEntry(
            user=user[:HISTORY_MAX_ID_CHARS],
            text=text[:HISTORY_MAX_TEXT_CHARS],
            thread_ts=_bounded_optional_id(thread_ts),
            msg_ts=_bounded_optional_id(msg_ts),
            wall_ts=wall_ts,
        )
        buf.append(entry)

        # Persist to disk for observe channels
        if is_observe:
            self._append_to_disk(channel_id, entry)

    def context_for(self, channel_id: str, thread_ts: str | None = None) -> str:
        """Format recent messages for injection into LLM context.

        When *thread_ts* is provided, messages are split into current-thread
        and other-thread sections so the LLM can distinguish them.
        Returns empty string if no relevant history exists.
        """
        buf = self._channels.get(channel_id)
        if not buf:
            return ""

        # Evict expired before formatting
        self._evict(buf, channel_id)

        if not buf:
            return ""

        now_mono = time.monotonic()
        now_wall = time.time()

        def _fmt(entry: HistoryEntry) -> str:
            if entry.wall_ts is not None:
                ago = int(now_wall - entry.wall_ts)
            else:
                ago = int(now_mono - entry.timestamp)
            age_str = f"{ago}s ago" if ago < 60 else f"{ago // 60}m ago"
            text = entry.text[:300]
            if len(entry.text) > 300:
                text += "\u2026"
            display = self._user_names.get(entry.user) or entry.user
            return f"  {display} ({age_str}): {text}"

        # Split by thread if thread_ts provided — only include current thread
        if thread_ts:
            # Include messages whose msg_ts matches thread_ts — this captures the
            # thread parent, whose own thread_ts is None until it receives a reply.
            current = [_fmt(e) for e in buf if e.thread_ts == thread_ts or e.msg_ts == thread_ts]
            if not current:
                return ""
            return (
                "[Recent channel messages for context:]\n"
                "[Current thread:]\n" + "\n".join(current) + "\n[End of channel context]\n\n"
            )

        # No thread_ts — only include top-level (non-thread) messages
        lines: list[str] = [_fmt(e) for e in buf if e.thread_ts is None]
        return (
            "[Recent channel messages for context:]\n"
            + "\n".join(lines)
            + "\n[End of channel context]\n\n"
        )

    def clear(self, channel_id: str) -> None:
        """Clear history for a specific channel."""
        self._channels.pop(channel_id, None)

    @property
    def channel_count(self) -> int:
        """Number of channels with buffered history."""
        return len(self._channels)

    def entry_count(self, channel_id: str) -> int:
        """Number of entries in a specific channel buffer."""
        buf = self._channels.get(channel_id)
        return len(buf) if buf else 0

    # ── Persistence helpers ──────────────────────────────────────────────

    @staticmethod
    def _filesystem_identity(info: os.stat_result) -> tuple[int, int] | None:
        """Return a usable directory identity, or ``None`` if unavailable."""
        device = getattr(info, "st_dev", None)
        inode = getattr(info, "st_ino", None)
        if not isinstance(device, int) or not isinstance(inode, int):
            return None
        if device == 0 and inode == 0:
            return None
        return device, inode

    def _prepare_history_root(self) -> tuple[Path, tuple[int, int]]:
        """Resolve, create, and first-trust the configured root exactly once.

        Persistence calls run this on the single history worker lane. The
        synchronous, non-loop ``_observe_path`` compatibility path may also
        initialize it directly; event-loop callers never do. Construction stores
        the raw path without touching the filesystem; the first operation that
        needs persistence resolves the configured/admin-managed link and
        captures the root identity under ``_history_root_lock``. Every later
        operation copies the resulting canonical path and identity without
        resolving again. A vanished or identity-changed trusted root is refused
        by each operation's pinned-descriptor comparison until a new instance
        starts.
        """
        with self._history_root_lock:
            root = self._history_dir
            if root is None:
                raise OSError(errno.ENOENT, "history directory is not configured")
            if self._history_root_identity is not None:
                return root, self._history_root_identity

            root = root.resolve()
            try:
                root.mkdir(parents=True)
            except FileExistsError:
                pass

            dfd = platform_compat.pin_directory(str(root))
            try:
                identity = self._filesystem_identity(os.fstat(dfd))
            finally:
                os.close(dfd)
            if identity is None:
                raise OSError(f"history directory has no stable filesystem identity: {root}")
            self._history_dir = root
            self._history_root_identity = identity
            return root, identity

    def _history_root_snapshot(self) -> tuple[Path, tuple[int, int]] | None:
        """Copy the trusted canonical root without filesystem IO.

        Never blocks: while a preparer holds the lock across its filesystem
        IO, this returns ``None`` (the pre-trust answer) instead of waiting.
        """
        if not self._history_root_lock.acquire(blocking=False):
            return None
        try:
            if self._history_dir is None or self._history_root_identity is None:
                return None
            return self._history_dir, self._history_root_identity
        finally:
            self._history_root_lock.release()

    def _observe_path(self, channel_id: str, history_root: Path | None = None) -> Path | None:
        """Return the JSONL file path for an observe channel, or None.

        A synchronous caller preserves the legacy by-return contract by
        preparing an unresolved root here; an event-loop caller returns None
        instead so filesystem work remains off the loop.
        """
        separators = {"/", "\\", os.sep}
        if os.altsep is not None:
            separators.add(os.altsep)
        if (
            not channel_id
            or channel_id == "."
            or ".." in channel_id
            or any(separator in channel_id for separator in separators)
        ):
            logger.warning("Refusing unsafe history channel id %s", channel_id)
            return None

        if history_root is None:
            root_snapshot = self._history_root_snapshot()
            if root_snapshot is None:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    try:
                        root_snapshot = self._prepare_history_root()
                    except OSError:
                        return None
                else:
                    return None
            history_root = root_snapshot[0]
        from kiro_crew.hooks import is_sensitive_path

        # ``history_root`` is the canonical path captured at first trust. The
        # leaf stays lexical so pinned-dirfd/no-follow operations see and refuse
        # a link planted at its name rather than resolving through it.
        path = history_root / f"{channel_id}.jsonl"
        # Boundary-aware containment: a bare str.startswith has no trailing
        # separator, so a sibling dir sharing the prefix (e.g. ".../hist-evil"
        # vs ".../hist") would pass. is_relative_to compares path components.
        if not path.is_relative_to(history_root):
            logger.warning("Refusing unsafe history path for channel %s", channel_id)
            return None
        if is_sensitive_path(str(path)):
            logger.warning("Refusing sensitive history path for channel %s", channel_id)
            return None
        return path

    def _submit_append(self, channel_id: str, job: Callable[[], None]) -> None:
        """Run a best-effort APPEND job off the event-loop thread, bounded.

        Only the per-message append travels this path. Terminal ops (rewrite,
        unlink) go through :meth:`_schedule_terminal`, which coalesces them
        per channel and never drops them.

        Appends are cheap and disposable: the deque is the source of truth
        and a terminal rewrite republishes it wholesale, so a lost append is
        recaptured by the next count-triggered compaction. Two consequences:

        - The queue is bounded (``_DISK_LANE_MAX_PENDING``, non-blocking
          check): on a stalled disk, an append past the bound is not queued —
          instead a coalesced REWRITE of the whole window is scheduled
          (never dropped, snapshots the deque, which already holds this
          entry), so the file recovers as soon as the lane does rather than
          waiting for the count-triggered compaction. The one window it will
          not publish is a deque still holding pre-observe (``wall_ts=None``)
          entries, which is not a complete view of the file (see
          _schedule_terminal); there the entry waits for the count-triggered
          compaction like any lost append.
        - Each append captures the channel's flush epoch at submit; it is
          superseded only when a terminal op scheduled AFTER it has actually
          PUBLISHED (``_published_epoch``) — that op's snapshot already
          contains this entry, so skipping avoids duplicate lines. A failed
          rewrite publishes nothing and supersedes no queued append, but it
          arms ``_rewrite_failed_channels``, so queued and new appends skip
          disk until a later rewrite succeeds. The deque retains their entries,
          and that successful rewrite persists its snapshot.

        Residual, accepted for this file's purpose (an ephemeral context
        cache): a hard crash can lose work still queued on the lane — the
        same window any buffered writer has, and the queue is normally empty
        because a write is microseconds while messages arrive seconds apart.
        The alternative (writing inline on the loop) is the event-loop stall
        this offload exists to avoid.
        """
        with self._terminal_lock:
            epoch = self._flush_epoch.get(channel_id, 0)
        if not self._disk_lane_slots.acquire(blocking=False):
            logger.warning(
                "History disk lane is saturated (%d pending appends) — "
                "dropping the append for channel %s and scheduling a "
                "coalesced rewrite of the window instead",
                _DISK_LANE_MAX_PENDING,
                channel_id,
            )
            # The rewrite path is coalesced per channel and never dropped, and
            # its content snapshots the deque — which already holds this entry
            # — so the message reaches disk as soon as the lane recovers,
            # instead of waiting for the count-triggered compaction.
            self._compact(channel_id)
            return

        def _job_with_release() -> None:
            try:
                with self._terminal_lock:
                    superseded = self._published_epoch.get(channel_id, 0) > epoch
                if not superseded:
                    job()
            finally:
                self._disk_lane_slots.release()

        try:
            channel_history_executor().submit(_job_with_release)
        except BaseException:
            # The submit itself failed (executor shut down mid-teardown): the
            # job will never run, so hand its slot back.
            self._disk_lane_slots.release()
            raise

    def _schedule_terminal(
        self,
        channel_id: str,
        op: str,
        *,
        rewrite_snapshot: list[HistoryEntry] | None = None,
    ) -> None:
        """Record a terminal disk op for a channel and ensure it flushes.

        Terminal ops are idempotent and last-writer-wins: a REWRITE publishes
        the in-memory window as the whole file, an UNLINK removes the file.
        The entry list is snapshotted HERE, on the calling thread — the
        deque's only mutator — so the window is race-free. The worker performs
        both serialization and IO. Coalescing to at most one pending op per
        channel bounds the queue structurally, and because a rewrite replaces
        the whole file, its correctness does not depend on queue ordering.
        Terminal ops are never dropped: disabling observe (UNLINK) and publishing
        the window (REWRITE) always win over queued appends. The
        pending op coalesces last-writer-wins, so the flush job — queued when
        the FIRST op was scheduled — executes whatever snapshot is NEWEST at
        pop time; an append queued after that first schedule can therefore
        run before or after the flush, and correctness comes from the epoch
        gate instead of arrival order: once a terminal op PUBLISHES, the
        appends it superseded skip their writes (their entries are already
        in its snapshot; see _submit_append), and an append a snapshot does
        not contain is never superseded by it.
        Every terminal op executes on the single-worker lane regardless of
        the calling thread, so execution is serialized, never concurrent —
        but completion order is NOT schedule order: coalescing lets a newer
        snapshot execute in an earlier op's flush slot, and the epoch
        reconciliation above is what makes that reordering safe.
        The canonical root and identity are copied here at schedule time
        without filesystem IO after first trust. An operation queued before
        first trust carries ``None`` and initializes the root when it reaches
        the worker; because this lane has exactly one worker, every later job
        then sees and copies the same canonical pair without re-resolving. The
        worker pins that root at use time and compares the pin with the copied
        identity, so an ancestor swapped after scheduling is refused.
        """
        if self._history_dir is None:
            return
        root_snapshot = self._history_root_snapshot()
        buf = self._channels.get(channel_id)
        snapshot = list(rewrite_snapshot) if rewrite_snapshot is not None else None
        if snapshot is None and op == _OP_REWRITE and buf:
            # Publish only the window's PERSISTABLE entries. Messages received
            # while observe was off carry ``wall_ts=None`` and never reach the
            # file, so a default snapshot is filtered HERE, and no caller can
            # hand the worker a mixed window. While the deque still holds such
            # entries it is NOT a complete view of the file either: the load
            # merge appends them after the disk entries, so in a full window
            # they displace the oldest persisted records and every later push
            # evicts another disk record while they stay — the persistable
            # slice is then a strict subset of the file's own window, and
            # publishing it would destroy the displaced records. Skip the
            # rewrite instead: the file stays a complete append log, and the
            # count trigger republishes once a cap of new messages has evicted
            # the ts-less entries (see _append_to_disk).
            persistable = [entry for entry in buf if entry.wall_ts is not None]
            if len(persistable) < len(buf):
                logger.debug(
                    "Skipping history rewrite for channel %s: the window still holds "
                    "non-persistable entries, so it is not a complete view of the file",
                    channel_id,
                )
                return
            # Snapshot only the newest ``observe_max_entries`` entries: a
            # config reload can shrink the cap while an existing deque still
            # carries the old, larger ``maxlen``, and the published file must
            # honour the CURRENT cap, not the deque's construction-time one.
            cap = self._observe_max_entries
            snapshot = persistable[-cap:] if cap > 0 else persistable
        with self._terminal_lock:
            had_pending = channel_id in self._pending_terminal
            gen = self._flush_epoch.get(channel_id, 0) + 1
            self._flush_epoch[channel_id] = gen
            self._pending_terminal[channel_id] = (
                gen,
                op,
                snapshot,
                root_snapshot,
            )
        if had_pending:
            return
        try:
            channel_history_executor().submit(lambda: self._flush_terminal(channel_id))
        except BaseException:
            with self._terminal_lock:
                self._pending_terminal.pop(channel_id, None)
            raise

    def _flush_terminal(self, channel_id: str) -> None:
        """Worker-side flush: pop the latest pending snapshot and do the IO.

        The lock is held only for the O(1) pop — never across serialization
        or disk IO. The entry list was snapshotted at schedule time, so the
        worker never iterates the live deque.
        """
        with self._terminal_lock:
            if channel_id not in self._pending_terminal:
                return
            (
                gen,
                op,
                snapshot,
                root_snapshot,
            ) = self._pending_terminal.pop(channel_id)
        try:
            history_root, root_identity = root_snapshot or self._prepare_history_root()
        except OSError:
            if op == _OP_REWRITE:
                with self._terminal_lock:
                    self._rewrite_failed_channels.add(channel_id)
            logger.warning(
                "Failed to prepare history root for channel %s", channel_id, exc_info=True
            )
            return
        path = self._observe_path(channel_id, history_root)
        if path is None:
            return
        content: str | None = None
        if snapshot is not None:
            lines = []
            for entry in snapshot:
                if entry.wall_ts is None:
                    continue
                line = json.dumps(
                    {
                        "user": entry.user,
                        "text": entry.text,
                        "thread_ts": entry.thread_ts,
                        "msg_ts": entry.msg_ts,
                        "ts": entry.wall_ts,
                    },
                    ensure_ascii=False,
                )
                lines.append(line + "\n")
            if lines:
                content = "".join(lines)
        self._write_terminal(
            channel_id,
            content,
            gen,
            path,
            root_identity,
        )

    def _write_terminal(
        self,
        channel_id: str,
        content: str | None,
        gen: int,
        path: Path,
        root_identity: tuple[int, int],
    ) -> None:
        """Perform one terminal op's disk IO. ``None`` removes the file.

        Publication is gated on SUCCESS: only when the unlink or the
        ``atomic_write`` actually completed does ``_published_epoch`` advance
        to this op's generation, releasing the appends this op superseded. A
        failed write publishes nothing — the queued appends run and land
        their entries, so a disk error costs the coalesced snapshot, never
        the messages behind it.

        ``path`` was derived on the worker from the canonical root snapshot
        captured at schedule time (or first trusted by this pre-trust job); it
        is never re-resolved after scheduling. The IO holds the same parent pin
        and identity as the appends (:func:`platform_compat.pin_directory`): a
        swapped-in link fails the pin, while a different real directory reached
        through a swapped ancestor fails the identity check. The rewrite passes the pinned
        descriptor to ``atomic_write`` (``parent_dir_fd``); the unlink
        resolves relative to it where the platform supports ``dir_fd``, and
        by name under the held pin elsewhere (Windows, where the pin itself
        blocks a parent swap for as long as it lives).
        """
        dfd = -1
        try:
            try:
                dfd = platform_compat.pin_directory(str(path.parent))
            except FileNotFoundError:
                if content is not None:
                    raise
                # Parent already gone: nothing to unlink — the op succeeded.
            else:
                pinned_identity = self._filesystem_identity(os.fstat(dfd))
                if pinned_identity is None:
                    logger.warning(
                        "Refusing terminal history operation because the pinned history "
                        "directory has no stable identity: %s",
                        path.parent,
                    )
                    return
                if pinned_identity != root_identity:
                    logger.warning(
                        "Refusing terminal history operation because the history directory "
                        "identity changed: %s",
                        path.parent,
                    )
                    return
                if content is None:
                    if _SUPPORTS_DIR_FD:
                        try:
                            os.unlink(path.name, dir_fd=dfd)
                        except FileNotFoundError:
                            pass
                    else:
                        path.unlink(missing_ok=True)
                else:
                    # Gate on atomic_write's OWN probe, not _SUPPORTS_DIR_FD:
                    # atomic_write REFUSES a descriptor with ValueError when
                    # pinned_parent_replace_supported() is false (it also needs
                    # renameat, which our probe does not check), and the worker's
                    # except-OSError below would not catch that — every
                    # compaction would then die silently inside the lane.
                    # Keep this in lockstep with _append_to_disk's 0o600 open:
                    # both paths publish the same credential-bearing history file.
                    # The lockstep is the POSIX mode only — the append opens 0o600
                    # and fchmods a wider pre-existing file there, while
                    # fchmod_safe makes mode= a no-op on Windows, where both
                    # paths leave the history directory's inherited ACL.
                    atomic_write(
                        path,
                        content,
                        mode=0o600,
                        parent_dir_fd=dfd if pinned_parent_replace_supported() else None,
                    )
        except OSError:
            verb = "remove" if content is None else "compact"
            if content is not None:
                with self._terminal_lock:
                    self._rewrite_failed_channels.add(channel_id)
            logger.warning("Failed to %s history file %s", verb, path, exc_info=True)
            return
        finally:
            if dfd != -1:
                os.close(dfd)
        with self._terminal_lock:
            self._rewrite_failed_channels.discard(channel_id)
            if gen > self._published_epoch.get(channel_id, 0):
                self._published_epoch[channel_id] = gen

    def _append_to_disk(self, channel_id: str, entry: HistoryEntry) -> None:
        """Append a single entry to the channel's JSONL file.

        The append runs deferred on the disk lane, so the path must not
        follow a symlink — or a Windows junction — swapped in after
        scheduling, neither at the leaf nor at its parent directory. The
        worker pins the parent with :func:`platform_compat.pin_directory`
        (the shared helper the backup/staging writers use) for the whole
        open+write: it refuses a link, a file, or a reparse point sitting at
        the parent's name, and on Windows the held handle also blocks
        renaming or deleting the directory for as long as it lives. On POSIX
        the leaf is then opened RELATIVE to the pinned descriptor with
        ``O_NOFOLLOW`` (openat — nothing re-walks the full path); on Windows
        the leaf is opened with
        :func:`platform_compat.open_append_no_reparse`, which never follows
        a reparse point — a plain open would resolve a UNC-target link and
        fire outbound SMB/NTLM authentication before any post-open check.
        Either way the failure is logged like any other append error, the
        entry stays in the deque, and the next compaction republishes it
        through ``atomic_write``'s own hardened path.
        """
        if self._history_dir is None:
            return
        root_snapshot = self._history_root_snapshot()
        line = json.dumps(
            {
                "user": entry.user,
                "text": entry.text,
                "thread_ts": entry.thread_ts,
                "msg_ts": entry.msg_ts,
                "ts": entry.wall_ts,
            },
            ensure_ascii=False,
        )

        def _append() -> None:
            with self._terminal_lock:
                disk_write_suppressed = (
                    channel_id in self._rewrite_failed_channels
                    or channel_id in self._load_failed_channels
                )
            if disk_write_suppressed:
                return
            try:
                history_root, root_identity = root_snapshot or self._prepare_history_root()
            except OSError:
                logger.warning(
                    "Failed to prepare history root for channel %s", channel_id, exc_info=True
                )
                return
            path = self._observe_path(channel_id, history_root)
            if path is None:
                return
            dfd = -1
            fd = -1
            try:
                # Pin the parent for the whole open+write
                # (platform_compat.pin_directory — the shared helper the
                # backup/staging writers use). It refuses a symlink, a file,
                # or a Windows junction sitting at the parent's name, and on
                # Windows the held handle also blocks renaming or deleting
                # the directory (or anything above it) for as long as it
                # lives — so the path cannot be re-pointed between our check
                # and the write on any platform.
                dfd = platform_compat.pin_directory(str(path.parent))
                pinned_identity = self._filesystem_identity(os.fstat(dfd))
                if pinned_identity is None:
                    logger.warning(
                        "Refusing history append because the pinned history "
                        "directory has no stable identity: %s",
                        path.parent,
                    )
                    return
                if pinned_identity != root_identity:
                    logger.warning(
                        "Refusing history append because the history directory "
                        "identity changed: %s",
                        path.parent,
                    )
                    return
                if _SUPPORTS_DIR_FD:
                    # POSIX: resolve the leaf RELATIVE to the pinned
                    # descriptor (openat) — nothing re-walks the full path —
                    # with O_NOFOLLOW refusing a linked leaf. O_NONBLOCK
                    # prevents a FIFO with no reader from wedging the sole
                    # history worker before the descriptor can be inspected.
                    flags = (
                        os.O_WRONLY
                        | os.O_APPEND
                        | os.O_CREAT
                        | _O_NOFOLLOW
                        | getattr(os, "O_NONBLOCK", 0)
                    )
                    fd = os.open(path.name, flags, 0o600, dir_fd=dfd)
                else:
                    # Windows: open under the held pin WITHOUT following a
                    # reparse point (platform_compat.open_append_no_reparse) —
                    # a plain open would resolve a UNC-target link and fire
                    # outbound SMB/NTLM authentication before any post-open
                    # check could refuse it.
                    fd = platform_compat.open_append_no_reparse(path)
                opened_mode = os.fstat(fd).st_mode
                if not stat.S_ISREG(opened_mode):
                    raise OSError(
                        errno.EINVAL,
                        "history file is not a regular file",
                        str(path),
                    )
                f = os.fdopen(fd, "a", encoding="utf-8")
                fd = -1  # ownership moved to f
                with f:
                    if _SUPPORTS_DIR_FD and stat.S_IMODE(opened_mode) != 0o600:
                        platform_compat.fchmod_safe(f.fileno(), 0o600)
                    f.write(line + "\n")
            except OSError:
                logger.warning("Failed to append to history file %s", path, exc_info=True)
            finally:
                if fd != -1:
                    os.close(fd)
                if dfd != -1:
                    os.close(dfd)

        self._submit_append(channel_id, _append)
        # Fold the append log back onto the window it feeds. ``_load_observe``
        # reads every line into a ``maxlen=observe_max_entries`` deque, so a
        # record past the newest ``observe_max_entries`` is parsed and then
        # immediately evicted — it costs disk and load time and can never reach
        # a caller. Compacting once per cap's worth of appends keeps the file
        # under twice the cap while charging one rewrite per that many messages,
        # rather than a read-and-rewrite on every message. The TTL-triggered
        # compaction in ``_load_observe`` stays: it bounds by age, this by count.
        # The counter lives on the calling thread — the deque's only mutator —
        # and counts append attempts: a failed or failure-suppressed append
        # still advances it, keeping the rewrite retry path armed.
        count = self._appends_since_compact.get(channel_id, 0) + 1
        if count >= self._observe_max_entries:
            with self._terminal_lock:
                load_failed = channel_id in self._load_failed_channels
                load_pending = channel_id in self._load_pending_channels
            if load_failed and not load_pending:
                # A failed load suppresses every disk write, and nothing else
                # in the process re-reads the file (``set_observe`` is issued
                # once per channel), so a transient failure — fd exhaustion, a
                # mount blip — would otherwise freeze persistence for the rest
                # of the process. Spend this count window on ONE retry of the
                # deferred load on the same lane. A success folds the complete
                # file into memory, lifts the suppression and republishes the
                # window through the compaction below (deferred while the load
                # is pending and replayed when it lands; scheduled directly
                # when the load ran synchronously). A still failing load
                # re-arms the suppression, the compaction below stays
                # suppressed, and the next retry is another cap of appends
                # away. A retry already in flight is not doubled.
                self._load_observe(channel_id)
            self._compact(channel_id)
            self._appends_since_compact[channel_id] = 0
        else:
            self._appends_since_compact[channel_id] = count

    def _open_observe_file(self, path: Path, root_identity: tuple[int, int]) -> BinaryIO:
        """Open the observe leaf for READING without following a link.

        The read gets the same guard as the three mutation paths
        (``_append_to_disk``, ``_write_terminal``): the parent is pinned with
        :func:`platform_compat.pin_directory` — a link, a file or a Windows
        junction at the directory's name is refused — and its identity must
        match the root captured at the trusted setup point. The leaf is then
        opened RELATIVE to that descriptor with ``O_NOFOLLOW`` (openat) on
        POSIX, or through :func:`platform_compat.open_file_no_reparse` on
        Windows, so a symlink planted at ``<channel>.jsonl`` cannot redirect
        the load outside the history root. ``O_NONBLOCK`` makes the open of a
        FIFO at the name return at once instead of waiting for a writer, and
        the ``fstat`` on the descriptor then refuses anything that is not a
        regular file — the only shape this cache ever writes. There is no
        ``exists()`` probe: an absent file raises ``FileNotFoundError`` from
        the open itself, so there is no check-to-open window to plant into.
        """
        dfd = -1
        fd = -1
        try:
            dfd = platform_compat.pin_directory(str(path.parent))
            pinned_identity = self._filesystem_identity(os.fstat(dfd))
            if pinned_identity is None:
                raise OSError(
                    errno.EINVAL,
                    "history directory has no stable filesystem identity",
                    str(path.parent),
                )
            if pinned_identity != root_identity:
                raise OSError(
                    errno.EPERM,
                    "history directory identity changed",
                    str(path.parent),
                )
            if _SUPPORTS_DIR_FD:
                flags = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                fd = os.open(path.name, flags, dir_fd=dfd)
            else:
                fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "history file is not a regular file", str(path))
            f = os.fdopen(fd, "rb")
        except BaseException:
            if fd != -1:
                os.close(fd)
            raise
        finally:
            if dfd != -1:
                os.close(dfd)
        return f

    def _load_observe(self, channel_id: str) -> list[HistoryEntry] | None:
        """Load on the history lane, then merge on the caller thread.

        Gateway-loop callers return immediately and receive the parsed result
        through ``call_soon_threadsafe``. Synchronous callers wait for the same
        lane operation so tests and non-async integrations keep their existing
        by-return visibility. In both shapes every filesystem operation,
        including first-use resolution and identity capture, stays on the lane.
        """
        if self._history_dir is None:
            return None
        with self._terminal_lock:
            generation = self._observe_generation.get(channel_id, 0)
            self._load_pending_channels.add(channel_id)
            # Captured HERE, not when the result lands: only a terminal op
            # scheduled in this process BEFORE this re-enable (an observe
            # off-then-on toggle) can have an unlink in flight. A compaction
            # scheduled by pushes that race the deferred read must not turn the
            # load into a window rewrite that supersedes those pushes' appends.
            supersedes_terminal = channel_id in self._flush_epoch
        # A well-formed file stays within two compaction windows, so retaining
        # the newest twice-cap records bounds load memory without dropping any
        # record that normal persistence can produce. An oversized inherited
        # file drops only its oldest records, which the load summary reports.
        read_max_entries = max(1, 2 * self._observe_max_entries)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                result = (
                    channel_history_executor()
                    .submit(
                        self._read_observe,
                        channel_id,
                        generation,
                        read_max_entries,
                    )
                    .result()
                )
            except BaseException:
                self._mark_load_failed(channel_id, generation)
                raise
            return self._apply_observe_load(channel_id, generation, result, supersedes_terminal)

        def _read_and_return() -> None:
            try:
                result = self._read_observe(channel_id, generation, read_max_entries)
            except BaseException:
                self._mark_load_failed(channel_id, generation)
                raise
            try:
                loop.call_soon_threadsafe(
                    self._apply_observe_load,
                    channel_id,
                    generation,
                    result,
                    supersedes_terminal,
                )
            except RuntimeError:
                self._mark_load_failed(channel_id, generation)
                logger.debug("Observe-load loop closed before history could be applied")

        try:
            channel_history_executor().submit(_read_and_return)
        except BaseException:
            self._mark_load_failed(channel_id, generation)
            raise
        return None

    def _mark_load_failed(self, channel_id: str, generation: int) -> None:
        """Atomically hand a current pending load to failed-load suppression."""
        with self._terminal_lock:
            if (
                self._observe_generation.get(channel_id) == generation
                and channel_id in self._observe_channels
            ):
                self._load_pending_channels.discard(channel_id)
                self._load_failed_channels.add(channel_id)

    def _clear_load_failed(self, channel_id: str, generation: int) -> None:
        """Lift failed-load suppression left by an earlier attempt of this session.

        Same guard shape as :meth:`_mark_load_failed`: a stale lane result must
        never touch the state of a newer observe session.
        """
        with self._terminal_lock:
            if (
                self._observe_generation.get(channel_id) == generation
                and channel_id in self._observe_channels
            ):
                self._load_failed_channels.discard(channel_id)

    def _read_observe(
        self,
        channel_id: str,
        generation: int,
        read_max_entries: int,
    ) -> _ObserveLoad | None:
        """Worker-side resolve, trust, and parse of one observe file."""
        try:
            history_root, root_identity = self._prepare_history_root()
        except OSError:
            self._mark_load_failed(channel_id, generation)
            logger.warning(
                "Failed to prepare history root for channel %s", channel_id, exc_info=True
            )
            return None
        path = self._observe_path(channel_id, history_root)
        if path is None:
            return None

        entries: deque[HistoryEntry] = deque(maxlen=read_max_entries)
        loaded_entries = 0
        fields_sliced = False

        try:
            f = self._open_observe_file(path, root_identity)
        except FileNotFoundError:
            # Nothing persisted for this channel. That is a SUCCESSFUL read of
            # an empty file, not a failure: with no file there is no unseen
            # prefix a rewrite could destroy, so suppression armed by an
            # earlier transient failure of this session would only cost
            # liveness. A failure keeps it (see the arms below).
            self._clear_load_failed(channel_id, generation)
            return None
        except OSError:
            # A link, FIFO, device or directory at the leaf — or a link at the
            # parent — never reaches the window; the file is only a cache.
            self._mark_load_failed(channel_id, generation)
            logger.warning("Refusing to read history file %s", path, exc_info=True)
            return None

        try:
            with f:
                for line_no, raw_bytes in enumerate(f, 1):
                    try:
                        raw = raw_bytes.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        fields_sliced = True
                        logger.warning(
                            "Corrupt UTF-8 JSONL line %d in %s — skipping",
                            line_no,
                            path,
                        )
                        continue
                    if not raw:
                        # A blank line has no producer in this writer; mark the file for
                        # rewrite so a padded file folds to the bound.
                        fields_sliced = True
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        # Mark malformed content dirty so compaction rewrites a clean view
                        # and truncates a torn tail before the next append can join it.
                        fields_sliced = True
                        logger.warning("Corrupt JSONL line %d in %s — skipping", line_no, path)
                        continue
                    if not isinstance(data, dict):
                        fields_sliced = True
                        logger.warning(
                            "Invalid history record for channel %s at line %d — skipping",
                            channel_id,
                            line_no,
                        )
                        continue
                    user = data.get("user")
                    text = data.get("text")
                    thread_ts = data.get("thread_ts")
                    msg_ts = data.get("msg_ts")
                    wall_ts = data.get("ts")
                    if (
                        not isinstance(user, str)
                        or not isinstance(text, str)
                        or not (thread_ts is None or isinstance(thread_ts, str))
                        or not (msg_ts is None or isinstance(msg_ts, str))
                        or not (
                            wall_ts is None
                            or (not isinstance(wall_ts, bool) and isinstance(wall_ts, (int, float)))
                        )
                    ):
                        fields_sliced = True
                        logger.warning(
                            "Invalid history record for channel %s at line %d — skipping",
                            channel_id,
                            line_no,
                        )
                        continue
                    if wall_ts is None:
                        # No writer persists a ts-less record; treat it as corruption and
                        # mark it for rewrite.
                        fields_sliced = True
                        continue
                    try:
                        wall_ts = float(wall_ts)
                    except (OverflowError, ValueError):
                        fields_sliced = True
                        logger.warning(
                            "Invalid history record for channel %s at line %d — skipping",
                            channel_id,
                            line_no,
                        )
                        continue
                    if not math.isfinite(wall_ts):
                        fields_sliced = True
                        logger.warning(
                            "Invalid history record for channel %s at line %d — skipping",
                            channel_id,
                            line_no,
                        )
                        continue
                    if (
                        len(user) > HISTORY_MAX_ID_CHARS
                        or len(text) > HISTORY_MAX_TEXT_CHARS
                        or (thread_ts is not None and len(thread_ts) > HISTORY_MAX_ID_CHARS)
                        or (msg_ts is not None and len(msg_ts) > HISTORY_MAX_ID_CHARS)
                    ):
                        fields_sliced = True
                    # Convert wall clock to a monotonic offset so existing code works
                    age_secs = time.time() - wall_ts
                    mono_ts = time.monotonic() - age_secs
                    entries.append(
                        HistoryEntry(
                            user=user[:HISTORY_MAX_ID_CHARS],
                            text=text[:HISTORY_MAX_TEXT_CHARS],
                            thread_ts=_bounded_optional_id(thread_ts),
                            msg_ts=_bounded_optional_id(msg_ts),
                            timestamp=mono_ts,
                            wall_ts=wall_ts,
                        )
                    )
                    loaded_entries += 1
        except OSError:
            self._mark_load_failed(channel_id, generation)
            logger.warning("Failed to read history file %s", path, exc_info=True)
            return None

        return _ObserveLoad(
            entries=entries,
            loaded_entries=loaded_entries,
            fields_sliced=fields_sliced,
        )

    def _apply_observe_load(
        self,
        channel_id: str,
        generation: int,
        result: _ObserveLoad | None,
        supersedes_terminal: bool,
    ) -> list[HistoryEntry] | None:
        """Merge one current lane result and schedule any required rewrite."""
        with self._terminal_lock:
            if (
                self._observe_generation.get(channel_id) != generation
                or channel_id not in self._observe_channels
            ):
                return None
            self._load_pending_channels.discard(channel_id)
            if result is not None:
                # The lane read the complete file, so no persisted prefix is
                # unseen any more: lift failed-load suppression the moment the
                # result lands, not only after the merge below, so no later
                # early return can leave a transient failure armed for the
                # rest of the process. A None result keeps whatever the lane
                # decided: a failed read armed it (``_mark_load_failed``) and
                # an absent file already cleared it (``_read_observe``).
                self._load_failed_channels.discard(channel_id)
            compaction_suppressed = channel_id in self._load_pending_compactions
            compaction_snapshot = list(self._load_pending_compaction_snapshots.get(channel_id, ()))
            load_failed = channel_id in self._load_failed_channels
            if result is None and not load_failed:
                self._load_pending_compactions.discard(channel_id)
                self._load_pending_compaction_snapshots.pop(channel_id, None)

        def _identity(e: HistoryEntry) -> object:
            return e.msg_ts if e.msg_ts else (e.user, e.text, e.thread_ts, e.wall_ts)

        cutoff = time.time() - self._observe_ttl_secs
        cap = self._observe_max_entries

        def _persistable_snapshot(source: Iterable[HistoryEntry]) -> list[HistoryEntry]:
            snapshot: list[HistoryEntry] = []
            snapshot_seen: set[object] = set()
            for entry in source:
                if entry.wall_ts is None or entry.wall_ts < cutoff:
                    continue
                key = _identity(entry)
                if key in snapshot_seen:
                    continue
                snapshot_seen.add(key)
                snapshot.append(entry)
            return snapshot[-cap:] if cap > 0 else snapshot

        if result is None:
            if compaction_suppressed and not load_failed:
                replay_snapshot = _persistable_snapshot(
                    (*compaction_snapshot, *self._channels.get(channel_id, ()))
                )
                if replay_snapshot:
                    self._compact(channel_id, rewrite_snapshot=replay_snapshot)
                    self._appends_since_compact[channel_id] = 0
            elif supersedes_terminal and not load_failed:
                # A successful absent-file re-enable has no unseen persisted
                # prefix. Publish its retained window through the same
                # persistable, deduped, capped gate as a file-present load.
                retained_snapshot = _persistable_snapshot(self._channels.get(channel_id, ()))
                if retained_snapshot:
                    self._schedule_terminal(
                        channel_id,
                        _OP_REWRITE,
                        rewrite_snapshot=retained_snapshot,
                    )
                    self._appends_since_compact[channel_id] = 0
            return None

        active_entries = [
            entry
            for entry in result.entries
            if entry.wall_ts is not None and entry.wall_ts >= cutoff
        ]
        had_expired = len(active_entries) < result.loaded_entries
        entries: deque[HistoryEntry] = deque(active_entries, maxlen=cap)
        fields_sliced = result.fields_sliced
        read_dropped_count = result.loaded_entries - len(result.entries)
        dropped_count = read_dropped_count + len(active_entries) - len(entries)
        dropped_over_cap = dropped_count > 0

        # Populate deque (recent entries only, respecting maxlen)
        buf = self._channels.get(channel_id)
        if buf is None:
            buf = deque(maxlen=self._observe_max_entries)
            self._channels[channel_id] = buf

        # Merge: disk entries first (older), then any already in-memory —
        # DEDUPED by message identity, so a re-enable that reloads the file
        # into a deque still holding the same entries (observe toggled
        # off-then-on) keeps exactly one copy of each. Identity is msg_ts
        # when present (unique per Slack message); entries without one fall
        # back to (user, text, thread_ts, wall_ts).
        existing = list(buf)
        active_existing = [
            entry for entry in existing if entry.wall_ts is None or entry.wall_ts >= cutoff
        ]
        had_expired = had_expired or len(active_existing) < len(existing)
        existing = active_existing
        buf.clear()
        seen: set[object] = set()

        # Build the file-publication view independently of the capped mixed
        # context buffer. Non-persistable messages received while observe was
        # off have ``wall_ts=None``; letting those entries consume deque slots
        # before a load-time rewrite would evict valid disk records and make
        # that loss permanent. The context deque may still hold the mixed
        # window, while rewrites retain the newest cap of persistable entries.
        persistable_snapshot = _persistable_snapshot((*entries, *existing))

        for e in entries:
            key = _identity(e)
            if key in seen:
                continue
            seen.add(key)
            buf.append(e)
        for e in existing:
            key = _identity(e)
            if key in seen:
                continue
            seen.add(key)
            buf.append(e)

        # The complete persisted file has now been folded into the window, so
        # the compaction requests deferred behind this load can be dropped —
        # the rewrite decided below publishes the merged window. Re-check the
        # generation before clearing them in case another thread toggled
        # observe mid-merge. (Failed-load suppression was lifted when the
        # result landed, above.)
        with self._terminal_lock:
            if (
                self._observe_generation.get(channel_id) != generation
                or channel_id not in self._observe_channels
            ):
                return None
            self._load_pending_compactions.discard(channel_id)
            self._load_pending_compaction_snapshots.pop(channel_id, None)
        logger.info(
            "Loaded %d entries for channel %s from disk; dropped %d over cap",
            len(entries),
            channel_id,
            dropped_count,
        )

        # Lazy compaction: drop expired entries, fold a file that already
        # holds more than the window back onto it, and persist bounded fields.
        # Count separately because a maxlen deque cannot reveal its evictions
        # from its final length.
        if had_expired or dropped_over_cap or fields_sliced:
            self._compact(channel_id, rewrite_snapshot=persistable_snapshot)
        # Supersede an UNLINK the worker already popped and is executing:
        # terminal ops coalesce last-writer-wins per channel and run in
        # schedule order on the single worker, so publishing the window here
        # lands after the in-flight unlink and restores the file. Only when
        # the window has entries — a REWRITE serialized from an empty buffer
        # is content=None, which _write_terminal executes as an unlink; the
        # empty-window case is already safe because set_observe cancelled the
        # pending unlink. Gated on a terminal op having been scheduled for
        # this channel in THIS process before set_observe ran (see
        # _load_observe): only a real observe off-then-on transition can have
        # an unlink in flight, so gateway boot schedules no data-scaled
        # serialization work. A successful load is required: the observe file
        # must never be overwritten by a strict subset of its own content.
        if persistable_snapshot and buf and (supersedes_terminal or compaction_suppressed):
            self._schedule_terminal(
                channel_id,
                _OP_REWRITE,
                rewrite_snapshot=persistable_snapshot,
            )
        self._appends_since_compact[channel_id] = 0
        return persistable_snapshot

    def _compact(
        self,
        channel_id: str,
        *,
        rewrite_snapshot: list[HistoryEntry] | None = None,
    ) -> None:
        """Publish the in-memory window as the whole file (or remove it).

        Every trigger (TTL on load, size on load, count on append) reaches
        this through ``push``/``set_observe`` calls on the gateway's
        event-loop thread, so the actual ``atomic_write`` runs as a coalesced
        terminal op off the loop (:meth:`_schedule_terminal`), which
        snapshots the window at schedule time — an empty window becomes an
        unlink, and a window still holding non-persistable entries is not
        published at all. A pending load suppresses subset publication and
        records the request for apply-time replay; a failed load keeps it
        suppressed until a later successful load has folded the complete file
        into memory.
        """
        with self._terminal_lock:
            if channel_id in self._load_pending_channels:
                self._load_pending_compactions.add(channel_id)
                snapshot = (
                    list(rewrite_snapshot)
                    if rewrite_snapshot is not None
                    else list(self._channels.get(channel_id, ()))
                )
                self._load_pending_compaction_snapshots[channel_id] = snapshot
                return
            if channel_id in self._load_failed_channels:
                return
        self._schedule_terminal(
            channel_id,
            _OP_REWRITE,
            rewrite_snapshot=rewrite_snapshot,
        )

    def _evict(self, buf: deque[HistoryEntry], channel_id: str | None = None) -> None:
        """Remove entries older than TTL from the front of the deque."""
        is_observe = channel_id and channel_id in self._observe_channels
        ttl = self._observe_ttl_secs if is_observe else self._ttl_secs
        if is_observe:
            # Observe channels use wall clock
            cutoff = time.time() - ttl
            while buf and buf[0].wall_ts is not None and buf[0].wall_ts < cutoff:
                buf.popleft()
        else:
            # Non-observe channels use monotonic clock
            cutoff = time.monotonic() - ttl
            while buf and buf[0].timestamp < cutoff:
                buf.popleft()
