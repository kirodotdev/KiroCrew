"""Tests for the stale-asset watchdog."""

from __future__ import annotations

import asyncio
import io
import logging
import time
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
    return await asyncio.wait_for(run_stale_asset_watchdog(shutdown, **kwargs), timeout=5.0)


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
@pytest.mark.parametrize("drains", [False, True], ids=["confirm-window", "drain-window"])
async def test_a_step_starting_before_the_signal_still_stands_down(caplog, drains):
    """The last owner check, after the confirm AND after the drain, sees it."""
    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    state = {"owner": None, "reads": 0}
    pending = {"n": 1}

    def _count() -> int:
        n, pending["n"] = pending["n"], 0
        if n == 0:
            state["owner"] = "the dashboard update"
        return n

    def _owner():
        state["reads"] += 1
        if not drains and state["reads"] == 1:
            # The tick's own read finds nothing; the step starts during the confirm.
            state["owner"] = "the dashboard update"
            return None
        if state["owner"] is not None:
            asyncio.get_running_loop().call_soon(shutdown.set)
        return state["owner"]

    with _gap(), _owner_is(_owner):
        fired = await _run(
            shutdown,
            count_in_flight=_count if drains else None,
            drain_timeout=5.0,
            drain_poll=0.01,
        )

    assert fired is False
    if drains:
        assert pending["n"] == 0, "the drain must have run"
    # Even after the drain logged its way to "shutdown", the stand-down is said.
    assert _records(caplog, text="standing down after drain")


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

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        side_effect=_assets_present,
    ), _owner_is(_owner):
        fired = await _run(shutdown)

    assert fired is True


@pytest.mark.asyncio
async def test_a_gap_a_boot_time_step_leaves_behind_arms_the_watchdog(caplog):
    """The step that owned the gap at startup ended with the bundle still gone.

    The update made that gap (a pruned versioned tree, a restart that found no
    interpreter), so it is not a dev install: the watchdog must arm and exit so
    the supervisor relaunches, instead of staying up on pruned code for good.
    """
    caplog.set_level(logging.INFO, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    owner_reads = {"n": 0}

    def _owner():
        owner_reads["n"] += 1
        return "the policy apply command" if owner_reads["n"] <= 2 else None

    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
        return_value=False,
    ), _owner_is(_owner):
        fired = await _run(shutdown)

    assert fired is True
    assert shutdown.is_set()
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
        await asyncio.wait_for(teardown_seen.wait(), timeout=5.0)
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
        await asyncio.wait_for(_apply_then_restart(), timeout=5.0)
        fired = await watchdog

    assert fired is False
    assert not _records(caplog, level=logging.CRITICAL)


@pytest.mark.asyncio
async def test_a_step_wedged_past_its_maximum_no_longer_holds_the_watchdog(caplog):
    """A stuck restart drain cannot keep a gateway with no bundle up for good."""
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
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        with _gap():
            fired = await _run(shutdown)
    finally:
        release.set()
        await restart

    assert fired is True
    assert [
        r for r in caplog.records if r.name == "kiro_crew.update_ownership" and "maximum" in r.getMessage()
    ]


# --- the re-entry check -----------------------------------------------------------


@pytest.fixture
def managed(monkeypatch):
    """A gateway a generated service definition launched."""
    monkeypatch.setenv("KIROCREW_SERVICE_MANAGED", "1")


def _verdict(status, reason="", *, probed=False):
    from kiro_crew.update_ownership import Reentry, ReentryVerdict

    return ReentryVerdict(getattr(Reentry, status), reason, probed=probed)


def _assert_backoff_doubles(asked_at: list[float], interval: float) -> None:
    """Each re-ask waited at least its backoff step: interval, then doubling.

    The watchdog sets the next ask to ``loop.time() + backoff`` after a check
    returns, and the asks are timed on that same clock, so every gap is at least
    its step however coarse the clock is or however late a tick wakes. Two
    measured gaps are not compared with each other: on a 15.6 ms clock a late
    tick turns the 20 ms step into the same reading as the 40 ms one.
    """
    gaps = [b - a for a, b in zip(asked_at[1:], asked_at[2:])]
    steps = [interval * 2 ** (k + 1) for k in range(len(gaps))]
    assert gaps and all(gap >= step - 1e-9 for gap, step in zip(gaps, steps)), (gaps, steps)


async def _run_until(shutdown, done: asyncio.Event, **kwargs):
    """Run the watchdog until *done* fires (or it returns), then stop it like SIGTERM."""
    task = asyncio.ensure_future(_run(shutdown, **kwargs))
    fired = asyncio.ensure_future(done.wait())
    try:
        await asyncio.wait_for(
            asyncio.wait({task, fired}, return_when="FIRST_COMPLETED"), timeout=5.0
        )
    finally:
        shutdown.set()
        fired.cancel()
        await asyncio.gather(fired, return_exceptions=True)
    return await task


@pytest.mark.asyncio
async def test_a_refused_relaunch_stays_up_says_so_once_and_backs_off(caplog):
    """Asked again on a doubling backoff, without the confirm and drain each tick."""
    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    asked_at: list[float] = []
    drains = {"n": 0}
    enough = asyncio.Event()

    def _check():
        asked_at.append(loop.time())
        if len(asked_at) >= 5:
            loop.call_soon_threadsafe(enough.set)
        return _verdict("REFUSED", "its supervisor's command (/old/kirocrew) is missing")

    def _count() -> int:
        drains["n"] += 1
        return 0

    with _gap():
        fired = await _run_until(
            shutdown, enough, reentry_check=_check, count_in_flight=_count, interval=0.01
        )

    assert fired is False
    criticals = _records(caplog, level=logging.CRITICAL)
    assert len(criticals) == 1 and "could not relaunch" in criticals[0]
    # One confirm-and-drain pass (the drain is counted again after the check),
    # then only the backoff's re-asks.
    assert drains["n"] == 2
    _assert_backoff_doubles(asked_at, interval=0.01)


@pytest.mark.asyncio
async def test_an_inconclusive_recheck_does_not_lift_a_refusal(caplog):
    """A flapping check: one CRITICAL, one drain, and the backoff keeps growing."""
    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    asked_at: list[float] = []
    drains = {"n": 0}
    enough = asyncio.Event()

    def _check():
        asked_at.append(loop.time())
        if len(asked_at) >= 6:
            loop.call_soon_threadsafe(enough.set)
        if len(asked_at) % 2:
            return _verdict("REFUSED", "its supervisor's command (/old/kirocrew) is missing")
        raise OSError("stalled mount")

    def _count() -> int:
        drains["n"] += 1
        return 0

    with _gap():
        fired = await _run_until(
            shutdown, enough, reentry_check=_check, count_in_flight=_count, interval=0.01
        )

    assert fired is False
    assert len(_records(caplog, level=logging.CRITICAL)) == 1
    assert drains["n"] == 2
    _assert_backoff_doubles(asked_at, interval=0.01)


@pytest.mark.asyncio
async def test_an_inconclusive_check_is_not_a_refusal():
    shutdown = asyncio.Event()

    def _boom():
        raise OSError("stalled mount")

    with _gap():
        assert await _run(shutdown, reentry_check=_boom) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["an-owner-registers", "the-bundle-returns"])
async def test_what_happens_during_the_check_is_seen_before_the_signal(caplog, change):
    """The check is the last await: the reads after it see a change made during it."""
    from kiro_crew import update_ownership

    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    state = {"present": False, "reads": 0}
    registered = asyncio.Event()

    def _assets_present() -> bool:
        state["reads"] += 1
        return state["reads"] == 1 or state["present"]

    def _check():
        # Runs on the check's worker while the loop keeps going.
        if change == "the-bundle-returns":
            state["present"] = True
        else:
            loop.call_soon_threadsafe(registered.set)
        return _verdict("REENTERABLE")

    async def _step_during_the_check():
        await registered.wait()
        with update_ownership.step(update_ownership.Step.POLICY_APPLY):
            await asyncio.sleep(0.2)
            shutdown.set()

    step = asyncio.ensure_future(_step_during_the_check())
    with patch(
        "kiro_crew.dashboard.stale_asset_watchdog.assets_present", side_effect=_assets_present
    ):
        task = asyncio.ensure_future(_run(shutdown, reentry_check=_check))
        if change == "the-bundle-returns":
            await asyncio.sleep(0.2)
            shutdown.set()
        fired = await task
    registered.set()
    await step

    assert fired is False
    assert not _records(caplog, level=logging.CRITICAL)


@pytest.mark.asyncio
async def test_an_operator_stop_during_the_check_is_not_reported_as_a_vanish(caplog):
    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _check():
        loop.call_soon_threadsafe(shutdown.set)  # SIGTERM lands during the check
        return _verdict("REENTERABLE")

    with _gap():
        fired = await _run(shutdown, reentry_check=_check)

    assert fired is False
    assert not _records(caplog, level=logging.CRITICAL)


@pytest.mark.asyncio
async def test_a_check_that_hangs_never_stacks_another_thread(monkeypatch):
    import threading

    from kiro_crew.dashboard import stale_asset_watchdog

    monkeypatch.setattr(stale_asset_watchdog, "_REENTRY_CHECK_TIMEOUT_SECS", 0.05)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls = {"n": 0}
    hang = threading.Event()
    waited = asyncio.Event()
    timeouts = {"n": 0}
    real_wait_for = asyncio.wait_for

    def _check():
        calls["n"] += 1
        if calls["n"] == 1:
            return _verdict("REFUSED", "its supervisor's command (/x) is missing")
        hang.wait(5)
        return _verdict("REFUSED", "its supervisor's command (/x) is missing")

    async def _counting_wait_for(aw, timeout):
        try:
            return await real_wait_for(aw, timeout)
        except asyncio.TimeoutError:
            if timeout == stale_asset_watchdog._REENTRY_CHECK_TIMEOUT_SECS:
                # Only the re-entry check's own bound counts.
                timeouts["n"] += 1
                if timeouts["n"] >= 3:
                    loop.call_soon(waited.set)
            raise

    monkeypatch.setattr(stale_asset_watchdog.asyncio, "wait_for", _counting_wait_for)
    try:
        with _gap():
            fired = await _run_until(shutdown, waited, reentry_check=_check, interval=0.01)
    finally:
        hang.set()

    assert fired is False
    # Several timed-out waits on one hung check: no second check ran beside it.
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_an_inconclusive_check_says_why_before_the_shutdown(caplog):
    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()

    def _boom():
        raise OSError("stalled mount")

    with _gap():
        assert await _run(shutdown, reentry_check=_boom) is True

    assert _records(caplog, level=logging.WARNING, text="stalled mount")


@pytest.mark.asyncio
async def test_an_update_that_stayed_up_keeps_a_managed_gateway_up_without_a_check(caplog, managed):
    from kiro_crew import update_ownership

    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    update_ownership.refuse_restart("the dependency sync did not complete")
    enough = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, enough.set)
    with _gap():
        fired = await _run_until(shutdown, enough, reentry_check=None)

    assert fired is False
    assert _records(caplog, level=logging.CRITICAL, text="an update stopped before restart")


@pytest.mark.asyncio
@pytest.mark.parametrize("standing", [False, True], ids=["first-gap", "lifting-a-refusal"])
async def test_a_refusal_recorded_during_the_drain_is_seen_before_the_signal(caplog, standing, managed):
    """Read with no await before the signal, even right after a refusal was lifted."""
    from kiro_crew import update_ownership

    caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
    shutdown = asyncio.Event()
    asks = {"n": 0}
    counts = {"n": 0}
    enough = asyncio.Event()

    def _check():
        asks["n"] += 1
        if standing and asks["n"] == 1:
            return _verdict("REFUSED", "its supervisor's command (/x) is missing")
        return _verdict("REENTERABLE")

    def _count() -> int:
        counts["n"] += 1
        if counts["n"] == (3 if standing else 1):
            # An update that will stay up ends while this pass drains.
            update_ownership.refuse_restart("the dependency sync did not complete")
            asyncio.get_running_loop().call_later(0.2, enough.set)
            return 1
        return 0

    with _gap():
        fired = await _run_until(
            shutdown, enough, reentry_check=_check, count_in_flight=_count, drain_poll=0.01
        )

    assert fired is False
    assert _records(caplog, level=logging.CRITICAL, text="an update stopped before restart")


@pytest.mark.asyncio
async def test_without_a_service_manager_a_stay_up_refuses_nothing(monkeypatch):
    """The exit behaves as before when nothing relaunches through a service manager."""
    from kiro_crew import update_ownership

    monkeypatch.delenv("KIROCREW_SERVICE_MANAGED", raising=False)
    shutdown = asyncio.Event()
    update_ownership.refuse_restart("the dependency sync did not complete")
    with _gap():
        assert await _run(shutdown, reentry_check=None) is True


@pytest.mark.asyncio
async def test_a_probe_that_proves_the_relaunch_clears_an_earlier_stay_up(managed):
    """An install repaired out of band: fresher evidence than the recorded refusal."""
    from kiro_crew import update_ownership

    shutdown = asyncio.Event()
    update_ownership.refuse_restart("the dependency sync did not complete")
    with _gap():
        fired = await _run(shutdown, reentry_check=lambda: _verdict("REENTERABLE", probed=True))

    assert fired is True
    assert update_ownership.restart_refusal() is None


@pytest.mark.asyncio
async def test_a_reentry_with_nothing_probed_does_not_clear_a_stay_up(managed):
    from kiro_crew import update_ownership

    shutdown = asyncio.Event()
    update_ownership.refuse_restart("the dependency sync did not complete")
    enough = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, enough.set)
    with _gap():
        fired = await _run_until(shutdown, enough, reentry_check=lambda: _verdict("REENTERABLE"))

    assert fired is False
    assert update_ownership.restart_refusal() == "the dependency sync did not complete"


@pytest.mark.asyncio
async def test_a_stay_up_recorded_while_the_probe_ran_outlives_its_answer(managed):
    """The probe began before the refusal, so it cannot speak for the state after it."""
    from kiro_crew import update_ownership

    shutdown = asyncio.Event()
    enough = asyncio.Event()
    loop = asyncio.get_running_loop()
    asks = {"n": 0}

    def _check():
        asks["n"] += 1
        if asks["n"] > 1:
            # Later asks cannot tell, so they neither lift nor clear anything.
            return _verdict("INCONCLUSIVE", "the check did not answer")
        loop.call_soon_threadsafe(
            update_ownership.refuse_restart, "the dependency sync did not complete"
        )
        loop.call_soon_threadsafe(loop.call_later, 0.3, enough.set)
        time.sleep(0.05)
        return _verdict("REENTERABLE", probed=True)

    with _gap():
        fired = await _run_until(shutdown, enough, reentry_check=_check)

    assert fired is False
    assert update_ownership.restart_refusal() == "the dependency sync did not complete"


@pytest.mark.asyncio
async def test_a_turn_admitted_during_the_check_is_drained_before_the_signal():
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    busy = {"turns": 0}
    seen_at_signal = []

    def _check():
        # A message lands while the check runs; its turn ends shortly after.
        loop.call_soon_threadsafe(busy.__setitem__, "turns", 1)
        loop.call_soon_threadsafe(loop.call_later, 0.05, busy.__setitem__, "turns", 0)
        return _verdict("REENTERABLE")

    real_set = shutdown.set

    def _set():
        seen_at_signal.append(busy["turns"])
        real_set()

    shutdown.set = _set
    with _gap():
        fired = await _run(
            shutdown,
            reentry_check=_check,
            count_in_flight=lambda: busy["turns"],
            drain_poll=0.01,
        )

    assert fired is True
    assert seen_at_signal == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reentry_check", [None, lambda: _verdict("REENTERABLE")], ids=["no-check", "check"])
async def test_a_wedged_turn_holds_the_signal_for_one_drain_budget_not_two(monkeypatch, reentry_check):
    """The drain after the check gets what the first drain left, nothing more."""
    from kiro_crew.dashboard import stale_asset_watchdog

    budgets = []
    real_drain = stale_asset_watchdog._drain_in_flight

    async def _spy(*args, drain_timeout, **kwargs):
        budgets.append(drain_timeout)
        await real_drain(*args, drain_timeout=drain_timeout, **kwargs)

    monkeypatch.setattr(stale_asset_watchdog, "_drain_in_flight", _spy)
    shutdown = asyncio.Event()
    with _gap():
        fired = await _run(
            shutdown,
            reentry_check=reentry_check,
            count_in_flight=lambda: 1,  # a turn that never ends
            drain_timeout=0.2,
            drain_poll=0.01,
        )

    assert fired is True
    # The first drain ran its whole budget out, so the second has none left.
    assert budgets == [0.2, 0.0]


@pytest.mark.asyncio
async def test_a_check_that_cannot_get_a_thread_is_inconclusive_not_fatal(monkeypatch):
    """A refused thread start must not end the watchdog for the life of the process."""
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _no_thread(*_a, **_k):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(loop, "run_in_executor", _no_thread)
    with _gap():
        assert await _run(shutdown, reentry_check=lambda: _verdict("REFUSED", "x")) is True


@pytest.mark.asyncio
async def test_an_answer_that_lands_after_its_bound_is_still_read(monkeypatch):
    """A slow REENTERABLE lifts a standing refusal instead of being dropped unread."""
    import threading

    from kiro_crew.dashboard import stale_asset_watchdog

    monkeypatch.setattr(stale_asset_watchdog, "_REENTRY_CHECK_TIMEOUT_SECS", 0.05)
    shutdown = asyncio.Event()
    calls = {"n": 0}

    def _check():
        calls["n"] += 1
        if calls["n"] == 1:
            return _verdict("REFUSED", "its supervisor's command (/x) is missing")
        if calls["n"] == 2:
            threading.Event().wait(0.15)  # past the bound, then the install is repaired
        return _verdict("REENTERABLE")

    with _gap():
        fired = await _run(shutdown, reentry_check=_check, interval=0.02)

    assert fired is True
    # The late answer lifted the refusal; one fresh check after the drain decided.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_a_check_left_running_by_one_gap_does_not_answer_the_next(monkeypatch):
    """A healthy sample ends the gap, and the old check's answer with it."""
    import threading

    from kiro_crew.dashboard import stale_asset_watchdog

    monkeypatch.setattr(stale_asset_watchdog, "_REENTRY_CHECK_TIMEOUT_SECS", 0.05)
    shutdown = asyncio.Event()
    calls = {"n": 0}
    release = threading.Event()
    state = {"reads": 0}

    def _check():
        calls["n"] += 1
        if calls["n"] == 1:
            return _verdict("REFUSED", "its supervisor's command (/x) is missing")
        if calls["n"] == 2:
            release.wait(5)
            return _verdict("REFUSED", "stale: from the first gap")
        return _verdict("REENTERABLE")

    def _assets_present() -> bool:
        state["reads"] += 1
        if state["reads"] == 1:
            return True  # armed
        if calls["n"] >= 2 and not release.is_set():
            # The second check has hung past its bound: the bundle comes back
            # for one sample, and the hung check then finishes.
            release.set()
            return True
        return False

    try:
        with patch(
            "kiro_crew.dashboard.stale_asset_watchdog.assets_present",
            side_effect=_assets_present,
        ):
            fired = await _run(shutdown, reentry_check=_check, interval=0.02)
    finally:
        release.set()

    assert fired is True
    assert calls["n"] == 3


def test_a_refused_relaunch_is_asked_again_within_a_few_intervals():
    """A repaired install must not wait out a long backoff on the fallback page."""
    from kiro_crew.dashboard import stale_asset_watchdog

    assert (
        stale_asset_watchdog._REENTRY_RECHECK_MAX_SECS
        <= 5 * stale_asset_watchdog._CHECK_INTERVAL_SECS
    )
