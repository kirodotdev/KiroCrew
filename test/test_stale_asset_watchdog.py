"""Tests for the stale-asset watchdog."""

from __future__ import annotations

import asyncio
import io
import logging
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
        fired = await asyncio.wait_for(
            run_stale_asset_watchdog(shutdown, interval=0.05, confirm_delay=0.01),
            timeout=5.0,
        )

    assert shutdown.is_set()
    # The watchdog reports that IT initiated the shutdown, so the gateway can
    # exit non-zero and a restart-on-failure supervisor relaunches it.
    assert fired is True
    # startup check + periodic tick + confirmation re-check + post-drain re-check
    assert call_count == 4


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
        fired = await asyncio.wait_for(
            run_stale_asset_watchdog(shutdown, interval=0.05, confirm_delay=0.01),
            timeout=5.0,
        )

    # Shutdown was set by the test (normal-shutdown path), not the watchdog:
    # the transient gap was re-checked, found recovered, and the loop resumed.
    assert call_count == 3
    assert fired is False


@pytest.mark.asyncio
async def test_watchdog_drains_in_flight_before_shutdown():
    """A vanish waits for in-flight work to finish before setting shutdown."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()
    call_count = 0

    def _mock_assets_present() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count <= 1  # startup True, then permanent vanish

    # Two in-flight tasks that clear after the first drain poll.
    pending = {"n": 2}
    poll_calls = {"n": 0}

    def _count() -> int:
        poll_calls["n"] += 1
        if poll_calls["n"] >= 2:
            pending["n"] = 0
        return pending["n"]

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_mock_assets_present,
    ):
        await asyncio.wait_for(
            run_stale_asset_watchdog(
                shutdown,
                interval=0.05,
                confirm_delay=0.01,
                count_in_flight=_count,
                drain_timeout=5.0,
                drain_poll=0.01,
            ),
            timeout=5.0,
        )

    # Shutdown still fired (the prune is permanent) but only after the drain
    # observed the in-flight work reach zero.
    assert shutdown.is_set()
    assert poll_calls["n"] >= 2


@pytest.mark.asyncio
async def test_watchdog_drain_respects_timeout():
    """If in-flight work never clears, shutdown still fires after the timeout."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()
    call_count = 0

    def _mock_assets_present() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count <= 1

    # Work that never drains — must not defer shutdown past the timeout.
    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_mock_assets_present,
    ):
        await asyncio.wait_for(
            run_stale_asset_watchdog(
                shutdown,
                interval=0.05,
                confirm_delay=0.01,
                count_in_flight=lambda: 3,  # permanently busy
                drain_timeout=0.1,
                drain_poll=0.02,
            ),
            timeout=5.0,
        )

    assert shutdown.is_set()


@pytest.mark.asyncio
async def test_watchdog_no_drain_when_no_work():
    """With zero in-flight work, shutdown fires immediately (no drain wait)."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()
    call_count = 0

    def _mock_assets_present() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count <= 1

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_mock_assets_present,
    ):
        await asyncio.wait_for(
            run_stale_asset_watchdog(
                shutdown,
                interval=0.05,
                confirm_delay=0.01,
                count_in_flight=lambda: 0,
                drain_timeout=30.0,  # large: would hang the test if drained
                drain_poll=0.01,
            ),
            timeout=5.0,
        )

    assert shutdown.is_set()


@pytest.mark.asyncio
async def test_watchdog_drain_predicate_failure_does_not_block_shutdown():
    """A broken count_in_flight predicate must not wedge shutdown."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()
    call_count = 0

    def _mock_assets_present() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count <= 1

    def _boom() -> int:
        raise RuntimeError("predicate exploded")

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_mock_assets_present,
    ):
        await asyncio.wait_for(
            run_stale_asset_watchdog(
                shutdown,
                interval=0.05,
                confirm_delay=0.01,
                count_in_flight=_boom,
                drain_timeout=30.0,
                drain_poll=0.01,
            ),
            timeout=5.0,
        )

    assert shutdown.is_set()


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
        fired = await asyncio.wait_for(
            run_stale_asset_watchdog(shutdown, interval=60),
            timeout=5.0,
        )

    assert shutdown.is_set()
    # An operator stop is NOT the watchdog's doing: the gateway must exit 0 so
    # a restart-on-failure supervisor leaves it stopped as asked.
    assert fired is False


@pytest.mark.asyncio
async def test_shutdown_exit_code_nonzero_only_when_watchdog_fired():
    """The gateway exits non-zero iff the watchdog itself initiated shutdown.

    This is the belt-and-braces for a supervisor still on `Restart=on-failure`
    (a unit generated before `Restart=always`, or hand-edited back): a clean
    exit 0 after an update prune left the gateway stranded, so the watchdog
    path must NOT exit 0. Every other outcome must stay 0 so an operator's
    `systemctl stop` is not turned into a restart.
    """
    from kiro_crew.dashboard.stale_asset_watchdog import (
        STALE_ASSET_EXIT_CODE,
        shutdown_exit_code,
    )

    assert STALE_ASSET_EXIT_CODE != 0

    loop = asyncio.get_running_loop()

    fired: asyncio.Future[bool] = loop.create_future()
    fired.set_result(True)
    assert shutdown_exit_code(fired) == STALE_ASSET_EXIT_CODE

    not_fired: asyncio.Future[bool] = loop.create_future()
    not_fired.set_result(False)
    assert shutdown_exit_code(not_fired) == 0

    # Event set by SIGTERM while the watchdog still sleeps: not done → 0.
    pending: asyncio.Future[bool] = loop.create_future()
    assert shutdown_exit_code(pending) == 0

    cancelled: asyncio.Future[bool] = loop.create_future()
    cancelled.cancel()
    assert shutdown_exit_code(cancelled) == 0

    # A crashed watchdog must not convert an operator stop into a restart.
    crashed: asyncio.Future[bool] = loop.create_future()
    crashed.set_exception(RuntimeError("boom"))
    assert shutdown_exit_code(crashed) == 0

    assert shutdown_exit_code(None) == 0


@pytest.mark.asyncio
async def test_real_vanish_task_maps_to_nonzero_exit():
    """End to end through the task: a real vanish → STALE_ASSET_EXIT_CODE."""
    from kiro_crew.dashboard.stale_asset_watchdog import (
        STALE_ASSET_EXIT_CODE,
        run_stale_asset_watchdog,
        shutdown_exit_code,
    )

    shutdown = asyncio.Event()
    call_count = 0

    def _mock_assets_present() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count <= 1

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_mock_assets_present,
    ):
        task = asyncio.create_task(
            run_stale_asset_watchdog(shutdown, interval=0.05, confirm_delay=0.01)
        )
        await asyncio.wait_for(shutdown.wait(), timeout=5.0)
        # Mirrors the gateway: the watchdog returns right after setting the
        # event, so by the time the gateway reads the task it is done.
        await asyncio.wait_for(task, timeout=1.0)

    assert shutdown_exit_code(task) == STALE_ASSET_EXIT_CODE


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

    The legacy ``dashboard.html`` fallback was removed (security-review), so
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

    with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp), \
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

    with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp), \
         patch("sys.stderr", stderr_capture):
        _probe_dashboard_health(7777)

    assert stderr_capture.getvalue() == ""


def test_token_probe_silent_on_network_error():
    """_probe_dashboard_health is silent when the GET fails."""
    from kiro_crew.cli_server import _probe_dashboard_health

    stderr_capture = io.StringIO()

    with patch("kiro_crew.cli_server.loopback_urlopen", side_effect=OSError("connection refused")), \
         patch("sys.stderr", stderr_capture):
        _probe_dashboard_health(7777)

    assert stderr_capture.getvalue() == ""


@pytest.mark.asyncio
async def test_watchdog_survives_asset_gap_that_heals_while_draining(caplog):
    """A rebuild that heals while in-flight turns drain must NOT shut down.

    The gap outlives the confirmation, so only the post-drain re-check can save
    the gateway. ``count_in_flight`` reports real pending work here so the drain
    actually spans the gap — with ``None`` the drain returns immediately and
    this path proves nothing.
    """
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    caplog.set_level(logging.CRITICAL, logger="kiro_crew.dashboard.stale_asset_watchdog")

    shutdown = asyncio.Event()
    checks = 0
    assets = True
    pending = 2

    def _mock_assets_present() -> bool:
        nonlocal checks
        checks += 1
        # startup sees assets; the tick and the confirmation both miss them
        # (rebuild still running); anything later sees them restored.
        if checks == 2 or checks == 3:
            return False
        if checks >= 5:
            asyncio.get_running_loop().call_soon(shutdown.set)
        return assets

    def _count_in_flight() -> int:
        # Each poll retires one turn; the rebuild completes as the last one
        # does, so the post-drain re-check is the first check to see assets.
        nonlocal pending, assets
        pending = max(0, pending - 1)
        if pending == 0:
            assets = True
        return pending

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_mock_assets_present,
    ):
        await asyncio.wait_for(
            run_stale_asset_watchdog(
                shutdown,
                interval=0.05,
                confirm_delay=0.01,
                count_in_flight=_count_in_flight,
                drain_timeout=5.0,
                drain_poll=0.01,
            ),
            timeout=5.0,
        )

    assert pending == 0, "the drain must have actually run"
    # Reaching a 5th check proves the watchdog resumed its loop instead of
    # firing: shutdown came from the test, not the watchdog.
    assert checks >= 5
    # A run that heals must not have announced a shutdown it then abandoned:
    # the CRITICAL belongs after the post-drain re-check, not before the drain.
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL], (
        "healed run logged a misleading graceful-shutdown CRITICAL"
    )
