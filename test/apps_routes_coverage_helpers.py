"""Helpers shared by the ``kiro_crew.apps.routes`` coverage modules.

``APP`` (the synthetic app's name), ``_make_app_source`` and ``_install`` (write its
manifest and install it), ``_setup_env`` (an isolated ``KIROCREW_HOME`` with the
out-of-process side effects neutralized) and ``_make_app`` (an aiohttp app with the routes
registered and, optionally, an authenticated app or dashboard identity).
``test_apps_routes_coverage.py`` and the route modules its docstring names import them by
name; each is read by two or more of those modules.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.routes import invalidate_app_secret_cache, register_app_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

APP = "cov-test-app"


def _make_app_source(tmp_path: Path, name: str = APP, **manifest_extra: Any) -> Path:
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Coverage Test App",
        "description": "App for routes coverage testing",
        "author": "tester",
    }
    manifest.update(manifest_extra)
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate KIROCREW_HOME and neutralize out-of-process side effects."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # These synthetic apps are third-party; admit them explicitly so the
    # execution guard is not the thing under test in every case.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod

    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod

    bmod._processes.clear()
    bmod._allocated_ports.clear()
    monkeypatch.setattr(routes_mod, "sel", lambda: MagicMock())
    invalidate_app_secret_cache(APP)
    return home


def _make_app(
    *, app_identity: str | None = None, dashboard_user: str | None = None
) -> web.Application:
    """An aiohttp app with the routes registered.

    ``app_identity`` stands in for ``token_auth_middleware`` having
    authenticated an APP token, which is what the proxy's cross-app guard
    reads off ``request["app"]``. ``dashboard_user`` stands in for the same
    middleware having authenticated a DASHBOARD subject (``request["user"]``
    with an empty ``request["app"]``); the owner gate then compares it to
    ``state.owner_id``, which the test state pins to ``"owner"``.
    """
    middlewares = []
    if app_identity is not None or dashboard_user is not None:

        @web.middleware
        async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
            request["app"] = app_identity if app_identity is not None else ""
            if dashboard_user is not None:
                request["user"] = dashboard_user
            return await handler(request)

        middlewares.append(_identity)
    app = web.Application(middlewares=middlewares)
    if dashboard_user is not None:
        app["state"] = SimpleNamespace(owner_id="owner", broadcast_ws=lambda *a, **k: None)
    register_app_routes(app)
    return app


def _install(tmp_path: Path, **manifest_extra: Any) -> None:
    install_app(_make_app_source(tmp_path, **manifest_extra))
