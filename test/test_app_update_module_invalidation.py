"""In-place app update: the next load must not reuse the previous version's modules.

An app's route hook is loaded under the synthetic ``_kirocrew_app_<name>.*``
namespace, and ``unload_app_modules`` has always evicted that namespace on
disable / update / reinstall. But a hook may also register its own package under
its REAL import name, straight from files inside the app's install directory (a
packaged app whose siblings use ordinary absolute imports does exactly that,
without touching ``sys.path``). If those real-name entries survive the unload,
an in-place N -> N+1 update in the same gateway process imports the fresh N+1
hook's siblings from the cached N modules: a function added in N+1 is missing,
route registration raises ``AttributeError``, and every route of the app
answers 404 until the gateway restarts.

These tests drive that exact sequence in one process through the real
``RouteRegistry`` and ``load_app_module``, and pin the boundary of the fix: only
modules loaded from the app's own directory are evicted, never another app's,
never Core's, and the execution gate still runs on the reload.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from skill_script_helpers import no_bytecode

from kiro_crew.apps import module_loader
from kiro_crew.apps.module_loader import is_app_module_loaded, unload_app_modules
from kiro_crew.apps.route_registry import RouteRegistry


@pytest.fixture(autouse=True)
def _admit_third_party_execution(_floor_monkeypatch) -> None:
    # The floor monkeypatch, not the shared one: a test that calls
    # ``monkeypatch.undo()`` must not lift this admission (D11).
    _floor_monkeypatch.setattr(
        "kiro_crew.apps.execution.third_party_execution_allowed", lambda: True
    )


@pytest.fixture(autouse=True)
def _no_bytecode():
    # A rewritten file of equal size within one mtime tick could otherwise be
    # served from a stale .pyc; the gateway case is a real reinstall, not that.
    with no_bytecode():
        yield


# ---------------------------------------------------------------------------
# Fixture app: a packaged app whose hook registers its real package name
# ---------------------------------------------------------------------------

_HANDLERS = """
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from aiohttp import web

from kiro_crew.apps.route_registry import AppRoute

_PACKAGE_NAME = {pkg!r}


def _ensure_package_importable():
    if _PACKAGE_NAME in sys.modules:
        return
    pkg_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME, pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = module
    spec.loader.exec_module(module)


def _bind(fn):
    async def handler(request, ctx):
        return web.json_response({{"value": fn()}})
    return handler


def register_routes(ctx):
    _ensure_package_importable()
    helpers = importlib.import_module(_PACKAGE_NAME + ".helpers")
    routes = [AppRoute("GET", "/ping", _bind(helpers.ping))]
{extra_routes}
    return routes
"""

_V1_HELPERS = "def ping():\n    return 'v1'\n"
_V2_HELPERS = "def ping():\n    return 'v2'\n\n\n" "def import_file():\n    return 'v2-import'\n"
_V2_ROUTES = '    routes.append(AppRoute("POST", "/import", _bind(helpers.import_file)))'

_HOOK = "src.pkg.handlers:register_routes"

# Stands in for ``import pkg.native`` of a compiled extension: created once, the
# way CPython initialises an extension once per process, then imported by name.
_NATIVE_ROUTE = """    native_key = _PACKAGE_NAME + ".native"
    if native_key not in sys.modules:
        import types
        native = types.ModuleType(native_key)
        native.__file__ = str(Path(__file__).resolve().parent / "native.so")
        native.answer = lambda: "native"
        sys.modules[native_key] = native
        setattr(sys.modules[_PACKAGE_NAME], "native", native)
    importlib.import_module(native_key)
    answer = sys.modules[_PACKAGE_NAME].native.answer
    routes.append(AppRoute("GET", "/native", _bind(answer)))"""


def _write_version(app_dir: Path, pkg: str, helpers: str, extra_routes: str = "") -> None:
    """Write one version of the app the way an in-place update rewrites files."""
    # The synthetic hook path is ``src.pkg.handlers``; the REAL package the hook
    # registers is named ``pkg`` (unique per test) and lives in the same directory.
    pkg_dir = app_dir / "src" / "pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "helpers.py").write_text(helpers, encoding="utf-8")
    (pkg_dir / "handlers.py").write_text(
        _HANDLERS.format(pkg=pkg, extra_routes=extra_routes), encoding="utf-8"
    )


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.health.mark_degraded = MagicMock()
    return ctx


def _registry() -> RouteRegistry:
    app = MagicMock()
    app.router.add_route = MagicMock()
    return RouteRegistry(app)


async def _call(registry: RouteRegistry, app_name: str, method: str, path: str):
    request = make_mocked_request(
        method,
        f"/api/apps/{app_name}{path}",
        match_info={"app_name": app_name, "path": path.lstrip("/")},
    )
    return await registry.dispatch(request)


def _value(response) -> str:
    import json

    return json.loads(response.body)["value"]


def _real_keys(pkg: str) -> list[str]:
    return [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]


@pytest.fixture
def pkg_name():
    name = f"kc_update_fixture_{uuid.uuid4().hex[:10]}"
    yield name
    # Only keys under this test's own unique name, which did not exist before
    # the test; there is no earlier entry to restore.
    for key in _real_keys(name):
        sys.modules.pop(key, None)  # flake-ok: evicts this test's unique-named fixture package


@pytest.fixture
def app_name():
    name = f"upd-{uuid.uuid4().hex[:8]}"
    yield name
    unload_app_modules(name)


# ---------------------------------------------------------------------------
# N -> N+1 in one process
# ---------------------------------------------------------------------------


class TestInPlaceUpdate:
    @pytest.mark.asyncio
    async def test_update_adding_a_function_registers_and_serves_it(
        self, tmp_path: Path, app_name: str, pkg_name: str
    ) -> None:
        app_dir = tmp_path / app_name
        registry = _registry()

        _write_version(app_dir, pkg_name, _V1_HELPERS)
        v1_ctx = _ctx()
        assert await registry.register_app_routes(app_name, app_dir, _HOOK, v1_ctx)
        assert _value(await _call(registry, app_name, "GET", "/ping")) == "v1"
        assert _real_keys(pkg_name), "fixture must register its real package name"

        # In-place update: the reconciler / dashboard tear the app down, the new
        # files land in the same directory, and the hook is loaded again.
        registry.deregister_app_routes(app_name)
        _write_version(app_dir, pkg_name, _V2_HELPERS, _V2_ROUTES)
        v2_ctx = _ctx()
        described = await registry.register_app_routes(app_name, app_dir, _HOOK, v2_ctx)

        v2_ctx.health.mark_degraded.assert_not_called()
        assert f"POST /api/apps/{app_name}/import" in described
        assert _value(await _call(registry, app_name, "POST", "/import")) == "v2-import"
        assert _value(await _call(registry, app_name, "GET", "/ping")) == "v2"

    def test_unload_evicts_real_name_modules_loaded_from_the_app_dir(
        self, tmp_path: Path, app_name: str, pkg_name: str
    ) -> None:
        app_dir = tmp_path / app_name
        _write_version(app_dir, pkg_name, _V1_HELPERS)
        register = module_loader.load_app_module(app_name, app_dir, _HOOK)
        register(_ctx())
        assert set(_real_keys(pkg_name)) == {pkg_name, f"{pkg_name}.helpers"}

        removed = unload_app_modules(app_name)

        assert _real_keys(pkg_name) == []
        assert not is_app_module_loaded(app_name)
        # 4 synthetic keys (root, src, src.pkg, src.pkg.handlers) + 2 real ones.
        assert removed == 6

    @pytest.mark.asyncio
    async def test_reinstall_same_version_is_repeatable(
        self, tmp_path: Path, app_name: str, pkg_name: str
    ) -> None:
        app_dir = tmp_path / app_name
        registry = _registry()
        for _ in range(3):
            _write_version(app_dir, pkg_name, _V2_HELPERS, _V2_ROUTES)
            ctx = _ctx()
            described = await registry.register_app_routes(app_name, app_dir, _HOOK, ctx)
            ctx.health.mark_degraded.assert_not_called()
            assert len(described) == 2
            assert _value(await _call(registry, app_name, "POST", "/import")) == "v2-import"
            registry.deregister_app_routes(app_name)
            assert _real_keys(pkg_name) == []
        # Nothing left to evict: a repeated unload is a no-op.
        assert unload_app_modules(app_name) == 0

    @pytest.mark.asyncio
    async def test_failed_update_then_fixed_update_recovers(
        self, tmp_path: Path, app_name: str, pkg_name: str
    ) -> None:
        app_dir = tmp_path / app_name
        registry = _registry()
        _write_version(app_dir, pkg_name, _V1_HELPERS)
        assert await registry.register_app_routes(app_name, app_dir, _HOOK, _ctx())

        registry.deregister_app_routes(app_name)
        _write_version(app_dir, pkg_name, "def ping(:\n", _V2_ROUTES)
        broken_ctx = _ctx()
        assert await registry.register_app_routes(app_name, app_dir, _HOOK, broken_ctx) == []
        broken_ctx.health.mark_degraded.assert_called_once()
        assert (await _call(registry, app_name, "GET", "/ping")).status == 404

        registry.deregister_app_routes(app_name)
        _write_version(app_dir, pkg_name, _V2_HELPERS, _V2_ROUTES)
        fixed_ctx = _ctx()
        assert await registry.register_app_routes(app_name, app_dir, _HOOK, fixed_ctx)
        fixed_ctx.health.mark_degraded.assert_not_called()
        assert _value(await _call(registry, app_name, "POST", "/import")) == "v2-import"

    @pytest.mark.asyncio
    async def test_execution_gate_still_refuses_the_reload(
        self, tmp_path: Path, app_name: str, pkg_name: str, monkeypatch
    ) -> None:
        app_dir = tmp_path / app_name
        registry = _registry()
        _write_version(app_dir, pkg_name, _V1_HELPERS)
        assert await registry.register_app_routes(app_name, app_dir, _HOOK, _ctx())
        registry.deregister_app_routes(app_name)

        monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: False)
        _write_version(app_dir, pkg_name, _V2_HELPERS, _V2_ROUTES)
        denied_ctx = _ctx()
        assert await registry.register_app_routes(app_name, app_dir, _HOOK, denied_ctx) == []
        denied_ctx.health.mark_degraded.assert_called_once()
        # The denied load executed nothing of the app, so nothing was re-imported.
        assert _real_keys(pkg_name) == []
        assert not is_app_module_loaded(app_name)


# ---------------------------------------------------------------------------
# Ownership boundary: only the updated app's own modules go
# ---------------------------------------------------------------------------


class TestOwnershipBoundary:
    @pytest.mark.asyncio
    async def test_sibling_app_modules_and_routes_untouched(
        self, tmp_path: Path, app_name: str, pkg_name: str
    ) -> None:
        sibling = f"sib-{uuid.uuid4().hex[:8]}"
        sibling_pkg = f"{pkg_name}_sibling"
        registry = _registry()
        _write_version(tmp_path / app_name, pkg_name, _V1_HELPERS)
        _write_version(tmp_path / sibling, sibling_pkg, _V2_HELPERS, _V2_ROUTES)
        try:
            assert await registry.register_app_routes(app_name, tmp_path / app_name, _HOOK, _ctx())
            assert await registry.register_app_routes(sibling, tmp_path / sibling, _HOOK, _ctx())
            sibling_before = {k: sys.modules[k] for k in _real_keys(sibling_pkg)}
            assert sibling_before

            registry.deregister_app_routes(app_name)

            assert _real_keys(pkg_name) == []
            assert {k: sys.modules.get(k) for k in sibling_before} == sibling_before
            assert is_app_module_loaded(sibling)
            assert _value(await _call(registry, sibling, "POST", "/import")) == "v2-import"
        finally:
            unload_app_modules(sibling)
            for key in _real_keys(sibling_pkg):
                sys.modules.pop(key, None)  # flake-ok: this test's unique-named sibling package

    def test_modules_outside_the_app_dir_are_untouched(
        self, tmp_path: Path, app_name: str, pkg_name: str, monkeypatch
    ) -> None:
        import types

        app_dir = tmp_path / app_name
        _write_version(app_dir, pkg_name, _V1_HELPERS)
        module_loader.load_app_module(app_name, app_dir, _HOOK)(_ctx())

        # Same name prefix, different directory: ``<app>-other`` must not be
        # swallowed by a bare string-prefix match on ``<app>``.
        neighbour_dir = tmp_path / f"{app_name}-other"
        neighbour_dir.mkdir()
        outside = types.ModuleType(f"{pkg_name}_outside")
        outside.__file__ = str(neighbour_dir / "outside.py")
        monkeypatch.setitem(sys.modules, outside.__name__, outside)
        # Core's namespace is never evicted, even if a file under the app dir
        # was somehow imported under it.
        core_named = types.ModuleType("kiro_crew._update_fixture_probe")
        core_named.__file__ = str(app_dir / "src" / "pkg" / "probe.py")
        monkeypatch.setitem(sys.modules, core_named.__name__, core_named)
        # A native extension under the app dir is left alone: CPython cannot
        # re-initialise most of them, so evicting one breaks the next import.
        native = types.ModuleType(f"{pkg_name}_native")
        native.__file__ = str(app_dir / "src" / "pkg" / "native.so")
        monkeypatch.setitem(sys.modules, native.__name__, native)

        unload_app_modules(app_name)

        assert sys.modules.get(outside.__name__) is outside
        assert sys.modules.get(core_named.__name__) is core_named
        assert sys.modules.get(native.__name__) is native
        assert _real_keys(pkg_name) == []

    @pytest.mark.asyncio
    async def test_native_child_keeps_its_parent_package_across_an_update(
        self, tmp_path: Path, app_name: str, pkg_name: str
    ) -> None:
        # The extension stays resident, so its parent package must too: a fresh
        # parent would not have ``native`` bound and the reload would 404.
        app_dir = tmp_path / app_name
        registry = _registry()
        _write_version(app_dir, pkg_name, _V1_HELPERS, _NATIVE_ROUTE)
        assert await registry.register_app_routes(app_name, app_dir, _HOOK, _ctx())
        parent = sys.modules[pkg_name]
        native = sys.modules[f"{pkg_name}.native"]

        registry.deregister_app_routes(app_name)

        assert sys.modules.get(pkg_name) is parent
        assert sys.modules.get(f"{pkg_name}.native") is native
        assert f"{pkg_name}.helpers" not in sys.modules
        assert not hasattr(parent, "helpers")

        _write_version(app_dir, pkg_name, _V2_HELPERS, _NATIVE_ROUTE)
        v2_ctx = _ctx()
        described = await registry.register_app_routes(app_name, app_dir, _HOOK, v2_ctx)

        v2_ctx.health.mark_degraded.assert_not_called()
        assert f"GET /api/apps/{app_name}/native" in described
        assert _value(await _call(registry, app_name, "GET", "/native")) == "native"
        assert _value(await _call(registry, app_name, "GET", "/ping")) == "v2"

    def test_builtin_apps_are_not_swept(
        self, tmp_path: Path, app_name: str, pkg_name: str, monkeypatch
    ) -> None:
        # Builtin apps ship inside the Kiro Crew package and are trusted like Core
        # code; their real-name modules are Core modules and stay resident.
        monkeypatch.setattr(module_loader, "_is_builtin_app", lambda *_: True)
        app_dir = tmp_path / app_name
        _write_version(app_dir, pkg_name, _V1_HELPERS)
        module_loader.load_app_module(app_name, app_dir, _HOOK)(_ctx())

        unload_app_modules(app_name)

        assert pkg_name in sys.modules
        assert not is_app_module_loaded(app_name)
