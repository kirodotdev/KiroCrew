"""A disable or an uninstall whose grant revoke is not durable does not report success.

``revoke_host_exec`` ends the ``dashboard_proxy`` grant in memory first, then answers
``False`` when a gateway restart could bring a grant back (the host-exec marker stayed,
or the dashboard grant record stayed with no tombstone to outrank it). The lifecycle
callers say ``revoke_incomplete`` instead of success.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from trusted_apps_api_helpers import home_fixture  # noqa: F401
from trusted_apps_api_helpers import _APP, _install


def _revoke_not_durable():
    return patch("kiro_crew.apps.dashboard_proxy.revoke", lambda name: False)


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "already-disabled"])
def test_a_disable_whose_revoke_is_not_durable_reports_it(
    home: Path, tmp_path: Path, enabled: bool
) -> None:
    from kiro_crew.apps import manager as appmanager

    _install(tmp_path, _APP, enabled=enabled)
    with _revoke_not_durable():
        result = appmanager.disable_app(_APP)
    assert result.ok is False
    assert result.error_code == "revoke_incomplete"
    assert "restart" in result.error


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "already-disabled"])
def test_a_disable_whose_revoke_holds_still_succeeds(
    home: Path, tmp_path: Path, enabled: bool
) -> None:
    from kiro_crew.apps import manager as appmanager

    _install(tmp_path, _APP, enabled=enabled)
    assert appmanager.disable_app(_APP).ok is True


def test_an_uninstall_whose_revoke_is_not_durable_is_refused(home: Path, tmp_path: Path) -> None:
    from kiro_crew.apps import manager as appmanager

    _install(tmp_path, _APP, enabled=False)
    with _revoke_not_durable():
        result = appmanager.uninstall_app(_APP)
    assert result.ok is False
    assert result.error_code == "revoke_incomplete"
    # Nothing was destroyed, so the owner can fix the cause and retry.
    assert appmanager.get_app(_APP) is not None
