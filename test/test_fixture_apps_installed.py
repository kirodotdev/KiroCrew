"""Consumer coverage for the apps-installed seed fixture."""

import re
from pathlib import Path

import pytest

from kiro_crew.apps import manager
from kiro_crew.testing.fixtures import seeded_home

# The Apps rail's fold, owned by the frontend shell.
APP_SHELL_TSX = Path(__file__).resolve().parents[1] / "website" / "src" / "shell" / "appShell.tsx"


def test_apps_installed_fixture_is_read_by_app_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Listing and registering both write the process-wide orphan cache;
    # monkeypatch restores it at teardown so a later list_apps() on this
    # worker rescans its own home.
    monkeypatch.setattr(manager, "_orphaned_builtins_cache", None)
    with seeded_home("apps-installed"):
        installed = {app["name"]: app for app in manager.list_apps()}

        fixture_notes = installed["fixture-notes"]
        assert fixture_notes["enabled"] is True
        assert fixture_notes["origin"] == "local"
        assert fixture_notes["resources"] == "gateway"
        assert fixture_notes["lifecycle"] == "gateway"
        assert fixture_notes["manifest"]["name"] == "fixture-notes"

        data_file = manager.app_data_dir("fixture-notes") / "notes.md"
        assert data_file.is_file()

        # The workflows manifest ships defaultEnabled false, so a home without a
        # record registers it disabled and its routes answer 403. The seeded
        # record is builtin-owned, so registration keeps it enabled and writes
        # the manifest beside it.
        manager.register_builtin_apps()
        registered = {app["name"]: app for app in manager.list_apps()}

        workflows = registered["workflows"]
        assert workflows["enabled"] is True
        assert workflows["manifest"]["name"] == "workflows"


def _apps_nav_limit() -> int:
    """``APPS_NAV_LIMIT``: the rows the Apps rail shows before its "N more" toggle."""
    source = APP_SHELL_TSX.read_text(encoding="utf-8")
    match = re.search(r"\bconst APPS_NAV_LIMIT = (\d+)$", source, re.MULTILINE)
    assert match, f"APPS_NAV_LIMIT not found in {APP_SHELL_TSX}"
    return int(match.group(1))


def test_apps_installed_local_apps_alone_overflow_the_apps_rail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rail's "N more" toggle renders from this seed without any builtin app.

    A rail row is an enabled app whose manifest declares a ui.pages route
    (``appNavTarget`` in website/src/appNav.ts). Only the fixture's own local
    apps are counted: builtin apps are leaving core, so a rail that overflows
    only because of them would stop overflowing as they go.
    """
    monkeypatch.setattr(manager, "_orphaned_builtins_cache", None)
    with seeded_home("apps-installed"):
        local = [app for app in manager.list_apps() if app["origin"] == "local"]

    rail = {
        app["name"]: app["manifest"]["ui"]["pages"]
        for app in local
        if app["enabled"] is True and app["manifest"].get("ui", {}).get("pages")
    }
    assert "fixture-notes" in rail
    assert len(rail) > _apps_nav_limit()
    for name, pages in rail.items():
        assert pages[0]["route"] == f"/apps/{name}"
