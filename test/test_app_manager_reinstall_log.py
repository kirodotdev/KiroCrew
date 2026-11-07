"""What ``install_app`` logs when its destination already holds an app folder with no record.

A default uninstall keeps the app's folder with only ``data/`` in it, so the next
install of the same name finds that folder. That is the ordinary reinstall, and it
must not be reported as crash recovery; a folder holding anything besides ``data/``
still is.
"""

from __future__ import annotations

import logging

from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from kiro_crew.apps.manager import get_app, install_app, uninstall_app

_ORPHAN = "Removing orphaned partial install"


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "kiro_crew.apps.manager" and r.levelno >= logging.WARNING
    ]


class TestReinstallLog:
    def test_a_reinstall_after_a_default_uninstall_is_not_crash_recovery(
        self, tmp_path, app_home, caplog
    ):
        assert install_app(_make_app_source(tmp_path)).ok
        (app_home / "apps" / "test-app" / "data" / "notes.md").write_text("kept\n")
        assert uninstall_app("test-app", keep_data=True).ok
        caplog.set_level(logging.INFO, logger="kiro_crew.apps.manager")

        assert install_app(_make_app_source(tmp_path / "again")).ok

        assert not [m for m in _warnings(caplog) if _ORPHAN in m]
        assert any(
            "Reinstalling test-app into the folder its uninstall kept" in r.getMessage()
            for r in caplog.records
        )
        assert (app_home / "apps" / "test-app" / "data" / "notes.md").read_text() == "kept\n"

    def test_a_folder_holding_more_than_data_is_still_an_orphan(self, tmp_path, app_home, caplog):
        assert install_app(_make_app_source(tmp_path)).ok
        assert uninstall_app("test-app", keep_data=True).ok
        (app_home / "apps" / "test-app" / "app.json").write_text("{}")  # a half-copied tree
        assert get_app("test-app") is None
        caplog.set_level(logging.INFO, logger="kiro_crew.apps.manager")

        assert install_app(_make_app_source(tmp_path / "again")).ok

        assert [m for m in _warnings(caplog) if _ORPHAN in m]
