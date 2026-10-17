"""Consumer coverage for the apps-installed seed fixture."""

import pytest

from kiro_crew.apps import manager
from kiro_crew.testing.fixtures import seeded_home


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
