"""Issue #6078 Part B: the reason a hook failed must outlive the AppContext.

``register_app_routes`` records WHY it could not wire an app up
(``ctx.health.mark_degraded``), but the context it writes to was dropped on the
gateway startup path — only ``on_app_enable`` ever read it back. An app that was
installed, trusted and enabled therefore looked exactly like an app that was
never installed: the dispatcher answers 404 either way.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import kiro_crew.apps.hooks_integration as hooks_mod
import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.route_registry import RouteRegistry


@pytest.fixture(autouse=True)
def _clean_hook_health():
    """The registry is process-global; no test may inherit another's entries."""
    hooks_mod._hook_health.clear()
    yield
    hooks_mod._hook_health.clear()


def _write_app(app_dir: Path, module: str, body: str) -> None:
    path = app_dir / (module.replace(".", "/") + ".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _app_info(name: str, routes_hook: str) -> dict:
    return {
        "name": name,
        "enabled": True,
        "manifest": {"name": name, "backend": {"hooks": {"routes": routes_hook}}},
    }


def _arm_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_dir: Path, info: dict
) -> None:
    """Point on_gateway_startup at one app living under tmp_path."""

    class _StubDispatcher(SimpleNamespace):
        async def cache_shutdown_for(self, app_info):
            return None

        async def dispatch_disable(self, app_info):
            return True

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", _StubDispatcher())
    monkeypatch.setattr(hooks_mod, "_route_registry", RouteRegistry(web.Application()))
    monkeypatch.setattr(hooks_mod, "list_apps", lambda: [info])
    monkeypatch.setattr(hooks_mod, "_app_hook_root", lambda _name: app_dir)
    monkeypatch.setattr(hooks_mod, "app_execution_denied", lambda *a, **kw: "")
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


class TestStartupPublishesHookHealth:
    @pytest.mark.asyncio
    async def test_failed_route_hook_reason_survives_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A route hook that cannot be imported leaves a readable reason behind."""
        app_dir = tmp_path / "apps" / "broken-app"
        _write_app(app_dir, "backend.routes", "raise RuntimeError('kaboom')\n")
        _arm_startup(
            monkeypatch,
            tmp_path,
            app_dir,
            _app_info("broken-app", "backend.routes:register"),
        )

        await hooks_mod.on_gateway_startup()

        health = hooks_mod.get_all_hook_health().get("broken-app")
        assert health is not None, (
            "startup dropped the reason the route hook failed — an enabled app is "
            "indistinguishable from one that was never installed"
        )
        assert health["status"] == "degraded"
        assert any("Route module load failed" in issue for issue in health["issues"])
        assert any("kaboom" in issue for issue in health["issues"])

    @pytest.mark.asyncio
    async def test_startup_caches_shutdown_callable_on_production_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT [BLOCKING]: cache_shutdown_for must be called from the PRODUCTION
        startup path (on_gateway_startup / on_app_enable), not the unused
        dispatch_enable. Otherwise the shutdown cache is always empty and an
        on_shutdown living in a module on_startup never imported is orphaned after
        uninstall. Here on_gateway_startup runs a successful startup and MUST
        invoke cache_shutdown_for on the dispatcher."""
        app_dir = tmp_path / "apps" / "cached-app"
        _write_app(app_dir, "backend.hooks", "def on_startup(ctx):\n    return None\n")
        info = {
            "name": "cached-app",
            "enabled": True,
            "manifest": {
                "name": "cached-app",
                "backend": {
                    "hooks": {
                        "on_startup": "backend.hooks:on_startup",
                        "on_shutdown": "backend.shutdown:on_shutdown",
                    }
                },
            },
        }
        cached: list[str] = []

        class _SpyDispatcher(SimpleNamespace):
            async def _invoke(self, name, hook_path, ctx, *, phase):
                return True  # startup succeeds

            def cache_shutdown_for(self, app_info):
                cached.append(app_info.get("name", ""))

                # Awaited by production now; return an awaitable.
                async def _noop():
                    return None

                return _noop()

            def _build_context(self, app_info):
                return SimpleNamespace()

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", _SpyDispatcher())
        monkeypatch.setattr(hooks_mod, "_route_registry", RouteRegistry(web.Application()))
        monkeypatch.setattr(hooks_mod, "list_apps", lambda: [info])
        monkeypatch.setattr(hooks_mod, "_app_hook_root", lambda _name: app_dir)
        monkeypatch.setattr(hooks_mod, "app_execution_denied", lambda *a, **kw: "")
        monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
        monkeypatch.setattr(
            hooks_mod,
            "_build_app_context_from_info",
            lambda *a, **kw: SimpleNamespace(
                job=None, health=SimpleNamespace(status="healthy", issues=[])
            ),
        )

        await hooks_mod.on_gateway_startup()

        assert cached == ["cached-app"], (
            "on_gateway_startup must cache the on_shutdown callable via the "
            "production path after a successful startup"
        )
        hooks_mod._loaded_hook_signatures.clear()
        hooks_mod._loaded_hook_manifests.clear()

    @pytest.mark.asyncio
    async def test_routes_only_app_still_caches_shutdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT round-10: a ROUTES-ONLY app (no on_startup) whose routes spawn
        background work, with a separate shutdown module, must still get its
        on_shutdown cached on healthy wiring -- otherwise a CLI uninstall deletes
        the files and the route-created work retains gateway privileges. Caching
        must fire independently of on_startup presence/success."""
        app_dir = tmp_path / "apps" / "routes-app"
        _write_app(app_dir, "backend.routes", "def register(ctx):\n    return []\n")
        info = {
            "name": "routes-app",
            "enabled": True,
            "manifest": {
                "name": "routes-app",
                "backend": {
                    "hooks": {
                        "routes": "backend.routes:register",
                        # NOTE: no on_startup at all; on_shutdown in a separate module.
                        "on_shutdown": "backend.shutdown:on_shutdown",
                    }
                },
            },
        }
        cached: list[str] = []

        class _SpyDispatcher(SimpleNamespace):
            def cache_shutdown_for(self, app_info):
                cached.append(app_info.get("name", ""))

                async def _noop():
                    return None

                return _noop()

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", _SpyDispatcher())
        monkeypatch.setattr(hooks_mod, "_route_registry", RouteRegistry(web.Application()))
        monkeypatch.setattr(hooks_mod, "list_apps", lambda: [info])
        monkeypatch.setattr(hooks_mod, "_app_hook_root", lambda _name: app_dir)
        monkeypatch.setattr(hooks_mod, "app_execution_denied", lambda *a, **kw: "")
        monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
        monkeypatch.setattr(
            hooks_mod,
            "_build_app_context_from_info",
            lambda *a, **kw: SimpleNamespace(
                job=None, health=SimpleNamespace(status="healthy", issues=[])
            ),
        )

        await hooks_mod.on_gateway_startup()

        assert cached == ["routes-app"], (
            "a routes-only app must still cache its on_shutdown on healthy wiring, "
            "independently of on_startup"
        )
        hooks_mod._loaded_hook_signatures.clear()
        hooks_mod._loaded_hook_manifests.clear()

    @pytest.mark.asyncio
    async def test_degraded_boot_stays_unrecorded_so_reconciler_retries_on_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A degraded wire-up (a failed route/startup import) must NOT record a
        loaded signature: leaving it un-recorded is what lets the out-of-process
        reconciler re-attempt the app on its next tick and pick it up once a
        transient failure clears -- the poll-retry-after-recovery the reconciler
        exists to provide. Freezing the degraded state as 'current loaded' would
        strand a transiently-broken app until its code changed."""
        hooks_mod._loaded_hook_signatures.clear()
        hooks_mod._loaded_hook_manifests.clear()
        app_dir = tmp_path / "apps" / "broken-app"
        _write_app(app_dir, "backend.routes", "raise RuntimeError('kaboom')\n")
        info = _app_info("broken-app", "backend.routes:register")
        _arm_startup(monkeypatch, tmp_path, app_dir, info)

        await hooks_mod.on_gateway_startup()

        # Degraded wiring is confirmed by the published health entry...
        assert hooks_mod.get_all_hook_health().get("broken-app") is not None
        # ...and the signature is deliberately NOT recorded, so the reconciler
        # re-attempts the app rather than treating broken hooks as loaded.
        assert (
            hooks_mod.loaded_hook_signature("broken-app") is None
        ), "degraded boot must leave no loaded record so it retries on recovery"
        hooks_mod._loaded_hook_signatures.clear()
        hooks_mod._loaded_hook_manifests.clear()

    @pytest.mark.asyncio
    async def test_degraded_boot_tears_down_startup_work_before_clearing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT round-8 [BLOCKING] F3: on the on_app_enable path (which the
        reconciler re-invokes every tick), a degraded wire-up (route import
        fails) that already ran a successful on_startup leaves detached startup
        work running. Clearing the loaded signature for retry WITHOUT tearing
        that work down first makes the reconciler re-run on_startup every poll,
        stacking a fresh worker on the live one until exhaustion. The degraded
        branch must stop the startup work BEFORE it clears the signature."""
        hooks_mod._loaded_hook_signatures.clear()
        hooks_mod._loaded_hook_manifests.clear()
        events: list[str] = []

        class _SpyDispatcher(SimpleNamespace):
            async def dispatch_disable(self, app_info):
                events.append(f"shutdown:{app_info.get('name', '')}")
                return True

            async def stop_detached_startup_hooks(self, app_name, *, bounded=False):
                events.append(f"stop:{app_name}:bounded={bounded}")
                return True

        real_clear = hooks_mod.clear_loaded_hook_signature

        def _spy_clear(app_name):
            events.append(f"clear:{app_name}")
            return real_clear(app_name)

        # A route hook that fails to import marks the context degraded, so
        # on_app_enable takes its clear-signature-for-retry branch.
        app_dir = tmp_path / "apps" / "broken-app"
        _write_app(app_dir, "backend.routes", "raise RuntimeError('kaboom')\n")
        info = _app_info("broken-app", "backend.routes:register")
        _arm_startup(monkeypatch, tmp_path, app_dir, info)
        monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", _SpyDispatcher())
        monkeypatch.setattr(hooks_mod, "clear_loaded_hook_signature", _spy_clear)

        await hooks_mod.on_app_enable("broken-app", info)

        assert (
            "shutdown:broken-app" in events
        ), "must run the on_shutdown lifecycle on degraded enable"
        assert "stop:broken-app:bounded=True" in events, "must settle detached startup work too"
        assert (
            "clear:broken-app" in events
        ), "must clear the signature for retry when teardown is clean"
        assert events.index("shutdown:broken-app") < events.index(
            "clear:broken-app"
        ), "the shutdown lifecycle must run BEFORE the signature clear, not after"
        hooks_mod._loaded_hook_signatures.clear()
        hooks_mod._loaded_hook_manifests.clear()

    @pytest.mark.asyncio
    async def test_degraded_enable_retains_signature_when_teardown_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT round-9: if the degraded-branch teardown does NOT confirm the
        startup worker stopped, clearing the signature would let the reconciler
        re-run on_startup and stack another worker. So a failed teardown must
        RETAIN the loaded signature (record it) instead of clearing -- the
        retained record suppresses the re-run."""
        hooks_mod._loaded_hook_signatures.clear()
        hooks_mod._loaded_hook_manifests.clear()

        class _FailingStopDispatcher(SimpleNamespace):
            async def dispatch_disable(self, app_info):
                return True

            async def stop_detached_startup_hooks(self, app_name, *, bounded=False):
                return False  # teardown could not confirm the worker stopped

        app_dir = tmp_path / "apps" / "broken-app"
        _write_app(app_dir, "backend.routes", "raise RuntimeError('kaboom')\n")
        info = _app_info("broken-app", "backend.routes:register")
        _arm_startup(monkeypatch, tmp_path, app_dir, info)
        monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", _FailingStopDispatcher())

        await hooks_mod.on_app_enable("broken-app", info)

        # Retained: a loaded signature IS present, so the reconciler will not
        # re-run on_startup and duplicate the still-live worker.
        assert (
            hooks_mod.loaded_hook_signature("broken-app") is not None
        ), "a failed degraded teardown must retain the loaded signature, not clear it"
        hooks_mod._loaded_hook_signatures.clear()
        hooks_mod._loaded_hook_manifests.clear()

    @pytest.mark.asyncio
    async def test_working_route_hook_records_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean wire-up must not leave a health entry for the dashboard to show."""
        app_dir = tmp_path / "apps" / "good-app"
        _write_app(app_dir, "backend.routes", "def register(ctx):\n    return []\n")
        _arm_startup(
            monkeypatch,
            tmp_path,
            app_dir,
            _app_info("good-app", "backend.routes:register"),
        )

        await hooks_mod.on_gateway_startup()

        assert hooks_mod.get_all_hook_health().get("good-app") is None
        assert hooks_mod.get_all_hook_health() == {}


class TestHookHealthLifecycle:
    def test_healthy_rewire_clears_a_stale_failure(self) -> None:
        """A fixed app stops reporting the failure it had on the previous wire-up."""
        hooks_mod._hook_health["formerly-broken"] = {"status": "degraded"}
        ctx = SimpleNamespace(health=SimpleNamespace(status="healthy"))

        assert hooks_mod._publish_hook_health("formerly-broken", ctx) is None
        assert hooks_mod.get_all_hook_health().get("formerly-broken") is None

    def test_snapshots_are_copies(self) -> None:
        """A reader cannot mutate the stored record through the value it got."""
        hooks_mod._hook_health["an-app"] = {"status": "degraded", "issues": ["one"]}

        snapshot = hooks_mod.get_all_hook_health().get("an-app")
        assert snapshot is not None
        snapshot["status"] = "tampered"

        assert hooks_mod.get_all_hook_health().get("an-app")["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_disable_clears_the_recorded_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disabled app has no live hooks, so a stored failure would be a lie."""
        monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", None)
        monkeypatch.setattr(hooks_mod, "_route_registry", None)
        hooks_mod._hook_health["going-away"] = {"status": "degraded"}

        await hooks_mod.on_app_disable("going-away", {"name": "going-away", "manifest": {}})

        assert hooks_mod.get_all_hook_health().get("going-away") is None


class TestListAppsSurfacesHookHealth:
    @pytest.mark.asyncio
    async def test_list_apps_reports_hook_health(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /api/apps carries the hook failure beside backend_status."""
        monkeypatch.setattr(
            routes_mod, "list_apps", lambda: [{"name": "broken-app", "enabled": True}]
        )
        monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])
        hooks_mod._hook_health["broken-app"] = {
            "status": "degraded",
            "issues": ["Route module load failed: boom"],
            "last_checked": "2026-08-26T00:00:00Z",
        }

        resp = await routes_mod.handle_list_apps(make_mocked_request("GET", "/api/apps"))

        assert resp.status == 200
        payload = json.loads(resp.text)
        assert payload[0]["hooks"]["health_status"]["status"] == "degraded"
        assert payload[0]["hooks"]["health_status"]["issues"] == ["Route module load failed: boom"]

    @pytest.mark.asyncio
    async def test_healthy_app_has_no_hooks_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            routes_mod, "list_apps", lambda: [{"name": "good-app", "enabled": True}]
        )
        monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])

        resp = await routes_mod.handle_list_apps(make_mocked_request("GET", "/api/apps"))

        payload = json.loads(resp.text)
        assert "hooks" not in payload[0]

    @pytest.mark.asyncio
    async def test_reported_issues_do_not_leak_the_stored_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redacting for the response must not rewrite what the gateway holds."""
        monkeypatch.setattr(
            routes_mod, "list_apps", lambda: [{"name": "broken-app", "enabled": True}]
        )
        monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])
        monkeypatch.setattr(routes_mod, "_redact_warning", lambda s: "REDACTED")
        hooks_mod._hook_health["broken-app"] = {
            "status": "degraded",
            "issues": ["Route module load failed: boom"],
        }

        resp = await routes_mod.handle_list_apps(make_mocked_request("GET", "/api/apps"))

        payload = json.loads(resp.text)
        assert payload[0]["hooks"]["health_status"]["issues"] == ["REDACTED"]
        assert hooks_mod._hook_health["broken-app"]["issues"] == ["Route module load failed: boom"]
