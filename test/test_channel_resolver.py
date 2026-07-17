"""Unit tests for slack/channel_resolver.py — name cache + lazy refresh."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from kiro_crew.slack.channel_resolver import (
    _CACHE_FILENAME,
    _CACHE_TTL_SECS,
    ChannelNameResolver,
)


def _make_slack(channels: list[dict] | Exception | None = None) -> AsyncMock:
    """Return a mock SlackClientOps whose conversations_list returns *channels*."""
    slack = AsyncMock()
    if isinstance(channels, Exception):
        slack.conversations_list = AsyncMock(side_effect=channels)
    else:
        slack.conversations_list = AsyncMock(return_value=channels or [])
    return slack


class TestResolveMany:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_dict(self, tmp_path):
        resolver = ChannelNameResolver(cache_path=tmp_path / _CACHE_FILENAME)
        slack = _make_slack([])
        result = await resolver.resolve_many(slack, [])
        assert result == {}
        slack.conversations_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolves_unknown_ids_via_api(self, tmp_path):
        resolver = ChannelNameResolver(cache_path=tmp_path / _CACHE_FILENAME)
        slack = _make_slack([
            {"id": "C111", "name": "engineering"},
            {"id": "C222", "name": "random"},
        ])
        result = await resolver.resolve_many(slack, ["C111", "C222"])
        assert result == {"C111": "engineering", "C222": "random"}
        slack.conversations_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_unresolved_id_falls_back_to_id(self, tmp_path):
        resolver = ChannelNameResolver(cache_path=tmp_path / _CACHE_FILENAME)
        slack = _make_slack([{"id": "C111", "name": "engineering"}])
        result = await resolver.resolve_many(slack, ["C111", "C999_GHOST"])
        assert result == {"C111": "engineering", "C999_GHOST": "C999_GHOST"}

    @pytest.mark.asyncio
    async def test_cache_hit_skips_api(self, tmp_path):
        resolver = ChannelNameResolver(cache_path=tmp_path / _CACHE_FILENAME)
        slack = _make_slack([{"id": "C111", "name": "engineering"}])
        # First call populates cache
        await resolver.resolve_many(slack, ["C111"])
        # Second call should hit cache
        slack.conversations_list.reset_mock()
        result = await resolver.resolve_many(slack, ["C111"])
        assert result == {"C111": "engineering"}
        slack.conversations_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_cache_refreshes(self, tmp_path, monkeypatch):
        resolver = ChannelNameResolver(cache_path=tmp_path / _CACHE_FILENAME)
        slack = _make_slack([{"id": "C111", "name": "engineering-old"}])
        await resolver.resolve_many(slack, ["C111"])

        # Force stale by rewinding fetched_at
        resolver._fetched_at = time.time() - _CACHE_TTL_SECS - 1
        slack.conversations_list = AsyncMock(
            return_value=[{"id": "C111", "name": "engineering-new"}]
        )
        result = await resolver.resolve_many(slack, ["C111"])
        assert result == {"C111": "engineering-new"}
        slack.conversations_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_failure_returns_id_fallback(self, tmp_path):
        resolver = ChannelNameResolver(cache_path=tmp_path / _CACHE_FILENAME)
        slack = _make_slack(RuntimeError("rate limited"))
        result = await resolver.resolve_many(slack, ["C111"])
        # Failed refresh — falls through to id fallback
        assert result == {"C111": "C111"}


class TestDiskCache:
    @pytest.mark.asyncio
    async def test_persists_to_disk(self, tmp_path):
        cache_path = tmp_path / _CACHE_FILENAME
        resolver = ChannelNameResolver(cache_path=cache_path)
        slack = _make_slack([{"id": "C111", "name": "engineering"}])
        await resolver.resolve_many(slack, ["C111"])
        assert cache_path.exists()
        data = json.loads(cache_path.read_text())
        assert data["names"] == {"C111": "engineering"}
        assert data["fetched_at"] > 0

    @pytest.mark.asyncio
    async def test_loads_from_disk_on_init(self, tmp_path):
        cache_path = tmp_path / _CACHE_FILENAME
        cache_path.write_text(json.dumps({
            "names": {"C111": "preloaded"},
            "fetched_at": time.time(),
        }))
        resolver = ChannelNameResolver(cache_path=cache_path)
        slack = _make_slack([])
        # Cache is fresh — no API call expected
        result = await resolver.resolve_many(slack, ["C111"])
        assert result == {"C111": "preloaded"}
        slack.conversations_list.assert_not_called()

    def test_corrupt_disk_cache_starts_fresh(self, tmp_path):
        cache_path = tmp_path / _CACHE_FILENAME
        cache_path.write_text("not valid json {{{")
        # Should not raise
        resolver = ChannelNameResolver(cache_path=cache_path)
        assert resolver._names == {}
        assert resolver._fetched_at == 0.0


class TestGetCached:
    def test_returns_none_for_unknown(self, tmp_path):
        resolver = ChannelNameResolver(cache_path=tmp_path / _CACHE_FILENAME)
        assert resolver.get_cached("C999") is None

    def test_returns_cached_name(self, tmp_path):
        resolver = ChannelNameResolver(cache_path=tmp_path / _CACHE_FILENAME)
        resolver._names["C111"] = "engineering"
        assert resolver.get_cached("C111") == "engineering"
