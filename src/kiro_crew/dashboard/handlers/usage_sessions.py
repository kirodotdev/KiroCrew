"""The Usage page's session activity: the kiro-cli and KAS sessions on disk, the parse
cached and run off the event loop, and the snapshot a request waits for only briefly.

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

from kiro_crew import platform_compat
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


# Per-transcript counts from the last parse, keyed on the resolved path and
# checked against ``(st_mtime_ns, st_size)``: a refresh reads again only the
# transcripts that changed since. Each parse replaces the whole map with the
# entries it saw, so a deleted transcript leaves it on the next parse.
_TRANSCRIPT_COUNTS: dict[str, tuple[int, int, str | None, int, int]] = {}


def _kas_transcripts(kas_root: Path, cli_dir: Path) -> tuple[list[Path], int]:
    """The KAS ``messages.jsonl`` paths under ``kas_root`` and the unread directory count.

    The KAS harness writes one record per session as
    ``<kas_root>/<id>/sess_<uuid>/messages.jsonl``, beside kiro-cli's flat ``cli``
    directory, and deletes the record when it tears the session down. A directory
    that disappears during the listing is skipped. One that exists but cannot be
    listed, or a root the path check refuses, leaves its sessions out of the totals
    and is therefore counted as a directory that did not load. How many transcripts
    it holds is unknown, so it is not counted as a transcript.

    Each record directory is opened through the pin that :func:`_pin_listing_dir`
    takes on the root. A link at the name fails that open and is skipped without a
    listing.
    """
    try:
        root = _pin_listing_dir(kas_root)
    except (FileNotFoundError, NotADirectoryError):
        return [], 0
    except OSError:
        return [], 1
    if root is None:
        # Refused by the path check, as a home on an untrusted network share is:
        # its sessions did not load, so it counts like an unreadable root.
        return [], 1
    found: list[Path] = []
    unread_dirs = 0
    with root:
        try:
            names = root.names()
        except (FileNotFoundError, NotADirectoryError):
            return [], 0
        except OSError:
            return [], 1
        for name in names:
            if name == cli_dir.name:
                continue
            try:
                child = root.child_if_real_dir(name)
                if child is None:
                    continue
                with child:
                    records = child.names()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                unread_dirs += 1
                continue
            found.extend(
                Path(child.path) / r / "messages.jsonl" for r in records if r.startswith("sess_")
            )
    return found, unread_dirs


def _pin_listing_dir(path: Path) -> platform_compat.PinnedDirectory | None:
    """A pin on *path* for listing it, or ``None`` when *path* fails the path check.

    The pin opens the canonical path that :func:`validate_file_path` returns, once
    the check has passed. On Windows, opening or listing a link to a UNC share sends
    an SMB authentication request to the share's host. The check therefore fails
    such a link before any open. A link planted at the name after the check fails
    the pin's open instead, and while the pin is held, the directory and its
    ancestors cannot be renamed.

    Only a reparse point that stands for another name is refused (``links_only``),
    so a Windows cloud-files or dedup directory is listed as the directory it is.
    The children that the listing opens through the pin are pinned with
    ``links_only`` too.

    Raises ``OSError`` when the directory does not open, for example when it does
    not exist.
    """
    canonical = validate_file_path(str(path))
    if canonical is None:
        return None
    pinned = platform_compat.pin_directory(canonical, links_only=True)
    return platform_compat.PinnedDirectory(pinned, canonical, links_only=True)


def _count_row(obj: dict, kas: bool) -> tuple[int, int]:
    """The (messages, tool calls) counts that a transcript row adds.

    A kiro-cli row states its kind in a top-level ``kind`` field, and a KAS row
    states it in ``payload.type``. KAS assistant rows include reasoning steps, and of
    those rows a message is one whose ``operationType`` is ``Say``.
    """
    if not kas:
        kind = obj.get("kind", "")
        if kind in ("Prompt", "AssistantMessage"):
            return 1, 0
        return (0, 1) if kind == "ToolResults" else (0, 0)
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return 0, 0
    row_type = payload.get("type")
    if row_type == "user" or (row_type == "assistant" and payload.get("operationType") == "Say"):
        return 1, 0
    return (0, 1) if row_type == "tool_call" else (0, 0)


def _count_transcript(path: Path, kas: bool) -> tuple[str | None, int, int] | None:
    """A transcript's (day, messages, tool calls), or ``None`` when it cannot be read.

    The day is the local day of the first row that carries a timestamp, or ``None``
    when no row carries one.
    """
    day = None
    msgs = 0
    tools = 0
    try:
        with path.open("rb") as fh:
            for line in bounded_records(fh, path, label="usage.sessions"):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if day is None:
                    day = _parse_row_day(obj.get("timestamp"))
                row_msgs, row_tools = _count_row(obj, kas)
                msgs += row_msgs
                tools += row_tools
    except (OSError, UnicodeDecodeError):
        return None
    return day, msgs, tools


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
    # Count of transcript FILES that did not load for any reason (validator
    # refusal, stat failure, read failure) -- surfaced so the page can say the
    # totals are incomplete instead of rendering a silent under-count. The name
    # matches the payload/frontend contract; it is the did-not-load total.
    refused_transcripts = 0
    # Count of session DIRECTORIES that did not load: the refused sessions/cli
    # directory, and a KAS root or record directory that was refused or could not
    # be listed. The number of transcripts inside such a directory is unknown, so
    # it is reported apart from ``refused_transcripts`` rather than added to it.
    unreadable_session_dirs = 0
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")

    # Set when the directory could not be read at all. Carried ALONGSIDE the
    # statistics rather than instead of them: every consumer of this payload
    # reads the period keys unconditionally, so an error-only object is not a
    # degraded answer, it is a differently-shaped one.
    read_error: dict[str, str] = {}
    try:
        pinned = _pin_listing_dir(sessions_dir)
        if pinned is None:
            # The path check refuses the directory itself when the home is on a
            # network share the UNC gate does not trust. Its transcripts did not
            # load, which the warning reports; an ``error`` here would make the
            # frontend drop the billing that does not depend on transcripts.
            entries = []
            unreadable_session_dirs += 1
        else:
            with pinned:
                entries = [Path(pinned.path) / name for name in pinned.names()]
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

    transcripts = [(f, False) for f in entries if f.suffix == ".jsonl"]
    kas_paths, unread_kas_dirs = _kas_transcripts(sessions_dir.parent, sessions_dir)
    unreadable_session_dirs += unread_kas_dirs
    transcripts += [(f, True) for f in kas_paths]

    global _TRANSCRIPT_COUNTS
    previous_counts = _TRANSCRIPT_COUNTS
    seen_counts: dict[str, tuple[int, int, str | None, int, int]] = {}

    for f, kas in transcripts:
        # Validate path through hooks.py (resolves symlinks, checks sensitive)
        resolved_str = validate_file_path(str(f))
        if resolved_str is None:
            # Counted, not swallowed: a refusal here is indistinguishable
            # from an idle account in the rendered numbers. Aggregated after the
            # loop rather than logged per file. A roaming-profile (UNC) home is
            # refused one level up, at the directory listing, and counted in
            # ``unreadable_session_dirs``.
            refused_transcripts += 1
            continue
        resolved = Path(resolved_str)
        try:
            st = resolved.stat()
        except FileNotFoundError:
            if kas:
                # A KAS record without a messages file is not a failed load: the
                # record holds only its session.json, or the harness deleted it at
                # teardown after the listing.
                continue
            refused_transcripts += 1
            continue
        except OSError:
            # A transcript that validated but cannot be stat'd did not load, so
            # it is dropped from the counts exactly like a refusal. Count
            # it in the same total: the warning's absence promises complete data,
            # so every did-not-load branch must feed it, not just the UNC refusal.
            refused_transcripts += 1
            continue
        mtime = st.st_mtime
        if mtime < cutoff:
            all_time_sessions += 1
            continue

        key = str(resolved)
        cached = previous_counts.get(key)
        if cached is not None and cached[:2] == (st.st_mtime_ns, st.st_size):
            seen_counts[key] = cached
            day, msgs, tools = cached[2], cached[3], cached[4]
        else:
            parsed = _count_transcript(resolved, kas)
            if parsed is None:
                # Same as the stat branch above: a transcript that could not be
                # read did not load, so it counts toward the incomplete-data
                # warning rather than vanishing from the totals.
                refused_transcripts += 1
                continue
            day, msgs, tools = parsed
            seen_counts[key] = (st.st_mtime_ns, st.st_size, day, msgs, tools)

        if day is None:
            day = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")

        daily[day] += 1
        total_sessions += 1
        all_time_sessions += 1
        daily_msgs[day] += msgs
        daily_tools[day] += tools
        total_msgs += msgs
        total_tools += tools

    _TRANSCRIPT_COUNTS = seen_counts

    if refused_transcripts:
        # Server-side only: %s of a Path is a filesystem path, which the
        # returned payload deliberately never carries (see the listing handler
        # above). Counts every transcript did-not-load branch (validator
        # refusal, stat failure, read failure).
        logger.warning(
            "usage: %d transcript(s) could not be loaded in %s; "
            "the reported session counts exclude them",
            refused_transcripts,
            sessions_dir,
        )
    if unreadable_session_dirs:
        logger.warning(
            "usage: %d session dir(s) under %s could not be read; "
            "the reported session counts exclude their transcripts",
            unreadable_session_dirs,
            sessions_dir.parent,
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
        # How many transcript files did not load. Carried in the payload -- not
        # just the server log -- so the page can say the count is incomplete
        # instead of rendering a confident zero.
        "refused_transcripts": refused_transcripts,
        # How many session directories did not load. On a roaming-profile (UNC)
        # home the path check refuses both session directories, so a zero
        # session count with a positive count here is the silent failure this
        # field makes visible. The page reports it without a number, because the
        # transcripts inside those directories were never counted.
        "unreadable_session_dirs": unreadable_session_dirs,
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
        "unreadable_session_dirs": 0,
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
