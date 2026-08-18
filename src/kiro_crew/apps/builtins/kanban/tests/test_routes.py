"""Registration contract tests for the Kanban builtin app.

These guard the wiring that ``dashboard/routes/system.py`` depends on at
gateway startup: it imports ``kiro_crew.apps.builtins.<name>`` for each name in
``BUILTIN_NAMES`` and calls ``register_routes`` on the PACKAGE.  If the
re-export in ``__init__.py`` is dropped, or the name falls out of
``BUILTIN_NAMES``, the app silently serves no API — the failure mode this file
exists to catch.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

import kiro_crew.apps.builtins.kanban as kanban_pkg
from kiro_crew.apps.builtins import BUILTIN_NAMES

APP_NAME = "kanban"

#: The API surface the frontend calls.  Paths are aiohttp canonical form.
#: This set is exhaustive on purpose -- the test asserts nothing extra is
#: registered either, so an endpoint no consumer calls cannot quietly reappear.
EXPECTED_ROUTES = {
    ("GET", f"/api/apps/{APP_NAME}/tasks"),
    ("POST", f"/api/apps/{APP_NAME}/tasks"),
    ("PATCH", f"/api/apps/{APP_NAME}/tasks/{{id}}"),
    ("DELETE", f"/api/apps/{APP_NAME}/tasks/{{id}}"),
    ("POST", f"/api/apps/{APP_NAME}/tasks/{{id}}/move"),
    ("POST", f"/api/apps/{APP_NAME}/tasks/{{id}}/run"),
    ("POST", f"/api/apps/{APP_NAME}/reconcile"),
}


def test_name_is_registered_as_builtin() -> None:
    """The startup loop only visits names listed in BUILTIN_NAMES."""
    assert APP_NAME in BUILTIN_NAMES


def test_package_reexports_register_routes() -> None:
    """system.py checks hasattr on the package, not on backend.routes."""
    assert callable(getattr(kanban_pkg, "register_routes", None))


def test_register_routes_installs_the_expected_surface() -> None:
    """Every endpoint the frontend calls is registered, and nothing extra."""
    app = web.Application()
    kanban_pkg.register_routes(app)

    registered = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
        # aiohttp's add_get() installs an implicit HEAD companion for each GET;
        # it is not part of the app's declared surface.
        and route.method != "HEAD"
    }
    assert registered == EXPECTED_ROUTES


def test_manifest_matches_the_served_prefix() -> None:
    """app.json's name drives /api/apps/<name>; a mismatch 404s the whole app."""
    manifest_path = Path(kanban_pkg.__file__).parent / "app.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["name"] == APP_NAME
    assert manifest["backend"]["routes"] == "backend.routes:register_routes"
    for declared in manifest["permissions"]["api"]:
        assert declared.startswith(f"/api/apps/{APP_NAME}")
    # The UI route is the key the frontend's builtinRegistry.ts maps to a page.
    assert manifest["ui"]["pages"][0]["route"] == f"/{APP_NAME}"
