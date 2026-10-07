"""When a cron job fires: the schedule evaluator.

Pure functions over a :class:`~kiro_crew.cron_service.model.CronJob` and a clock
reading -- the next-run math the listing shows, the boundary the timer arms on,
the due decision the timer tick makes, the jitter a run sleeps and the human
rendering of a schedule. Every timezone read goes through the published config
default (:func:`published_config_timezone`), never a config load, because the
timer tick and prompt assembly call these on the event loop.

The service owns the state these read (the job list, which jobs are running);
nothing here holds any, bar a bounded warn-once memo for bad cron expressions.
"""

from __future__ import annotations

import logging
import math
import random
import re
import time
from collections.abc import Container, Iterable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

try:
    from cron_descriptor import Options, get_description  # type: ignore[import-untyped]
except ImportError:
    Options = None  # type: ignore[assignment,misc]
    get_description = None  # type: ignore[assignment]
from croniter import croniter  # type: ignore[import-untyped]

from kiro_crew import platform_compat

if TYPE_CHECKING:
    from kiro_crew.cron_service.model import CronJob, CronSchedule

logger = logging.getLogger("kiro_crew.cron")

_TIMER_POLL_SECS = 30  # check for due cron-expr jobs

# Bound skip_date advancement by a WALL-CLOCK horizon rather than an iteration
# count. An iteration cap couples the bound to schedule granularity: sized for a
# weekly cron (old 52) it broke daily crons; re-sized for daily it would then
# break sub-daily (e.g. a */5 cron does 288 fires/day and would exhaust a
# daily-sized cap within days). Bounding by wall-clock time removes the coupling
# entirely — a daily cron and a */5 cron both simply look ~2 years ahead for the
# next non-skipped fire. A large absolute iteration ceiling remains ONLY as an
# anti-infinite-loop safety net for a pathological all-skipped sub-minute
# schedule; realistic skip_dates lists are short (hand-entered) and exit far
# sooner, so the horizon is the binding constraint in every practical case.
_MAX_SKIP_DATE_HORIZON_SECS = 2 * 365 * 24 * 3600  # ~2 years of look-ahead
_MAX_SKIP_DATE_LOOKAHEAD = 500_000  # absolute safety ceiling (anti-infinite-loop)

# Jitter bounds (seconds) to spread job execution and avoid traffic spikes
_JITTER_HOURLY_MAX = 5 * 60  # 0–5 minutes for hourly jobs
_JITTER_DAILY_MAX = 59 * 60  # 0–59 minutes for daily jobs
# Longest single sleep inside the jitter wait. The wait ends on a WALL-CLOCK
# deadline, but asyncio sleeps on time.monotonic(), which on macOS
# (mach_absolute_time) does not advance while the host is asleep. Slicing the
# wait bounds how long a resumed host keeps sleeping past a deadline the wall
# clock already crossed: at most one slice of awake time.
_JITTER_WALL_SLICE_SECS = 30


def cron_expr_matches(expr: str, dt: datetime) -> bool:
    """Check if ``dt`` matches a 5-field cron expression (min hour dom month dow)."""
    try:
        return croniter.match(expr, dt)
    except (ValueError, KeyError):
        return False


def validate_cron_expr(expr: str) -> bool:
    """Return True if ``expr`` is a syntactically valid 5-field cron expression."""
    return croniter.is_valid(expr)


def _humanize_cron(expr: str, tz_name: str = "") -> str:
    """Convert a 5-field cron expression to human-readable string with timezone."""
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if get_description is None:
        return expr
    opts = Options()
    opts.use_24hour_time_format = False
    try:
        desc = get_description(expr, opts)
    except Exception:
        return expr

    # Timezone-aware display: evaluate the cron expression in the job's
    # timezone (matching compute_next_run_ts) and display the local time.
    parts = expr.split()
    if tz_name and len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
        try:
            tz = ZoneInfo(tz_name)
            # Evaluate in job timezone, same as the scheduler does
            base = seams.datetime.now(tz)
            next_local = croniter(expr, base).get_next(seams.datetime).astimezone(tz)
            local_time = platform_compat.strftime(next_local, "%-I:%M %p %Z")
            # cron_descriptor produces UTC-based text; replace the time portion
            utc_base = seams.datetime.now(timezone.utc)
            next_as_utc = croniter(expr, utc_base).get_next(seams.datetime)
            utc_time = platform_compat.strftime(next_as_utc, "%-I:%M %p")
            utc_time_padded = next_as_utc.strftime("%I:%M %p")
            result = desc.replace(f"At {utc_time}", f"At {local_time}")
            if result == desc:
                result = desc.replace(f"At {utc_time_padded}", f"At {local_time}")
            if result == desc:
                # Fallback: prepend local time if replacement failed
                result = f"At {local_time}, {desc.removeprefix('At ')}"
            return result
        except Exception:
            pass

    return desc


def format_schedule(schedule: CronSchedule, tz_name: str = "") -> str:
    """Human-readable schedule description."""
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    # Fallback: the published config default. Reading the snapshot rather than
    # loading config.json is what lets a loop-side caller omit tz_name safely.
    if not tz_name:
        tz_name = seams.published_config_timezone()
    if schedule.kind == "cron" and schedule.cron_expr:
        return _humanize_cron(schedule.cron_expr, tz_name)
    if schedule.kind == "every" and schedule.every_secs:
        secs = schedule.every_secs
        if secs % 3600 == 0:
            return f"every {secs // 3600}h"
        if secs % 60 == 0:
            return f"every {secs // 60}m"
        return f"every {secs}s"
    if schedule.kind == "at" and schedule.at_ts:
        # Tolerance lives HERE, at the render site, not in the deserializer: a
        # stored at_ts that datetime.fromtimestamp cannot represent (NaN or
        # Infinity from bare json.loads, epoch milliseconds, a beyond-year-9999
        # stamp, a pre-epoch value on Windows) must degrade to a fallback
        # string instead of raising inside the comprehension that serializes
        # EVERY job on GET /api/crons -- and the record itself is preserved for
        # the operator to repair. Same degrade-on-render posture as
        # CronJob._render_run_stamp.
        try:
            tz = ZoneInfo(tz_name) if tz_name else None
            if tz:
                now = seams.datetime.now(tz)
                dt = seams.datetime.fromtimestamp(schedule.at_ts, tz)
            else:
                now = seams.datetime.now().astimezone()
                dt = seams.datetime.fromtimestamp(schedule.at_ts).astimezone()
            if dt.date() == now.date():
                return f"at {dt:%I:%M %p %Z}"
            return f"at {dt:%I:%M %p %Z}, {platform_compat.strftime(dt, '%b %-d')}"
        except Exception:
            logger.debug("format_schedule: unrenderable at_ts %r", schedule.at_ts, exc_info=True)
            return "at an invalid stored time"
    return schedule.kind


def is_valid_timezone(tz_name: str) -> bool:
    """Return True if ``tz_name`` is a resolvable IANA timezone key.

    Validates via the ``ZoneInfo`` constructor -- a single targeted, cached
    lookup -- rather than ``available_timezones()``, which recursively walks
    the entire tzdata tree and opens many files on every call. Because this
    runs on callers reachable from the async event loop (dashboard cron PATCH
    -> CronService.update_job), the cheap constructor path avoids blocking the
    gateway (see ``no-blocking-call-on-event-loop``). ``ZoneInfo`` raises
    ``ZoneInfoNotFoundError`` for unknown keys and ``ValueError`` for malformed
    ones (e.g. absolute paths, ``..``); both are treated as invalid.
    """
    if not tz_name:
        return False
    try:
        ZoneInfo(tz_name)
    except Exception:
        return False
    return True


def is_valid_skip_date(value: object) -> bool:
    """Return True iff ``value`` is a strict, zero-padded ``YYYY-MM-DD`` date.

    ``datetime.strptime(s, "%Y-%m-%d")`` accepts non-padded inputs such as
    ``"<year>-1-1"``: they parse fine, but fire-time skip matching compares
    against a zero-padded rendering (``"<year>-01-01"``), so the intended skip
    silently never matches and the job runs on a date the user told it to
    skip -- with no error anywhere. Requiring the parsed value to round-trip
    exactly back to ``%Y-%m-%d`` rejects non-padded (and calendar-invalid)
    inputs at every persistence path, independent of the running Python
    version's ``date.fromisoformat`` leniency.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    s = str(value)
    try:
        return seams.datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d") == s
    except (ValueError, TypeError):
        return False


def get_local_tz() -> tuple[str, ZoneInfo]:
    """Return (tz_name, ZoneInfo) from the published config default, or UTC.

    Reads :func:`published_config_timezone` rather than loading ``config.json``:
    prompt assembly (``context.py``), the dashboard cron handler and the
    messaging commands all reach this from the event loop, where a
    stat/read/validate would be a per-call stall
    (``no-blocking-call-on-event-loop``). The snapshot is refreshed by every
    successful config load, so a settings change still reaches a running gateway.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        tz_name = seams.published_config_timezone() or "UTC"
        return tz_name, ZoneInfo(tz_name)
    except Exception:
        logger.warning(
            "Failed to load timezone from config, falling back to UTC",
            exc_info=True,
        )
        return "UTC", ZoneInfo("UTC")


# Patterns for parse_time_string
_RE_IN_DURATION = re.compile(
    r"^in\s+(\d+)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours)$", re.I
)
_UNIT_SECS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
}


def _wall_clock_resolution_error(value: datetime, tz: ZoneInfo, tz_name: str) -> str | None:
    """Return an error when a zone cannot resolve the requested wall clock."""
    clock_format = "%H:%M:%S" if value.second else "%H:%M"
    clock = value.strftime(clock_format)
    date = f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
    try:
        round_tripped = value.astimezone(timezone.utc).astimezone(tz)
    except (OverflowError, ValueError):
        return f"Error: {date} {clock} in {tz_name} is outside the supported date range"
    wall_clock = (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
    )
    round_tripped_wall_clock = (
        round_tripped.year,
        round_tripped.month,
        round_tripped.day,
        round_tripped.hour,
        round_tripped.minute,
        round_tripped.second,
        round_tripped.microsecond,
    )
    if round_tripped_wall_clock == wall_clock:
        return None
    return (
        f"Error: {clock} on {date} does not exist in {tz_name} "
        "(the zone's clocks skip it); pick a time the zone has"
    )


def parse_time_string(s: str, tz_name: str = "") -> float | str:
    """Parse a human time string into a Unix timestamp. Returns error string on failure.

    Lives here, next to :func:`get_local_tz`, because EVERY one-shot entry point
    needs it: the ``cron_add`` MCP tool, ``POST /api/crons`` and
    ``kirocrew cron add --at``. A second copy would let them drift, and "5pm"
    resolving to different instants depending on which door the request came
    through is exactly the class of bug a shared parser prevents. Relative forms
    ("in 30 minutes") are absolute already; a wall clock ("5pm", "tomorrow 9am",
    an ISO date and time) is read in *tz_name*, the job's own timezone, when the
    caller has one -- the same zone the job's ``cron_expr`` and ``skip_dates``
    are evaluated in and its schedule is rendered in. Without one it is read in
    the CONFIGURED timezone, never the process's, so a gateway running in UTC
    still honours the user's setting. *tz_name* must already have passed
    :func:`is_valid_timezone`; every caller checks it before parsing so a bad
    name is refused with that caller's own message.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    s = s.strip()
    if tz_name:
        resolved_tz_name = tz_name
        tz = ZoneInfo(tz_name)
    else:
        resolved_tz_name, tz = seams.get_local_tz()
    now = seams.datetime.now(tz)

    # "in 5 minutes", "in 2 hours"
    m = _RE_IN_DURATION.match(s)
    if m:
        secs = int(m.group(1)) * _UNIT_SECS[m.group(2).lower()]
        return time.time() + secs

    # Try common formats with optional "tomorrow"
    tomorrow = False
    text = s
    if text.lower().startswith("tomorrow"):
        tomorrow = True
        text = re.sub(r"^at\b\s*", "", text[8:].strip())

    # "5pm", "5:30pm", "17:00", "9:30am"
    for fmt in ("%I%p", "%I:%M%p", "%H:%M", "%I %p", "%I:%M %p"):
        try:
            parsed = seams.datetime.strptime(text, fmt)
            result = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            if tomorrow:
                result += timedelta(days=1)
            elif result <= now:
                result += timedelta(days=1)  # "5pm" when it's already 6pm → tomorrow
            error = _wall_clock_resolution_error(result, tz, resolved_tz_name)
            return error or result.timestamp()
        except ValueError:
            continue

    # ISO-ish: "YYYY-MM-DD HH:MM", space or "T" separator, seconds optional
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = seams.datetime.strptime(text, fmt).replace(tzinfo=now.tzinfo)
            error = _wall_clock_resolution_error(parsed, tz, resolved_tz_name)
            return error or parsed.timestamp()
        except ValueError:
            continue

    return f"Error: could not parse time '{s}'. Examples: '5pm', 'in 30 minutes', 'tomorrow 9am'"


def _job_tz(job: CronJob) -> ZoneInfo:
    """Return the job's timezone, falling back to the published default then UTC.

    Reads :func:`published_config_timezone` rather than loading ``config.json``.
    Both callers reach this from the event loop: :meth:`CronService._on_timer`
    scans EVERY cron-expression job through :meth:`_is_due` on every tick, and
    :meth:`CronJob.set_run_result` renders a completed run's stamp, so a config
    stat/read/validate here was a recurring gateway stall
    (``no-blocking-call-on-event-loop``). The snapshot is refreshed by every
    successful config load, so a timezone change still reaches a running
    gateway on the following tick.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        tz_name = job.timezone or seams.published_config_timezone() or "UTC"
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("Failed to resolve timezone for job %s, using UTC", job.id, exc_info=True)
        return ZoneInfo("UTC")


def compute_next_run_ts(job: CronJob, now: float | None = None) -> float | None:
    """Return the next fire time as a UTC epoch, or ``None`` if unknown.

    Never returns a non-finite float: the result feeds ``next_run_ts`` on the
    ``GET /api/crons`` payload, and ``json.dumps`` (``allow_nan=True`` by
    default) would emit the bare token ``Infinity`` -- invalid JSON that makes
    the client reject the WHOLE listing. Finite extremes a hand-edited store
    can carry still overflow here through arithmetic (``last + every_secs``
    with ``every_secs=1e308`` sums to ``inf``), so the guard sits on the
    RESULT, covering every arithmetic route at the serialize site.
    """
    result = _compute_next_run_ts_raw(job, now)
    # Float-gated like every non-finite check in this module: a bignum int
    # result serializes as digits (valid JSON) and math.isfinite(huge_int)
    # raises OverflowError -- the exact escape this guard exists to prevent.
    if isinstance(result, float) and not math.isfinite(result):
        return None
    return result


# hash(job id) -> hash(cron expression) already warned about by _next_cron_boundary_ts.
# Fixed-size ints, so the count cap below bounds the bytes too.
_BAD_CRON_WARNED: dict[int, int] = {}
_BAD_CRON_WARNED_MAX = 1024
_BAD_CRON_REFUSED = [0]  # evaluations the full memo refused a row


def _next_cron_boundary_ts(job: CronJob, now: float) -> float | None:
    """Return the immediate next cron boundary as a UTC epoch, ignoring skip_dates.

    For TIMER ARMING only. It answers "when is the next minute this expression
    matches" with a single ``croniter.get_next`` -- O(1), no ``skip_dates``
    traversal -- so a job with many ``skip_dates`` cannot make the on-loop
    re-arm walk up to ``_MAX_SKIP_DATE_LOOKAHEAD`` occurrences. ``skip_dates`` is
    still enforced at fire time by :meth:`CronService._is_due`, so a wake that
    lands on a skipped boundary is a cheap no-op that re-arms to the following
    boundary. Returns ``None`` for a non-cron or invalid expression, or when the
    boundary is non-finite, so the caller falls back to the poll interval.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    sched = job.schedule
    if sched.kind != "cron" or sched.cron_expr is None:
        return None
    try:
        tz = _job_tz(job)
        base = seams.datetime.fromtimestamp(now, tz=tz)
        nxt = croniter(sched.cron_expr, base).get_next(float)
    except Exception as exc:  # CroniterBadDateError for an unsatisfiable expression
        # Re-armed every poll: warn once per job+expression; a full memo refuses rows, said once.
        key, val = hash(job.id), hash(sched.cron_expr)
        if key in _BAD_CRON_WARNED or len(_BAD_CRON_WARNED) < _BAD_CRON_WARNED_MAX:
            if _BAD_CRON_WARNED.get(key) != val:
                _BAD_CRON_WARNED[key] = val
                logger.warning("Bad cron %r for job %s: %r", sched.cron_expr, job.id, exc)
        else:
            if not _BAD_CRON_REFUSED[0]:
                logger.warning("Bad cron warn-once memo is full; further jobs are not logged")
            _BAD_CRON_REFUSED[0] = min(_BAD_CRON_REFUSED[0] + 1, 2**31)  # saturating count
        return None
    _BAD_CRON_WARNED.pop(hash(job.id), None)
    if isinstance(nxt, float) and not math.isfinite(nxt):
        return None
    return nxt


def _compute_next_run_ts_raw(job: CronJob, now: float | None = None) -> float | None:
    """Unchecked next-fire-time computation; see :func:`compute_next_run_ts`."""
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        if not job.enabled:
            return None
        sched = job.schedule
        now = now if now is not None else time.time()
        if sched.kind == "every" and sched.every_secs is not None:
            last = job.last_run_ts if job.last_run_ts is not None else job.created_ts
            if last is None:
                return None
            nxt = last + sched.every_secs
            return nxt if nxt > now else now
        if sched.kind == "at" and sched.at_ts is not None:
            return sched.at_ts if sched.at_ts > now else None
        if sched.kind == "cron" and sched.cron_expr is not None:
            # croniter interprets cron_expr in base's timezone; get_next(float) returns UTC epoch
            tz = _job_tz(job)
            base = seams.datetime.fromtimestamp(now, tz=tz)
            cron = croniter(sched.cron_expr, base)
            # Advance past any skip_dates, bounded by a wall-clock horizon so the
            # bound does not depend on schedule granularity (a daily and a */5
            # cron both look ~2 years ahead). The iteration count is only a hard
            # safety ceiling against a pathological all-skipped sub-minute config.
            horizon = now + _MAX_SKIP_DATE_HORIZON_SECS
            for _ in range(_MAX_SKIP_DATE_LOOKAHEAD):
                nxt = cron.get_next(float)
                if not job.skip_dates:
                    return nxt
                if nxt > horizon:
                    logger.warning(
                        "No valid next run within ~2y horizon for job %s (all dates skipped)",
                        job.id,
                    )
                    return None
                local_date = seams.datetime.fromtimestamp(nxt, tz=tz).strftime("%Y-%m-%d")
                if local_date not in job.skip_dates:
                    return nxt
            logger.warning(
                "No valid next run within %d-iteration safety cap for job %s (all dates skipped)",
                _MAX_SKIP_DATE_LOOKAHEAD,
                job.id,
            )
            return None
    except Exception:
        logger.warning("Failed to compute next run for job %s", job.id, exc_info=True)
        return None
    return None


def compute_jitter(job: CronJob) -> float:
    """Return random jitter seconds based on schedule frequency.

    - strict_schedule=True or one-shot 'at' jobs: no jitter
    - Sub-hourly (every < 3600s or cron whose parsed minute field fires
      more than once per hour): no jitter
    - Hourly (every 3600–86399s or cron firing hourly): 0–5 min
    - Daily (every >= 86400s or cron firing daily): 0–59 min
    - Unrecognized cron patterns (fallback): 0–5 min
    """
    if job.strict_schedule:
        return 0.0
    sched = job.schedule
    if sched.kind == "at":
        return 0.0  # one-shot jobs fire at exact time
    if sched.kind == "every" and sched.every_secs:
        if sched.every_secs >= 86400:
            return random.uniform(0, _JITTER_DAILY_MAX)
        elif sched.every_secs >= 3600:
            return random.uniform(0, _JITTER_HOURLY_MAX)
        else:
            return 0.0  # sub-hourly jobs shouldn't be jittered
    if sched.kind == "cron" and sched.cron_expr:
        # Ask croniter for the normalized minute set. Wildcard collapses
        # to ["*"]; lists, ranges, steps, and their combinations expand
        # and deduplicate, so cardinality reflects actual fires per hour.
        try:
            expanded, _ = croniter.expand(sched.cron_expr)
            minute_values = expanded[0]
        except (KeyError, TypeError, ValueError):
            minute_values = []
        if minute_values == ["*"] or len(minute_values) > 1:
            return 0.0

        parts = sched.cron_expr.split()
        if len(parts) == 5:
            # Single literal hour (e.g., "0 3 * * *") = truly daily/weekly
            if parts[1].isdigit():
                return random.uniform(0, _JITTER_DAILY_MAX)
            # Multi-hour patterns (*/2, 1,13) or wildcard = hourly jitter
            if parts[1] != "*":
                return random.uniform(0, _JITTER_HOURLY_MAX)
        return random.uniform(0, _JITTER_HOURLY_MAX)
    return 0.0


def is_due(job: CronJob, now: float) -> bool:
    """Whether ``job`` is due at ``now``: its schedule has arrived and ``now`` is not a skip date."""
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if job.schedule.kind == "every" and job.schedule.every_secs:
        last = job.last_run_ts or job.created_ts
        if now < last + job.schedule.every_secs:
            return False
    elif job.schedule.kind == "at" and job.schedule.at_ts:
        if now < job.schedule.at_ts:
            return False
    elif job.schedule.kind == "cron" and job.schedule.cron_expr:
        tz = _job_tz(job)
        dt = seams.datetime.fromtimestamp(now, tz=tz)
        if not seams.cron_expr_matches(job.schedule.cron_expr, dt):
            return False
        # Don't re-fire within the same UTC minute (immune to DST ambiguity)
        if job.last_run_ts and int(job.last_run_ts) // 60 == int(now) // 60:
            return False
    else:
        return False
    # Skip dates check (evaluated in job's local timezone, applies to all schedule types)
    if job.skip_dates:
        local_date = seams.datetime.fromtimestamp(now, _job_tz(job)).strftime("%Y-%m-%d")
        if local_date in job.skip_dates:
            return False
    return True


def missed_cron_boundary(job: CronJob, since: float, now: float) -> float | None:
    """The most recent cron boundary in ``(since, now]`` that no scan could fire, or None.

    A cron-expression job is due only while its expression matches the CURRENT
    minute (:func:`is_due`), so a boundary that falls while the scheduler is
    not scanning -- the host asleep, or the event loop stalled -- is never
    fired: on wake the minute has passed. ``every``/``at`` jobs need no help,
    because they stay due until they run. ``since`` is the wall-clock time of
    the scheduler's previous scan IN THIS PROCESS, so a boundary after it is
    one no scan saw.

    Only the LATEST such boundary is returned, so any number of missed
    occurrences collapses to one catch-up fire. None when that boundary falls
    in ``now``'s minute (the ordinary :func:`is_due` path owns it), when the
    job already ran at or after it, when it or ``now`` lands on a skip date, or when the
    schedule cannot be evaluated.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    sched = job.schedule
    if sched.kind != "cron" or not sched.cron_expr or now <= since:
        return None
    try:
        tz = _job_tz(job)
        base = seams.datetime.fromtimestamp(now, tz=tz)
        prev = croniter(sched.cron_expr, base).get_prev(float)
    except Exception:
        return None
    if not isinstance(prev, (int, float)) or not math.isfinite(prev):
        return None
    if prev <= since or int(prev) // 60 == int(now) // 60:
        return None
    if job.last_run_ts and job.last_run_ts >= prev:
        return None
    if job.skip_dates:
        # The missed boundary's date AND the date the catch-up would run on:
        # a skipped day is skipped whichever of the two it is.
        for ts in (prev, now):
            local_date = seams.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
            if local_date in job.skip_dates:
                return None
    return prev


def next_wake_secs(jobs: Iterable[CronJob], claimed: Container[str], now: float) -> float | None:
    """Seconds from ``now`` until the next of ``jobs`` should fire, or None when none will.

    A disabled job, and a job whose id is in ``claimed`` (a run occupies it), is
    skipped: the claimed job's next due time is only knowable once its run ends,
    which re-arms the timer then.
    """
    delays: list[float] = []
    for job in jobs:
        if not job.enabled or job.id in claimed:
            continue
        if job.schedule.kind == "every" and job.schedule.every_secs:
            last = job.last_run_ts or job.created_ts
            next_run = last + job.schedule.every_secs
            delays.append(max(0.0, next_run - now))
        elif job.schedule.kind == "at" and job.schedule.at_ts:
            delays.append(max(0.0, job.schedule.at_ts - now))
        elif job.schedule.kind == "cron":
            # Wake ON the next cron boundary (as `at`/`every` do), not on a
            # flat poll whose phase is unrelated to the schedule -- a phase
            # gap can straddle a cron's single matching minute and drop the
            # occurrence, most visibly on low-frequency crons. Use the
            # skip-free boundary helper: it is O(1), so a job with many
            # skip_dates cannot make this on-loop re-arm traverse up to
            # _MAX_SKIP_DATE_LOOKAHEAD occurrences. skip_dates is enforced at
            # fire time by is_due; a wake on a skipped boundary is a no-op
            # that re-arms to the next one. _effective_delay() still caps the
            # armed delay at _TIMER_POLL_SECS so an externally-added job is
            # picked up within one poll.
            cron_next = _next_cron_boundary_ts(job, now)
            if cron_next is not None:
                delays.append(max(0.0, cron_next - now))
            else:
                delays.append(_TIMER_POLL_SECS)
    return min(delays) if delays else None
