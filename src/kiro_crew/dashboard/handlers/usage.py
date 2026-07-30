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
from kiro_crew.config.paths import config_dir, kiro_sessions_dir
from kiro_crew.hooks import validate_file_path

logger = logging.getLogger(__name__)

_SESSIONS_DIR = kiro_sessions_dir()
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
_TOKEN_USAGE_DIR = config_dir() / "usage" / "tokens"
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


_CONTEXT_TOP_SESSIONS = 8
# Fingerprint + TTL cache, same contract as _TOKEN_CACHE: the Telemetry panel
# polls every 5s, and the shards are append-only, so (name, mtime, size) over
# the window invalidates exactly when a turn lands.
_CONTEXT_CACHE: dict[str, Any] | None = None
_CONTEXT_CACHE_KEY: tuple[Any, ...] | None = None
_CONTEXT_CACHE_TS: float = 0.0
_CONTEXT_CACHE_TTL = 30.0


def context_occupancy(days: int = 14) -> dict[str, Any]:
    """Aggregate per-turn context-window occupancy from the token row store.

    ``persist_token_record`` writes ``context_used`` / ``context_window`` on
    every turn across all nine dispatch surfaces, but nothing read them — the
    fields were write-only, so the session-death early warning they exist for
    was invisible. This is their read side.

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
            with shard_path.open() as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    used = _coerce_int(obj.get("context_used"))
                    window = _coerce_int(obj.get("context_window"))
                    if used <= 0 or window <= 0:
                        continue
                    ts_epoch = 0.0
                    ts_raw = obj.get("ts") or ""
                    try:
                        ts_str = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                        ts_epoch = datetime.fromisoformat(ts_str).timestamp()
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if ts_epoch < cutoff:
                        continue
                    p = (used / window) * 100.0
                    pcts.append(p)
                    slot = str(obj.get("slot") or "unknown")
                    cur = per_session.get(slot)
                    if cur is None:
                        cur = {
                            "slot": slot,
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
                                "surface": str(obj.get("surface") or ""),
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
        idx = min(len(pcts) - 1, max(0, int(round(q * (len(pcts) - 1)))))
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
                if isinstance(candidate, str) and candidate and candidate != "auto":
                    return candidate
    except Exception:
        pass
    return ""


def _resolve_model(model: str, model_source: object) -> str:
    """Resolve the model to record, treating the ``"auto"`` sentinel as unresolved.

    ``"auto"`` is not a model — it means "let the backend choose" — so recording
    it would put a non-model value in the attribution dimension. Several
    surfaces pass it verbatim (``agent.model`` defaults to ``"auto"``, and the
    task runner forwards that value), so gating only on an empty string lets it
    through. When the caller's value is unresolved we take the provider's
    resolved id; if that is unavailable the field stays blank, which is what
    ``test_late_backfill_skips_auto_sentinel`` requires ("the record stays blank
    until a real model is known").
    """
    if (model or "").strip().lower() not in ("", "auto"):
        return model
    if model_source is None:
        return "" if (model or "").strip().lower() == "auto" else model
    return read_effective_model(model_source)


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
) -> dict[str, Any]:
    """Build the JSONL token-usage record dict (no I/O).

    ``surface`` tags the dispatch origin (``dashboard``, ``cron``, ``subagent``,
    …) and ``agent`` the agent id resolved for the turn. ``context_used`` /
    ``context_window`` record context-window occupancy (read from the provider
    via :func:`read_context_tokens` at the call site). All four are additive and
    default to empty/0, so pre-existing shards and existing callers stay valid.
    """
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
        # Additive per-turn fields (issue #647): context occupancy + dispatch
        # origin. Old shards lack these keys; readers must tolerate their
        # absence. context_* are int-coerced so a bad value can't break json.dumps.
        "surface": surface or "",
        "agent": agent or "",
        "context_used": _coerce_int(context_used),
        "context_window": _coerce_int(context_window),
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
    model_source: object = None,
) -> None:
    """Append a token usage record to today's shard under
    ``<data home>/usage/tokens/YYYY-MM-DD.jsonl`` (synchronous).

    The ``provider`` field tags the source LLM backend (acp,
    claude_code, bedrock) so the dashboard chart can filter by provider.
    ``surface`` / ``agent`` tag the dispatch origin and resolved agent, and
    ``context_used`` / ``context_window`` record context-window occupancy — all
    additive and defaulted so existing callers stay valid.

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
    model_source: object = None,
) -> None:
    """Async variant: builds the record on-loop, offloads the file write.

    Called per agent turn (EVENT_COMPLETE) from the chat runner, which runs on
    the aiohttp event loop — the synchronous open/append previously blocked all
    co-resident coroutines for the IO window. These are best-effort analytics
    (no fsync, exceptions swallowed), so off-loop write loses no durability.
    See :func:`persist_token_record` for the ``surface`` / ``agent`` /
    ``context_used`` / ``context_window`` / ``model_source`` fields.
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
        )
        await asyncio.to_thread(_write_token_record, record, now)
    except Exception:
        logger.debug("Failed to persist token record for slot %s", slot_key, exc_info=True)


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
