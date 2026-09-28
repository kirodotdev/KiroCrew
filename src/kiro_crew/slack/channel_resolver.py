"""Slack channel name resolver with in-memory + on-disk cache.

Resolves channel IDs (e.g. ``C0AU38Q0E4B``) to human-readable names (e.g.
``pcn-orchestrator-interest``) for display in the dashboard handoff dropdown
and any other surface that lists tracked channels.

Why this exists:
    ``ChannelConfig`` (in ``cfg.slack_channels``) stores ``activation`` and
    ``agent`` per channel but no ``name`` field, so without resolution
    ``api_slack_channels`` would surface raw IDs in the UI. A single
    ``conversations.list`` API call resolves them all at once and we cache the
    result so the dropdown opens instantly after the first warm-up.

Cache behavior:
    * In-memory dict, refreshed lazily on cache miss or TTL expiry.
    * Persisted to ``~/.kiro/crew/slack-channels.cache.json`` so a gateway
      restart does not refetch.
    * TTL = 1 hour. Slack channel renames are rare; users can manually clear
      the cache file if needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "slack-channels.cache.json"
_CACHE_TTL_SECS = 3600  # 1 hour
_REFRESH_LOCK_TIMEOUT_SECS = 30


class ChannelNameResolver:
    """Resolve Slack channel IDs to display names with caching.

    Single instance shared via ``DashboardState._channel_resolver`` so all
    callers benefit from one warm cache.
    """

    def __init__(self, cache_path: Path | None = None) -> None:
        self._cache_path = cache_path or (config_dir() / _CACHE_FILENAME)
        self._names: dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        try:
            if not self._cache_path.exists():
                return
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            names = data.get("names", {})
            fetched_at = float(data.get("fetched_at", 0))
            if isinstance(names, dict):
                self._names = {str(k): str(v) for k, v in names.items()}
                self._fetched_at = fetched_at
        except Exception as exc:
            # Corrupt cache file — start fresh, don't crash
            logger.warning("Failed to load channel name cache: %s", exc)

    def _save_to_disk(self) -> None:
        try:
            payload = json.dumps(
                {"names": self._names, "fetched_at": self._fetched_at},
                indent=2,
                ensure_ascii=False,
            )
            atomic_write(self._cache_path, payload)
        except Exception as exc:
            logger.warning("Failed to persist channel name cache: %s", exc)

    def _is_fresh(self) -> bool:
        return (time.time() - self._fetched_at) < _CACHE_TTL_SECS

    async def resolve_many(
        self,
        slack: SlackClientOps,
        channel_ids: list[str],
    ) -> dict[str, str]:
        """Return ``{channel_id: name}`` for the given IDs.

        Strategy:
            * Hit cache first; if all hits and cache is fresh, return without API call.
            * On any miss or stale cache, do a single ``conversations.list`` to
              warm the cache, then look up.
            * IDs that still cannot be resolved are returned with the ID itself
              as the name (preserves caller-side fallback semantics).
        """
        if not channel_ids:
            return {}

        unresolved = [cid for cid in channel_ids if cid not in self._names]
        if unresolved or not self._is_fresh():
            await self._refresh(slack)

        return {cid: self._names.get(cid, cid) for cid in channel_ids}

    async def _refresh(self, slack: SlackClientOps) -> None:
        """Fetch the full channel list once and update the cache."""
        # Use a lock so concurrent requests don't all fire conversations.list
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=_REFRESH_LOCK_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            logger.warning("Channel resolver refresh lock timed out; skipping")
            return
        try:
            # Re-check freshness inside the lock — another caller may have refreshed
            if self._is_fresh():
                return
            channels = await slack.conversations_list()
            new_names: dict[str, str] = {}
            for ch in channels or []:
                cid = ch.get("id", "")
                name = ch.get("name", "")
                if cid and name:
                    new_names[cid] = name
            if new_names:
                # Merge: prefer newly fetched, keep older entries (covers archived channels)
                merged = dict(self._names)
                merged.update(new_names)
                self._names = merged
                self._fetched_at = time.time()
                self._save_to_disk()
        except Exception as exc:
            logger.warning("Failed to refresh channel name cache: %s", exc)
        finally:
            self._lock.release()

    def get_cached(self, channel_id: str) -> str | None:
        """Synchronous cache lookup — returns None if not cached."""
        return self._names.get(channel_id)
