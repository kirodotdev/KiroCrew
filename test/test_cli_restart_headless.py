"""Headless restart mode: boot publication, legacy compatibility and refusal."""

import asyncio
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from kiro_crew import cli_gateway_mode, cli_server
from kiro_crew.instances import run_marker
from kiro_crew.slack import gateway
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


class FakeClock:
    """The service-path mode wait's clock: each sleep advances it, nothing sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.t += secs


@pytest.fixture
def restart(monkeypatch, tmp_path):
    monkeypatch.setattr(
        run_marker, "os", SimpleNamespace(**{**vars(os), "getpid": lambda: UNALLOCATABLE_PID})
    )
    monkeypatch.setattr(run_marker, "pid_start_token", lambda pid: "gateway-start")
    monkeypatch.setattr(
        cli_gateway_mode,
        "lock_holder",
        lambda home: SimpleNamespace(pid=UNALLOCATABLE_PID, alive=True),
    )
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: port)
    monkeypatch.setattr(cli_server, "_print_running_cron_runs", lambda: None)
    monkeypatch.setattr(cli_server, "_refuse_desktop_supervised", lambda *a, **kw: False)
    monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: True)
    monkeypatch.setattr(
        cli_server.platform_compat, "find_listening_pids", lambda port: [UNALLOCATABLE_PID]
    )
    monkeypatch.setattr(cli_server, "_wait_for_pids_exit", lambda *a, **kw: [])
    monkeypatch.setattr(cli_server, "_refuse_if_lock_held", lambda *a: None)
    monkeypatch.setattr(cli_server, "_wait_gateway_ready", lambda *a: (cli_server._READY_OK, None))
    secret = MagicMock(return_value="local-secret-fixture")
    monkeypatch.setattr(cli_server, "read_local_secret", secret)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"token":"test-session"}'
    monkeypatch.setattr(
        cli_server, "_owner_verified_secret_urlopen", MagicMock(return_value=response)
    )
    monkeypatch.setattr(cli_server, "resolve_dashboard_host", lambda **kw: "localhost")
    monkeypatch.setattr(
        cli_server.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(dashboard=SimpleNamespace(url="http://localhost:7000")),
    )
    monkeypatch.setattr(cli_server, "sel", MagicMock())
    clock = FakeClock()
    monkeypatch.setattr(cli_gateway_mode, "_now", clock.now)
    monkeypatch.setattr(cli_gateway_mode, "_sleep", clock.sleep)
    monkeypatch.setattr(cli_server, "_own_console_script", lambda: "/fake/kirocrew")
    stopped = MagicMock()
    monkeypatch.setattr(cli_server, "_stop", stopped)
    spawned = MagicMock(return_value=SimpleNamespace(pid=UNALLOCATABLE_PID + 1))
    monkeypatch.setattr(cli_server.subprocess, "Popen", spawned)
    return stopped, spawned


def replacement_argv(restart):
    stopped, spawned = restart
    cli_server._restart(7000)
    stopped.assert_called_once_with(7000)
    spawned.assert_called_once()
    kwargs = spawned.call_args.kwargs
    kwargs["stdout"].close()
    return spawned.call_args.args[0]


def test_headless_restart_adds_the_flag_before_stop_clears_markers(restart):
    run_marker.write_gateway_mode(headless=True)
    restart[0].side_effect = lambda port: run_marker.clear_marker(port)
    assert replacement_argv(restart) == [
        "/fake/kirocrew",
        "gateway",
        "--port",
        "7000",
        "--headless",
    ]


def test_ordinary_restart_keeps_the_same_argv(restart):
    run_marker.write_gateway_mode(headless=False)
    assert replacement_argv(restart) == ["/fake/kirocrew", "gateway", "--port", "7000"]


def test_absent_record_keeps_legacy_restart(restart):
    assert replacement_argv(restart) == ["/fake/kirocrew", "gateway", "--port", "7000"]


def test_absent_field_keeps_legacy_restart(restart):
    path = run_marker._run_dir() / "gateway-mode.json"
    path.write_text("{}", encoding="utf-8")
    assert replacement_argv(restart) == ["/fake/kirocrew", "gateway", "--port", "7000"]


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b"null",
        b"",
        b"\xff",
        b'{"headless": null}',
        b'{"headless": 0}',
        b'{"headless": "false"}',
    ],
)
def test_damaged_record_refuses_before_stop_or_spawn(restart, raw, capsys):
    path = run_marker._run_dir() / "gateway-mode.json"
    path.write_bytes(raw)
    with pytest.raises(SystemExit) as exc:
        cli_server._restart(7000)
    assert exc.value.code == 1
    restart[0].assert_not_called()
    restart[1].assert_not_called()
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Cannot read the gateway launch mode" in error
    assert "kirocrew gateway --headless" in error


@pytest.mark.parametrize("live", [False, True])
def test_unreadable_record_refuses_before_stop_or_spawn(restart, monkeypatch, capsys, live):
    run_marker.write_gateway_mode(headless=True)
    monkeypatch.setattr(
        cli_gateway_mode,
        "lock_holder",
        lambda home: SimpleNamespace(pid=UNALLOCATABLE_PID if live else None, alive=live),
    )
    monkeypatch.setattr(
        run_marker, "read_bytes_with_retry", MagicMock(side_effect=PermissionError("denied"))
    )
    with pytest.raises(SystemExit) as exc:
        cli_server._restart(7000)
    assert exc.value.code == 1
    restart[0].assert_not_called()
    restart[1].assert_not_called()
    assert "restart refused" in capsys.readouterr().err


class BootReached(Exception):
    """Stop the real boot method before it starts any service."""


def _boot_up_to_the_first_service(monkeypatch) -> MagicMock:
    """Let the real ``run`` reach its mode write, and stop it at the step after.

    ``apply_armed_update_at_boot`` is the first thing ``run`` does once the mode is
    published and before any service or listener starts.
    """
    from kiro_crew import update_boot_apply
    from kiro_crew.config import loosening_provenance

    monkeypatch.setattr(gateway.crash_guard, "install_loop_handler", MagicMock())
    monkeypatch.setattr(gateway.platform_compat, "raise_nofile_soft_limit", MagicMock())
    monkeypatch.setattr(loosening_provenance, "prime_host_verdict", lambda: None)
    monkeypatch.setattr(gateway, "signing_key_preflight", lambda: None)
    first_service = MagicMock(side_effect=BootReached)
    monkeypatch.setattr(update_boot_apply, "apply_armed_update_at_boot", first_service)
    return first_service


@pytest.mark.asyncio
async def test_headless_boot_rewrites_the_mode_before_starting_services(restart, monkeypatch):
    run_marker.write_gateway_mode(headless=False)
    _boot_up_to_the_first_service(monkeypatch)
    orch = SimpleNamespace(_headless=True, _background_tasks=set())
    with pytest.raises(BootReached):
        await gateway.GatewayOrchestrator.run(orch)
    assert run_marker.read_gateway_mode()["headless"] is True
    assert "--headless" in replacement_argv(restart)


@pytest.mark.asyncio
async def test_ordinary_boot_writes_nothing_before_starting_services(restart, monkeypatch):
    run_marker.write_gateway_mode(headless=True)
    _boot_up_to_the_first_service(monkeypatch)
    writes = MagicMock()
    monkeypatch.setattr(run_marker, "write_gateway_mode", writes)
    orch = SimpleNamespace(_headless=False, _background_tasks=set())
    with pytest.raises(BootReached):
        await gateway.GatewayOrchestrator.run(orch)
    writes.assert_not_called()
    assert run_marker.read_gateway_mode()["headless"] is True


class ListenerUp(Exception):
    """Stop the real boot method just after it schedules its post-bind marker work."""


def _boot_past_the_listener(monkeypatch, *, headless: bool):
    """Run the real ``run`` through the listener start, with every service mocked.

    Returns the orchestrator and the ``(headless, ...)`` mode writes seen when the
    listener started; ``run`` raises :class:`ListenerUp` right after the post-bind
    marker block.
    """
    from unittest.mock import AsyncMock

    from kiro_crew import gc_tuning, session, update_boot_apply
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import loosening_provenance
    from kiro_crew.crew_log import slot_backfill_child
    from kiro_crew.metrics import sessions as session_metrics

    monkeypatch.setattr(gateway.crash_guard, "install_loop_handler", MagicMock())
    monkeypatch.setattr(gateway.platform_compat, "raise_nofile_soft_limit", MagicMock())
    monkeypatch.setattr(loosening_provenance, "prime_host_verdict", lambda: None)
    monkeypatch.setattr(loosening_provenance, "adopt_unendorsed", lambda: None)
    monkeypatch.setattr(gateway, "signing_key_preflight", lambda: None)
    monkeypatch.setattr(update_boot_apply, "apply_armed_update_at_boot", AsyncMock())
    monkeypatch.setattr(session, "cleanup_orphaned_sessions", MagicMock())
    monkeypatch.setattr(agent_specs, "sweep_kas_launch_snapshots", lambda cutoff: None)
    monkeypatch.setattr(session_metrics, "backfill_crashed_sessions", lambda cutoff: 0)
    monkeypatch.setattr(gateway, "warm_backend", MagicMock())
    monkeypatch.setattr(gateway.cautious_boot, "initialize", AsyncMock())
    monkeypatch.setattr("gc.freeze", MagicMock())
    monkeypatch.setattr("gc.enable", MagicMock())
    monkeypatch.setattr(gc_tuning, "apply_threshold", lambda: False)
    monkeypatch.setattr(slot_backfill_child, "enable", MagicMock())

    orch = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    orch._background_tasks = set()
    orch._test_mode = True
    orch._no_dashboard = False
    orch._headless = headless
    orch._cfg = None
    orch._dashboard_port = 0
    orch.dashboard_state = None
    orch.subagent_mgr = None
    seen_at_listener: list[tuple] = []
    calls = []
    real_write = run_marker.write_gateway_mode

    def recording_write(*, headless: bool) -> None:
        calls.append(headless)
        real_write(headless=headless)

    monkeypatch.setattr(run_marker, "write_gateway_mode", recording_write)

    async def listener_starts() -> None:
        seen_at_listener.append(tuple(calls))

    for method in ("_init_services", "_start_embeddings", "_init_mcp_gateway", "_init_cron"):
        monkeypatch.setattr(orch, method, AsyncMock())
    for method in (
        "_init_mcp_discovery",
        "_init_subagents",
        "_init_task_runner",
        "_announce_cron_quarantine",
        "_schedule_console_script_repair",
        "_schedule_session_start_sizing",
    ):
        monkeypatch.setattr(orch, method, MagicMock())
    monkeypatch.setattr(orch, "_init_dashboard", AsyncMock(side_effect=listener_starts))
    monkeypatch.setattr(orch, "_wire_mcp_gateway_dashboard", MagicMock(side_effect=ListenerUp))
    return orch, seen_at_listener, calls


async def _post_bind_mode_write(orch) -> None:
    """Await the ordinary start's mode task, if it has not finished already."""
    import asyncio

    pending = [
        task
        for task in orch._background_tasks
        if task.get_coro().__qualname__.endswith("_record_ordinary_mode")
    ]
    await asyncio.wait_for(asyncio.gather(*pending), timeout=FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [True, False])
async def test_mode_is_written_before_the_listener_only_when_headless(
    restart, monkeypatch, headless
):
    run_marker.write_gateway_mode(headless=not headless)
    orch, seen_at_listener, calls = _boot_past_the_listener(monkeypatch, headless=headless)
    with pytest.raises(ListenerUp):
        await gateway.GatewayOrchestrator.run(orch)
    await _post_bind_mode_write(orch)
    # Headless: written once, before the listener. Ordinary: nothing before it, once after.
    assert seen_at_listener == [(True,) if headless else ()]
    assert calls == [headless]
    assert run_marker.read_gateway_mode()["headless"] is headless
    assert ("--headless" in replacement_argv(restart)) is headless


@pytest.mark.asyncio
async def test_ordinary_boot_completes_when_the_mode_write_fails(restart, monkeypatch, caplog):
    run_marker.write_gateway_mode(headless=True)
    orch, _, calls = _boot_past_the_listener(monkeypatch, headless=False)
    monkeypatch.setattr(run_marker, "atomic_write", MagicMock(side_effect=OSError("disk full")))
    with caplog.at_level("WARNING"):
        with pytest.raises(ListenerUp):
            await gateway.GatewayOrchestrator.run(orch)
        await _post_bind_mode_write(orch)
    assert calls == [False]
    warnings = [r for r in caplog.records if "gateway-mode.json" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"
    assert "disk full" in warnings[0].getMessage()
    assert "refuses if no live gateway can confirm it" in warnings[0].getMessage()
    # The stale record stays, and it can only say headless.
    assert run_marker.read_gateway_mode()["headless"] is True


@pytest.mark.asyncio
async def test_failed_mode_write_aborts_before_starting_services(monkeypatch):
    first_service = _boot_up_to_the_first_service(monkeypatch)
    monkeypatch.setattr(run_marker, "atomic_write", MagicMock(side_effect=OSError("disk full")))
    orch = SimpleNamespace(_headless=True, _background_tasks=set())
    with pytest.raises(OSError, match="disk full"):
        await gateway.GatewayOrchestrator.run(orch)
    first_service.assert_not_called()


@pytest_asyncio.fixture
async def blocked_mode_write(monkeypatch):
    """Expire the real wait only after the writer signals it is blocked."""
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    returned = threading.Event()
    workers = []
    modes = []
    bounded_write = gateway._write_gateway_mode_bounded

    def write(*, headless):
        workers.append(threading.current_thread())
        modes.append(headless)
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(FALLBACK_TIMEOUT_SECS), "mode writer was not released"
        finally:
            returned.set()

    async def expired_write():
        await bounded_write(timeout=0)

    async def wait_after_entry(aw, timeout):
        if timeout == 0:
            await asyncio.wait_for(entered.wait(), timeout=FALLBACK_TIMEOUT_SECS)
        return await asyncio.wait_for(aw, timeout=timeout)

    monkeypatch.setattr(run_marker, "write_gateway_mode", write)
    monkeypatch.setattr(gateway, "_write_gateway_mode_bounded", expired_write)
    monkeypatch.setattr(
        gateway, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": wait_after_entry})
    )
    try:
        yield workers, modes
    finally:
        release.set()
        if workers:
            assert returned.wait(FALLBACK_TIMEOUT_SECS), "mode writer did not return"
        for worker in workers:
            # Join our dedicated thread, not an executor owned by the event loop
            # when a mutation restores to_thread; loop teardown joins that pool.
            if worker.name == "kc-gateway-mode":
                worker.join(timeout=FALLBACK_TIMEOUT_SECS)
                assert not worker.is_alive(), "mode writer did not exit"


@pytest.mark.asyncio
async def test_blocked_headless_mode_write_refuses_before_services(
    monkeypatch, blocked_mode_write, capsys, caplog
):
    first_service = _boot_up_to_the_first_service(monkeypatch)
    orch = SimpleNamespace(_headless=True, _background_tasks=set())

    async def boot():
        # Catch inside the task: SystemExit must not terminate pytest's event loop.
        with pytest.raises(SystemExit) as exc:
            await gateway.GatewayOrchestrator.run(orch)
        return exc.value.code

    assert await asyncio.wait_for(boot(), timeout=FALLBACK_TIMEOUT_SECS) == 1
    first_service.assert_not_called()
    workers, modes = blocked_mode_write
    assert modes == [True]
    assert workers[0].daemon, "a stalled mode write must not hold up interpreter exit"
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "mode record could not be written within 0 seconds" in error
    assert "Check that the data home is reachable" in error
    assert any(r.levelname == "CRITICAL" and "mode record" in r.message for r in caplog.records)


def test_restart_captures_mode_before_stop(restart):
    run_marker.write_gateway_mode(headless=True)
    restart[0].side_effect = lambda port: run_marker.write_gateway_mode(headless=False)
    assert "--headless" in replacement_argv(restart)


@pytest.mark.asyncio
async def test_dev_fleet_foreground_restart_reaches_the_same_mode_guard(restart, monkeypatch):
    from kiro_crew.apps.builtins.dev_fleet.gateway_service import ForegroundBackend

    run_marker.write_gateway_mode(headless=True)
    monkeypatch.setattr(ForegroundBackend, "_usable_launcher", staticmethod(lambda path: path))

    def dispatch(argv):
        assert argv == ["/fake/kirocrew", "restart", "--port", "7000"]
        cli_server._restart(int(argv[-1]))

    backend = ForegroundBackend(
        marker_ports=lambda: [7000],
        read_pid=lambda port: UNALLOCATABLE_PID,
        read_launcher=lambda port: "/fake/kirocrew",
        pid_exists=lambda pid: True,
        spawn=dispatch,
        confinement=lambda: None,
        desktop_supervised=lambda port: False,
    )
    assert await backend.restart_detached() == (True, "")
    stopped, spawned = restart
    stopped.assert_called_once_with(7000)
    spawned.assert_called_once()
    spawned.call_args.kwargs["stdout"].close()
    assert "--headless" in spawned.call_args.args[0]


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.parametrize("managed", [True, False])
@pytest.mark.parametrize("headless", [True, False])
def test_restart_access_output(restart, monkeypatch, capsys, terminal, managed, headless):
    run_marker.write_gateway_mode(headless=headless)
    monkeypatch.setattr(cli_server.sys.stdout, "isatty", lambda: terminal)
    if managed:
        run_marker.write_gateway_mode(headless=not headless)
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: 7000)

        def service_restart():
            run_marker.write_gateway_mode(headless=headless)
            return True

        monkeypatch.setattr(cli_server.service_controller, "restart_service", service_restart)
        cli_server._restart(None)
        restart[0].assert_not_called()
        restart[1].assert_not_called()
        headline = "✅ Restarted kirocrew service.\n"
    else:
        restart[0].side_effect = lambda port: run_marker.clear_marker(port)
        argv = replacement_argv(restart)
        assert ("--headless" in argv) is headless
        headline = (
            f"✅ Started detached gateway (pid {UNALLOCATABLE_PID + 1}). "
            "Logs: kirocrew logs -f\n"
        )
    output = capsys.readouterr()
    assert output.err == ""
    if headless:
        assert output.out == (
            headline + "👻 Headless: API on http://127.0.0.1:7000/v1\n"
            "   Credential for a Crew API client: kirocrew token --bearer\n"
        )
        assert "?token=" not in output.out
        assert "sign-in" not in output.out
        cli_server.read_local_secret.assert_not_called()
        cli_server._owner_verified_secret_urlopen.assert_not_called()
    elif terminal:
        assert output.out == headline + "\n🔑 http://localhost:7000?token=test-session\n"
        cli_server._owner_verified_secret_urlopen.assert_called_once()
    else:
        assert output.out == headline + "\n🔑 Sign in: run kirocrew token for a sign-in link\n"
        cli_server.read_local_secret.assert_not_called()
        cli_server._owner_verified_secret_urlopen.assert_not_called()


@pytest.mark.parametrize("headless", [True, False])
def test_restart_timeout_names_the_served_surface(restart, monkeypatch, capsys, headless):
    run_marker.write_gateway_mode(headless=headless)
    monkeypatch.setattr(cli_server, "_resolve_restart_ready_timeout", lambda: 60)
    monkeypatch.setattr(
        cli_server, "_wait_gateway_ready", lambda *a: (cli_server._READY_TIMEOUT, None)
    )
    try:
        with pytest.raises(SystemExit) as exc:
            cli_server._restart(7000)
        assert exc.value.code == 1
    finally:
        restart[1].call_args.kwargs["stdout"].close()
    surface = "the API" if headless else "the dashboard"
    assert capsys.readouterr().out == (
        f"❌ Replacement gateway (pid {UNALLOCATABLE_PID + 1}) did not become ready within "
        "60s. It is still running but not serving port 7000.\n"
        f"   It may be slow to start or wedged during startup; nothing is serving {surface} yet.\n"
        "   To follow its startup, run: kirocrew logs -f\n"
    )
    cli_server.read_local_secret.assert_not_called()


@pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("invalid mode")])
def test_service_restart_withholds_link_when_mode_is_unknown(restart, monkeypatch, capsys, error):
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: 7000)
    restarted = MagicMock(return_value=True)
    monkeypatch.setattr(cli_server.service_controller, "restart_service", restarted)
    monkeypatch.setattr(run_marker, "read_gateway_mode", MagicMock(side_effect=error))
    cli_server._restart(None)
    restarted.assert_called_once_with()
    assert cli_gateway_mode._now() == 0.0  # refused at once, not after the bound
    assert capsys.readouterr().out == (
        "✅ Restarted kirocrew service.\n"
        "⚠️ Cannot read the gateway launch mode; no sign-in link printed.\n"
    )
    cli_server.read_local_secret.assert_not_called()
    cli_server._owner_verified_secret_urlopen.assert_not_called()
    restart[0].assert_not_called()
    restart[1].assert_not_called()


@pytest.mark.parametrize("run_fails", [False, True])
def test_the_home_lock_is_held_for_the_whole_gateway_run(monkeypatch, run_fails):
    """The mode record relies on this: no second gateway on the home can write it.

    An ordinary start writes the record on a background thread, and nothing waits
    for that thread at shutdown. Only the lock keeps a late write from landing
    over the record of a headless gateway started next on the same home.
    """
    import sys
    from unittest.mock import patch

    from kiro_crew.cli_logging import _stop_log_queue_listener

    order = []
    lock_cls = MagicMock()
    held = MagicMock()
    held.release.side_effect = lambda: order.append("release")

    def acquire():
        order.append("acquire")
        return held

    def run(*args, **kwargs):
        order.append("run")
        if run_fails:
            raise RuntimeError("gateway run failed")

    lock_cls.return_value.acquire.side_effect = acquire
    monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
    with (
        patch("kiro_crew.cli.GatewayLock", lock_cls),
        patch("kiro_crew.cli_server._gateway"),
        patch("kiro_crew.cli.asyncio.run", side_effect=run),
        patch("kiro_crew.cli.faulthandler.enable"),
    ):
        from kiro_crew.cli import main

        try:
            main()
        except (SystemExit, RuntimeError):
            pass
        finally:
            _stop_log_queue_listener()

    assert order == ["acquire", "run", "release"]


_MODE_CASES = [
    (
        {"headless": True, "pid": UNALLOCATABLE_PID + 1},
        (UNALLOCATABLE_PID, "gateway-start"),
        False,
        True,
    ),
    (
        {"headless": True, "pid": UNALLOCATABLE_PID, "start_token": "gateway-start"},
        (UNALLOCATABLE_PID, "gateway-start"),
        True,
        False,
    ),
    (
        {"headless": True, "pid": UNALLOCATABLE_PID, "start_token": "old-start"},
        (UNALLOCATABLE_PID, "gateway-start"),
        False,
        True,
    ),
    ({"headless": True}, (UNALLOCATABLE_PID, "gateway-start"), False, True),
    ({"headless": True}, None, None, False),
    ({"headless": True, "pid": UNALLOCATABLE_PID}, None, None, False),
    (
        {"headless": True, "pid": UNALLOCATABLE_PID + 1, "start_token": "old-start"},
        None,
        None,
        False,
    ),
    ({"headless": False}, None, False, False),
    (None, None, False, False),
    (
        {"headless": True, "pid": UNALLOCATABLE_PID},
        (UNALLOCATABLE_PID, "gateway-start"),
        True,
        False,
    ),
    (
        {"headless": True, "pid": UNALLOCATABLE_PID, "start_token": "gateway-start"},
        (UNALLOCATABLE_PID, ""),
        True,
        False,
    ),
    (
        {"headless": False, "pid": UNALLOCATABLE_PID},
        (UNALLOCATABLE_PID, "gateway-start"),
        False,
        False,
    ),
    (
        {"headless": False, "pid": UNALLOCATABLE_PID + 1},
        (UNALLOCATABLE_PID, "gateway-start"),
        False,
        True,
    ),
    ({}, None, False, False),
]


@pytest.mark.parametrize("record,live,headless,stale", _MODE_CASES)
def test_gateway_mode_decision(record, live, headless, stale):
    assert run_marker.gateway_mode_decision(record, live) == (headless, stale)


@pytest.mark.parametrize("record,live,headless,stale", _MODE_CASES)
@pytest.mark.parametrize("managed", [False, True])
def test_restart_checks_the_mode_writer(
    restart, monkeypatch, capsys, record, live, headless, stale, managed
):
    path = run_marker._run_dir() / "gateway-mode.json"
    if record is not None:
        path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(
        cli_gateway_mode,
        "lock_holder",
        lambda home: SimpleNamespace(pid=live[0] if live else None, alive=live is not None),
    )
    monkeypatch.setattr(run_marker, "pid_start_token", lambda pid: live[1] if live else "")
    monkeypatch.setattr(cli_server.sys.stdout, "isatty", lambda: False)
    if managed:
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: 7000)
        monkeypatch.setattr(cli_server.service_controller, "restart_service", lambda: True)
        cli_server._restart(None)
        restart[0].assert_not_called()
        restart[1].assert_not_called()
    else:
        if live is None:
            monkeypatch.setattr(cli_server.platform_compat, "find_listening_pids", lambda port: [])
            monkeypatch.setattr(
                cli_server.platform_compat, "listening_pid_tool_available", lambda: True
            )
            monkeypatch.setattr(cli_server, "_report_authenticated_shutdown", lambda port: False)
            monkeypatch.setattr(
                cli_server, "lock_holder", lambda home: SimpleNamespace(pid=None, alive=False)
            )
        if headless is None:
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(7000)
            assert exc.value.code == 1
            restart[0].assert_not_called()
            restart[1].assert_not_called()
            assert capsys.readouterr().err == _UNVERIFIED
            cli_server.sel().log_api_access.assert_not_called()
            return
        cli_server._restart(7000)
        restart[1].assert_called_once()
        restart[1].call_args.kwargs["stdout"].close()
        assert ("--headless" in restart[1].call_args.args[0]) is headless
        if live is None:
            restart[0].assert_not_called()
        else:
            restart[0].assert_called_once_with(7000)
    output = capsys.readouterr()
    if headless is None:
        assert output.out == "✅ Restarted kirocrew service.\n" + _UNKNOWN_SERVICE
        assert output.err == ""
        cli_server.read_local_secret.assert_not_called()
        cli_server._owner_verified_secret_urlopen.assert_not_called()
        return
    assert ("API on http://127.0.0.1:7000/v1" in output.out) is headless
    assert ("Sign in:" in output.out) is not headless
    # Only a stale record that said headless is reported: ignoring any other one
    # changes nothing.
    warned = stale and record is not None and record.get("headless") is True
    assert output.err == ("⚠️ Gateway mode record is stale and was ignored.\n" if warned else "")


@pytest.mark.parametrize("token", ["gateway-start", ""])
def test_mode_writer_records_its_own_identity(monkeypatch, tmp_path, token):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    identity = MagicMock(return_value=token)
    monkeypatch.setattr(run_marker, "pid_start_token", identity)
    run_marker.write_gateway_mode(headless=True)
    identity.assert_called_once_with(os.getpid())
    path = tmp_path / "run" / "gateway-mode.json"
    expected = {"headless": True, "pid": os.getpid()}
    if token:
        expected["start_token"] = token
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", True),
        ("pid", 0),
        ("pid", "12"),
        ("pid", None),
        ("start_token", 12),
        ("start_token", None),
    ],
)
def test_malformed_writer_identity_refuses_restart(restart, capsys, field, value):
    path = run_marker._run_dir() / "gateway-mode.json"
    path.write_text(json.dumps({"headless": True, field: value}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli_server._restart(7000)
    assert exc.value.code == 1
    restart[0].assert_not_called()
    restart[1].assert_not_called()
    assert "restart refused" in capsys.readouterr().err


def test_unknown_live_identity_retains_mode(restart, monkeypatch):
    from kiro_crew.gateway_lock import LockProbeError

    run_marker.write_gateway_mode(headless=True)
    monkeypatch.setattr(
        cli_gateway_mode,
        "lock_holder",
        MagicMock(side_effect=LockProbeError(run_marker._run_dir(), OSError("probe unavailable"))),
    )
    assert "--headless" in replacement_argv(restart)


def test_mode_identity_comes_from_this_home_not_another_listener(restart, monkeypatch, capsys):
    run_marker.write_gateway_mode(headless=True)
    monkeypatch.setattr(
        cli_server.platform_compat, "find_listening_pids", lambda port: [UNALLOCATABLE_PID + 2]
    )
    probe = MagicMock(return_value=SimpleNamespace(pid=UNALLOCATABLE_PID, alive=True))
    monkeypatch.setattr(cli_gateway_mode, "lock_holder", probe)
    assert "--headless" in replacement_argv(restart)
    probe.assert_called_once_with(run_marker.config_dir())
    assert capsys.readouterr().err == ""


def test_mode_identity_is_captured_before_shutdown(restart, monkeypatch, capsys):
    run_marker.write_gateway_mode(headless=True)
    probe = MagicMock(return_value=SimpleNamespace(pid=UNALLOCATABLE_PID, alive=True))
    monkeypatch.setattr(cli_gateway_mode, "lock_holder", probe)

    def stopped(port):
        probe.assert_called_once_with(run_marker.config_dir())
        probe.return_value = SimpleNamespace(pid=UNALLOCATABLE_PID + 1, alive=True)

    restart[0].side_effect = stopped
    assert "--headless" in replacement_argv(restart)
    assert capsys.readouterr().err == ""


_UNVERIFIED = (
    "❌ No gateway of this data home is running; the last recorded mode is "
    "headless and cannot be checked; restart refused. Start it with "
    "kirocrew gateway --headless or kirocrew gateway for one with the dashboard.\n"
)
_UNKNOWN_SERVICE = "⚠️ Cannot read the gateway launch mode; no sign-in link printed.\n"
_NEW = UNALLOCATABLE_PID + 1
_OLD = UNALLOCATABLE_PID + 2
_STALE = "⚠️ Gateway mode record is stale and was ignored.\n"
_LINK = "\n🔑 http://localhost:7000?token=test-session\n"
_API = "👻 Headless: API on http://127.0.0.1:7000/v1\n"


def _service_restart(monkeypatch, steps):
    """Restart through the service; each home-lock probe takes the next step.

    A step is (lock holder pid or None, record written just before the probe
    answers, or None to leave the file). The last step repeats. Returns the
    number of probes.
    """
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: 7000)
    monkeypatch.setattr(cli_server.service_controller, "restart_service", lambda: True)
    monkeypatch.setattr(cli_server.sys.stdout, "isatty", lambda: True)
    path = run_marker._run_dir() / "gateway-mode.json"
    probes = []

    def holder(home):
        pid, record = steps[min(len(probes), len(steps) - 1)]
        probes.append(pid)
        if record is not None:
            path.write_text(json.dumps(record), encoding="utf-8")
        return SimpleNamespace(pid=pid, alive=pid is not None)

    monkeypatch.setattr(cli_gateway_mode, "lock_holder", holder)
    cli_server._restart(None)
    return len(probes)


@pytest.mark.parametrize("old_headless", [False, True])
def test_service_restart_waits_for_the_ordinary_replacement_to_publish(
    restart, monkeypatch, capsys, old_headless
):
    old = {"headless": old_headless, "pid": _OLD}
    probes = _service_restart(
        monkeypatch, [(_NEW, old), (_NEW, old), (_NEW, {"headless": False, "pid": _NEW})]
    )
    assert probes == 3
    output = capsys.readouterr()
    assert output.out == "✅ Restarted kirocrew service.\n" + _LINK
    assert output.err == ""


def test_service_restart_waits_for_the_headless_replacement_to_publish(
    restart, monkeypatch, capsys
):
    probes = _service_restart(
        monkeypatch,
        [(_NEW, {"headless": False, "pid": _OLD}), (_NEW, {"headless": True, "pid": _NEW})],
    )
    assert probes == 2
    output = capsys.readouterr()
    assert output.out.startswith("✅ Restarted kirocrew service.\n" + _API)
    assert "?token=" not in output.out
    assert output.err == ""
    cli_server.read_local_secret.assert_not_called()


@pytest.mark.parametrize("headless", [False, True])
def test_service_restart_waits_for_the_lock_then_the_record(restart, monkeypatch, capsys, headless):
    leftover = {"headless": True, "pid": _OLD}
    probes = _service_restart(
        monkeypatch,
        [(None, leftover), (None, leftover), (_NEW, {"headless": headless, "pid": _NEW})],
    )
    assert probes == 3
    output = capsys.readouterr()
    assert output.out.startswith("✅ Restarted kirocrew service.\n" + (_API if headless else ""))
    assert (_LINK in output.out) is not headless
    assert output.err == ""


@pytest.mark.parametrize("live", [True, False])
def test_service_restart_decides_once_the_bound_passes(restart, monkeypatch, capsys, live):
    """A live stale holder is ordinary; without one, print no mode or access link."""
    _service_restart(monkeypatch, [(_NEW if live else None, {"headless": True, "pid": _OLD})])
    assert cli_gateway_mode._now() >= cli_server._RESTART_READY_TIMEOUT_DEFAULT
    output = capsys.readouterr()
    if live:
        assert output.out == "✅ Restarted kirocrew service.\n" + _LINK
        assert output.err == _STALE
    else:
        assert output.out == "✅ Restarted kirocrew service.\n" + _UNKNOWN_SERVICE
        assert output.err == ""
        cli_server.read_local_secret.assert_not_called()
        cli_server._owner_verified_secret_urlopen.assert_not_called()


def test_service_restart_refuses_a_damaged_record_at_once(restart, monkeypatch, capsys):
    _service_restart(monkeypatch, [(_NEW, {"headless": "yes", "pid": _OLD})])
    assert cli_gateway_mode._now() == 0.0
    assert capsys.readouterr().out == (
        "✅ Restarted kirocrew service.\n"
        "⚠️ Cannot read the gateway launch mode; no sign-in link printed.\n"
    )
    cli_server.read_local_secret.assert_not_called()


@pytest.mark.parametrize("headless", [False, True])
def test_stale_record_is_reported_only_when_it_said_headless(restart, capsys, headless):
    path = run_marker._run_dir() / "gateway-mode.json"
    path.write_text(json.dumps({"headless": headless, "pid": _OLD}), encoding="utf-8")
    assert run_marker.read_gateway_headless(live_identity=(_NEW, "gateway-start")) is False
    assert capsys.readouterr().err == (_STALE if headless else "")


def test_bearer_help_names_the_paths_that_read_it():
    import argparse

    from kiro_crew.cli_parser_gateway import register_token_parser

    parser = argparse.ArgumentParser()
    register_token_parser(parser.add_subparsers())
    token = parser._subparsers._group_actions[0].choices["token"]
    (bearer,) = [a for a in token._actions if "--bearer" in a.option_strings]
    assert "api_key" not in bearer.help
    assert "OpenAI" not in bearer.help
    assert "Crew API paths under /v1/" in bearer.help
    assert "POST /v1/chat/completions does not read that header" in bearer.help


@pytest.mark.parametrize("managed", [False, True])
def test_indeterminate_probe_does_not_mean_no_live_gateway(restart, monkeypatch, capsys, managed):
    from kiro_crew.gateway_lock import LockProbeError

    run_marker.write_gateway_mode(headless=True)
    error = LockProbeError(run_marker._run_dir(), OSError("probe unavailable"))
    probe = MagicMock(side_effect=error)
    monkeypatch.setattr(cli_gateway_mode, "lock_holder", probe)
    with pytest.raises(LockProbeError):
        cli_gateway_mode.home_gateway_identity()
    if managed:
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: 7000)
        monkeypatch.setattr(cli_server.service_controller, "restart_service", lambda: True)
        cli_server._restart(None)
        assert cli_gateway_mode._now() >= cli_server._RESTART_READY_TIMEOUT_DEFAULT
        assert _API in capsys.readouterr().out
    else:
        monkeypatch.setattr(cli_server.platform_compat, "find_listening_pids", lambda port: [])
        monkeypatch.setattr(
            cli_server.platform_compat, "listening_pid_tool_available", lambda: True
        )
        monkeypatch.setattr(cli_server, "_report_authenticated_shutdown", lambda port: False)
        monkeypatch.setattr(cli_server, "lock_holder", probe)
        with pytest.raises(SystemExit) as exc:
            cli_server._restart(7000)
        assert exc.value.code == 1
        output = capsys.readouterr()
        assert output.err == ""
        assert "Not signalling anything and not starting a replacement" in output.out
        assert "reason=lock_probe_indeterminate" in (
            cli_server.sel().log_api_access.call_args.kwargs["resources"]
        )
    restart[0].assert_not_called()
    restart[1].assert_not_called()


@pytest.mark.parametrize("record", [None, {}, {"headless": False}, {"headless": True}])
def test_indeterminate_probe_uses_the_validated_record(monkeypatch, record, unused_tcp_port):
    from kiro_crew.gateway_lock import LockProbeError

    monkeypatch.setattr(run_marker, "read_gateway_mode", lambda: record)
    monkeypatch.setattr(
        cli_gateway_mode,
        "lock_holder",
        MagicMock(side_effect=LockProbeError(run_marker.config_dir(), OSError("unavailable"))),
    )
    assert cli_gateway_mode.restart_gateway_headless(
        unused_tcp_port, cli_server._RESTART_READY_POLL_INTERVAL
    ) is bool(record and record.get("headless"))


@pytest.mark.parametrize("data", [[], {"headless": "yes"}, {"pid": 0}, {"start_token": 4}])
def test_pure_mode_decision_refuses_malformed_records(data):
    with pytest.raises(ValueError):
        run_marker.gateway_mode_decision(data, None)


_STARTING = (
    "❌ The gateway of this data home is still starting and its mode cannot be "
    "checked; restart refused. Stop it with kirocrew stop, then start it with "
    "kirocrew gateway --headless or kirocrew gateway for one with the dashboard.\n"
)


@pytest.fixture
def mode_window(restart, monkeypatch, unused_tcp_port):
    """Drive foreground boot observations entirely in process with a fake clock."""
    state = SimpleNamespace(
        clock=FakeClock(),
        port=unused_tcp_port,
        pid=_NEW,
        token="gateway-start",
        record={"headless": False, "pid": _OLD},
        listeners=[],
        advance=lambda: None,
    )
    monkeypatch.setattr(cli_gateway_mode, "_now", state.clock.now)

    def sleep(seconds):
        state.clock.sleep(seconds)
        state.advance()

    monkeypatch.setattr(cli_gateway_mode, "_sleep", sleep)
    monkeypatch.setattr(
        cli_gateway_mode,
        "lock_holder",
        lambda home: SimpleNamespace(pid=state.pid, alive=state.pid is not None),
    )
    monkeypatch.setattr(run_marker, "pid_start_token", lambda pid: state.token)
    monkeypatch.setattr(run_marker, "read_gateway_mode", lambda: state.record)
    monkeypatch.setattr(cli_server.platform_compat, "listening_pid_tool_available", lambda: True)
    state.lookup = MagicMock(side_effect=lambda port: state.listeners)
    monkeypatch.setattr(cli_server.platform_compat, "find_listening_pids", state.lookup)
    return state


def _foreground_mode(state):
    return cli_gateway_mode.restart_gateway_headless(
        state.port, cli_server._RESTART_READY_POLL_INTERVAL
    )


@pytest.mark.parametrize(
    "record",
    [
        None,
        {"headless": False, "pid": _OLD},
        {"headless": False, "pid": _NEW, "start_token": "old-start"},
    ],
)
def test_foreground_waits_for_headless_publication(mode_window, record, capsys):
    state = mode_window
    state.record = record
    state.advance = lambda: setattr(state, "record", {"headless": True, "pid": _NEW})
    assert _foreground_mode(state) is True
    assert state.clock.sleeps == [cli_server._RESTART_READY_POLL_INTERVAL]
    assert state.lookup.call_count == 2
    state.lookup.assert_called_with(state.port)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("settles_by", ["listener", "record", "slack-only"])
def test_foreground_waits_for_ordinary_evidence(mode_window, settles_by):
    state = mode_window

    def advance():
        if settles_by == "listener":
            state.listeners = [_NEW]
        else:
            state.record = {"headless": False, "pid": _NEW}
            state.listeners = [] if settles_by == "slack-only" else [_NEW]

    state.advance = advance
    assert _foreground_mode(state) is False
    assert state.clock.sleeps == [cli_server._RESTART_READY_POLL_INTERVAL]


@pytest.mark.parametrize("record", [None, {"headless": False, "pid": _OLD}])
@pytest.mark.parametrize("via_restart", [False, True])
def test_foreground_unnamed_deadline_refuses_without_stop_or_spawn(
    mode_window, restart, record, capsys, via_restart, monkeypatch
):
    state = mode_window
    state.record = record
    monkeypatch.setattr(cli_server, "_report_authenticated_shutdown", lambda port: False)
    monkeypatch.setattr(
        cli_server, "lock_holder", lambda home: SimpleNamespace(pid=state.pid, alive=True)
    )
    with pytest.raises(SystemExit) as exc:
        if via_restart:
            cli_server._restart(state.port)
        else:
            _foreground_mode(state)
    assert exc.value.code == 1
    assert state.clock.t == run_marker.GATEWAY_MODE_WRITE_TIMEOUT_SECS
    assert state.clock.sleeps
    assert capsys.readouterr() == ("", _STARTING)
    restart[0].assert_not_called()
    restart[1].assert_not_called()


@pytest.mark.parametrize("record", [None, {"headless": False}, {"headless": True}])
def test_foreground_holder_exits_uses_current_record(mode_window, record, capsys):
    state = mode_window

    def advance():
        state.pid = None
        state.record = record

    state.advance = advance
    if record and record.get("headless"):
        with pytest.raises(SystemExit) as exc:
            _foreground_mode(state)
        assert exc.value.code == 1
        assert capsys.readouterr() == ("", _UNVERIFIED)
    else:
        assert _foreground_mode(state) is False
        assert capsys.readouterr() == ("", "")
    assert state.clock.sleeps == [cli_server._RESTART_READY_POLL_INTERVAL]


@pytest.mark.parametrize("headless", [None, False, True])
def test_foreground_without_lookup_waits_for_record_or_refuses(
    mode_window, monkeypatch, headless, capsys
):
    state = mode_window
    state.listeners = [_NEW]
    monkeypatch.setattr(cli_server.platform_compat, "listening_pid_tool_available", lambda: False)
    if headless is None:
        with pytest.raises(SystemExit) as exc:
            _foreground_mode(state)
        assert exc.value.code == 1
        assert state.clock.t == run_marker.GATEWAY_MODE_WRITE_TIMEOUT_SECS
        assert capsys.readouterr() == ("", _STARTING)
    else:
        state.advance = lambda: setattr(state, "record", {"headless": headless, "pid": _NEW})
        assert _foreground_mode(state) is headless
        assert state.clock.sleeps == [cli_server._RESTART_READY_POLL_INTERVAL]
    state.lookup.assert_not_called()


def test_foreground_legacy_listener_needs_no_wait(mode_window):
    state = mode_window
    state.record = None
    state.listeners = [_NEW]
    assert _foreground_mode(state) is False
    assert state.clock.sleeps == []


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize(
    "record", [None, {"headless": False, "pid": _NEW}, {"headless": True, "pid": _NEW}]
)
def test_foreground_settled_states_need_no_wait(mode_window, live, record, capsys):
    state = mode_window
    state.pid = _NEW if live else None
    state.record = record
    if live and record is None:
        state.listeners = [_NEW]
    if not live and record and record["headless"]:
        with pytest.raises(SystemExit) as exc:
            _foreground_mode(state)
        assert exc.value.code == 1
        assert capsys.readouterr() == ("", _UNVERIFIED)
    else:
        assert _foreground_mode(state) is bool(record and record["headless"])
    assert state.clock.sleeps == []


def test_foreground_changed_holder_stays_inside_original_deadline(mode_window, capsys):
    state = mode_window
    # An unrelated listener never proves this home's holder ordinary.
    state.record = None
    state.listeners = [UNALLOCATABLE_PID]
    state.advance = lambda: setattr(state, "pid", state.pid + 1)
    with pytest.raises(SystemExit) as exc:
        _foreground_mode(state)
    assert exc.value.code == 1
    assert state.clock.t == run_marker.GATEWAY_MODE_WRITE_TIMEOUT_SECS
    assert capsys.readouterr() == ("", _STARTING)


def test_foreground_changed_holder_can_publish_its_mode(mode_window):
    state = mode_window

    def advance():
        state.pid = _OLD
        state.record = {"headless": True, "pid": _OLD}

    state.advance = advance
    assert _foreground_mode(state) is True
    assert state.clock.sleeps == [cli_server._RESTART_READY_POLL_INTERVAL]


def test_foreground_reads_record_after_listener_observation(mode_window):
    state = mode_window

    def listening(port):
        state.record = {"headless": True, "pid": _NEW}
        return [_NEW]

    state.lookup.side_effect = listening
    assert _foreground_mode(state) is True
    assert state.clock.sleeps == []


def test_restart_rechecks_listeners_after_mode_settles(mode_window, restart):
    state = mode_window

    def advance():
        state.record = {"headless": True, "pid": _NEW}
        state.listeners = [_NEW]

    state.advance = advance
    cli_server._restart(state.port)
    restart[0].assert_called_once_with(state.port)
    restart[1].assert_called_once()
    restart[1].call_args.kwargs["stdout"].close()
    assert "--headless" in restart[1].call_args.args[0]
    assert state.lookup.call_count == 3  # two mode probes, then restart's own lookup
