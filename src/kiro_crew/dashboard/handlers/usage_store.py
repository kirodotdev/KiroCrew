"""The per-turn usage store and how its rows are read: where the shards live in the data
home, which daily shards a reporting window covers, the window a request names, how a
row's slot is classified, and the readers for a row's time and numeric fields.

An owner of :mod:`kiro_crew.dashboard.handlers.usage`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.messaging.link import telemetry_channel_of

# The facade's logger, so a log filter or capture keyed on the usage handler's name
# sees this module's records too.
logger = logging.getLogger("kiro_crew.dashboard.handlers.usage")


# Data-home paths are resolved per call, never captured at import.
#
# ``config_dir()`` / ``kiro_sessions_dir()`` read ``KIROCREW_HOME`` on every
# call, so binding their result to a module constant freezes whatever the home
# happened to be when this module was first imported. That breaks three things:
# pod isolation (a pod sets ``KIROCREW_HOME`` for its own process), the one-time
# ``~/.kirocrew`` -> ``~/.kiro/crew`` migration (deliberately lazy), and test
# isolation -- the autouse ``_isolate_kirocrew_home`` fixture runs *after*
# collection has already imported this module, so it silently cannot reach a
# frozen constant.
#
# The module-level name is kept as an explicit ``None`` override hook so callers
# that already patch it (tests, tooling) keep working; ``None`` means "resolve
# from the live home". This mirrors ``instances/registry.py``.
_SESSIONS_DIR: Path | None = None


def _sessions_dir() -> Path:
    """Kiro sessions directory, resolved against the live data home."""
    return _SESSIONS_DIR if _SESSIONS_DIR is not None else kiro_sessions_dir()


# See the _SESSIONS_DIR note above: resolved per call, ``None`` = live home.
_TOKEN_USAGE_DIR: Path | None = None


#: Window (in days) the Session Activity card and its Daily History table cover.
#: The per-day credits column reads the same window so the two line up.
_SESSIONS_HISTORY_DAYS = 30


def _token_usage_dir() -> Path:
    """Per-turn usage shard directory, resolved against the live data home."""
    if _TOKEN_USAGE_DIR is not None:
        return _TOKEN_USAGE_DIR
    return data_home() / "usage" / "tokens"


def _shard_path_for(ts: datetime) -> Path:
    """Return the daily shard path for the local-day component of ``ts``.

    Shards are partitioned by the user's local date so the file boundary
    matches the day boundary the dashboard chart renders against.
    """
    return _token_usage_dir() / f"{ts.astimezone().strftime('%Y-%m-%d')}.jsonl"


def _shards_in_window(days: float, until: float | None = None) -> list[Path]:
    """Return shards whose date falls inside the last ``days`` days.

    ``until`` (an epoch) anchors the window's END instead of now, so a range
    that ended in the past also drops the shards written after it.

    The directory listing is cheap (≤31 entries) and we filter by filename
    rather than statting each file, so this stays well under a millisecond
    even on years-old installs.

    A directory that exists but cannot be listed (a permission change, a
    roaming or network home that is briefly unreachable) yields the same empty
    window as a missing one: every reader of the shards treats an unreadable
    shard as "no rows", and the directory is held to the same rule, so a
    transient listing failure costs one refresh rather than the whole request.
    """
    paths: list[Path] = []
    shard_dir = _token_usage_dir()
    if not shard_dir.exists():
        return paths
    end = (
        datetime.now().astimezone() if until is None else datetime.fromtimestamp(until).astimezone()
    )
    # Subtract in epoch seconds, then convert: ``end - timedelta`` would keep
    # END's UTC offset, so a window whose start sits on the other side of a DST
    # change lands on the wrong local day and drops that day's shard.
    cutoff_date = datetime.fromtimestamp(end.timestamp() - days * 86400).astimezone().date()
    last_date = end.date()
    try:
        entries = list(shard_dir.iterdir())
    except OSError as exc:
        logger.warning("usage: cannot list the per-turn usage shard directory: %s", exc)
        return paths
    for p in entries:
        if not p.is_file() or p.suffix != ".jsonl":
            continue
        try:
            shard_date = datetime.strptime(p.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if cutoff_date <= shard_date and (until is None or shard_date <= last_date):
            paths.append(p)
    return paths


#: Default window (in days) for a spend request that names no window, and the
#: fixed window ``slot_spend`` sums for the Sessions table. The Telemetry panel's
#: range control can now ask ``cost_breakdown`` for any other window, so the
#: Spend card and the Sessions table agree only while the panel shows this
#: default; with another range picked they may legitimately differ.
SPEND_WINDOW_DAYS = 7


#: The shortest rolling (``days``) window. Fractions above it are honoured.
_MIN_ROLLING_DAYS = 1.0


#: The widest window the Telemetry panel's range control may ask for. Wider
#: requests are clamped to it rather than refused. The per-turn shards are never
#: pruned, so this is a read-cost ceiling, not a retention fact: the spend view
#: reads TWICE the window (for its preceding-period comparison), and 180 daily
#: shards is the most one 5s poll should open.
MAX_WINDOW_DAYS = 90


class TimeWindow(NamedTuple):
    """An effective ``[start, end)`` reporting window, in epoch seconds.

    ``rolling`` means the end tracks "now": the window was asked for as a length
    (``days``), not as a fixed range. Readers keep passing ``until=None`` for a
    rolling window so their caches key on the LENGTH, not on a ``now`` that
    changes every request and would never hit.
    """

    start: float
    end: float
    rolling: bool

    @property
    def days(self) -> float:
        return (self.end - self.start) / 86400

    @property
    def until(self) -> float | None:
        """The ``until`` readers take: ``None`` for a rolling window."""
        return None if self.rolling else self.end


def _parse_bound(raw: str) -> float | None:
    """One ``since``/``until`` query value as epoch seconds, or ``None`` if unusable.

    Epoch seconds only: the panel sends its local-midnight bounds that way so the
    browser's timezone decides what a calendar day is.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def resolve_window(
    *,
    days: str | None = None,
    since: str | None = None,
    until: str | None = None,
    default_days: float,
    now: float | None = None,
) -> TimeWindow:
    """Turn the raw ``days`` / ``since`` / ``until`` query values into a window.

    Never refuses: anything out of range is CLAMPED, because a range control
    that errors on an over-wide pick leaves the panel blank. The end is capped
    at now (rounded up to the minute), the start at ``MAX_WINDOW_DAYS`` before now,
    an inverted range is swapped, and a range that collapses to nothing
    becomes the day before its end, or the oldest legal day when its end
    sits at the start clamp.

    ``since`` or ``until`` makes the window fixed. ``days`` alone, or nothing,
    is rolling. Unparseable values fall back as if they were absent.
    """
    now = time.time() if now is None else now
    floor = now - MAX_WINDOW_DAYS * 86400
    # A fixed range's end is capped at the next minute boundary rather than at
    # the instant: a range that runs to "today" would otherwise carry a new
    # ``until`` on every 5s poll and miss every reader's cache. No row is
    # written in the future, so the extra seconds hold nothing.
    ceiling = math.ceil(now / 60) * 60
    start = _parse_bound(since) if since else None
    end = _parse_bound(until) if until else None
    if start is None and end is None:
        try:
            length = float(days) if days else float(default_days)
        except ValueError:
            length = float(default_days)
        if not math.isfinite(length):
            length = float(default_days)
        # One day is the shortest rolling window, as it was before ``days``
        # took fractions: a sub-day length would report minutes as "0.0 days".
        # A finite length at or below zero clamps up to it too, as main's
        # ``max(1, days)`` did; only a missing or unusable value takes the default.
        length = max(_MIN_ROLLING_DAYS, min(length, float(MAX_WINDOW_DAYS)))
        return TimeWindow(now - length * 86400, now, True)
    if end is None or end > ceiling:
        end = ceiling
    if start is None:
        start = end - float(default_days) * 86400
    if start > end:
        start, end = end, start
    end = min(max(end, floor), ceiling)
    start = max(start, floor)
    if end - start <= 0:
        start = max(end - 86400, floor)
    if end - start <= 0:
        # Both ends sat at the floor; the oldest legal day is all that is left.
        start, end = floor, floor + 86400
    return TimeWindow(start, end, False)


#: Keys that are not a session at all, and so are not rows.
#:
#: A subagent is a FRAGMENT of another session's turn, not a session with its own
#: lifecycle — nobody starts one, resumes one, or goes to look at one. It also
#: cannot be attributed: a `subagent:*` usage row carries no field pointing back
#: at the session that spawned it, so it cannot even be nested under its parent.
#: Ranking 218 of them beside 34 real sessions buried the list in work the user
#: never held.
#:
#: These rows are dropped at the point the row is READ, so their credits leave
#: the window total, the deltas and every breakdown together. That is deliberate:
#: filtering at the grouping step instead would leave the money in the total with
#: no row that could explain it, and no two views would agree. The consequence is
#: that these totals are the totals of the sessions the panel LISTS, not of the
#: account — stated in `docs/system-specs/modules/metrics.md` and pinned by
#: `test_a_subagent_is_not_a_session_and_reaches_no_figure`.
_NON_SESSION_CHANNELS = frozenset({"subagent"})


# Channels that ARE sessions but run without a person on the other end.
#
# A cron tick, the heartbeat and the task runner each have their own lifecycle —
# something scheduled them and they live and die on their own — so they are
# sessions, and they belong in a list keyed by session. They collapse to one `bg`
# category so they read as background work rather than as conversations.
#
# `other` and `unknown` are deliberately NOT here. `telemetry_channel_of` returns
# `other` for a key shape it does not recognise and `unknown` for an absent slot,
# so folding them in would bury every new surface under background — which is the
# precise failure this set is a DENYLIST to avoid. Membership is "background by
# nature", not "not recognised": an unclassified session stays visible under its
# own category, where it can be reported and fixed.
_BACKGROUND_CHANNELS = frozenset(
    {
        "cron",
        "heartbeat",
        "taskrunner",
        "workflow_pool",
        # A workflow STAGE's own session, which gained the ``workflow`` label
        # when the turn histogram needed one (it read ``other`` before, pooled
        # with unrecognised key shapes). Listed deliberately rather than left to
        # this set's fail-open behaviour: ``workflow_pool`` — the pool those
        # stages run on — is already background, and a stage is the same kind of
        # thing, so surfacing it as its own new panel category would split one
        # concept across two rows.
        "workflow",
        "background",
        "consolidation",
        "secretary",
    }
)


#: The one category that can be opened from the dashboard. Kept separate from
#: the category list because "is a session" and "has a route" are different
#: questions — a Telegram thread is a first-class session with nowhere for a
#: dashboard link to go, which is exactly what a "titled -> link it" rule gets
#: wrong.
NAVIGABLE_CATEGORY = "dashboard"


_LEGACY_TELEMETRY_SURFACES = {"task_runner": "taskrunner"}


def _canonical_telemetry_surface(surface: str) -> str:
    """Return the operational telemetry spelling used for new and stored rows.

    The alias keeps historical token shards comparable with current writes;
    artifact use-case labels are a separate schema and are not normalized here.
    """
    return _LEGACY_TELEMETRY_SURFACES.get(surface, surface)


def is_session_slot(slot: str) -> bool:
    """Whether *slot* is a session in its own right, and so earns a row."""
    return bool(slot) and telemetry_channel_of(slot) not in _NON_SESSION_CHANNELS


def session_category(slot: str) -> str:
    """Classify *slot* into the session taxonomy the panel groups by.

    Returns ``bg`` for a session that runs unattended, otherwise the transport it
    came from (``dashboard``, ``telegram``, ``slack``, …). Built on
    :func:`telemetry_channel_of` rather than matching key shapes here: that is
    the one place that knows every session-key form, so a surface it learns
    about is classified consistently for metrics and for this panel.

    A DENYLIST decides what is background, deliberately. If a new transport is
    added and ``_BACKGROUND_CHANNELS`` is not updated, the session shows up under
    its own new category — visible, reportable, fixable — whereas an allowlist of
    "real" transports would silently file it as background and bury it.
    """
    channel = telemetry_channel_of(slot) if slot else "unknown"
    return "bg" if channel in _BACKGROUND_CHANNELS else channel


def _parse_row_dt(raw: Any) -> datetime | None:
    """A row's timestamp as a ``datetime``, or ``None`` when unparseable.

    THE one spelling for reading a stored row timestamp (``Z`` rewritten to
    ``+00:00`` for py3.10's ``fromisoformat``; a naive stamp left naive so a
    caller's ``.timestamp()`` reads it in local time): every reader of the same
    rows derives from this helper, because two readers that disagree about
    which rows a window contains produce numbers that cannot be reconciled.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts_str = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def _parse_row_ts(raw: str) -> float | None:
    """A shard row's ``ts`` as an epoch, or ``None`` when unparseable.

    ``timestamp()`` is guarded too: a parseable-but-extreme stamp (year 1)
    raises ``ValueError`` on local-time conversion, and a corrupt row must
    never take a read path down.
    """
    dt = _parse_row_dt(raw)
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _parse_row_day(raw: Any) -> str | None:
    """A row timestamp's LOCAL calendar day (``YYYY-MM-DD``), or ``None``.

    Same guard rationale as :func:`_parse_row_ts`: ``astimezone()`` performs
    the same local-time conversion and raises on the same extreme stamps.
    """
    dt = _parse_row_dt(raw)
    if dt is None:
        return None
    try:
        return dt.astimezone().strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return None


def _usage_number(value: Any) -> int | float | None:
    """A shard row's numeric field, or ``None`` when it is not a usable number.

    ints are accepted directly: ``math.isfinite`` would convert to float first
    and an oversized int raises ``OverflowError`` (a corrupt row must never 500
    a read path). bool is an int subclass and is not a count.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _coerce_int(value: Any) -> int:
    """Coerce ``value`` to ``int``, defaulting to 0 for non-numeric input.

    Keeps the emitted record JSON-serializable even if a caller passes a
    non-numeric context value.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
