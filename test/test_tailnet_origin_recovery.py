"""Background recovery for a tailnet origin missed during gateway startup."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from kiro_crew.dashboard import tailnet as recovery
from kiro_crew.dashboard.urls import build_allowed_hosts

_HOST = "desk.tail-abc.ts.net"


class TestRecovery:
    @pytest.mark.asyncio
    async def test_failed_startup_probe_recovers_without_restart(self, monkeypatch) -> None:
        """A later valid name updates the shared Origin/Host source of truth."""

        app = web.Application()
        baseline = {"http://127.0.0.1:5476"}
        app["allowed_origins"] = baseline
        state = recovery.TailnetOriginState(load_enabled=lambda: True)
        probes = 0
        governance_tools: list[str] = []

        def _name() -> str | None:
            nonlocal probes
            probes += 1
            assert f"https://{_HOST}" not in baseline
            return None if probes == 1 else _HOST

        def _pinned(*, audit_tool: str = "") -> bool:
            governance_tools.append(audit_tool)
            return False

        monkeypatch.setattr(recovery, "_ORIGIN_RECOVERY_INITIAL_SECS", 0)
        monkeypatch.setattr(recovery, "_ORIGIN_RECOVERY_MAX_SECS", 0)
        monkeypatch.setattr(recovery, "self_dns_name", _name)
        monkeypatch.setattr(recovery, "is_governance_pinned_off", _pinned)
        monkeypatch.setattr(recovery, "_origin_resolved_now", lambda: 1_786_100_000)

        await recovery._recover_tailnet_origin(app, state)

        assert probes == 2
        assert baseline == {
            "http://127.0.0.1:5476",
            f"https://{_HOST}",
        }
        assert _HOST in build_allowed_hosts(baseline)
        assert (state.host, state.resolved_at) == (_HOST, 1_786_100_000)
        assert governance_tools == [
            "tailnet_origin_recover",
            "tailnet_origin_recover",
            "tailnet_origin_recover",
        ]

    @pytest.mark.asyncio
    async def test_disable_before_activation_keeps_boundary_closed(self, monkeypatch) -> None:
        """The live config is re-read after resolution and wins before the grant."""

        app = web.Application()
        allowed = {"http://127.0.0.1:5476"}
        app["allowed_origins"] = allowed
        enabled_states = iter([True, False])
        state = recovery.TailnetOriginState(load_enabled=lambda: next(enabled_states))

        monkeypatch.setattr(recovery, "_ORIGIN_RECOVERY_INITIAL_SECS", 0)
        monkeypatch.setattr(recovery, "self_dns_name", lambda: _HOST)
        monkeypatch.setattr(
            recovery,
            "is_governance_pinned_off",
            lambda **_kwargs: False,
        )

        await recovery._recover_tailnet_origin(app, state)

        assert allowed == {"http://127.0.0.1:5476"}
        assert (state.host, state.resolved_at) == ("", 0)

    @pytest.mark.asyncio
    async def test_policy_pin_never_runs_the_daemon(self, monkeypatch) -> None:
        app = web.Application()
        app["allowed_origins"] = {"http://127.0.0.1:5476"}
        enabled_states = iter([True, False])
        state = recovery.TailnetOriginState(load_enabled=lambda: next(enabled_states))
        probed: list[bool] = []
        governance_tools: list[str] = []

        monkeypatch.setattr(recovery, "_ORIGIN_RECOVERY_INITIAL_SECS", 0)
        monkeypatch.setattr(recovery, "_ORIGIN_RECOVERY_MAX_SECS", 0)
        monkeypatch.setattr(
            recovery,
            "self_dns_name",
            lambda: probed.append(True) or _HOST,
        )
        monkeypatch.setattr(
            recovery,
            "is_governance_pinned_off",
            lambda *, audit_tool="": governance_tools.append(audit_tool) or True,
        )

        await recovery._recover_tailnet_origin(app, state)

        assert probed == []
        assert state.host == ""
        assert governance_tools == ["tailnet_origin_recover"]

    @pytest.mark.asyncio
    async def test_cleanup_cancels_and_awaits_the_task(self, monkeypatch) -> None:
        started = asyncio.Event()

        async def _pending(_app: web.Application, _state: recovery.TailnetOriginState) -> None:
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(recovery, "_recover_tailnet_origin", _pending)
        app = web.Application()
        app["allowed_origins"] = {"http://127.0.0.1:5476"}
        recovery.install_tailnet_origin_recovery(
            app,
            enabled=True,
            initial_host="",
            load_enabled=lambda: True,
        )

        await recovery._start_origin_recovery(app)
        await started.wait()
        state = app["tailnet_origin_state"]
        assert isinstance(state, recovery.TailnetOriginState)
        task = state.task
        assert task is not None and not task.done()

        await recovery._stop_origin_recovery(app)

        assert task.cancelled()
        assert state.task is None

    @pytest.mark.asyncio
    async def test_missing_origin_set_fails_closed(self, monkeypatch) -> None:
        app = web.Application()
        state = recovery.TailnetOriginState(load_enabled=lambda: True)

        monkeypatch.setattr(recovery, "_ORIGIN_RECOVERY_INITIAL_SECS", 0)
        monkeypatch.setattr(recovery, "self_dns_name", lambda: _HOST)
        monkeypatch.setattr(
            recovery,
            "is_governance_pinned_off",
            lambda **_kwargs: False,
        )

        await recovery._recover_tailnet_origin(app, state)

        assert (state.host, state.resolved_at) == ("", 0)

    @pytest.mark.asyncio
    async def test_unreadable_config_is_not_treated_as_enabled(self) -> None:
        def _unreadable() -> bool:
            raise OSError("unreadable")

        assert await recovery._origin_configured_enabled(_unreadable) is None

    @pytest.mark.asyncio
    async def test_guard_absorbs_an_unexpected_failure(self, monkeypatch, caplog) -> None:
        async def _boom(
            _app: web.Application,
            _state: recovery.TailnetOriginState,
        ) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(recovery, "_recover_tailnet_origin", _boom)

        with caplog.at_level("ERROR", logger=recovery.logger.name):
            await recovery._run_origin_recovery_guarded(
                web.Application(),
                recovery.TailnetOriginState(),
            )

        assert "request boundary remains unchanged" in caplog.text


def test_runtime_accessor_prefers_recovered_state() -> None:
    app: dict[str, object] = {
        "tailnet_host": "",
        "tailnet_resolved_at": 0,
        "tailnet_origin_state": recovery.TailnetOriginState(
            host=_HOST,
            resolved_at=1_786_100_000,
        ),
    }

    assert recovery.running_tailnet_origin(app) == (_HOST, 1_786_100_000)


def test_runtime_accessor_sanitizes_legacy_timestamp() -> None:
    assert recovery.running_tailnet_origin(
        {"tailnet_host": _HOST, "tailnet_resolved_at": "not-an-int"}
    ) == (_HOST, 0)


def test_install_with_initial_host_needs_no_background_hooks(monkeypatch) -> None:
    app = web.Application()
    monkeypatch.setattr(recovery, "_origin_resolved_now", lambda: 1_786_100_000)
    startup_hooks = len(app.on_startup)
    cleanup_hooks = len(app.on_cleanup)

    recovery.install_tailnet_origin_recovery(
        app,
        enabled=True,
        initial_host=_HOST,
        load_enabled=lambda: True,
    )

    assert recovery.running_tailnet_origin(app) == (_HOST, 1_786_100_000)
    assert len(app.on_startup) == startup_hooks
    assert len(app.on_cleanup) == cleanup_hooks
