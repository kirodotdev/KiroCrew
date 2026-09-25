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
