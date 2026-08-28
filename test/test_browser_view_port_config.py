"""Tests for the dashboard.browser_view_port config field (issue #6655).

The field pins the browser live-view server (``playwright-cli show``) to a fixed
loopback port so remote-gateway users can forward it through an SSH tunnel.
The tests pin the contract: it defaults to 0 (OS-assigned ephemeral, today's
behavior), round-trips through save/load, a malformed value falls back to 0
rather than crashing the load, out-of-range values fall back to unset (never
clamping into a pin the operator did not name), and the view-start handler
passes the configured pin through to
``ensure_running`` — with 0 mapped to ``None`` so the module keeps its
ephemeral default.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


def test_browser_view_port_default_is_ephemeral():
    """0 means "unset": the view keeps binding an OS-assigned ephemeral port."""
    assert KiroCrewConfig().dashboard.browser_view_port == 0


def test_browser_view_port_save_load(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.browser_view_port = 45613
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["browser_view_port"] == 45613
    assert KiroCrewConfig.load().dashboard.browser_view_port == 45613


def test_browser_view_port_absent_key_stays_zero(cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
    assert KiroCrewConfig.load().dashboard.browser_view_port == 0


def test_browser_view_port_malformed_falls_back_to_zero(cfg_file):
    """A broken value must never crash the load or invent a pin."""
    for bad in ("not-a-port", True, None, [8080], 1.5):
        cfg_file.write_text(
            json.dumps({"dashboard": {"browser_view_port": bad}}),
            encoding="utf-8",
        )
        assert KiroCrewConfig.load().dashboard.browser_view_port == 0, bad


def test_browser_view_port_out_of_range_falls_back_to_unset(cfg_file):
    """An out-of-range port must revert to unset, never clamp into a live pin:
    a tunnel that forwards 8080 does not forward 65535 either, so a clamped
    value is as wrong as a malformed one."""
    for raw, expected in ((-5, 0), (0, 0), (70000, 0), (65536, 0), ("8080", 8080), (65535, 65535)):
        cfg_file.write_text(
            json.dumps({"dashboard": {"browser_view_port": raw}}),
            encoding="utf-8",
        )
        assert KiroCrewConfig.load().dashboard.browser_view_port == expected, raw


# ── Handler end-to-end: the pin reaches ensure_running ──


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_api_access = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


@pytest.fixture()
def view_start_app(cfg_file, mock_sel):
    from kiro_crew.dashboard.handlers.messaging import api_browser_view_start

    app = web.Application()
    app.router.add_post("/api/browser/view/start", api_browser_view_start)
    return as_owner(app)


async def _post_start(app: web.Application) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/browser/view/start")
        assert resp.status == 200


@pytest.mark.asyncio
async def test_view_start_passes_the_configured_pin(cfg_file, view_start_app):
    """browser_view_port=N in config reaches ensure_running(N) end-to-end."""
    cfg_file.write_text(json.dumps({"dashboard": {"browser_view_port": 45613}}), encoding="utf-8")
    calls: list[int | None] = []
    with (
        patch(
            "kiro_crew.browser_cli.view.ensure_running",
            side_effect=lambda port=None: calls.append(port),
        ),
        patch(
            "kiro_crew.browser_cli.view.status",
            return_value={"status": "stopped", "url": None, "port": None, "reason": None},
        ),
    ):
        await _post_start(view_start_app)

    assert calls == [45613]


@pytest.mark.asyncio
async def test_view_start_unset_pin_maps_to_none(cfg_file, view_start_app):
    """0 (the default) must reach ensure_running as None, keeping ephemeral."""
    calls: list[int | None] = []
    with (
        patch(
            "kiro_crew.browser_cli.view.ensure_running",
            side_effect=lambda port=None: calls.append(port),
        ),
        patch(
            "kiro_crew.browser_cli.view.status",
            return_value={"status": "stopped", "url": None, "port": None, "reason": None},
        ),
    ):
        await _post_start(view_start_app)

    assert calls == [None]
