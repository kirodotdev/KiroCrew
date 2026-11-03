"""An unreadable Design Critique route module is skipped at boot, not fatal.

The package keeps its route module out of its ``__init__`` and serves
``register_routes`` from a module ``__getattr__``. That lookup is what the startup
route loop in ``dashboard/routes/system.py`` guards against ``OSError``, so a route
file an endpoint security agent denies is logged and skipped there, and the other
built-ins still register.
"""

from __future__ import annotations

import importlib
import logging
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew.dashboard.routes import system

ROUTES = "kiro_crew.apps.builtins.design_critique.backend.routes"


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner", broadcast_ws=lambda *a, **k: None)
    return app


def test_the_route_module_is_read_by_the_lookup_not_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = importlib.import_module("kiro_crew.apps.builtins.design_critique")
    real_import = importlib.import_module

    def _import(name: str, *args, **kwargs):
        if name == ROUTES:
            raise PermissionError(13, "Permission denied", "backend/routes.py")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _import)
    with pytest.raises(PermissionError):
        getattr(package, "register_routes")


def test_an_unreadable_route_module_does_not_abort_route_setup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_import = importlib.import_module

    def _import(name: str, *args, **kwargs):
        if name == ROUTES:
            raise PermissionError(13, "Permission denied", "backend/routes.py")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _import)
    app = _app()
    with caplog.at_level(logging.WARNING, logger=system.__name__):
        system.register(app)  # must not raise

    warned = [r.getMessage() for r in caplog.records if "design_critique" in r.getMessage()]
    assert warned and "PermissionError" in warned[0], warned
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert not any(p.startswith("/api/apps/design-critique/") for p in paths)
