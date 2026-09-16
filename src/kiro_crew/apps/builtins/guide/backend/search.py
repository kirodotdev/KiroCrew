"""Guide data: load, base+overlay merge, keyword search, media lookup.

This module is the ONE source of truth for what a guide entry is and how it is
searched. Both readers share it: the in-process HTTP routes (``routes.py``) and
the stdio MCP server (``mcp_server.py``), so the ranking an agent sees over MCP
and the ranking the UI shows are computed by the same code.

Data model — two physically separate files:

* ``data/guide-base.json`` ships inside the public wheel next to this package.
  It holds the ``audience ∈ {external, both}`` entries with no internal fields.
* ``<app_data_dir>/overlay/guide-overlay.json`` is seeded only by an internal
  edition's companion bundle. Its mere PRESENCE is the edition signal — there is
  no runtime ``audience`` filter and no ``EDITION`` constant, so a public build
  physically has no internal bytes to leak.

The overlay contract (what CR-1's ``seed_guide_internal()`` MUST emit). The
overlay is a JSON object with three optional arrays — the internal edition
declares only what differs from the public base:

* ``entries`` — whole internal-only entries, appended as-is. Their ``id`` must
  not collide with a base entry (a collision replaces the base entry).
* ``excluded_both_entries`` — whole ``audience=both`` entries the public build
  stripped down; appended as-is, same id rule as ``entries``.
* ``patches`` — a list of ``{"id": <base id>, "patch": {<field>: <value>}}``.
  Each patch locates a base entry by ``id`` and overwrites the named fields with
  the internal edition's COMPLETE value for that field — scalars, lists, and
  dicts alike are replaced wholesale (no append/merge; the patch value is the
  authoritative field). A patch whose ``id`` matches no base entry is logged and
  skipped.

Legacy shapes are still accepted: a bare list, or an object with only
``entries``, loads those as whole append-or-replace entries. Base is read once;
the overlay is re-read lazily whenever its file mtime changes, so an edition's
data car can drop a new overlay and the change is picked up without a restart.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger("kirocrew.app.guide")

APP_NAME = "guide"

#: Package-bundled data (public base + base media), resolved next to this file.
_PKG_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_BASE_JSON = _PKG_DATA_DIR / "guide-base.json"
_BASE_MEDIA_DIR = _PKG_DATA_DIR / "media"

#: Relative locations of the overlay inside the app data dir.
_OVERLAY_SUBDIR = "overlay"
_OVERLAY_JSON_NAME = "guide-overlay.json"
_OVERLAY_MEDIA_SUBDIR = "media"

#: Field-weighting for keyword scoring. Title and keywords dominate; the
#: Chinese title and topic are weak signals so an English query still ranks.
_FIELD_WEIGHTS: dict[str, float] = {
    "title": 5.0,
    "keywords": 4.0,
    "symptom": 3.0,
    "title_zh": 2.0,
    "topic": 1.0,
}

_lock = threading.RLock()
_base_cache: list[dict[str, Any]] | None = None
_overlay_mtime: float | None = None
_merged_cache: list[dict[str, Any]] | None = None


def _overlay_dir() -> Path:
    """The app-data overlay directory (may not exist on a public install).

    Imported lazily so the module loads even in a bare context where the apps
    manager is not importable (it is in every real gateway/MCP process).
    """
    from kiro_crew.apps.manager import app_data_dir

    return app_data_dir(APP_NAME) / _OVERLAY_SUBDIR


def _overlay_json_path() -> Path:
    return _overlay_dir() / _OVERLAY_JSON_NAME


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    """Parse a JSON file expected to hold ``{"entries": [...]}`` or a bare list.

    Fail-open: a missing or malformed file yields an empty list and logs, rather
    than taking down boot (matches the design's fail-open data contract).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (ValueError, OSError):
        logger.warning("guide: could not parse %s; ignoring", path, exc_info=True)
        return []
    if isinstance(raw, dict):
        raw = raw.get("entries", [])
    if not isinstance(raw, list):
        logger.warning("guide: %s is not a list of entries; ignoring", path)
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("id")]


def _load_base() -> list[dict[str, Any]]:
    global _base_cache
    if _base_cache is None:
        _base_cache = _read_json_list(_BASE_JSON)
        logger.info("guide: loaded %d base entries", len(_base_cache))
    return _base_cache


def _read_json_obj(path: Path) -> Any:
    """Parse a JSON file to its raw object (dict or list), fail-open to None.

    Used for the overlay, whose canonical shape is an object with
    ``entries`` / ``patches`` / ``excluded_both_entries`` arrays (see the module
    docstring). A missing or malformed file yields None and logs.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        logger.warning("guide: could not parse %s; ignoring overlay", path, exc_info=True)
        return None


def _apply_whole(by_id: dict[str, dict[str, Any]], order: list[str], entries: Any) -> None:
    """Append whole entries (append-or-replace by id). Non-dicts/idless skipped."""
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        eid = str(entry["id"])
        if eid not in by_id:
            order.append(eid)
        by_id[eid] = dict(entry)


def _apply_patches(by_id: dict[str, dict[str, Any]], patches: Any) -> None:
    """Apply field-level patches: each named field is replaced WHOLESALE.

    The patch value is the internal edition's authoritative value for that field
    (scalar, list, or dict) — there is deliberately no append/merge. A patch for
    an unknown id is logged and skipped.
    """
    if not isinstance(patches, list):
        return
    for item in patches:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "")
        patch = item.get("patch")
        if not pid or not isinstance(patch, dict):
            logger.warning("guide: skipping malformed overlay patch (id=%r)", item.get("id"))
            continue
        target = by_id.get(pid)
        if target is None:
            logger.warning("guide: overlay patch for unknown id %s; skipping", pid)
            continue
        merged = dict(target)
        for key, value in patch.items():
            if key == "id":
                continue
            merged[key] = value
        by_id[pid] = merged


def _compose(base: list[dict[str, Any]], overlay_raw: Any) -> list[dict[str, Any]]:
    """Compose base + overlay into the merged entry list, base order first.

    ``overlay_raw`` is the parsed overlay object: the canonical
    ``{entries, patches, excluded_both_entries}`` shape, a legacy bare list, or
    a legacy object with only ``entries`` — all handled here.
    """
    by_id: dict[str, dict[str, Any]] = {str(e["id"]): dict(e) for e in base}
    order: list[str] = [str(e["id"]) for e in base]
    if isinstance(overlay_raw, list):
        _apply_whole(by_id, order, overlay_raw)  # legacy bare list
    elif isinstance(overlay_raw, dict):
        _apply_whole(by_id, order, overlay_raw.get("entries"))
        _apply_whole(by_id, order, overlay_raw.get("excluded_both_entries"))
        _apply_patches(by_id, overlay_raw.get("patches"))
    return [by_id[i] for i in order]


def _entries() -> list[dict[str, Any]]:
    """Return the merged entry list, reloading the overlay if its mtime changed."""
    global _overlay_mtime, _merged_cache
    with _lock:
        base = _load_base()
        try:
            mtime: float | None = _overlay_json_path().stat().st_mtime
        except OSError:
            mtime = None
        if _merged_cache is None or mtime != _overlay_mtime:
            overlay_raw = _read_json_obj(_overlay_json_path()) if mtime is not None else None
            _merged_cache = _compose(base, overlay_raw)
            _overlay_mtime = mtime
            logger.info("guide: composed %d entries (%d base)", len(_merged_cache), len(base))
        return _merged_cache


# ── public read API ──────────────────────────────────────────────────────────


def all_entries() -> list[dict[str, Any]]:
    """A shallow copy of every merged entry, in base-then-overlay order."""
    return [dict(e) for e in _entries()]


def index() -> dict[str, Any]:
    """The id set plus distinct platform/topic values across the merged data.

    Backs ``GET /index``: ``ids`` feeds in-text entry autolinking (only a real id
    becomes a link) and ``platforms`` / ``topics`` populate the filter chips.
    """
    entries = _entries()
    ids = [str(e["id"]) for e in entries if e.get("id")]
    platforms: set[str] = set()
    topics: set[str] = set()
    for e in entries:
        plat = e.get("platform")
        for p in plat if isinstance(plat, list) else [plat] if plat else []:
            platforms.add(str(p))
        top = e.get("topic")
        for tp in top if isinstance(top, list) else [top] if top else []:
            topics.add(str(tp))
    return {"ids": ids, "platforms": sorted(platforms), "topics": sorted(topics)}


def get_entry(entry_id: str) -> dict[str, Any] | None:
    """One merged entry by id, or None."""
    for entry in _entries():
        if str(entry.get("id")) == entry_id:
            return dict(entry)
    return None


def _field_text(entry: dict[str, Any], field: str) -> str:
    value = entry.get(field)
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value or "")


def _platform_matches(entry: dict[str, Any], platform: str) -> bool:
    plats = entry.get("platform")
    if not plats:
        return True  # unscoped entry applies everywhere
    if isinstance(plats, str):
        plats = [plats]
    wanted = platform.strip().lower()
    values = {str(p).strip().lower() for p in plats}
    return wanted in values or "all" in values


def fix_summary(entry: dict[str, Any]) -> str:
    """A one-line 'how to fix' from the first actionable step, else the symptom.

    A step's ``do`` may be a plain string, or a platform-variant dict
    (``{"default": ..., "macos": ...}``) — prefer ``default``, then any variant.
    """
    steps = entry.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            do = step.get("do")
            if isinstance(do, dict):
                do = do.get("default") or next(
                    (v for v in do.values() if isinstance(v, str) and v.strip()), ""
                )
            if isinstance(do, str) and do.strip():
                return do
    return str(entry.get("symptom") or "")


def _summarize(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id"),
        "title": entry.get("title"),
        "symptom": entry.get("symptom"),
        "trust": entry.get("trust"),
        "fix": fix_summary(entry),
    }


def search(
    query: str,
    *,
    platform: str | None = None,
    topic: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Keyword-scored entry summaries, best match first.

    Scoring is a weighted token-overlap over title/keywords/symptom/title_zh/
    topic (see ``_FIELD_WEIGHTS``), nudged by the entry's own ``weight``. An
    empty query returns the highest-weight entries so the UI can show a default
    list. ``platform``/``topic`` are hard filters applied before scoring.
    """
    limit = max(1, min(int(limit), 50))
    tokens = [t for t in query.lower().split() if t]

    candidates = _entries()
    if platform:
        candidates = [e for e in candidates if _platform_matches(e, platform)]
    if topic:
        wanted = topic.strip().lower()

        def _has_topic(e: dict[str, Any]) -> bool:
            tp = e.get("topic")
            if isinstance(tp, list):
                return wanted in {str(x).strip().lower() for x in tp}
            return str(tp or "").strip().lower() == wanted

        candidates = [e for e in candidates if _has_topic(e)]

    scored: list[tuple[float, float, dict[str, Any]]] = []
    for entry in candidates:
        weight = float(entry.get("weight") or 1.0)
        if not tokens:
            scored.append((0.0, weight, entry))
            continue
        score = 0.0
        for field, fweight in _FIELD_WEIGHTS.items():
            haystack = _field_text(entry, field).lower()
            if not haystack:
                continue
            for tok in tokens:
                if tok in haystack:
                    score += fweight
        if score > 0.0:
            scored.append((score * weight, weight, entry))

    # Sort by score desc, then entry weight desc, then id for stability.
    scored.sort(key=lambda t: (t[0], t[1], str(t[2].get("id"))), reverse=True)
    return [_summarize(e) for _score, _w, e in scored[:limit]]


# ── media ────────────────────────────────────────────────────────────────────


def resolve_media(key: str) -> Path | None:
    """Resolve a media key to a real file, overlay taking precedence over base.

    Two-tier lookup: the internal overlay's ``media/`` dir is checked first so an
    edition can override or add media, then the package's bundled ``media/`` dir.
    The key is confined to its media root (no traversal, no absolute paths); a
    key that escapes returns None.
    """
    key = (key or "").strip().lstrip("/")
    # Reject traversal by inspecting the key's path components (PurePosixPath,
    # since a media key is always '/'-delimited) rather than assembling '/' by
    # hand; the resolve()+relative_to containment below is the real guard.
    if not key or ".." in PurePosixPath(key).parts:
        return None
    for root in (_overlay_dir() / _OVERLAY_MEDIA_SUBDIR, _BASE_MEDIA_DIR):
        try:
            candidate = (root / key).resolve()
            candidate.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        if candidate.is_file():
            return candidate
    return None


def reset_cache() -> None:
    """Drop all caches. Test seam; also lets a caller force a full reload."""
    global _base_cache, _overlay_mtime, _merged_cache
    with _lock:
        _base_cache = None
        _overlay_mtime = None
        _merged_cache = None
