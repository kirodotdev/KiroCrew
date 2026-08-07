"""GET /api/skills/budget — per-skill 30-day injection cost with alias folding.

This is the *control-plane* read endpoint powering the Context Budget screen.
Unlike ``list_skills()`` (runs on the event loop during context assembly and
must be O(skills) with no extra IO), this does deliberate filesystem work
(``Path.resolve()`` per ledger key) and caches the expensive alias map so
repeated dashboard refreshes are cheap.

The fold logic lives here — not in ``list_skills()`` — because:
1. ``list_skills()`` guarantees one stat per skill (its docstring contract) and
   runs on the hot path; the fold needs per-ledger-key path resolution.
2. The fold is a user-action-frequency operation (dashboard page load), not a
   per-message one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.skill_usage import SkillUsageLedger

from ._shared import _get_skills

logger = logging.getLogger(__name__)

#: 30-day window matching the ledger's _MAX_AGE_SECS.
_WINDOW_DAYS = 30


def _safe_display_name(meta: dict[str, str], key: str) -> str:
    """The frontmatter ``name``, with credential-shaped text redacted.

    An auto-skill's frontmatter is written by the agent, so this string is
    LLM-authored and reaches the dashboard verbatim. Redacting is cheap and a
    legitimate skill name never matches these patterns, so the display path pays
    nothing for the guarantee.
    """
    raw = meta.get("name", key)
    cleaned, _ = redact_exfiltration_urls(raw)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


def _char_length(skill_file: Path) -> int | None:
    """Characters in *skill_file*, or ``None`` if it cannot be read/decoded.

    A context budget is denominated in characters, and ``st_size`` is bytes —
    the two diverge for any skill with non-ASCII prose. Reading the file is
    acceptable at this endpoint's frequency (a user opening a screen), which is
    exactly why the cost is computed here and not in ``list_skills()``.
    """
    try:
        return len(skill_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _compute_budget(
    skills_loader: Any,
) -> dict[str, Any]:
    """Blocking function that computes the full budget response.

    Runs on the discovery executor, off the event loop.
    """
    skill_pairs = skills_loader._iter()
    ledger: SkillUsageLedger | None = skills_loader._usage

    # Build the alias map via the loader's public method (cached on key set).
    alias_map: dict[str, list[str]] = {}
    ledger_snapshot: dict[str, tuple[int, float]] = {}
    if ledger is not None:
        # Folding is supplementary: a missing ledger or an unreadable one still
        # yields usable rows, so an I/O error or a malformed ledger degrades to
        # "no folds". A programming error must NOT degrade — silently returning
        # zero folds is indistinguishable from correct output and reinstates the
        # undercount this endpoint exists to correct.
        try:
            ledger_snapshot = ledger.snapshot()
            alias_map = skills_loader.resolve_ledger_aliases()
        except (OSError, ValueError):
            logger.warning("skill-budget: alias map build failed", exc_info=True)

    now = time.time()
    rows: list[dict[str, Any]] = []
    total_chars = 0

    # A served key can be an alias of another served key (a file-level symlink
    # leaves both directories real, so both are served). Its hits were folded
    # into the canonical row, so emitting it again would list one file twice and
    # count its size twice in the total.
    folded_away = {alias for aliases in alias_map.values() for alias in aliases}

    for key, skill_file in skill_pairs:
        if key in folded_away:
            continue
        # Size
        try:
            st = skill_file.stat()
            size_bytes = st.st_size
        except OSError:
            size_bytes = 0

        # Frontmatter. Caught HERE, not in the loader: this endpoint only reads,
        # so one undecodable SKILL.md should cost that row its metadata rather
        # than 500 the whole screen. The loader deliberately propagates instead,
        # because its write callers must abort rather than rewrite a file with
        # metadata they failed to read.
        try:
            meta = skills_loader._cached_frontmatter(skill_file)
        except (OSError, ValueError):
            logger.warning(
                "skill-budget: unreadable frontmatter for %s", key, exc_info=True
            )
            meta = {}

        is_always = meta.get("always", "").lower() == "true"

        # Deliveries: own key + aliases
        deliveries: int | None = None
        idle_days: float | None = None

        if ledger is not None:
            own_entry = ledger_snapshot.get(key)
            alias_keys = alias_map.get(key, [])

            if own_entry is not None or alias_keys:
                total_hits = 0
                latest_seen = 0.0
                if own_entry is not None:
                    total_hits += own_entry[0]
                    latest_seen = max(latest_seen, own_entry[1])
                for ak in alias_keys:
                    alias_entry = ledger_snapshot.get(ak)
                    if alias_entry is not None:
                        total_hits += alias_entry[0]
                        latest_seen = max(latest_seen, alias_entry[1])
                deliveries = total_hits if total_hits > 0 else (
                    0 if (own_entry is not None or alias_keys) else None
                )
                # idle_days: days since last_seen
                if latest_seen > 0:
                    idle_days = (now - latest_seen) / 86400.0
                    idle_days = round(idle_days, 1)
                else:
                    idle_days = None
            else:
                # No ledger entry at all — untracked.
                deliveries = None
                idle_days = None
        else:
            # No ledger available.
            deliveries = None
            idle_days = None

        # chars computation:
        # For always:true skills, the ledger never records deliveries (they are
        # injected unconditionally every turn without going through
        # _record_use), so deliveries is unreliable. Report chars as None to
        # signal "unmeasurable" rather than a misleading 0.
        chars: int | None
        if is_always:
            chars = None
        else:
            # Cost is CHARACTERS, which is what the screen says and what a
            # context budget is denominated in. `st_size` is BYTES: a skill with
            # non-ASCII prose encodes to more UTF-8 bytes than it has characters,
            # so using it here inflated that skill's spend and misranked the
            # table. `size_bytes` stays as-is — it is the file size, correctly.
            char_len = _char_length(skill_file)
            if char_len is None:
                # Undecodable: no honest character count exists for it.
                chars = None
            else:
                computed = char_len * (deliveries if deliveries is not None else 0)
                total_chars += computed
                chars = computed

        # Determine source
        try:
            owned = skills_loader._owned_hint(skill_file)
        except Exception:
            owned = False
        source = "kirocrew" if owned else "external"

        row: dict[str, Any] = {
            "key": key,
            "name": _safe_display_name(meta, key),
            "size_bytes": size_bytes,
            "deliveries": deliveries,
            "chars": chars,
            "inject_on_trigger": (
                meta.get("inject_on_trigger", "").strip().lower() != "false"
            ),
            "always": is_always,
            "owned": owned,
            "source": source,
            "idle_days": idle_days,
        }

        # folded_from: alias keys whose SKILL.md resolves to the same file
        folded = alias_map.get(key, [])
        if folded:
            row["folded_from"] = sorted(folded)

        rows.append(row)

    return {
        "window_days": _WINDOW_DAYS,
        "total_chars": total_chars,
        "rows": rows,
    }


async def api_skills_budget(request: web.Request) -> web.Response:
    """GET /api/skills/budget — per-skill 30-day injection cost.

    Offloads the blocking filesystem work (path resolution for alias folding,
    frontmatter reads) to the discovery executor, same as GET /api/skills.
    """
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)

    result = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        _compute_budget,
        skills,
    )
    return web.json_response(result)
