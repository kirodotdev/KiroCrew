"""The two kinds of song link Mood Music accepts, and nothing else.

A pick comes from the agent, which read it off a web page, so every link is
untrusted until it passes here.

* YouTube: only an 11-character video id survives. The page builds the embed URL
  itself, always on ``https://www.youtube-nocookie.com/embed/<id>``, so no host,
  path or query from the agent ever reaches an iframe.
* bilibili: an ``https`` URL whose host is exactly one of :data:`BILIBILI_HOSTS`,
  with no user info and no port. It is rebuilt from its parsed parts, so what is
  stored is what was checked.

The dashboard mirrors these rules in ``website/src/apps/mood-music/links.ts``.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

#: A YouTube video id: exactly 11 of ``A-Z a-z 0-9 _ -``.
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

#: The hosts a bilibili link may name, matched exactly.
BILIBILI_HOSTS = frozenset({"www.bilibili.com", "m.bilibili.com", "b23.tv"})

#: Hosts whose ``/watch?v=<id>`` link names a YouTube video.
_YOUTUBE_WATCH_HOSTS = frozenset(
    {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com"}
)

_MAX_URL = 2048


def valid_video_id(value: object) -> str | None:
    """*value* when it is a YouTube video id, else ``None``."""
    if isinstance(value, str) and VIDEO_ID_RE.fullmatch(value):
        return value
    return None


def _split_https(url: object):
    """The parsed parts of a plain ``https`` URL, or ``None``."""
    if not isinstance(url, str) or len(url) > _MAX_URL:
        return None
    # A backslash, a space or a control char is read differently by a browser
    # than by urlsplit, so a URL carrying one is refused rather than parsed.
    if "\\" in url or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port is not None:
        return None
    return parts


def youtube_video_id(url: object) -> str | None:
    """The video id a YouTube watch or share link names, or ``None``."""
    parts = _split_https(url)
    if parts is None:
        return None
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        return valid_video_id(parts.path.removeprefix("/"))
    if host in _YOUTUBE_WATCH_HOSTS and parts.path == "/watch":
        ids = parse_qs(parts.query).get("v") or []
        return valid_video_id(ids[0]) if len(ids) == 1 else None
    return None


def bilibili_url(url: object) -> str | None:
    """*url* rebuilt from its parts when it is an https bilibili link, else ``None``."""
    parts = _split_https(url)
    if parts is None:
        return None
    host = (parts.hostname or "").lower()
    if host not in BILIBILI_HOSTS:
        return None
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))
