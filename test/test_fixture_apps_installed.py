"""Consumer coverage for the apps-installed seed fixture."""

from kiro_crew.apps import manager
from kiro_crew.testing.fixtures import seeded_home


def test_apps_installed_fixture_is_read_by_app_manager() -> None:
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
