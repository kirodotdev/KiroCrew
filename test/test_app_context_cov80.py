"""Coverage for ``apps/context`` — health bookkeeping and the permission-gated factory.

``AppHealthStatus`` is what the enable path reports when a subsystem comes up
half-wired, and ``build_app_context`` is the deny-by-default wiring: a service is
populated only when BOTH the permission and the host implementation are present.
Both halves of each of those conditions are pinned here.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.context import AppHealthStatus, build_app_context
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.event_bus import EventBus
from kiro_crew.apps.spawn_sdk import SpawnSDK


class TestAppHealthStatus:
    def test_starts_healthy_and_reports_only_the_status(self) -> None:
        assert AppHealthStatus().to_dict() == {"status": "healthy"}

    def test_mark_degraded_records_issue_and_timestamp(self) -> None:
        health = AppHealthStatus()
        health.mark_degraded("cron sdk unavailable")
        assert health.status == "degraded"
        d = health.to_dict()
        assert d["issues"] == ["cron sdk unavailable"]
        assert d["last_checked"].endswith("Z")
        assert health.last_checked == d["last_checked"]

    def test_mark_error_overrides_degraded_and_appends(self) -> None:
        health = AppHealthStatus()
        health.mark_degraded("first")
        health.mark_error("second")
        assert health.status == "error"
        assert health.to_dict()["issues"] == ["first", "second"]

    def test_issue_free_status_omits_optional_keys(self) -> None:
        health = AppHealthStatus(status="degraded")
        assert health.to_dict() == {"status": "degraded"}


class TestBuildAppContext:
    def test_no_permissions_populates_no_services(self, tmp_path) -> None:
        ctx = build_app_context("nulla", tmp_path)
        assert (ctx.cron, ctx.events, ctx.storage, ctx.spawn) == (None, None, None, None)
        assert ctx.name == "nulla"
        assert ctx.data_dir == tmp_path
        assert ctx.config == {}
        assert ctx.logger.name == "kirocrew.app.nulla"
        assert ctx.health.status == "healthy"

    def test_all_permissions_with_hosts_populate_every_service(self, tmp_path) -> None:
        def _spawn_impl(*_a: Any, **_k: Any) -> str:
            return "spawn-1"

        _spawn_impl.done_probe = lambda spawn_id: True  # type: ignore[attr-defined]

        ctx = build_app_context(
            "plena",
            tmp_path,
            permissions={"cron": True, "events": ["evt.one"], "spawn": True, "storage": True},
            cron_service=object(),
            broadcast_fn=lambda payload: None,
            spawn_impl=_spawn_impl,
            app_config={"tuning": "kx"},
        )
        assert isinstance(ctx.cron, CronSDK)
        assert isinstance(ctx.events, EventBus)
        assert isinstance(ctx.spawn, SpawnSDK)
        assert isinstance(ctx.storage, AppStorage)
        assert ctx.config == {"tuning": "kx"}
        assert ctx.spawn.is_done("anything") is True
        assert (tmp_path / "kv").is_dir()

    def test_permission_without_host_service_stays_none(self, tmp_path) -> None:
        ctx = build_app_context(
            "hostless",
            tmp_path,
            permissions={"cron": True, "events": ["evt.one"], "spawn": True},
        )
        assert (ctx.cron, ctx.events, ctx.spawn) == (None, None, None)

    def test_host_service_without_permission_stays_none(self, tmp_path) -> None:
        ctx = build_app_context(
            "permless",
            tmp_path,
            permissions={"events": []},
            cron_service=object(),
            broadcast_fn=lambda payload: None,
            spawn_impl=lambda *a, **k: "spawn-1",
        )
        assert (ctx.cron, ctx.events, ctx.spawn) == (None, None, None)

    def test_spawn_permission_must_be_exactly_true(self, tmp_path) -> None:
        ctx = build_app_context(
            "truthy",
            tmp_path,
            permissions={"spawn": "yes"},
            spawn_impl=lambda *a, **k: "spawn-1",
        )
        assert ctx.spawn is None

    def test_spawn_sdk_without_done_probe_reports_not_done(self, tmp_path) -> None:
        ctx = build_app_context(
            "probeless",
            tmp_path,
            permissions={"spawn": True},
            spawn_impl=lambda *a, **k: "spawn-1",
        )
        assert isinstance(ctx.spawn, SpawnSDK)
        assert ctx.spawn.is_done("spawn-1") is False

    def test_storage_only_needs_the_permission(self, tmp_path) -> None:
        ctx = build_app_context("solum", tmp_path, permissions={"storage": True})
        assert isinstance(ctx.storage, AppStorage)
        assert ctx.storage.app_name == "solum"
