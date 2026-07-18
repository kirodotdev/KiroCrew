"""Kiro usage handlers — local session analytics + kiro-cli billing."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import model_registry
from kiro_crew.acp.types import TurnUsage
from kiro_crew.hooks import validate_file_path

logger = logging.getLogger(__name__)

_SESSIONS_DIR = Path.home() / ".kiro" / "sessions" / "cli"
_CACHE: dict[str, Any] = {}
_CACHE_TS: float = 0.0
_CACHE_TTL = 120  # 2 min
_CACHE_LOCK = asyncio.Lock()

# Cache for the raw _parse_sessions() result, used by api_usage's
# claude_code/bedrock branch (api_kiro_usage has its own _CACHE of the full
# response). _parse_sessions does a full iterdir + per-file stat + line-by-line
# json.loads of every in-window shard, so it is both TTL-cached (120s) and run
# off the event loop. Previously the cc/bedrock branch parsed inline on the loop
# on every poll. _SESSIONS_CACHE_LOCK collapses concurrent cold-cache requests
# into a single parse (mirrors api_kiro_usage's _CACHE_LOCK).
# None = unpopulated. A sentinel (not truthiness) so a valid-but-empty parse
# result ({}) is still cached and served from the fast path, rather than
# re-parsing on every call.
_SESSIONS_CACHE: dict[str, Any] | None = None
_SESSIONS_CACHE_TS: float = 0.0
_SESSIONS_CACHE_LOCK = asyncio.Lock()

# Cache for _parse_token_history — shards are append-only so we key the
# cache on a tuple of (filename, mtime, size) for every shard in the
# 30-day window. Any append to any shard changes the key, invalidating
# the cache exactly when needed. A 2 min TTL is also enforced as a
# safety net for clock skew and manual file edits.
_TOKEN_CACHE: dict[str, Any] = {}
_TOKEN_CACHE_KEY: tuple[tuple[str, float, int], ...] | None = None
_TOKEN_CACHE_TS: float = 0.0
_TOKEN_CACHE_TTL = 120  # 2 min
_TOKEN_USAGE_DIR = Path.home() / ".kirocrew" / "usage" / "tokens"
_TOKEN_HISTORY_DAYS = 30


def _shard_path_for(ts: datetime) -> Path:
    """Return the daily shard path for the local-day component of ``ts``.

    Shards are partitioned by the user's local date so the file boundary
    matches the day boundary the dashboard chart renders against.
    """
    return _TOKEN_USAGE_DIR / f"{ts.astimezone().strftime('%Y-%m-%d')}.jsonl"


def _shards_in_window(days: int) -> list[Path]:
    """Return shards whose date falls inside the last ``days`` days.

    The directory listing is cheap (≤31 entries) and we filter by filename
    rather than statting each file, so this stays well under a millisecond
    even on years-old installs.
    """
    paths: list[Path] = []
    if not _TOKEN_USAGE_DIR.exists():
        return paths
    cutoff_date = (datetime.now().astimezone() - timedelta(days=days)).date()
    for p in _TOKEN_USAGE_DIR.iterdir():
        if not p.is_file() or p.suffix != ".jsonl":
            continue
        try:
            shard_date = datetime.strptime(p.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if shard_date >= cutoff_date:
            paths.append(p)
    return paths


def _build_token_record(
    slot_key: str, model: str, event: object, provider: str, now: datetime
) -> dict[str, Any]:
    """Build the JSONL token-usage record dict (no I/O)."""
    # Usage lives on event.usage (TurnUsage). Fall back to the event itself when
    # it isn't a real TurnUsage (legacy / non-AcpEvent producers, test doubles).
    # credits is float-coerced so a non-numeric value can't break JSON serialization.
    _u = getattr(event, "usage", None)
    u = _u if isinstance(_u, TurnUsage) else event
    try:
        credits = float(getattr(u, "credits", 0.0))
    except (TypeError, ValueError):
        credits = 0.0
    return {
        "_type": "tokens",
        "ts": now.isoformat(),
        "slot": slot_key,
        "provider": provider or "",
        "model": model or "",
        "input": getattr(u, "input_tokens", 0),
        "output": getattr(u, "output_tokens", 0),
        "cache_create": getattr(u, "cache_creation_tokens", 0),
        "cache_read": getattr(u, "cache_read_tokens", 0),
        "cost": getattr(u, "cost_usd", 0.0),
        "credits": credits,
        "turns": getattr(u, "num_turns", 0),
        "duration_ms": getattr(u, "duration_ms", 0),
    }


def _write_token_record(record: dict[str, Any], now: datetime) -> None:
    """Append a prebuilt token record to today's shard (blocking I/O)."""
    shard_path = _shard_path_for(now)
    parent = shard_path.parent
    # mkdir only when missing — the dir is created once per day, not per turn.
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(shard_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def persist_token_record(slot_key: str, model: str, event: object, provider: str = "") -> None:
    """Append a token usage record to today's shard under
    ``~/.kirocrew/usage/tokens/YYYY-MM-DD.jsonl`` (synchronous).

    The ``provider`` field tags the source LLM backend (acp,
    claude_code, bedrock) so the dashboard chart can filter by provider.

    On the async chat hot path, prefer ``persist_token_record_async`` which
    offloads the blocking write off the event loop.
    """
    try:
        now = datetime.now().astimezone()
        _write_token_record(_build_token_record(slot_key, model, event, provider, now), now)
    except Exception:
        logger.debug("Failed to persist token record for slot %s", slot_key, exc_info=True)


async def persist_token_record_async(
    slot_key: str, model: str, event: object, provider: str = ""
) -> None:
    """Async variant: builds the record on-loop, offloads the file write.

    Called per agent turn (EVENT_COMPLETE) from the chat runner, which runs on
    the aiohttp event loop — the synchronous open/append previously blocked all
    co-resident coroutines for the IO window. These are best-effort analytics
    (no fsync, exceptions swallowed), so off-loop write loses no durability.
    """
    try:
        now = datetime.now().astimezone()
        record = _build_token_record(slot_key, model, event, provider, now)
        await asyncio.to_thread(_write_token_record, record, now)
    except Exception:
        logger.debug("Failed to persist token record for slot %s", slot_key, exc_info=True)


def _parse_token_history() -> dict[str, Any]:
    """Parse token usage from the daily-sharded usage directory.

    Reads ``~/.kirocrew/usage/tokens/YYYY-MM-DD.jsonl`` shards. Only shards
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
            with shard_path.open() as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    day = None
                    if "ts" in obj:
                        try:
                            ts_str = obj["ts"]
                            if ts_str.endswith("Z"):
                                ts_str = ts_str[:-1] + "+00:00"
                            ts_dt = datetime.fromisoformat(ts_str)
                            if ts_dt.timestamp() < cutoff:
                                continue
                            day = ts_dt.astimezone().strftime("%Y-%m-%d")
                        except (ValueError, TypeError, AttributeError):
                            pass
                    if not day:
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
    if not _SESSIONS_DIR.exists():
        return {"error": "No sessions directory"}

    cutoff = time.time() - (30 * 86400)
    daily: Counter = Counter()
    daily_msgs: Counter = Counter()
    daily_tools: Counter = Counter()
    total_sessions = 0
    total_msgs = 0
    total_tools = 0
    all_time_sessions = 0
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")

    try:
        entries = list(_SESSIONS_DIR.iterdir())
    except OSError as exc:
        return {"error": f"Cannot read sessions directory: {exc}"}

    for f in entries:
        if f.suffix != ".jsonl":
            continue
        # Validate path through hooks.py (resolves symlinks, checks sensitive)
        resolved_str = validate_file_path(str(f))
        if resolved_str is None:
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
            with resolved.open() as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if day is None and "timestamp" in obj:
                        try:
                            ts_str = obj["timestamp"]
                            if ts_str.endswith("Z"):
                                ts_str = ts_str[:-1] + "+00:00"
                            day = datetime.fromisoformat(ts_str).astimezone().strftime("%Y-%m-%d")
                        except (ValueError, TypeError, AttributeError):
                            pass
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
    if not _SESSIONS_DIR.exists():
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
