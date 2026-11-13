"""Tests for the stale-asset watchdog."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import update_restart_ready
from kiro_crew.testing.wait import async_wait_until, await_until


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
        fired = await await_until(
            asyncio.ensure_future(
                run_stale_asset_watchdog(shutdown, interval=0.05, confirm_delay=0.01)
            ),
            what="watchdog completed",
        )

    # Shutdown was set by the test (normal-shutdown path), not the watchdog:
    # the transient gap was re-checked, found recovered, and the loop resumed.
    assert call_count == 3
    assert fired is False


@pytest.mark.asyncio
async def test_watchdog_does_not_arm_when_assets_never_existed():
    """A dev install that never built its frontend is NOT killed."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        return_value=False,
    ):
        await await_until(
            asyncio.ensure_future(run_stale_asset_watchdog(shutdown, interval=60)),
            what="watchdog completed (no-arm path)",
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
        fired = await await_until(
            asyncio.ensure_future(run_stale_asset_watchdog(shutdown, interval=60)),
            what="watchdog completed (normal shutdown)",
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

    with (
        patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp),
        patch("sys.stderr", stderr_capture),
    ):
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

    with (
        patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp),
        patch("sys.stderr", stderr_capture),
    ):
        _probe_dashboard_health(7777)

    assert stderr_capture.getvalue() == ""


def test_token_probe_silent_on_network_error():
    """_probe_dashboard_health is silent when the GET fails."""
    from kiro_crew.cli_server import _probe_dashboard_health

    stderr_capture = io.StringIO()

    with (
        patch("kiro_crew.cli_server.loopback_urlopen", side_effect=OSError("connection refused")),
        patch("sys.stderr", stderr_capture),
    ):
        _probe_dashboard_health(7777)

    assert stderr_capture.getvalue() == ""


# --- update stand-down ------------------------------------------------------------

_WATCHDOG_LOGGER = "kiro_crew.dashboard.stale_asset_watchdog"


def _gap(*, present_reads: int = 1):
    """``assets_present`` stand-in: present for the first reads, then gone."""
    calls = {"n": 0}

    def _assets_present() -> bool:
        calls["n"] += 1
        return calls["n"] <= present_reads

    return patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_assets_present,
    )


def _owner_is(reader):
    return patch("kiro_crew.update_ownership.current_owner", side_effect=reader)


async def _run(shutdown, **kwargs):
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    kwargs.setdefault("interval", 0.01)
    kwargs.setdefault("confirm_delay", 0.01)
    return await await_until(
        asyncio.ensure_future(run_stale_asset_watchdog(shutdown, **kwargs)),
        what="watchdog completed",
    )


def _records(caplog, *, level=None, text=None):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _WATCHDOG_LOGGER
        and (level is None or r.levelno == level)
        and (text is None or text in r.getMessage())
    ]


@pytest.mark.asyncio
async def test_watchdog_stands_down_while_an_update_step_owns_the_gap(caplog):
    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    asked = {"n": 0}

    def _owner():
        asked["n"] += 1
        if asked["n"] >= 4:
            asyncio.get_running_loop().call_soon(shutdown.set)
        return "the policy apply command"

    with _gap(), _owner_is(_owner):
        fired = await _run(shutdown)

    assert fired is False
    standing = _records(caplog, text="standing down")
    # Named, once per gap.
    assert len(standing) == 1 and "the policy apply command" in standing[0]
    assert not _records(caplog, level=logging.CRITICAL)


@pytest.mark.asyncio
async def test_a_step_live_at_arm_time_is_waited_out_then_the_watchdog_arms():
    """A boot-time update with the bundle missing is not read as a dev install."""
    shutdown = asyncio.Event()
    reads = {"n": 0}

    def _assets_present() -> bool:
        reads["n"] += 1
        # missing while the boot update runs, present once it finished, then
        # pruned for good with no owner.
        return reads["n"] in (3, 4)

    def _owner():
        return "the policy apply command" if reads["n"] < 3 else None

    with (
        patch(
            "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
            side_effect=_assets_present,
        ),
        _owner_is(_owner),
    ):
        await _watch_until_noted(shutdown)


@pytest.mark.asyncio
async def test_a_gap_a_boot_time_step_leaves_behind_arms_the_watchdog(caplog):
    """The step that owned the gap at startup ended with the bundle still gone.

    The update made that gap (a pruned versioned tree, a restart that found no
    interpreter), so it is not a dev install: the watchdog must arm and say so,
    instead of staying silent on pruned code for good.
    """
    caplog.set_level(logging.INFO, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    owner_reads = {"n": 0}

    def _owner():
        owner_reads["n"] += 1
        return "the policy apply command" if owner_reads["n"] <= 2 else None

    with (
        patch(
            "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
            return_value=False,
        ),
        _owner_is(_owner),
    ):
        await _watch_until_noted(shutdown)

    assert not _records(caplog, text="not arming")


@pytest.mark.asyncio
async def test_a_restart_into_an_update_is_not_raced(caplog):
    """The real registry: a step, then a restart whose teardown takes a while."""
    from kiro_crew import update_ownership

    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    teardown_seen = asyncio.Event()

    @update_ownership.owning(update_ownership.Step.RESTART)
    async def _restart():
        # The teardown: wait until the watchdog has looked at the gap at least once.
        await await_until(teardown_seen, what="watchdog saw teardown")
        shutdown.set()  # stands for the exec

    async def _apply_then_restart():
        with update_ownership.step(update_ownership.Step.POLICY_APPLY):
            await asyncio.sleep(0)
        await _restart()

    reads = {"n": 0}
    real_owner = update_ownership.current_owner

    def _owner():
        reads["n"] += 1
        owner = real_owner()
        if owner == "the restart into an applied update" and reads["n"] >= 3:
            teardown_seen.set()
        return owner

    with _gap(), _owner_is(_owner):
        watchdog = asyncio.ensure_future(_run(shutdown))
        await await_until(
            asyncio.ensure_future(_apply_then_restart()), what="apply_then_restart done"
        )
        fired = await watchdog

    assert fired is False
    assert not _records(caplog, level=logging.CRITICAL)


@pytest.mark.asyncio
async def test_a_step_wedged_past_its_maximum_no_longer_holds_the_watchdog(caplog):
    """A stuck restart drain cannot keep a gateway with no bundle silent for good."""
    import time

    from kiro_crew import update_ownership

    caplog.set_level(logging.WARNING, logger="kiro_crew.update_ownership")
    shutdown = asyncio.Event()
    entered, release = asyncio.Event(), asyncio.Event()

    async def _wedged_restart():
        with update_ownership.step(update_ownership.Step.RESTART):
            # Scaled down: the entry's maximum runs out almost at once.
            update_ownership._live[-1].deadline = time.monotonic() + 0.05
            entered.set()
            await release.wait()

    restart = asyncio.ensure_future(_wedged_restart())
    try:
        await await_until(entered, what="wedged restart entered")
        with _gap():
            await _watch_until_noted(shutdown)
    finally:
        release.set()
        await restart

    assert [
        r
        for r in caplog.records
        if r.name == "kiro_crew.update_ownership" and "maximum" in r.getMessage()
    ]


# --- the watchdog never exits the gateway -----------------------------------------


async def _watch_until_noted(shutdown, **kwargs):
    """Run the watchdog until it records the restart notice, then stop it as an operator would."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    kwargs.setdefault("interval", 0.01)
    kwargs.setdefault("confirm_delay", 0.01)
    task = asyncio.ensure_future(run_stale_asset_watchdog(shutdown, **kwargs))
    try:
        await async_wait_until(
            lambda: update_restart_ready.current() is not None,
            describe=lambda: "restart notice recorded",
        )
    finally:
        shutdown.set()
        assert await await_until(task, what="watchdog stopped") is False


@pytest.mark.asyncio
async def test_a_vanished_bundle_is_reported_and_the_gateway_keeps_serving(caplog):
    """A confirmed vanish records the notice and exits nothing."""
    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    changed: list[int] = []

    with _gap():
        await _watch_until_noted(shutdown, on_change=lambda: changed.append(1))

    note = update_restart_ready.current()
    assert note is not None and note.reason == update_restart_ready.REASON_ASSETS_GONE
    assert changed == [1]
    assert len(_records(caplog, level=logging.CRITICAL, text="keeps serving")) == 1


@pytest.mark.asyncio
async def test_the_watchdog_never_sets_the_shutdown_event_itself():
    """Watched across many ticks of a permanent vanish, the event stays clear."""
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    shutdown = asyncio.Event()
    reads = {"n": 0}

    def _present() -> bool:
        reads["n"] += 1
        return reads["n"] == 1

    with patch("kiro_crew.dashboard.stale_asset_watchdog.assets_present", side_effect=_present):
        task = asyncio.ensure_future(
            run_stale_asset_watchdog(shutdown, interval=0.01, confirm_delay=0.01)
        )
        await async_wait_until(
            lambda: reads["n"] >= 12, describe=lambda: "a dozen ticks of the vanish"
        )
        assert not shutdown.is_set()
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_the_notice_is_said_once_and_cleared_when_the_bundle_returns(caplog):
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    caplog.set_level(logging.INFO, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    state = {"present": True, "reads": 0}
    changed: list[int] = []

    def _present() -> bool:
        state["reads"] += 1
        return state["present"]

    with patch("kiro_crew.dashboard.stale_asset_watchdog.assets_present", side_effect=_present):
        task = asyncio.ensure_future(
            run_stale_asset_watchdog(
                shutdown, interval=0.01, confirm_delay=0.01, on_change=lambda: changed.append(1)
            )
        )
        # Armed: it has read the bundle at startup and at least once on a tick.
        await async_wait_until(lambda: state["reads"] >= 2, describe=lambda: "watchdog armed")
        state["present"] = False
        await async_wait_until(
            lambda: update_restart_ready.current() is not None, describe=lambda: "notice recorded"
        )
        state["present"] = True
        await async_wait_until(
            lambda: update_restart_ready.current() is None, describe=lambda: "notice cleared"
        )
        shutdown.set()
        await await_until(task, what="watchdog stopped")

    assert changed == [1, 1]
    assert len(_records(caplog, level=logging.CRITICAL)) == 1


@pytest.mark.asyncio
async def test_a_notice_another_source_recorded_is_not_cleared_by_a_healthy_bundle():
    from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog

    update_restart_ready.mark(update_restart_ready.REASON_STAGED, version="9.9.9")
    shutdown = asyncio.Event()
    reads = {"n": 0}

    def _present() -> bool:
        reads["n"] += 1
        return True

    with patch("kiro_crew.dashboard.stale_asset_watchdog.assets_present", side_effect=_present):
        task = asyncio.ensure_future(run_stale_asset_watchdog(shutdown, interval=0.01))
        await async_wait_until(lambda: reads["n"] >= 5, describe=lambda: "healthy ticks")
        shutdown.set()
        await await_until(task, what="watchdog stopped")

    note = update_restart_ready.current()
    assert note is not None and note.reason == update_restart_ready.REASON_STAGED


# --- the operator's auto-restart opt-out ------------------------------------------


@pytest.fixture
def _no_state_dir(monkeypatch):
    from kiro_crew.dashboard import stale_asset_watchdog as wd

    monkeypatch.setattr(wd, "_auto_restart_state_dir", None)
    monkeypatch.delenv(wd.AUTO_RESTART_ENV, raising=False)
    return wd


def test_auto_restart_is_on_by_default(_no_state_dir):
    assert _no_state_dir.auto_restart_disabled() is False


@pytest.mark.parametrize(
    ("value", "disabled"), [("0", True), (" 0 ", True), ("1", False), ("", False)]
)
def test_the_environment_variable_turns_auto_restart_off_only_at_zero(
    _no_state_dir, monkeypatch, value, disabled
):
    monkeypatch.setenv(_no_state_dir.AUTO_RESTART_ENV, value)
    assert _no_state_dir.auto_restart_disabled() is disabled


def test_the_disabled_file_in_the_edition_state_dir_turns_auto_restart_off(_no_state_dir, tmp_path):
    _no_state_dir.set_auto_restart_state_dir(tmp_path)
    assert _no_state_dir.auto_restart_disabled() is False
    (tmp_path / _no_state_dir.AUTO_RESTART_OFF_FILE).write_text("")
    assert _no_state_dir.auto_restart_disabled() is True
    _no_state_dir.set_auto_restart_state_dir(None)
    assert _no_state_dir.auto_restart_disabled() is False


def test_an_unreadable_state_dir_is_not_an_opt_out(_no_state_dir, monkeypatch, tmp_path):
    _no_state_dir.set_auto_restart_state_dir(tmp_path)

    def _boom(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "exists", _boom)
    assert _no_state_dir.auto_restart_disabled() is False


@pytest.mark.asyncio
async def test_the_report_says_when_automatic_restart_is_turned_off(
    _no_state_dir, monkeypatch, caplog
):
    caplog.set_level(logging.CRITICAL, logger=_WATCHDOG_LOGGER)
    monkeypatch.setenv(_no_state_dir.AUTO_RESTART_ENV, "0")
    shutdown = asyncio.Event()
    with _gap():
        await _watch_until_noted(shutdown)
    [line] = _records(caplog, level=logging.CRITICAL)
    assert "Automatic restart is turned off" in line
