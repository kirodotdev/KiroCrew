"""Notification gate for Mochi — leading-edge merge window, quiet mode, priority bypass.

Sits in front of ``MochiRuntime.notify_user`` (the direct-connect sink fan-out) and
solves a real loss the direct path has: the bubble surface REPLACES its text on
every notify (``useBubble.setBubble``), so a burst — one poll cycle hitting three
watch items — leaves the user seeing only the last bubble. The activity log and
chat push keep a record, but the ambient surface (the point of a desktop pet)
drops all but one.

An earlier port of the upstream ``NotificationBuffer`` class was removed from this
tree twice over: upstream itself never consumed it (its live path was the same
direct fan-out this module wraps), and its trailing-edge design held even a lone
notification for the full window — a delay upstream never shipped. This gate is a
redesign, not a resurrection:

**Leading edge.** The first notification in a quiet stretch delivers IMMEDIATELY
and opens the merge window. Only notifications arriving while the window is open
are buffered; when it closes they flush as one merged summary per source. A lone
notification therefore has ZERO added latency — the common case is unchanged, and
only actual bursts coalesce.

**Quiet mode.** ``silence()`` holds every non-critical notification until the
quiet period ends (user resumes, or the deadline passes and ``tick`` observes it),
then flushes the backlog merged. Unlike the removed class, expiry is observed by
``tick`` — the owner loop guarantees a tick every cycle, so there is no need for
the mutating ``is_silent`` getter the old port carried (a read that flushed as a
side effect). ``is_silent`` here is a plain predicate.

**Critical bypass.** ``priority == "critical"`` skips both gates. A "meeting in
5 minutes" reminder must not sit in a merge window or a quiet period.

Delivery is one callable — the runtime's ``notify_user`` — so every path below
this gate (bubble, activity log, chat push with its re-notify guard, mood) stays
exactly the already-tested direct-connect fan-out. A merged flush synthesizes a
single action dict; the per-source summary wording is ported verbatim from
upstream's grouping table.
"""

from __future__ import annotations

from typing import Any, Callable

#: Merge window: notifications arriving within this period after a delivery are
#: coalesced into one summary. Matches upstream's constant.
MERGE_WINDOW_MS = 5_000

#: Default quiet-mode duration when none is given.
DEFAULT_SILENT_MINS = 60

#: Max buffered notifications before eviction (memory guard). Eviction prefers
#: the oldest ``low``-priority item over the oldest item outright.
MAX_BUFFER_SIZE = 100

Notification = dict[str, Any]
Deliver = Callable[[Notification], None]


class NotificationGate:
    """Leading-edge merge window + quiet mode in front of one deliver callable."""

    def __init__(self, deliver: Deliver) -> None:
        self._deliver_cb = deliver
        self._buffer: list[Notification] = []
        self._silent_until = 0
        # Merge-window close deadline, or None when no window is open. A
        # non-None value means "a delivery just happened; coalesce arrivals".
        self._window_deadline: int | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def push(self, action: Notification, now_ms: int) -> None:
        """Admit a notification.

        ``critical`` delivers immediately, bypassing both the merge window and
        quiet mode. During quiet mode everything else buffers. Otherwise:
        window open → buffer; window closed → deliver NOW and open the window
        (the leading edge — a lone notification is never delayed).
        """
        if action.get("priority") == "critical":
            self._deliver_cb(action)
            return

        if self.is_silent(now_ms):
            self._buffer.append(action)
            self._cap_buffer()
            return

        if self._window_deadline is not None:
            self._buffer.append(action)
            self._cap_buffer()
            return

        self._deliver_cb(action)
        self._window_deadline = now_ms + MERGE_WINDOW_MS

    def silence(self, now_ms: int, duration_mins: int = DEFAULT_SILENT_MINS) -> None:
        """Enter quiet mode for ``duration_mins`` from ``now_ms``."""
        self._silent_until = now_ms + duration_mins * 60_000

    def unsilence(self, now_ms: int) -> None:
        """Leave quiet mode and flush the backlog, merged."""
        self._silent_until = 0
        self._window_deadline = None
        self._flush_all()

    def is_silent(self, now_ms: int) -> bool:
        """Whether quiet mode is in effect. Plain predicate — never mutates."""
        return self._silent_until != 0 and now_ms < self._silent_until

    @property
    def silent_until(self) -> int:
        """Quiet-mode expiry timestamp (ms), or 0 when not quiet."""
        return self._silent_until

    @property
    def pending_count(self) -> int:
        return len(self._buffer)

    def tick(self, now_ms: int) -> None:
        """Observe deadlines. Called every owner-loop cycle.

        Quiet expiry: clear the deadline and flush the backlog merged. Window
        expiry: close the window and flush whatever coalesced (nothing buffered
        means the window simply closes — the leading delivery already went out).
        """
        if self._silent_until != 0 and now_ms >= self._silent_until:
            self._silent_until = 0
            self._window_deadline = None
            self._flush_all()
            return
        if self._window_deadline is not None and now_ms >= self._window_deadline:
            self._window_deadline = None
            self._flush_all()

    # ── Internal ───────────────────────────────────────────────────────────

    def _flush_all(self) -> None:
        """Deliver everything buffered, merged per source to reduce noise."""
        if not self._buffer:
            return
        to_flush = self._buffer
        self._buffer = []

        groups: dict[str, list[Notification]] = {}
        for notif in to_flush:
            groups.setdefault(str(notif.get("source")), []).append(notif)

        for source, notifs in groups.items():
            if len(notifs) == 1:
                self._deliver_cb(notifs[0])
            else:
                self._deliver_merged(source, notifs)

    def _deliver_merged(self, source: str, notifs: list[Notification]) -> None:
        # First notification carrying a mood wins (upstream's Array.find).
        first_with_mood = next((n for n in notifs if n.get("mood")), None)
        merged: Notification = {
            "action": "notify",
            "summary": self._build_merged_summary(source, notifs),
            "source": source,
        }
        if any(n.get("sticky") for n in notifs):
            merged["sticky"] = True
        if any(n.get("pushToChat") for n in notifs):
            merged["pushToChat"] = True
            # The chat push carries the detail the bubble summary compresses
            # away: every constituent's chat content, joined. Without this a
            # merged flush would push only "3 reminders" into the transcript.
            parts = [
                str(n.get("chatMessage") or n.get("summary"))
                for n in notifs
                if n.get("chatMessage") or n.get("summary")
            ]
            merged["chatMessage"] = "\n".join(parts)
        if first_with_mood is not None:
            merged["mood"] = str(first_with_mood["mood"])
        self._deliver_cb(merged)

    def _build_merged_summary(self, source: str, notifs: list[Notification]) -> str:
        # Wording ported verbatim from upstream's grouping table (emoji are
        # message CONTENT here, not UI chrome — same class as toast text).
        joined = "\n".join(str(n["summary"]) for n in notifs if n.get("summary"))
        count = len(notifs)

        if source == "approval":
            return f"🔧 {count} operations waiting for approval"
        if source == "watch":
            # All-low means every watch check came back unchanged — the common
            # case, worth one quiet line instead of N.
            if all(n.get("priority") == "low" for n in notifs):
                return f"👀 Checked {count} watch items — all unchanged"
            return joined
        if source == "reminder":
            return joined if count <= 3 else f"⏰ {count} reminders"
        return joined if count <= 2 else f"{count} notifications"

    def _cap_buffer(self) -> None:
        """Evict when over the cap, preferring the oldest ``low`` item."""
        if len(self._buffer) <= MAX_BUFFER_SIZE:
            return
        for i, notif in enumerate(self._buffer):
            if notif.get("priority") == "low":
                del self._buffer[i]
                return
        del self._buffer[0]
