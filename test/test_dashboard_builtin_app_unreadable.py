"""One built-in app whose files cannot be read does not abort gateway boot.

An endpoint security agent denying read access to one file of an optional
built-in app raised ``PermissionError`` out of the route-registration loop, which
tolerated only ``ModuleNotFoundError``, so the whole gateway failed to start.
"""

from __future__ import annotations

import importlib
import logging
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.dashboard.routes import system


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner", broadcast_ws=lambda *a, **k: None)
    return app


def _route_count(app: web.Application) -> int:
    return len(list(app.router.routes()))


def test_an_unreadable_builtin_is_skipped_and_the_rest_still_register(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert len(BUILTIN_NAMES) >= 2, "the test needs a builtin to fail and one to load"
    failing = BUILTIN_NAMES[0]
    real_import = importlib.import_module

    def _import(name: str, *args, **kwargs):
        if name == f"kiro_crew.apps.builtins.{failing}":
            raise PermissionError(13, "Permission denied", f"{failing}/service.py")
        return real_import(name, *args, **kwargs)

    baseline = _app()
    system.register(baseline)

    monkeypatch.setattr(system.importlib, "import_module", _import)
    app = _app()
    with caplog.at_level(logging.WARNING, logger=system.__name__):
        system.register(app)  # must not raise

    warned = [r.getMessage() for r in caplog.records if failing in r.getMessage()]
    assert warned and "PermissionError" in warned[0], warned
    # The other builtins still registered their routes: only the failing one is missing.
    assert 0 < _route_count(app) <= _route_count(baseline)


def test_a_builtin_with_a_missing_dependency_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = BUILTIN_NAMES[0]
    real_import = importlib.import_module

    def _import(name: str, *args, **kwargs):
        if name == f"kiro_crew.apps.builtins.{failing}":
            raise ModuleNotFoundError("No module named 'missing_dep'", name="missing_dep")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(system.importlib, "import_module", _import)
    with pytest.raises(ModuleNotFoundError):
        system.register(_app())


def test_an_oserror_while_registering_routes_still_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the import is tolerated: a failure partway through ``register_routes``
    would leave the app half-registered, so it must not be swallowed."""
    failing = BUILTIN_NAMES[0]
    real_import = importlib.import_module

    def _broken_register(app: web.Application) -> None:
        raise PermissionError(13, "Permission denied", "state.json")

    def _import(name: str, *args, **kwargs):
        if name == f"kiro_crew.apps.builtins.{failing}":
            return SimpleNamespace(register_routes=_broken_register)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(system.importlib, "import_module", _import)
    with pytest.raises(PermissionError):
        system.register(_app())
