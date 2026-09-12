"""Channel History Buffer — rolling window of recent messages per channel.

Captures all messages in group channels (not just @mentions) so that
when the agent is invoked, it has conversational context about what
was being discussed.

Non-observe channels are ephemeral / in-memory only.  Observe-mode
channels are persisted to disk as JSONL so history survives restarts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, pinned_parent_replace_supported
from kiro_crew.executors import submit_channel_history_job

logger = logging.getLogger(__name__)

# Defaults
_DEFAULT_MAX_ENTRIES = 50  # per channel
_DEFAULT_TTL_SECS = 300  # 5 minutes

# Observe-mode channels get a deeper buffer
OBSERVE_MAX_ENTRIES = 200
OBSERVE_TTL_SECS = 604800  # 1 week

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
        self._history_dir = history_dir
        self._channels: dict[str, deque[HistoryEntry]] = {}
        self._observe_channels: set[str] = set()  # channels with deeper buffer
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
        # channel_id -> (generation, serialized file content or None=unlink).
        # The generation is the _flush_epoch value at schedule time.
        self._pending_terminal: dict[str, tuple[int, str | None, Path]] = {}
        # Schedule counter: bumped when a terminal op is SCHEDULED.
        self._flush_epoch: dict[str, int] = {}
        # Publish counter: advanced to a terminal op's generation only after
        # its disk IO SUCCEEDS. An append queued before a terminal op is
        # superseded (its entry is in that op's snapshot) only once the op
        # has actually published — a failed write supersedes nothing, so the
        # queued appends still land their entries (see _submit_append).
        self._published_epoch: dict[str, int] = {}

    def set_user_name(self, user_id: str, name: str) -> None:
        """Cache a display name for a user ID."""
        if user_id and name:
            self._user_names[user_id] = name

    def set_observe(self, channel_id: str) -> None:
        """Enable observe mode for a channel (deeper history buffer).

        If a history file exists on disk, loads it into memory.
        """
        self._observe_channels.add(channel_id)
        # Cancel any queued UNLINK from a prior unset_observe BEFORE the
        # load: observe is on again, so the file must survive. The cancel
        # is unconditional — it must not depend on the load succeeding,
        # or a transient read failure (fd exhaustion, mount blip) would
        # leave the stale unlink armed to delete a still-valid file. A
        # snapshot the worker has already popped is beyond reach; the
        # superseding REWRITE below covers that in-flight case.
        with self._terminal_lock:
            pending = self._pending_terminal.get(channel_id)
            if pending is not None and pending[1] is None:
                self._pending_terminal.pop(channel_id, None)

        # Upgrade existing buffer to larger capacity
        buf = self._channels.get(channel_id)
        if buf is not None and buf.maxlen != self._observe_max_entries:
            new_buf: deque[HistoryEntry] = deque(buf, maxlen=self._observe_max_entries)
            self._channels[channel_id] = new_buf

        # Load persisted history from disk
        self._load_observe(channel_id)

        # Supersede an UNLINK the worker already popped and is executing:
        # terminal ops coalesce last-writer-wins per channel and run in
        # schedule order on the single worker, so publishing the window here
        # lands after the in-flight unlink and restores the file. Only when
        # the window has entries — a REWRITE serialized from an empty buffer
        # is content=None, which _write_terminal executes as an unlink; the
        # empty-window case is already safe because the pending unlink was
        # cancelled above.
        buf = self._channels.get(channel_id)
        if buf:
            self._schedule_terminal(channel_id, _OP_REWRITE)

    def unset_observe(self, channel_id: str) -> None:
        """Disable observe mode for a channel (revert to default buffer).

        Keeps the newest ``max_entries`` window in memory — an @mention right
        after observe-off still gets its ordinary channel context. Re-enable
        cannot duplicate what is retained: ``_load_observe`` dedupes the
        disk/memory merge by message identity.
        """
        self._observe_channels.discard(channel_id)
        # The file is removed below, so the count against it is meaningless; drop
        # it rather than let one int per channel ever observed outlive the file.
        self._appends_since_compact.pop(channel_id, None)
        buf = self._channels.get(channel_id)
        if buf is not None and buf.maxlen != self._max_entries:
            new_buf: deque[HistoryEntry] = deque(buf, maxlen=self._max_entries)
            self._channels[channel_id] = new_buf

        # Remove the persisted history file as a terminal op: it is coalesced
        # per channel and never droppable, so disabling observe always wins
        # against any queued or dropped writes for this channel.
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
            user=user, text=text, thread_ts=thread_ts, msg_ts=msg_ts, wall_ts=wall_ts
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

    def _observe_path(self, channel_id: str) -> Path | None:
        """Return the JSONL file path for an observe channel, or None."""
        if self._history_dir is None:
            return None
        from kiro_crew.hooks import is_sensitive_path

        history_root = self._history_dir.resolve()
        path = (self._history_dir / f"{channel_id}.jsonl").resolve()
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
          waiting for the count-triggered compaction.
        - Once a process-exit drain has begun, admission is refused outright
          (:func:`~kiro_crew.executors.submit_channel_history_job` returns
          ``False``): a job queued behind the drain's sentinel would only be
          discarded by the exec that follows it.
        - Each append captures the channel's flush epoch at submit; it is
          superseded only when a terminal op scheduled AFTER it has actually
          PUBLISHED (``_published_epoch``) — that op's snapshot already
          contains this entry, so skipping avoids duplicate lines. A
          scheduled-but-failed rewrite publishes nothing, so the queued
          appends still run and their entries reach disk.

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
            admitted = submit_channel_history_job(_job_with_release)
        except BaseException:
            # The submit itself failed (executor shut down mid-teardown): the
            # job will never run, so hand its slot back.
            self._disk_lane_slots.release()
            raise
        if not admitted:
            # A process-exit drain has begun: the gate-check and submit are
            # one atomic step, so nothing can queue behind the drain's
            # sentinel and be discarded by the exec. The entry is already in
            # the in-memory deque; the process is exiting.
            self._disk_lane_slots.release()
            logger.warning(
                "History lane is draining for process exit — refusing the append for channel %s",
                channel_id,
            )

    def _schedule_terminal(self, channel_id: str, op: str) -> None:
        """Record a terminal disk op for a channel and ensure it flushes.

        Terminal ops are idempotent and last-writer-wins: a REWRITE publishes
        the in-memory window as the whole file, an UNLINK removes the file.
        The window is serialized HERE, on the calling thread — the deque's
        only mutator — so the snapshot is race-free, and the worker only does
        IO on immutable bytes. Coalescing to at most one pending op per
        channel bounds the queue structurally, and because a rewrite replaces
        the whole file, its correctness does not depend on queue ordering.
        Terminal ops are never dropped while the lane admits work: disabling
        observe (UNLINK) and publishing the window (REWRITE) always win over
        queued appends. The one refusal is the process-exit drain — once
        admission closes, a terminal op scheduled after it is popped with a
        warning, the same contract appends get (see the drain branch below). The
        pending op coalesces last-writer-wins, so the flush job — queued when
        the FIRST op was scheduled — executes whatever snapshot is NEWEST at
        pop time; an append queued after that first schedule can therefore
        run before or after the flush, and correctness comes from the epoch
        gate instead of arrival order: once a terminal op PUBLISHES, the
        appends it superseded skip their writes (their entries are already
        in its snapshot; see _submit_append), and an append a snapshot does
        not contain is never superseded by it.
        Every terminal op executes on the single-worker lane regardless of
        the calling thread, so completion order equals schedule order.
        The target PATH is also captured here, at schedule time: the
        containment check inside ``_observe_path`` resolves symlinks, so
        running it in the deferred worker would itself follow a directory
        swapped in after scheduling — the exact steer the worker's parent pin
        exists to refuse.
        """
        path = self._observe_path(channel_id)
        if path is None:
            return
        buf = self._channels.get(channel_id)
        content: str | None = None
        if op == _OP_REWRITE and buf:
            lines = []
            # Serialize only the newest ``observe_max_entries`` entries: a
            # config reload can shrink the cap while an existing deque still
            # carries the old, larger ``maxlen``, and the published file must
            # honour the CURRENT cap, not the deque's construction-time one.
            cap = self._observe_max_entries
            snapshot = list(buf)[-cap:] if cap > 0 else list(buf)
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
        with self._terminal_lock:
            had_pending = channel_id in self._pending_terminal
            gen = self._flush_epoch.get(channel_id, 0) + 1
            self._flush_epoch[channel_id] = gen
            self._pending_terminal[channel_id] = (gen, content, path)
        if had_pending:
            return
        try:
            admitted = submit_channel_history_job(lambda: self._flush_terminal(channel_id))
        except BaseException:
            with self._terminal_lock:
                self._pending_terminal.pop(channel_id, None)
            raise
        if not admitted:
            # A process-exit drain has begun: the terminal op cannot queue
            # behind the sentinel either. Drop the pending snapshot — the
            # window lives in the deque of the exiting process.
            with self._terminal_lock:
                self._pending_terminal.pop(channel_id, None)
            logger.warning(
                "History lane is draining for process exit — dropping the terminal op for channel %s",
                channel_id,
            )

    def _flush_terminal(self, channel_id: str) -> None:
        """Worker-side flush: pop the latest pending snapshot and do the IO.

        The lock is held only for the O(1) pop — never across disk IO. The
        snapshot was serialized at schedule time, so this touches no shared
        mutable state.
        """
        with self._terminal_lock:
            if channel_id not in self._pending_terminal:
                return
            gen, content, path = self._pending_terminal.pop(channel_id)
        self._write_terminal(channel_id, content, gen, path)

    def _write_terminal(self, channel_id: str, content: str | None, gen: int, path: Path) -> None:
        """Perform one terminal op's disk IO. ``None`` removes the file.

        Publication is gated on SUCCESS: only when the unlink or the
        ``atomic_write`` actually completed does ``_published_epoch`` advance
        to this op's generation, releasing the appends this op superseded. A
        failed write publishes nothing — the queued appends run and land
        their entries, so a disk error costs the coalesced snapshot, never
        the messages behind it.

        ``path`` was containment-checked at SCHEDULE time (see
        ``_schedule_terminal``); re-deriving it here would resolve a
        directory swapped in after scheduling. The IO holds the same parent
        pin as the appends (:func:`platform_compat.pin_directory`): a
        swapped-in link fails the pin instead of steering the rewrite or
        unlink at an external file. The rewrite passes the pinned descriptor
        to ``atomic_write`` (``parent_dir_fd``); the unlink resolves relative
        to it where the platform supports ``dir_fd``, and by name under the
        held pin elsewhere (Windows, where the pin itself blocks a parent
        swap for as long as it lives).
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
                    atomic_write(
                        path,
                        content,
                        parent_dir_fd=dfd if pinned_parent_replace_supported() else None,
                    )
        except OSError:
            verb = "remove" if content is None else "compact"
            logger.warning("Failed to %s history file %s", verb, path, exc_info=True)
            return
        finally:
            if dfd != -1:
                os.close(dfd)
        with self._terminal_lock:
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
        path = self._observe_path(channel_id)
        if path is None:
            return
        # Create the parent NOW, at schedule time, not in the deferred worker:
        # a worker-side mkdir can resurrect a directory removed after
        # scheduling (a torn-down test tmp_path, an unset_observe'd channel).
        # If the directory disappears before the worker's open, the open
        # fails ENOENT and is logged like any other append error — the entry
        # stays in the deque for the next compaction.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Failed to create history dir for %s", path, exc_info=True)
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
            dfd = -1
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
                if _SUPPORTS_DIR_FD:
                    # POSIX: resolve the leaf RELATIVE to the pinned
                    # descriptor (openat) — nothing re-walks the full path —
                    # with O_NOFOLLOW refusing a linked leaf.
                    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_NOFOLLOW
                    fd = os.open(path.name, flags, 0o600, dir_fd=dfd)
                else:
                    # Windows: open under the held pin WITHOUT following a
                    # reparse point (platform_compat.open_append_no_reparse) —
                    # a plain open would resolve a UNC-target link and fire
                    # outbound SMB/NTLM authentication before any post-open
                    # check could refuse it.
                    fd = platform_compat.open_append_no_reparse(path)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                logger.warning("Failed to append to history file %s", path, exc_info=True)
            finally:
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
        # and counts submissions: a failed append still advances it, which at
        # worst compacts one message early.
        count = self._appends_since_compact.get(channel_id, 0) + 1
        if count >= self._observe_max_entries:
            self._compact(channel_id)
            self._appends_since_compact[channel_id] = 0
        else:
            self._appends_since_compact[channel_id] = count

    def _load_observe(self, channel_id: str) -> None:
        """Load persisted observe history from disk into the in-memory deque.

        Filters out entries older than TTL.  If any expired entries were
        found, rewrites the file without them (lazy compaction).
        """
        path = self._observe_path(channel_id)
        if path is None or not path.exists():
            return

        cutoff = time.time() - self._observe_ttl_secs
        entries: list[HistoryEntry] = []
        had_expired = False

        try:
            with path.open("r", encoding="utf-8") as f:
                for line_no, raw in enumerate(f, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Corrupt JSONL line %d in %s — skipping", line_no, path)
                        continue
                    wall_ts = data.get("ts")
                    if wall_ts is None:
                        continue
                    if wall_ts < cutoff:
                        had_expired = True
                        continue
                    # Convert wall clock to a monotonic offset so existing code works
                    age_secs = time.time() - wall_ts
                    mono_ts = time.monotonic() - age_secs
                    entries.append(
                        HistoryEntry(
                            user=data.get("user", ""),
                            text=data.get("text", ""),
                            thread_ts=data.get("thread_ts"),
                            msg_ts=data.get("msg_ts"),
                            timestamp=mono_ts,
                            wall_ts=wall_ts,
                        )
                    )
        except OSError:
            logger.warning("Failed to read history file %s", path, exc_info=True)
            return

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
        # back to (user, text, thread_ts).
        existing = list(buf)
        buf.clear()
        seen: set[object] = set()

        def _identity(e: HistoryEntry) -> object:
            return e.msg_ts if e.msg_ts else (e.user, e.text, e.thread_ts)

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

        logger.info("Loaded %d entries for channel %s from disk", len(entries), channel_id)

        # Lazy compaction: drop expired entries, and fold a file that already
        # holds more than the window back onto it. The count arm matters on its
        # own — a file written before the append path bounded it, or one filled
        # faster than the TTL retires anything, has nothing expired to trigger
        # the first arm while still carrying records the deque above just threw
        # away.
        if had_expired or len(entries) > self._observe_max_entries:
            self._compact(channel_id)
        self._appends_since_compact[channel_id] = 0

    def _compact(self, channel_id: str) -> None:
        """Publish the in-memory window as the whole file (or remove it).

        Every trigger (TTL on load, size on load, count on append) reaches
        this through ``push``/``set_observe`` calls on the gateway's
        event-loop thread, so the actual ``atomic_write`` runs as a coalesced
        terminal op off the loop (:meth:`_schedule_terminal`), which
        snapshots the window at schedule time — an empty window becomes an
        unlink.
        """
        self._schedule_terminal(channel_id, _OP_REWRITE)

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
