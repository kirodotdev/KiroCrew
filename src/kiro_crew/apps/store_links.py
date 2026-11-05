"""The optional ``links`` field of an ``app.json``: extra links for the store page.

An app declares ``"links": [{"label": "Wiki", "url": "https://..."}]`` and the
App Store detail page shows each entry as a button in its Details card. The
manifest is third-party content, so every entry is checked here before any
reader sees it. A bad entry is dropped on its own and one log line names how
many went; a bad ``links`` value never fails the manifest it sits in.

There is no host allowlist. The button always shows the URL's host under the
label, so the reader sees where it goes before clicking, and only ``https:``
URLs with a plain host (no userinfo, no non-default port) are kept.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Most entries one manifest may declare; later ones are dropped in order.
MAX_LINKS = 6
#: Label length bounds, in characters, after surrounding whitespace is trimmed.
MAX_LINK_LABEL_CHARS = 40
#: Longest accepted URL, in characters.
MAX_LINK_URL_CHARS = 2048


def _has_control(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _clean_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip()
    if not 1 <= len(label) <= MAX_LINK_LABEL_CHARS or _has_control(label):
        return None
    return label


def _clean_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_LINK_URL_CHARS:
        return None
    # Whitespace, control characters and backslashes are refused outright: a
    # browser and urlsplit can disagree on how they split, and a URL whose host
    # the page shows must be the host the click reaches.
    if _has_control(value) or any(ch.isspace() or ch == "\\" for ch in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc or not parts.hostname:
        return None
    if port is not None and port != 443:
        return None
    return value


def sanitize_links(raw: Any, *, app_name: str = "") -> list[dict[str, str]]:
    """The valid entries of a manifest's ``links`` value, in declared order.

    Returns ``[]`` for a value that is not a list. Each kept entry is exactly
    ``{"label": str, "url": str}``; unknown keys on an entry are not carried.
    """
    if not isinstance(raw, list):
        logger.warning("app %r: ignoring app.json links, not a list", app_name)
        return []
    kept: list[dict[str, str]] = []
    dropped = 0
    over_cap = 0
    for entry in raw:
        if len(kept) >= MAX_LINKS:
            over_cap += 1
            continue
        if not isinstance(entry, dict):
            dropped += 1
            continue
        label = _clean_label(entry.get("label"))
        url = _clean_url(entry.get("url"))
        if label is None or url is None:
            dropped += 1
            continue
        kept.append({"label": label, "url": url})
    if dropped or over_cap:
        logger.warning(
            "app %r: dropped %d invalid app.json links entries and %d past the cap of %d",
            app_name,
            dropped,
            over_cap,
            MAX_LINKS,
        )
    return kept
