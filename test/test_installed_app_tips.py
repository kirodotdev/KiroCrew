"""An installed app from a versionless index-tier registry is told of a newer release.

Such a registry leaves each app's version to its own ``app.json``. The listing
reads an installed app's ``app.json`` from its checkout, so nothing but these
tip reads tells ``update_status`` (and so the app's banner and the automatic
update) what the branch publishes now. Both the explicit refresh and the
listing's own index fetch run them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from external_registry_helpers import cache_dir_fixture  # noqa: F401

from kiro_crew.apps import installed_app_tips
from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps import update_status
from kiro_crew.apps.registry import (
    _expire_cache_file,
    _external_registry_cache_identity,
    _external_registry_cache_path,
    _manifest_cache_path,
    _read_manifest_cache,
    _write_manifest_cache,
    refresh_registries,
)

REGISTRY = "acme"
REGISTRY_URL = "https://github.com/acme/apps"
APP_URL = "https://github.com/acme/cool-app"


def _reg() -> Any:
    return types.SimpleNamespace(name=REGISTRY, repo=REGISTRY_URL, branch="main")


def _configure_registry(monkeypatch, repo: str = REGISTRY_URL) -> None:
    """Pin the configured registries to the one this module reads, at *repo*.

    ``_owner_designated_repo_target`` reads them through ``KiroCrewConfig.load``.
    """
    from kiro_crew.config.loader import ExternalRegistryConfig, KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.registries = [ExternalRegistryConfig(name=REGISTRY, repo=repo, branch="main")]
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))


def _row(name: str = "cool-app", **extra: Any) -> dict[str, Any]:
    """A fresh index row as the fetch returns it: tagged with its registry, no version."""
    return {
        "name": name,
        "repo": f"acme/{name}",
        "gitUrl": f"https://github.com/acme/{name}",
        "branch": "main",
        "_registry": REGISTRY,
        **extra,
    }


def _installed(name: str = "cool-app", **extra: Any) -> dict[str, Any]:
    """An installed record that kept the row it came from, as an install writes it."""
    return {
        "name": name,
        "version": "1.0.0",
        "source": f"registry:{name}",
        "sourceUrl": f"https://github.com/acme/{name}",
        "sourceRegistry": REGISTRY,
        **extra,
    }


@pytest.fixture(name="tips")
def tips_fixture(monkeypatch, cache_dir):  # noqa: F811 - the shared cache_dir fixture
    """Index tier, the given installed records, and a tip fetch that records its calls."""
    import kiro_crew.apps.manager as manager_mod

    state: dict[str, Any] = {
        "installed": [_installed()],
        "tip": {"name": "cool-app", "version": "1.2.0"},
        "calls": [],
        "pins": [],
        "owner_designated": [],
    }

    async def _fetch(repo, branch, subdirectory="", app_name="", git_url="", **kwargs):
        if app_name:
            # The listing's own fetch names the app, so it answers from the
            # installed checkout, which still names the installed version.
            provenance = kwargs.get("provenance")
            if isinstance(provenance, dict):
                provenance["source"] = "installed"
            return {"name": app_name, "version": "1.0.0"}
        state["calls"].append({"app_name": app_name, "git_url": git_url, "branch": branch})
        state["pins"].append(kwargs.get("commit", ""))
        state["owner_designated"].append(kwargs.get("owner_designated", False))
        tip = state["tip"]
        if isinstance(tip, BaseException):
            raise tip
        return dict(tip) if isinstance(tip, dict) else tip

    monkeypatch.setattr(reg_mod, "_registry_trust_tier_of", lambda reg: "index")
    monkeypatch.setattr(reg_mod, "_fetch_app_manifest", _fetch)
    monkeypatch.setattr(manager_mod, "list_apps", lambda: list(state["installed"]))
    return state


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


class TestWhichRowsAreRead:
    def test_a_versionless_row_of_an_installed_app_gets_its_tip_cached(self, tips):
        row = _row()
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 1
        # No app name: the fetch skips the installed checkout and clones the tip.
        assert tips["calls"] == [{"app_name": "", "git_url": APP_URL, "branch": "main"}]
        assert json.loads(_manifest_cache_path(row).read_text())["version"] == "1.2.0"

    def test_a_commit_on_the_row_still_reads_the_branch_tip(self, tips):
        # install_from_registry drops a non-catalog row's pin and installs the
        # branch tip, so the pinned tree would name a release no update installs.
        row = _row(commit="0" * 40)
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 1
        assert tips["pins"] == [""]
        assert tips["calls"] == [{"app_name": "", "git_url": APP_URL, "branch": "main"}]
        assert _read_manifest_cache(row)["version"] == "1.2.0"

    def test_a_row_in_another_repository_clones_anonymously(self, tips, monkeypatch):
        _configure_registry(monkeypatch)
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [_row()])) == 1
        assert tips["owner_designated"] == [False]
        assert tips["calls"][0]["git_url"] == APP_URL

    def test_a_row_in_the_registry_repository_takes_the_same_repository_carve_out(
        self, tips, monkeypatch
    ):
        # The listing and the install clone such a row with the configured target
        # and the owner's credentials; an anonymous clone of a private registry
        # repository fails, and the newer release would go unreported.
        configured = "https://user:tok@github.com/acme/apps"
        _configure_registry(monkeypatch, repo=configured)
        row = _row(gitUrl=REGISTRY_URL, repo="acme/apps", subdirectory="cool-app")
        tips["installed"] = [_installed(sourceUrl=REGISTRY_URL)]
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 1
        assert tips["owner_designated"] == [True]
        assert tips["calls"][0]["git_url"] == configured

    def test_a_row_with_its_own_version_is_not_read(self, tips):
        row = _row(version="1.1.0")
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert tips["calls"] == []

    def test_an_app_that_is_not_installed_is_not_read(self, tips):
        tips["installed"] = []
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [_row()])) == 0
        assert tips["calls"] == []

    @pytest.mark.parametrize(
        "record",
        [
            _installed(sourceRegistry="someone-else"),
            _installed(sourceUrl="https://github.com/someone-else/cool-app"),
            _installed(source="/home/me/cool-app"),
            {"name": "cool-app", "version": "1.0.0", "source": "registry:cool-app"},
        ],
        ids=["other-registry", "other-repository", "local-install", "no-provenance"],
    )
    def test_an_app_installed_from_another_source_is_not_read(self, tips, record):
        tips["installed"] = [record]
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [_row()])) == 0
        assert tips["calls"] == []

    def test_an_app_moved_here_from_another_pinned_registry_is_read(self, tips, monkeypatch):
        monkeypatch.setattr(
            reg_mod,
            "_pinned_registries",
            lambda: [
                types.SimpleNamespace(name=n, repo=f"https://forge/{n}.git")
                for n in (REGISTRY, "other-tier")
            ],
        )
        tips["installed"] = [_installed(sourceRegistry="other-tier")]
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [_row()])) == 1
        assert tips["calls"][0]["git_url"] == APP_URL

    def test_a_row_tagged_for_another_registry_is_not_read(self, tips):
        row = {**_row(), "_registry": "someone-else"}
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert tips["calls"] == []

    def test_a_row_whose_source_cache_is_still_fresh_is_not_read_again(self, tips):
        # That cache already answers the listing and update_status, so the
        # listing's hourly index fetch clones each tip at most once per its TTL.
        row = _row()
        assert _write_manifest_cache(row, {"name": "cool-app", "version": "1.1.0"})
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert tips["calls"] == []
        assert _read_manifest_cache(row)["version"] == "1.1.0"

    def test_a_row_whose_source_cache_lapsed_is_read(self, tips):
        row = _row()
        assert _write_manifest_cache(row, {"name": "cool-app", "version": "1.1.0"})
        _expire_cache_file(_manifest_cache_path(row))
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 1
        assert _read_manifest_cache(row)["version"] == "1.2.0"

    def test_an_owner_tier_registry_is_left_to_the_prewarm(self, tips, monkeypatch):
        monkeypatch.setattr(reg_mod, "_registry_trust_tier_of", lambda reg: "owner")
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [_row()])) == 0
        assert tips["calls"] == []

    def test_one_refresh_reads_at_most_the_cap(self, tips, monkeypatch):
        monkeypatch.setattr(installed_app_tips, "MAX_ROWS", 2)
        names = ["app-a", "app-b", "app-c"]
        tips["installed"] = [_installed(n) for n in names]
        tips["tip"] = None
        _run(installed_app_tips.refresh_installed_tips(_reg(), [_row(n) for n in names]))
        assert len(tips["calls"]) == 2

    def test_the_cap_counts_only_rows_that_qualify(self, tips, monkeypatch, caplog):
        monkeypatch.setattr(installed_app_tips, "MAX_ROWS", 2)
        tips["installed"] = [_installed(n) for n in ("app-a", "app-b")]
        tips["tip"] = None
        # Rows that do not qualify neither use up the cap nor trigger its warning.
        rows = [_row("not-installed"), _row("app-a"), _row("app-b"), _row("app-c")]
        with caplog.at_level(logging.INFO):
            _run(installed_app_tips.refresh_installed_tips(_reg(), rows))
        # The reads run concurrently, so compare the set, not the call order.
        assert sorted(c["git_url"] for c in tips["calls"]) == [
            "https://github.com/acme/app-a",
            "https://github.com/acme/app-b",
        ]
        assert "more than" not in caplog.text

    def test_a_qualifying_row_past_the_cap_is_named_in_a_warning(self, tips, monkeypatch, caplog):
        monkeypatch.setattr(installed_app_tips, "MAX_ROWS", 1)
        tips["installed"] = [_installed(n) for n in ("app-a", "app-b")]
        tips["tip"] = None
        with caplog.at_level(logging.WARNING):
            _run(installed_app_tips.refresh_installed_tips(_reg(), [_row("app-a"), _row("app-b")]))
        assert len(tips["calls"]) == 1
        assert f"registry {REGISTRY!r}" in caplog.text and "not read" in caplog.text


class TestWhatIsWritten:
    def test_a_manifest_for_another_app_is_not_cached(self, tips):
        tips["tip"] = {"name": "another-app", "version": "9.0.0"}
        row = _row()
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert not _manifest_cache_path(row).exists()

    def test_a_failed_fetch_writes_nothing_and_does_not_raise(self, tips):
        tips["tip"] = OSError("clone failed")
        row = _row()
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert not _manifest_cache_path(row).exists()

    def test_a_tip_still_running_at_the_budget_is_cut_off(self, tips, monkeypatch):
        monkeypatch.setattr(installed_app_tips, "BATCH_BUDGET_SECS", 0)
        never = asyncio.Event()

        async def _hang(*args, **kwargs):
            await never.wait()

        monkeypatch.setattr(reg_mod, "_fetch_app_manifest", _hang)
        row = _row()
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert not _manifest_cache_path(row).exists()


@pytest.fixture(name="listing")
def listing_fixture(tips, monkeypatch):
    """The one configured index-tier registry, whose index fetch returns one versionless row."""
    _configure_registry(monkeypatch)
    fetch = AsyncMock(return_value=[{k: v for k, v in _row().items() if k != "_registry"}])
    monkeypatch.setattr("kiro_crew.apps.registry._fetch_external_registry_index", fetch)
    return fetch


def _expire_index_cache() -> None:
    """Age the configured registry's index cache past its TTL, as an hour passing does."""
    (reg,) = reg_mod._effective_registries()
    _expire_cache_file(_external_registry_cache_path(_external_registry_cache_identity(reg)))


class TestTheListingReadsTheTip:
    """The listing's own index fetch reads the tip, with no refresh.

    It refetches the index whenever its one-hour cache lapsed. Without the tip
    read there, the row of an installed app resolves to the installed checkout
    and ``update_status`` keeps the lapsed source cache, so the app shows no
    newer release unless the user refreshes the store.
    """

    def test_a_fresh_index_fetch_reads_the_tip_before_the_rows_resolve(self, tips, listing):
        rows = _run(reg_mod._load_external_registries())

        assert listing.await_count == 1
        assert tips["calls"] == [{"app_name": "", "git_url": APP_URL, "branch": "main"}]
        (row,) = [r for r in rows if r.get("name") == "cool-app"]
        # The row resolves to what the branch publishes, not the installed checkout.
        assert _run(reg_mod._resolve_manifest(row))["version"] == "1.2.0"
        detail = update_status.attach_update("cool-app", _installed())
        assert detail["update"]["available"] is True
        assert detail["update"]["latestVersion"] == "1.2.0"

    def test_a_tip_is_cloned_once_per_manifest_ttl_not_once_per_index_fetch(self, tips, listing):
        _run(reg_mod._load_external_registries())
        # Within the index's hour: a cache hit, which never reads a tip.
        _run(reg_mod._load_external_registries())
        assert listing.await_count == 1
        # The index lapsed, but the tip read is still within the manifest TTL.
        _expire_index_cache()
        _run(reg_mod._load_external_registries())
        assert listing.await_count == 2
        assert len(tips["calls"]) == 1
        # Once that lapses too, the next index fetch reads the tip again.
        _expire_cache_file(_manifest_cache_path(_row()))
        _expire_index_cache()
        tips["tip"] = {"name": "cool-app", "version": "1.3.0"}
        _run(reg_mod._load_external_registries())
        assert len(tips["calls"]) == 2
        assert _read_manifest_cache(_row())["version"] == "1.3.0"

    def test_an_owner_tier_registry_is_not_read_here(self, tips, listing, monkeypatch):
        monkeypatch.setattr(reg_mod, "_registry_trust_tier_of", lambda reg: "owner")
        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", AsyncMock(return_value=0))
        _run(reg_mod._load_external_registries())
        assert listing.await_count == 1
        assert tips["calls"] == []


class TestRefreshReadsTheTip:
    @pytest.mark.asyncio
    async def test_the_tip_survives_the_refreshs_own_expiry_and_reaches_update_status(
        self, tips, monkeypatch
    ):
        reg = MagicMock()
        reg.name = REGISTRY
        reg.repo = "https://github.com/acme/apps"
        reg.branch = "main"
        config = MagicMock()
        config.registries = [reg]
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: config)
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(return_value=[{k: v for k, v in _row().items() if k != "_registry"}]),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry", AsyncMock(return_value=[{"name": "cool-app"}])
        )
        # A fresh source cache does not hold the refresh back: it expires it first.
        assert _write_manifest_cache(_row(), {"name": "cool-app", "version": "1.0.0"})

        result = await refresh_registries()

        assert result["refreshed"] == [REGISTRY]
        assert tips["calls"] and tips["calls"][0]["app_name"] == ""
        # Written after the refresh expired the manifest caches: the TTL-checked
        # read returns it, which an expired file would not.
        assert _read_manifest_cache(_row())["version"] == "1.2.0"
        detail = update_status.attach_update("cool-app", _installed())
        assert detail["update"]["available"] is True
        assert detail["update"]["latestVersion"] == "1.2.0"
