"""Kiro usage handlers — local session analytics + kiro-cli billing."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import math
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.acp.types import TurnUsage
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.context_blocks import USER_LABEL
from kiro_crew.hooks import validate_file_path
from kiro_crew.jsonl_util import bounded_records
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.metrics.turns import (
    OUTCOME_UNCLASSIFIED,
    emit_turn_duration,
    emit_turn_usage,
    turn_outcome,
)

logger = logging.getLogger(__name__)

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


_CACHE: dict[str, Any] = {}
_CACHE_TS: float = 0.0
_CACHE_TTL = 120  # 2 min
_CACHE_LOCK = LoopBoundLock()

# Cache for the raw _parse_sessions() result, used by api_usage's
# claude_code/bedrock branch (api_kiro_usage has its own _CACHE of the full
# response). _parse_sessions does a full iterdir + per-file stat + line-by-line
# json.loads of every in-window shard, so it is both TTL-cached (120s) and run
# off the event loop. _SESSIONS_CACHE_LOCK collapses concurrent cold-cache
# requests into a single parse (mirrors api_kiro_usage's _CACHE_LOCK).
# None = unpopulated. A sentinel (not truthiness) so a valid-but-empty parse
# result ({}) is still cached and served from the fast path, rather than
# re-parsing on every call.
_SESSIONS_CACHE: dict[str, Any] | None = None
_SESSIONS_CACHE_TS: float = 0.0
_SESSIONS_CACHE_LOCK = LoopBoundLock()

# Cache for _parse_token_history — shards are append-only so we key the
# cache on a tuple of (filename, mtime, size) for every shard in the
# 30-day window. Any append to any shard changes the key, invalidating
# the cache exactly when needed. A 2 min TTL is also enforced as a
# safety net for clock skew and manual file edits.
_TOKEN_CACHE: dict[str, Any] = {}
_TOKEN_CACHE_KEY: tuple[tuple[str, float, int], ...] | None = None
_TOKEN_CACHE_TS: float = 0.0
_TOKEN_CACHE_TTL = 120  # 2 min
# See the _SESSIONS_DIR note above: resolved per call, ``None`` = live home.
_TOKEN_USAGE_DIR: Path | None = None
_TOKEN_HISTORY_DAYS = 30


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


def _shards_in_window(days: int) -> list[Path]:
    """Return shards whose date falls inside the last ``days`` days.

    The directory listing is cheap (≤31 entries) and we filter by filename
    rather than statting each file, so this stays well under a millisecond
    even on years-old installs.
    """
    paths: list[Path] = []
    shard_dir = _token_usage_dir()
    if not shard_dir.exists():
        return paths
    cutoff_date = (datetime.now().astimezone() - timedelta(days=days)).date()
    for p in shard_dir.iterdir():
        if not p.is_file() or p.suffix != ".jsonl":
            continue
        try:
            shard_date = datetime.strptime(p.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if shard_date >= cutoff_date:
            paths.append(p)
    return paths


#: Window (in days) that the spend tab and the sessions table both sum over.
#: This is the single source of truth: ``cost_breakdown`` defaults to it, and
#: ``slot_spend`` uses it, so the two surfaces are arithmetically incapable of
#: reporting different totals for the same session.
SPEND_WINDOW_DAYS = 7

# Bare dashboard slot key: chat-<seq>-<epoch>. The shard's ``slot`` field uses
# this shape (without a ``dashboard:`` prefix), while the session manager keys
# are ``dashboard:chat-<seq>-<epoch>``. Normalization aligns them.
_BARE_CHAT_SLOT_RE = re.compile(r"^chat-\d+-\d+$")

# ── per-slot spend (shared by the Sessions table and Spend tab) ────────────


def spend_key_for_slot(slot: str) -> str:
    """The key a slot's spend is filed under in :func:`slot_spend`'s result.

    One owner for this rule. Shards record a bare dashboard slot key
    (``chat-69-1785905004``) while sessions are addressed as
    ``dashboard:chat-69-…``; a caller that re-derived the prefix would be a second
    owner, and a second owner of an identity rule is exactly how the spend join
    and the session list drifted apart before.
    """
    return f"dashboard:{slot}" if _BARE_CHAT_SLOT_RE.match(slot) else slot


_SLOT_SPEND_CACHE: dict[str, dict[str, float]] = {}
_SLOT_SPEND_CACHE_SIG: tuple[object, ...] = ()
_SLOT_SPEND_CACHE_AT: float = 0.0

# The shard signature alone cannot key this cache. The result also depends on
# ``cutoff = now - days*86400``, which moves continuously, so on an idle machine
# (no shard write) a row that ages PAST the cutoff would keep being counted
# until some shard changed -- up to a day, since ``_shards_in_window`` only
# re-picks the shard set when the date rolls. A short TTL bounds that drift to
# seconds against a 7-day window while still sparing the 5s poll a re-read.
_SLOT_SPEND_TTL_S = 60.0


def slot_spend(days: int = SPEND_WINDOW_DAYS) -> dict[str, dict[str, float]]:
    """Per-session spend over the last *days*: ``{session_key: {"credits", "turns"}}``.

    This is the ONE aggregation path that both the Sessions table and the Spend
    tab's per-conversation view must go through. It uses the same per-row
    ``ts_epoch`` cutoff as :func:`cost_breakdown`, so a row inside the boundary
    shard but older than the cutoff is NOT counted; and it normalizes bare
    dashboard slot keys (``chat-69-1785905004``) to the full session key form
    (``dashboard:chat-69-1785905004``) so a direct lookup by session key works.

    Cached against shard size + mtime AND a short TTL, so polling every few
    seconds does not re-read the window while the moving cutoff still takes
    effect. Safe to call from a thread (the offloaded sampling thread in
    session_memory).
    """
    global _SLOT_SPEND_CACHE, _SLOT_SPEND_CACHE_SIG, _SLOT_SPEND_CACHE_AT

    now = time.time()
    paths = _shards_in_window(days)
    sig: tuple[object, ...] = tuple(
        (str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths if p.exists()
    )
    if (
        sig == _SLOT_SPEND_CACHE_SIG
        and _SLOT_SPEND_CACHE_SIG
        and now - _SLOT_SPEND_CACHE_AT < _SLOT_SPEND_TTL_S
    ):
        return _SLOT_SPEND_CACHE

    cutoff = now - (days * 86400)
    out: dict[str, dict[str, float]] = {}

    for path in paths:
        try:
            with path.open("rb") as fh:
                for line in bounded_records(fh, path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    # Per-row timestamp cutoff: the shard file date is a coarse
                    # filter (a shard can span midnight), so rows older than the
                    # cutoff must still be excluded individually.
                    ts_epoch = _parse_row_ts(str(obj.get("ts") or ""))
                    if ts_epoch is None or ts_epoch < cutoff:
                        continue
                    slot = str(obj.get("slot") or "")
                    if not slot or not is_session_slot(slot):
                        continue
                    credits = obj.get("credits")
                    if not isinstance(credits, (int, float)) or not math.isfinite(credits):
                        continue
                    # Normalize bare dashboard keys to the full session-key form.
                    key = spend_key_for_slot(slot)
                    cur = out.setdefault(key, {"credits": 0.0, "turns": 0.0})
                    cur["credits"] += float(credits)
                    cur["turns"] += 1
        except (OSError, UnicodeDecodeError):
            continue

    _SLOT_SPEND_CACHE, _SLOT_SPEND_CACHE_SIG = out, sig
    _SLOT_SPEND_CACHE_AT = now
    return out


# A payload backstop, not a top-N: the panel lists sessions for the user to
# browse, sort and group, so cutting it to the "hottest" few hid most of them
# behind a number they could not reach. Measured over a 7d window this is 260
# sessions across all categories, which ships comfortably; the cap only exists
# so an account with thousands does not send all of them on every poll.
_CONTEXT_TOP_SESSIONS = 500
# Fingerprint + TTL cache, same contract as _TOKEN_CACHE: the Telemetry panel
# polls every 5s, and the shards are append-only, so (name, mtime, size) over
# the window invalidates exactly when a turn lands.
# Rough characters-per-token for English prose, used ONLY to express the
# un-instrumented remainder (kiro-cli's base prompt + tool catalogue + steering)
# in the same unit as KiroCrew's own exactly-counted blocks. Never applied to
# those blocks themselves, and every surface that shows the derived number
# labels it as an estimate.
_EST_CHARS_PER_TOKEN = 4.0

_CONTEXT_CACHE: dict[str, Any] | None = None
_CONTEXT_CACHE_KEY: tuple[Any, ...] | None = None
_CONTEXT_CACHE_TS: float = 0.0
_CONTEXT_CACHE_TTL = 30.0

_COST_CACHE: dict[str, Any] | None = None
_COST_CACHE_KEY: tuple[Any, ...] | None = None
_COST_CACHE_TS: float = 0.0
_COST_CACHE_TTL = 30.0
# A payload backstop, not a display choice: the panel polls every few seconds,
# so an account with thousands of sessions should not ship all of them on every
# refetch. The shown/total pair travels with the list either way, so truncation
# is stated rather than implied.
_COST_TOP_CONVOS = 500
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
        "secretary",
    }
)

#: The one category that can be opened from the dashboard. Kept separate from
#: the category list because "is a session" and "has a route" are different
#: questions — a Telegram thread is a first-class session with nowhere for a
#: dashboard link to go, which is exactly the bug the old "titled -> link it"
#: rule shipped.
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


# Below this, a per-turn growth slope is noise rather than a trend.
_COST_MIN_GROWTH_TURNS = 6
# Occupancy at which the runtime compacts, so the projection has a real target.
_COMPACTION_PCT = 90.0
# A turn-to-turn occupancy fall this large is a compaction, not measurement
# jitter, and so ends the segment a growth slope may be fitted on. Occupancy
# does drift down by a fraction of a point between turns (what counts toward
# `context_used` varies), which is why the threshold is not simply "any fall".
_COMPACTION_DROP_PCT = 1.0


def context_occupancy(days: int = 14) -> dict[str, Any]:
    """Aggregate per-turn context-window occupancy from the token row store.

    ``persist_token_record`` writes ``context_used`` / ``context_window`` on
    every turn across all nine dispatch surfaces; this is their read side,
    surfacing the session-death early warning those fields exist for.

    Occupancy is a per-turn ratio, so it is aggregated here rather than in the
    OTEL pipeline: the useful question is "which SESSION is close to its
    window", and slot keys are unbounded-cardinality labels that do not belong
    on a metric. Returns the turn-level percentile spread plus the hottest
    sessions by peak occupancy, newest first on ties.

    Rows predating the field, or any row whose window is missing/zero, are
    skipped — an unknown window cannot yield a ratio, and defaulting one would
    invent a number.
    """
    per_session: dict[str, dict[str, Any]] = {}
    pcts: list[float] = []
    cutoff = time.time() - (days * 86400)

    shard_paths = _shards_in_window(days)
    try:
        cache_key: tuple[Any, ...] | None = (
            days,
            tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in shard_paths)),
        )
    except OSError:
        cache_key = None
    now = time.time()
    if (
        cache_key is not None
        and _CONTEXT_CACHE_KEY == cache_key
        and _CONTEXT_CACHE is not None
        and (now - _CONTEXT_CACHE_TS) < _CONTEXT_CACHE_TTL
    ):
        return _CONTEXT_CACHE

    for shard_path in shard_paths:
        try:
            with shard_path.open("rb") as fh:
                for line in bounded_records(fh, shard_path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    used = _coerce_int(obj.get("context_used"))
                    window = _coerce_int(obj.get("context_window"))
                    if used <= 0 or window <= 0:
                        continue
                    ts_raw = str(obj.get("ts") or "")
                    ts_epoch = _parse_row_ts(ts_raw)
                    if ts_epoch is None or ts_epoch < cutoff:
                        continue
                    slot = str(obj.get("slot") or "unknown")
                    # Before the percentile sample, not after: the spread and the
                    # session list have to describe the same population, or the
                    # p90 on the card disagrees with every row beneath it.
                    if not is_session_slot(slot):
                        continue
                    p = (used / window) * 100.0
                    pcts.append(p)
                    cur = per_session.get(slot)
                    if cur is None:
                        cur = {
                            "slot": slot,
                            "category": session_category(slot),
                            "turns": 0,
                            "peak_pct": 0.0,
                            "used": 0,
                            "window": window,
                            "agent": "",
                            "model": "",
                            "surface": "",
                            "ts": "",
                            "_ts_epoch": 0.0,
                        }
                        per_session[slot] = cur
                    cur["turns"] = int(cur["turns"]) + 1
                    if p > float(cur["peak_pct"]):
                        cur["peak_pct"] = p
                    # Identity + absolute numbers describe the LATEST turn, so a
                    # session that switched model or agent mid-life is reported
                    # as what it is now, not what it once was.
                    if ts_epoch >= float(cur["_ts_epoch"]):
                        cur.update(
                            {
                                "_ts_epoch": ts_epoch,
                                "ts": str(ts_raw),
                                "used": used,
                                "window": window,
                                "agent": str(obj.get("agent") or ""),
                                "model": str(obj.get("model") or ""),
                                "surface": _canonical_telemetry_surface(
                                    str(obj.get("surface") or "")
                                ),
                            }
                        )
        except (OSError, UnicodeDecodeError):
            continue

    def _store(result: dict[str, Any]) -> dict[str, Any]:
        global _CONTEXT_CACHE, _CONTEXT_CACHE_KEY, _CONTEXT_CACHE_TS
        if cache_key is not None:
            _CONTEXT_CACHE, _CONTEXT_CACHE_KEY, _CONTEXT_CACHE_TS = result, cache_key, now
        return result

    if not pcts:
        return _store({"turns": 0, "sessions": [], "window_days": days})

    pcts.sort()

    def _q(q: float) -> float:
        # Nearest-rank on the sorted samples: these are exact per-turn values,
        # not histogram buckets, so no interpolation is warranted.
        idx = min(len(pcts) - 1, max(0, math.ceil(q * len(pcts)) - 1))
        return round(pcts[idx], 1)

    sessions = sorted(
        per_session.values(),
        key=lambda s: (float(s["peak_pct"]), float(s["_ts_epoch"])),
        reverse=True,
    )[:_CONTEXT_TOP_SESSIONS]
    for s in sessions:
        s.pop("_ts_epoch", None)
        s["peak_pct"] = round(float(s["peak_pct"]), 1)

    return _store(
        {
            "turns": len(pcts),
            "p50_pct": _q(0.50),
            "p90_pct": _q(0.90),
            "max_pct": round(pcts[-1], 1),
            "sessions": sessions,
            "window_days": days,
        }
    )


#: Numeric per-turn fields copied from a shard row into a usage-turns row, in
#: the order the API returns them. One owner: the reader below and its tests
#: both consume this tuple, so a field added to the recorder shows up in the
#: API by being added HERE, not by editing two lists that can drift.
TURN_USAGE_FIELDS: tuple[str, ...] = (
    "input",
    "output",
    "cache_create",
    "cache_read",
    "credits",
    "cost",
    "duration_ms",
    "context_used",
    "context_window",
)


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


def slot_turn_usage(
    slot: str, days: int = SPEND_WINDOW_DAYS, *, app: str | None = None
) -> list[dict[str, Any]]:
    """Per-turn usage rows for ONE session, oldest first.

    The per-turn drill-down under :func:`slot_spend`'s aggregate: each row is
    one turn's shard record — model, token counts, credits, duration, and the
    context meter — for the slot named. Built for the app-facing usage API: an
    app that runs agent sessions (via app-owned slots) can account for what its
    own turns cost without reaching into the shard files, whose location and
    row shape are this module's private contract.

    Same walk and the same reasons as :func:`context_trace`: per-session
    per-turn detail stays OUT of the OTEL pipeline because slot keys are
    unbounded-cardinality labels. Malformed lines and foreign slots are
    skipped, not errors — a shard is an append-only log written by a live
    gateway, and a torn tail line is expected during a write.

    ``app`` is the ownership filter for app callers: only rows STAMPED with
    that app at write time are returned. Ownership recorded on the row is what
    survives the slot — a live-slot check both leaks on slot-name reuse (a
    recreated slot vouches for the previous owner's rows) and denies an app
    its own completed sessions. Rows written before the stamp existed carry no
    ``app`` and are invisible to app callers: deny-by-default for ownership
    that was never recorded. ``None`` (the dashboard) reads everything.

    The window is enforced per ROW, not only per shard file: the oldest shard
    in the window covers a whole day, so without a row-level cutoff a request
    for N days returns rows up to a day older than asked — wrong in the one
    place this API exists for, accounting.
    """
    turns: list[dict[str, Any]] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    for shard_path in _shards_in_window(days):
        try:
            with shard_path.open("rb") as fh:
                for line in bounded_records(fh, shard_path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    if str(obj.get("slot") or "") != slot:
                        continue
                    if app is not None and str(obj.get("app") or "") != app:
                        continue
                    ts = _parse_row_ts(str(obj.get("ts") or ""))
                    if ts is None or ts < cutoff:
                        # An unparseable timestamp cannot prove it is inside the
                        # window; accounting excludes what it cannot date.
                        continue
                    row: dict[str, Any] = {
                        "ts": str(obj.get("ts") or ""),
                        "model": str(obj.get("model") or ""),
                    }
                    for field in TURN_USAGE_FIELDS:
                        value = _usage_number(obj.get(field))
                        if value is not None:
                            row[field] = value
                    turns.append(row)
        except (OSError, UnicodeDecodeError):
            continue
    return turns


def context_trace(slot: str, days: int = 14) -> dict[str, Any]:
    """Per-turn injection breakdown for one session, newest shard last.

    Reads the ``ctx_blocks`` / ``phase`` fields ``persist_token_record`` writes
    each turn and returns them in chronological order, plus per-block totals.
    Each turn also carries the row's ``credits`` and ``duration_ms`` when the
    shard recorded usable numbers: injection and billing live on the same row,
    so the drill-down answers "what was injected and what it cost" in one read.

    Kept out of the OTEL pipeline for the same reason as
    :func:`context_occupancy`: this is per-session, per-turn detail, and slot
    keys are unbounded-cardinality labels that must not become metric labels.
    The bounded half (block label -> size, aggregated) is what belongs on a
    metric; this is the drill-down.

    Rows written before this field existed simply carry no ``ctx_blocks`` and
    are skipped, so the trace starts where the recording does rather than
    inventing zeros for history.

    ``estimated_other_chars`` is the remainder of the model's context that
    KiroCrew did NOT inject — kiro-cli's own base prompt, its tool catalogue and
    its steering files. It is an ESTIMATE and labelled as one everywhere it is
    surfaced: the provider reports occupancy in tokens while every KiroCrew
    block here is counted in exact characters, so the two can only be compared
    through :data:`_EST_CHARS_PER_TOKEN`. Zero when occupancy is unknown or the
    subtraction would go negative.
    """
    turns: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    for shard_path in _shards_in_window(days):
        try:
            with shard_path.open("rb") as fh:
                for line in bounded_records(fh, shard_path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    if str(obj.get("slot") or "") != slot:
                        continue
                    raw = obj.get("ctx_blocks")
                    if not isinstance(raw, dict) or not raw:
                        continue
                    blocks = {str(k): _coerce_int(v) for k, v in raw.items() if _coerce_int(v) > 0}
                    if not blocks:
                        continue
                    for label, size in blocks.items():
                        totals[label] = totals.get(label, 0) + size
                    turn_row: dict[str, Any] = {
                        "ts": str(obj.get("ts") or ""),
                        "phase": str(obj.get("phase") or ""),
                        "blocks": blocks,
                        "total_chars": sum(blocks.values()),
                        "context_used": _coerce_int(obj.get("context_used")),
                        "context_window": _coerce_int(obj.get("context_window")),
                        "model": str(obj.get("model") or ""),
                    }
                    # The same shard row also carries the turn's billing; the
                    # trace returns it rather than making the panel walk the
                    # shards a second time through the usage-turns reader and
                    # re-join what was never apart.
                    for field in ("credits", "duration_ms"):
                        value = _usage_number(obj.get(field))
                        if value is not None:
                            turn_row[field] = value
                    turns.append(turn_row)
        except (OSError, UnicodeDecodeError):
            continue

    turns.sort(key=lambda t: str(t["ts"]))
    injected = sum(totals.values())
    # Occupancy is per-turn cumulative, so the largest reading in the session is
    # the closest thing to "how full did this window get".
    peak_used = max((int(t["context_used"]) for t in turns), default=0)
    estimated_other = 0
    if peak_used > 0:
        estimated_other = max(0, int(peak_used * _EST_CHARS_PER_TOKEN) - injected)
    return {
        "slot": slot,
        "turns": turns,
        "totals": totals,
        "injected_chars": injected,
        "user_chars": totals.get(USER_LABEL, 0),
        "estimated_other_chars": estimated_other,
        "peak_context_used": peak_used,
        "context_window": next(
            (int(t["context_window"]) for t in reversed(turns) if t["context_window"]), 0
        ),
        "window_days": days,
    }


def cost_breakdown(days: int = SPEND_WINDOW_DAYS) -> dict[str, Any]:
    """Aggregate per-turn spend from the token row store into a cost view.

    Answers "what did the last *days* cost, compared with the *days* before it,
    and what drove the difference". ``credits`` is the only billing dimension the
    acp provider populates -- input/output/cache/cost come back hard zero -- so
    it is the unit here, with ``context_used`` as the second real signal.

    Three deliberate shapes:

    * **Every** model is reported, never a top-N slice. A truncated list hides a
      model switch, which on real data moved the bill far more than usage volume
      did, and a single-user store is small enough to render complete.
    * ``channel`` is derived from the session key, NOT read from the row's
      ``surface`` field. Historical rows can carry a wrong but non-empty value
      such as ``dashboard`` for a Telegram turn, and the token-row schema has no
      version or writer marker that distinguishes them from trustworthy rows.
      The key therefore remains authoritative until the store gains such a
      boundary; a surface-first fallback would silently misattribute history.
    * Context bands are absolute token counts rather than occupancy ratios:
      spend tracks how many tokens get re-sent, and window sizes differ per
      model, so a ratio would average incomparable populations.
    """
    now = time.time()
    cutoff = now - (days * 86400)
    prior_cutoff = now - (2 * days * 86400)

    shard_paths = _shards_in_window(2 * days)
    try:
        cache_key: tuple[Any, ...] | None = (
            days,
            tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in shard_paths)),
        )
    except OSError:
        cache_key = None
    if (
        cache_key is not None
        and _COST_CACHE_KEY == cache_key
        and _COST_CACHE is not None
        and (now - _COST_CACHE_TS) < _COST_CACHE_TTL
    ):
        return _COST_CACHE

    def _blank() -> dict[str, Any]:
        return {"credits": 0.0, "turns": 0}

    cur_tot, prev_tot = _blank(), _blank()
    by_model: dict[str, dict[str, Any]] = {}
    by_channel: dict[str, dict[str, Any]] = {}
    prev_model: dict[str, float] = {}
    prev_channel: dict[str, float] = {}
    bands: dict[int, list[float]] = {}
    convos: dict[str, dict[str, Any]] = {}
    by_category: dict[str, dict[str, Any]] = {}
    prev_category: dict[str, float] = {}
    priciest: dict[str, Any] = {"credits": 0.0, "slot": "", "ts": ""}

    for shard_path in shard_paths:
        try:
            with shard_path.open("rb") as fh:
                for line in bounded_records(fh, shard_path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    ts_raw = str(obj.get("ts") or "")
                    ts_epoch = _parse_row_ts(ts_raw)
                    if ts_epoch is None or ts_epoch < prior_cutoff:
                        continue

                    credits = float(obj.get("credits") or 0.0)
                    # Shards written before the persist-side guard can already
                    # hold `NaN` / `Infinity` — `json.loads` accepts those bare
                    # tokens even though they are not valid JSON. Letting one
                    # through would poison every total it touches and make
                    # `web.json_response` emit a body no browser can parse, so
                    # the row is dropped rather than counted as free.
                    if not math.isfinite(credits):
                        continue
                    model = str(obj.get("model") or "unknown")
                    slot = str(obj.get("slot") or "")

                    # Dropped here, before ANY accumulator sees it, so every
                    # figure on the page is computed over one population. A
                    # subagent is a fragment of another session's turn rather
                    # than a session, and it carries no field pointing back at
                    # the session that spawned it, so it can be neither listed
                    # nor attributed. Filtering at the grouping step instead
                    # would leave it in the totals and force a reconciling
                    # footnote for money with no row.
                    if slot and not is_session_slot(slot):
                        continue

                    if ts_epoch < cutoff:
                        prev_tot["credits"] += credits
                        prev_tot["turns"] = int(prev_tot["turns"]) + 1
                        prev_model[model] = prev_model.get(model, 0.0) + credits
                        ch = telemetry_channel_of(slot or None)
                        prev_channel[ch] = prev_channel.get(ch, 0.0) + credits
                        cat = session_category(slot)
                        prev_category[cat] = prev_category.get(cat, 0.0) + credits
                        continue

                    cur_tot["credits"] += credits
                    cur_tot["turns"] = int(cur_tot["turns"]) + 1
                    if credits > float(priciest["credits"]):
                        priciest = {"credits": credits, "slot": slot, "ts": ts_raw}

                    for bucket, name in (
                        (by_model, model),
                        (by_channel, telemetry_channel_of(slot or None)),
                        (by_category, session_category(slot)),
                    ):
                        e = bucket.setdefault(name, {"name": name, "credits": 0.0, "turns": 0})
                        e["credits"] = float(e["credits"]) + credits
                        e["turns"] = int(e["turns"]) + 1

                    used = _coerce_int(obj.get("context_used"))
                    if used > 0 and credits > 0:
                        bands.setdefault(min(used // 200_000, 5), []).append(credits)

                    if slot:
                        c = convos.setdefault(
                            slot,
                            {
                                "slot": slot,
                                "credits": 0.0,
                                "turns": 0,
                                "peak_pct": 0.0,
                                "first_ts": ts_epoch,
                                "last_ts": ts_epoch,
                                "_occ": [],
                            },
                        )
                        c["credits"] = float(c["credits"]) + credits
                        c["turns"] = int(c["turns"]) + 1
                        c["first_ts"] = min(float(c["first_ts"]), ts_epoch)
                        c["last_ts"] = max(float(c["last_ts"]), ts_epoch)
                        window = _coerce_int(obj.get("context_window"))
                        if used > 0 and window > 0:
                            pct = used / window * 100.0
                            c["peak_pct"] = max(float(c["peak_pct"]), pct)
                            c["_occ"].append((ts_epoch, pct))
        except (OSError, UnicodeDecodeError):
            continue

    def _store(result: dict[str, Any]) -> dict[str, Any]:
        global _COST_CACHE, _COST_CACHE_KEY, _COST_CACHE_TS
        if cache_key is not None:
            _COST_CACHE, _COST_CACHE_KEY, _COST_CACHE_TS = result, cache_key, now
        return result

    def _per_turn(e: dict[str, Any]) -> float:
        t = int(e["turns"])
        return round(float(e["credits"]) / t, 1) if t else 0.0

    total = float(cur_tot["credits"])

    def _rank(
        bucket: dict[str, dict[str, Any]], deltas: dict[str, float] | None
    ) -> list[dict[str, Any]]:
        out = []
        for e in sorted(bucket.values(), key=lambda x: -float(x["credits"])):
            row = {
                "name": e["name"],
                "credits": round(float(e["credits"]), 1),
                "turns": int(e["turns"]),
                "per_turn": _per_turn(e),
                "share_pct": round(float(e["credits"]) / total * 100, 1) if total else 0.0,
            }
            if deltas is not None:
                was = deltas.get(str(e["name"]), 0.0)
                # A model with no prior spend has no percentage to report; the
                # frontend renders that as "new" rather than a fake infinity.
                row["delta_pct"] = (
                    round((float(e["credits"]) - was) / was * 100, 0) if was > 0 else None
                )
            out.append(row)
        return out

    band_rows = []
    for idx in sorted(bands):
        vals = bands[idx]
        lo = idx * 200_000
        band_rows.append(
            {
                "label": f"{lo // 1000}k–{(lo + 200_000) // 1000}k" if idx < 5 else "1M+",
                "turns": len(vals),
                "mean_credits": round(sum(vals) / len(vals), 1),
            }
        )

    convo_rows = []
    for c in sorted(convos.values(), key=lambda x: -float(x["credits"]))[:_COST_TOP_CONVOS]:
        occ = sorted(c.pop("_occ"))
        growth: float | None = None
        to_90: int | None = None
        # Fit the slope only on the stretch since the last compaction, never
        # across the whole window. Occupancy is sawtooth, not monotonic: a
        # compaction drops it back to near-empty, and 7 of the 8 top-spending
        # conversations measured carry between one and four such drops. A secant
        # from the first turn to the last therefore spans discontinuities and is
        # dragged down by every reset, which understates the live growth rate by
        # ~47x on real data (one 104-turn conversation projected 795 turns of
        # headroom while its current segment was climbing at 4%/turn, i.e. ~17
        # turns from compaction). Erring toward "plenty of room left" is the
        # damaging direction for a figure whose whole purpose is a warning.
        seg = occ
        for i in range(1, len(occ)):
            if occ[i][1] < occ[i - 1][1] - _COMPACTION_DROP_PCT:
                seg = occ[i:]
        # A slope needs enough points in the CURRENT segment, not merely enough
        # turns in the window: a long conversation freshly past a compaction
        # knows nothing about its new trajectory yet, and withholding is the
        # honest answer rather than projecting from two or three points.
        if len(seg) >= _COST_MIN_GROWTH_TURNS:
            rate = (seg[-1][1] - seg[0][1]) / (len(seg) - 1)
            if rate > 0:
                growth = round(rate, 2)
                remaining = _COMPACTION_PCT - seg[-1][1]
                to_90 = math.ceil(remaining / rate) if remaining > 0 else 0
        convo_rows.append(
            {
                "slot": c["slot"],
                # The session taxonomy the panel groups by: `bg` for machine
                # work, otherwise the transport it came from. The frontend needs
                # it for two things — the category chip, and linkability, since
                # only a dashboard session has a route to open. Deriving either
                # from the key shape in the frontend would put a second,
                # drifting copy of session-key knowledge there.
                "category": session_category(c["slot"]),
                # The unollapsed label underneath the category, so a `bg` row
                # still says whether it was a subagent, a cron or the heartbeat
                # instead of becoming an anonymous lump.
                "channel": telemetry_channel_of(c["slot"]),
                "credits": round(float(c["credits"]), 1),
                "turns": int(c["turns"]),
                "peak_pct": round(float(c["peak_pct"]), 1),
                "span_days": round((float(c["last_ts"]) - float(c["first_ts"])) / 86400, 1),
                # A closed conversation has no stored title, so the frontend
                # labels it by start date -- eight rows all reading "untitled"
                # cannot be told apart, which defeats the ranking.
                "first_ts": round(float(c["first_ts"]), 3),
                "growth_pct_per_turn": growth,
                "turns_to_compaction": to_90,
            }
        )

    prev_credits = float(prev_tot["credits"])
    return _store(
        {
            "window_days": days,
            "credits": round(total, 1),
            "turns": int(cur_tot["turns"]),
            "per_turn": _per_turn(cur_tot),
            "prior_credits": round(prev_credits, 1),
            "prior_turns": int(prev_tot["turns"]),
            "prior_per_turn": _per_turn(prev_tot),
            "delta_pct": (
                round((total - prev_credits) / prev_credits * 100, 0) if prev_credits > 0 else None
            ),
            "priciest": {
                "credits": round(float(priciest["credits"]), 1),
                "slot": priciest["slot"],
                "ts": priciest["ts"],
            },
            "by_model": _rank(by_model, prev_model),
            "by_channel": _rank(by_channel, prev_channel),
            "by_category": _rank(by_category, prev_category),
            "context_bands": band_rows,
            "conversations": convo_rows,
            "conversation_count": len(convos),
            # The one category with a route. Sent rather than duplicated in
            # the frontend so a change here cannot leave a dead link there.
            "navigable_category": NAVIGABLE_CATEGORY,
        }
    )


def read_context_tokens(source: object) -> tuple[int, int]:
    """Return ``(context_used, context_window)`` from a provider/client.

    Reads the provider's public ``context_used_tokens()`` /
    ``context_window_tokens()`` accessors (declared on ``providers/base.py`` and
    implemented for ACP in ``providers/acp.py`` and ``acp/session_provider.py``).
    Guarded with ``getattr`` so non-ACP providers and test doubles that lack the
    accessors — or an accessor that raises — yield ``(0, 0)`` rather than
    propagating. Never raises: this is best-effort analytics on the turn hot
    path, and a measurement helper must never break the turn it measures.
    """
    try:
        used_fn = getattr(source, "context_used_tokens", None)
        window_fn = getattr(source, "context_window_tokens", None)
        if not callable(used_fn) or not callable(window_fn):
            return (0, 0)
        return (int(used_fn()), int(window_fn()))
    except Exception:
        return (0, 0)


def _wrapper_chain(source: object) -> list[object]:
    """Collect *source* and the provider/client/handle wrappers nested under it.

    ``providers/acp.py`` keeps its inner object on ``_client`` (also exposed as
    ``client``), ``acp/session_provider.py`` keeps the session handle on
    ``_handle``, and both the handle and the provider keep the spawned CLI
    runtime on ``_runtime`` (``session_handle.py:215``, ``session_provider.py:61``)
    — and these nest, because ``providers/acp.py:610`` assigns an
    ``AcpSessionProvider`` to ``_client`` (the ``-> AcpClient`` annotation there
    carries a ``type: ignore``). A default Kiro turn therefore hides its resolved
    state two levels down, at ``provider.client._handle``, so probing a fixed
    depth misses it. Breadth-first with a node cap and an identity-based visited
    set, so a wrapper that points back at itself terminates.

    ``_runtime`` is traversed **last** deliberately. It is the only holder of
    ``_agent`` for the session-provider shape (``runtime.py:273``), but it also
    carries the process-level ``--model`` argument (``runtime.py:280``); visiting
    it after ``_handle`` keeps session-level model state ahead of process-level
    state when :func:`read_effective_model` falls through to ``_model``.
    """
    chain: list[object] = []
    pending: list[object] = [source]
    while pending and len(chain) < 8:
        node = pending.pop(0)
        if node is None or any(seen is node for seen in chain):
            continue
        chain.append(node)
        for holder in ("client", "_client", "_handle", "_runtime"):
            inner = getattr(node, holder, None)
            if inner is not None and not isinstance(inner, (str, bytes, int)):
                pending.append(inner)
    return chain


def read_effective_agent(source: object) -> str:
    """Return the agent id that actually served the turn, or ``""``.

    The slot's ``agent`` is an alias that ``resolve_agent_bindings()`` maps to a
    concrete kiro agent before dispatch (``chat_runner.py:2392`` resolves it and
    :2408 passes the result into ``get_or_create``), so a slot set to ``default``
    can be served by ``kirocrew``. Recording the alias would attribute the turn
    to an agent that never ran. ``AcpClient`` stores what it was constructed with
    on ``_agent`` (``acp/client.py:1270``), which is that resolved value — the
    same "what actually ran" precedence used for the model. Never raises.
    """
    try:
        for node in _wrapper_chain(source):
            candidate = getattr(node, "_agent", "")
            if isinstance(candidate, str) and candidate:
                return candidate
    except Exception:
        pass
    return ""


def read_effective_model(source: object) -> str:
    """Return the model id the provider actually resolved for the turn, or ``""``.

    Attribution only — deliberately NOT the same as
    ``chat_runner._backfill_canonical_model``. That helper canonicalizes and
    DROPS Bedrock profile-form ids for non-``claude_code`` providers, because its
    result is written back into ``slot.model`` and a profile id there pins the
    slot to one profile+region across resumes. Nothing here is written back, so
    the raw resolved id is what we want: for cost attribution, the profile that
    actually served the turn is strictly more informative than the alias the
    caller asked for.

    Walks the wrapper chain via :func:`_wrapper_chain` rather than a fixed depth,
    because a default Kiro turn hides the resolved id two levels down.

    Two passes over the collected chain, because a resolved id **anywhere** beats
    a plain ``_model`` anywhere: ``_resolved_model_id`` is the id the backend
    actually settled on (the same precedence ``AcpClient`` uses internally,
    ``self._resolved_model_id or self._model``), while ``_model`` may still hold
    the ``"auto"`` sentinel or a pre-resolution request. Skips ``"auto"``. Never
    raises.
    """
    try:
        chain = _wrapper_chain(source)
        for attr in ("_resolved_model_id", "_model"):
            for node in chain:
                candidate = getattr(node, attr, "")
                if isinstance(candidate, str) and candidate and candidate.strip().lower() != "auto":
                    return candidate
    except Exception:
        pass
    return ""


def _source_requests_auto(source: object) -> bool:
    """Whether the provider chain still reports Auto as its model request.

    This is deliberately separate from :func:`read_effective_model`: ``auto``
    is not a resolved model id, but it is useful attribution when a completed
    turn has no more specific id from the backend. A blank request does not
    prove Auto was selected, so it remains blank in the row store.
    """
    try:
        return any(
            isinstance(candidate := getattr(node, "_model", ""), str)
            and candidate.strip().lower() == "auto"
            for node in _wrapper_chain(source)
        )
    except Exception:
        return False


def read_turn_model(source: object) -> str:
    """Return what served the turn: a resolved id, ``"auto"``, or ``""``.

    :func:`read_effective_model` alone collapses two different situations into
    ``""``: a turn whose model the provider never reported, and a turn the user
    deliberately handed to Auto. They read identically to a consumer, so a
    display surface that omits a blank model shows nothing for an Auto turn and
    an absent model becomes indistinguishable from a missing measurement.

    ``"auto"`` is not a model id and is never presented as one — it is the
    honest answer to "who chose", which is all the backend discloses when
    Auto routes a turn (the ACP per-turn metadata frame carries context and
    metering only). Callers that need a concrete id for a pricing or window
    lookup must keep using :func:`read_effective_model`. Never raises.
    """
    resolved = read_effective_model(source)
    if resolved:
        return resolved
    return "auto" if _source_requests_auto(source) else ""


def _resolve_model(model: str, model_source: object) -> str:
    """Resolve the model to record, retaining a known Auto selection.

    A concrete resolved id remains the preferred accounting dimension. When a
    completed Auto turn exposes no concrete id, ``"auto"`` still distinguishes
    that deliberate backend choice from an unavailable model source. A blank
    caller value with no Auto request remains blank because it carries no model
    information at all.
    """
    requested = (model or "").strip().lower()
    if requested not in ("", "auto"):
        return model
    resolved = read_turn_model(model_source) if model_source is not None else ""
    if resolved:
        return resolved
    # An explicit Auto request stands on its own: the caller named it, so it is
    # recorded even when the provider chain no longer reports it.
    return "auto" if requested == "auto" else ""


def _coerce_int(value: Any) -> int:
    """Coerce ``value`` to ``int``, defaulting to 0 for non-numeric input.

    Keeps the emitted record JSON-serializable even if a caller passes a
    non-numeric context value.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build_token_record(
    slot_key: str,
    model: str,
    event: object,
    provider: str,
    now: datetime,
    *,
    surface: str = "",
    agent: str = "",
    context_used: int = 0,
    context_window: int = 0,
    elapsed_ms: int = 0,
    ctx_blocks: dict[str, int] | None = None,
    phase: str = "",
    app: str = "",
) -> dict[str, Any]:
    """Build the JSONL token-usage record dict (no I/O).

    ``app`` stamps the OWNING app on the row when the turn ran on an app-owned
    slot. Ownership recorded at write time is what survives the slot: a live
    ``_app`` check at read time both leaks on slot-name reuse (a recreated slot
    would vouch for the previous owner's rows) and denies an app its own
    completed sessions. Empty for dashboard/user slots, and absent from rows
    written before the field existed — an app caller sees neither, which is
    the deny-by-default answer for ownership that was never recorded.

    ``surface`` tags the dispatch origin (``dashboard``, ``cron``, ``subagent``,
    …) and ``agent`` the agent id resolved for the turn. ``context_used`` /
    ``context_window`` record context-window occupancy (read from the provider
    via :func:`read_context_tokens` at the call site). All four are additive and
    default to empty/0, so pre-existing shards and existing callers stay valid.

    ``elapsed_ms`` is the caller's locally measured wall clock for the turn and
    is the FALLBACK for ``duration_ms``: the provider-reported value wins when
    non-zero, otherwise the local measurement is recorded. Both are needed
    because the acp provider always reports ``TurnUsage.duration_ms == 0``
    (nothing assigns it), so a provider-only read would record a literal 0 for
    every real turn and leave the row store unable to answer "how long did this
    turn take".

    This mirrors the precedence the OTEL emit path already uses
    (``chat_runner._attach_turn_stats``: ``value = duration_ms or elapsed_ms``),
    so the histogram and the row store cannot disagree about one turn.
    """
    # Usage lives on event.usage (TurnUsage). Fall back to the event itself when
    # it isn't a real TurnUsage (legacy / non-AcpEvent producers, test doubles).
    # credits is float-coerced so a non-numeric value can't break json.dumps;
    # non-finite floats are caught once for every field in _write_token_record.
    _u = getattr(event, "usage", None)
    u = _u if isinstance(_u, TurnUsage) else event
    try:
        credits = float(getattr(u, "credits", 0.0))
    except (TypeError, ValueError):
        credits = 0.0
    # Turn stop reason, read off the EVENT (AcpEvent carries it; a bare
    # TurnUsage from provider_last_turn_usage does not — recorded as "").
    # Free-form per-row (this store has no cardinality limit, unlike OTel
    # attrs), so watchdog outcomes (STOP_REASON_TOOL_STALL / _STALE_RECOVER)
    # can be joined against the row's ``agent`` field retroactively — this is
    # where per-agent stall analysis happens, deliberately NOT on metric attrs.
    _stop = getattr(event, "stop_reason", "")
    return {
        "_type": "tokens",
        "ts": now.isoformat(),
        "slot": slot_key,
        "app": app,
        "provider": provider or "",
        "model": model or "",
        "input": getattr(u, "input_tokens", 0),
        "output": getattr(u, "output_tokens", 0),
        "cache_create": getattr(u, "cache_creation_tokens", 0),
        "cache_read": getattr(u, "cache_read_tokens", 0),
        "cost": getattr(u, "cost_usd", 0.0),
        "credits": credits,
        "turns": getattr(u, "num_turns", 0),
        "duration_ms": getattr(u, "duration_ms", 0) or _coerce_int(elapsed_ms),
        # Additive per-turn fields: context occupancy + dispatch origin. Old
        # shards lack these keys; readers must tolerate their absence.
        # context_* are int-coerced so a bad value can't break json.dumps.
        "surface": surface or "",
        "agent": agent or "",
        "context_used": _coerce_int(context_used),
        "context_window": _coerce_int(context_window),
        # Per-turn injection breakdown: block label -> characters, from
        # kiro_crew.context_blocks.split_blocks. Sizes are characters (exact and
        # tokenizer-independent). ``phase`` separates the one-off session-start
        # injection from the much smaller per-turn one so a reader never pools
        # the two populations into one meaningless percentile.
        "ctx_blocks": {
            str(k): _coerce_int(v) for k, v in (ctx_blocks or {}).items() if _coerce_int(v) > 0
        },
        "phase": phase or "",
        # Additive: the turn's terminal stop reason ("" when the producer has
        # none). str-coerced so a non-string on a test double / legacy event
        # can't break json.dumps.
        "stop_reason": _stop if isinstance(_stop, str) else "",
    }


def _finite_only(record: dict[str, Any]) -> dict[str, Any]:
    """Replace non-finite floats with 0.0 so the row stays valid JSON.

    ``json.dumps`` writes NaN and the infinities as the bare tokens ``NaN`` /
    ``Infinity`` / ``-Infinity``. Those are not valid JSON, but ``json.loads``
    accepts them, so a single such row travels silently from the store into a
    ``web.json_response`` body that no browser will parse — taking every panel
    reading the row store down rather than losing one turn's numbers. Provider
    floats (``cost_usd``, ``credits``) reach the record unvalidated, and a float
    is exactly what a bad one looks like, so the check cannot be a type check.
    """
    return {
        k: (0.0 if isinstance(v, float) and not math.isfinite(v) else v) for k, v in record.items()
    }


def _write_token_record(record: dict[str, Any], now: datetime) -> None:
    """Append a prebuilt token record to today's shard (blocking I/O)."""
    shard_path = _shard_path_for(now)
    parent = shard_path.parent
    # mkdir only when missing — the dir is created once per day, not per turn.
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # allow_nan=False turns a non-finite value into a raised ValueError instead
    # of an invalid-JSON row. Sanitizing only on that failure keeps the hot path
    # free of a per-turn scan of every field.
    try:
        line = json.dumps(record, allow_nan=False)
    except ValueError:
        # Name the fields before sanitizing. Without this the fallback rewrites
        # a provider's measurement to 0.0 and the turn is persisted as free,
        # with nothing on record that it happened. Field NAMES only: the values
        # are the corrupt measurement and the rest of the record is per-turn
        # telemetry, neither of which belongs in a log line.
        #
        # Re-raise when nothing non-finite is present: allow_nan=False is not
        # the only way json.dumps raises ValueError, and sanitizing cannot fix
        # the others. Swallowing them here would emit a warning naming no field
        # and blame corruption that did not occur.
        non_finite = sorted(
            k for k, v in record.items() if isinstance(v, float) and not math.isfinite(v)
        )
        if not non_finite:
            raise
        logger.warning(
            "usage row: replaced non-finite value(s) with 0.0 before persisting; " "fields=%s",
            ",".join(non_finite),
        )
        line = json.dumps(_finite_only(record), allow_nan=False)
    with open(shard_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def persist_token_record(
    slot_key: str,
    model: str,
    event: object,
    provider: str = "",
    *,
    surface: str = "",
    agent: str = "",
    context_used: int = 0,
    context_window: int = 0,
    elapsed_ms: int = 0,
    ctx_blocks: dict[str, int] | None = None,
    phase: str = "",
    app: str = "",
    model_source: object = None,
) -> None:
    """Append a token usage record to today's shard under
    ``<data home>/usage/tokens/YYYY-MM-DD.jsonl`` (synchronous).

    Has NO caller today, and unlike :func:`persist_token_record_async` it does
    not emit ``kirocrew.turn.duration``. A future direct caller of this variant
    would therefore write a usage row for a turn that appears in no latency or
    fault-rate reading — silently reopening the gap the async variant's emit
    closed. If you reach for this for a real per-turn path, emit there too (or
    call the async variant).

    The ``provider`` field tags the source LLM backend (acp,
    claude_code, bedrock) so the dashboard chart can filter by provider.
    ``surface`` / ``agent`` tag the dispatch origin and resolved agent, and
    ``context_used`` / ``context_window`` record context-window occupancy — all
    additive and defaulted so existing callers stay valid.

    ``elapsed_ms`` is the caller's locally measured turn wall clock, used only
    when the provider reports no duration. Every dispatch surface owns its own
    measurement because there is no global turn boundary to hang one clock on.

    ``model_source`` is a provider/client used ONLY to fill ``model`` when the
    caller could not resolve one (several dispatch surfaces never pick a model
    explicitly and would otherwise record an empty string, losing the
    per-model attribution dimension). An explicit ``model`` always wins.

    On the async chat hot path, prefer ``persist_token_record_async`` which
    offloads the blocking write off the event loop.
    """
    try:
        model = _resolve_model(model, model_source)
        now = datetime.now().astimezone()
        _write_token_record(
            _build_token_record(
                slot_key,
                model,
                event,
                provider,
                now,
                surface=surface,
                agent=agent,
                context_used=context_used,
                context_window=context_window,
                elapsed_ms=elapsed_ms,
                ctx_blocks=ctx_blocks,
                phase=phase,
                app=app,
            ),
            now,
        )
    except Exception:
        logger.debug("Failed to persist token record for slot %s", slot_key, exc_info=True)


async def persist_token_record_async(
    slot_key: str,
    model: str,
    event: object,
    provider: str = "",
    *,
    surface: str = "",
    agent: str = "",
    context_used: int = 0,
    context_window: int = 0,
    elapsed_ms: int = 0,
    ctx_blocks: dict[str, int] | None = None,
    phase: str = "",
    app: str = "",
    model_source: object = None,
    emit_metric: bool = True,
) -> None:
    """Async variant: builds the record on-loop, offloads the file write.

    Called per agent turn (EVENT_COMPLETE) from the chat runner, which runs on
    the aiohttp event loop, where a synchronous open/append would block all
    co-resident coroutines for the IO window. These are best-effort analytics
    (no fsync, exceptions swallowed), so off-loop write loses no durability.
    See :func:`persist_token_record` for the ``surface`` / ``agent`` /
    ``context_used`` / ``context_window`` / ``elapsed_ms`` / ``model_source``
    fields.

    ALSO emits the per-turn OTEL family — ``kirocrew.turn.duration`` plus the
    turn's usage (``kirocrew.turn.tokens`` and whichever of
    ``kirocrew.turn.credits`` / ``kirocrew.turn.cost_usd`` the backend billed in)
    — and this is the only place that does. Being the one call every dispatch
    surface already makes once per turn is exactly why: the emit used to live in
    ``chat_runner`` beside the dashboard turn loop, so cron, heartbeat, memory
    consolidation, subagents, task-runner steps, workflow stages and every
    messaging channel were absent from turn latency and fault rate entirely — and
    absent does not read as absent, it reads as healthy. See
    :mod:`kiro_crew.metrics.turns`.

    Every sample reuses the BUILT record's own fields — ``duration_ms``, the
    token counts, ``credits``/``cost``, and the RESOLVED ``model``/``provider`` —
    so the row store and the metrics cannot disagree about one turn, the property
    the record builder's docstring already claims and now enforces by
    construction rather than by two call sites being handed the same variables.

    ``emit_metric=False`` says the CALLER owns this turn's samples. The
    dashboard passes it because its persist call sits behind a
    ``usage_has_billing`` gate: a turn that timed out having billed nothing
    writes no row, and letting the row's absence swallow the samples would drop
    exactly the faults ``fault_rate`` exists to count. It emits unconditionally
    itself instead, which is also how its ``stall_exhausted`` refinement reaches
    the histogram. Do NOT set this without emitting — the turn then goes
    unrecorded, which reads as a healthy gap rather than a missing one.
    """
    try:
        model = _resolve_model(model, model_source)
        now = datetime.now().astimezone()
        record = _build_token_record(
            slot_key,
            model,
            event,
            provider,
            now,
            surface=surface,
            agent=agent,
            context_used=context_used,
            context_window=context_window,
            elapsed_ms=elapsed_ms,
            ctx_blocks=ctx_blocks,
            phase=phase,
            app=app,
        )
        # Before the offloaded write: a file-write failure must not cost the
        # latency sample, which needs nothing from disk.
        if emit_metric:
            _emit_turn_histogram(record, slot_key, event)
        await asyncio.to_thread(_write_token_record, record, now)
    except Exception:
        logger.debug("Failed to persist token record for slot %s", slot_key, exc_info=True)


def _emit_turn_histogram(
    record: dict,
    slot_key: str,
    event: object,
) -> None:
    """Emit this turn's OTEL samples from a built record.

    ``kirocrew.turn.duration`` plus the turn's usage family
    (``kirocrew.turn.tokens`` and whichever of ``kirocrew.turn.credits`` /
    ``kirocrew.turn.cost_usd`` the backend billed in). Split out so the emit is
    one statement in the persist path and can be asserted directly by a test
    without writing a row.

    Reads every value off the RECORD rather than recomputing from
    ``event``/``elapsed_ms``: the builder already applies the
    provider-reported-wins-else-wall-clock rule for the duration and already
    coerces the usage fields, so duplicating either here is how the row store and
    the metrics would come to disagree about one turn. ``model`` and ``provider``
    come from the same place for the same reason — the record's ``model`` is the
    RESOLVED one (``_resolve_model``), so the attribute names the model that ran
    rather than the alias the caller asked for.

    An ABSENT ``stop_reason`` attribute and a ``None``/empty one mean different
    things and must not collapse. A bare ``TurnUsage`` has no such attribute and
    cannot say how its turn ended (``unclassified``); an ``LLMEvent`` whose
    ``stop_reason`` is ``None`` is reporting a CLEAN turn, which is what the acp
    path leaves on a normal completion. The distinction lives here rather than in
    ``turn_outcome`` because only this layer can see whether the attribute exists
    at all — and it must not be resolved by a default: labelling the bare-usage
    case ``ok`` claims successes nobody observed, while labelling it ``unknown``
    puts it in ``telemetry._TERMINAL_FAULT_OUTCOMES`` and invents a fault for
    every clean background turn.
    """
    _MISSING = object()
    stop = getattr(event, "stop_reason", _MISSING)
    outcome = OUTCOME_UNCLASSIFIED if stop is _MISSING else turn_outcome(stop)  # type: ignore[arg-type]  # noqa: E501
    model = str(record.get("model") or "")
    provider = str(record.get("provider") or "")
    emit_turn_duration(
        record.get("duration_ms"),
        session_key=slot_key,
        outcome=outcome,
        model=model,
        provider=provider,
    )
    emit_turn_usage(
        input_tokens=record.get("input"),
        output_tokens=record.get("output"),
        credits=record.get("credits"),
        cost_usd=record.get("cost"),
        model=model,
        provider=provider,
    )


def _parse_token_history() -> dict[str, Any]:
    """Parse token usage from the daily-sharded usage directory.

    Reads ``<data home>/usage/tokens/YYYY-MM-DD.jsonl`` shards. Only shards
    inside the 30-day window are opened, so read cost stays O(window)
    regardless of total history.

    Each daily entry includes a ``providers`` map and a ``models`` map so
    the dashboard chart can offer provider/model filters.

    The result is cached on a tuple of (filename, mtime, size) for every
    shard in the window. Any append to any shard changes the key, so we
    re-parse exactly when needed. A 2 min TTL is also enforced as a safety
    net for clock skew and manual edits.
    """
    global _TOKEN_CACHE, _TOKEN_CACHE_KEY, _TOKEN_CACHE_TS

    shard_paths = _shards_in_window(_TOKEN_HISTORY_DAYS)
    if not shard_paths:
        # Drop any stale cache so we don't serve old data after manual deletion.
        _TOKEN_CACHE = {}
        _TOKEN_CACHE_KEY = None
        return {}

    # Fast path: serve cached result if no shard has changed since last parse
    # AND we're inside the TTL window.
    cache_key: tuple[tuple[str, float, int], ...] | None
    try:
        cache_key = tuple(
            sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in shard_paths)
        )
    except OSError:
        cache_key = None
    now = time.time()
    if (
        cache_key is not None
        and _TOKEN_CACHE_KEY == cache_key
        and (now - _TOKEN_CACHE_TS) < _TOKEN_CACHE_TTL
        and _TOKEN_CACHE
    ):
        return _TOKEN_CACHE

    cutoff = time.time() - (_TOKEN_HISTORY_DAYS * 86400)
    daily_input: Counter = Counter()
    daily_output: Counter = Counter()
    daily_cache_create: Counter = Counter()
    daily_cache_read: Counter = Counter()
    daily_cost: dict[str, float] = {}
    # Per-model per-day breakdown: {day: {model: {input, output, cache_create, cache_read, cost}}}
    daily_models: dict[str, dict[str, dict[str, float]]] = {}
    # Per-provider per-day breakdown (same shape).
    daily_providers: dict[str, dict[str, dict[str, float]]] = {}
    # Per-day provider × model cross-tab: {day: {provider: {model: bucket}}}
    # Required so the chart can show accurate values when both filters are set
    daily_pm: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    # Per-day list of {provider, model} pairs so the frontend can build
    # cascading filter options.
    seen_providers: set[str] = set()
    seen_models: set[str] = set()
    # Map of {provider: set[model]} so the frontend can cascade the model
    # dropdown off the selected provider and prevent invalid pairings.
    seen_provider_models: dict[str, set[str]] = {}

    for shard_path in shard_paths:
        try:
            with shard_path.open("rb") as fh:
                for line in bounded_records(fh, shard_path, label="usage"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    ts_epoch = _parse_row_ts(str(obj.get("ts") or ""))
                    if ts_epoch is None or ts_epoch < cutoff:
                        continue
                    day = _parse_row_day(obj.get("ts"))
                    if day is None:
                        continue
                    inp = obj.get("input", 0)
                    out = obj.get("output", 0)
                    cc = obj.get("cache_create", 0)
                    cr = obj.get("cache_read", 0)
                    cost = obj.get("cost", 0.0)
                    daily_input[day] += inp
                    daily_output[day] += out
                    daily_cache_create[day] += cc
                    daily_cache_read[day] += cr
                    daily_cost[day] = daily_cost.get(day, 0.0) + cost
                    provider = obj.get("provider", "")
                    # Per-model aggregation. For claude_code records, canonicalize
                    # the stored model string so pre/post-migration records (raw
                    # provider id vs canonical key) aggregate into ONE bucket.
                    # canonicalize_for_provider no-ops for other providers, so
                    # opencode/kiro model namespaces are never rewritten.
                    model = model_registry.canonicalize_for_provider(obj.get("model", ""), provider)
                    if model:
                        seen_models.add(model)
                        if day not in daily_models:
                            daily_models[day] = {}
                        if model not in daily_models[day]:
                            daily_models[day][model] = {
                                "input": 0,
                                "output": 0,
                                "cache_create": 0,
                                "cache_read": 0,
                                "cost_usd": 0.0,
                            }
                        m = daily_models[day][model]
                        m["input"] += inp
                        m["output"] += out
                        m["cache_create"] += cc
                        m["cache_read"] += cr
                        m["cost_usd"] += cost
                    # Per-provider aggregation
                    if provider:
                        seen_providers.add(provider)
                        if day not in daily_providers:
                            daily_providers[day] = {}
                        if provider not in daily_providers[day]:
                            daily_providers[day][provider] = {
                                "input": 0,
                                "output": 0,
                                "cache_create": 0,
                                "cache_read": 0,
                                "cost_usd": 0.0,
                            }
                        p = daily_providers[day][provider]
                        p["input"] += inp
                        p["output"] += out
                        p["cache_create"] += cc
                        p["cache_read"] += cr
                        p["cost_usd"] += cost
                    # Provider × model pairing (only count combos that actually
                    # appear together in a record so the frontend can scope its
                    # model dropdown to the selected provider).
                    if provider and model:
                        seen_provider_models.setdefault(provider, set()).add(model)
                        pm_day = daily_pm.setdefault(day, {})
                        pm_prov = pm_day.setdefault(provider, {})
                        pm_bucket = pm_prov.setdefault(
                            model,
                            {
                                "input": 0,
                                "output": 0,
                                "cache_create": 0,
                                "cache_read": 0,
                                "cost_usd": 0.0,
                            },
                        )
                        pm_bucket["input"] += inp
                        pm_bucket["output"] += out
                        pm_bucket["cache_create"] += cc
                        pm_bucket["cache_read"] += cr
                        pm_bucket["cost_usd"] += cost
        except (OSError, UnicodeDecodeError):
            # Skip a corrupt or unreadable shard rather than failing the
            # whole parse — the rest of the window is still useful.
            continue

    total_input = sum(daily_input.values())
    total_output = sum(daily_output.values())
    total_cache_create = sum(daily_cache_create.values())
    total_cache_read = sum(daily_cache_read.values())

    # Build daily token history
    all_days = sorted(set(daily_input.keys()) | set(daily_output.keys()))
    daily_history = []
    for d in all_days:
        entry: dict[str, Any] = {
            "date": d,
            "input": daily_input[d],
            "output": daily_output[d],
            "cache_create": daily_cache_create[d],
            "cache_read": daily_cache_read[d],
            "cost_usd": round(daily_cost.get(d, 0.0), 6),
        }
        if d in daily_models:
            entry["models"] = {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in sorted(daily_models[d].items())
            }
        if d in daily_providers:
            entry["providers"] = {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in sorted(daily_providers[d].items())
            }
        if d in daily_pm:
            entry["provider_models"] = {
                p: {
                    m: {**v, "cost_usd": round(v["cost_usd"], 6)}
                    for m, v in sorted(models_for_p.items())
                }
                for p, models_for_p in sorted(daily_pm[d].items())
            }
        daily_history.append(entry)

    result = {
        "total_input": total_input,
        "total_output": total_output,
        "cache_creation": total_cache_create,
        "cache_read": total_cache_read,
        "total": total_input + total_output + total_cache_create + total_cache_read,
        "cost_usd": round(sum(daily_cost.values()), 6),
        "daily_history": daily_history,
        "providers": sorted(seen_providers),
        "models": sorted(seen_models),
        "provider_models": {p: sorted(ms) for p, ms in sorted(seen_provider_models.items())},
    }
    if cache_key is not None:
        _TOKEN_CACHE = result
        _TOKEN_CACHE_KEY = cache_key
        _TOKEN_CACHE_TS = now
    return result


def _parse_sessions() -> dict:
    """Parse local kiro session files for usage analytics."""
    sessions_dir = _sessions_dir()
    if not sessions_dir.exists():
        return {"error": "No sessions directory"}

    cutoff = time.time() - (30 * 86400)
    daily: Counter = Counter()
    daily_msgs: Counter = Counter()
    daily_tools: Counter = Counter()
    total_sessions = 0
    total_msgs = 0
    total_tools = 0
    all_time_sessions = 0
    refused_transcripts = 0
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")

    try:
        entries = list(sessions_dir.iterdir())
    except OSError as exc:
        # The OSError carries a filesystem path; keep it server-side and return
        # a generic message (the ``error`` field is rendered verbatim in the UI).
        logger.warning("usage: cannot read sessions directory: %s", exc)
        return {"error": "cannot read sessions directory", "code": "sessions_dir_unreadable"}

    for f in entries:
        if f.suffix != ".jsonl":
            continue
        # Validate path through hooks.py (resolves symlinks, checks sensitive)
        resolved_str = validate_file_path(str(f))
        if resolved_str is None:
            # Counted, not swallowed (#6733): a refusal here is indistinguishable
            # from an idle account in the rendered numbers, and on a
            # roaming-profile (UNC) home EVERY transcript lands in this branch --
            # so the page reports a confident zero with nothing anywhere to say
            # why. Aggregated after the loop rather than logged per file, because
            # that failure mode refuses all of them. Admitting the transcript dir
            # to the UNC gate -- which is what would make the count correct
            # rather than merely explained -- is deferred to #8079; it needs a
            # resolution that refuses links atomically first.
            refused_transcripts += 1
            continue
        resolved = Path(resolved_str)
        try:
            mtime = resolved.stat().st_mtime
        except OSError:
            continue
        all_time_sessions += 1
        if mtime < cutoff:
            continue

        day = None  # derive from first JSONL entry's timestamp
        msgs = 0
        tools = 0
        try:
            with resolved.open("rb") as fh:
                for line in bounded_records(fh, resolved, label="usage.sessions"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if day is None:
                        day = _parse_row_day(obj.get("timestamp"))
                    kind = obj.get("kind", "")
                    if kind in ("Prompt", "AssistantMessage"):
                        msgs += 1
                    elif kind == "ToolResults":
                        tools += 1
        except (OSError, UnicodeDecodeError):
            continue

        if day is None:
            day = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")

        daily[day] += 1
        total_sessions += 1
        daily_msgs[day] += msgs
        daily_tools[day] += tools
        total_msgs += msgs
        total_tools += tools

    if refused_transcripts:
        # Server-side only: %s of a Path is a filesystem path, which the
        # returned payload deliberately never carries (see the iterdir handler
        # above).
        logger.warning(
            "usage: %d transcript(s) refused by path validation in %s; "
            "the reported session counts exclude them",
            refused_transcripts,
            sessions_dir,
        )

    # Build daily history sorted by date
    all_days = sorted(set(daily.keys()))
    history = []
    for d in all_days:
        history.append(
            {
                "date": d,
                "sessions": daily[d],
                "messages": daily_msgs[d],
                "tool_calls": daily_tools[d],
            }
        )

    # Compute period summaries
    week_start = (now_dt - timedelta(days=now_dt.weekday())).strftime("%Y-%m-%d")
    month_start = now_dt.strftime("%Y-%m-01")

    today = [h for h in history if h["date"] == today_str]
    week = [h for h in history if h["date"] >= week_start]
    month = [h for h in history if h["date"] >= month_start]

    return {
        "total_sessions": total_sessions,
        "total_messages": total_msgs,
        "total_tool_calls": total_tools,
        "all_time_sessions": all_time_sessions,
        "daily_history": history,
        "today": {
            "sessions": sum(h["sessions"] for h in today),
            "messages": sum(h["messages"] for h in today),
            "tool_calls": sum(h["tool_calls"] for h in today),
        },
        "this_week": {
            "sessions": sum(h["sessions"] for h in week),
            "messages": sum(h["messages"] for h in week),
            "tool_calls": sum(h["tool_calls"] for h in week),
        },
        "this_month": {
            "sessions": sum(h["sessions"] for h in month),
            "messages": sum(h["messages"] for h in month),
            "tool_calls": sum(h["tool_calls"] for h in month),
        },
        "avg_msgs_per_session": round(total_msgs / max(total_sessions, 1), 1),
        "avg_tools_per_session": round(total_tools / max(total_sessions, 1), 1),
    }


async def _cached_parse_sessions() -> dict:
    """Run _parse_sessions() off the event loop with a 120s TTL cache.

    Both usage endpoints call this so neither blocks the aiohttp loop on the
    iterdir + per-file stat + json.loads scan, and a burst of polls reuses one
    parse. Returns {} when there is no sessions directory (the common case for
    claude_code/bedrock, where ~/.kiro/sessions/cli is kiro-cli's own store).
    """
    global _SESSIONS_CACHE, _SESSIONS_CACHE_TS
    now = time.time()
    # Fast path — lock-free read (double-checked locking, like api_kiro_usage).
    # `is not None` (not truthiness) so a valid-but-empty {} parse is still a hit.
    if now - _SESSIONS_CACHE_TS < _CACHE_TTL and _SESSIONS_CACHE is not None:
        return _SESSIONS_CACHE
    if not _sessions_dir().exists():
        return {}
    async with _SESSIONS_CACHE_LOCK:
        # Re-check: a concurrent request may have refreshed while we waited, so
        # a burst of cold-cache polls collapses into a single parse.
        now = time.time()
        if now - _SESSIONS_CACHE_TS < _CACHE_TTL and _SESSIONS_CACHE is not None:
            return _SESSIONS_CACHE
        loop = asyncio.get_running_loop()
        sessions = await loop.run_in_executor(None, _parse_sessions)
        if isinstance(sessions, dict) and "error" not in sessions:
            _SESSIONS_CACHE = sessions
            _SESSIONS_CACHE_TS = time.time()
    return sessions


def get_usage_cache() -> dict:
    """Public accessor for billing usage cache from sessions handler."""
    try:
        from kiro_crew.dashboard.handlers.sessions import _usage_cache

        return dict(_usage_cache) if _usage_cache else {}
    except (ImportError, TypeError):
        logger.debug("Failed to read billing cache", exc_info=True)
        return {}


async def api_kiro_usage(request: web.Request) -> web.Response:
    """GET /api/usage/kiro — local session analytics + cached billing."""
    global _CACHE, _CACHE_TS
    now = time.time()

    # Fast path — lock-free read is intentional; worst case is one extra
    # cache refresh which is harmless (double-checked locking pattern).
    if now - _CACHE_TS < _CACHE_TTL and _CACHE:
        return web.json_response(_CACHE)

    async with _CACHE_LOCK:
        # Re-check after acquiring lock; another request may have refreshed.
        now = time.time()
        if now - _CACHE_TS < _CACHE_TTL and _CACHE:
            return web.json_response(_CACHE)

        username = getpass.getuser()

        # Parse local sessions (runs in thread to avoid blocking)
        loop = asyncio.get_running_loop()
        sessions = await loop.run_in_executor(None, _parse_sessions)

        # Get billing from existing usage cache
        billing: dict = {}
        usage = get_usage_cache()
        # Only surface billing when a real credit plan parsed. The cache can hold
        # an {"available": False} sentinel (kiro-cli absent / unparseable output)
        # which is truthy but carries no billing fields — treat it as no billing.
        if usage.get("credits_plan") is not None:
            billing = {
                "credits_used": usage.get("credits_used"),
                "credits_plan": usage.get("credits_plan"),
                "credits_overage": usage.get("credits_overage"),
                "percentage": usage.get("percentage"),
                "cost_usd": usage.get("cost_usd"),
                "resets": usage.get("resets"),
                "plan": usage.get("plan"),
                "overage_rate": usage.get("overage_rate"),
            }

        response: dict[str, Any] = {
            "username": username,
            "sessions": sessions,
            "billing": billing,
        }

        if "error" in sessions:
            response["error"] = sessions["error"]
        else:
            _CACHE = response
            _CACHE_TS = time.time()

    return web.json_response(response)


async def api_usage(request: web.Request) -> web.Response:
    """GET /api/usage — usage stats for the kiro-cli (KiroACP) provider."""
    return await api_kiro_usage(request)
