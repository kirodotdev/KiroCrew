"""The Usage page's session activity: the kiro-cli sessions on disk, the parse cached and
run off the event loop, and the snapshot a request waits for only briefly.

An owner of :mod:`kiro_crew.dashboard.handlers.usage`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kiro_crew.dashboard.handlers.usage_spend import daily_credits
from kiro_crew.dashboard.handlers.usage_store import (
    _SESSIONS_HISTORY_DAYS,
    _parse_row_day,
    _sessions_dir,
)
from kiro_crew.hooks import validate_file_path
from kiro_crew.jsonl_util import bounded_records
from kiro_crew.loop_lock import LoopBoundLock

# The facade's logger, so a log filter or capture keyed on the usage handler's name
# sees this module's records too.
logger = logging.getLogger("kiro_crew.dashboard.handlers.usage")


_CACHE_TTL = 120  # 2 min


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


def _parse_sessions() -> dict:
    """Parse local kiro session files for usage analytics."""
    sessions_dir = _sessions_dir()

    cutoff = time.time() - (_SESSIONS_HISTORY_DAYS * 86400)
    daily: Counter = Counter()
    daily_msgs: Counter = Counter()
    daily_tools: Counter = Counter()
    total_sessions = 0
    total_msgs = 0
    total_tools = 0
    all_time_sessions = 0
    # Count of transcripts that did NOT load for any reason (validator refusal,
    # stat failure, read failure) -- surfaced so the page can say the totals are
    # incomplete instead of rendering a silent under-count. The name matches the
    # payload/frontend contract; it is the did-not-load total.
    refused_transcripts = 0
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")

    # Set when the directory could not be read at all. Carried ALONGSIDE the
    # statistics rather than instead of them: every consumer of this payload
    # reads the period keys unconditionally, so an error-only object is not a
    # degraded answer, it is a differently-shaped one.
    read_error: dict[str, str] = {}
    try:
        entries = list(sessions_dir.iterdir())
    except FileNotFoundError:
        # First-run homes have no transcript directory yet; use the same
        # complete zero statistics as an existing, empty directory.
        entries = []
    except OSError as exc:
        # The OSError carries a filesystem path; keep it server-side and report a
        # generic message (the ``error`` field is rendered verbatim in the UI).
        logger.warning("usage: cannot read sessions directory: %s", exc)
        entries = []
        read_error = {
            "error": "cannot read sessions directory",
            "code": "sessions_dir_unreadable",
        }

    for f in entries:
        if f.suffix != ".jsonl":
            continue
        # Validate path through hooks.py (resolves symlinks, checks sensitive)
        resolved_str = validate_file_path(str(f))
        if resolved_str is None:
            # Counted, not swallowed: a refusal here is indistinguishable
            # from an idle account in the rendered numbers, and on a
            # roaming-profile (UNC) home EVERY transcript lands in this branch --
            # so the page reports a confident zero with nothing anywhere to say
            # why. Aggregated after the loop rather than logged per file, because
            # that failure mode refuses all of them. Admitting the transcript dir
            # to the UNC gate -- which is what would make the count correct
            # rather than merely explained -- is deferred; it needs a resolution
            # that refuses links atomically first.
            refused_transcripts += 1
            continue
        resolved = Path(resolved_str)
        try:
            mtime = resolved.stat().st_mtime
        except OSError:
            # A transcript that validated but cannot be stat'd did not load, so
            # it is dropped from the counts exactly like a refusal. Count
            # it in the same total: the warning's absence promises complete data,
            # so every did-not-load branch must feed it, not just the UNC refusal.
            refused_transcripts += 1
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
            # Same as the stat branch above: a transcript that could not be read
            # did not load, so it counts toward the incomplete-data warning
            # rather than vanishing from the totals.
            refused_transcripts += 1
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
        # above). Counts every did-not-load branch (validator refusal, stat
        # failure, read failure), not just the UNC refusal.
        logger.warning(
            "usage: %d transcript(s) could not be loaded in %s; "
            "the reported session counts exclude them",
            refused_transcripts,
            sessions_dir,
        )

    # Build daily history sorted by date. Credits come from the per-turn usage
    # shards, not the transcripts, so a day can carry spend without a transcript
    # (a background slot, a refused file): such a day still gets a row, with
    # zero sessions, so that spend is shown rather than dropped. Counter lookups
    # on those days read 0 without inserting a key.
    credits_by_day = daily_credits(_SESSIONS_HISTORY_DAYS)
    all_days = sorted(set(daily.keys()) | set(credits_by_day.keys()))
    history: list[dict[str, Any]] = []
    for d in all_days:
        history.append(
            {
                "date": d,
                "sessions": daily[d],
                "messages": daily_msgs[d],
                "tool_calls": daily_tools[d],
                "credits": round(credits_by_day.get(d, 0.0), 2),
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
        # How many transcripts the path validator refused. Carried in
        # the payload -- not just the server log -- so the page can say the
        # count is incomplete instead of rendering a confident zero. On a
        # roaming-profile (UNC) home this is every transcript, so a zero
        # session count with a positive refusal count is the exact silent
        # failure this field makes visible.
        "refused_transcripts": refused_transcripts,
        # Present only when the directory read itself failed. ``api_kiro_usage``
        # keys its no-cache decision on this, and the zeros above are then a
        # SHAPE, not a measurement -- which is why the message has to travel with
        # them rather than replace them.
        **read_error,
    }


async def _cached_parse_sessions() -> dict:
    """Run _parse_sessions() off the event loop with a 120s TTL cache.

    Both usage endpoints call this so neither blocks the aiohttp loop on the
    iterdir + per-file stat + json.loads scan, and a burst of polls reuses one
    parse. A missing sessions directory is parsed into the route's complete zero
    shape; returning a bare ``{}`` would violate the frontend contract.
    """
    global _SESSIONS_CACHE, _SESSIONS_CACHE_TS
    now = time.time()
    # Fast path — lock-free read (double-checked locking, like api_kiro_usage).
    # `is not None` (not truthiness) so a valid-but-empty {} parse is still a hit.
    if now - _SESSIONS_CACHE_TS < _CACHE_TTL and _SESSIONS_CACHE is not None:
        return _SESSIONS_CACHE
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


# A cold 30-day session scan can traverse many large JSONL transcripts. Local
# dashboard requests can wait for it, but a phone reaches the gateway through an
# extra transport; waiting for the whole scan leaves its Usage card empty until
# completion. Give cheap scans a small synchronous budget, then return cached
# billing + a truthful refreshing marker while the one shared scan continues.
_USAGE_SESSION_WAIT_SECONDS = 0.05


_USAGE_SESSION_REFRESH_TASK: asyncio.Task[dict[str, Any]] | None = None


def _empty_session_summary() -> dict[str, Any]:
    """Complete zero-shaped session payload used only during a cold refresh."""
    empty_period = {"sessions": 0, "messages": 0, "tool_calls": 0}
    return {
        "total_sessions": 0,
        "total_messages": 0,
        "total_tool_calls": 0,
        "all_time_sessions": 0,
        "daily_history": [],
        "today": dict(empty_period),
        "this_week": dict(empty_period),
        "this_month": dict(empty_period),
        "avg_msgs_per_session": 0.0,
        "avg_tools_per_session": 0.0,
        "refused_transcripts": 0,
    }


async def _usage_sessions_snapshot() -> tuple[dict[str, Any], bool]:
    """Return session analytics and whether a slower refresh is still running."""
    global _USAGE_SESSION_REFRESH_TASK

    loop = asyncio.get_running_loop()
    task = _USAGE_SESSION_REFRESH_TASK
    if task is not None and task.done():
        _USAGE_SESSION_REFRESH_TASK = None
        if task.cancelled():
            task = None
        else:
            completed = task.result()
            # Successful production refreshes populate _SESSIONS_CACHE before the
            # task completes. A patched/test refresh or an error result does not;
            # return that one result rather than discarding its only copy. Otherwise
            # continue through freshness below so an expired cache starts a new scan.
            if _SESSIONS_CACHE is None or "error" in completed:
                return completed, False
            task = None
    elif task is not None and task.get_loop() is not loop:
        # Module globals survive pytest/event-loop replacement and embedded
        # gateway restarts. A task cannot be awaited from a different loop.
        if not task.done():
            task.cancel()
        task = None
        _USAGE_SESSION_REFRESH_TASK = None

    now = time.time()
    if now - _SESSIONS_CACHE_TS < _CACHE_TTL and _SESSIONS_CACHE is not None:
        return _SESSIONS_CACHE, False

    if task is None:
        task = loop.create_task(_cached_parse_sessions(), name="usage-session-refresh")
        _USAGE_SESSION_REFRESH_TASK = task

    try:
        sessions = await asyncio.wait_for(asyncio.shield(task), timeout=_USAGE_SESSION_WAIT_SECONDS)
    except asyncio.TimeoutError:
        stale = _SESSIONS_CACHE
        if isinstance(stale, dict) and "today" in stale:
            return stale, True
        return _empty_session_summary(), True
    except Exception:
        if task.done():
            _USAGE_SESSION_REFRESH_TASK = None
        raise

    _USAGE_SESSION_REFRESH_TASK = None
    return sessions, False
