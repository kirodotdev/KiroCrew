"""Session lifecycle, usage, search, approvals, and reset handlers."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider  # noqa: F811

from aiohttp import web

# circular import: handlers/__init__.py re-exports this module's handlers, so
# a `from ... import` of individual names would fail mid-cycle. `import ... as`
# binds via sys.modules and defers attribute access to call time, which also
# keeps tests' monkeypatching of handlers.redact_* effective (late binding).
import kiro_crew.dashboard.handlers as _h
from kiro_crew.acp.client import _resolve_kiro_bin_for_spawn
from kiro_crew.dashboard.handlers import kiro_usage_api
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_not_ready
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.history import INCOGNITO_MEMORY_MODES, SEARCH_MIN_CHARS, _archive_dir
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.mcp_discovery import (
    discover_servers_to_sync,
    register_servers_for_cc,
    sync_to_agent_config,
)
from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv
from kiro_crew.security import redact, redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import sanitize_string

logger = logging.getLogger(__name__)

_SHUTDOWN_TIMEOUT_SECS = 10


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


async def api_sessions_context(request: web.Request) -> web.Response:
    """GET /api/sessions/context — context usage for all active sessions."""
    state: DashboardState = request.app["state"]
    return web.json_response({"sessions": state.sessions.context_info()})


_health_cache: dict[str, dict] = {}
_health_cache_ts: float = 0.0
_health_lock: asyncio.Lock | None = None
_HEALTH_REFRESH_SECS = 15


async def api_sessions_health(request: web.Request) -> web.Response:
    """GET /api/sessions/health — slots flagged as stalled from log scan."""
    global _health_cache, _health_cache_ts, _health_lock
    if _health_lock is None:
        _health_lock = asyncio.Lock()
    now = time.monotonic()
    if now - _health_cache_ts > _HEALTH_REFRESH_SECS:
        async with _health_lock:
            # Re-check after acquiring lock (another request may have refreshed)
            if time.monotonic() - _health_cache_ts > _HEALTH_REFRESH_SECS:
                try:
                    from kiro_crew.dashboard import session_health

                    _health_cache = await asyncio.to_thread(session_health.compute_session_health)
                    _health_cache_ts = time.monotonic()
                except Exception:
                    logger.warning("session_health scan failed", exc_info=True)
                    _health_cache_ts = time.monotonic()
    return web.json_response({"stalled": _health_cache})


_usage_cache: dict[str, object] = {}
_usage_cache_ts: float = 0.0
_USAGE_REFRESH_SECS = 600  # background refresh every 10 min
_usage_fetching = False


def _safe_float(text: str) -> float | None:
    """Parse a float, returning None on malformed input instead of raising."""
    try:
        return float(text)
    except ValueError:
        return None


def _parse_usage(raw: str) -> dict[str, object]:
    """Parse structured fields from kiro-cli /usage output."""
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw)
    result: dict[str, object] = {"raw": ""}

    lines = clean.splitlines()
    usage_lines: list[str] = []
    capture = False
    for line in lines:
        if "Estimated Usage" in line:
            capture = True
        if capture:
            usage_lines.append(line)
    result["raw"] = "\n".join(usage_lines).strip()

    # Parse fields. First-wins on each field so a duplicate header or echoed
    # line later in the (untrusted) output can't overwrite a real value, and
    # malformed numbers skip the field via _safe_float rather than aborting.
    for line in usage_lines:
        if "resets on" in line and "resets" not in result:
            m = re.search(r"resets on (\S+)", line)
            if m:
                result["resets"] = m.group(1)
            if "|" in line:
                result["plan"] = line.rsplit("|", 1)[-1].strip()
        if "Credits used" in line and "credits_used" not in result:
            m = re.search(r"Credits used:\s*([\d.]+)", line)
            if m:
                v = _safe_float(m.group(1))
                if v is not None:
                    result["credits_used"] = v
        if "Est. cost" in line and "cost_usd" not in result:
            m = re.search(r"\$([\d.]+)", line)
            if m:
                v = _safe_float(m.group(1))
                if v is not None:
                    result["cost_usd"] = v
        if "covered in plan" in line and "credits_plan" not in result:
            m = re.search(r"\(([\d.]+)\s+of\s+([\d.]+)", line)
            if m:
                covered = _safe_float(m.group(1))
                plan = _safe_float(m.group(2))
                if covered is not None and plan is not None:
                    result["credits_covered"] = covered
                    result["credits_plan"] = plan
        if "billed at" in line and "overage_rate" not in result:
            m = re.search(r"\$([\d.]+)\s+per", line)
            if m:
                # Coerce to float so both sources emit one type for the
                # canonical shape and consumers never branch on source.
                rate = _safe_float(m.group(1))
                if rate is not None:
                    result["overage_rate"] = rate

    # Bonus / promotional credits are a separate pool kiro-cli lists in its own
    # section after the plan, e.g.:
    #     Bonus Credits:
    #       Welcome bonus: 386.34/500 (expires in 15 days)
    # This pool is drawn down BEFORE the plan, so while it lasts the plan meter
    # barely moves — which looked like a frozen counter until we surfaced it.
    # Capture the first bonus line (first-wins) so the dashboard can pool it into
    # the header total and show it in the credits modal.
    in_bonus = False
    for line in usage_lines:
        if "Bonus Credits" in line:
            in_bonus = True
            continue
        if not in_bonus or "bonus_limit" in result:
            continue
        m = re.search(r"([A-Za-z][A-Za-z0-9 ]*?):\s*([\d.]+)\s*/\s*([\d.]+)", line)
        if not m:
            continue
        used = _safe_float(m.group(2))
        limit = _safe_float(m.group(3))
        if used is not None and limit is not None and limit > 0:
            result["bonus_label"] = m.group(1).strip()
            result["bonus_used"] = used
            result["bonus_limit"] = limit
            exp = re.search(r"\(([^)]*expires[^)]*)\)", line, re.IGNORECASE)
            if exp:
                result["bonus_expires_label"] = exp.group(1).strip()
    return result


def _normalize_text_usage(parsed: dict[str, object]) -> dict[str, object]:
    """Convert the text-scrape parse result to the canonical usage shape.

    Emits the canonical usage shape the dashboard consumes so it never branches
    on source:
      credits_used = TOTAL used, credits_overage = overage above plan,
      credits_covered = in-plan portion, credits_plan = limit, percentage.

    In the raw text, "Credits used:" is the OVERAGE field (0 for org accounts,
    and absent entirely on kiro-cli 2.11.x), while "(X of Y covered in plan)" is
    the in-plan covered/limit. Total = covered + overage. Post-regression the
    text carries no overage, so this honestly reports covered==total.
    """
    covered = parsed.get("credits_covered")
    plan = parsed.get("credits_plan")
    if not isinstance(covered, (int, float)) or not isinstance(plan, (int, float)):
        # No usable credit plan — preserve whatever parsed (e.g. just {"raw": ...}).
        return dict(parsed)
    raw_used = parsed.get("credits_used")
    overage = float(raw_used) if isinstance(raw_used, (int, float)) else 0.0
    total = float(covered) + overage
    out: dict[str, object] = dict(parsed)
    out["credits_used"] = total
    out["credits_overage"] = overage
    out["credits_covered"] = float(covered)
    out["credits_plan"] = float(plan)
    out["percentage"] = round(total / plan * 100, 1) if plan else 0.0
    out["source"] = "text"
    return out


def _redact_strings(value: object) -> object:
    """Recursively redact credentials / exfil URLs from every string leaf.

    Walks dicts and lists so nested values cannot bypass redaction, used on
    untrusted kiro-cli output before it is cached and served to the dashboard.
    """
    if isinstance(value, str):
        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        return value
    if isinstance(value, dict):
        return {k: _redact_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_strings(v) for v in value]
    return value


def _cache_transient_failure() -> None:
    """Record a transient usage-fetch failure without blanking the pill.

    A timeout, an unexpected error, or a single unparseable scrape is transient.
    Overwriting a previously-good cache with ``{"available": False}`` on any of
    these hid the credit pill entirely for up to a full refresh interval — the
    "disappearing pill" bug. Instead, when we already hold a good value, keep it
    and flag it ``stale`` (the dashboard can dim it); only fall back to
    ``available: False`` when there is no prior value to show (e.g. a cold-start
    failure), preserving the original hide-on-no-data behavior. The definitive
    "kiro-cli absent" case still sets ``available: False`` directly at its call
    site — that is not transient.
    """
    global _usage_cache, _usage_cache_ts
    if _usage_cache.get("credits_plan") is not None:
        _usage_cache = {**_usage_cache, "stale": True}
    else:
        _usage_cache = {"available": False}
    _usage_cache_ts = time.time()


async def _fetch_usage_bg() -> None:
    """Background task: fetch usage and update cache."""
    global _usage_cache, _usage_cache_ts, _usage_fetching
    if _usage_fetching:
        return
    _usage_fetching = True
    proc = None
    sandbox_cleanup = None
    kiro_bin: str | None = None
    try:
        kiro_bin = await _resolve_kiro_bin_for_spawn()
        if not kiro_bin:
            # kiro-cli absent (non-Kiro provider): cache an unavailable marker so
            # the dashboard hides the credit pill instead of polling forever.
            _usage_cache = {"available": False}
            _usage_cache_ts = time.time()
            return
        # Primary source: the real GetUsageLimits API. It reads the live bearer
        # token kiro-cli maintains and returns the true used/limit/overage, so it
        # survives kiro-cli stdout format changes (the regression that dropped
        # the overage line). Runs on the subprocess pool (not the default
        # to_thread pool): the client makes blocking urllib calls that can hang
        # on DNS / a wedged TLS handshake, so they are isolated from the
        # maintenance/cron pools. Fails closed (returns None) so we fall through
        # to the text scrape rather than showing a fabricated number.
        api_usage = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), kiro_usage_api.fetch_usage_limits
        )
        if api_usage and api_usage.get("credits_plan") is not None:
            # API output is untrusted too: redact every string leaf before caching.
            api_usage = {k: _redact_strings(v) for k, v in api_usage.items()}
            _usage_cache = api_usage
            _usage_cache_ts = time.time()
            logger.info(
                "Kiro usage refreshed (api): %s / %s credits",
                api_usage.get("credits_used", "?"),
                api_usage.get("credits_plan", "?"),
            )
            return
        # Fallback: scrape kiro-cli /usage stdout. Lossy for org-managed accounts
        # on recent kiro-cli (no overage line), but the only source when the API
        # path is unavailable (no token / non-Kiro build).
        # Route through the OS-level sandbox, consistent with how the main agent
        # kiro-cli process is spawned (AcpClient._spawn -> wrap_argv).
        argv, sandbox_cleanup = wrap_argv(
            [kiro_bin, "chat", "--no-interactive", "--agent", "kirocrew-lite", "/usage"],
            mode="standard",
        )
        argv = cgroup_scope_argv(argv)  # cgroup DoS ceiling (Talos bdf0d7e5)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=resource_limit_preexec(),
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        raw = (out or err or b"").decode(errors="replace")
        parsed = _parse_usage(raw)
        if parsed.get("credits_plan") is not None:
            # Converge on the canonical shape (credits_used = total, explicit
            # credits_overage) so the dashboard never branches on source, then
            # redact credentials / exfil URLs from every string leaf before the
            # dict is cached and served (kiro-cli output is untrusted).
            parsed = _normalize_text_usage(parsed)
            parsed = {k: _redact_strings(v) for k, v in parsed.items()}
            _usage_cache = parsed
            _usage_cache_ts = time.time()
            logger.info(
                "Kiro usage refreshed (text): %s credits used",
                parsed.get("credits_used", "?"),
            )
        else:
            # No parseable credit plan this cycle (unrecognized /usage output,
            # or transient garbage). Keep the last good value (stale) rather than
            # blanking the pill; only hide when we have nothing to show.
            _cache_transient_failure()
    except asyncio.TimeoutError:
        # Transient hang — keep the last good value (stale) instead of blanking.
        logger.debug("Background usage fetch timed out")
        _cache_transient_failure()
    except Exception:
        logger.debug("Background usage fetch failed", exc_info=True)
        _cache_transient_failure()
    finally:
        # Always reap the subprocess on any exit path (timeout, error, or task
        # cancellation, which is a BaseException the excepts above don't catch)
        # so a leaked kiro-cli process can't hold the agent lock or keep burning
        # credit quota. kill() is non-blocking; the OS reaps the zombie.
        _usage_fetching = False
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                # Await termination so the asyncio transport + pipe FDs close
                # (otherwise they leak, and this runs on a timer). Bounded by
                # wait_for so a wedged process can't reintroduce an unbounded hang.
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        if sandbox_cleanup:
            try:
                os.remove(sandbox_cleanup)
            except OSError:
                pass


async def api_sessions_usage(request: web.Request) -> web.Response:
    """GET /api/sessions/usage — cached kiro credit usage (background refresh)."""
    # Same browser-storm guard as api_models: the /usage scrape shells out to
    # `kiro-cli chat --no-interactive ... /usage`, which auto-opens a browser
    # login while signed out. This endpoint is polled every 30s by the top-bar
    # credit pill, so an unauthenticated gateway spawned a browser every 30s.
    blocked = await reject_if_kiro_not_ready(request)
    if blocked is not None:
        return blocked
    now = time.time()
    if now - _usage_cache_ts > _USAGE_REFRESH_SECS:
        # Fire background fetch, return stale cache immediately
        state: DashboardState = request.app["state"]
        task = asyncio.create_task(_fetch_usage_bg())
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"usage": _usage_cache})


async def api_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions — list conversation session files.

    Query params:
      - ``limit``: max sessions to return (default 50, max 200)
      - ``offset``: skip first N sessions (default 0)
      - ``preview``: when truthy, attach a redacted last-message ``preview``
        to each returned session (bounded tail read; page-scoped so the
        default list stays a cheap metadata scan)

    Returns ``{sessions, total, has_more}`` for pagination.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"sessions": [], "total": 0, "has_more": False})
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(request.query.get("offset", "0"))
    except (TypeError, ValueError):
        offset = 0
    want_preview = (request.query.get("preview") or "").lower() in ("1", "true", "yes")
    all_sessions = state.conversation_log.list_sessions()
    total = len(all_sessions)
    page = all_sessions[offset : offset + limit]
    if want_preview:
        log = state.conversation_log

        def _attach_previews(sessions: list[dict]) -> None:
            for s in sessions:
                preview = log.last_message_preview(s.get("key", ""))
                if preview:
                    preview, _ = _h.redact_exfiltration_urls(preview)
                    preview, _ = _h.redact_credentials(preview)
                    s["preview"] = preview

        # Tail reads are sync file IO — keep them off the event loop.
        await asyncio.get_running_loop().run_in_executor(None, _attach_previews, page)
    return web.json_response(
        {
            "sessions": page,
            "total": total,
            "has_more": offset + limit < total,
        }
    )


_SUMMARIZE_MAX_SESSIONS = 8  # bound cost/latency: only the top-N get an LLM pass
_SUMMARIZE_MODEL = "claude-haiku-4.5"  # cheap/fast — a one-liner needs no heavy model
_SUMMARIZE_MSG_LIMIT = 12  # messages fed to the summarizer per session
_SUMMARIZE_TIMEOUT_SECS = 30  # per-session deadline so one stalled prompt can't pin the shared _bg session
_SUMMARIZE_PROMPT = (
    "Summarize the following conversation in ONE terse line (max 18 words), "
    "describing what the user and assistant are working on. No preamble, no "
    "quotes, no trailing period. If the topic is unclear, reply exactly SKIP.\n\n"
    "===== CONVERSATION =====\n"
    "{transcript}\n"
    "===== END ====="
)


def _build_summary_prompt(messages: list[dict]) -> str | None:
    """Build a one-line-summary prompt from a session's recent messages."""
    lines: list[str] = []
    for m in messages[:_SUMMARIZE_MSG_LIMIT]:
        role = m.get("role", "")
        content = " ".join(str(m.get("content", "")).split())
        if role in ("user", "assistant") and content:
            lines.append(f"{role}: {content[:300]}")
    if not lines:
        return None
    return _SUMMARIZE_PROMPT.format(transcript="\n".join(lines))


async def _summarize_one(state: DashboardState, key: str) -> str:
    """Generate a one-line LLM summary for a single session. "" on any failure.

    Mirrors dashboard.chat_title._generate_title_via_kiro: uses an ephemeral
    background session on the cheap/fast model and destroys it in a finally.
    Best-effort — every failure path returns "" so the caller falls back to the
    session's stored title.
    """
    log = state.conversation_log
    if not log:
        return ""
    loop = asyncio.get_running_loop()
    # get_metadata + recent do synchronous full-file reads (read_text + per-line
    # JSON parse, up to 2MB). Offload to the executor so a batch of large session
    # files never freezes the gateway event loop — mirrors api_sessions above.
    meta = await loop.run_in_executor(None, log.get_metadata, key)
    # Defense in depth: never summarize an incognito/temporary session even if a
    # caller somehow passes its key.
    if str(meta.get("memory_mode", "")).lower() in INCOGNITO_MEMORY_MODES:
        return ""
    # Cache: a summary persisted in a sidecar file is reusable as long as the
    # session file hasn't changed since it was generated. session_mtime advances
    # only on real message appends (preserved across metadata writes), so it is a
    # cheap, exact staleness signal — a repeat list_sessions(summarize=true) for
    # an unchanged session pays zero LLM cost. The cache lives in a sidecar
    # (never the session JSONL) so summarizing an *active* session never rewrites
    # its log and cannot lose a concurrently-appended message.
    sig = await loop.run_in_executor(None, log.session_mtime, key)
    cached = await loop.run_in_executor(None, log.get_cached_summary, key)
    if cached:
        return str(cached)
    messages = await loop.run_in_executor(
        None,
        functools.partial(
            log.recent, key, max_messages=_SUMMARIZE_MSG_LIMIT, roles={"user", "assistant"}
        ),
    )
    prompt = _build_summary_prompt(messages)
    if not prompt:
        return ""
    try:
        text = await run_bg_oneliner(
            state.sessions, prompt, model=_SUMMARIZE_MODEL, timeout=_SUMMARIZE_TIMEOUT_SECS
        )
    except Exception:
        logger.debug("Session summary generation failed for %s", key, exc_info=True)
        return ""
    summary = text.strip().strip('"').strip("'").strip(".")
    if not summary or summary.upper() == "SKIP":
        return ""
    summary, _ = redact_exfiltration_urls(summary)
    summary, _ = redact_credentials(summary)
    summary = summary[:200]
    # Persist for reuse in a sidecar cache (best-effort; keyed by the mtime we
    # observed above so a concurrent append invalidates it on the next call).
    # Writing the sidecar never touches the session JSONL, so it cannot race a
    # concurrent append or reorder list_sessions.
    if sig is not None:
        try:
            await loop.run_in_executor(
                None,
                functools.partial(log.set_cached_summary, key, summary, sig),
            )
        except Exception:
            logger.debug("Failed to persist summary cache for %s", key, exc_info=True)
    return summary


async def api_sessions_summarize(request: web.Request) -> web.Response:
    """POST /api/sessions/summarize — one-line LLM summaries for given sessions.

    Body: ``{"keys": ["<session_key>", ...]}``. Only the first
    ``_SUMMARIZE_MAX_SESSIONS`` keys are summarized (cost/latency bound); the
    rest are silently skipped and the caller falls back to their titles.
    Returns ``{"summaries": {key: one_line_summary}}`` — keys that produced no
    usable summary are omitted. Best-effort: a per-session failure never fails
    the whole request.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"summaries": {}})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list):
        return web.json_response({"error": "keys must be a list"}, status=400)
    # Dedupe while preserving order, drop non-strings, then bound the count.
    seen: set[str] = set()
    ordered: list[str] = []
    for k in keys:
        if isinstance(k, str) and k and k not in seen:
            seen.add(k)
            ordered.append(k)
    ordered = ordered[:_SUMMARIZE_MAX_SESSIONS]

    summaries: dict[str, str] = {}
    for key in ordered:
        if not state.conversation_log.has_log(key):
            continue
        summary = await _summarize_one(state, key)
        if summary:
            summaries[key] = summary
    return web.json_response({"summaries": summaries})


async def api_sessions_search(request: web.Request) -> web.Response:
    """GET /api/sessions/search — content search over session JSONL files.

    Query params:
      - ``q``: search string (min 2 chars; empty returns no results)
      - ``limit``: max results (default 50, max 200)

    Returns ``{sessions}`` — same metadata shape as:func:`api_sessions`.
    Session titles may be LLM-generated and are redacted before return.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"sessions": []})
    q = sanitize_string(request.query.get("q", "")).strip()[:256]
    if len(q) < SEARCH_MIN_CHARS:
        return web.json_response({"sessions": []})
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50
    sessions = await asyncio.get_running_loop().run_in_executor(
        None, state.conversation_log.search_sessions, q, limit
    )
    for s in sessions:
        title = s.get("title")
        if title:
            title, _ = _h.redact_exfiltration_urls(title)
            title, _ = _h.redact_credentials(title)
            s["title"] = title
        snip = s.get("snippet")
        if snip:
            snip, _ = _h.redact_exfiltration_urls(snip)
            snip, _ = _h.redact_credentials(snip)
            s["snippet"] = snip
    return web.json_response({"sessions": sessions})


async def api_session_detail(request: web.Request) -> web.Response:
    """GET /api/sessions/{key} — return messages for a session."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    if not state.conversation_log:
        return web.json_response([])
    return web.json_response(state.conversation_log.read_messages(key))


async def api_session_delete(request: web.Request) -> web.Response:
    """DELETE /api/sessions/{key} — permanently delete a history session."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)
    # delete_session enters _locked (flock acquire + os.close); offload off the
    # loop so a wedged cross-process peer can't freeze chat/WS/heartbeat.
    ok = await asyncio.to_thread(state.conversation_log.delete_session, key)
    if ok:
        try:
            await _remove_slot_for_history_key(state, key)
        except Exception:
            logger.warning("cleanup failed for session %s", key, exc_info=True)
        state.push_slots_update()
        state.push_refresh("history")
    return web.json_response({"ok": ok})


async def _remove_slot_for_history_key(state: DashboardState, key: str) -> None:
    """Remove the active chat slot corresponding to a history key.

    Slot keys may be the raw history key (``dashboard_chat-X-TS`` when
    resumed from history) or the stripped form (``chat-X-TS`` for
    sessions that were never closed and resumed).  Try the exact key
    first, then the stripped variant.  Also kills the kiro-cli session
    to prevent orphaned processes.
    """
    from kiro_crew.dashboard.chat import _history_key_for  # circular import  # noqa: F811

    slot = state._slots.pop(key, None)
    if not slot:
        stripped = key
        if stripped.startswith("dashboard:"):
            stripped = stripped[len("dashboard:") :]
        while stripped.startswith("dashboard_"):
            stripped = stripped[len("dashboard_") :]
        slot = state._slots.pop(stripped, None)
    if not slot:
        # Reverse: history key has no prefix, but slot was stored with one
        slot = state._slots.pop("dashboard_" + key, None)
    if slot:
        # A pending ask_question is owned by the slot's running turn, but its
        # future lives in DashboardState rather than on slot.task. History
        # deletion tears down that task and provider directly, bypassing the
        # normal stop/delete handlers; resolve the wait first so the MCP HTTP
        # request returns and its finally block retracts the now-stale card.
        cancelled = state.cancel_questions_for_slot(slot.key)
        if cancelled:
            logger.info(
                "History delete: cancelled %d pending question(s) on slot %s",
                cancelled,
                slot.key,
            )
    if slot and slot.running and slot.task is not None:
        slot.task.cancel()
        try:
            await asyncio.wait_for(slot.task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    # Kill the kiro-cli subprocess to free resources
    if slot:
        try:
            await state.sessions.destroy(_history_key_for(key))
        except Exception:
            pass


async def api_sessions_clear(request: web.Request) -> web.Response:
    """DELETE /api/sessions — permanently delete closed history sessions only.

    Skips sessions currently open in the sidebar (any slot in
    ``state._slots``) and sessions with ``pinned=True`` on disk.
    Bulk-archiving open unpinned/idle sessions is out of scope here.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)

    from kiro_crew.dashboard.chat import _history_key_for  # noqa: F811

    protected: set[str] = set()
    for slot in state._slots.values():
        hk = _history_key_for(slot.key)
        protected.add(hk)
        protected.add(hk.replace(":", "_", 1))

    sessions = state.conversation_log.list_sessions()
    count = 0
    skipped = 0
    failed = 0
    cleanup_tasks = []
    for s in sessions:
        key = s["key"]
        if key in protected:
            skipped += 1
            continue
        try:
            meta = state.conversation_log.get_metadata(key)
        except Exception:
            logger.warning(
                "api_sessions_clear: unreadable metadata for %s, skipping", key, exc_info=True
            )
            skipped += 1
            continue
        if not isinstance(meta, dict):
            skipped += 1
            continue
        if meta.get("pinned"):
            skipped += 1
            continue
        try:
            # delete_session enters _locked (flock + os.close) — offload off the
            # event loop so a wedged peer can't stall the bulk clear on it.
            if await asyncio.to_thread(state.conversation_log.delete_session, key):
                cleanup_tasks.append(_remove_slot_for_history_key(state, key))
                count += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            logger.warning("api_sessions_clear: delete raised for %s", key, exc_info=True)
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    if count:
        state.push_slots_update()
        state.push_refresh("history")
    logger.info("api_sessions_clear: cleared=%d skipped=%d failed=%d", count, skipped, failed)
    return web.json_response(
        {"ok": failed == 0, "cleared": count, "skipped": skipped, "failed": failed}
    )


# ── Approvals ──


async def api_approvals(request: web.Request) -> web.Response:
    """GET /api/approvals — list pending tool approvals."""
    state: DashboardState = request.app["state"]
    return web.json_response(list(state._pending_approvals.values()))


async def api_approval_resolve(request: web.Request) -> web.Response:
    """POST /api/approvals/{id}/{action} — approve or reject."""
    state: DashboardState = request.app["state"]
    approval_id = request.match_info["id"]
    action = request.match_info["action"]
    if action not in ("approve", "reject"):
        return web.json_response({"error": "invalid action"}, status=400)
    ok = state.resolve_approval(approval_id, action == "approve")
    if not ok:
        return web.json_response({"error": "not found or expired"}, status=404)
    return web.json_response({"ok": True})


async def api_session_keepalive(request: web.Request) -> web.Response:
    """POST /api/session-keepalive — refresh activity timestamp on the
    session's provider so idle-detection/stale-checks don't SIGTERM a
    session that's intentionally blocking in a long-running MCP tool
    (e.g. the `wait` tool).

    Authenticated via X-Internal-Secret; session is selected via the
    X-Session-Key header that all MCP subprocesses already send.
    """
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        return web.json_response({"error": "X-Session-Key required"}, status=400)
    provider = state.sessions.get_provider(session_key)
    if provider is None:
        return web.json_response({"error": "session not found"}, status=404)
    try:
        provider.touch_activity()
    except Exception as exc:
        logger.debug("touch_activity failed for %s: %s", session_key, exc)
        return web.json_response({"error": "touch failed"}, status=500)
    return web.json_response({"ok": True})


async def api_session_tool_policy(request: web.Request) -> web.Response:
    """GET /api/session-tool-policy — return managedToolPolicy for the
    calling session's agent.

    Used by managed MCP servers (kirocrew-core, kirocrew-cron) to filter
    their tool lists per-agent.  Returns {"exclude": [...]} on success,
    or 400/404 when the session cannot be identified (deny-by-default:
    callers that cannot prove identity get an error, not an empty policy).
    Authenticated via X-Internal-Secret + X-Session-Key.
    """
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        _sel().log_api_access(
            caller="unknown",
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources="missing X-Session-Key",
        )
        return web.json_response({"error": "X-Session-Key required"}, status=400)

    # Resolve agent name from session
    agent_name = ""

    # Dashboard slot
    if session_key.startswith("dashboard:"):
        slot_key = session_key[len("dashboard:"):]
        slot = state.get_slot(slot_key)
        if slot:
            agent_name = slot.agent
    # Subagent — look up in SubagentManager
    elif session_key.startswith("subagent:"):
        if state.subagents:
            subagent_id = session_key[len("subagent:"):]
            info = state.subagents.get(subagent_id)
            if info:
                agent_name = info.agent
    # Cron — fall through to session manager lookup below
    elif session_key.startswith("cron:"):
        pass

    # Also check session manager for agent name
    if not agent_name and state.sessions:
        agent_name = state.sessions.get_agent(session_key)

    if not agent_name:
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources="agent not resolved",
        )
        return web.json_response({"error": "agent not resolved"}, status=404)

    # Sanitize agent_name to prevent path traversal
    if "/" in agent_name or "\\" in agent_name or ".." in agent_name:
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources=f"invalid agent_name={agent_name!r}",
        )
        return web.json_response({"error": "invalid agent name"}, status=400)

    # Read agent config from disk
    agent_path = Path.home() / ".kiro" / "agents" / f"{agent_name}.json"
    if not agent_path.is_file():
        return web.json_response({})

    try:
        config = json.loads(agent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return web.json_response({})

    policy = config.get("managedToolPolicy", {})
    if not isinstance(policy, dict):
        return web.json_response({})

    _sel().log_api_access(
        caller=session_key,
        operation="session_tool_policy",
        outcome="ok",
        source="dashboard",
        resources=f"agent={agent_name}",
    )
    return web.json_response(policy)


async def _reset_all_sessions(request: web.Request) -> int:
    """Reset all active sessions so they pick up config changes.

    Reloads provider factory (handles provider switch ACP→CC or vice versa),
    shuts down all active sessions AND drains the warm pool (pre-spawned
    processes loaded the old MCP config at spawn time).
    New sessions cold-start on next message.
    Returns the number of sessions reset.
    """
    state: DashboardState = request.app["state"]
    sessions = state.sessions

    # Reload factory so provider switch takes effect immediately
    await sessions.reload_provider_factory()

    # Pop all active sessions
    providers: list[LLMProvider] = []
    count = sessions.count
    if count > 0:
        providers = await sessions.drain_all_providers()

    # Drain warm pool — pre-spawned processes have stale MCP config
    pool_providers = await sessions.drain_warm_pool()
    providers.extend(pool_providers)

    if count > 0 or pool_providers:
        logger.info(
            "Reset %d session(s) + %d pool process(es) after config change",
            count, len(pool_providers),
        )

    state.broadcast_ws("sessions_restarting", {"status": "restarting"})

    async def _background_restart() -> None:
        if providers:

            async def _safe_shutdown(p: LLMProvider) -> None:
                import kiro_crew.dashboard.handlers as _h  # noqa: F811

                _timeout = _SHUTDOWN_TIMEOUT_SECS
                try:
                    await asyncio.wait_for(p.shutdown(), timeout=_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Session shutdown hung past %.1fs; forcing kill",
                        _timeout,
                    )
                    try:
                        _h._sync_kill_provider(p)
                    except Exception:
                        logger.exception("Force-kill fallback also failed for %r", p)
                except Exception:
                    pass

            await asyncio.gather(*[_safe_shutdown(p) for p in providers])

        sessions._pool_started = False
        await sessions.start_pool(blocking=False)
        logger.info("Background session restarted")
        state.push_refresh("agents")
        state.push_slots_update()
        state.broadcast_ws("sessions_restarting", {"status": "ready"})

    task = asyncio.create_task(_background_restart())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    return count


async def api_sessions_restart(request: web.Request) -> web.Response:
    """POST /api/sessions/restart — reset all kiro-cli sessions.

    Forces fresh context injection on the next message. Use after editing
    memory, lessons, or skills to pick up changes immediately.

    Also syncs MCP servers from mcp.json → kirocrew.json so newly
    installed servers (e.g. via AIM) are picked up on restart.
    """
    # Sync MCP servers before restarting so new installs take effect.
    # Run in thread — discover/sync do blocking file I/O and subprocess calls.
    # Cap at 30s so a hung kiro-cli subprocess doesn't stall the restart.
    synced = 0
    try:

        async def _sync() -> int:
            to_sync = await asyncio.to_thread(discover_servers_to_sync)
            if to_sync:
                ok: bool = await asyncio.to_thread(sync_to_agent_config, to_sync)
                # Register for CC unconditionally (CC uses its own .mcp.json)
                await asyncio.to_thread(register_servers_for_cc, to_sync)
                if ok:
                    return len(to_sync)
            return 0

        synced = await asyncio.wait_for(_sync(), timeout=30)
    except Exception:
        logger.warning("MCP server sync failed before restart", exc_info=True)
    count = await _reset_all_sessions(request)
    return web.json_response({"ok": True, "sessions_reset": count, "mcp_synced": synced})


async def api_session_archive_list(request: web.Request) -> web.Response:
    """GET /api/session/archive?key=... — list archive files for a session key."""
    from typing import Any

    from kiro_crew.history import _archive_dir, _safe_key

    key = request.query.get("key", "").strip()
    adir = _archive_dir()
    if not adir.exists():
        return web.json_response({"archives": []})
    prefix = f"{_safe_key(key)}__" if key else ""

    def _collect() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for p in adir.glob(f"{prefix}*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            stem = p.stem
            # Archive filenames use '__' delimiter: {safekey}__{stamp}.jsonl
            sep = stem.find("__")
            safekey = stem[:sep] if sep >= 0 else stem
            stamp = stem[sep + 2 :] if sep >= 0 else ""
            items.append(
                {
                    "name": p.name,
                    "key": safekey,
                    "stamp": stamp,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    items = await asyncio.to_thread(_collect)
    return web.json_response({"archives": items})


async def api_session_archive_read(request: web.Request) -> web.Response:
    """GET /api/session/archive/{name} — read a single archive file as JSONL text."""
    name = request.match_info.get("name", "")
    if not name.endswith(".jsonl"):
        return web.json_response({"error": "invalid archive name"}, status=400)
    adir = _archive_dir().resolve()
    try:
        resolved = (adir / name).resolve()
    except (OSError, RuntimeError, ValueError):
        return web.json_response({"error": "invalid archive name"}, status=400)
    # Canonical path check: file must be a direct child of the archive dir.
    if resolved.parent != adir:
        return web.json_response({"error": "invalid archive name"}, status=400)

    def _read_capped(p: Path, limit: int = 250_000) -> str:
        with p.open(encoding="utf-8") as f:
            data = f.read(limit)
        # Truncate at last newline to keep NDJSON valid.
        if len(data) == limit:
            nl = data.rfind("\n")
            if nl > 0:
                data = data[: nl + 1]
        return data

    try:
        raw = await asyncio.to_thread(_read_capped, resolved)
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read archive %s: %s", name, exc)
        return web.json_response({"error": "unreadable archive"}, status=422)
    # Archives contain LLM output; redact credentials and exfiltration URLs before serving.
    redacted = await asyncio.to_thread(
        lambda: redact(raw)
    )
    return web.Response(text=redacted, content_type="application/x-ndjson")
