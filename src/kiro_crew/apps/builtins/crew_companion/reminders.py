"""Reminder model and scheduling rules — pure, so every rule is unit-tested
without a filesystem, a gateway or a clock.

Ported behaviour-first from the Crew Companion desktop app's
``src/shared/reminders.ts``. The TypeScript is the specification until a rule is
*intentionally* changed: several behaviours below look like quirks and are
deliberate, and each one is called out where it lives. The original module's own
tests (``src/test/reminders.test.ts``, 29 cases) are ported alongside as
``test_crew_companion_reminders.py`` so the port is pinned rather than asserted.

A deliberately small model: one interval, one time. Six fields and a time
comparison is the whole of it.

WHAT DELIBERATELY DID NOT COME ACROSS
-------------------------------------
``labelFor`` / ``upNext`` / ``localeFor`` / ``clockLabel`` are *presentation* —
"in 45 min", "3:00 PM", "Tmrw". They stay in the renderer
(``website/src/apps/crew-companion/``), which formats in the dashboard's own
language through ``i18nT``. Porting them here would create a second source of
truth for date formatting in a second language stack, and the backend has no
business knowing what a row looks like.

The natural-language parser also stays in TypeScript. It already lives at
``website/src/apps/crew-companion/reminderParse.ts`` (+ ``reminderParseZh``,
``reminderText``), hardened over ten review rounds, and the page POSTs an
already-resolved ``{text, fireAt, everyMinutes}``. Re-implementing 900 lines of
span-alignment and day-part rules in a second language would re-earn every one of
those bugs for no gain.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

# ── ISO 8601 ────────────────────────────────────────────────────────────────

# JavaScript's `Date.prototype.toISOString()` always emits a trailing "Z", and
# every `fireAt` already on disk was written by it. `datetime.fromisoformat`
# only learned to accept "Z" in Python 3.11, and CI runs 3.10 as well — so a
# bare `fromisoformat` would raise on real stored data on the older interpreter
# and pass on the newer one. Normalising here keeps that difference out of every
# call site.


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 instant, tolerating the JS ``Z`` suffix. Always aware."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    # A naive string means "local", which is how the desktop app's own
    # `new Date(...)` would have read it; anchor it so comparisons never mix
    # aware and naive and raise.
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def to_iso(moment: datetime) -> str:
    """Render an instant exactly the way the desktop app did.

    ``Date.prototype.toISOString()`` always produces UTC with THREE decimal
    places and a ``Z`` suffix — ``2026-07-31T18:00:00.000Z``. Emitting
    ``...18:00:00Z`` instead would still parse, but it would silently change the
    format of every row rewritten in a file the desktop app created, and it would
    break the original test suite's exact-string assertions that this port is
    pinned against. So the milliseconds are deliberate, not decoration.
    """
    utc = moment.astimezone(timezone.utc)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"


# ── Model ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Recurrence:
    """How a reminder repeats. ``None`` in place of this means one-time.

    A single interval covers the phrasings people actually use — "every hour",
    "every 90 minutes", and daily via 1440 anchored to ``fire_at``'s time of day.
    """

    every_minutes: int


@dataclass(frozen=True)
class Reminder:
    id: str
    #: What to say when it fires.
    text: str
    #: When it next fires, ISO 8601.
    fire_at: str
    #: None for one-time.
    recurrence: Recurrence | None = None
    created_at: str = ""
    #: Set once a one-time reminder has fired; recurring ones are re-armed.
    done: bool = False


@dataclass(frozen=True)
class BreatheBudget:
    """How many times breathing has been SUGGESTED today (not performed)."""

    day: str
    count: int


@dataclass(frozen=True)
class ReminderFile:
    version: int = 1
    reminders: tuple[Reminder, ...] = ()
    breathe_budget: BreatheBudget | None = None


EMPTY_FILE = ReminderFile()


# ── Break nudges ────────────────────────────────────────────────────────────

#: The break rotation. One is chosen at random each time the break timer fires —
#: a single feature with one toggle and one interval, not four separate reminders
#: the user has to manage.
#:
#: The keys are listed explicitly rather than generated as ``break.water.{n}``:
#: a missing key would then surface only at runtime as a raw key string in the
#: notification, whereas an explicit list can be checked against the catalogue.
BREAK_NUDGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("water", tuple(f"break.water.{n}" for n in range(1, 6))),
    ("stretch", tuple(f"break.stretch.{n}" for n in range(1, 6))),
    ("distance", tuple(f"break.distance.{n}" for n in range(1, 6))),
    ("breathe", tuple(f"break.breathe.{n}" for n in range(1, 6))),
)

#: How often the pet may SUGGEST the breathing exercise in a day.
#:
#: Breathing asks for ~80 seconds and full attention, so an unprompted suggestion
#: is a far bigger ask than "drink some water". Twice a day keeps it feeling
#: offered rather than nagged. The user can always start it themselves from the
#: panel — this caps the PROMPT, never the exercise.
MAX_BREATHE_PROMPTS_PER_DAY = 2

#: Break-interval choices offered as one-tap presets. The panel and the dashboard
#: page render this same list, so the two surfaces cannot drift.
BREAK_PRESETS = (30, 45, 60, 90)

#: Bounds for a custom interval. Below 5 the companion would be a pest; above 8h
#: it would never fire in a working day.
BREAK_MIN_MINS = 5
BREAK_MAX_MINS = 480


def day_key(now: datetime) -> str:
    """Local calendar day key, for the daily prompt budget.

    Deliberately LOCAL rather than UTC: the budget exists to stop the companion
    nagging twice in one of the user's days, and a user near midnight UTC would
    otherwise get their allowance reset in the middle of an afternoon.
    """
    return f"{now.year:04d}-{now.month:02d}-{now.day:02d}"


def can_prompt_breathe(file: ReminderFile, now: datetime) -> bool:
    """Whether the companion may still suggest breathing today."""
    budget = file.breathe_budget
    if budget is None or budget.day != day_key(now):
        return True  # new day, budget resets
    return budget.count < MAX_BREATHE_PROMPTS_PER_DAY


def note_breathe_prompt(file: ReminderFile, now: datetime) -> ReminderFile:
    """Record that breathing was suggested, rolling over at midnight."""
    key = day_key(now)
    budget = file.breathe_budget
    if budget is not None and budget.day == key:
        return replace(file, breathe_budget=BreatheBudget(key, budget.count + 1))
    return replace(file, breathe_budget=BreatheBudget(key, 1))


def pick_variant(
    keys: Sequence[str],
    rand: Callable[[], float] = random.random,
    avoid: str | None = None,
) -> str:
    """Choose one phrasing.

    ``avoid`` is the key used last time and is skipped where possible — a rotation
    that can immediately repeat itself does not read as variety, it reads as a
    bug. With a single variant left there is nothing to avoid, so the constraint
    is DROPPED rather than yielding an empty pool. That fallback is the reason
    this is not simply a filter.
    """
    pool: Sequence[str] = (
        [k for k in keys if k != avoid] if len(keys) > 1 and avoid else keys
    )
    if not pool:  # avoid removed everything (all keys identical) — degrade, never raise
        pool = keys
    index = min(len(pool) - 1, int(rand() * len(pool)))
    return pool[index]


def pick_break_nudge(
    rand: Callable[[], float] = random.random,
    allow_breathe: bool = True,
) -> tuple[str, tuple[str, ...]]:
    """Pick a break nudge as ``(id, keys)``.

    ``allow_breathe=False`` drops the breathing suggestion from the pool for the
    rest of the day, so the remaining nudges still fire normally — the break is
    not skipped, only the breathing suggestion is.
    """
    pool = (
        BREAK_NUDGES
        if allow_breathe
        else tuple(n for n in BREAK_NUDGES if n[0] != "breathe")
    )
    index = min(len(pool) - 1, int(rand() * len(pool)))
    return pool[index]


def jittered_interval_seconds(
    base_minutes: float,
    jitter_fraction: float = 0.15,
    rand: Callable[[], float] = random.random,
) -> float:
    """Jitter the break interval so it feels like a companion, not a metronome.

    Returns SECONDS (the TypeScript returned milliseconds; seconds is the unit
    asyncio and the gateway's scheduler speak). Never less than a minute, so a
    tiny configured interval cannot turn into a busy loop.
    """
    spread = base_minutes * jitter_fraction
    minutes = base_minutes + (rand() * 2 - 1) * spread
    return max(60.0, round(minutes * 60.0))


def clamp_break_mins(raw: str | float | int) -> int | None:
    """Parse a user-typed interval, or ``None`` when it is not usable.

    Returning None rather than a fallback matters: a bad value must leave the
    current setting ALONE instead of silently resetting it to a default the user
    did not choose.
    """
    try:
        number = float(str(raw).strip()) if not isinstance(raw, (int, float)) else float(raw)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return int(min(BREAK_MAX_MINS, max(BREAK_MIN_MINS, round(number))))


# ── Firing ──────────────────────────────────────────────────────────────────


def due_reminders(reminders: Iterable[Reminder], now: datetime) -> list[Reminder]:
    """Reminders due at ``now`` — fire time passed, not already finished."""
    return [r for r in reminders if not r.done and parse_iso(r.fire_at) <= now]


#: Ceiling on the roll-forward loop in :func:`advance`, guarding a pathological
#: interval from spinning forever.
_MAX_ADVANCE_STEPS = 100_000


def advance(reminder: Reminder, now: datetime) -> Reminder:
    """Advance a reminder past ``now`` after it has fired.

    One-time reminders are marked done. Recurring ones roll forward to the next
    occurrence STRICTLY after now — rolling in a loop rather than adding a single
    interval, so a reminder that came due while the machine was asleep does not
    then fire repeatedly to "catch up" on every missed slot. That loop is the
    behaviour, not an optimisation: replacing it with one addition reintroduces
    the catch-up storm.
    """
    if reminder.recurrence is None:
        return replace(reminder, done=True)

    step = timedelta(minutes=max(1, reminder.recurrence.every_minutes))
    next_at = parse_iso(reminder.fire_at)
    steps = 0
    while next_at <= now and steps < _MAX_ADVANCE_STEPS:
        next_at += step
        steps += 1
    return replace(reminder, fire_at=to_iso(next_at))


def skip_once(reminder: Reminder, now: datetime) -> Reminder:
    """Push a recurring reminder past its next occurrence, without deleting it.

    "Not this time" is a different intent from "never again": deleting a daily
    reminder to dismiss today's is destructive, and re-creating it is work.
    One-time reminders have nothing to skip TO, so they come back unchanged and
    the caller should not offer the action for them.

    The loop runs at least once and lands STRICTLY in the future — a reminder
    whose slot already passed would otherwise be "skipped" to another past time
    and fire immediately.
    """
    if reminder.recurrence is None:
        return reminder

    step = timedelta(minutes=max(1, reminder.recurrence.every_minutes))
    next_at = parse_iso(reminder.fire_at)
    steps = 0
    while True:
        next_at += step
        steps += 1
        if next_at > now or steps >= _MAX_ADVANCE_STEPS:
            break
    return replace(reminder, fire_at=to_iso(next_at))


# ── Serialisation ───────────────────────────────────────────────────────────
#
# The wire and disk format stays exactly the camelCase JSON the desktop app
# wrote, so an existing store loads untouched and the renderer's TypeScript
# types need no change.


def reminder_to_dict(reminder: Reminder) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": reminder.id,
        "text": reminder.text,
        "fireAt": reminder.fire_at,
        "recurrence": (
            {"everyMinutes": reminder.recurrence.every_minutes}
            if reminder.recurrence
            else None
        ),
        "createdAt": reminder.created_at,
    }
    if reminder.done:
        payload["done"] = True
    return payload


def reminder_from_dict(raw: Any) -> Reminder | None:
    """Build a Reminder from stored JSON, or None when the row is unusable.

    Mirrors the desktop app's ``isReminder`` guard: a store with one corrupt row
    must load every other row rather than failing wholesale, because a reminder
    app that throws on load is worse than one that starts short.
    """
    if not isinstance(raw, dict):
        return None
    ident, text, fire_at = raw.get("id"), raw.get("text"), raw.get("fireAt")
    if not isinstance(ident, str) or not isinstance(text, str):
        return None
    if not isinstance(fire_at, str):
        return None
    try:
        parse_iso(fire_at)
    except (ValueError, TypeError):
        return None

    recurrence = None
    rec_raw = raw.get("recurrence")
    if isinstance(rec_raw, dict):
        every = rec_raw.get("everyMinutes")
        # Bounded range, matching the route's add-time guard: a stored
        # `1e309` is float infinity — it passes `> 0` and `int(inf)` raises
        # OverflowError, killing the row restore; and an absurd finite value
        # would later overflow `timedelta` in the recurrence scheduler,
        # which stops ALL reminders. Ten years in minutes, same cap.
        if (
            isinstance(every, (int, float))
            and not isinstance(every, bool)
            and 0 < every <= 10 * 366 * 24 * 60
        ):
            recurrence = Recurrence(int(every))

    created = raw.get("createdAt")
    return Reminder(
        id=ident,
        text=text,
        fire_at=fire_at,
        recurrence=recurrence,
        created_at=created if isinstance(created, str) else "",
        done=bool(raw.get("done")),
    )


def file_to_dict(file: ReminderFile) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "reminders": [reminder_to_dict(r) for r in file.reminders],
    }
    if file.breathe_budget is not None:
        payload["breatheBudget"] = {
            "day": file.breathe_budget.day,
            "count": file.breathe_budget.count,
        }
    return payload


def file_from_dict(raw: Any) -> ReminderFile:
    """Read a store, tolerating every failure mode: absent, bad JSON, wrong shape."""
    if not isinstance(raw, dict) or not isinstance(raw.get("reminders"), list):
        return EMPTY_FILE

    reminders = tuple(
        r for r in (reminder_from_dict(x) for x in raw["reminders"]) if r is not None
    )

    budget = None
    budget_raw = raw.get("breatheBudget")
    if isinstance(budget_raw, dict):
        day, count = budget_raw.get("day"), budget_raw.get("count")
        if isinstance(day, str) and isinstance(count, int):
            budget = BreatheBudget(day, count)

    return ReminderFile(version=1, reminders=reminders, breathe_budget=budget)
