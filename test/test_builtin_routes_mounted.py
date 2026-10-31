"""A builtin whose manifest declares ``backend.routes`` has those routes mounted.

The gateway mounts a builtin's in-process routes by importing
``kiro_crew.apps.builtins.<name>`` for each entry of ``BUILTIN_NAMES`` and calling
the package's ``register_routes(app)`` (``dashboard/routes/system.py``). A builtin
left out of the list, or whose package does not export the function, loads with
no routes, and every call to them answers 404.
"""

import importlib
import json
from pathlib import Path

import pytest
from aiohttp import web

import kiro_crew.apps.builtins as builtins_pkg
from kiro_crew.apps.builtins import BUILTIN_NAMES

ROOT = Path(builtins_pkg.__file__).parent
WITH_ROUTES = sorted(
    p.parent.name
    for p in ROOT.glob("*/app.json")
    if (json.loads(p.read_text(encoding="utf-8")).get("backend") or {}).get("routes")
)


def test_routes_declaring_builtins_exist():
    assert "code_intel" in WITH_ROUTES


@pytest.mark.parametrize("name", WITH_ROUTES)
def test_builtin_with_routes_is_mounted_at_startup(name):
    assert name in BUILTIN_NAMES
    module = importlib.import_module(f"kiro_crew.apps.builtins.{name}")
    assert callable(getattr(module, "register_routes", None))


def test_code_intel_registers_its_routes():
    app = web.Application()
    importlib.import_module("kiro_crew.apps.builtins.code_intel").register_routes(app)
    paths = {route.resource.canonical for route in app.router.routes()}
    assert {"/api/apps/code-intel/binding", "/api/apps/code-intel/pool"} <= paths
