"""Store-listing latency: a failed index fetch is not retried on every listing, and
the catalog's fresh inventory read runs beside the external-registry load.

No network: the index fetch, the catalog fetch and the external load are fakes.
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from external_registry_helpers import cache_dir_fixture  # noqa: F401

from kiro_crew.apps import registry
from kiro_crew.apps.registry import (
    _EXTERNAL_REGISTRY_CACHE_TTL,
    _EXTERNAL_REGISTRY_FAILURE_TTL,
    _external_registry_cache_identity,
    _external_registry_cache_path,
    _external_registry_failure_path,
    _load_external_registries,
    _read_external_registry_failure,
    _write_external_registry_cache,
    _write_external_registry_failure,
    refresh_registries,
)
from kiro_crew.apps.registry_pipeline import caches as caches_module
from kiro_crew.testing.clock import ManualClock

_STALE_ROWS = [{"name": "cool-app", "repo": "https://git.example.com/labs/cool", "branch": "main"}]


def _registry(branch: str = "main") -> SimpleNamespace:
    return SimpleNamespace(
        name="labs", repo="https://git.example.com/labs/apps.git", branch=branch, trust="index"
    )


def _backdate(path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


@pytest.fixture()
def labs(cache_dir, monkeypatch):
    """One configured registry whose index cache is past its TTL, and a fetch fake."""
    reg = _registry()
    identity = _external_registry_cache_identity(reg)
    _write_external_registry_cache(identity, _STALE_ROWS)
    _backdate(_external_registry_cache_path(identity), _EXTERNAL_REGISTRY_CACHE_TTL + 60)
    monkeypatch.setattr(registry, "_effective_registries", lambda: [reg])
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(registry, "_fetch_external_registry_index", fetch)
    return SimpleNamespace(reg=reg, identity=identity, fetch=fetch)


class TestIndexFailureMemory:
    @pytest.mark.asyncio
    async def test_a_failed_fetch_is_not_retried_by_the_next_listing(self, labs):
        first = await _load_external_registries()
        second = await _load_external_registries()

        assert labs.fetch.await_count == 1
        assert [r["name"] for r in first] == ["cool-app"]
        assert [r["name"] for r in second] == ["cool-app"]
        assert _read_external_registry_failure(labs.identity) is True

    @pytest.mark.asyncio
    async def test_the_listing_fetches_again_once_the_window_has_passed(self, labs):
        await _load_external_registries()
        _backdate(
            _external_registry_failure_path(labs.identity), _EXTERNAL_REGISTRY_FAILURE_TTL + 1
        )

        assert _read_external_registry_failure(labs.identity) is False

        rows = await _load_external_registries()

        assert labs.fetch.await_count == 2
        assert [r["name"] for r in rows] == ["cool-app"]
        assert _read_external_registry_failure(labs.identity) is True

    @pytest.mark.asyncio
    async def test_a_successful_fetch_clears_the_marker(self, labs):
        await _load_external_registries()
        _backdate(
            _external_registry_failure_path(labs.identity), _EXTERNAL_REGISTRY_FAILURE_TTL + 1
        )
        labs.fetch.return_value = [{"name": "new-app", "repo": "https://git.example.com/labs/new"}]

        rows = await _load_external_registries()

        assert [r["name"] for r in rows] == ["new-app"]
        assert not _external_registry_failure_path(labs.identity).exists()

    def test_the_marker_carries_the_failure_time_the_writer_read(self, labs, monkeypatch):
        """The marker's age is measured with ``time.time()``, so its stamp must come
        from that clock too and not from the filesystem's."""
        failed_at = time.time() - 120
        with monkeypatch.context() as patch:
            ManualClock(start=failed_at).install(patch, caches_module)
            _write_external_registry_failure(labs.identity)

        stamp = _external_registry_failure_path(labs.identity).stat().st_mtime
        assert abs(stamp - failed_at) <= 2  # FAT keeps two-second mtimes

    @pytest.mark.asyncio
    async def test_a_marker_a_moment_ahead_of_the_clock_still_holds_the_fetch_back(self, labs):
        """A coarse wall clock (about 15.6 ms on Windows) reads a just-written mtime
        as slightly in the future; that marker is still this code's own."""
        marker = _external_registry_failure_path(labs.identity)
        marker.write_text("")
        ahead = time.time() + 1
        os.utime(marker, (ahead, ahead))
        labs.fetch.return_value = [{"name": "new-app", "repo": "https://git.example.com/labs/new"}]

        rows = await _load_external_registries()

        assert labs.fetch.await_count == 0
        assert [r["name"] for r in rows] == ["cool-app"]

    @pytest.mark.asyncio
    async def test_a_marker_dated_in_the_future_does_not_hold_the_fetch_back(self, labs):
        marker = _external_registry_failure_path(labs.identity)
        marker.write_text("")
        later = time.time() + 3600
        os.utime(marker, (later, later))
        labs.fetch.return_value = [{"name": "new-app", "repo": "https://git.example.com/labs/new"}]

        rows = await _load_external_registries()

        assert labs.fetch.await_count == 1
        assert [r["name"] for r in rows] == ["new-app"]
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_another_branch_of_the_same_registry_is_not_held_back(self, labs, monkeypatch):
        await _load_external_registries()
        monkeypatch.setattr(registry, "_effective_registries", lambda: [_registry("next")])
        labs.fetch.return_value = [{"name": "new-app", "repo": "https://git.example.com/labs/new"}]

        rows = await _load_external_registries()

        assert labs.fetch.await_count == 2
        assert [r["name"] for r in rows] == ["new-app"]
        assert _read_external_registry_failure(labs.identity) is True

    @pytest.mark.asyncio
    async def test_refresh_fetches_inside_the_window(self, labs, monkeypatch):
        monkeypatch.setattr(registry, "list_registry", AsyncMock(return_value=[]))
        await _load_external_registries()
        labs.fetch.return_value = [{"name": "new-app", "repo": "https://git.example.com/labs/new"}]

        result = await refresh_registries()

        assert labs.fetch.await_count == 2
        assert result["refreshed"] == ["labs"]
        assert not _external_registry_failure_path(labs.identity).exists()

    @pytest.mark.asyncio
    async def test_a_failed_refresh_starts_the_window(self, labs, monkeypatch):
        monkeypatch.setattr(registry, "list_registry", AsyncMock(return_value=[]))

        result = await refresh_registries()
        await _load_external_registries()

        assert result["failed"] == ["labs"]
        assert labs.fetch.await_count == 1


class TestCatalogInventoryReadRunsBesideExternalLoad:
    @pytest.mark.asyncio
    async def test_the_two_reads_overlap(self, monkeypatch):
        """The fresh inventory read waits for the external load to START.

        Run one after the other, the inventory read would wait out its bound and
        record False, because the external load starts only after it returns.
        """
        external_started = threading.Event()
        overlapped: list[bool] = []

        def _inventory_read():
            overlapped.append(external_started.wait(timeout=10))
            return {}

        async def _external():
            external_started.set()
            return [{"name": "ext-app", "repo": "https://git.example.com/x", "_registry": "labs"}]

        async def _resolve(entry):
            return entry

        monkeypatch.setattr(
            registry.official_catalog,
            "list_catalog_rows",
            lambda: [{"name": "keep-app"}, {"name": "unseeded-git", "source": {"type": "git"}}],
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        monkeypatch.setattr(registry, "_catalog_installable_rows", _inventory_read)
        monkeypatch.setattr(registry, "_load_external_registries", _external)
        monkeypatch.setattr(registry, "_resolve_manifest", _resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}

        assert overlapped == [True], "the inventory read finished before the external load began"
        assert set(rows) == {"keep-app", "ext-app"}
