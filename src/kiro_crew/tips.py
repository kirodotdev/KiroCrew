"""Feature Tips — memory-personalized feature discovery with weighted-random selection.

Design: Joe's cadence-gate + glow + snooze model (2026-07).
Selection uses weighted-random newer-biased pick (recency_decay ** rank).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.context import ContextBuilder
from kiro_crew.platform_compat import restrict_to_owner
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

# Regenerate tips every 6 hours
_REFRESH_INTERVAL_SECS = 6 * 60 * 60

# Doc selection is allowlist-based (see kiro_crew.tips_allowlist) — internal
# architecture/incident docs must never surface as user-facing tips.
_MAX_GENERATED_TIPS = 8

_PROMPT_TEMPLATE = """\
You are generating personalized feature tips for a developer assistant called KiroCrew.

Based on the user context and the feature catalog below, generate {max_tips} tips \
for features the user would benefit from but may not know about.

IMPORTANT guidance:
1. Read the provided user context and recent memory carefully.
2. PREFER tips that are RELEVANT to what the user has been doing recently — if they \
work with cron jobs daily, tip about cron features first.
3. BUT note some features are context-independent (theme, split view, keyboard shortcuts) \
and may be suggested regardless of recent activity.

TONE — this is the most important rule. The reader is a developer mid-task; write \
like the product documentation, not like marketing:
- State plainly what the feature does and when it is useful. Nothing else.
- NO superlatives or hype ("powerful", "amazing", "supercharge", "game-changing", \
"effortlessly", "unlock", "transform", "revolutionize").
- NO exclamation marks. NO emoji. NO rhetorical questions ("Did you know...?", \
"Tired of...?").
- Do not oversell outcomes. "Runs independent tasks in parallel" is right; \
"Get results 10x faster" is wrong.
- Model the register on these real doc one-liners:
  "Schedule recurring tasks — 'every weekday at 9am give me a pipeline briefing'"
  "Spawn parallel background workers for fan-out research and multi-package work"
  "Persistent preferences, project context, and learned corrections across sessions"

Each tip must have:
- id: a short kebab-case identifier (e.g. "cron-scheduling-tip")
- title: the feature's plain name or its core action, under 60 chars \
(e.g. "Schedule recurring reports", not "Never miss a beat with cron!")
- feature: the feature name from the catalog
- body: 1-2 sentences stating what it does, ideally with one concrete usage example
- why: 1 factual sentence connecting it to THIS user's actual recent workflow \
(name the specific activity; if the connection is weak, pick a different feature \
rather than inventing one)
- doc: the doc filename from the catalog
- cta_prompt: a ready-to-send chat message demonstrating the feature

Feature catalog:
---
{catalog}
---

User context:
---
{context}
---

Previously shown tip IDs (avoid repeating): {shown_ids}
Dismissed tip IDs (never suggest): {dismissed_ids}

Respond with ONLY a JSON array. No explanation, no markdown fences.
"""


# ── Catalog ──


@dataclass
class CatalogEntry:
    """A feature discovered from docs/*.md."""

    feature: str  # H1 title (without #)
    summary: str  # first paragraph
    doc: str  # filename (e.g. 'cron-and-scheduling.md')
    mtime: float = 0.0  # file mtime for recency ordering


def _scan_docs_catalog() -> list[CatalogEntry]:
    """Scan kiro_crew/docs/*.md for H1 + first paragraph."""
    docs_dir = Path(__file__).parent / "docs"
    entries: list[CatalogEntry] = []
    if not docs_dir.is_dir():
        return entries
    for md in sorted(docs_dir.glob("*.md")):
        if md.name not in TIP_DOC_ALLOWLIST:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
            mtime = md.stat().st_mtime
        except OSError:
            continue
        # Extract H1
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        if not m:
            continue
        title = m.group(1).strip()
        # First non-empty paragraph after H1
        rest = text[m.end():].lstrip("\n")
        para = ""
        for block in rest.split("\n\n"):
            stripped = block.strip()
            if stripped and not stripped.startswith("#"):
                para = " ".join(stripped.split())
                break
        if not para:
            continue
        entries.append(CatalogEntry(feature=title, summary=para[:300], doc=md.name, mtime=mtime))
    return entries


# ── State ──


@dataclass
class TipsState:
    """Persistent tips state saved to disk."""

    shown: dict[str, int] = field(default_factory=dict)  # tip_id -> count
    dismissed: list[str] = field(default_factory=list)  # permanent ack (by tip id)
    # Permanent ack by catalog doc: LLM-generated tips invent their own ids on
    # every regeneration, so id-only matching lets a dismissed feature resurface
    # under a fresh id (Codex round-15). Doc is the stable feature identity.
    dismissed_docs: list[str] = field(default_factory=list)
    snoozed: dict[str, float] = field(default_factory=dict)  # tip_id -> snooze_ts
    # Snooze by stable doc identity: generated ids drift across regenerations,
    # so id-only snooze lets the same feature resurface under a fresh id well
    # before the 48h window expires -- same drift dismissals fixed in round-15
    # (Codex round-28).
    snoozed_docs: dict[str, float] = field(default_factory=dict)  # doc -> snooze_ts
    opted_out: bool = False
    last_generated: float = 0.0
    last_shown_ts: float = 0.0  # cadence gate timestamp (set on feedback, not on serve)
    tips: list[dict] = field(default_factory=list)  # type: ignore[type-arg]
    offered: dict | None = field(default=None)  # type: ignore[type-arg]  # currently offered tip dict
    # id -> doc mapping recorded when a tip is shown: 'shown' clears offered
    # and maybe_refresh() may replace st.tips before the user dismisses, so
    # without this a stale generated id could not be resolved back to its
    # stable doc identity and doc-level dismissal would silently fail
    # (Codex round-27). Bounded: pruned to the most recent 32 entries.
    shown_docs: dict[str, str] = field(default_factory=dict)


def _state_path() -> Path:
    # Use the shared path helper: KIROCREW_HOME tilde-expansion/resolution and
    # unsafe system-directory rejection must match the rest of the config stack
    # (a raw os.environ read would split tips state from normal config for
    # custom homes containing "~").
    return config_dir() / "tips_state.json"


def _load_state() -> TipsState:
    path = _state_path()
    if not path.exists():
        return TipsState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Structural validation (Codex round-12): a syntactically valid but
        # non-dict/mistyped state file must degrade to defaults, not raise and
        # 500 every tips endpoint until the file is repaired by hand.
        if not isinstance(data, dict):
            logger.warning("tips_state.json has non-dict root; using defaults")
            return TipsState()

        def _typed(key: str, expected: type, default):  # type: ignore[no-untyped-def]
            v = data.get(key, default)
            return v if isinstance(v, expected) else default

        def _finite(v: object, default: float = 0.0) -> float | None:
            """Coerce to a finite float; None if impossible.

            float() on a several-hundred-digit JSON int raises OverflowError
            (Codex round-16) — a persisted-state file must never be able to
            crash cache init and 500 every tips endpoint.
            """
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            try:
                f = float(v)
            except (ValueError, OverflowError):
                return None
            return f if math.isfinite(f) else None

        # Per-entry validation (Codex round-15): container-type checks alone
        # let e.g. {"snoozed": {"x": "bad"}} through, crashing arithmetic in
        # _is_eligible with a 500 on every request until repaired by hand.
        shown_raw = _typed("shown", dict, {})
        shown = {
            k: v for k, v in shown_raw.items()
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        }
        snoozed_raw = _typed("snoozed", dict, {})
        snoozed = {
            k: f for k, v in snoozed_raw.items()
            if isinstance(k, str) and (f := _finite(v)) is not None
        }
        dismissed = [d for d in _typed("dismissed", list, []) if isinstance(d, str)]
        dismissed_docs = [d for d in _typed("dismissed_docs", list, []) if isinstance(d, str)]
        shown_docs = {
            k: v for k, v in _typed("shown_docs", dict, {}).items()
            if isinstance(k, str) and isinstance(v, str)
        }
        snoozed_docs = {
            k: fv for k, v in _typed("snoozed_docs", dict, {}).items()
            if isinstance(k, str) and (fv := _finite(v)) is not None
        }

        return TipsState(
            shown=shown,
            dismissed=dismissed,
            dismissed_docs=dismissed_docs,
            shown_docs=shown_docs,
            snoozed_docs=snoozed_docs,
            snoozed=snoozed,
            opted_out=_typed("opted_out", bool, False),
            last_generated=_finite(data.get("last_generated")) or 0.0,
            last_shown_ts=_finite(data.get("last_shown_ts")) or 0.0,
            tips=[
                st_t for t in _typed("tips", list, [])
                if (st_t := _sanitize_persisted_tip(t)) is not None
            ],
            offered=_sanitize_persisted_tip(data.get("offered")),
        )
    except (OSError, ValueError, TypeError, OverflowError):
        # ValueError covers json.JSONDecodeError; OverflowError defense-in-depth.
        return TipsState()


def _save_state(st: TipsState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        json.dumps(
            {
                "shown": st.shown,
                "dismissed": st.dismissed,
                "dismissed_docs": st.dismissed_docs,
                "shown_docs": st.shown_docs,
                "snoozed_docs": st.snoozed_docs,
                "snoozed": st.snoozed,
                "opted_out": st.opted_out,
                "last_generated": st.last_generated,
                "last_shown_ts": st.last_shown_ts,
                "tips": st.tips,
                "offered": st.offered,
            },
            indent=2,
        )
        + "\n",
        # Owner-only: generated tips embed memory-derived content (preferences,
        # projects, recent activity) — must not be world-readable on shared
        # machines. mode also corrects permissions of pre-existing 0644 files
        # on the next write (atomic replace).
        mode=0o600,
    )
    # mode= only sets POSIX bits; on Windows an owner-only DACL is needed too.
    # Warn-and-continue: a lockdown failure must not break tips persistence,
    # but it must be visible.
    try:
        restrict_to_owner(path)
    except OSError:
        logger.warning("Could not restrict tips_state.json to owner", exc_info=True)


# ── Cache ──


@dataclass
class TipsCache:
    """In-memory tips cache with background refresh."""

    catalog: list[CatalogEntry] = field(default_factory=list)
    state: TipsState = field(default_factory=TipsState)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)  # type: ignore[type-arg]


_tips_init_lock = asyncio.Lock()

# Path to the bundled pre-generated tips catalog (release-time artifact).
_BUNDLED_CATALOG_FILE = Path(__file__).resolve().parent / "data" / "tips_catalog.json"


def _load_bundled_catalog() -> list[CatalogEntry] | None:
    """Load the pre-generated tips catalog shipped with the package.

    Returns None if the file is missing or corrupt (caller should fall back to live scan).
    """
    if not _BUNDLED_CATALOG_FILE.is_file():
        return None
    try:
        data = json.loads(_BUNDLED_CATALOG_FILE.read_text(encoding="utf-8"))
        # A syntactically valid but malformed catalog (list root, non-numeric
        # mtime, huge int) must degrade to the live-scan fallback, never crash
        # cache init (Codex round-19).
        if not isinstance(data, dict):
            return None
        entries_raw = data.get("entries", [])
        if not isinstance(entries_raw, list) or not entries_raw:
            return None
        entries: list[CatalogEntry] = []
        for e in entries_raw:
            if not isinstance(e, dict):
                continue
            feature = e.get("feature", "")
            summary = e.get("summary", "")
            doc = e.get("doc", "")
            if feature and summary and doc:
                try:
                    mtime = float(e.get("mtime", 0.0))
                except (TypeError, ValueError, OverflowError):
                    mtime = 0.0
                if not math.isfinite(mtime):
                    mtime = 0.0
                entries.append(CatalogEntry(feature=feature, summary=summary[:300], doc=doc, mtime=mtime))
        return entries if entries else None
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        # ValueError covers json.JSONDecodeError; the rest are defense-in-depth
        # so a bad bundled file can never take down every tips endpoint.
        return None


async def get_tips_cache(state: DashboardState) -> TipsCache:
    """Get or create the tips cache on the state object (async: offloads IO)."""
    if not hasattr(state, "_tips_cache"):
        async with _tips_init_lock:
            if not hasattr(state, "_tips_cache"):
                loop = asyncio.get_running_loop()
                # Prefer bundled catalog; fall back to live docs scan if missing/corrupt
                catalog = await loop.run_in_executor(None, _load_bundled_catalog)
                if catalog is not None:
                    logger.debug("Tips catalog loaded from bundled data (%d entries)", len(catalog))
                else:
                    logger.debug("Bundled tips catalog unavailable; falling back to live docs scan")
                    catalog = await loop.run_in_executor(None, _scan_docs_catalog)
                st = await loop.run_in_executor(None, _load_state)
                cache = TipsCache()
                cache.catalog = catalog
                cache.state = st
                state._tips_cache = cache  # type: ignore[attr-defined]
    return state._tips_cache  # type: ignore[attr-defined]


# ── Selection (weighted-random, newer-biased) ──


def _select_tip(
    candidates: list[dict],  # type: ignore[type-arg]
    catalog: list[CatalogEntry],
    recency_decay: float = 0.6,
    rng: random.Random | None = None,
) -> dict | None:  # type: ignore[type-arg]
    """Pick one tip via weighted-random with newer-biased weights.

    Weight = recency_decay ** rank, where rank orders eligible tips newest-first
    by catalog recency (doc file mtime).
    """
    if not candidates:
        return None

    if rng is None:
        rng = random.Random()

    # Build mtime lookups from catalog: key by doc filename (authoritative —
    # LLM-generated tips carry the catalog doc in "doc" but usually invent
    # their own id, so id-keyed lookup alone degenerates to insertion order,
    # Codex round-14) and by the catalog-derived id (for catalog fallback
    # tips whose doc field may have been sanitized away).
    mtime_by_doc: dict[str, float] = {entry.doc: entry.mtime for entry in catalog}
    mtime_by_id: dict[str, float] = {
        entry.doc.replace(".md", "-tip"): entry.mtime for entry in catalog
    }

    def _tip_mtime(t: dict, index: int) -> float:  # type: ignore[type-arg]
        doc = t.get("doc", "")
        if doc in mtime_by_doc:
            return mtime_by_doc[doc]
        tid = t.get("id", "")
        if tid in mtime_by_id:
            return mtime_by_id[tid]
        # Unknown provenance: insertion order as a stable last-resort proxy
        return float(index)

    mtimes = {id(t): _tip_mtime(t, i) for i, t in enumerate(candidates)}

    # Sort candidates newest-first by mtime
    sorted_candidates = sorted(
        candidates, key=lambda t: mtimes[id(t)], reverse=True
    )

    # Compute weights: recency_decay ** rank (rank 0 = newest)
    weights = [recency_decay ** rank for rank in range(len(sorted_candidates))]
    total = sum(weights)
    if total == 0:
        return sorted_candidates[0]

    # Weighted random selection
    r = rng.random() * total
    cumulative = 0.0
    for i, w in enumerate(weights):
        cumulative += w
        if r <= cumulative:
            return sorted_candidates[i]
    return sorted_candidates[-1]


# ── Context & Generation ──


def _build_context(state: DashboardState) -> str:
    """Assemble context for the tips prompt from memory and recent activity."""
    parts: list[str] = []
    try:
        memory = ContextBuilder.get_memory_for(None)
        prefs = memory.read_preferences()
        if prefs and prefs.strip() != "# User Preferences\n\n<!-- Learned from conversations -->":
            parts.append(f"## User Preferences\n{prefs[:2000]}")
        projects = memory.read_projects()
        if projects and projects.strip() != "# Active Projects\n\n<!-- Current work context -->":
            parts.append(f"## Active Projects\n{projects[:3000]}")
        recent_history = memory.read_recent_history(days=2)
        if recent_history:
            parts.append(f"## Recent Activity\n{recent_history[:4000]}")
    except Exception:
        logger.debug("Failed to read memory for tips", exc_info=True)
    return "\n\n".join(parts)


# The ONLY fields a tip dict may carry end-to-end (parse → persist → serve).
# _parse_tips projects onto exactly this set so unknown/nested LLM output can
# never bypass string redaction or reach the dashboard.
_TIP_ALLOWED_FIELDS = ("id", "feature", "title", "body", "why", "doc", "cta_prompt")


def _parse_tips(text: str) -> list[dict]:  # type: ignore[type-arg]
    """Parse LLM response into a list of tip dicts."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            valid = []
            for t in result:
                if not isinstance(t, dict):
                    continue
                if not _validate_tip_fields(t):
                    continue
                # Allowlist projection (Codex round-12 HIGH): keep ONLY the
                # seven allowed string fields. Unknown/extra fields from the
                # LLM (including nested dicts) would bypass _redact_tips's
                # string-only redaction and reach persistence + the dashboard.
                t = {k: t[k] for k in _TIP_ALLOWED_FIELDS}
                # Sanitize doc: only allow http(s) URLs or relative .md filenames
                doc = t.get("doc", "")
                if isinstance(doc, str) and doc:
                    if not (re.match(r"^https?://", doc) or re.match(r"^[\w./-]+\.md$", doc)):
                        t["doc"] = ""
                valid.append(t)
            return valid[:_MAX_GENERATED_TIPS]
    except (json.JSONDecodeError, TypeError):
        pass
    logger.warning("Failed to parse tips response (%d chars, content withheld)", len(text))
    return []


# Field length limits for LLM-generated tip dicts
_TIP_FIELD_LIMITS: dict[str, int] = {
    "id": 100,
    "feature": 200,
    "title": 200,
    "body": 1000,
    "why": 1000,
    "doc": 1000,
    "cta_prompt": 1000,
}

_TIP_REQUIRED_NONEMPTY = ("id", "title", "body")


def _sanitize_persisted_tip(t: object) -> dict | None:  # type: ignore[type-arg]
    """Normalize a tip dict loaded from disk through the SAME validation as
    generated tips (Codex round-18): required string fields, length caps,
    allowlist projection. Returns None for anything invalid — a malformed
    persisted tip like {"id": []} must be discarded, not crash _is_eligible
    with a 500 on every request.
    """
    if not isinstance(t, dict):
        return None
    if not _validate_tip_fields(t):
        return None
    return {k: t[k] for k in _TIP_ALLOWED_FIELDS}


def _validate_tip_fields(t: dict) -> bool:  # type: ignore[type-arg]
    """Validate that all required tip fields are strings within length bounds."""
    required = _TIP_ALLOWED_FIELDS
    for k in required:
        v = t.get(k)
        if not isinstance(v, str):
            return False
        limit = _TIP_FIELD_LIMITS.get(k, 1000)
        if len(v) > limit:
            t[k] = v[:limit]  # truncate rather than reject
    # Non-empty check for critical fields
    for k in _TIP_REQUIRED_NONEMPTY:
        if not t.get(k, "").strip():
            return False
    return True


def _redact_tips(tips: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    """Apply security redaction to each tip field."""
    result = []
    for t in tips:
        redacted: dict = {}  # type: ignore[type-arg]
        for k, v in t.items():
            if isinstance(v, str):
                v, _ = redact_exfiltration_urls(v)
                v, _ = redact_credentials(v)
            redacted[k] = v
        result.append(redacted)
    return result


def _fallback_tips(catalog: list[CatalogEntry], st: TipsState) -> list[dict]:  # type: ignore[type-arg]
    """Non-personalized tips from catalog when bg session unavailable."""
    tips = []
    for entry in catalog:
        tid = entry.doc.replace(".md", "-tip")
        if tid in st.dismissed:
            continue
        tips.append(
            {
                "id": tid,
                "feature": entry.feature,
                "title": entry.feature,
                "body": entry.summary,
                "why": "",
                "doc": entry.doc,
                "cta_prompt": f"Tell me about {entry.feature}",
            }
        )
    return tips


def _is_eligible(
    tip: dict, st: TipsState, now: float, snooze_hours: float  # type: ignore[type-arg]
) -> bool:
    """Check if a tip is eligible (not dismissed, not actively snoozed)."""
    tid = tip.get("id", "")
    if tid in st.dismissed:
        return False
    # Doc-level dismissal: stable across LLM regenerations that invent new ids
    doc = tip.get("doc", "")
    if doc and doc in st.dismissed_docs:
        return False
    if tid in st.snoozed:
        snooze_ts = st.snoozed[tid]
        if now - snooze_ts < snooze_hours * 3600:
            return False
    if doc and doc in st.snoozed_docs:
        if now - st.snoozed_docs[doc] < snooze_hours * 3600:
            return False
    return True


async def generate_tips(state: DashboardState) -> list[dict]:  # type: ignore[type-arg]
    """Generate personalized tips using the background kiro-cli session."""
    cache = await get_tips_cache(state)
    loop = asyncio.get_running_loop()
    context = await loop.run_in_executor(None, _build_context, state)
    catalog_text = "\n".join(f"- {e.feature} ({e.doc}): {e.summary}" for e in cache.catalog)
    if not catalog_text:
        return []

    st = cache.state
    prompt = _PROMPT_TEMPLATE.format(
        max_tips=_MAX_GENERATED_TIPS,
        catalog=catalog_text,
        context=context or "(no user context available)",
        shown_ids=", ".join(st.shown.keys()) or "none",
        dismissed_ids=", ".join(st.dismissed) or "none",
    )

    try:
        session = await state.sessions.get_bg_session()
    except Exception:
        logger.debug("Background session unavailable for tips")
        return _fallback_tips(cache.catalog, st)

    text = ""
    try:
        # Pin to Haiku-class model for cost efficiency (per-session override,
        # same pattern as chat_title.py). Best-effort: if the backend cannot
        # switch, falls through on the session's default model.
        loop2 = asyncio.get_running_loop()
        cfg = await loop2.run_in_executor(None, KiroCrewConfig.load)
        tips_model = cfg.dashboard.tips_model
        _set_model = getattr(session, "set_model", None)
        if _set_model is not None:
            try:
                await _set_model(tips_model)
            except Exception:
                logger.debug("Tips model override to %s failed; using default", tips_model)

        async def _stream() -> str:
            nonlocal text
            async for event in session.prompt(prompt):
                if event.kind == EVENT_TEXT_CHUNK:
                    text += event.text
                elif event.kind == EVENT_PERMISSION_REQUEST:
                    sel().log_tool_invocation(
                        session_key="_bg",
                        tool_name=getattr(event, "title", "unknown"),
                        outcome="denied",
                        source="tips",
                    )
                    await session.reject_tool(event.request_id)
                elif event.kind == EVENT_COMPLETE:
                    break
            return text

        await asyncio.wait_for(_stream(), timeout=90)
    except asyncio.TimeoutError:
        logger.warning("Tips generation timed out")
        return _fallback_tips(cache.catalog, st)
    finally:
        await session.destroy()

    tips = _parse_tips(text)
    if tips:
        tips = _redact_tips(tips)
        logger.info("Generated %d personalized tips", len(tips))
        return tips

    return _fallback_tips(cache.catalog, st)


async def refresh_tips(state: DashboardState, cache: TipsCache) -> None:
    """Background task: regenerate tips.

    Generation runs OUTSIDE the cache lock so handlers are never blocked.
    """
    try:
        tips = await generate_tips(state)
    except Exception:
        logger.warning("Tips generation failed", exc_info=True)
        return
    async with cache._lock:
        cache.state.tips = tips
        cache.state.last_generated = time.time()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _save_state, cache.state)


async def maybe_refresh(state: DashboardState, cache: TipsCache) -> None:
    """Trigger a background refresh if tips are stale."""
    now = time.time()
    if now - cache.state.last_generated < _REFRESH_INTERVAL_SECS:
        return
    if cache._task and not cache._task.done():
        return
    task = asyncio.create_task(refresh_tips(state, cache))
    cache._task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


# ── HTTP Handlers ──


async def api_tips_next(request: web.Request) -> web.Response:
    """GET /api/tips/next — return the next tip + glow flag.

    Returns JSON: {"tip": <tip-or-null>, "glow": <bool>}
    - glow=true ONLY when cadence gate is open AND eligible tip exists.
    - 204 only for flag-off / optout.

    Offered-tip persistence: once a tip is selected, it is persisted as state["offered"]
    and re-served on every GET until the user provides feedback (ack/snooze/dismiss).
    last_shown_ts is set ONLY on feedback, not on serve.
    """
    state: DashboardState = request.app["state"]

    loop = asyncio.get_running_loop()
    cfg = await loop.run_in_executor(None, KiroCrewConfig.load)
    if not cfg.dashboard.tips_enabled:
        return web.Response(status=204)

    cache = await get_tips_cache(state)
    st = cache.state

    if st.opted_out:
        return web.Response(status=204)

    # Trigger background refresh if stale
    await maybe_refresh(state, cache)

    now = time.time()
    cadence_hours = cfg.dashboard.tips_cadence_hours
    snooze_hours = cfg.dashboard.tips_snooze_hours
    recency_decay = cfg.dashboard.tips_recency_decay

    # Hold the lock across the whole inspect→select→persist transaction so two
    # concurrent tabs cannot both observe "no offered tip + cadence open" and
    # overwrite each other's selection (single-offer contract). Everything under
    # the lock is fast (list filters + one small file write) — the long LLM
    # generation path lives in maybe_refresh's background task, NOT here.
    async with cache._lock:
        # If there is an outstanding offered tip, re-serve it (unless it became ineligible)
        if st.offered and isinstance(st.offered, dict):
            offered_id = st.offered.get("id", "")
            if offered_id and _is_eligible(st.offered, st, now, snooze_hours):
                offered_tip = _redact_tips([st.offered])[0]
                return web.json_response({"tip": offered_tip, "glow": True})
            # Offered tip became ineligible (dismissed/snoozed externally) — clear it
            st.offered = None
            await loop.run_in_executor(None, _save_state, st)

        # Cadence gate: has enough time passed since last feedback?
        cadence_open = (now - st.last_shown_ts) >= (cadence_hours * 3600)

        if not cadence_open:
            return web.json_response({"tip": None, "glow": False})

        # Gather eligible candidates
        candidates = [
            t for t in st.tips if _is_eligible(t, st, now, snooze_hours)
        ]
        if not candidates:
            # Try fallback from catalog
            candidates = [
                t for t in _fallback_tips(cache.catalog, st)
                if _is_eligible(t, st, now, snooze_hours)
            ]

        if not candidates:
            return web.json_response({"tip": None, "glow": False})

        # Exploration blend: with probability explore_ratio, pick uniformly at random
        # from ALL eligible catalog-derived candidates (general features); otherwise
        # use the weighted-random newer-biased pick from the generated pool.
        explore_ratio = cfg.dashboard.tips_explore_ratio
        rng = random.Random()
        tip: dict | None = None  # type: ignore[type-arg]
        if rng.random() < explore_ratio:
            # Explore: pick from catalog-derived fallback tips (general, context-independent)
            all_catalog_candidates = [
                t for t in _fallback_tips(cache.catalog, st)
                if _is_eligible(t, st, now, snooze_hours)
            ]
            if all_catalog_candidates:
                tip = rng.choice(all_catalog_candidates)
            else:
                tip = _select_tip(candidates, cache.catalog, recency_decay=recency_decay)
        else:
            # Exploit: weighted-random newer-biased from generated tips
            tip = _select_tip(candidates, cache.catalog, recency_decay=recency_decay)
        if not tip:
            return web.json_response({"tip": None, "glow": False})

        # Defense-in-depth: redact at point of serving
        tip = _redact_tips([tip])[0]

        # Persist as offered (do NOT set last_shown_ts — that happens on feedback)
        tip_id = tip.get("id", "")
        if tip_id:
            st.offered = tip
            st.shown[tip_id] = st.shown.get(tip_id, 0) + 1
            await loop.run_in_executor(None, _save_state, st)

    return web.json_response({"tip": tip, "glow": True})


async def api_tips_status(request: web.Request) -> web.Response:
    """GET /api/tips/status — read-only tips availability state.

    Returns JSON: {"enabled_config": <bool>, "opted_out": <bool>, "cadence_hours": <float>}
    - enabled_config: instance-level ``tips_enabled`` config flag.
    - opted_out: per-instance opt-out flag (KiroCrew is a single-owner
      personal gateway; tips state is instance-wide by design) (optout/optin
      feedback actions). Lets the settings UI render the current state.
    - cadence_hours: configured ``tips_cadence_hours`` — lets the client derive
      its polling gate as min(20min, cadence) so sub-20-minute cadence configs
      take effect while the default posture keeps the 20-minute UI floor.
    """
    state: DashboardState = request.app["state"]

    loop = asyncio.get_running_loop()
    cfg = await loop.run_in_executor(None, KiroCrewConfig.load)

    cache = await get_tips_cache(state)
    return web.json_response(
        {
            "enabled_config": bool(cfg.dashboard.tips_enabled),
            "opted_out": bool(cache.state.opted_out),
            "cadence_hours": float(cfg.dashboard.tips_cadence_hours),
        }
    )


async def api_tips_feedback(request: web.Request) -> web.Response:
    """POST /api/tips/feedback — record user feedback on a tip.

    Body: {"id": "tip-id", "action": "shown|ack|dismiss|snooze|helpful|optout|optin"}
    - shown: tip was actually displayed — start the cadence gate and clear the
      outstanding offered tip WITHOUT dismissing it (stays eligible for future
      weighted-random re-selection). Prevents the same offered tip from being
      re-served every turn to passive users who never click ✕.
    - ack / dismiss: permanent dismiss (add to acknowledged list)
    - snooze: temporarily hide (eligible again after snooze_hours)
    - helpful: engagement signal (no state change beyond logging)
    - optout: disable tips entirely
    - optin: re-enable tips

    ack/snooze also update last_shown_ts (cadence gate resets).
    """
    state: DashboardState = request.app["state"]
    cache = await get_tips_cache(state)
    st = cache.state

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    tip_id = body.get("id", "")
    action = body.get("action", "")
    if not isinstance(tip_id, str) or not isinstance(action, str) or len(tip_id) > 100:
        return web.json_response({"error": "invalid fields"}, status=400)

    valid_actions = ("shown", "ack", "dismiss", "snooze", "helpful", "optout", "optin")
    if action not in valid_actions:
        return web.json_response({"error": "invalid action"}, status=400)

    now = time.time()

    async with cache._lock:
        if action == "shown":
            # Display-time signal: start cadence + release the offered slot, but
            # keep the tip eligible (NOT dismissed) for future re-selection.
            # Record the id -> doc mapping first: offered is cleared here and the
            # pool may regenerate before dismissal (Codex round-27).
            if isinstance(st.offered, dict) and st.offered.get("id") == tip_id:
                doc_val = st.offered.get("doc", "")
                if isinstance(doc_val, str) and doc_val:
                    st.shown_docs[tip_id] = doc_val
                    while len(st.shown_docs) > 32:
                        st.shown_docs.pop(next(iter(st.shown_docs)))
            st.last_shown_ts = now
            st.offered = None
        elif action in ("ack", "dismiss"):
            if tip_id and tip_id not in st.dismissed:
                st.dismissed.append(tip_id)
            # Also record the catalog doc (stable feature identity): LLM
            # regeneration invents fresh ids, so id-only dismissal would let
            # the same feature resurface (Codex round-15).
            if tip_id:
                doc = ""
                if isinstance(st.offered, dict) and st.offered.get("id") == tip_id:
                    doc = st.offered.get("doc", "")
                else:
                    for t in st.tips:
                        if isinstance(t, dict) and t.get("id") == tip_id:
                            doc = t.get("doc", "")
                            break
                if not doc:
                    doc = st.shown_docs.get(tip_id, "")
                if not doc:
                    # Catalog-fallback tips carry the catalog-derived id
                    # ("X.md" -> "X-tip") but may be gone from offered (cleared
                    # by "shown") and absent from st.tips (Codex round-17) —
                    # reverse-map through the catalog itself.
                    for entry in cache.catalog:
                        if entry.doc.replace(".md", "-tip") == tip_id:
                            doc = entry.doc
                            break
                if doc and doc not in st.dismissed_docs:
                    st.dismissed_docs.append(doc)
            st.last_shown_ts = now
            st.offered = None
        elif action == "snooze":
            if tip_id:
                st.snoozed[tip_id] = now
                doc = ""
                if isinstance(st.offered, dict) and st.offered.get("id") == tip_id:
                    doc = st.offered.get("doc", "")
                else:
                    for t in st.tips:
                        if isinstance(t, dict) and t.get("id") == tip_id:
                            doc = t.get("doc", "")
                            break
                if not doc:
                    doc = st.shown_docs.get(tip_id, "")
                if not doc:
                    for entry in cache.catalog:
                        if entry.doc.replace(".md", "-tip") == tip_id:
                            doc = entry.doc
                            break
                if isinstance(doc, str) and doc:
                    st.snoozed_docs[doc] = now
            st.last_shown_ts = now
            st.offered = None
        elif action == "helpful":
            pass
        elif action == "optout":
            st.opted_out = True
            st.offered = None
        elif action == "optin":
            st.opted_out = False

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _save_state, st)
    return web.json_response({"ok": True})
