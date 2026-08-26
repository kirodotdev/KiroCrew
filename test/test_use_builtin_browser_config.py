"""Tests for the dashboard.use_builtin_browser config field (GET/PUT round-trip).

The field chooses whether the ``browser`` MCP tool drives the built-in native
panel or falls back to playwright-cli. The tests pin the contract: it defaults
ON, a malformed value fails OPEN to ON (never silently disables the panel), and
-- because it is edited from the Browser panel while the Chat panel PUTs the
whole config object from its own cache -- the handler applies it ONLY when it is
the sole submitted setting, so a stale full-object Chat save can never revert it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


def test_use_builtin_browser_default_true():
    cfg = KiroCrewConfig()
    assert cfg.dashboard.use_builtin_browser is True


def test_use_builtin_browser_save_load(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.use_builtin_browser = False
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["use_builtin_browser"] is False
    assert KiroCrewConfig.load().dashboard.use_builtin_browser is False


def test_use_builtin_browser_absent_key_stays_true(cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
    assert KiroCrewConfig.load().dashboard.use_builtin_browser is True


def test_use_builtin_browser_non_bool_fails_open_true(cfg_file):
    """A malformed value must fail OPEN to True: loaded through _safe_bool with a
    True default, so a broken config never silently disables the built-in panel."""
    for bad in ("false", 0, 1, ["no"], None):
        cfg_file.write_text(
            json.dumps({"dashboard": {"use_builtin_browser": bad}}),
            encoding="utf-8",
        )
        assert KiroCrewConfig.load().dashboard.use_builtin_browser is True, bad


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


@pytest.fixture()
def handler_app(cfg_file, mock_sel):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    return app


@pytest.mark.asyncio
async def test_handler_put_sole_key_persists(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"use_builtin_browser": False})
        assert resp.status == 200
        assert KiroCrewConfig.load().dashboard.use_builtin_browser is False


@pytest.mark.asyncio
async def test_handler_put_sole_key_invalid_400_with_code(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"use_builtin_browser": "yes"})
        assert resp.status == 400
        body = await resp.json()
        assert "boolean" in body["error"]
        # A new non-2xx body must carry a machine-readable code (error-code contract).
        assert body["code"] == "invalid_use_builtin_browser"
        assert KiroCrewConfig.load().dashboard.use_builtin_browser is True


@pytest.mark.asyncio
async def test_handler_get_returns_use_builtin_browser(handler_app, cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"use_builtin_browser": False}}),
        encoding="utf-8",
    )
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["use_builtin_browser"] is False


@pytest.mark.asyncio
async def test_handler_get_default_true(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["use_builtin_browser"] is True


@pytest.mark.asyncio
async def test_multikey_put_does_not_clobber_use_builtin_browser(handler_app, cfg_file):
    """Pins the lost-update fix: use_builtin_browser is applied ONLY when it is the
    sole submitted key. A Chat-panel full-object PUT built from a stale cache must
    not revert a value the Browser panel just changed."""
    async with TestClient(TestServer(handler_app)) as client:
        # Browser panel turns it OFF via the sole-key path.
        r = await client.put("/api/dashboard/config", json={"use_builtin_browser": False})
        assert r.status == 200
        assert KiroCrewConfig.load().dashboard.use_builtin_browser is False

        # A stale Chat-panel save carries use_builtin_browser=True alongside its own
        # change. The multi-key PUT must NOT apply it.
        body = await (await client.get("/api/dashboard/config")).json()
        body["use_builtin_browser"] = True  # stale cached value
        body["quick_send"] = True
        r2 = await client.put("/api/dashboard/config", json=body)
        assert r2.status == 200

        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.quick_send is True
        assert cfg.dashboard.use_builtin_browser is False  # not reverted


@pytest.mark.asyncio
async def test_handler_put_malformed_dashboard_section_recovers(handler_app, cfg_file):
    """A malformed (non-dict) `dashboard` section on disk must not crash the write.
    The update_config_locked mutate coerces it to a fresh dict instead of raising
    TypeError -> HTTP 500; the setting persists and the section is recovered."""
    cfg_file.write_text(json.dumps({"dashboard": []}), encoding="utf-8")
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"use_builtin_browser": False})
        assert resp.status == 200
        assert KiroCrewConfig.load().dashboard.use_builtin_browser is False


@pytest.mark.asyncio
async def test_handler_put_corrupt_config_logs_failure_and_500(handler_app, cfg_file, mock_sel):
    """A corrupt on-disk config makes update_config_locked's fail-closed read raise;
    the handler must emit an outcome='failure' SEL event and return 500 with a
    machine-readable code, never an unlogged 500 (backend-security-controls audit
    requirement; pre-PR cfg.save() logged a success here)."""
    cfg_file.write_text("{ this is not valid json", encoding="utf-8")
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"use_builtin_browser": False})
        assert resp.status == 500
        body = await resp.json()
        assert body["code"] == "dashboard_config_write_failed"
    outcomes = [
        c.kwargs.get("outcome") for c in mock_sel.log_tool_invocation.call_args_list
    ]
    assert "failure" in outcomes, f"expected a failure SEL event, got {outcomes}"


@pytest.mark.asyncio
async def test_handler_put_cancelled_write_logs_failure(handler_app, cfg_file, mock_sel):
    """Cancellation during the off-thread write (client disconnect / shutdown) must
    still emit an outcome='failure' SEL event and re-raise -- `except Exception`
    alone would miss it, since CancelledError is a BaseException, dropping the
    authorized attempt from the audit chain (backend-security-controls)."""
    import asyncio as _asyncio

    def _raise_cancel(*a, **k):
        raise _asyncio.CancelledError()

    with patch("kiro_crew.config.loader.update_config_locked", _raise_cancel):
        async with TestClient(TestServer(handler_app)) as client:
            try:
                await client.put("/api/dashboard/config", json={"use_builtin_browser": False})
            except Exception:
                pass  # cancellation propagates as a client-side disconnect/500
    calls = [
        (c.kwargs.get("outcome"), c.kwargs.get("error"))
        for c in mock_sel.log_tool_invocation.call_args_list
    ]
    assert ("failure", "request_cancelled") in calls, calls
