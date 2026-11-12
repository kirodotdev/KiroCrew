"""``uninstall_app_in_process``: the uninstall sequence for in-process callers.

An edition that retires an app it does not want on a machine (for example an app whose
features became native) has no owner request to send to ``POST /api/apps/{name}/uninstall``,
and the sequence behind it is private. These tests pin the public entry: it runs the same
teardown as the route, keeps ``data/``, removes the app's crons, records its own caller in
the audit trail, and refuses what the route refuses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app


def _web_app(crons: object | None) -> web.Application:
    app = web.Application()
    state = MagicMock()
    state.crons = crons
    state.sessions = None
    state.notification_bus = None
    app["state"] = state
    return app


@pytest.mark.asyncio
class TestUninstallAppInProcess:
    async def test_removes_the_app_keeps_data_and_drops_its_crons(self, tmp_path, app_home):
        assert install_app(_make_app_source(tmp_path)).ok
        data_file = app_home / "apps" / "test-app" / "data" / "notes.json"
        data_file.write_text('{"kept": true}', encoding="utf-8")
        deregister = AsyncMock(return_value=2)
        audit = MagicMock()

        with (
            patch("kiro_crew.apps.routes.deregister_app_crons_reporting_failures", deregister),
            patch("kiro_crew.apps.routes.sel", return_value=audit),
        ):
            from kiro_crew.apps.routes import uninstall_app_in_process

            result = await uninstall_app_in_process(
                _web_app(MagicMock()), "test-app", caller="edition"
            )

        assert result["ok"] is True
        assert result["status"] == 200
        assert not (app_home / "apps" / "test-app" / APP_MANIFEST_FILENAME).exists()
        assert data_file.read_text(encoding="utf-8") == '{"kept": true}'
        deregister.assert_awaited_once()
        callers = {c.kwargs.get("caller") for c in audit.log_api_access.call_args_list}
        assert callers == {"edition"}

    async def test_an_app_that_is_not_installed_is_a_404(self, app_home):
        from kiro_crew.apps.routes import uninstall_app_in_process

        result = await uninstall_app_in_process(_web_app(None), "absent", caller="edition")

        assert result == {
            "ok": False,
            "status": 404,
            "error": "app 'absent' not installed",
        }

    async def test_a_locked_app_is_refused_and_left_in_place(self, tmp_path, app_home):
        assert install_app(_make_app_source(tmp_path)).ok
        installed = {"name": "test-app", "lifecycle": "locked", "manifest": {}}

        with patch("kiro_crew.apps.routes.get_app", return_value=installed):
            from kiro_crew.apps.routes import uninstall_app_in_process

            result = await uninstall_app_in_process(_web_app(None), "test-app", caller="edition")

        assert result["ok"] is False
        assert result["status"] == 400
        assert (app_home / "apps" / "test-app" / APP_MANIFEST_FILENAME).is_file()
