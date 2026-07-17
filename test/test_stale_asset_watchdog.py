"""Tests for the stale-asset watchdog (Mesh-2690)."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_watchdog_shuts_down_when_assets_vanish():
    """When assets stay missing through the confirmation re-check, shutdown fires."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()
    call_count = 0

    def _mock_assets_present() -> bool:
        nonlocal call_count
        call_count += 1
        # First call (startup check) → True; every later call (periodic tick
        # + confirmation re-check) → False, i.e. a permanent Toolbox prune.
        return call_count <= 1

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_mock_assets_present,
    ):
        # Short interval/delay so the test is fast
        await asyncio.wait_for(
            run_stale_asset_watchdog(shutdown, interval=0.05, confirm_delay=0.01),
            timeout=5.0,
        )

    assert shutdown.is_set()
    # startup check + periodic tick + confirmation re-check
    assert call_count == 3


@pytest.mark.asyncio
async def test_watchdog_survives_transient_asset_gap():
    """A brief asset gap (e.g. frontend rebuild) does NOT shut the gateway down."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()
    call_count = 0

    def _mock_assets_present() -> bool:
        nonlocal call_count
        call_count += 1
        # startup → True; periodic tick → False (rebuild deleted dist/);
        # confirmation re-check → True (rebuild finished). Then end the test
        # by setting shutdown externally, as a normal shutdown would.
        if call_count == 3:
            asyncio.get_running_loop().call_soon(shutdown.set)
        return call_count != 2

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_mock_assets_present,
    ):
        await asyncio.wait_for(
            run_stale_asset_watchdog(shutdown, interval=0.05, confirm_delay=0.01),
            timeout=5.0,
        )

    # Shutdown was set by the test (normal-shutdown path), not the watchdog:
    # the transient gap was re-checked, found recovered, and the loop resumed.
    assert call_count == 3


@pytest.mark.asyncio
async def test_watchdog_does_not_arm_when_assets_never_existed():
    """A dev install that never built its frontend is NOT killed."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        return_value=False,
    ):
        await asyncio.wait_for(
            run_stale_asset_watchdog(shutdown, interval=60),
            timeout=5.0,
        )

    # The watchdog returned without setting shutdown — it's not armed.
    assert not shutdown.is_set()


@pytest.mark.asyncio
async def test_watchdog_exits_cleanly_on_normal_shutdown():
    """If the shutdown event is set externally, the watchdog returns without error."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        return_value=True,
    ):
        # Set shutdown after a brief delay
        asyncio.get_running_loop().call_later(0.05, shutdown.set)
        await asyncio.wait_for(
            run_stale_asset_watchdog(shutdown, interval=60),
            timeout=5.0,
        )

    assert shutdown.is_set()


def test_assets_present_detects_dist_index(tmp_path: Path):
    """assets_present returns True when dist/index.html exists."""
    from kiro_crew.dashboard import stale_asset_watchdog as mod

    fake_dist_index = tmp_path / "dist" / "index.html"
    fake_dist_index.parent.mkdir()
    fake_dist_index.write_text("<!doctype html>")

    with patch.object(mod, "_DIST_INDEX", fake_dist_index):
        assert mod.assets_present() is True


def test_assets_present_false_without_dist_index(tmp_path: Path):
    """assets_present returns False when dist/index.html is absent.

    The legacy ``dashboard.html`` fallback was removed (Talos V2285871874), so
    the React bundle's ``dist/index.html`` is the sole presence criterion.
    """
    from kiro_crew.dashboard import stale_asset_watchdog as mod

    fake_dist_index = tmp_path / "dist" / "index.html"  # does not exist

    with patch.object(mod, "_DIST_INDEX", fake_dist_index):
        assert mod.assets_present() is False


def test_assets_present_false_when_dist_dir_is_empty(tmp_path: Path):
    """Empty dist/ directory (partial-prune state) is treated as absent.

    Regression guard: the watchdog's presence check must match the handler's
    serve criterion — an empty ``dist/`` node with no ``index.html`` still
    causes the handler to serve the "Dashboard HTML not found" guidance page.
    """
    from kiro_crew.dashboard import stale_asset_watchdog as mod

    empty_dist_dir = tmp_path / "dist"
    empty_dist_dir.mkdir()  # directory exists, but no index.html inside
    fake_dist_index = empty_dist_dir / "index.html"

    with patch.object(mod, "_DIST_INDEX", fake_dist_index):
        assert mod.assets_present() is False


# ── Token CLI probe tests (call the real helper, not a copy) ──


def test_token_probe_warns_on_stale_dashboard():
    """_probe_dashboard_health emits a warning when the marker is present."""
    from kiro_crew.cli_server import _probe_dashboard_health

    stale_body = b"<h1>Dashboard HTML not found</h1><p>some explanation</p>"
    mock_resp = MagicMock()
    mock_resp.read.return_value = stale_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    stderr_capture = io.StringIO()

    with patch("kiro_crew.cli_server.urllib.request.urlopen", return_value=mock_resp), \
         patch("sys.stderr", stderr_capture):
        _probe_dashboard_health(7777)

    assert "stale dashboard" in stderr_capture.getvalue()


def test_token_probe_silent_on_healthy_dashboard():
    """_probe_dashboard_health stays silent when dashboard is real."""
    from kiro_crew.cli_server import _probe_dashboard_health

    healthy_body = b"<!DOCTYPE html><html><head><title>KiroCrew</title></head></html>"
    mock_resp = MagicMock()
    mock_resp.read.return_value = healthy_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    stderr_capture = io.StringIO()

    with patch("kiro_crew.cli_server.urllib.request.urlopen", return_value=mock_resp), \
         patch("sys.stderr", stderr_capture):
        _probe_dashboard_health(7777)

    assert stderr_capture.getvalue() == ""


def test_token_probe_silent_on_network_error():
    """_probe_dashboard_health is silent when the GET fails."""
    from kiro_crew.cli_server import _probe_dashboard_health

    stderr_capture = io.StringIO()

    with patch("kiro_crew.cli_server.urllib.request.urlopen", side_effect=OSError("connection refused")), \
         patch("sys.stderr", stderr_capture):
        _probe_dashboard_health(7777)

    assert stderr_capture.getvalue() == ""
