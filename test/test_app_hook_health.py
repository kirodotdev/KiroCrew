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
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", SimpleNamespace())
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
