"""Featured rows for the Add Skill / Add Server dialogs.

Neither public catalog offers a public "popular" listing: skills.sh refuses a
query under two characters and the MCP registry lists servers alphabetically.
So the dialogs' starting list is a short bundled file,
``data/discovery_featured.json``, of entries each provider can install by id.
The handlers render them exactly like search results, so preview, install and
the Installed badge go through the same code a search hit does.

Read once and cached; a missing or malformed file yields no rows rather than
breaking the dialog, which then falls back to its type-to-search hint.
"""

from __future__ import annotations

import functools
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FILE = Path(__file__).resolve().parent / "data" / "discovery_featured.json"

# The keys each kind's rows must carry as strings; anything else is dropped.
_REQUIRED = ("provider", "id", "name")


@functools.lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("discovery_featured.json unreadable; no featured rows", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def featured_entries(kind: str) -> list[dict[str, Any]]:
    """The featured rows for ``kind`` (``"skills"`` or ``"mcp_servers"``).

    Returns fresh copies, so a caller may not mutate the cache.
    """
    rows = _load().get(kind)
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) and row.get(k) for k in _REQUIRED
        ):
            continue
        # Keep only string fields, plus ``methods`` as a list of strings, so a
        # caller can read any field with ``row.get(key, "")`` and trust its type.
        clean: dict[str, Any] = {k: v for k, v in row.items() if isinstance(v, str)}
        methods = row.get("methods")
        clean["methods"] = (
            [m for m in methods if isinstance(m, str)] if isinstance(methods, list) else []
        )
        out.append(clean)
    return out
