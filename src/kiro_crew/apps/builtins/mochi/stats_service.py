"""Companion stats for Mochi — counters, streaks, milestones, memory snapshots.

Ported from ``src/main/statsService.ts`` (+ the stats helpers it pulled from
``shared/types.ts``). Spec: the characterization suite at
``statsService.characterization.test.ts`` (migration-only, not shipped)
(the module shipped with zero tests; ``statsFormatters.test.ts`` covers only
renderer formatting).

Quirks preserved (characterized, kept deliberately):

* Uptime accrual sets ``dirty`` but does NOT check milestones — a days-N
  milestone crossed by pure uptime is celebrated on the NEXT user action.
* Sleep heuristic: an uptime gap > 120s credits exactly 60s; a gap > 3h also
  resets ``earliestActiveTime`` and logs a wake-up, but only before local
  noon.
* ``busiestDay`` counts only SENT messages; today's running count resumes
  from the persisted record only when the dates match.
* Milestone baseline is seeded on load without firing (no stale-celebration
  burst for long-lived companions); seeding sets ``dirty`` but does not
  schedule a flush — the baseline persists on the next activity.

DELIBERATE DEVIATIONS from the TypeScript (authorized; each is a fix or a
KiroCrew-composition change, not a silent drift):

1. **No singleton, no hardcoded path.** The original baked
   ``~/Library/Application Support/DesktopBuddy/stats.json`` at module import
   and exported a shared instance. The port takes ``data_dir`` (KiroCrew data
   home) and keeps ``stats.json`` directly inside it.
2. **Deterministic number grouping.** Milestone texts used
   ``toLocaleString()``, whose output depends on the process locale. The port
   formats with explicit en-US comma grouping — what users actually saw.
3. **Atomic persistence.** The original ``writeFileSync`` could leave a
   truncated ``stats.json`` on crash; the port writes tmp+rename via
   ``atomic_write`` (0600). Read-side corruption recovery is kept regardless.

PORTING NOTES
-------------

Clock injected; both timers owner-driven. The 500ms flush debounce and the
60s uptime interval become deadlines fired by ``tick(now_ms)``, with the
original interleaving preserved: ``flush`` itself ticks uptime first, and the
uptime path can emit the memory-hour snapshot. Date/time strings are LOCAL
time on purpose — "busiest day" and "early bird" are user-facing calendar
concepts, so they follow the host clock, exactly like the original.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable

from kiro_crew.apps.builtins.mochi.queue_file import _iso
from kiro_crew.apps.builtins.mochi.watchlist_file import _js_round
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

STATS_FILE_NAME = "stats.json"

# Debounce for dirty-state flushes.
FLUSH_DELAY_MS = 500

# Owner cadence for uptime accrual (the original setInterval period).
UPTIME_TICK_MS = 60_000

# Sleep heuristic: a gap longer than this credits only one normal tick.
_SLEEP_GAP_SECS = 120
_CREDIT_ON_SLEEP_SECS = 60

# A gap this long (before local noon) is treated as overnight sleep.
_OVERNIGHT_GAP_SECS = 3 * 3600

# Memory snapshots switch from "early days" to "well-acquainted" guidance.
_ONE_WEEK_HOURS = 168

_EARLY_GUIDANCE = (
    "You are still in the early days with your owner. Focus on being helpful, "
    "learning their habits, and showing what you can do through actions rather "
    "than explanations."
)
_ACQUAINTED_GUIDANCE = (
    "You and your owner are well-acquainted now. Reflect on what you know about "
    "them — their work patterns, preferences, personality. Share personal "
    "observations naturally when the moment feels right."
)

_DAY_MILESTONES = (1, 7, 30, 100, 365)
_MSG_MILESTONES = (10, 50, 100, 500, 1000, 5000)
_STREAK_MILESTONES = (3, 7, 14, 30, 100)
_STEP_MILESTONES = (100, 1000, 10000)

CompanionStats = dict[str, Any]

MilestoneCb = Callable[[str, str], None]
LogActivityCb = Callable[[str, str], None]


def _grouped(n: int) -> str:
    """En-US comma grouping (deviation 2: was locale-dependent)."""
    return f"{n:,}"


def _local_date_str(now_ms: int) -> str:
    """YYYY-MM-DD in host-local time, like the original's getFullYear() trio."""
    d = datetime.fromtimestamp(now_ms / 1000)
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


def _local_time_str(now_ms: int) -> str:
    """HH:mm in host-local time."""
    d = datetime.fromtimestamp(now_ms / 1000)
    return f"{d.hour:02d}:{d.minute:02d}"


def _utc_iso(now_ms: int) -> str:
    return _iso(now_ms)


def create_default_stats(now_ms: int) -> CompanionStats:
    """Fresh default stats (never a shared reference)."""
    return {
        "firstLaunch": _utc_iso(now_ms),
        "streak": 1,
        "lastActiveDate": _local_date_str(now_ms),
        "companionSeconds": 0,
        "messages": {"sent": 0, "received": 0},
        "walkSteps": 0,
        "screenshots": 0,
        "peeks": 0,
        "drags": 0,
        "thinkingSeconds": 0,
        "latestActiveTime": "",
        "earliestActiveTime": "",
        "moods": {},
        "longestChat": 0,
        "busiestDay": {"date": "", "messages": 0},
        "lastMemoryHour": 0,
        "celebratedMilestones": [],
    }


def merge_stats(base: CompanionStats, overrides: dict[str, Any]) -> CompanionStats:
    """Fill missing fields from defaults. ``??`` semantics: an explicit null
    falls back like absence; ``messages``/``busiestDay`` are spread-merged (a
    null there also falls back, since spreading null adds nothing)."""

    def take(key: str) -> Any:
        value = overrides.get(key)
        return base[key] if value is None else value

    def take_mapping(key: str) -> dict[str, Any]:
        # Only spread a genuine mapping. A malformed persisted value (a list,
        # string, number, ... — anything truthy-but-not-a-dict) would otherwise
        # make ``**value`` raise TypeError and break parse_stats_json's "any
        # corruption yields defaults, never a throw" contract, leaving Mochi
        # enabled without its owner loop. Non-dicts fall back to nested defaults.
        value = overrides.get(key)
        return value if isinstance(value, dict) else {}

    merged = {key: take(key) for key in base}
    merged["messages"] = {
        **base["messages"],
        **take_mapping("messages"),
    }
    merged["busiestDay"] = {
        **base["busiestDay"],
        **take_mapping("busiestDay"),
    }
    return merged


def parse_stats_json(raw: str, now_ms: int) -> CompanionStats:
    """Parse persisted stats; any corruption yields defaults, never a throw."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return create_default_stats(now_ms)
    if not isinstance(parsed, dict):
        return create_default_stats(now_ms)
    return merge_stats(create_default_stats(now_ms), parsed)


def _synchronized(method):
    """Serialize a StatsService method under ``self._lock`` (a reentrant RLock).

    Recorders and get_stats() run on request threads while tick()/flush()/reset()
    run on the owner loop or an offloaded worker thread; without this a `/stat`
    write can interleave a threaded flush (torn json.dumps, or a lost count when
    flush saves the old snapshot then clears the dirty deadline). RLock so the
    already-locked tick() -> flush() nesting on one thread does not self-deadlock.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class StatsService:
    """Counters and celebrations for the companion relationship."""

    def __init__(
        self,
        data_dir: str,
        log_activity: LogActivityCb | None = None,
    ) -> None:
        # Deviation 1: injected data home instead of a module-level singleton
        # bound to ~/Library/Application Support.
        self._stats_path = os.path.join(data_dir, STATS_FILE_NAME)
        self._log_activity: LogActivityCb = log_activity or (lambda t, c: None)
        self._stats: CompanionStats = {}
        self._dirty = False
        self._session_messages = 0
        self._today_sent_count = 0
        self._today_sent_date = ""
        self._thinking_start: int | None = None
        self._uptime_start = 0
        self._milestone_cb: MilestoneCb | None = None
        # Owner-driven timers (see tick): flush debounce + uptime cadence.
        self._flush_deadline: int | None = None
        self._next_uptime_tick: int | None = None
        # Serializes tick() and reset(). Both run in a worker thread
        # (asyncio.to_thread) to keep disk I/O off the event loop; this lock
        # makes them mutually exclusive so a due flush in one thread can no
        # longer rewrite stale counters over a concurrent reset.
        self._lock = threading.RLock()

    def on_milestone(self, cb: MilestoneCb) -> None:
        """Register the celebration callback. Fires at most once per milestone
        across the app's lifetime (tracked in persisted stats)."""
        self._milestone_cb = cb

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def load(self, now_ms: int) -> None:
        """Load stats (defaults on missing/corrupt), resume today's counters,
        arm the uptime cadence, and seed the milestone baseline."""
        try:
            raw: str | None = None
            if os.path.exists(self._stats_path):
                with open(self._stats_path, encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            self._stats = (
                parse_stats_json(raw, now_ms) if raw is not None else create_default_stats(now_ms)
            )
        except OSError:
            self._stats = create_default_stats(now_ms)

        # Resume today's sent count from the persisted record on a date match.
        today = _local_date_str(now_ms)
        if self._stats["busiestDay"].get("date") == today:
            self._today_sent_count = self._stats["busiestDay"].get("messages", 0)
            self._today_sent_date = today

        self._uptime_start = now_ms
        self._next_uptime_tick = now_ms + UPTIME_TICK_MS

        self._seed_milestone_baseline()

    def reset(self, now_ms: int) -> bool:
        """Wipe stats to defaults, unlinking the file and reloading.

        Dispatched off the event loop (``asyncio.to_thread``) and held under
        ``self._lock`` so it is mutually exclusive with tick()/flush(): a due
        flush running in another worker thread can no longer rewrite a stale
        in-memory snapshot back over the wipe. The dirty flag, the pending flush
        deadline, and the session counter are cleared first so a tick right after
        the lock is released cannot resurrect pre-reset state. Returns True if a
        stats file was actually removed.
        """
        with self._lock:
            return self._reset_locked(now_ms)

    def _reset_locked(self, now_ms: int) -> bool:
        removed = False
        try:
            os.remove(self._stats_path)
            removed = True
        except FileNotFoundError:
            pass
        except OSError as err:  # pragma: no cover - defensive
            logger.warning("[StatsService] reset could not remove stats: %s", err)
        self._dirty = False
        self._flush_deadline = None
        self._session_messages = 0
        self._today_sent_count = 0
        self._today_sent_date = ""
        self.load(now_ms)
        return removed

    def _seed_milestone_baseline(self) -> None:
        """Mark every already-achieved milestone celebrated WITHOUT firing."""
        s = self._stats
        total_msgs = s["messages"]["sent"] + s["messages"]["received"]
        days = s["companionSeconds"] // 86_400
        achieved: list[str] = []
        achieved += [f"days-{n}" for n in _DAY_MILESTONES if days >= n]
        achieved += [f"msgs-{n}" for n in _MSG_MILESTONES if total_msgs >= n]
        achieved += [f"streak-{n}" for n in _STREAK_MILESTONES if s["streak"] >= n]
        achieved += [f"steps-{n}" for n in _STEP_MILESTONES if s["walkSteps"] >= n]
        for key in achieved:
            if key not in s["celebratedMilestones"]:
                s["celebratedMilestones"].append(key)
                self._dirty = True
        # NOTE: no flush is scheduled here — the baseline persists on the next
        # activity's flush, matching the original.

    # ── Timers (owner-driven) ──────────────────────────────────────────────

    def tick(self, now_ms: int) -> None:
        """Fire due timers in deadline order: the 500ms flush debounce and the
        60s uptime cadence, preserving the original interleaving (a flush
        itself ticks uptime first).

        Held under ``self._lock`` so it cannot interleave with reset()."""
        with self._lock:
            self._tick_locked(now_ms)

    def _tick_locked(self, now_ms: int) -> None:
        while True:
            due: list[tuple[int, str]] = []
            if self._flush_deadline is not None and self._flush_deadline <= now_ms:
                due.append((self._flush_deadline, "flush"))
            if self._next_uptime_tick is not None and self._next_uptime_tick <= now_ms:
                due.append((self._next_uptime_tick, "uptime"))
            if not due:
                return
            # Tie-break: the uptime interval was created at load, before any
            # flush timeout, so at an equal deadline it fires FIRST (timer-id
            # order in the original runtime).
            due.sort(key=lambda t: (t[0], 0 if t[1] == "uptime" else 1))
            deadline, kind = due[0]
            if kind == "flush":
                self._flush_deadline = None
                self.flush(now_ms)
            else:
                assert self._next_uptime_tick is not None
                self._next_uptime_tick += UPTIME_TICK_MS
                self._tick_uptime(now_ms)

    def mark_dirty(self, now_ms: int) -> None:
        """Record a change: check milestones now, flush after the debounce."""
        self._dirty = True
        self._check_milestones()
        self._flush_deadline = now_ms + FLUSH_DELAY_MS

    @_synchronized
    def flush(self, now_ms: int) -> None:
        """Write to disk if dirty. Also captures pending uptime first."""
        self._tick_uptime(now_ms)
        if self._dirty:
            # Only clear the dirty flag when the write actually landed. Clearing
            # it unconditionally dropped pending stats whenever `save()` swallowed
            # an OSError — the update then vanished on the next restart.
            if self.save():
                self._dirty = False
        self._flush_deadline = None

    def save(self) -> bool:
        """Persist immediately. Deviation 3: atomic (tmp+rename, 0600) — the
        original's plain write could leave a truncated file on crash.

        Returns True on a successful write, False if persistence failed (so the
        caller keeps the dirty state and retries on the next flush)."""
        try:
            atomic_write(self._stats_path, json.dumps(self._stats, indent=2), mode=0o600)
            return True
        except OSError as err:
            # Keep in-memory state; retried on the next flush.
            logger.warning("[StatsService] Persist failed: %s", err)
            return False

    # ── Uptime / memory snapshots ──────────────────────────────────────────

    def reset_uptime_origin(self, now_ms: int) -> None:
        """Restart uptime accrual from *now* (presence returned after a gap).

        Mirrors the original's app-relaunch behaviour: a closed pet app had no
        ticker, so time away was never credited — without this reset the
        return tick would credit the whole gap (or the 60s sleep heuristic).
        """
        self._uptime_start = now_ms
        self._next_uptime_tick = now_ms + UPTIME_TICK_MS

    def _tick_uptime(self, now_ms: int) -> None:
        if self._uptime_start <= 0:
            return
        elapsed = int(_js_round((now_ms - self._uptime_start) / 1000))
        # A big gap means the machine slept — credit one normal tick only.
        credited = _CREDIT_ON_SLEEP_SECS if elapsed > _SLEEP_GAP_SECS else elapsed
        if credited <= 0:
            return
        self._stats["companionSeconds"] += credited
        self._uptime_start = now_ms
        self._dirty = True
        if elapsed > _OVERNIGHT_GAP_SECS:
            hour = datetime.fromtimestamp(now_ms / 1000).hour
            if hour < 12:
                self._stats["earliestActiveTime"] = _local_time_str(now_ms)
                sleep_hours = int(_js_round(elapsed / 3600))
                self._log_activity(
                    "sleep", f"Owner woke up after ~{sleep_hours}h away. Good morning!"
                )
        self._check_memory_hour()

    def _check_memory_hour(self) -> None:
        """Write an hourly memory snapshot to the activity log on crossing."""
        current_hour = self._stats["companionSeconds"] // 3600
        if current_hour <= self._stats["lastMemoryHour"]:
            return
        self._stats["lastMemoryHour"] = current_hour

        s = self._stats
        total_msgs = s["messages"]["sent"] + s["messages"]["received"]
        parts = [
            f"Companion hour #{current_hour}",
            f"Messages: {total_msgs} total ({s['messages']['sent']} sent, "
            f"{s['messages']['received']} received)",
            f"Steps: {s['walkSteps']}",
        ]
        if s["screenshots"] > 0:
            parts.append(f"Screenshots: {s['screenshots']}")
        if s["peeks"] > 0:
            parts.append(f"Peeks: {s['peeks']}")
        if s["drags"] > 0:
            parts.append(f"Drags: {s['drags']}")
        if s["streak"] > 1:
            parts.append(f"Streak: {s['streak']} days")

        mood_entries = [(m, c) for m, c in s["moods"].items() if c > 0]
        mood_entries.sort(key=lambda kv: -kv[1])  # stable: ties keep insertion
        if mood_entries:
            top3 = ", ".join(f"{m}({c})" for m, c in mood_entries[:3])
            parts.append(f"Top moods: {top3}")

        if s["longestChat"] > 0:
            parts.append(f"Longest chat: {s['longestChat']} messages")
        if s["busiestDay"]["messages"] > 0:
            parts.append(
                f"Busiest day: {s['busiestDay']['date']} " f"({s['busiestDay']['messages']} msgs)"
            )

        guidance = _EARLY_GUIDANCE if current_hour < _ONE_WEEK_HOURS else _ACQUAINTED_GUIDANCE
        self._log_activity("memory", f"[memory] {'. '.join(parts)}. | {guidance}")

    # ── Milestones ─────────────────────────────────────────────────────────

    def _check_milestones(self) -> None:
        if self._milestone_cb is None:
            return
        s = self._stats
        total_msgs = s["messages"]["sent"] + s["messages"]["received"]
        days = s["companionSeconds"] // 86_400

        candidates: list[tuple[str, bool, str, str]] = []
        for n in _DAY_MILESTONES:
            msg = "Our first day together! 🎉" if n == 1 else f"{n} days together! 🎉"
            candidates.append((f"days-{n}", days >= n, msg, "🕐"))
        for n in _MSG_MILESTONES:
            candidates.append(
                (f"msgs-{n}", total_msgs >= n, f"{_grouped(n)} messages exchanged! 💬", "💬")
            )
        for n in _STREAK_MILESTONES:
            candidates.append(
                (f"streak-{n}", s["streak"] >= n, f"{n}-day streak — you keep showing up! 🔥", "🔥")
            )
        for n in _STEP_MILESTONES:
            candidates.append(
                (
                    f"steps-{n}",
                    s["walkSteps"] >= n,
                    f"{_grouped(n)} steps wandered across your screen! 🐾",
                    "🐾",
                )
            )

        for key, hit, msg, emoji in candidates:
            if not hit or key in s["celebratedMilestones"]:
                continue
            s["celebratedMilestones"].append(key)
            self._dirty = True
            try:
                self._milestone_cb(msg, emoji)
            except Exception:  # noqa: BLE001 — celebrations are never fatal
                logger.debug("[StatsService] milestone callback failed", exc_info=True)

    # ── Introspection ──────────────────────────────────────────────────────

    @_synchronized
    def get_stats(self) -> CompanionStats:
        """Deep copy, safe to hand across boundaries."""
        return copy.deepcopy(self._stats)

    # ── Recorders ──────────────────────────────────────────────────────────

    def _update_active_time(self, now_ms: int) -> None:
        now = _local_time_str(now_ms)
        if not self._stats["earliestActiveTime"] or now < self._stats["earliestActiveTime"]:
            self._stats["earliestActiveTime"] = now
        if not self._stats["latestActiveTime"] or now > self._stats["latestActiveTime"]:
            self._stats["latestActiveTime"] = now

    @_synchronized
    def record_message_sent(self, now_ms: int) -> None:
        """Sent count, session/longest chat, busiest-day record, active times."""
        self._stats["messages"]["sent"] += 1
        self._session_messages += 1
        self._stats["longestChat"] = max(self._stats["longestChat"], self._session_messages)

        today = _local_date_str(now_ms)
        if self._today_sent_date != today:
            self._today_sent_count = 0
            self._today_sent_date = today
        self._today_sent_count += 1

        if self._today_sent_count > self._stats["busiestDay"]["messages"]:
            self._stats["busiestDay"] = {"date": today, "messages": self._today_sent_count}

        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)

    @_synchronized
    def record_message_received(self, now_ms: int) -> None:
        self._stats["messages"]["received"] += 1
        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)

    @_synchronized
    def record_app_launch(self, now_ms: int) -> None:
        """Streak bookkeeping: same day keeps, yesterday extends, older resets.

        "Yesterday" is CALENDAR arithmetic (the original's
        ``new Date(y, m, d - 1)``), not now-minus-24h — the two disagree on
        DST-transition days.
        """
        today = _local_date_str(now_ms)
        if self._stats["lastActiveDate"] != today:
            local_now = datetime.fromtimestamp(now_ms / 1000)
            yesterday_date = date(local_now.year, local_now.month, local_now.day) - timedelta(
                days=1
            )
            yesterday = (
                f"{yesterday_date.year}-{yesterday_date.month:02d}-" f"{yesterday_date.day:02d}"
            )
            if self._stats["lastActiveDate"] == yesterday:
                self._stats["streak"] += 1
            else:
                self._stats["streak"] = 1
            self._stats["lastActiveDate"] = today
        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)

    @_synchronized
    def record_walk(self, now_ms: int, steps: int = 1) -> None:
        self._stats["walkSteps"] += steps
        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)

    @_synchronized
    def record_screenshot(self, now_ms: int) -> None:
        self._stats["screenshots"] += 1
        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)

    @_synchronized
    def record_peek(self, now_ms: int) -> None:
        self._stats["peeks"] += 1
        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)

    @_synchronized
    def record_drag(self, now_ms: int) -> None:
        self._stats["drags"] += 1
        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)

    @_synchronized
    def record_thinking_start(self, now_ms: int) -> None:
        self._thinking_start = now_ms

    @_synchronized
    def record_thinking_end(self, now_ms: int) -> None:
        if self._thinking_start is None:
            return
        elapsed = int(_js_round((now_ms - self._thinking_start) / 1000))
        self._stats["thinkingSeconds"] += elapsed
        self._thinking_start = None
        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)

    @_synchronized
    def record_mood(self, mood: str, now_ms: int) -> None:
        self._stats["moods"][mood] = self._stats["moods"].get(mood, 0) + 1
        self._update_active_time(now_ms)
        self.mark_dirty(now_ms)
