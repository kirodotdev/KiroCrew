"""The store's catalog listing drops ``builtin`` rows this build does not ship.

A catalog document can be ahead of the gateway reading it. A ``builtin`` row it
publishes for a name this wheel has no manifest for is not installable here, so
listing it would render a card whose Install can only fail at click time.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps import registry


def _catalog(monkeypatch, rows):
    """Serve ``rows`` as the published catalog with nothing else reachable."""
    monkeypatch.setattr(registry.official_catalog, "list_catalog_rows", lambda: rows)
    monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
    monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
    monkeypatch.setattr(registry.official_catalog, "fetch_inventory_entries", lambda: [])

    async def _no_external():
        return []

    monkeypatch.setattr(registry, "_load_external_registries", _no_external)


class TestCatalogBuiltinRowsIntersectShippedBuiltins:
    @pytest.mark.asyncio
    async def test_unshipped_builtin_row_is_not_listed(self, monkeypatch):
        """A catalog ``builtin`` name this build lacks renders nothing at all."""
        _catalog(
            monkeypatch,
            [
                {"name": "shipped-app", "source": {"type": "builtin"}},
                {"name": "future-builtin", "source": {"type": "builtin"}},
            ],
        )
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})

        names = {r["name"] for r in await registry.list_catalog_apps()}
        assert "future-builtin" not in names

    @pytest.mark.asyncio
    async def test_shipped_builtin_row_is_still_listed(self, monkeypatch):
        """The other arm: a builtin this build DOES ship keeps its card."""
        _catalog(
            monkeypatch,
            [
                {"name": "shipped-app", "source": {"type": "builtin"}},
                {"name": "future-builtin", "source": {"type": "builtin"}},
            ],
        )
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})

        names = {r["name"] for r in await registry.list_catalog_apps()}
        assert "shipped-app" in names

    @pytest.mark.asyncio
    async def test_rows_of_other_source_types_are_untouched(self, monkeypatch):
        """Only ``builtin`` rows are judged against the shipped set."""
        _catalog(
            monkeypatch,
            [
                {"name": "plain-row", "displayName": "Plain Row"},
                {"name": "future-builtin", "source": {"type": "builtin"}},
            ],
        )
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: set())

        names = {r["name"] for r in await registry.list_catalog_apps()}
        assert names == {"plain-row"}

    @pytest.mark.asyncio
    async def test_edition_builtin_row_is_listed_while_the_catalog_is_reachable(self, monkeypatch):
        """An edition's ``registry_rows`` builtin survives the catalog replacing the seed."""
        _catalog(monkeypatch, [{"name": "shipped-app", "source": {"type": "builtin"}}])
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [
                {"name": "edition-app", "displayName": "Edition App", "source": {"type": "builtin"}}
            ],
        )
        monkeypatch.setattr(
            registry, "shipped_builtin_names", lambda: {"shipped-app", "edition-app"}
        )

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert set(rows) == {"shipped-app", "edition-app"}
        assert rows["edition-app"]["displayName"] == "Edition App"

    @pytest.mark.asyncio
    async def test_edition_rows_only_add_names_the_catalog_does_not_publish(self, monkeypatch):
        """A catalog row wins a name collision, as it does over the seed."""
        _catalog(
            monkeypatch,
            [{"name": "shipped-app", "displayName": "Catalog", "source": {"type": "builtin"}}],
        )
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [
                {"name": "shipped-app", "displayName": "Edition", "source": {"type": "builtin"}}
            ],
        )
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})

        rows = await registry.list_catalog_apps()
        assert [r["displayName"] for r in rows] == ["Catalog"]

    @pytest.mark.asyncio
    async def test_the_first_of_two_edition_rows_sharing_a_name_is_listed(self, monkeypatch):
        """Edition rows dedupe by name as ``_load_registry_file`` does: the first wins."""
        _catalog(monkeypatch, [{"name": "shipped-app", "source": {"type": "builtin"}}])
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [
                {"name": "edition-app", "displayName": "First", "source": {"type": "builtin"}},
                {"name": "edition-app", "displayName": "Second", "source": {"type": "builtin"}},
            ],
        )
        monkeypatch.setattr(
            registry, "shipped_builtin_names", lambda: {"shipped-app", "edition-app"}
        )

        rows = [r for r in await registry.list_catalog_apps() if r["name"] == "edition-app"]
        assert [r["displayName"] for r in rows] == ["First"]

    @pytest.mark.asyncio
    async def test_edition_row_gets_its_manifest_copy_and_probe_while_the_catalog_is_reachable(
        self, monkeypatch
    ):
        """A bare edition row is dressed and probed as ``list_registry`` does offline."""
        _catalog(monkeypatch, [{"name": "shipped-app", "source": {"type": "builtin"}}])
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [
                {
                    "name": "edition-app",
                    "repo": "example/edition-app",
                    "detectInstalled": "true",
                }
            ],
        )
        fetched: list[str] = []

        async def _resolve_manifest(entry):
            fetched.append(entry["name"])
            return {**entry, "displayName": "Edition App", "version": "1.2.0"}

        probed: list[str] = []

        async def _detect_installed_probe(entries, installed_map):
            probed.extend(e["name"] for e in entries)
            return {"edition-app"}

        monkeypatch.setattr(registry, "_resolve_manifest", _resolve_manifest)
        monkeypatch.setattr(registry, "_detect_installed_probe", _detect_installed_probe)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert fetched == ["edition-app"], "only the edition row pays the manifest fetch"
        assert probed == ["edition-app"]
        edition = rows["edition-app"]
        assert edition["displayName"] == "Edition App"
        assert edition["version"] == "1.2.0"
        assert edition["installed"] is True

    @pytest.mark.asyncio
    async def test_edition_row_verified_follows_its_index_author_not_its_manifest(
        self, monkeypatch
    ):
        """The badge reads the edition index's author, never the app's own app.json."""
        _catalog(monkeypatch, [{"name": "shipped-app", "source": {"type": "builtin"}}])
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [
                {"name": "indexed-by-us", "repo": "example/indexed-by-us", "author": "kirocrew"},
                {"name": "claims-us", "repo": "example/claims-us"},
            ],
        )

        async def _resolve_manifest(entry):
            # Each app's own manifest says the opposite of its index row.
            author = "someone-else" if entry["name"] == "indexed-by-us" else "kirocrew"
            return {**entry, "author": author}

        monkeypatch.setattr(registry, "_resolve_manifest", _resolve_manifest)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert rows["indexed-by-us"]["verified"] is True
        assert rows["claims-us"]["verified"] is False

    @pytest.mark.asyncio
    async def test_edition_row_reports_an_update_from_its_manifest_version(self, monkeypatch):
        """``updateAvailable`` compares the manifest's version with the installed one."""
        _catalog(monkeypatch, [{"name": "shipped-app", "source": {"type": "builtin"}}])
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})
        monkeypatch.setattr(
            registry,
            "list_installed_apps",
            lambda: [{"name": "edition-app", "version": "1.0.0", "enabled": True}],
        )
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [{"name": "edition-app", "repo": "example/edition-app"}],
        )

        async def _resolve_manifest(entry):
            return {**entry, "version": "1.1.0"}

        monkeypatch.setattr(registry, "_resolve_manifest", _resolve_manifest)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert rows["edition-app"]["updateAvailable"] is True

    @pytest.mark.asyncio
    async def test_edition_builtin_row_this_build_does_not_ship_is_dropped(self, monkeypatch):
        """Edition rows pass the same shipped-builtin filter as catalog rows."""
        _catalog(monkeypatch, [{"name": "shipped-app", "source": {"type": "builtin"}}])
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [{"name": "absent-app", "source": {"type": "builtin"}}],
        )
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})

        names = {r["name"] for r in await registry.list_catalog_apps()}
        assert names == {"shipped-app"}

    @pytest.mark.asyncio
    async def test_edition_row_a_bundled_seed_row_shadows_is_not_listed(self, monkeypatch):
        """Install-by-name gives a bundled seed row the name, so its edition twin is not listed."""
        _catalog(monkeypatch, [{"name": "shipped-app", "source": {"type": "builtin"}}])
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})
        bundled = {"name": "shared-app", "repo": "example/bundled-app"}
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [bundled])
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [
                {"name": "shared-app", "repo": "example/edition-app"},
                {"name": "shared-app", "repo": "example/bundled-app", "displayName": "Late"},
            ],
        )

        async def _resolve_manifest(entry):
            return {**entry, "displayName": "Edition App"}

        monkeypatch.setattr(registry, "_resolve_manifest", _resolve_manifest)

        names = {r["name"] for r in await registry.list_catalog_apps()}
        assert names == {"shipped-app"}

    @pytest.mark.asyncio
    async def test_edition_row_the_merged_seed_carries_is_still_listed(self, monkeypatch):
        """The real seed already holds edition rows; that alone must not hide them."""
        _catalog(monkeypatch, [{"name": "shipped-app", "source": {"type": "builtin"}}])
        monkeypatch.setattr(registry, "shipped_builtin_names", lambda: {"shipped-app"})
        edition = {"name": "edition-app", "repo": "example/edition-app"}
        monkeypatch.setattr(
            registry,
            "_load_registry_file",
            lambda: [{"name": "core-app", "repo": "example/core-app"}, dict(edition)],
        )
        monkeypatch.setattr(registry, "_edition_registry_rows", lambda: [dict(edition)])

        async def _resolve_manifest(entry):
            return {**entry, "displayName": "Edition App"}

        monkeypatch.setattr(registry, "_resolve_manifest", _resolve_manifest)

        names = {r["name"] for r in await registry.list_catalog_apps()}
        assert "edition-app" in names

    def test_a_manifest_registration_would_reject_is_not_in_the_shipped_set(
        self, tmp_path, monkeypatch
    ):
        """Discovery validates the manifest; registration wants more than that."""
        import json

        from kiro_crew.apps import manager
        from kiro_crew.apps.discovery import discover_builtin_apps

        builtins_dir = tmp_path / "builtins"
        (builtins_dir / "authorless").mkdir(parents=True)
        (builtins_dir / "authorless" / "app.json").write_text(
            json.dumps(
                {
                    "name": "authorless",
                    "version": "1.0.0",
                    "displayName": "Authorless",
                    "description": "no author, so registration skips it",
                }
            ),
            encoding="utf-8",
        )

        discovered = discover_builtin_apps(builtins_dir)
        assert [a["name"] for a in discovered] == ["authorless"]
        assert manager._validate_builtin_app(discovered[0])

        monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
        monkeypatch.setattr(manager, "discover_builtin_apps", lambda: discovered)
        monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])

        assert manager.shipped_builtin_names() == set()

    def test_an_installed_builtin_whose_manifest_broke_is_not_orphaned(self, tmp_path, monkeypatch):
        """Orphan detection asks a wider question and must not inherit the screen."""
        from kiro_crew.apps import manager

        valid = {
            "name": "brokenish",
            "version": "1.0.0",
            "displayName": "Brokenish",
            "description": "registers cleanly at first",
            "author": "kiro",
        }
        apps_root = tmp_path / "apps"
        apps_root.mkdir()
        monkeypatch.setattr(manager, "app_dir", lambda name: apps_root / name)
        monkeypatch.setattr(manager, "apps_dir", lambda: apps_root)
        monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
        monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])
        monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [dict(valid)])
        # Scanning writes this process global; monkeypatch restores it at teardown
        # so a later `list_apps()` on this worker still sees real orphan markers.
        monkeypatch.setattr(manager, "_orphaned_builtins_cache", None)
        manager.register_builtin_apps()
        assert (apps_root / "brokenish" / "installed.json").is_file()

        broken = {k: v for k, v in valid.items() if k != "author"}
        monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [dict(broken)])

        assert "brokenish" not in manager.shipped_builtin_names()
        assert "brokenish" not in manager.detect_orphaned_builtins(force_refresh=True)

    def test_shipped_builtin_names_reports_the_packages_own_builtins(self):
        """The helper reads the real shipped set, not a hand-written list."""
        from kiro_crew.apps.discovery import discover_builtin_apps
        from kiro_crew.apps.manager import shipped_builtin_names

        discovered = {a["name"] for a in discover_builtin_apps()}
        assert discovered
        assert discovered <= shipped_builtin_names()
