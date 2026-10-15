"""The Usage page's session activity: the kiro-cli sessions on disk with their estimated
token counts, the parse cached and run off the event loop, and the snapshot a request
waits for only briefly.

An owner of :mod:`kiro_crew.dashboard.handlers.usage`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kiro_crew import model_registry
from kiro_crew.dashboard.handlers.usage_spend import daily_credits
from kiro_crew.dashboard.handlers.usage_store import (
    _SESSIONS_HISTORY_DAYS,
    _parse_row_day,
    _sessions_dir,
)
from kiro_crew.hooks import (
    FileTooLargeError,
    safe_read_file_bytes_nolink,
    validate_file_path,
)
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


#: A kiro-cli session's metadata document (``<id>.json`` beside its ``.jsonl``
#: transcript) is read whole, so it is bounded. Real documents stay near 2 MB at
#: most; a larger file is counted as unreadable instead of loaded.
_CLI_SESSION_JSON_MAX_BYTES = 8 * 1024 * 1024


#: Characters per token for the output estimate: kiro-cli records each reply's
#: length in characters and no token count.
_EST_CHARS_PER_TOKEN = 4


#: Ceiling on every count read from a session document. Real counts sit far
#: below it, and the bound keeps each product and sum built from them a finite
#: float, so a corrupt document cannot raise ``OverflowError`` on this read path.
_EST_COUNT_MAX = 10**12


def _est_count(value: Any) -> int | None:
    """A non-negative whole count from a session document, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _EST_COUNT_MAX else None


def _est_percent(value: Any) -> float | None:
    """A context-usage percentage in ``[0, 100]``, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value) if 0 <= value <= 100 else None
    if isinstance(value, float) and math.isfinite(value) and 0.0 <= value <= 100.0:
        return value
    return None


def _turn_window(model: str, session_model: str, session_window: int | None) -> int | None:
    """The context window, in tokens, that a turn's usage percentage is a share of.

    kiro-cli stores only the percentage, measured against the window of the model
    that served the turn. The session document names one model and its window
    (the session's current model), so that window applies to turns served by
    that model or by ``auto``. Any other model's window comes from the model
    registry; ``None`` means no source supplies a usable window.
    """
    if session_window and (not model or model == "auto" or model == session_model):
        return session_window
    return _est_count(model_registry.model_window(model)) if model else None


def _estimate_cli_tokens(
    listing: list[os.DirEntry[str]],
    *,
    root: Path,
    since_day: str,
    since_epoch: float,
    until_day: str,
) -> dict[str, Any]:
    """Estimated Kiro CLI token use per local day, from the session documents.

    kiro-cli writes zero into every token field of its per-turn metadata, so real
    counts are not on disk. Each turn does record how many model requests it
    made, its context-window usage at the end as a percentage, and its reply
    length in characters. Every request re-sends the context, so a turn's input
    is its request count times the context size averaged between the previous
    turn's end and its own end. Output is the reply length divided by
    :data:`_EST_CHARS_PER_TOKEN` as an approximation. That length leaves out
    tool-call arguments and reasoning, and characters per token vary, so the
    estimate is not a bound in either direction.

    A turn with no usable context reading (it ended in an error before kiro-cli
    recorded one, or its model's window is unknown) is taken to have kept the
    context it started with. A session's first turn starts from zero.

    Returns ``{"by_day": {day: {"input", "output", "requests"}}, "unreadable": n}``
    for turns that ended on a day from *since_day* through *until_day* (both
    inclusive, local ``YYYY-MM-DD``). A turn stamped after *until_day* (clock
    skew, an imported session) is left out, so a caller passing today never
    receives a future day. *listing* is the sessions directory's
    :func:`os.scandir` entries. Each JSON document is validated first, and its
    mtime then comes from its listing entry: on Windows the listing already
    holds it, and on POSIX the entry stats its own path without following a
    link. A validated document untouched since before *since_epoch* is skipped
    before the no-link reader runs. A document inside the window whose listing
    entry is a link, whatever its target, counts as unreadable before the reader
    runs: :func:`validate_file_path` resolves a link, so the reader would open
    the target, and a link to another document in the directory would count
    that document's turns twice. The link check reads the type the directory
    listing reports for the entry.
    ``unreadable`` counts every document that :func:`validate_file_path` refuses
    before the mtime check, whatever its age, because the estimator does not
    inspect an unvalidated path. It also counts validated documents whose mtime
    cannot be read, documents inside the window that are links, and documents
    inside the window that the no-link reader refuses because its path
    validation fails, a link is planted at the resolved name after validation,
    the inode has a second hard link, the inode is not a regular file, the opened
    file is outside *root*, the file cannot be read, or the file exceeds the size
    bound. It also counts documents that are nested too deeply, are not JSON, are
    not shaped like a session document (an object whose ``session_state`` object
    holds the ``conversation_metadata.user_turn_metadatas`` list), or whose
    non-empty turn list has no turn with an ``end_timestamp`` that
    :func:`_parse_row_day` can read; a non-zero count makes the totals a lower
    bound. kiro-cli writes only session documents here, so a renamed key in its
    format raises that warning instead of reading as a session with nothing to
    count. An empty turn list is a session with nothing to count yet, not an
    unreadable one.
    """
    by_day: dict[str, dict[str, float]] = {}
    unreadable = 0
    for entry in listing:
        f = Path(entry.path)
        if f.suffix != ".json":
            continue
        if validate_file_path(str(f)) is None:
            unreadable += 1
            continue
        try:
            # The mtime comes from the listing entry, after validation. On
            # Windows the listing already holds it, so this check opens no
            # path: a junction swapped into an ancestor of the sessions
            # directory after validation cannot turn it into an SMB probe. On
            # POSIX the entry stats its own path without following a link. The
            # skip keeps the reader from running on every document the
            # directory has ever held.
            if entry.stat(follow_symlinks=False).st_mtime < since_epoch:
                continue
        except OSError:
            unreadable += 1
            continue
        if entry.is_symlink():
            # The validator resolves a link, so the reader would open the
            # target and pass containment for a link to another document in
            # this directory, counting that document's turns a second time.
            # The entry answers from the type the directory listing reports
            # (on Windows always; on POSIX unless the filesystem reports no
            # type, when it falls back to an lstat that does not follow the
            # link).
            unreadable += 1
            continue
        try:
            raw = safe_read_file_bytes_nolink(
                str(f), str(root), max_bytes=_CLI_SESSION_JSON_MAX_BYTES
            )
        except FileTooLargeError:
            unreadable += 1
            continue
        if raw is None:
            unreadable += 1
            continue
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError):  # Invalid text/JSON or excessive nesting
            unreadable += 1
            continue
        state = doc.get("session_state") if isinstance(doc, dict) else None
        meta = state.get("conversation_metadata") if isinstance(state, dict) else None
        turns = meta.get("user_turn_metadatas") if isinstance(meta, dict) else None
        if not isinstance(turns, list):
            # Not shaped like a session document. kiro-cli writes nothing else
            # here, so a renamed key in its format raises the unreadable-sessions
            # warning instead of reading as a session with nothing to count.
            unreadable += 1
            continue
        rts = state.get("rts_model_state") if isinstance(state, dict) else None
        info = rts.get("model_info") if isinstance(rts, dict) else None
        if not isinstance(info, dict):
            info = {}
        model_id = info.get("model_id")
        session_model = model_id if isinstance(model_id, str) else ""
        session_window = _est_count(info.get("context_window_tokens")) or None
        prev_context = 0.0
        dated = False
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_model = turn.get("model")
            model = turn_model if isinstance(turn_model, str) else ""
            pct = _est_percent(turn.get("final_context_usage_percentage"))
            if pct is None:
                pct = _est_percent(turn.get("context_usage_percentage"))
            window = _turn_window(model, session_model, session_window)
            requests = _est_count(turn.get("total_request_count")) or 0
            reply_chars = _est_count(turn.get("assistant_response_length")) or 0
            context = pct / 100.0 * window if pct is not None and window else prev_context
            input_tokens = requests * (prev_context + context) / 2.0
            # A turn outside the window still seeds the next turn's start.
            prev_context = context
            day = _parse_row_day(turn.get("end_timestamp"))
            if day is not None:
                dated = True
            if day is None or day < since_day or day > until_day:
                continue
            bucket = by_day.setdefault(day, {"input": 0.0, "output": 0.0, "requests": 0.0})
            bucket["input"] += input_tokens
            bucket["output"] += reply_chars / _EST_CHARS_PER_TOKEN
            bucket["requests"] += requests
        if turns and not dated:
            # Turns that cannot be placed on any day (a renamed stamp key) are a
            # session the estimate cannot read, so it feeds the warning. An empty
            # turn list is a session with nothing to count yet, and does not.
            unreadable += 1
    return {
        "by_day": {
            day: {k: int(round(v)) for k, v in counts.items()} for day, counts in by_day.items()
        },
        "unreadable": unreadable,
    }


def _sum_estimate(
    by_day: dict[str, dict[str, int]], first_day: str, end_day: str | None
) -> dict[str, int]:
    """Sum :func:`_estimate_cli_tokens` days in ``[first_day, end_day)``; no end when ``None``."""
    total = {"input": 0, "output": 0, "requests": 0}
    for day, counts in by_day.items():
        if day >= first_day and (end_day is None or day < end_day):
            for key in total:
                total[key] += counts[key]
    return total


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
        # One listing feeds both scans. The token estimate reads each document's
        # mtime from its entry (see _estimate_cli_tokens).
        with os.scandir(sessions_dir) as scan:
            listing = list(scan)
    except FileNotFoundError:
        # First-run homes have no transcript directory yet; use the same
        # complete zero statistics as an existing, empty directory.
        listing = []
    except OSError as exc:
        # The OSError carries a filesystem path; keep it server-side and report a
        # generic message (the ``error`` field is rendered verbatim in the UI).
        logger.warning("usage: cannot read sessions directory: %s", exc)
        listing = []
        read_error = {
            "error": "cannot read sessions directory",
            "code": "sessions_dir_unreadable",
        }
    entries = [Path(e.path) for e in listing]

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
    # The estimate feeds two readers with different windows: the card shows the
    # whole previous month, the history rows below keep their 30-day window. The
    # scan starts at the earlier of the two starts and stops at today, and each
    # reader then takes its own days from the result.
    month_start_dt = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_start_dt = (month_start_dt - timedelta(days=1)).replace(day=1)
    history_start_dt = datetime.fromtimestamp(cutoff).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    scan_start_dt = min(last_month_start_dt, history_start_dt)
    month_start = month_start_dt.strftime("%Y-%m-%d")
    last_month_start = last_month_start_dt.strftime("%Y-%m-%d")
    history_start = history_start_dt.strftime("%Y-%m-%d")
    estimate = _estimate_cli_tokens(
        listing,
        root=sessions_dir,
        since_day=scan_start_dt.strftime("%Y-%m-%d"),
        since_epoch=scan_start_dt.timestamp(),
        until_day=today_str,
    )
    tokens_by_day: dict[str, dict[str, int]] = estimate["by_day"]
    token_days = {d for d in tokens_by_day if d >= history_start}
    all_days = sorted(set(daily.keys()) | set(credits_by_day.keys()) | token_days)
    history: list[dict[str, Any]] = []
    for d in all_days:
        day_tokens = tokens_by_day.get(d)
        history.append(
            {
                "date": d,
                "sessions": daily[d],
                "messages": daily_msgs[d],
                "tool_calls": daily_tools[d],
                "credits": round(credits_by_day.get(d, 0.0), 2),
                "est_tokens": day_tokens["input"] + day_tokens["output"] if day_tokens else 0,
            }
        )

    # Compute period summaries
    week_start = (now_dt - timedelta(days=now_dt.weekday())).strftime("%Y-%m-%d")

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
        # Kiro CLI token use estimated from the session documents (see
        # _estimate_cli_tokens): calendar month to date and the whole previous
        # month. A non-zero count beside them means the sums leave those session
        # documents out.
        "estimated_tokens": {
            "this_month": _sum_estimate(tokens_by_day, month_start, None),
            "last_month": _sum_estimate(tokens_by_day, last_month_start, month_start),
            "unreadable_sessions": estimate["unreadable"],
        },
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
        "estimated_tokens": {
            "this_month": {"input": 0, "output": 0, "requests": 0},
            "last_month": {"input": 0, "output": 0, "requests": 0},
            "unreadable_sessions": 0,
        },
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
