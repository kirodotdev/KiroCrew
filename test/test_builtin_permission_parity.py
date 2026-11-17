"""Permission parity of the built-in wheel copies, checked once per app.

A built-in approves what its wheel copy ships at every start, and the app
repository's copy declares the same list, so its adoption is no widening (E9).
These checks hold for every app; each app's own file keeps its data (``NAME``,
``OWN``, ``HOST``, ``PAGE_CALLS``), the reason its host paths are what they are,
and the checks only that app needs. A new app with a wheel copy adds its module to
``APPS``.

Owning spec: docs/system-specs/modules/app-kit-platform.md, "Permission parity of
the wheel copies".
"""

from __future__ import annotations

import pytest
import test_agent_worlds_permission_parity as agent_worlds
import test_code_review_sage_permission_parity as code_review_sage
import test_design_tweak_permission_parity as design_tweak
import test_dev_fleet_permission_parity as dev_fleet

from kiro_crew.apps import builtin_adoption_checks as checks
from kiro_crew.apps import manager
from kiro_crew.dashboard import token_auth_app_scope as scope

APPS = [agent_worlds, code_review_sage, design_tweak, dev_fleet]


@pytest.fixture(params=APPS, ids=lambda app: app.NAME)
def app(request):
    return request.param


@pytest.fixture
def registered(app, tmp_path, monkeypatch):
    """The app's real wheel copy registered as a built-in in an isolated data home."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    shipped = app._shipped()
    monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda *a, **k: [shipped])
    monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])
    manager.register_builtin_apps()
    return shipped


def test_the_wheel_copy_declares_exactly_its_own_paths_and_its_host_paths(app):
    assert app._shipped()["permissions"]["api"] == app.OWN + app.HOST


def test_every_host_path_the_page_calls_is_declared(app):
    declared = app._shipped()["permissions"]["api"]
    for path in app.PAGE_CALLS:
        assert any(scope._api_pattern_matches(p, path) for p in declared), path


def test_a_registered_builtin_approves_what_the_wheel_ships(app, registered):
    grants = manager._read_approved_grants(app.NAME)
    assert grants is not None and grants["api"] == app.OWN + app.HOST


def test_the_repository_copy_adopts_without_widening(app, registered):
    manifest = app._repository_manifest(app.OWN + app.HOST)
    assert checks.permission_refusal(app.NAME, manifest) is None
