"""Dev Fleet's wheel copy declares the host paths its page already calls.

The page POSTs four routes the gateway itself serves under the app's namespace
(Make live, Restart gateway, Open with a pod token, Create a release worktree)
and reads the app list (``GET /api/apps``) for its backend-down banner. The
compiled built-in page called them as dashboard code; an adopted bundle calls
through the scoped client, which refuses an undeclared path. So the wheel copy
declares the four routes as bare entries (none has a child) and the list as
``=/api/apps``, the exact entry: a bare ``/api/apps`` would also cover install,
uninstall, enable and every other app's routes. The app repository declares the
same list, so its adoption is no widening (E9).

Owning spec: docs/system-specs/modules/app-kit-platform.md, "Scoped frontend HTTP
transport".
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kiro_crew.apps import builtin_adoption_checks as checks
from kiro_crew.apps import manager
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard import token_auth_app_scope as scope

NAME = "dev-fleet"
ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "src" / "kiro_crew" / "apps" / "builtins" / "dev_fleet" / "app.json"
OWN = ["/apps/dev-fleet/api", "/apps/dev-fleet/api/*"]
GATEWAY_ROUTES = [
    "/api/apps/dev-fleet/make-live",
    "/api/apps/dev-fleet/restart-gateway",
    "/api/apps/dev-fleet/pod-token",
    "/api/apps/dev-fleet/create-release-worktree",
]
HOST = GATEWAY_ROUTES + ["=/api/apps"]
#: Every host path the page calls (``website/src/apps/dev-fleet``).
PAGE_CALLS = GATEWAY_ROUTES + ["/api/apps"]
#: Neighbours of those paths no declaration names.
UNDECLARED = [
    "/api/apps/install",
    "/api/apps/registry/install",
    "/api/apps/other-app/enable",
    "/api/apps/other-app/config",
    "/api/appsx",
    "/api/apps/",
    "/api/sessions",
]


def _shipped() -> dict:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))


@pytest.fixture
def registered(tmp_path, monkeypatch):
    """The real wheel copy registered as a built-in in an isolated data home."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    shipped = _shipped()
    monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda *a, **k: [shipped])
    monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])
    manager.register_builtin_apps()
    return shipped


def _repository_manifest(api: list[str]) -> AppManifest:
    """The app repository's manifest: the wheel copy's, with *api* as its grant."""
    data = copy.deepcopy(_shipped())
    data["permissions"]["api"] = api
    return AppManifest.from_dict(data)


def _matches(declared: list[str], path: str) -> bool:
    return any(scope._api_pattern_matches(p, path) for p in declared)


def test_the_wheel_copy_declares_exactly_its_own_paths_and_the_host_paths():
    assert _shipped()["permissions"]["api"] == OWN + HOST


def test_every_gateway_route_the_page_posts_is_a_registered_route():
    """Each declared route is one the gateway registers, so none names a typo."""
    source = (ROOT / "src/kiro_crew/apps/builtins/dev_fleet/gateway_routes.py").read_text(
        encoding="utf-8"
    )
    for route in GATEWAY_ROUTES:
        tail = route.removeprefix("/api/apps/dev-fleet")
        assert f'add_post(f"{{API_PREFIX}}{tail}"' in source, route


def test_every_host_path_the_page_calls_is_declared_and_no_neighbour_is():
    declared = _shipped()["permissions"]["api"]
    for path in PAGE_CALLS:
        assert _matches(declared, path), path
    for path in UNDECLARED:
        assert not _matches(declared, path), path


def test_a_registered_builtin_approves_what_the_wheel_ships(registered):
    grants = manager._read_approved_grants(NAME)
    assert grants is not None and grants["api"] == OWN + HOST


def test_the_repository_copy_adopts_without_widening(registered):
    assert checks.permission_refusal(NAME, _repository_manifest(OWN + HOST)) is None


def test_a_bare_app_list_entry_is_still_a_widening(registered):
    widened = _repository_manifest(OWN + GATEWAY_ROUTES + ["/api/apps"])
    assert checks.permission_refusal(NAME, widened) == "permission_widening"


def test_the_app_token_reaches_the_list_alone(registered, monkeypatch):
    from kiro_crew.apps import host_exec_grants

    monkeypatch.setattr(host_exec_grants, "host_exec_granted", lambda name: name == NAME)
    monkeypatch.setattr(scope, "_app_perms_cache", {})
    monkeypatch.setattr(scope, "_app_perms_denied", set())
    assert scope.app_token_path_allowed(NAME, "/api/apps")
    for path in UNDECLARED:
        assert not scope.app_token_path_allowed(NAME, path), path
