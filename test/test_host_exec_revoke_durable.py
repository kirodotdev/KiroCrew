"""A host-exec revoke that cannot remove the grant says so, and an uninstall refuses.

The grant (``host-exec-grants/<app>``) authorizes a pod outside the sandbox. An uninstall
that dropped the app but left the marker behind would authorize a pod for a different app
later installed under the same name. ``revoke_host_exec`` therefore reports whether the
marker is gone, and the uninstall keeps the app installed (its approvals put back) when it
is not, like every other step of the trust-grant cleanup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app_manager_helpers import _make_app_source, app_home_fixture  # noqa: F401 - fixture

from kiro_crew.apps import host_exec_grants as heg
from kiro_crew.apps.manager import get_app, install_app, uninstall_app

_APP = "test-app"


class _StuckMarker:
    """A grant marker that is there and cannot be removed (a read-only or busy file)."""

    def __init__(self, real: Path):
        self._real = real

    def unlink(self) -> None:
        raise PermissionError(13, "Permission denied", str(self._real))

    def is_file(self) -> bool:
        return self._real.is_file()


def test_revoke_reports_a_marker_it_could_not_remove(app_home, monkeypatch):
    assert heg.grant_host_exec(_APP) is True
    real = heg._grant_path(_APP)
    monkeypatch.setattr(heg, "_grant_path", lambda _name: _StuckMarker(real))
    assert heg.revoke_host_exec(_APP) is False
    assert heg.host_exec_granted(_APP) is True


def test_revoke_reports_success_and_a_missing_marker(app_home):
    assert heg.revoke_host_exec(_APP) is True, "no grant: nothing to revoke"
    assert heg.grant_host_exec(_APP) is True
    assert heg.revoke_host_exec(_APP) is True
    assert heg.host_exec_granted(_APP) is False


def test_an_uninstall_whose_revoke_fails_keeps_the_app(app_home, tmp_path, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    assert heg.grant_host_exec(_APP) is True
    real = heg._grant_path(_APP)
    monkeypatch.setattr(heg, "_grant_path", lambda _name: _StuckMarker(real))
    result = uninstall_app(_APP)
    assert result.ok is False, "a grant left behind would authorize the next app of this name"
    assert get_app(_APP) is not None, "the app stays installed"
    assert real.is_file()


@pytest.mark.parametrize("granted", [True, False])
def test_an_uninstall_with_a_working_revoke_still_succeeds(app_home, tmp_path, granted):
    assert install_app(_make_app_source(tmp_path)).ok
    if granted:
        assert heg.grant_host_exec(_APP) is True
    result = uninstall_app(_APP)
    assert result.ok is True, result.error
    assert heg.host_exec_granted(_APP) is False
