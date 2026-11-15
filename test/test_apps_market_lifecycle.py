"""Full life of an app installed from the marketplace: install, uninstall, reinstall.

``test_apps_marketplace_install.py`` pins the install side. These cases pin what
happens after: uninstall must drop every trace of the per-listing trust grant, a
reinstall of the next version must not inherit it, and a failed uninstall must
put the whole grant (including the ``market:<ref>`` repository binding) back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from test_apps_marketplace_install import (
    APP_NAME,
    REF,
    FakeMarketClient,
    _app_files,
    _zip,
)


def _install(version: int | None = None) -> None:
    from kiro_crew.apps.marketplace_install import complete_market_install, prepare_market_install

    prepared = prepare_market_install(FakeMarketClient(payload=_zip(_app_files())), REF, version)
    result = complete_market_install(prepared, confirmed=True)
    assert result.ok, result.error


def _grant_market_trust(home: Path) -> None:
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_allow_third_party": False,
                    "apps_trusted": [APP_NAME],
                    "apps_trusted_local": [],
                    "apps_trusted_repositories": {APP_NAME: f"market:{REF}"},
                }
            }
        ),
        encoding="utf-8",
    )


def _agent(home: Path) -> dict[str, Any]:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))["agent"]


def _assert_no_grant(home: Path) -> None:
    agent = _agent(home)
    assert APP_NAME not in agent.get("apps_trusted", [])
    assert APP_NAME not in agent.get("apps_trusted_repositories", {})
    assert APP_NAME not in agent.get("apps_trusted_local", [])


def test_uninstall_removes_record_folders_and_every_trust_entry(app_home):
    from kiro_crew.apps.manager import _read_installed, get_app, uninstall_app
    from kiro_crew.apps.manager_records import app_dir
    from kiro_crew.apps.marketplace_install import market_staging_root

    _install()
    _grant_market_trust(app_home)

    result = uninstall_app(APP_NAME, keep_data=False)

    assert result.ok, result.error
    assert get_app(APP_NAME) is None
    assert _read_installed(APP_NAME) is None
    assert not app_dir(APP_NAME).exists()
    staging = market_staging_root()
    assert not staging.exists() or not any(staging.iterdir())
    _assert_no_grant(app_home)


def test_reinstalling_the_next_version_needs_a_fresh_trust_grant(app_home):
    from kiro_crew.apps.manager import _read_installed, enable_app, uninstall_app

    _install(version=3)
    _grant_market_trust(app_home)
    assert uninstall_app(APP_NAME, keep_data=False).ok

    _install(version=4)

    meta = _read_installed(APP_NAME)
    assert meta is not None
    assert meta.source == f"market:{REF}@4"
    _assert_no_grant(app_home)
    denied = enable_app(APP_NAME)
    assert not denied.ok
    assert denied.error_code == "app_execution_denied"


def test_keep_data_uninstall_keeps_data_and_still_drops_the_grant(app_home):
    from kiro_crew.apps.manager import get_app, uninstall_app
    from kiro_crew.apps.manager_records import app_dir

    _install()
    _grant_market_trust(app_home)
    data = app_dir(APP_NAME) / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "notes.txt").write_text("keep me", encoding="utf-8")

    result = uninstall_app(APP_NAME, keep_data=True)

    assert result.ok, result.error
    assert get_app(APP_NAME) is None
    assert (data / "notes.txt").read_text(encoding="utf-8") == "keep me"
    _assert_no_grant(app_home)


def test_failed_uninstall_restores_the_market_repository_binding(app_home, monkeypatch):
    from kiro_crew.apps import manager_uninstall
    from kiro_crew.apps.manager import get_app, uninstall_app

    _install()
    _grant_market_trust(app_home)

    def _boom(_dest: Path) -> None:
        raise OSError("simulated delete failure")

    monkeypatch.setattr(manager_uninstall, "_rmtree_app_dir", _boom)

    result = uninstall_app(APP_NAME, keep_data=False)

    assert not result.ok
    assert get_app(APP_NAME) is not None
    agent = _agent(app_home)
    assert agent["apps_trusted"] == [APP_NAME]
    assert agent["apps_trusted_repositories"] == {APP_NAME: f"market:{REF}"}
