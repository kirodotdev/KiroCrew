"""Persistence and the tick that fires reminders and break nudges.

All scheduling RULES live in :mod:`reminders` (pure, unit-tested). This module is
the side-effecting shell: read and write JSON, run a tick, queue what fired.
Same split the desktop app used, kept deliberately — it is why the rules could be
ported and pinned without a filesystem or a clock.

HOW A FIRED REMINDER REACHES THE USER
-------------------------------------
The companion draws its own bubbles, exactly as the desktop app did: a break
nudge auto-dismisses after 45s with a countdown, a reminder the user set never
auto-dismisses, and the breathing suggestion carries a call-to-action. Those
rules are presentation and stay in TypeScript (``notificationPolicy.ts``) next to
the renderer that obeys them. This side only decides WHAT fired and WHEN.

The gateway offers no server-push channel to an app's own windows — the one SSE
endpoint in ``apps/routes.py`` is for streaming registry installs — so delivery
is a **queue the overlay drains**, which is also how the reference builtin's
overlay works. Each fire gets a monotonic sequence number and the overlay asks
for everything after the last number it saw. That shape is chosen over "return
and clear" because a dropped HTTP response would otherwise lose a reminder
silently, and over a timestamp because two fires can land in the same second.

PRESENCE, AND WHY IT IS NOT AN IDLE CHECK
-----------------------------------------
The desktop app suppressed break nudges while the user was away (screen locked,
suspended, long idle) but still delivered time-anchored reminders late, on
return — nudging someone to stretch at a locked screen is pure noise, while a
reminder they set for 3pm must still arrive. A gateway process has no reliable
idle signal, so the overlay reports presence instead and silence is read as
away. That inverts the trust: the window that can actually see the user is the
one that says so.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kiro_crew.apps.builtins.crew_companion.reminders import (
    BREAK_MAX_MINS,
    BREAK_MIN_MINS,
    Recurrence,
    Reminder,
    ReminderFile,
    advance,
    can_prompt_breathe,
    clamp_break_mins,
    due_reminders,
    file_from_dict,
    file_to_dict,
    jittered_interval_seconds,
    note_breathe_prompt,
    parse_iso,
    pick_break_nudge,
    pick_variant,
    skip_once,
    to_iso,
)
from kiro_crew.platform_compat import chmod_safe

logger = logging.getLogger(__name__)

#: Refuse an absurdly large store rather than trying to parse it.
MAX_BYTES = 2_000_000

#: Tick cadence, seconds. Reminders are minute-grained, so a second is ample and
#: the work per tick is a dict comparison unless something is actually due.
TICK_SECONDS = 1.0
#: How often accumulated activity stats are flushed to disk. Stats mutate every
#: tick (1 Hz) but writing the store every second would hammer the disk for a
#: keepsake counter; a bounded flush means an ungraceful exit loses at most
#: this many seconds of "kept you company" time instead of everything since
#: the last unrelated save. stop() still flushes exactly.
STATS_FLUSH_SECONDS = 60.0

#: How long a presence ping stays valid. Longer than the overlay's own ping
#: interval so one dropped request does not read as the user walking away.
PRESENCE_TTL_SECONDS = 90.0

#: Cap on queued fires. A companion that was left running while the overlay was
#: closed should not accumulate an unbounded backlog to dump at once; the oldest
#: are dropped because the newest nudge is the only one still worth showing.
MAX_PENDING = 50

DEFAULT_BREAK_MINS = 45


@dataclass(frozen=True)
class Config:
    break_nudges_enabled: bool = True
    session_notifications_enabled: bool = True
    break_reminder_mins: int = DEFAULT_BREAK_MINS
    #: Kept for parity with the desktop app's payload. The dashboard page formats
    #: in the dashboard's own language, so this is not used for display.
    language: str = "English"
    #: Which appearance pack the companion is wearing. The built-in ghost's id when
    #: unset, so a deleted pack degrades to the default rather than to no art.
    active_appearance: str = "kiro-ghost"
    #: Where the user last left the companion on screen, in display coordinates.
    #:
    #: Persisted because a companion that returns to a default corner every restart
    #: is not where the user put it — and they moved it deliberately, usually to keep
    #: it out of the way of something. ``None`` means "never moved", which the
    #: renderer resolves to its own default placement rather than 0,0.
    pet_x: int | None = None
    pet_y: int | None = None
    #: The dress-up prop worn on the built-in ghost, by id.
    #:
    #: The gallery writes it (nested, as ``kiro.accessory``) and the overlay reads
    #: it back on every load. It was accepted and thrown away: picking a prop
    #: applied it to the live React state, so it looked saved until the next
    #: restart put the ghost back to bare. The desktop app kept it in the same
    #: place, `kiro.accessory`, defaulting to 'none'.
    accessory: str = "none"
    #: Colour presets the user saved themselves, in the renderer's own shape.
    #:
    #: Stored opaquely on purpose — the palette shape belongs to the gallery, and
    #: re-declaring it here would mean two definitions to keep in step. A tuple so
    #: the frozen dataclass cannot be mutated in place behind the lock.
    custom_presets: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Stats:
    """The companion's cumulative record, rendered read-only as "Memories".

    ``companion_seconds`` counts **time the companion was enabled**, which is the
    closest honest analogue of the desktop app's app-open time now that there is
    no separate app to open. It accumulates in the tick rather than from a start
    timestamp, so a crash loses at most one second instead of a whole session.
    """

    first_launch: str = ""
    streak: int = 0
    companion_seconds: int = 0
    breathing_sessions: int = 0
    reminders_created: int = 0
    #: Local HH:MM of the earliest and latest activity ever seen.
    earliest_active_time: str = ""
    latest_active_time: str = ""
    #: Local day key of the most recent tick, for the streak calculation.
    last_active_day: str = ""


@dataclass
class Fire:
    """Something the companion should say, waiting for the overlay to collect it."""

    seq: int
    #: 'reminder' | 'break' | 'break-breathe' | 'command' — the renderer's key.
    kind: str
    #: Already-resolved text for a reminder; a catalogue KEY for a break nudge.
    text: str = ""
    key: str = ""
    at: str = ""


@dataclass
class _State:
    reminders: ReminderFile = field(default_factory=lambda: ReminderFile())
    config: Config = field(default_factory=Config)
    stats: Stats = field(default_factory=Stats)


def _day_key_local(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"


class CompanionStore:
    """The companion's persisted state plus the timer that fires things.

    Deliberately synchronous and lock-guarded rather than async: every operation
    is a small JSON read/write, the tick runs in its own thread, and the gateway's
    request handlers reach it from the event loop. One lock around the whole state
    is simpler to reason about than partial async locking, and there is no
    contention to optimise away.
    """

    def __init__(
        self,
        data_dir: Path,
        rand: Callable[[], float] = random.random,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        on_fire: Callable[[], None] | None = None,
    ) -> None:
        self._path = data_dir / "crew-companion-reminders.json"
        self._rand = rand
        self._now = now
        # Called right after a fire is queued, so the overlay can be told instead of
        # waiting for its next poll. Optional: the store must work without it (tests
        # construct it bare, and the app may lack the events permission), and a
        # failing notifier must never lose the fire — see _queue_locked.
        self._on_fire = on_fire
        self._lock = threading.RLock()
        self._state = _State()
        #: The last state that actually reached disk, for rollback on a failed
        #: write. ``None`` until the first successful save, in which case a failure
        #: has nothing better to fall back to than what is already in memory.
        self._persisted: _State | None = None
        #: The queue/seq as of the last successful save — the rollback pair for
        #: _persisted (see _save_locked's failure path).
        self._persisted_pending: list[Fire] = []
        self._persisted_seq = 0
        self._pending: list[Fire] = []
        self._seq = 0
        self._next_break_at = 0.0
        self._last_stats_flush = 0.0
        self._last_nudge_key: str | None = None
        self._last_presence = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── persistence ─────────────────────────────────────────────────────────

    def load(self) -> None:
        """Read the store, tolerating every failure mode.

        Missing file (first run), bad JSON, wrong shape — none of them raise. A
        reminder app that throws on load is worse than one that starts empty.
        """
        raw: Any = None
        try:
            if self._path.is_file():
                if self._path.stat().st_size > MAX_BYTES:
                    logger.warning("crew-companion: store too large, ignoring")
                else:
                    raw = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("crew-companion: unreadable store (%s), starting empty", exc)

        with self._lock:
            self._state = _State(
                reminders=file_from_dict(raw.get("reminders_file") if isinstance(raw, dict) else None)
                if isinstance(raw, dict) and "reminders_file" in raw
                else file_from_dict(raw),
                config=_config_from_dict(raw),
                stats=_stats_from_dict(raw),
            )
            # What we just read IS what is on disk, so it is the rollback target
            # until a later save replaces it.
            self._persisted = self._state
            # Restore the pending queue and its sequence (see _save_locked for
            # why they persist). Tolerant like everything else in load():
            # malformed entries are skipped, a missing/absurd seq falls back to
            # the highest restored fire so numbering can only move forward.
            restored: list[Fire] = []
            if isinstance(raw, dict) and isinstance(raw.get("pending"), list):
                for item in raw["pending"]:
                    if not isinstance(item, dict):
                        continue
                    try:
                        restored.append(
                            Fire(
                                seq=int(item.get("seq", 0)),
                                kind=str(item.get("kind", "")),
                                text=str(item.get("text", "")),
                                key=str(item.get("key", "")),
                                at=str(item.get("at", "")),
                            )
                        )
                    except (TypeError, ValueError, OverflowError):
                        # OverflowError included: a stored `1e309` parses to
                        # float infinity and `int(inf)` raises OverflowError,
                        # not ValueError — one bad row must not abort startup.
                        continue
            self._pending = restored
            raw_seq = raw.get("seq") if isinstance(raw, dict) else None
            floor = max((f.seq for f in restored), default=0)
            self._seq = max(int(raw_seq), floor) if isinstance(raw_seq, int) else floor
            # What we just read IS what is on disk — the rollback pair starts here.
            self._persisted_pending = list(self._pending)
            self._persisted_seq = self._seq
            if not self._state.stats.first_launch:
                # Stamp it AND persist it. Setting this in memory only meant every
                # load re-stamped it, so "together since" silently reset to today
                # on each gateway restart — the one number in Memories that is
                # supposed to be immovable.
                self._state = replace(
                    self._state,
                    stats=replace(self._state.stats, first_launch=to_iso(self._now())),
                )
                self._save_locked()

    def _save_locked(self) -> None:
        """Write via tmp file + rename so a crash mid-write cannot truncate the store.

        Raises on failure — deliberately. Swallowing the OSError meant a full or
        read-only data home produced an HTTP 200: the panel cleared its input, the
        user believed the reminder was set, and the next restart read a file that
        had never been written. A visible error is recoverable; a confident lie is
        not.

        On failure it also rolls memory back to the last state that reached disk.
        Doing it HERE rather than at each of the eight mutation sites is deliberate:
        every one of them assigns `self._state` and then saves, so a per-site
        rollback is eight chances to forget one — and the state that actually
        reached disk is the only correct thing to roll back to anyway.
        """
        payload = file_to_dict(self._state.reminders)
        payload["config"] = _config_to_dict(self._state.config)
        payload["stats"] = _stats_to_dict(self._state.stats)
        # The pending queue and its sequence persist WITH the store. A due
        # reminder is consumed from `reminders` the moment it is queued, so
        # between that tick and the overlay's poll the fire exists ONLY here —
        # a gateway restart in that window silently lost it (the client's
        # refetch-from-zero restart recovery found nothing to refetch). `seq`
        # rides along so a restart does not reissue numbers below a client's
        # persisted cursor, which would make the survivors invisible too.
        payload["pending"] = [asdict(f) for f in self._pending]
        payload["seq"] = self._seq
        tmp = self._path.with_suffix(f".json.tmp.{os.getpid()}")
        try:
            serialized = json.dumps(payload, indent=2)
            # Enforce the SAME cap the loader applies. `load()` ignores a file
            # over MAX_BYTES and starts from defaults — so a successful write
            # of an oversized (but valid) payload meant the next restart
            # silently discarded ALL companion data and the following save
            # overwrote the file for good. Refusing here routes through the
            # existing rollback + 503 path: memory falls back to the last
            # state that reached disk and the caller sees an explicit error.
            if len(serialized.encode("utf-8")) > MAX_BYTES:
                raise OSError(
                    f"store payload exceeds MAX_BYTES ({MAX_BYTES}); refusing to"
                    " write a file the loader would ignore"
                )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(serialized, "utf-8")
            # chmod_safe, not os.chmod: the root AGENTS.md mandates the
            # platform_compat shim, which is a no-op where POSIX modes mean
            # nothing (Windows) instead of raising or silently misleading.
            chmod_safe(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("crew-companion: store write failed: %s", exc)
            try:
                tmp.unlink(missing_ok=True)   # no half-written temps left behind
            except OSError:
                pass
            if self._persisted is not None:
                self._state = self._persisted
                # The queue fails back WITH the state. Rolling back only
                # `_state` re-armed the reminder row while the already-queued
                # fire stayed in `_pending` — every retried tick then queued the
                # same reminder again and the user heard it once per retry.
                # Restoring both to the last state that reached disk means the
                # retry re-queues exactly once, not cumulatively.
                self._pending = list(self._persisted_pending)
                self._seq = self._persisted_seq
            raise
        self._persisted = self._state
        self._persisted_pending = list(self._pending)
        self._persisted_seq = self._seq

    # ── reads ───────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """The GET /reminders payload, in the camelCase shape the page expects.

        ``present`` reports whether the desktop overlay is currently on screen —
        the SAME signal that gates break nudges, surfaced so the dashboard page
        can tell "running" from merely "enabled". It is the negation of
        :meth:`_is_away`: true only while the overlay's presence ping (every 30s,
        see the overlay's ``PRESENCE_MS``) is inside ``PRESENCE_TTL_SECONDS``.
        This read itself always answers while the app is enabled — the in-process
        backend does not need the overlay — so a closed overlay is a definite
        ``present: false``, not a failed request. Folding it into this payload
        rather than a second endpoint keeps the page's existing poll a single
        request.
        """
        with self._lock:
            cfg = self._state.config
            return {
                "reminders": [
                    r
                    for r in file_to_dict(self._state.reminders)["reminders"]
                    if not r.get("done")
                ],
                "breakNudgesEnabled": cfg.break_nudges_enabled,
                "sessionNotificationsEnabled": cfg.session_notifications_enabled,
                "breakReminderMins": cfg.break_reminder_mins,
                "language": cfg.language,
                "petX": cfg.pet_x,
                "petY": cfg.pet_y,
                "activeAppearance": cfg.active_appearance,
                # Nested under `kiro` because that is where the gallery writes it
                # and where the overlay looks for it; a flat key here would read
                # as "no prop" and quietly undress the ghost.
                "kiro": {"accessory": cfg.accessory},
                "customPresets": list(cfg.custom_presets),
                "present": not self._is_away(),
            }

    def stats_payload(self) -> dict[str, Any]:
        with self._lock:
            s = self._state.stats
            return {
                "stats": {
                    "firstLaunch": s.first_launch,
                    "streak": s.streak,
                    "companionSeconds": s.companion_seconds,
                    "breathingSessions": s.breathing_sessions,
                    "remindersCreated": s.reminders_created,
                    "earliestActiveTime": s.earliest_active_time,
                    "latestActiveTime": s.latest_active_time,
                },
                "petName": "Crew Companion",
                "language": self._state.config.language,
            }

    def drain(self, since: int) -> dict[str, Any]:
        """Fires with a sequence number greater than ``since``.

        Cursor-based rather than destructive: the overlay can lose a response, or
        two overlays can exist across displays, without a reminder vanishing.
        """
        with self._lock:
            items = [f for f in self._pending if f.seq > since]
            return {
                "cursor": self._seq,
                "fires": [
                    {"seq": f.seq, "kind": f.kind, "text": f.text, "key": f.key, "at": f.at}
                    for f in items
                ],
            }

    # ── mutations ───────────────────────────────────────────────────────────

    def add(self, text: str, fire_at: str, every_minutes: int | None = None) -> dict[str, Any]:
        """Store an already-resolved reminder.

        The natural-language parsing happens in the renderer, which POSTs a
        concrete ``fireAt``. This deliberately does no natural-language parsing:
        duplicating 900 lines of span-alignment and day-part rules in a second
        language would re-earn every bug they took ten review rounds to remove.

        It does, however, insist the instant is READABLE. Rejecting it here and
        not in the route keeps one chokepoint for every writer, and the route
        already renders a ValueError as a 400. The cost of letting one through is
        not one bad row: ``due_reminders`` re-parses every row on every tick, so a
        single unparsable ``fireAt`` raises for the whole scan and silently stops
        reminders AND break nudges until the process restarts. (A restart clears
        it -- ``row_from_dict`` drops unreadable rows on load -- which is exactly
        what would make it maddening to diagnose in the wild.)
        """
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("text is required")
        try:
            parse_iso(fire_at)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"fireAt is not a readable instant: {fire_at!r}") from exc
        reminder = Reminder(
            id=str(uuid.uuid4()),
            text=cleaned,
            fire_at=fire_at,
            recurrence=Recurrence(int(every_minutes)) if every_minutes else None,
            created_at=to_iso(self._now()),
        )
        with self._lock:
            self._state = replace(
                self._state,
                reminders=replace(
                    self._state.reminders,
                    reminders=self._state.reminders.reminders + (reminder,),
                ),
                # Counted here rather than at each call site: the page, the panel
                # and the MCP tool all funnel through this one function, so none
                # of them can forget to increment it.
                stats=replace(
                    self._state.stats,
                    reminders_created=self._state.stats.reminders_created + 1,
                ),
            )
            self._save_locked()
        return {"ok": True, "id": reminder.id}

    def remove(self, ident: str) -> dict[str, Any]:
        with self._lock:
            kept = tuple(r for r in self._state.reminders.reminders if r.id != ident)
            found = len(kept) != len(self._state.reminders.reminders)
            if found:
                self._state = replace(
                    self._state,
                    reminders=replace(self._state.reminders, reminders=kept),
                )
                self._save_locked()
        return {"ok": found}

    def skip(self, ident: str) -> dict[str, Any]:
        """Push a recurring reminder past its next occurrence. No-op for one-time."""
        with self._lock:
            rows = list(self._state.reminders.reminders)
            for i, r in enumerate(rows):
                if r.id != ident:
                    continue
                moved = skip_once(r, self._now())
                if moved is r:  # one-time: nothing to skip to
                    return {"ok": False, "reason": "not-recurring"}
                rows[i] = moved
                self._state = replace(
                    self._state,
                    reminders=replace(self._state.reminders, reminders=tuple(rows)),
                )
                self._save_locked()
                return {"ok": True, "fireAt": moved.fire_at}
        return {"ok": False, "reason": "not-found"}

    def patch_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial config change, ignoring values that are not usable.

        An unusable interval leaves the current setting ALONE rather than resetting
        it to a default the user did not choose — same rule as the desktop app's
        ``clampBreakMins`` returning null.
        """
        with self._lock:
            cfg = self._state.config
            before_mins = cfg.break_reminder_mins
            if isinstance(patch.get("breakNudgesEnabled"), bool):
                cfg = replace(cfg, break_nudges_enabled=patch["breakNudgesEnabled"])
            if isinstance(patch.get("sessionNotificationsEnabled"), bool):
                cfg = replace(
                    cfg, session_notifications_enabled=patch["sessionNotificationsEnabled"]
                )
            if "breakReminderMins" in patch:
                clamped = clamp_break_mins(patch["breakReminderMins"])
                if clamped is not None:
                    cfg = replace(cfg, break_reminder_mins=clamped)
            if isinstance(patch.get("activeAppearance"), str):
                chosen = patch["activeAppearance"].strip()
                if chosen:
                    cfg = replace(cfg, active_appearance=chosen)
            if "petX" in patch and "petY" in patch:
                px, py = _coord(patch["petX"]), _coord(patch["petY"])
                # Both or neither: half a position is worse than none, because the
                # companion would land on an axis the user never chose.
                if px is not None and py is not None:
                    cfg = replace(cfg, pet_x=px, pet_y=py)
            # `kiro.accessory` arrives nested; `customPresets` flat. Both were
            # accepted and dropped before, and the endpoint still answered ok.
            nested = patch.get("kiro")
            if isinstance(nested, dict) and isinstance(nested.get("accessory"), str):
                worn = nested["accessory"].strip()
                if worn:
                    cfg = replace(cfg, accessory=worn)
            if isinstance(patch.get("customPresets"), list):
                cfg = replace(cfg, custom_presets=tuple(patch["customPresets"]))
            self._state = replace(self._state, config=cfg)
            self._save_locked()
            # Re-arm ONLY when the interval itself changed, so a shortened interval
            # takes effect now rather than after the old, longer one elapses.
            #
            # This used to re-arm on EVERY patch, which quietly broke break nudges
            # altogether: the overlay saves the companion's position through this same
            # config endpoint, and the companion moves ITSELF (the idle fidget). So
            # each little hop reset the break countdown, and a companion left alone
            # postponed its own breaks indefinitely. Measured live: 22 seconds before
            # a nudge was due, a position write pushed it back out to 269 seconds.
            #
            # The app this was ported from never had the bug — it re-arms only on
            # start, on return from away, and after firing, and reads the interval
            # lazily at arm time. Gating on a real change keeps the prompt behaviour
            # that comment wanted without inventing the regression.
            if cfg.break_reminder_mins != before_mins:
                self._next_break_at = 0.0
        self._last_stats_flush = 0.0
        return {"ok": True}

    def note_presence(self) -> dict[str, Any]:
        """The overlay reporting that the user is there."""
        self._last_presence = time.monotonic()
        return {"ok": True}

    def note_breathing_session(self) -> dict[str, Any]:
        """A guided exercise was COMPLETED (distinct from being suggested)."""
        with self._lock:
            self._state = replace(
                self._state,
                stats=replace(
                    self._state.stats,
                    breathing_sessions=self._state.stats.breathing_sessions + 1,
                ),
            )
            self._save_locked()
        return {"ok": True}

    def queue_window_command(self, target: str) -> dict[str, Any]:
        """Record a dashboard request to open one of the companion's windows.

        The dashboard page cannot open the panel or the avatar gallery itself:
        those are Electron windows owned by the desktop main process, reachable
        only from the always-running overlay, which holds the preload bridge. So
        the page POSTs the intent here and it rides the SAME queue a nudge does —
        the overlay drains ``/pending`` on the poll it already runs, sees a
        ``command`` kind, and carries it out instead of drawing a bubble. No new
        channel and no faster poll. ``target`` is the window to open ('panel' or
        'gallery'); the route has already validated it.
        """
        with self._lock:
            self._queue_locked("command", text=target)
        return {"ok": True}

    # ── the tick ────────────────────────────────────────────────────────────

    def _is_away(self) -> bool:
        if self._last_presence == 0.0:
            return True  # never heard from an overlay — nobody to nudge
        return (time.monotonic() - self._last_presence) > PRESENCE_TTL_SECONDS

    def _queue_locked(self, kind: str, *, text: str = "", key: str = "") -> None:
        self._seq += 1
        self._pending.append(
            Fire(seq=self._seq, kind=kind, text=text, key=key, at=to_iso(self._now()))
        )
        if len(self._pending) > MAX_PENDING:
            # Trim to the cap, but drop AMBIENT fires before a reminder.
            #
            # The cap exists so an overlay closed for days cannot grow an unbounded
            # queue, and it still holds absolutely. What changed is the order of
            # sacrifice: dropping the oldest indiscriminately meant a reminder the user
            # explicitly set could be discarded to make room for break nudges. A nudge
            # is ambient; a reminder is a promise. So nudges and commands give way
            # first, and only once they are exhausted do the oldest reminders go —
            # which keeps the bound real even when the whole backlog is reminders.
            excess = len(self._pending) - MAX_PENDING
            ambient = [i for i, f in enumerate(self._pending) if f.kind != "reminder"]
            drop = set(ambient[:excess])
            if len(drop) < excess:
                still = excess - len(drop)
                for i, f in enumerate(self._pending):
                    if still == 0:
                        break
                    if i not in drop:
                        drop.add(i)
                        still -= 1
            self._pending = [f for i, f in enumerate(self._pending) if i not in drop]
        # Nudge the overlay. The fire is already in the queue, so a broadcast that
        # fails costs latency (the poll still finds it) and never a notification —
        # which is why this is wrapped rather than allowed to escape into the tick.
        if self._on_fire is not None:
            try:
                self._on_fire()
            except Exception:  # noqa: BLE001 — a push failure must not stop the tick
                logger.debug("crew-companion: fire broadcast failed", exc_info=True)

    def tick(self) -> None:
        """One pass. Safe to call directly from tests with an injected clock."""
        now = self._now()
        monotonic = time.monotonic()

        with self._lock:
            self._accumulate_activity_locked(now)
            # Bounded stats flush (see STATS_FLUSH_SECONDS). Piggybacks on the
            # tick's lock; skipped whenever any other mutation already saved,
            # because _save_locked writes the WHOLE store including stats.
            if monotonic - self._last_stats_flush >= STATS_FLUSH_SECONDS:
                self._last_stats_flush = monotonic
                try:
                    self._save_locked()
                except OSError:
                    # A full disk must not kill the tick; the write-failure path
                    # already rolled state+queue back and logged. Stats retry on
                    # the next flush window.
                    pass

            # 1. Scheduled reminders. These fire even if they came due while the
            #    user was away — they asked for a specific time, so late is right
            #    and dropped is not.
            due = due_reminders(self._state.reminders.reminders, now)
            if due:
                for r in due:
                    self._queue_locked("reminder", text=r.text)
                advanced = {r.id: advance(r, now) for r in due}
                rows = tuple(
                    advanced.get(r.id, r) for r in self._state.reminders.reminders
                )
                # A fired one-time reminder is finished; drop it rather than
                # accumulating dead rows forever.
                rows = tuple(r for r in rows if not r.done)
                self._state = replace(
                    self._state,
                    reminders=replace(self._state.reminders, reminders=rows),
                )
                self._save_locked()

            # 2. Break rotation.
            if not self._state.config.break_nudges_enabled:
                return
            if self._is_away():
                # Push the next break out, so returning does not trigger one
                # immediately.
                self._arm_break_locked(monotonic)
                return
            if self._next_break_at == 0.0:
                self._arm_break_locked(monotonic)
                return
            if monotonic < self._next_break_at:
                return

            allow_breathe = can_prompt_breathe(self._state.reminders, now)
            nudge_id, keys = pick_break_nudge(self._rand, allow_breathe)
            key = pick_variant(keys, self._rand, self._last_nudge_key)
            self._last_nudge_key = key
            self._queue_locked(
                "break-breathe" if nudge_id == "breathe" else "break", key=key
            )
            if nudge_id == "breathe":
                self._state = replace(
                    self._state,
                    reminders=note_breathe_prompt(self._state.reminders, now),
                )
                self._save_locked()
            self._arm_break_locked(monotonic)

    def _arm_break_locked(self, monotonic: float) -> None:
        self._next_break_at = monotonic + jittered_interval_seconds(
            self._state.config.break_reminder_mins, rand=self._rand
        )

    def _accumulate_activity_locked(self, now: datetime) -> None:
        """Count a second of enabled time, and keep the activity window current."""
        s = self._state.stats
        hhmm = f"{now.hour:02d}:{now.minute:02d}"
        today = _day_key_local(now)

        streak = s.streak
        if s.last_active_day != today:
            # A gap of more than one day breaks the streak; consecutive days extend it.
            streak = s.streak + 1 if _is_yesterday(s.last_active_day, today) else 1

        self._state = replace(
            self._state,
            stats=replace(
                s,
                companion_seconds=s.companion_seconds + int(TICK_SECONDS),
                earliest_active_time=(
                    hhmm if not s.earliest_active_time or hhmm < s.earliest_active_time
                    else s.earliest_active_time
                ),
                latest_active_time=(
                    hhmm if not s.latest_active_time or hhmm > s.latest_active_time
                    else s.latest_active_time
                ),
                last_active_day=today,
                streak=streak,
            ),
        )

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin ticking. Idempotent, so a repeated on_startup is harmless."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="crew-companion-tick", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop ticking and flush. Idempotent."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        with self._lock:
            self._save_locked()

    def _run(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                # A throwing tick must never kill the loop, or reminders stop
                # silently — which is the one failure a reminder app cannot have.
                logger.exception("crew-companion: tick failed")


def _is_yesterday(previous: str, today: str) -> bool:
    """Whether ``previous`` is the calendar day immediately before ``today``."""
    if not previous:
        return False
    try:
        a = datetime.strptime(previous, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        b = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (b - a).days == 1


# ── config / stats serialisation ────────────────────────────────────────────


def _coord(raw: Any) -> int | None:
    """Coerce a stored coordinate, or None when it is not a usable number.

    Deliberately tolerant: this value round-trips through JSON written by a previous
    version and by the renderer, so a string, a float or a null all have to degrade
    to "no saved position" rather than raise and take the whole store down with them.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError, OverflowError):
        return None


def _config_from_dict(raw: Any) -> Config:
    section = raw.get("config") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return Config()
    mins = clamp_break_mins(section.get("breakReminderMins", DEFAULT_BREAK_MINS))
    return Config(
        break_nudges_enabled=bool(section.get("breakNudgesEnabled", True)),
        session_notifications_enabled=bool(
            section.get("sessionNotificationsEnabled", True)
        ),
        break_reminder_mins=mins if mins is not None else DEFAULT_BREAK_MINS,
        active_appearance=(
            section["activeAppearance"]
            if isinstance(section.get("activeAppearance"), str) and section["activeAppearance"]
            else "kiro-ghost"
        ),
        pet_x=_coord(section.get("petX")),
        pet_y=_coord(section.get("petY")),
        language=(
            section["language"] if isinstance(section.get("language"), str) else "English"
        ),
        accessory=(
            section["accessory"] if isinstance(section.get("accessory"), str) else "none"
        ),
        # A corrupt or hand-edited list degrades to "no saved presets" rather than
        # failing the whole load: the built-in palettes still work without them.
        custom_presets=(
            tuple(section["customPresets"])
            if isinstance(section.get("customPresets"), list)
            else ()
        ),
    )


def _config_to_dict(cfg: Config) -> dict[str, Any]:
    return {
        "breakNudgesEnabled": cfg.break_nudges_enabled,
        "sessionNotificationsEnabled": cfg.session_notifications_enabled,
        "breakReminderMins": cfg.break_reminder_mins,
        "language": cfg.language,
        "petX": cfg.pet_x,
        "petY": cfg.pet_y,
        "activeAppearance": cfg.active_appearance,
        "accessory": cfg.accessory,
        "customPresets": list(cfg.custom_presets),
    }


def _stats_from_dict(raw: Any) -> Stats:
    section = raw.get("stats") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return Stats()

    def _int(key: str) -> int:
        value = section.get(key)
        if not (isinstance(value, (int, float)) and value >= 0):
            return 0
        try:
            return int(value)
        except OverflowError:
            # A stored float infinity passes `>= 0` but `int(inf)` raises —
            # one corrupt stat must degrade to 0, not abort the whole load.
            return 0

    def _str(key: str) -> str:
        value = section.get(key)
        return value if isinstance(value, str) else ""

    return Stats(
        first_launch=_str("firstLaunch"),
        streak=_int("streak"),
        companion_seconds=_int("companionSeconds"),
        breathing_sessions=_int("breathingSessions"),
        reminders_created=_int("remindersCreated"),
        earliest_active_time=_str("earliestActiveTime"),
        latest_active_time=_str("latestActiveTime"),
        last_active_day=_str("lastActiveDay"),
    )


def _stats_to_dict(s: Stats) -> dict[str, Any]:
    return {
        "firstLaunch": s.first_launch,
        "streak": s.streak,
        "companionSeconds": s.companion_seconds,
        "breathingSessions": s.breathing_sessions,
        "remindersCreated": s.reminders_created,
        "earliestActiveTime": s.earliest_active_time,
        "latestActiveTime": s.latest_active_time,
        "lastActiveDay": s.last_active_day,
    }


__all__ = [
    "BREAK_MAX_MINS",
    "BREAK_MIN_MINS",
    "CompanionStore",
    "Config",
    "Fire",
    "Stats",
]
