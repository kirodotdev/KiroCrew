"""``AppContext.http_app`` is handed out only where it is already reachable.

Lifecycle hooks receive only the ``AppContext``, so background work needs the Application
there. It is set only for apps declaring a ``routes`` hook, which already reach it via
``request.app``; a lifecycle-only app must get ``None``, or it would be a new grant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew.apps import hooks_integration as hooks_mod
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.lifecycle import LifecycleDispatcher
from kiro_crew.apps.route_registry import RouteRegistry


def _app_info(name: str, hooks: dict[str, str]) -> dict:
    return {
        "name": name,
        "enabled": True,
        "manifest": {
            "name": name,
            "version": "1.0.0",
            "backend": {"hooks": hooks},
            "permissions": {"api": [f"/api/apps/{name}"]},
        },
    }


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A registry on a real Application, with the apps tree under tmp_path."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    gateway_app = web.Application()
    registry = RouteRegistry(gateway_app)
    monkeypatch.setattr(hooks_mod, "_route_registry", registry)
    return gateway_app, registry


def test_registry_publishes_the_application_it_dispatches_on():
    gateway_app = web.Application()
    registry = RouteRegistry(gateway_app)
    assert registry.http_app is gateway_app


def test_registry_http_app_is_read_only():
    """A caller that only meant to look must not be able to swap it."""
    registry = RouteRegistry(web.Application())
    with pytest.raises(AttributeError):
        registry.http_app = web.Application()


def test_a_routes_declaring_app_receives_the_gateway_application(wired):
    gateway_app, _registry = wired
    ctx = hooks_mod._build_app_context_from_info(
        _app_info("with-routes", {"routes": "app.hooks:register_routes"})
    )
    assert ctx.http_app is gateway_app


def test_a_lifecycle_only_app_receives_nothing(wired):
    """The negative control: no routes hook means no request path, so no handle.

    Without this the gate could be quietly dropped and every test above would
    still pass -- they only assert the object arrives when it should.
    """
    ctx = hooks_mod._build_app_context_from_info(
        _app_info("lifecycle-only", {"on_startup": "app.hooks:on_startup"})
    )
    assert ctx.http_app is None


def test_an_app_with_no_backend_hooks_receives_nothing(wired):
    ctx = hooks_mod._build_app_context_from_info(_app_info("hookless", {}))
    assert ctx.http_app is None


def test_an_empty_routes_hook_is_not_a_declaration(wired):
    """``"routes": ""`` is how a manifest spells "no routes"; it must not qualify."""
    ctx = hooks_mod._build_app_context_from_info(_app_info("blank-routes", {"routes": ""}))
    assert ctx.http_app is None


def test_no_registry_means_no_handle(tmp_path, monkeypatch):
    """Before the registry is wired there is nothing to hand out, and no crash."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(hooks_mod, "_route_registry", None)
    ctx = hooks_mod._build_app_context_from_info(
        _app_info("early", {"routes": "app.hooks:register_routes"})
    )
    assert ctx.http_app is None


def test_the_factory_defaults_to_no_handle(tmp_path):
    """A context built without the gateway's wiring carries no Application.

    The factory takes the handle from its caller and never sources one itself, so
    a context built in a test -- or by any code that is not the hooks wiring --
    cannot accidentally acquire the live gateway.
    """
    ctx = build_app_context(app_name="plain", data_dir=tmp_path)
    assert ctx.http_app is None


def test_the_factory_passes_the_handle_straight_through(tmp_path):
    gateway_app = web.Application()
    ctx = build_app_context(app_name="plain", data_dir=tmp_path, http_app=gateway_app)
    assert ctx.http_app is gateway_app


@pytest.mark.asyncio
async def test_the_handle_is_the_same_object_a_route_handler_would_see(tmp_path, monkeypatch):
    """The two paths must agree, or the gate's justification does not hold.

    A real HTTP request lets the handler compare its ``request.app`` with the context's
    ``http_app``; a registry dispatching elsewhere would report ``same: false``.
    """
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    # Same allowance the other hook tests make: loading an app's Python is gated
    # on operator trust, which is not the property under test here.
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
    gateway_app = web.Application()
    registry = RouteRegistry(gateway_app)
    monkeypatch.setattr(hooks_mod, "_route_registry", registry)

    app_root = tmp_path / "hookapp"
    (app_root / "app").mkdir(parents=True)
    (app_root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (app_root / "app" / "hooks.py").write_text(
        "from aiohttp import web\n"
        "from kiro_crew.apps.route_registry import AppRoute\n"
        "\n"
        "\n"
        "async def _ping(request, context):\n"
        "    return web.json_response({'same': request.app is context.http_app})\n"
        "\n"
        "\n"
        "def register_routes(ctx):\n"
        "    return [AppRoute('GET', '/ping', _ping)]\n",
        encoding="utf-8",
    )

    ctx = hooks_mod._build_app_context_from_info(
        _app_info("hookapp", {"routes": "app.hooks:register_routes"})
    )
    registered = await registry.register_app_routes(
        "hookapp", app_root, "app.hooks:register_routes", ctx
    )
    assert registered, f"route registration failed: {ctx.health.to_dict()}"
    registry.ensure_catch_all()

    async with TestClient(TestServer(gateway_app)) as client:
        response = await client.get("/api/apps/hookapp/ping")
        assert response.status == 200
        assert await response.json() == {"same": True}

    assert ctx.http_app is gateway_app


def test_hook_declaring_app_manifest_shape_is_read_defensively(wired):
    """A manifest missing ``backend`` entirely must not raise here."""
    info = {"name": "bare", "enabled": True, "manifest": {"name": "bare"}}
    ctx = hooks_mod._build_app_context_from_info(info)
    assert ctx.http_app is None


def test_written_manifest_on_disk_round_trips(wired, tmp_path):
    """The shape this helper reads is the shape a manifest is stored in.

    The gate reads ``backend.hooks.routes`` out of a nested dict. Asserting it
    against a manifest that made a round trip through JSON on disk keeps the gate
    honest about the real storage form rather than a hand-built dict.
    """
    gateway_app, _registry = wired
    manifest = {
        "name": "ondisk",
        "version": "1.0.0",
        "backend": {"hooks": {"routes": "app.hooks:register_routes"}},
    }
    path = Path(tmp_path) / "app.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    info = {
        "name": "ondisk",
        "enabled": True,
        "manifest": json.loads(path.read_text(encoding="utf-8")),
    }
    ctx = hooks_mod._build_app_context_from_info(info)
    assert ctx.http_app is gateway_app


class TestTheTwoBuildersAgree:
    """A startup context and a shutdown context must carry the same handle.

    The shutdown dispatcher builds its own context, not the enable path's. A ``None``
    there makes ``on_shutdown`` return early, and the background work outlives disable.
    """

    def test_a_shutdown_context_carries_the_handle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        gateway_app = web.Application()
        dispatcher = LifecycleDispatcher(http_app=gateway_app)
        ctx = dispatcher._build_context(
            _app_info("with-routes", {"routes": "app.hooks:register_routes"}),
            phase="startup",
        )
        assert ctx.http_app is gateway_app

    def test_a_shutdown_context_honours_the_same_gate(self, tmp_path, monkeypatch):
        """The fix must not widen the grant on the way in.

        Supplying the Application to the dispatcher would be an easy way to hand
        it to every app; the gate has to apply on this path too.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        dispatcher = LifecycleDispatcher(http_app=web.Application())
        ctx = dispatcher._build_context(
            _app_info("lifecycle-only", {"on_startup": "app.hooks:on_startup"}),
            phase="startup",
        )
        assert ctx.http_app is None

    def test_a_dispatcher_without_the_application_yields_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        dispatcher = LifecycleDispatcher()
        ctx = dispatcher._build_context(
            _app_info("with-routes", {"routes": "app.hooks:register_routes"}),
            phase="startup",
        )
        assert ctx.http_app is None

    @pytest.mark.parametrize(
        "hooks",
        [
            pytest.param({"routes": "app.hooks:register_routes"}, id="routes"),
            pytest.param(
                {
                    "routes": "app.hooks:register_routes",
                    "on_startup": "app.hooks:on_startup",
                    "on_shutdown": "app.hooks:on_shutdown",
                },
                id="routes-and-lifecycle",
            ),
            pytest.param({"on_startup": "app.hooks:on_startup"}, id="lifecycle-only"),
            pytest.param({"routes": ""}, id="blank-routes"),
            pytest.param({}, id="no-hooks"),
        ],
    )
    def test_both_builders_reach_the_same_answer(self, tmp_path, monkeypatch, hooks):
        """Whatever the manifest, the two paths must not disagree.

        Parametrised over the shapes that decide the gate rather than asserting
        one case, because the defect this guards is a DIVERGENCE: either builder
        could be changed alone and only a comparison catches it.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        gateway_app = web.Application()
        monkeypatch.setattr(hooks_mod, "_route_registry", RouteRegistry(gateway_app))
        info = _app_info("either-way", hooks)

        from_enable = hooks_mod._build_app_context_from_info(info)
        from_lifecycle = LifecycleDispatcher(http_app=gateway_app)._build_context(
            info, phase="startup"
        )

        assert from_enable.http_app is from_lifecycle.http_app


def test_the_gate_predicate_is_shared_not_copied():
    """Both builders must call the one predicate, not each spell the rule.

    A second copy of "declares a routes hook" is how the two contexts drift apart
    again, so the predicate is asserted to be importable and authoritative rather
    than left as a convention.
    """
    from kiro_crew.apps.context import http_app_for_manifest

    gateway_app = web.Application()
    assert (
        http_app_for_manifest({"backend": {"hooks": {"routes": "a:b"}}}, gateway_app) is gateway_app
    )
    assert http_app_for_manifest({"backend": {"hooks": {"on_startup": "a:b"}}}, gateway_app) is None
    assert http_app_for_manifest({}, gateway_app) is None
    assert http_app_for_manifest({"backend": {"hooks": {"routes": "a:b"}}}, None) is None


def test_the_live_wiring_feeds_both_builders_one_application(tmp_path, monkeypatch):
    """``init_hooks_system`` must seed the registry and the dispatcher alike.

    The enable path reads ``_route_registry.http_app`` and the lifecycle path the
    dispatcher's field. Other tests seed both by hand, so only this checks the wiring.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(hooks_mod, "_route_registry", None)
    monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", None)
    gateway_app = web.Application()

    hooks_mod.init_hooks_system(gateway_app)

    registry = hooks_mod.get_route_registry()
    dispatcher = hooks_mod.get_lifecycle_dispatcher()
    assert registry is not None and dispatcher is not None
    assert registry.http_app is gateway_app
    assert dispatcher._http_app is gateway_app

    info = _app_info("one-application", {"routes": "app.hooks:register_routes"})
    assert (
        hooks_mod._build_app_context_from_info(info).http_app
        is dispatcher._build_context(info, phase="startup").http_app
        is gateway_app
    )


class TestTeardownReadsTheEnableItIsTearingDown:
    """Teardown must reuse the grant from enable, not recompute it from the manifest.

    The app writes the on-disk manifest. Dropping ``routes`` later would hand ``None`` to
    ``on_shutdown`` and the worker survives the disable; adding ``routes`` later must not
    grant a shutdown hook whose startup never held it. So both answers are recorded.
    """

    @pytest.fixture(autouse=True)
    def _clean_caches(self, tmp_path, monkeypatch):
        """Both caches are process-global; a leaked entry would fake a pass here."""
        from kiro_crew.apps.module_loader import clear_all_shutdown_callables

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        clear_all_shutdown_callables()
        yield
        clear_all_shutdown_callables()

    def test_a_manifest_that_drops_routes_still_gets_the_handle_at_teardown(self):
        gateway_app = web.Application()
        dispatcher = LifecycleDispatcher(http_app=gateway_app)

        at_enable = _app_info(
            "drops-routes",
            {"routes": "app.hooks:register_routes", "on_shutdown": "app.hooks:on_shutdown"},
        )
        assert dispatcher._build_context(at_enable, phase="startup").http_app is gateway_app

        # The app rewrote its own record: lifecycle hook kept, routes hook gone.
        at_teardown = _app_info("drops-routes", {"on_shutdown": "app.hooks:on_shutdown"})
        assert dispatcher._build_context(at_teardown, phase="shutdown").http_app is gateway_app

    def test_a_manifest_that_adds_routes_does_not_gain_the_handle_at_teardown(self):
        """The mirror case: teardown must not grant what startup never had."""
        gateway_app = web.Application()
        dispatcher = LifecycleDispatcher(http_app=gateway_app)

        at_enable = _app_info("adds-routes", {"on_startup": "app.hooks:on_startup"})
        assert dispatcher._build_context(at_enable, phase="startup").http_app is None

        at_teardown = _app_info(
            "adds-routes",
            {"routes": "app.hooks:register_routes", "on_shutdown": "app.hooks:on_shutdown"},
        )
        assert dispatcher._build_context(at_teardown, phase="shutdown").http_app is None

    def test_an_unchanged_manifest_is_unaffected(self):
        """The ordinary case must keep behaving exactly as before."""
        gateway_app = web.Application()
        dispatcher = LifecycleDispatcher(http_app=gateway_app)
        info = _app_info(
            "steady",
            {"routes": "app.hooks:register_routes", "on_shutdown": "app.hooks:on_shutdown"},
        )
        assert dispatcher._build_context(info, phase="startup").http_app is gateway_app
        assert dispatcher._build_context(info, phase="shutdown").http_app is gateway_app

    def test_with_nothing_recorded_teardown_reads_the_manifest(self):
        """The fallback is the pre-existing behaviour, not a withheld handle.

        A teardown can run for an app this process never enabled (a gateway that
        restarted, a path outside the production wiring). Treating "no record" as
        "no grant" would silently stop feeding the handle to apps that legitimately
        hold it, which is the same defect pointing the other way.
        """
        gateway_app = web.Application()
        dispatcher = LifecycleDispatcher(http_app=gateway_app)
        info = _app_info(
            "never-enabled-here",
            {"routes": "app.hooks:register_routes", "on_shutdown": "app.hooks:on_shutdown"},
        )
        assert dispatcher._build_context(info, phase="shutdown").http_app is gateway_app

    def test_a_reload_invalidates_the_recorded_grant(self):
        """A record from enable v1 must not describe the app loaded by enable v2."""
        from kiro_crew.apps.module_loader import cached_http_app_grant, unload_app_modules

        gateway_app = web.Application()
        dispatcher = LifecycleDispatcher(http_app=gateway_app)
        info = _app_info("reloaded", {"routes": "app.hooks:register_routes"})
        dispatcher._build_context(info, phase="startup")
        assert cached_http_app_grant("reloaded") is True

        unload_app_modules("reloaded")
        assert cached_http_app_grant("reloaded") is None

    @pytest.mark.asyncio
    async def test_an_app_with_no_startup_hook_is_recorded_too(self):
        """``on_shutdown`` without ``on_startup`` builds no startup context at all.

        Its route handlers can still have spawned work the shutdown hook is meant
        to stop, so the enable-time record cannot come only from the startup
        builder. Both production load paths call ``cache_shutdown_for``, so the
        grant is written there as well.
        """
        gateway_app = web.Application()
        dispatcher = LifecycleDispatcher(http_app=gateway_app)

        at_enable = _app_info(
            "no-startup-hook",
            {"routes": "app.hooks:register_routes", "on_shutdown": "app.hooks:on_shutdown"},
        )
        await dispatcher.cache_shutdown_for(at_enable)

        at_teardown = _app_info("no-startup-hook", {"on_shutdown": "app.hooks:on_shutdown"})
        assert dispatcher._build_context(at_teardown, phase="shutdown").http_app is gateway_app

    def test_the_grant_and_the_shutdown_callable_share_one_lifetime(self):
        """They describe ONE enable, so neither may be cleared without the other.

        A teardown holding the callable from this enable and the grant from an
        earlier one would be reading two different enables. The two clear paths are
        asserted rather than left to a comment, because dropping one of the two
        lines is exactly the drift this guards.
        """
        from kiro_crew.apps.module_loader import (
            cache_http_app_grant,
            cache_shutdown_callable,
            cached_http_app_grant,
            clear_all_shutdown_callables,
            clear_shutdown_callable,
            resolve_loaded_callable,
        )

        def _noop() -> None:
            return None

        for clear in (lambda: clear_shutdown_callable("paired"), clear_all_shutdown_callables):
            cache_shutdown_callable("paired", _noop)
            cache_http_app_grant("paired", True)
            assert resolve_loaded_callable("paired", "app.hooks:on_shutdown") is _noop
            assert cached_http_app_grant("paired") is True

            clear()

            assert resolve_loaded_callable("paired", "app.hooks:on_shutdown") is None
            assert cached_http_app_grant("paired") is None
