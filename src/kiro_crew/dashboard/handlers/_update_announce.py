"""The advance notice of the next update, read from ``announce.json``.

The release feed (``latest-cli.json``) carries a version and a publish date and
no notes. The About page's "Upcoming update" block needs a version, a date, up
to three highlights and a link to the full notes, so the release flow may
publish one optional file beside the channel's manifest::

    {base}/feed/{channel}/announce.json

A missing, unreadable, oversized or malformed file means "no notice", never an
error: this is display metadata that nothing acts on, so every failure here
returns ``None`` and the update check carries on. Plain string handling only,
no regular expressions.
"""

from __future__ import annotations

import datetime
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TypedDict

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")

#: Schema id of ``announce.json``; a file naming another schema is ignored.
ANNOUNCE_SCHEMA = "kirocrew-update-announcement-v1"

#: Most highlights the About block shows, and the longest one, in characters.
MAX_HIGHLIGHTS = 3
MAX_HIGHLIGHT_CHARS = 140

#: Notes beyond this are not read: highlights come from the top of the text.
_MAX_NOTES_CHARS = 4000
_MAX_VERSION_CHARS = 64
_MAX_URL_CHARS = 300


class Announcement(TypedDict):
    """The validated fields of ``announce.json``."""

    version: str
    date: str
    highlights: list[str]
    notes_url: str


class Upcoming(TypedDict):
    """What the About block renders: an announced build the feed does not offer yet."""

    state: str
    version: str
    date: str
    highlights: list[str]
    notes_url: str


def day_of(value: object) -> str:
    """The leading ``YYYY-MM-DD`` of a date or timestamp string, else ``""``."""
    if not isinstance(value, str) or len(value) < 10:
        return ""
    day = value[:10]
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        return ""
    return day


def https_url(value: object) -> str:
    """``value`` when it is a bounded ``https://`` URL, else ``""``."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if len(url) > _MAX_URL_CHARS or not url.startswith("https://") or len(url) == len("https://"):
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        return ""
    return url


def _clean(text: str) -> str:
    return " ".join(text.replace("**", "").replace("`", "").split())


def _clip(text: str) -> str:
    if len(text) <= MAX_HIGHLIGHT_CHARS:
        return text
    return text[: MAX_HIGHLIGHT_CHARS - 1].rstrip() + "…"


def highlights_from_notes(notes: str) -> list[str]:
    """Up to :data:`MAX_HIGHLIGHTS` highlights from a release-notes text.

    List items (``- `` or ``* ``) are the highlights. Notes written as plain
    lines, with no list at all, fall back to their first non-heading lines.
    """
    lines = [line.strip() for line in notes[:_MAX_NOTES_CHARS].splitlines()]
    items = [_clean(line[2:]) for line in lines if line[:2] in ("- ", "* ")]
    if not any(items):
        items = [_clean(line) for line in lines if line and not line.startswith("#")]
    return [_clip(item) for item in items if item][:MAX_HIGHLIGHTS]


def parse_announcement(raw: bytes, *, channel: str) -> Announcement | None:
    """Validate the file's bytes; ``None`` for anything not a usable notice."""
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        logger.debug("announce.json is not valid JSON")
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("schema") != ANNOUNCE_SCHEMA or doc.get("channel") != channel:
        return None
    version = doc.get("version")
    if not isinstance(version, str) or not version or len(version) > _MAX_VERSION_CHARS:
        return None
    notes = doc.get("notes")
    highlights = highlights_from_notes(notes) if isinstance(notes, str) else []
    return {
        "version": version,
        "date": day_of(doc.get("date")),
        "highlights": highlights,
        "notes_url": https_url(doc.get("notes_url")),
    }


async def fetch_announcement(
    url: str,
    fetch: Callable[[str], Awaitable[tuple[int, bytes]]],
    *,
    channel: str,
    max_bytes: int,
) -> Announcement | None:
    """GET ``url`` through the checker's own ``fetch``; never raises."""
    try:
        status, raw = await fetch(url)
    except Exception:
        logger.debug("announce.json fetch failed: %s", url, exc_info=True)
        return None
    if status != 200 or len(raw) > max_bytes:
        return None
    return parse_announcement(raw, channel=channel)


def compose_upcoming(announcement: Announcement, *, version: str) -> Upcoming:
    """The About block's record for an announcement, shown under ``version``."""
    return {
        "state": "announced",
        "version": version,
        "date": announcement["date"],
        "highlights": announcement["highlights"],
        "notes_url": announcement["notes_url"],
    }
