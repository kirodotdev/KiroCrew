"""Tests for builtin app enable/disable — config sync and service notification.

The open-source build ships no bundled builtin gateway services, so
``_BUILTIN_SERVICE_APPS`` is empty by default.  These tests exercise the
generic builtin-service machinery (config sync + restart notification) by
registering a synthetic test builtin into that registry, rather than relying
on any specific bundled service (the former ``secretary`` builtin was removed).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.apps import routes
from kiro_crew.apps.routes import (
    _notify_builtin_service,
    _sync_builtin_config,
    handle_disable_app,
)

# Synthetic builtin used to drive the generic machinery.
_TEST_BUILTIN = "test-svc"
_TEST_CFG_KEY = "test_svc"
_TEST_RESTART_ATTR = "_test_svc_restart"


@pytest.fixture
def register_test_builtin():
    """Register a synthetic builtin into the service registry for the test."""
    with patch.dict(
        routes._BUILTIN_SERVICE_APPS,
        {_TEST_BUILTIN: (_TEST_CFG_KEY, _TEST_RESTART_ATTR)},
        clear=False,
    ):
        yield


# ── _sync_builtin_config ──


class TestSyncBuiltinConfig:
    def test_sets_enabled_false(self, tmp_path, register_test_builtin):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({_TEST_CFG_KEY: {"enabled": True, "poll_interval_seconds": 60}}))
        with patch("kiro_crew.apps.routes.config_path", return_value=cfg):
            _sync_builtin_config(_TEST_BUILTIN, enabled=False)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data[_TEST_CFG_KEY]["enabled"] is False
        assert data[_TEST_CFG_KEY]["poll_interval_seconds"] == 60  # preserved

    def test_sets_enabled_true(self, tmp_path, register_test_builtin):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({_TEST_CFG_KEY: {"enabled": False}}))
        with patch("kiro_crew.apps.routes.config_path", return_value=cfg):
            _sync_builtin_config(_TEST_BUILTIN, enabled=True)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data[_TEST_CFG_KEY]["enabled"] is True

    def test_creates_section_if_missing(self, tmp_path, register_test_builtin):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"agent": {"model": "auto"}}))
        with patch("kiro_crew.apps.routes.config_path", return_value=cfg):
            _sync_builtin_config(_TEST_BUILTIN, enabled=False)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data[_TEST_CFG_KEY]["enabled"] is False
        assert data["agent"]["model"] == "auto"  # preserved

    def test_creates_file_if_missing(self, tmp_path, register_test_builtin):
        cfg = tmp_path / "config.json"
        with patch("kiro_crew.apps.routes.config_path", return_value=cfg):
            _sync_builtin_config(_TEST_BUILTIN, enabled=True)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data[_TEST_CFG_KEY]["enabled"] is True

    def test_noop_for_non_builtin(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({}))
        with patch("kiro_crew.apps.routes.config_path", return_value=cfg):
            _sync_builtin_config("some-other-app", enabled=False)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert _TEST_CFG_KEY not in data

    def test_raises_on_corrupt_config(self, tmp_path, register_test_builtin):
        cfg = tmp_path / "config.json"
        cfg.write_text("{corrupt json!!!")
        with patch("kiro_crew.apps.routes.config_path", return_value=cfg):
            with pytest.raises(OSError):
                _sync_builtin_config(_TEST_BUILTIN, enabled=False)
        # File should be untouched — not overwritten with empty dict
        assert cfg.read_text(encoding="utf-8") == "{corrupt json!!!"


# ── _notify_builtin_service ──


class TestNotifyBuiltinService:
    @pytest.mark.asyncio
    async def test_calls_restart_on_success(self, register_test_builtin):
        restart_fn = AsyncMock(return_value="ok")
        state = type("S", (), {_TEST_RESTART_ATTR: restart_fn})()
        request = type("R", (), {"app": {"state": state}})()
        result = await _notify_builtin_service(request, _TEST_BUILTIN)
        assert result is None
        restart_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_warning_on_failure(self, register_test_builtin):
        restart_fn = AsyncMock(side_effect=RuntimeError("boom"))
        state = type("S", (), {_TEST_RESTART_ATTR: restart_fn})()
        request = type("R", (), {"app": {"state": state}})()
        result = await _notify_builtin_service(request, _TEST_BUILTIN)
        assert "restart failed" in result

    @pytest.mark.asyncio
    async def test_returns_warning_when_no_callback(self, register_test_builtin):
        state = type("S", (), {})()
        request = type("R", (), {"app": {"state": state}})()
        result = await _notify_builtin_service(request, _TEST_BUILTIN)
        assert "restart gateway" in result.lower()

    @pytest.mark.asyncio
    async def test_noop_for_non_builtin(self):
        request = type("R", (), {"app": {}})()
        result = await _notify_builtin_service(request, "some-other-app")
        assert result is None

    @pytest.mark.asyncio
    async def test_init_returned_without_service_is_ok(self, register_test_builtin):
        restart_fn = AsyncMock(return_value="init returned without service")
        state = type("S", (), {_TEST_RESTART_ATTR: restart_fn})()
        request = type("R", (), {"app": {"state": state}})()
        result = await _notify_builtin_service(request, _TEST_BUILTIN)
        assert result is None


# ── Integration: handle_disable_app syncs config for builtin ──


class TestHandleDisableBuiltin:
    @pytest.mark.asyncio
    async def test_disable_builtin_syncs_config_and_stops_service(self, tmp_path, register_test_builtin):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({_TEST_CFG_KEY: {"enabled": True}}))

        restart_fn = AsyncMock(return_value="ok")
        state = type("S", (), {_TEST_RESTART_ATTR: restart_fn})()

        request = AsyncMock()
        request.match_info = {"name": _TEST_BUILTIN}
        request.app = {"state": state}

        with (
            patch("kiro_crew.apps.routes.get_app", return_value={
                "name": _TEST_BUILTIN, "origin": "builtin",
                "resources": "gateway", "lifecycle": "locked",
                "enabled": True, "manifest": {},
            }),
            patch("kiro_crew.apps.routes.disable_app") as mock_disable,
            patch("kiro_crew.apps.routes.stop_app_backend"),
            patch("kiro_crew.apps.routes.deregister_app"),
            patch("kiro_crew.apps.routes.config_path", return_value=cfg),
            patch("kiro_crew.apps.routes.sel"),
        ):
            mock_disable.return_value = type("R", (), {"ok": True, "to_dict": lambda self: {"ok": True, "name": _TEST_BUILTIN, "message": "disabled"}})()
            resp = await handle_disable_app(request)

        body = json.loads(resp.body)
        assert body["ok"] is True
        # Config.json should now say enabled: false
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data[_TEST_CFG_KEY]["enabled"] is False
        # Restart callback should have been called (stops the service)
        restart_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_disable_non_builtin_skips_config_sync(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({}))

        request = AsyncMock()
        request.match_info = {"name": "my-app"}
        request.app = {}

        with (
            patch("kiro_crew.apps.routes.get_app", return_value={
                "name": "my-app", "origin": "registry",
                "resources": "gateway", "lifecycle": "gateway",
                "enabled": True, "manifest": {},
            }),
            patch("kiro_crew.apps.routes.disable_app") as mock_disable,
            patch("kiro_crew.apps.routes.stop_app_backend"),
            patch("kiro_crew.apps.routes.deregister_app"),
            patch("kiro_crew.apps.routes.config_path", return_value=cfg),
            patch("kiro_crew.apps.routes.sel"),
        ):
            mock_disable.return_value = type("R", (), {"ok": True, "to_dict": lambda self: {"ok": True, "name": "my-app", "message": "disabled"}})()
            resp = await handle_disable_app(request)

        body = json.loads(resp.body)
        assert body["ok"] is True
        # Config.json should be untouched
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert _TEST_CFG_KEY not in data


# ── _redact_warning ──


class TestRedactWarning:
    def test_passes_through_clean_string(self):
        from kiro_crew.apps.routes import _redact_warning
        assert _redact_warning("config sync failed: boom") == "config sync failed: boom"

    def test_redacts_credentials(self):
        from kiro_crew.apps.routes import _redact_warning
        result = _redact_warning("error: AKIA1234567890ABCDEF leaked")
        assert "AKIA" not in result or "[REDACTED]" in result


# ── _notify_builtin_service edge cases ──


class TestNotifyBuiltinServiceEdgeCases:
    @pytest.mark.asyncio
    async def test_returns_warning_when_no_state(self, register_test_builtin):
        request = type("R", (), {"app": {}})()
        result = await _notify_builtin_service(request, _TEST_BUILTIN)
        assert result is not None
        assert "gateway" in result.lower()

    @pytest.mark.asyncio
    async def test_returns_warning_on_unexpected_result(self, register_test_builtin):
        restart_fn = AsyncMock(return_value="something unexpected")
        state = type("S", (), {_TEST_RESTART_ATTR: restart_fn})()
        request = type("R", (), {"app": {"state": state}})()
        result = await _notify_builtin_service(request, _TEST_BUILTIN)
        assert "restart returned" in result
