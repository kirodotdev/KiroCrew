"""An installed app's own app.json never stands in for what its source publishes.

The listing reads an installed app's display copy from its persistent checkout,
which names the INSTALLED version. That answer is cached apart from the source's
manifest cache, which the listing's ``version``, the update status and the
owner-tier prewarm's warm check read as what the branch publishes. Stored in the
source cache it made an installed app read as current for the cache's life and
kept the prewarm from ever renewing it (an app from a registry whose rows carry no
``version`` was then never offered an update).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from apps_registry_coverage_helpers import cache_dir_fixture  # noqa: F401
from registry_store_art_prewarm_helpers import (
    MANIFEST,
    _entry,
    _fake_fetch,
    _pin_registry,
)

from kiro_crew.apps import registry
from kiro_crew.apps.registry import (
    _TRUST_OWNER,
    _installed_manifest_cache_path,
    _manifest_cache_path,
    _prewarm_owner_tier_store_assets,
    _read_installed_manifest_cache,
    _read_manifest_cache,
    _write_manifest_cache,
)

_ROW = {"name": "demo", "gitUrl": "https://github.com/o/demo.git", "branch": "main"}


def _installed_checkout(monkeypatch, tmp_path, version: str) -> None:
    """An installed checkout of _ROW's source whose app.json is at *version*."""
    src = tmp_path / "app-sources" / "demo"
    src.mkdir(parents=True)
    body = {"name": "demo", "description": "installed copy", "version": version}
    (src / "app.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

    async def _true(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(registry, "_clone_origin_matches", _true)
    monkeypatch.setattr(registry, "_clone_branch_matches", _true)
    monkeypatch.setattr(registry, "_owner_designated_repo_target", lambda entry: "")

    def _no_remote(url: str) -> bool:
        raise AssertionError("remote clone attempted although the checkout answers")

    monkeypatch.setattr(registry, "is_clone_host_trusted", _no_remote)


class TestListingCache:
    @pytest.mark.asyncio
    async def test_the_installed_copy_is_cached_apart_from_the_source(
        self, cache_dir, monkeypatch, tmp_path
    ):
        _installed_checkout(monkeypatch, tmp_path, "0.1.11")
        got = await registry._resolve_manifest(dict(_ROW))
        assert got["version"] == "0.1.11"
        assert _read_manifest_cache(_ROW) is None
        assert _read_installed_manifest_cache(_ROW)["version"] == "0.1.11"

    @pytest.mark.asyncio
    async def test_the_cached_installed_copy_answers_the_next_listing(
        self, cache_dir, monkeypatch, tmp_path
    ):
        _installed_checkout(monkeypatch, tmp_path, "0.1.11")
        await registry._resolve_manifest(dict(_ROW))

        async def _no_fetch(*a: Any, **k: Any) -> None:
            raise AssertionError("the cached installed copy should answer")

        monkeypatch.setattr(registry, "_fetch_app_manifest", _no_fetch)
        got = await registry._resolve_manifest(dict(_ROW))
        assert got["description"] == "installed copy"

    @pytest.mark.asyncio
    async def test_a_source_manifest_wins_over_the_installed_copy(
        self, cache_dir, monkeypatch, tmp_path
    ):
        """The branch tip a prewarm fetched is what the listing's version reads."""
        _installed_checkout(monkeypatch, tmp_path, "0.1.11")
        await registry._resolve_manifest(dict(_ROW))
        assert _write_manifest_cache(_ROW, {"name": "demo", "version": "0.1.13"})
        got = await registry._resolve_manifest(dict(_ROW))
        assert got["version"] == "0.1.13"

    @pytest.mark.asyncio
    async def test_a_remote_answer_still_fills_the_source_cache(self, cache_dir, monkeypatch):
        async def _remote(*a: Any, **k: Any) -> dict[str, Any]:
            return {"name": "demo", "version": "2.0.0"}

        monkeypatch.setattr(registry, "_owner_designated_repo_target", lambda entry: "")
        monkeypatch.setattr(registry, "_fetch_app_manifest", _remote)
        await registry._resolve_manifest(dict(_ROW))
        assert _read_manifest_cache(_ROW)["version"] == "2.0.0"
        assert not _installed_manifest_cache_path(_ROW).exists()

    def test_the_two_caches_never_share_a_file(self):
        assert _installed_manifest_cache_path(_ROW) != _manifest_cache_path(_ROW)
        assert _installed_manifest_cache_path(_ROW).name.endswith(".installed.json")


class TestPrewarmOrder:
    @pytest.mark.asyncio
    async def test_an_installed_app_is_cloned_before_every_other_row(self, monkeypatch):
        """Its source cache is the only place the branch's version can come from, so
        it goes first even behind cold rows."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(registry, "_PREWARM_CONCURRENCY", 1)
        rows = []
        for i in range(4):
            url = f"https://forge.example.com/org/app-{i:02d}.git"
            rows.append(_entry(name=f"app-{i:02d}", gitUrl=url, repo=url))
        installed = _installed_manifest_cache_path(rows[3])
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text(json.dumps({"name": "app-03", "version": "0.1.0"}), encoding="utf-8")
        calls = _fake_fetch(monkeypatch, {"ui/icon.svg": b"<svg/>"}, dict(MANIFEST))

        assert await _prewarm_owner_tier_store_assets(reg, rows) == 4

        order = [c["git_url"].rsplit("/", 1)[-1].removesuffix(".git") for c in calls]
        assert order[0] == "app-03"
        assert order[1:] == ["app-00", "app-01", "app-02"]
