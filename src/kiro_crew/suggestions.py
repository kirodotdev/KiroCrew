"""Contextual prompt suggestions — pre-computed via background LLM."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.context import ContextBuilder
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

# Regenerate suggestions every 30 minutes
_REFRESH_INTERVAL_SECS = 30 * 60

# The fixed vocabulary the dashboard maps to an icon. Anything else the model
# returns collapses to "general", so a new kind is a change here AND in the
# welcome view's icon map.
SUGGESTION_KINDS = ("code", "review", "ops", "tasks", "write", "research", "schedule", "general")
_DEFAULT_KIND = "general"

Suggestion = dict[str, str]

# Fallback suggestions when LLM is unavailable or context is empty
_FALLBACK_SUGGESTIONS: list[Suggestion] = [
    {"text": "Summarize my recent activity", "kind": "general"},
    {"text": "Search my code for usage examples", "kind": "research"},
    {"text": "Summarize this week's chat activity", "kind": "general"},
    {"text": "Help me write a design doc", "kind": "write"},
    {"text": "Review my latest changes", "kind": "review"},
    {"text": "What should I work on next?", "kind": "tasks"},
]


def _fallback() -> list[Suggestion]:
    return [dict(s) for s in _FALLBACK_SUGGESTIONS]


_PROMPT_TEMPLATE = """\
You are generating contextual prompt suggestions for a developer assistant dashboard.

Based on the user's current context below, generate 4-6 short, actionable prompt suggestions \
that the user might want to ask right now. Each suggestion should be a single sentence \
(under 60 characters) that the user can click to start a conversation.

Consider:
- What they were working on recently (projects, sessions)
- Their preferences and habits
- Time of day and day of week
- Pending tasks or follow-ups implied by recent activity

Current context:
---
{context}
---

Tag each suggestion with exactly one kind from this list:
code (write or change code), review (review a PR or changes), ops (pipelines, builds, \
deploys, alarms), tasks (tickets, todos, triage), write (docs, notes, messages), \
research (search, investigate, explain), schedule (reminders, recurring jobs), \
general (anything else).

Respond with ONLY a JSON array of objects with "text" and "kind" keys. No explanation, \
no markdown fences.
Example: [{"text": "Continue refactoring auth module", "kind": "code"}, \
{"text": "Review yesterday's changes", "kind": "review"}, \
{"text": "Draft the release notes", "kind": "write"}]
"""


@dataclass
class SuggestionsCache:
    """Holds pre-computed suggestions with a timestamp."""

    suggestions: list[Suggestion] = field(default_factory=_fallback)
    generated_at: float = 0.0
    # LoopBoundLock, not asyncio.Lock: the cache is stored on the
    # long-lived DashboardState, which outlives any single event loop.
    _lock: LoopBoundLock = field(default_factory=LoopBoundLock, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)  # type: ignore[type-arg]


def _build_context(state: DashboardState) -> str:
    """Assemble context for the suggestions prompt from memory and recent activity."""
    parts: list[str] = []

    # Active workspace memory
    try:
        # The GLOBAL store by name, not by omission. This surface summarizes the
        # operator's own memory for a dashboard panel, so it stays on the v1
        # path deliberately rather than inheriting whichever crew spoke last.
        memory = ContextBuilder.get_memory_for(memory_store=DEFAULT_MEMORY_STORE)
        prefs = memory.read_preferences()
        if prefs and prefs.strip() != "# User Preferences\n\n<!-- Learned from conversations -->":
            parts.append(f"## User Preferences\n{prefs[:2000]}")

        projects = memory.read_projects()
        if projects and projects.strip() != "# Active Projects\n\n<!-- Current work context -->":
            parts.append(f"## Active Projects\n{projects[:3000]}")

        # Recent history (last 2 days)
        recent_history = memory.read_recent_history(days=2)
        if recent_history:
            parts.append(f"## Recent Activity\n{recent_history[:4000]}")
    except Exception:
        logger.debug("Failed to read memory for suggestions", exc_info=True)

    # Recent session titles and last messages
    try:
        if state.conversation_log:
            sessions = state.conversation_log.list_sessions()
            if sessions:
                session_parts: list[str] = []
                for s in sessions[:5]:
                    title = s.get("title", "")
                    key = s.get("key", "")
                    if not key:
                        continue
                    line = f"- **{title or key}**"
                    try:
                        recent = state.conversation_log.recent(key, max_messages=6)
                        user_msgs = [
                            m["content"][:150]
                            for m in recent
                            if m.get("role") == "user" and m.get("content")
                        ][-3:]
                        if user_msgs:
                            line += "\n" + "\n".join(f"  - User: {msg}" for msg in user_msgs)
                    except Exception:
                        pass
                    session_parts.append(line)
                if session_parts:
                    parts.append("## Recent Sessions\n" + "\n".join(session_parts))
    except Exception:
        logger.debug("Failed to read sessions for suggestions", exc_info=True)

    # Cron jobs (what's scheduled)
    try:
        cron_jobs = state.crons.list_jobs()
        if cron_jobs:
            cron_names = [f"- {j.name}" for j in cron_jobs[:5]]
            parts.append("## Active Cron Jobs\n" + "\n".join(cron_names))
    except Exception:
        logger.debug("Failed to read crons for suggestions", exc_info=True)

    # Time context
    now = datetime.now()
    parts.append(f"## Current Time\n{now.strftime('%A, %B %d %Y at %H:%M')}")

    return "\n\n".join(parts)


def _coerce_suggestion(item: object) -> Suggestion | None:
    """One parsed item -> {text, kind}; a bare string (older prompt shape) is kind general."""
    if isinstance(item, str):
        text, kind = item, _DEFAULT_KIND
    elif isinstance(item, dict) and isinstance(item.get("text"), str):
        text = item["text"]
        raw_kind = item.get("kind")
        kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else _DEFAULT_KIND
    else:
        return None
    text = text.strip()
    if not text or len(text) > 80:
        return None
    return {"text": text, "kind": kind if kind in SUGGESTION_KINDS else _DEFAULT_KIND}


def _parse_suggestions(text: str) -> list[Suggestion]:
    """Parse LLM response into a list of {text, kind} suggestions."""
    text = text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            parsed = [s for s in (_coerce_suggestion(item) for item in result) if s]
            return parsed[:6]
    except (json.JSONDecodeError, TypeError):
        pass

    logger.warning("Failed to parse suggestions response: %s", text[:200])
    return []


def _redact_suggestions(suggestions: list[Suggestion]) -> list[Suggestion]:
    """Apply security redaction to each suggestion's text."""
    result: list[Suggestion] = []
    for s in suggestions:
        text, _ = redact_exfiltration_urls(s["text"])
        text, _ = redact_credentials(text)
        result.append({"text": text, "kind": s["kind"]})
    return result


async def generate_suggestions(state: DashboardState) -> list[Suggestion]:
    """Generate suggestions using the background kiro-cli session."""
    # _build_context() calls list_sessions() + recent() — O(all sessions) disk IO.
    # Offload to keep the event loop responsive (same pattern as this PR's other
    # two offload sites in sessions.py).
    context = await asyncio.to_thread(_build_context, state)
    if not context or len(context) < 50:
        logger.debug("Insufficient context for suggestions — using fallback")
        return _fallback()

    prompt = _PROMPT_TEMPLATE.replace("{context}", context)

    # run_bg_oneliner owns the shared background-session skeleton (acquire _bg,
    # reject + SEL-audit any tool call, drive the event loop, destroy in finally)
    # and its reactive rejected-model fallback — so a partition that does not
    # serve "auto" (e.g. GovCloud) retries once with an advertised model instead
    # of failing permanently. Best-effort: on any error fall back to the static
    # suggestions rather than surfacing it.
    try:
        text = await run_bg_oneliner(state.sessions, prompt, sel_source="suggestions", timeout=60)
    except Exception:
        logger.warning("Suggestions generation failed", exc_info=True)
        return _fallback()

    suggestions = _parse_suggestions(text)
    if suggestions:
        suggestions = _redact_suggestions(suggestions)
        logger.info("Generated %d suggestions", len(suggestions))
        return suggestions

    return _fallback()


async def refresh_suggestions(state: DashboardState, cache: SuggestionsCache) -> None:
    """Background task: regenerate suggestions."""
    async with cache._lock:
        try:
            suggestions = await generate_suggestions(state)
            cache.suggestions = suggestions
            cache.generated_at = time.time()
        except Exception:
            logger.warning("Suggestions generation failed", exc_info=True)


async def maybe_refresh(state: DashboardState, cache: SuggestionsCache) -> None:
    """Trigger a background refresh if suggestions are stale."""
    now = time.time()
    if now - cache.generated_at < _REFRESH_INTERVAL_SECS:
        return
    if cache._lock.locked():
        return

    # Fire and forget
    task = asyncio.create_task(refresh_suggestions(state, cache))
    cache._task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def get_suggestions_cache(state: DashboardState) -> SuggestionsCache:
    """Get or create the suggestions cache on the state object."""
    if not hasattr(state, "_suggestions_cache"):
        state._suggestions_cache = SuggestionsCache()  # type: ignore[attr-defined]
    return state._suggestions_cache  # type: ignore[attr-defined]


# ── HTTP Handler ──


async def api_suggestions(request: web.Request) -> web.Response:
    """GET /api/suggestions — return pre-computed contextual suggestions.

    Query params:
        force=1  — force a fresh generation (ignores cache age)
    """
    state: DashboardState = request.app["state"]
    cache = get_suggestions_cache(state)
    force = request.query.get("force") == "1"

    if force:
        try:
            await asyncio.wait_for(refresh_suggestions(state, cache), timeout=45)
        except (asyncio.TimeoutError, Exception):
            pass
    elif cache.generated_at == 0:
        # Never generated yet — wait for result
        if cache._lock.locked() and cache._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(cache._task), timeout=45)
            except (asyncio.TimeoutError, Exception):
                pass
        if cache.generated_at == 0 and not cache._lock.locked():
            try:
                await asyncio.wait_for(refresh_suggestions(state, cache), timeout=45)
            except (asyncio.TimeoutError, Exception):
                pass
    else:
        await maybe_refresh(state, cache)

    return web.json_response(
        {
            "suggestions": cache.suggestions,
            "generated_at": cache.generated_at,
            "stale": (
                (time.time() - cache.generated_at) > _REFRESH_INTERVAL_SECS
                if cache.generated_at
                else True
            ),
        }
    )
