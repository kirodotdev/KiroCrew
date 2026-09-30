"""Channel History Buffer — rolling window of recent messages per channel.

Captures all messages in group channels (not just @mentions) so that
when the agent is invoked, it has conversational context about what
was being discussed.

Non-observe channels are ephemeral / in-memory only.  Observe-mode
channels are persisted to disk as JSONL so history survives restarts.

The persisted history is a pair of generations, ``<channel>.jsonl`` (live)
and ``<channel>.jsonl.1`` (older). Every observed message is appended to the
live file; once the live file holds ``observe_max_entries`` records it is
renamed over the older generation and the next append starts a fresh live
file. Rotation is one rename — no record is ever re-serialized, so the file
on disk is always exactly what was appended to it, and total disk use stays
at about twice the cap while at least one full generation is retained.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable

from kiro_crew import jsonl_util, platform_compat
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

# Six-byte JSON escapes dominate bounded fields; 1 KiB covers keys, separators and ``ts``.
_HISTORY_RECORD_BYTE_CAP = 6 * (HISTORY_MAX_TEXT_CHARS + 3 * HISTORY_MAX_ID_CHARS) + 1024

#: Suffix of the older generation beside the live history file.
_OLDER_GENERATION_SUFFIX = ".1"


def _bounded_optional_id(value: str | None) -> str | None:
    return value[:HISTORY_MAX_ID_CHARS] if value is not None else None


#: ``O_NOFOLLOW`` refuses to open a symlink at all; ``O_DIRECTORY`` makes
#: "open this only if it is a directory" atomic with the open. Both are 0
#: (no-ops) where the platform lacks them (Windows).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

#: Whether every leaf operation on the history lane can address the leaf
#: RELATIVE to a pinned parent descriptor (openat / renameat / unlinkat):
#: open the directory itself refusing links, then resolve the leaf against
#: that descriptor via ``dir_fd``, so nothing re-walks the full path. Same
#: gate shape as the backup and clone-setup writers. Absent on Windows, where
#: the fallback is the no-reparse opens in ``platform_compat`` plus by-name
#: rename/unlink under the held pin (which there blocks renaming or deleting
#: the directory for as long as it lives).
_SUPPORTS_DIR_FD = (
    os.open in getattr(os, "supports_dir_fd", set())
    and os.rename in getattr(os, "supports_dir_fd", set())
    and os.unlink in getattr(os, "supports_dir_fd", set())
    and _O_NOFOLLOW != 0
    and _O_DIRECTORY != 0
)

#: How many best-effort APPEND jobs may sit queued on the disk lane at once.
#: The queue is normally empty (a write is microseconds, messages arrive
#: seconds apart), so the cap only bites when storage has stalled; an append
#: past it is dropped with a warning while the deque keeps the entry. The
#: unlink an observe-off schedules does not consume these slots: it is one
#: job per user action, so it is bounded structurally.
_DISK_LANE_MAX_PENDING = 64

#: Bytes per read when measuring a history generation's line count, so a
#: newline-free run of any length costs one fixed-size buffer.
_MEASURE_CHUNK_BYTES = 64 * 1024

#: The two grounds on which a load removes a whole generation, as the log
#: line states them: the load measured every record against the TTL, or it
#: found nothing it could parse and so measured nothing.
_EVERY_RECORD_EXPIRED = "every record is past the TTL"
_NO_PARSABLE_RECORD = "holds no parsable record"

#: Seconds a paused channel waits before its first scheduled re-read of its
#: generations, and the ceiling that wait doubles toward while a generation
#: stays unreadable. A pause protects records a load could not read (a link
#: planted at a generation's name, a permission or mount change), and what
#: clears it is a repair minutes to hours later, not the next message: so the
#: retry costs the lane one guarded read a minute at first and one an hour at
#: most, which bounds what a permanently unreadable file costs while keeping
#: the resume within one interval of the repair.
_PAUSE_RETRY_INITIAL_SECS = 60.0
_PAUSE_RETRY_MAX_SECS = 3600.0

#: Monotonic clock the scheduled pause retries read. A module attribute so a
#: test can advance it without moving the clock the entries and the TTL use.
_pause_clock: Callable[[], float] = time.monotonic


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
class _GenerationScan:
    """What one pass over a history generation found, for the load's disk decisions.

    ``readable`` is False when the leaf was refused (a symlink, hard link,
    FIFO, device or directory at the name, or a link at the parent): such a
    generation is left exactly where it is. ``lines`` counts every line,
    valid or not, because every line costs one line of the disk bound.
    ``oldest`` and ``newest`` are the valid records' timestamp range,
    ``None`` when the file holds no valid record.
    """

    readable: bool
    lines: int = 0
    torn: bool = False
    oldest: float | None = None
    newest: float | None = None


@dataclass
class _PauseRetry:
    """When a paused channel's disk appends next re-read its generations.

    ``next_at`` is a reading of ``_pause_clock``; ``interval`` is the wait
    that set it, doubled up to ``_PAUSE_RETRY_MAX_SECS`` each time a retry
    still finds a generation unreadable.
    """

    next_at: float
    interval: float


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
        # the lane returns its parsed result. Guarded by ``_observe_lock``
        # because the result is applied on the loop while a test or a
        # non-loop caller may toggle observe from another thread.
        self._observe_generation: dict[str, int] = {}
        self._observe_lock = threading.Lock()
        self._user_names: dict[str, str] = {}  # user_id -> display name cache
        # Bounds the best-effort append queue (see _DISK_LANE_MAX_PENDING).
        # Released by the worker as each job finishes; acquired non-blocking
        # at submit, so the event-loop thread never waits on it.
        self._disk_lane_slots = threading.Semaphore(_DISK_LANE_MAX_PENDING)
        # Lane-owned rotation state, touched ONLY by jobs running on the
        # single history worker, so it needs no lock. ``_live_records`` is
        # how many records the live file holds — seeded by the load from the
        # file's own line count, advanced by each completed append, reset by a
        # rotation or an unlink — and drives the count-triggered rotation.
        # ``_live_torn`` marks a live file whose last line has no terminating
        # newline (a crash mid-append, or a write that failed part way): the
        # next append starts on a fresh line so the torn tail cannot swallow
        # the record that follows it. ``_live_unknown`` marks a live file that
        # could not be measured after an attempted write failed: later appends
        # remeasure it before any rotation and pause while it remains unreadable.
        # ``_rotate_failed`` holds the channels
        # whose last rotation attempt failed: their disk appends pause until
        # a rotation succeeds, and the refusal is logged once, not per message.
        # ``_disk_paused`` holds the channels whose load found a generation
        # unreadable that a rotation would replace or rename away (the live
        # file beside a readable older generation, or the older generation
        # itself): their disk appends pause, and ``_pause_retry`` says when the
        # lane next re-reads the files. The pause lifts at the channel's next
        # load or at the first scheduled re-read that finds every generation
        # readable; until then no append opens, rotates or writes anything.
        self._live_records: dict[str, int] = {}
        self._live_torn: set[str] = set()
        self._live_unknown: set[str] = set()
        self._rotate_failed: set[str] = set()
        self._disk_paused: set[str] = set()
        self._pause_retry: dict[str, _PauseRetry] = {}

    def set_user_name(self, user_id: str, name: str) -> None:
        """Cache a display name for a user ID."""
        if user_id and name:
            self._user_names[user_id] = name

    def set_observe_limits(self, max_entries: int, ttl_secs: int) -> None:
        """Apply live observe limits, resizing every active observe buffer.

        The disk bound follows on its own: the lane compares the live file's
        record count against the cap in force at each append, so a lowered
        cap rotates at the next append past it and a raised cap lets the live
        file grow to the new cap before rotating. Nothing is rewritten.
        """
        self._observe_max_entries = max_entries
        self._observe_ttl_secs = ttl_secs
        for channel_id in self._observe_channels:
            buf = self._channels.get(channel_id)
            if buf is not None and buf.maxlen != max_entries:
                self._channels[channel_id] = deque(buf, maxlen=max_entries)

    def set_observe(self, channel_id: str) -> None:
        """Enable observe mode for a channel (deeper history buffer).

        If a history file exists on disk, loads it into memory.
        """
        with self._observe_lock:
            self._observe_channels.add(channel_id)
            self._observe_generation[channel_id] = self._observe_generation.get(channel_id, 0) + 1

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
        after observe-off still gets its ordinary channel context. Both
        generations of the persisted history are removed on the lane, after
        every append already queued for the channel.
        """
        with self._observe_lock:
            self._observe_channels.discard(channel_id)
            self._observe_generation[channel_id] = self._observe_generation.get(channel_id, 0) + 1
        buf = self._channels.get(channel_id)
        if buf is not None and buf.maxlen != self._max_entries:
            new_buf: deque[HistoryEntry] = deque(buf, maxlen=self._max_entries)
            self._channels[channel_id] = new_buf

        self._remove_from_disk(channel_id)

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

    @staticmethod
    def _older_generation(path: Path) -> Path:
        """The ``.1`` generation beside the live history file ``path``."""
        return path.with_name(path.name + _OLDER_GENERATION_SUFFIX)

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
        """Return the live JSONL file path for an observe channel, or None.

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

    def _pin_history_parent(self, path: Path, root_identity: tuple[int, int]) -> int:
        """Pin ``path``'s parent for one lane operation and verify its identity.

        Every disk operation on the lane — append, read, rotate, unlink —
        holds the parent open through :func:`platform_compat.pin_directory`
        (the shared helper the backup/staging writers use): it refuses a
        symlink, a file, or a Windows junction sitting at the parent's name,
        and on Windows the held handle also blocks renaming or deleting the
        directory for as long as it lives. The pinned descriptor's identity
        must match the root captured at first trust, so a different real
        directory reached through a swapped ancestor is refused as well.
        Raises ``OSError`` on refusal; the caller owns the returned descriptor
        and releases it with ``os.close``.
        """
        dfd = platform_compat.pin_directory(str(path.parent))
        try:
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
        except BaseException:
            os.close(dfd)
            raise
        return dfd

    def _submit_append(self, channel_id: str, job: Callable[[], None]) -> None:
        """Run a best-effort APPEND job off the event-loop thread, bounded.

        Only the per-message append travels this path; the observe-off
        unlink goes through :meth:`_remove_from_disk` and is never dropped.

        The queue is bounded (``_DISK_LANE_MAX_PENDING``, non-blocking check)
        so a stalled disk cannot accumulate one queued closure per message:
        an append past the bound is dropped with a warning while the deque
        keeps the entry, so in-memory context is unaffected. Residual,
        accepted for this file's purpose (an ephemeral context cache): a
        stalled disk or a hard crash can lose work still queued on the lane —
        the same window any buffered writer has, and the queue is normally
        empty because a write is microseconds while messages arrive seconds
        apart. The alternative (writing inline on the loop) is the event-loop
        stall this offload exists to avoid.
        """
        if not self._disk_lane_slots.acquire(blocking=False):
            logger.warning(
                "History disk lane is saturated (%d pending appends) — "
                "dropping the disk append for channel %s; the entry stays in memory",
                _DISK_LANE_MAX_PENDING,
                channel_id,
            )
            return

        def _job_with_release() -> None:
            try:
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

    def _append_to_disk(self, channel_id: str, entry: HistoryEntry) -> None:
        """Append a single entry to the channel's live JSONL file.

        The append runs deferred on the disk lane, so the path must not
        follow a symlink — or a Windows junction — swapped in after
        scheduling, neither at the leaf nor at its parent directory. The
        worker pins the parent (:meth:`_pin_history_parent`) for the
        open+write. On POSIX the leaf is then opened RELATIVE to the pinned
        descriptor with ``O_NOFOLLOW`` (openat — nothing re-walks the full
        path); on Windows the leaf is opened with
        :func:`platform_compat.open_append_no_reparse`, which never follows
        a reparse point — a plain open would resolve a UNC-target link and
        fire outbound SMB/NTLM authentication before any post-open check.
        Either open is then judged on its descriptor: anything that is not a
        regular file with exactly one link — a FIFO, device or directory, or
        a hard link to a file elsewhere, which no path guard can see — is
        refused before a byte is written or the mode is touched.

        Before the write, the worker rotates the live file aside when the
        lines this write adds would take it past ``observe_max_entries``
        (see :meth:`_rotate_generation`). When that rotation fails the disk
        append is skipped: the live file never exceeds the cap, the entry
        stays in the deque, and every later append retries the rotation
        first, so persistence resumes with the first rename that succeeds.
        The pause is logged once per run of failures, never per message.
        A load that finds a generation unreadable which a rotation would
        replace or rename away — the live file beside a readable older
        generation, or the older generation itself — pauses the channel's disk
        appends the same way: the job returns before it opens, rotates or
        writes anything. That pause lifts at the channel's next load, or on
        its own: once ``_PAUSE_RETRY_INITIAL_SECS`` have passed since the
        pause (doubling after each retry that still fails, up to
        ``_PAUSE_RETRY_MAX_SECS``) the next append re-reads both generations
        through the guarded read a load uses (:meth:`_resume_paused_appends`)
        and, when every generation that exists reads, seeds the live count
        and torn state from that read and carries on with the append.

        An append failure is logged and the entry stays in the deque. After
        an attempted write fails, the live file is measured through the same
        guarded read used by a load, and that measurement replaces its line
        count and torn state. If the measurement fails too, later disk appends
        pause until a fresh measurement or load can seed those values, so an
        unknown count can never trigger a rotation.
        """
        if self._history_dir is None:
            return
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
            paused = channel_id in self._disk_paused
            if paused and not self._pause_retry_due(channel_id):
                # The load logged the pause once; the entry stays in memory
                # and the files on disk stay exactly as the load found them
                # until the scheduled re-read is due.
                return
            try:
                history_root, root_identity = self._prepare_history_root()
            except OSError:
                logger.warning(
                    "Failed to prepare history root for channel %s", channel_id, exc_info=True
                )
                return
            path = self._observe_path(channel_id, history_root)
            if path is None:
                return
            if paused and not self._resume_paused_appends(channel_id, path, root_identity):
                return
            if channel_id in self._live_unknown:
                live = self._measure_generation(path, root_identity)
                if not self._accept_live_measurement(channel_id, live):
                    logger.debug(
                        "History file %s still cannot be measured; disk append for channel %s "
                        "remains paused",
                        path,
                        channel_id,
                    )
                    return
            cap = max(1, self._observe_max_entries)
            # ``json.dumps`` escapes newlines, so each completed append adds
            # exactly one line. A torn live tail is already included in
            # ``_live_records``; its leading newline only terminates that
            # existing line so the new record starts cleanly and is not
            # swallowed on load. Rotate first when the appended record would
            # take the live file past the cap.
            torn = channel_id in self._live_torn
            payload_lines = 1
            if self._live_records.get(channel_id, 0) + payload_lines > cap:
                if not self._rotate_generation(channel_id, path, root_identity):
                    # The live file is full and cannot be moved aside: skip
                    # the disk write rather than grow it past the cap.
                    return
                self._live_records[channel_id] = 0
                self._live_torn.discard(channel_id)
                torn = False
            payload = ("\n" if torn else "") + line + "\n"
            write_attempted = False
            dfd = -1
            fd = -1
            try:
                dfd = self._pin_history_parent(path, root_identity)
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
                # Both arms are judged on the DESCRIPTOR: only a regular file
                # with exactly one link is written or chmod'ed. A hard link
                # shares its target's inode under its own name, so no path
                # guard and neither no-follow open can see it; ``st_nlink``
                # is the one check that does (the shape of
                # ``platform_log_append._regular_single_link``). Refused here,
                # before ``write_attempted`` and before the mode tightening,
                # a planted entry gets neither a record nor a chmod.
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError(
                        errno.EINVAL,
                        "history file is not a regular file with one link",
                        str(path),
                    )
                f = os.fdopen(fd, "a", encoding="utf-8")
                fd = -1  # ownership moved to f
                with f:
                    if _SUPPORTS_DIR_FD and stat.S_IMODE(info.st_mode) != 0o600:
                        # Best effort: a mount that refuses the owner-only
                        # tightening keeps its mode and still gets the record.
                        platform_compat.fchmod_safe(f.fileno(), 0o600)
                    write_attempted = True
                    f.write(payload)
            except OSError:
                if write_attempted:
                    # The write or close failed, so a guarded read replaces
                    # the count and torn flag with the live file's actual end.
                    # An unreadable result marks the count unknown; later
                    # appends remeasure before they may rotate or write.
                    live = self._measure_generation(path, root_identity)
                    self._accept_live_measurement(channel_id, live)
                # Before the write was attempted no byte can have landed: the
                # file's end is unchanged, so neither the count nor the torn
                # flag moves.
                logger.warning("Failed to append to history file %s", path, exc_info=True)
                return
            finally:
                if fd != -1:
                    os.close(fd)
                if dfd != -1:
                    os.close(dfd)
            # The write and close completed, so every newline in the
            # payload landed. Advance the measured count and clear torn state.
            self._live_records[channel_id] = self._live_records.get(channel_id, 0) + payload_lines
            self._live_torn.discard(channel_id)
            self._live_unknown.discard(channel_id)

        self._submit_append(channel_id, _append)

    def _rotate_generation(
        self, channel_id: str, path: Path, root_identity: tuple[int, int]
    ) -> bool:
        """Rename the live file over the older generation, on the lane.

        One rename inside the pinned parent: ``renameat`` relative to the
        pinned descriptor on POSIX, ``os.replace`` under the held pin on
        Windows. A rename operates on the directory entries themselves, so a
        link planted at either name is moved or replaced, never followed. Any
        older ``.1`` is replaced, keeping exactly one previous generation.

        Never raises: a failure is reported as ``False`` and the caller
        pauses the channel's disk appends until a rotation succeeds, so the
        live file stays at the cap. It is logged once per run of consecutive
        failures (a planted directory at the ``.1`` name, say, would
        otherwise warn on every message). An absent live file is nothing to
        rotate and counts as done; an absent or refused history directory is
        a failed rotation. No sibling lock file is taken, unlike
        ``jsonl_util.rotate_jsonl_at``: ``gateway_lock`` admits one gateway
        per home and the single-worker lane is that gateway's only writer of
        these files, so two rotations can never race (the exemption is
        recorded in the channel-history spec).
        """
        older = self._older_generation(path)
        dfd = -1
        try:
            dfd = self._pin_history_parent(path, root_identity)
            try:
                if _SUPPORTS_DIR_FD:
                    os.rename(path.name, older.name, src_dir_fd=dfd, dst_dir_fd=dfd)
                else:
                    os.replace(path, older)
            except FileNotFoundError:
                pass
        except OSError:
            if channel_id in self._rotate_failed:
                logger.debug("Failed to rotate history file %s again", path, exc_info=True)
            else:
                self._rotate_failed.add(channel_id)
                logger.warning(
                    "Failed to rotate history file %s — disk appends for channel %s are "
                    "paused until the rotation succeeds; the in-memory window is unaffected",
                    path,
                    channel_id,
                    exc_info=True,
                )
            return False
        finally:
            if dfd != -1:
                os.close(dfd)
        self._rotate_failed.discard(channel_id)
        return True

    def _remove_from_disk(self, channel_id: str) -> None:
        """Remove both generations of a channel's history, on the lane.

        Queued behind every append already submitted for the channel, so an
        earlier write can never recreate a file this removed. Not admission
        bounded: it is one job per observe-off. The lane-owned rotation state
        is reset with it. Each generation is removed independently: a failure
        to unlink one (a Windows sharing violation, a directory at the name)
        is logged for that leaf and never stops the attempt on the other.
        """
        if self._history_dir is None:
            return

        def _remove() -> None:
            self._live_records.pop(channel_id, None)
            self._live_torn.discard(channel_id)
            self._live_unknown.discard(channel_id)
            self._rotate_failed.discard(channel_id)
            self._clear_disk_pause(channel_id)
            try:
                history_root, root_identity = self._prepare_history_root()
            except OSError:
                logger.warning(
                    "Failed to prepare history root for channel %s", channel_id, exc_info=True
                )
                return
            path = self._observe_path(channel_id, history_root)
            if path is None:
                return
            dfd = -1
            try:
                try:
                    dfd = self._pin_history_parent(path, root_identity)
                except FileNotFoundError:
                    # Parent already gone: nothing to remove.
                    return
                for leaf in (path, self._older_generation(path)):
                    try:
                        if _SUPPORTS_DIR_FD:
                            os.unlink(leaf.name, dir_fd=dfd)
                        else:
                            leaf.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.warning("Failed to remove history file %s", leaf, exc_info=True)
            except OSError:
                logger.warning("Failed to remove history file %s", path, exc_info=True)
            finally:
                if dfd != -1:
                    os.close(dfd)

        channel_history_executor().submit(_remove)

    def _open_observe_file(self, path: Path, root_identity: tuple[int, int]) -> BinaryIO:
        """Open one history generation for READING without following a link.

        The read gets the same guard as the mutation paths: the parent is
        pinned and identity-checked (:meth:`_pin_history_parent`), then the
        leaf is opened RELATIVE to that descriptor with ``O_NOFOLLOW``
        (openat) on POSIX, or through
        :func:`platform_compat.open_file_no_reparse` on Windows, so a symlink
        planted at either generation's name cannot redirect the load outside
        the history root. ``O_NONBLOCK`` makes the open of a FIFO at the name
        return at once instead of waiting for a writer, and the ``fstat`` on
        the descriptor then refuses anything that is not a regular file with
        exactly one link — the only shape this cache ever writes. The link
        count is what catches a hard link: it shares an outside file's inode
        under the generation's own name, so neither no-follow open sees it.
        There is no ``exists()`` probe: an absent file raises
        ``FileNotFoundError`` from the open itself, so there is no
        check-to-open window to plant into.
        """
        dfd = -1
        fd = -1
        try:
            dfd = self._pin_history_parent(path, root_identity)
            if _SUPPORTS_DIR_FD:
                flags = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                fd = os.open(path.name, flags, dir_fd=dfd)
            else:
                fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
            # Judged on the DESCRIPTOR: a hard link at the name is another
            # name for an outside inode that no path guard can see, so it is
            # refused like a FIFO, device or directory and the generation is
            # treated as unreadable (``platform_log_append._regular_single_link``
            # shape).
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(
                    errno.EINVAL,
                    "history file is not a regular file with one link",
                    str(path),
                )
            f = os.fdopen(fd, "rb")
        except BaseException:
            if fd != -1:
                os.close(fd)
            raise
        finally:
            if dfd != -1:
                os.close(dfd)
        return f

    @staticmethod
    def _count_generation_lines(handle: BinaryIO) -> tuple[int, bool]:
        """Count LF-terminated lines and a trailing partial line in fixed chunks."""
        newlines = 0
        last_byte = b""
        while chunk := handle.read(_MEASURE_CHUNK_BYTES):
            newlines += chunk.count(b"\n")
            last_byte = chunk[-1:]
        torn = last_byte not in (b"", b"\n")
        return newlines + (1 if torn else 0), torn

    def _measure_generation(
        self, path: Path, root_identity: tuple[int, int]
    ) -> _GenerationScan | None:
        """Measure a generation's line count and torn end through the guarded read.

        The count matches :meth:`_read_generation`: every newline-terminated
        line plus a trailing partial line. The file is read in fixed-size
        chunks (``_MEASURE_CHUNK_BYTES``) that only count newlines and track
        the last byte, so memory stays bounded however long a line is.
        """
        try:
            f = self._open_observe_file(path, root_identity)
        except FileNotFoundError:
            return None
        except OSError:
            return _GenerationScan(readable=False)

        try:
            with f:
                lines, torn = self._count_generation_lines(f)
        except OSError:
            return _GenerationScan(readable=False)
        return _GenerationScan(readable=True, lines=lines, torn=torn)

    def _accept_live_measurement(self, channel_id: str, scan: _GenerationScan | None) -> bool:
        """Apply a measured live-file end; return false when it is unreadable."""
        if scan is not None and not scan.readable:
            self._live_records.pop(channel_id, None)
            self._live_torn.discard(channel_id)
            self._live_unknown.add(channel_id)
            return False

        self._live_records[channel_id] = 0 if scan is None else scan.lines
        if scan is not None and scan.torn:
            self._live_torn.add(channel_id)
        else:
            self._live_torn.discard(channel_id)
        self._live_unknown.discard(channel_id)
        return True

    def _pause_disk_appends(self, channel_id: str) -> None:
        """Pause a channel's disk appends, on the lane, with its first re-read scheduled."""
        self._disk_paused.add(channel_id)
        self._pause_retry[channel_id] = _PauseRetry(
            next_at=_pause_clock() + _PAUSE_RETRY_INITIAL_SECS,
            interval=_PAUSE_RETRY_INITIAL_SECS,
        )

    def _clear_disk_pause(self, channel_id: str) -> None:
        """Lift a channel's disk-append pause and drop its re-read schedule, on the lane."""
        self._disk_paused.discard(channel_id)
        self._pause_retry.pop(channel_id, None)

    def _pause_retry_due(self, channel_id: str) -> bool:
        """Whether a paused channel's next scheduled re-read may run now."""
        return _pause_clock() >= self._pause_retry[channel_id].next_at

    def _resume_paused_appends(
        self, channel_id: str, path: Path, root_identity: tuple[int, int]
    ) -> bool:
        """Re-read a paused channel's generations; lift the pause once every one reads.

        Runs on the lane when an append arrives after the scheduled interval.
        Both generations are measured through the guarded read a load uses
        (:meth:`_measure_generation`) — a read only, never a rename or an
        unlink, so the files stay exactly as the load found them however the
        measurement goes. When every generation that exists is readable the
        pause lifts: the live measurement seeds the live count and torn
        state the way a load does, one INFO line records the resume, and the
        caller's append proceeds (rotating first when the live file is full).
        While any existing generation is still unreadable the channel stays
        paused, the next re-read is scheduled at double the wait (capped at
        ``_PAUSE_RETRY_MAX_SECS``), and nothing is logged above DEBUG, so a
        file that stays unreadable for weeks costs one line at the pause and
        one at the resume.
        """
        older = self._measure_generation(self._older_generation(path), root_identity)
        live = self._measure_generation(path, root_identity)
        if any(scan is not None and not scan.readable for scan in (older, live)):
            interval = min(self._pause_retry[channel_id].interval * 2, _PAUSE_RETRY_MAX_SECS)
            self._pause_retry[channel_id] = _PauseRetry(
                next_at=_pause_clock() + interval, interval=interval
            )
            logger.debug(
                "History files for channel %s still cannot all be read; disk appends remain "
                "paused, next re-read in %.0fs",
                channel_id,
                interval,
            )
            return False
        self._clear_disk_pause(channel_id)
        self._accept_live_measurement(channel_id, live)
        logger.info(
            "History files for channel %s can be read again — disk appends resumed",
            channel_id,
        )
        return True

    def _load_observe(self, channel_id: str) -> None:
        """Load on the history lane, then merge on the caller thread.

        Gateway-loop callers return immediately and receive the parsed result
        through ``call_soon_threadsafe``. Synchronous callers wait for the same
        lane operation so tests and non-async integrations keep their existing
        by-return visibility. In both shapes every filesystem operation,
        including first-use resolution and identity capture, stays on the lane.
        A load never writes a record. Its only disk changes are whole-file.
        Loads are the only source of TTL-driven removals: the unlink of a
        generation whose every record is past the TTL (or that holds no
        parsable record), and one rotation of the live file aside when no
        older generation exists and the live file's oldest record is past
        the TTL or the file is an inherited one still over the cap.
        """
        if self._history_dir is None:
            return
        with self._observe_lock:
            generation = self._observe_generation.get(channel_id, 0)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            entries = channel_history_executor().submit(self._read_observe, channel_id).result()
            self._apply_observe_load(channel_id, generation, entries)
            return

        def _read_and_return() -> None:
            entries = self._read_observe(channel_id)
            try:
                loop.call_soon_threadsafe(self._apply_observe_load, channel_id, generation, entries)
            except RuntimeError:
                logger.debug("Observe-load loop closed before history could be applied")

        channel_history_executor().submit(_read_and_return)

    def _read_observe(self, channel_id: str) -> deque[HistoryEntry] | None:
        """Worker-side resolve, trust, and parse of both history generations.

        Reads the older generation, then the live one, into a deque bounded
        to the cap in force when the read begins, so an oversized inherited
        file costs O(cap) memory and only its oldest records fall off the
        window — they stay on disk, untouched. Also seeds the lane-owned
        rotation state from the live file: its record count, and whether its
        last line is torn.

        Expiry runs only here, at a load (a gateway start or an observe
        re-enable), never continuously. Disk changes are whole-file, never a
        rewrite, and judged against the TTL in force once both generations
        are read (so a TTL raised during the read is honoured). A readable
        generation holding no record inside the TTL is unlinked. Then, when
        no older generation exists — it was absent, or was just unlinked —
        the live file is rotated aside once if either its oldest valid record
        is past the TTL or it is an inherited single file still over the cap,
        so the next append starts a fresh live file. A live file is never
        rotated over an existing ``.1`` for TTL reasons: a ``.1`` that still
        holds a fresh record is replaced only by the append-path rotation,
        once the live file is full. A generation that failed to read is left
        exactly where it is: no unlink, no rotation. An unreadable live file
        beside an absent ``.1`` seeds the lane's count at the cap so the next
        append rotates it aside before writing; beside a readable ``.1`` it
        pauses the channel's disk appends instead, since that rotation would
        rename it over the only generation a load can still read. An
        unreadable older generation pauses the channel's disk appends whatever
        the live file holds, so no rotation can replace it. A pause holds
        until the channel's next load or until a scheduled re-read on the
        append path finds every generation readable
        (:meth:`_resume_paused_appends`).

        Together these bound retention: an expired record leaves the disk when
        a load finds its generation's newest record past the TTL or a rotation
        replaces that generation. Once any inherited oversized generation has
        been replaced, rotation keeps at most two cap-sized generations between
        loads.
        """
        try:
            history_root, root_identity = self._prepare_history_root()
        except OSError:
            logger.warning(
                "Failed to prepare history root for channel %s", channel_id, exc_info=True
            )
            return None
        path = self._observe_path(channel_id, history_root)
        if path is None:
            return None

        cap = max(1, self._observe_max_entries)
        cutoff = time.time() - self._observe_ttl_secs
        entries: deque[HistoryEntry] = deque(maxlen=cap)
        older_path = self._older_generation(path)
        older = self._read_generation(older_path, root_identity, channel_id, entries, cutoff)
        live = self._read_generation(path, root_identity, channel_id, entries, cutoff)
        # A readable generation with nothing the window could still use is
        # removed whole, for the reason actually measured: every record past
        # the TTL, or no parsable record at all (a file holding only a torn
        # partial line, say). The cutoff is taken again here, after both
        # reads, so the TTL in force at the decision is the one applied.
        cutoff = time.time() - self._observe_ttl_secs
        if older is not None and older.readable:
            if older.newest is None:
                if self._unlink_generation(older_path, root_identity, _NO_PARSABLE_RECORD):
                    older = None
            elif older.newest < cutoff:
                if self._unlink_generation(older_path, root_identity, _EVERY_RECORD_EXPIRED):
                    older = None
        if live is not None and live.readable:
            if live.newest is None:
                if self._unlink_generation(path, root_identity, _NO_PARSABLE_RECORD):
                    live = None
            elif live.newest < cutoff:
                if self._unlink_generation(path, root_identity, _EVERY_RECORD_EXPIRED):
                    live = None
        live_records = 0
        live_torn = False
        live_aged = False
        # The load seeds all lane-owned live-file state afresh from disk.
        self._live_unknown.discard(channel_id)
        self._clear_disk_pause(channel_id)
        if live is not None and live.readable:
            live_records = live.lines
            live_torn = live.torn
            live_aged = live.oldest is not None and live.oldest < cutoff
        elif live is not None and older is not None and older.readable:
            # The live file exists but could not be read, and the older
            # generation is the only history a load can read. A rotation
            # would rename the unreadable file over it, so the channel's disk
            # appends pause until a load or a scheduled re-read finds both
            # files readable; neither file is touched, and the window keeps
            # every pushed message.
            self._pause_disk_appends(channel_id)
            logger.warning(
                "History file %s could not be read and %s is the only readable "
                "generation — disk appends for channel %s are paused until a scheduled "
                "re-read finds every generation readable (first retry after %ds, then "
                "doubling to at most %ds) or the history is reloaded; the in-memory "
                "window is unaffected",
                path,
                older_path,
                channel_id,
                _PAUSE_RETRY_INITIAL_SECS,
                _PAUSE_RETRY_MAX_SECS,
            )
        elif live is not None:
            # The live file exists but could not be read, so its line count is
            # unknown, and no readable older generation stands to be replaced.
            # Seed the count at the cap and mark it torn: beside an absent
            # ``.1`` the next append rotates it aside before writing, so the
            # bound holds without a rewrite; beside an unreadable ``.1`` the
            # pause below holds every append instead.
            live_records = cap
            live_torn = True
        self._live_records[channel_id] = live_records
        if live_torn:
            self._live_torn.add(channel_id)
        else:
            self._live_torn.discard(channel_id)
        # One rotation at most, and only into an empty ``.1`` slot: a live
        # file whose oldest record is past the TTL is moved aside whole so a
        # later load can remove it once its newest record expires too. The
        # same rename moves aside an inherited single file over the bound. An
        # unreadable older generation pauses the channel's disk appends —
        # until a load or a scheduled re-read finds every generation readable
        # — whatever the live file holds, so no rotation, here or on the
        # append path, can replace the records whose read failed.
        older_absent = older is None
        if older is not None and not older.readable:
            self._pause_disk_appends(channel_id)
            logger.warning(
                "History file %s could not be read beside %s — disk appends for channel %s "
                "are paused until a scheduled re-read finds every generation readable (first "
                "retry after %ds, then doubling to at most %ds) or the history is reloaded; "
                "the in-memory window is unaffected",
                older_path,
                path,
                channel_id,
                _PAUSE_RETRY_INITIAL_SECS,
                _PAUSE_RETRY_MAX_SECS,
            )
        elif older_absent and (live_aged or live_records > cap):
            if self._rotate_generation(channel_id, path, root_identity):
                self._live_records[channel_id] = 0
                self._live_torn.discard(channel_id)
        # The log reports only what was measured: the line count of each
        # generation that was read. ``live_records`` is the lane's rotation
        # seed — the cap, for an unreadable live file — not a measurement, and
        # an unreadable scan carries no line count at all, so a generation
        # that exists but could not be read is named instead of being folded
        # into the total.
        counted = 0
        unread: list[Path] = []
        for leaf, scan in ((older_path, older), (path, live)):
            if scan is None:
                continue
            if scan.readable:
                counted += scan.lines
            else:
                unread.append(leaf)
        if unread:
            logger.info(
                "Loaded %d entries for channel %s from disk (%d records counted; %s could not "
                "be read, and the records of an unreadable generation are not counted)",
                len(entries),
                channel_id,
                counted,
                " and ".join(str(leaf) for leaf in unread),
            )
        else:
            logger.info(
                "Loaded %d entries for channel %s from disk (%d records in the two generations)",
                len(entries),
                channel_id,
                counted,
            )
        return entries

    def _read_generation(
        self,
        path: Path,
        root_identity: tuple[int, int],
        channel_id: str,
        entries: deque[HistoryEntry],
        cutoff: float,
    ) -> _GenerationScan | None:
        """Parse one generation into ``entries``; ``None`` when the file is absent.

        The returned scan carries how many lines the file holds (every line
        counts toward the disk bound, valid or not), whether the last one
        lacks a terminating newline, and the oldest and newest valid record
        timestamps (``None`` when the file holds no valid record), which are
        what decide whether the generation is past the TTL as a whole or only
        at its head. A refused leaf comes back as an unreadable scan so the
        caller leaves it in place. Each line is validated on its own —
        undecodable bytes, malformed JSON, a non-object, wrong field types, a
        missing or non-finite timestamp — and a bad line is skipped with a
        warning; oversized records are skipped by a bounded framer, and
        oversized fields are truncated to the same caps ``push`` applies.
        Nothing here writes: a skipped line simply costs one line of the bound
        until its generation is rotated out or removed.
        """
        try:
            measure_file = self._open_observe_file(path, root_identity)
        except FileNotFoundError:
            return None
        except OSError:
            # A symlink, hard link, FIFO, device or directory at the leaf — or
            # a link at the parent — never reaches the window; the file is
            # only a cache.
            logger.warning("Refusing to read history file %s", path, exc_info=True)
            return _GenerationScan(readable=False)

        try:
            with measure_file:
                lines, torn = self._count_generation_lines(measure_file)
        except OSError:
            logger.warning("Failed to read history file %s", path, exc_info=True)
            return _GenerationScan(readable=False)

        try:
            f = self._open_observe_file(path, root_identity)
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("Refusing to read history file %s", path, exc_info=True)
            return _GenerationScan(readable=False)

        accepted_bytes = 0
        oldest: float | None = None
        newest: float | None = None
        try:
            with f:
                records = jsonl_util.bounded_raw_records(
                    f,
                    path,
                    cap=_HISTORY_RECORD_BYTE_CAP,
                    label="channel history load",
                )
                for line_no, raw_bytes in enumerate(records, 1):
                    accepted_bytes += len(raw_bytes)
                    try:
                        raw = raw_bytes.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        logger.warning(
                            "Corrupt UTF-8 JSONL line %d in %s — skipping",
                            line_no,
                            path,
                        )
                        continue
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Corrupt JSONL line %d in %s — skipping", line_no, path)
                        continue
                    if not isinstance(data, dict):
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
                        or isinstance(wall_ts, bool)
                        or not isinstance(wall_ts, (int, float))
                    ):
                        # A ts-less record has no producer in this writer and
                        # is treated like any other malformed line.
                        logger.warning(
                            "Invalid history record for channel %s at line %d — skipping",
                            channel_id,
                            line_no,
                        )
                        continue
                    try:
                        wall_ts = float(wall_ts)
                    except (OverflowError, ValueError):
                        wall_ts = math.nan
                    if not math.isfinite(wall_ts):
                        logger.warning(
                            "Invalid history record for channel %s at line %d — skipping",
                            channel_id,
                            line_no,
                        )
                        continue
                    if oldest is None or wall_ts < oldest:
                        oldest = wall_ts
                    if newest is None or wall_ts > newest:
                        newest = wall_ts
                    if wall_ts < cutoff:
                        continue
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
                consumed_bytes = f.tell()
                if consumed_bytes > accepted_bytes:
                    logger.warning(
                        "History load skipped an oversized record over %d bytes in %r",
                        _HISTORY_RECORD_BYTE_CAP,
                        path,
                    )
        except OSError:
            logger.warning("Failed to read history file %s", path, exc_info=True)
            return _GenerationScan(readable=False)
        return _GenerationScan(
            readable=True,
            lines=lines,
            torn=torn,
            oldest=oldest,
            newest=newest,
        )

    def _unlink_generation(self, leaf: Path, root_identity: tuple[int, int], reason: str) -> bool:
        """Remove one generation the window can make no use of, on the lane.

        ``reason`` is the ground the caller measured — every record past the
        TTL, or no parsable record at all — and is what the log line states.
        The unlink runs inside the pinned parent (:meth:`_pin_history_parent`):
        relative to the pinned descriptor on POSIX, by name under the held pin
        on Windows. An unlink removes the directory entry itself, so a link
        planted at the name is removed, never followed. A file that is
        already gone counts as removed. Never raises: a failure is logged and
        reported as ``False``, and the generation stays until the next load
        or rotation.
        """
        dfd = -1
        try:
            dfd = self._pin_history_parent(leaf, root_identity)
            if _SUPPORTS_DIR_FD:
                os.unlink(leaf.name, dir_fd=dfd)
            else:
                leaf.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Failed to remove history file %s", leaf, exc_info=True)
            return False
        finally:
            if dfd != -1:
                os.close(dfd)
        logger.info("Removed history file %s: %s", leaf, reason)
        return True

    def _apply_observe_load(
        self,
        channel_id: str,
        generation: int,
        entries: deque[HistoryEntry] | None,
    ) -> None:
        """Merge one current lane result into the channel's deque.

        A result from an observe session that has since been toggled is
        dropped: the file it read may already be removed, and a later load
        of the new session brings back whatever is on disk.
        """
        with self._observe_lock:
            if (
                self._observe_generation.get(channel_id) != generation
                or channel_id not in self._observe_channels
            ):
                return
        if not entries:
            return

        buf = self._channels.get(channel_id)
        if buf is None:
            buf = deque(maxlen=self._observe_max_entries)
            self._channels[channel_id] = buf

        # Merge: disk entries first (older), then any already in-memory
        existing = list(buf)
        buf.clear()
        buf.extend(entries)
        buf.extend(existing)

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
