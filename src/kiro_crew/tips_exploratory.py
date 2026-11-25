"""The exploratory tip bank: one-line tips shown beside "Thinking..." while a turn runs.

A separate bank from ``data/tips_curated.json``. Exploratory tips never enter the
tip card above the composer, and curated, generated and catalog tips never reach
the thinking row. The two share only the dismissal store and the
``kirocrew.tips.feedback`` counter, so a dismissed exploratory tip stays dismissed
and the counter tells them apart by ``source=exploratory``.

Kept import-light (no aiohttp, no dashboard state) so the metrics labelling can
read the shipped ids without importing the tips runtime.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_FILE = Path(__file__).resolve().parent / "data" / "tips_exploratory.json"

#: What a tip may require of the dashboard. The dashboard confirms each one
#: before it shows the tip and treats a key it does not know as unmet, so a new
#: key here hides its tips on an older dashboard rather than showing them.
REQUIREMENT_KEYS: frozenset[str] = frozenset(
    {
        "desktop",
        "keyboard",
        "command_bar",
        "split_send",
        "terminal",
        "terminal_docked",
        "local_session",
        "project_files",
        "microphone",
        "speech_playback",
    }
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BODY_MAX_CHARS = 160


def _clean(raw: object) -> dict[str, object] | None:
    """One validated entry, or None when any field is malformed."""
    if not isinstance(raw, dict):
        return None
    tip_id, body, requires, source = (
        raw.get("id"),
        raw.get("body"),
        raw.get("requires"),
        raw.get("source"),
    )
    if not isinstance(tip_id, str) or not _ID_RE.match(tip_id):
        return None
    if not isinstance(body, str) or not body.strip() or len(body) > _BODY_MAX_CHARS:
        return None
    if "\n" in body:
        return None
    if not isinstance(requires, list) or not all(
        isinstance(r, str) and r in REQUIREMENT_KEYS for r in requires
    ):
        return None
    if not isinstance(source, str) or not source.startswith("https://"):
        return None
    return {"id": tip_id, "body": body, "requires": list(requires), "source": source}


@functools.lru_cache(maxsize=1)
def load_exploratory_tips() -> tuple[dict[str, object], ...]:
    """The shipped exploratory tips, each validated. Empty when the file is unreadable.

    A malformed entry, or a second entry reusing an id, is dropped rather than
    served: a bad bank file must never take down the tips endpoints.
    """
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("exploratory tips file unreadable", exc_info=True)
        return ()
    raw = data.get("tips") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return ()
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in raw:
        tip = _clean(entry)
        if tip is None or tip["id"] in seen:
            continue
        seen.add(str(tip["id"]))
        out.append(tip)
    return tuple(out)


def exploratory_tip_ids() -> frozenset[str]:
    """Ids of the shipped exploratory tips."""
    return frozenset(str(t["id"]) for t in load_exploratory_tips())
