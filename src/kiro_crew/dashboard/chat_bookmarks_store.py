"""The per-session bookmarks store: links a person or the agent kept with one chat.

One JSON list under the data home (``chat_bookmarks.json``), one record per
bookmark keyed by the chat slot it belongs to. The store loads lazily, off the
event loop, on its first use, and every mutation runs under one lock and is
persisted with an atomic, owner-only replace before it is published.

The load contract matches ``chat_pins.json`` (``state_parts/sidebar_stores.py``):
a malformed record is kept verbatim and written back on the next save, and a
file that could not be parsed at all is never overwritten.

Both writers share :meth:`ChatBookmarkStore.add` and :meth:`ChatBookmarkStore.remove`:
the HTTP routes in :mod:`kiro_crew.dashboard.chat_bookmarks` and the agent's
``chat_bookmark`` directive applier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger(__name__)

CHAT_BOOKMARKS_FILE = "chat_bookmarks.json"
MAX_BOOKMARK_URL_CHARS = 2048
MAX_BOOKMARK_TITLE_CHARS = 200
MAX_BOOKMARKS_PER_SLOT = 100
_ORIGINS = frozenset({"user", "agent"})


class BookmarkError(ValueError):
    """A refused bookmark write; ``code`` is the machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_url(raw: str) -> str:
    """Return *raw* as a bookmarkable URL, or raise :class:`BookmarkError`.

    Only absolute ``http``/``https`` URLs with a host are kept: a bookmark is
    rendered as a link, so ``javascript:``, ``data:`` and ``file:`` targets are
    refused rather than escaped. A URL the redactors would rewrite is refused
    too, so no credential is ever stored and the served URL is the stored one.
    """
    url = raw.strip()
    if not url:
        raise BookmarkError("missing_url", "url is required")
    if len(url) > MAX_BOOKMARK_URL_CHARS:
        raise BookmarkError("url_too_large", f"url exceeds {MAX_BOOKMARK_URL_CHARS} characters")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
        raise BookmarkError("invalid_url", "url must not contain whitespace")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise BookmarkError("invalid_url", "url is not a valid URL") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise BookmarkError("invalid_url", "url must be an absolute http or https URL")
    if _redact(url) != url:
        raise BookmarkError("url_contains_credential", "url carries a credential and was not kept")
    return url


def _redact(text: str) -> str:
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


def public_bookmark(record: dict[str, Any]) -> dict[str, Any]:
    """The record as served: URL and title re-redacted at the output boundary.

    A record on disk may predate the current redactor patterns, so both
    free-text fields run through the redactors on every response path.
    """
    return {
        "id": record["id"],
        "slot_key": record["slot_key"],
        "url": _redact(record["url"]),
        "title": _redact(record.get("title", "")),
        "origin": record.get("origin", "user"),
        "created_at": record["created_at"],
    }


def _is_valid(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    for key in ("id", "slot_key", "url", "created_at"):
        if not isinstance(record.get(key), str) or not record[key]:
            return False
    if not isinstance(record.get("title", ""), str):
        return False
    if len(record["url"]) > MAX_BOOKMARK_URL_CHARS:
        return False
    return len(record.get("title", "")) <= MAX_BOOKMARK_TITLE_CHARS


class ChatBookmarkStore:
    """Bookmarks for every chat slot, loaded once and mutated under one lock."""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._unparsed: list[Any] = []
        self._loaded = False
        self._load_failed = False
        # LoopBoundLock: the dashboard state outlives any single event loop.
        self._lock = LoopBoundLock()

    def _path(self):
        return config_dir() / CHAT_BOOKMARKS_FILE

    def _load(self) -> None:
        path = self._path()
        if not path.exists():
            self._items, self._unparsed, self._load_failed = [], [], False
            return
        try:
            raw = loads_user_json(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            logger.warning("%s has malformed content: %s", CHAT_BOOKMARKS_FILE, exc)
            self._items, self._load_failed = [], True
            return
        if not isinstance(raw, list):
            logger.warning("%s is not a list; ignoring", CHAT_BOOKMARKS_FILE)
            self._items, self._load_failed = [], True
            return
        self._items = [r for r in raw if _is_valid(r)]
        self._unparsed = [r for r in raw if not _is_valid(r)]
        if self._unparsed:
            logger.warning(
                "%s: kept %d malformed record(s) unchanged",
                CHAT_BOOKMARKS_FILE,
                len(self._unparsed),
            )
        self._load_failed = False

    def _save(self) -> None:
        if self._load_failed:
            raise OSError(
                f"{CHAT_BOOKMARKS_FILE} could not be read at load; refusing to replace it"
            )
        atomic_write(
            self._path(),
            json.dumps([*self._items, *self._unparsed]),
            fsync=True,
            mode=0o600,
        )

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            # A transient OSError propagates and leaves the store unloaded, so
            # the next call retries instead of serving an empty list.
            await asyncio.to_thread(self._load)
            self._loaded = True

    async def list_for(self, slot_key: str) -> list[dict[str, Any]]:
        """The slot's bookmarks, oldest first, as served."""
        async with self._lock:
            await self._ensure_loaded()
            rows = [r for r in self._items if r["slot_key"] == slot_key]
        rows.sort(key=lambda r: r["created_at"])
        return [public_bookmark(r) for r in rows]

    async def add(
        self, slot_key: str, url: str, title: str = "", *, origin: str = "user"
    ) -> tuple[dict[str, Any], bool]:
        """Bookmark *url* on *slot_key*; return ``(record, created)``.

        Idempotent per ``(slot_key, url)``: bookmarking a URL the slot already
        keeps returns the existing record, with its title updated when a new
        non-empty one is given.
        """
        clean_url = normalize_url(url)
        clean_title = _redact(" ".join(title.split()))[:MAX_BOOKMARK_TITLE_CHARS]
        if origin not in _ORIGINS:
            raise ValueError(f"unknown bookmark origin {origin!r}")
        async with self._lock:
            await self._ensure_loaded()
            existing = next(
                (r for r in self._items if r["slot_key"] == slot_key and r["url"] == clean_url),
                None,
            )
            if existing is not None:
                if not clean_title or clean_title == existing.get("title", ""):
                    return public_bookmark(existing), False
                previous = existing.get("title", "")
                existing["title"] = clean_title
                try:
                    await asyncio.to_thread(self._save)
                except Exception:
                    existing["title"] = previous
                    raise
                return public_bookmark(existing), False
            if sum(1 for r in self._items if r["slot_key"] == slot_key) >= MAX_BOOKMARKS_PER_SLOT:
                raise BookmarkError(
                    "bookmark_limit_reached",
                    f"this session already keeps {MAX_BOOKMARKS_PER_SLOT} bookmarks",
                )
            record = {
                "id": uuid.uuid4().hex[:12],
                "slot_key": slot_key,
                "url": clean_url,
                "title": clean_title,
                "origin": origin,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._items.append(record)
            try:
                await asyncio.to_thread(self._save)
            except Exception:
                self._items.pop()
                raise
        return public_bookmark(record), True

    async def remove(self, slot_key: str, *, bookmark_id: str = "", url: str = "") -> bool:
        """Remove one bookmark of *slot_key* by id or by URL; return whether one went."""

        def _matches(r: dict[str, Any]) -> bool:
            if r["slot_key"] != slot_key:
                return False
            return r["id"] == bookmark_id if bookmark_id else r["url"] == url.strip()

        async with self._lock:
            await self._ensure_loaded()
            idx = next((i for i, r in enumerate(self._items) if _matches(r)), None)
            if idx is None:
                return False
            removed = self._items.pop(idx)
            try:
                await asyncio.to_thread(self._save)
            except Exception:
                self._items.insert(idx, removed)
                raise
        return True

    async def slot_of(self, bookmark_id: str) -> str | None:
        """The slot key a bookmark id belongs to, or ``None``."""
        async with self._lock:
            await self._ensure_loaded()
            return next((r["slot_key"] for r in self._items if r["id"] == bookmark_id), None)
