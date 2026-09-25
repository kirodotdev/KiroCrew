"""Does the real gateway boot, serve, restart and stop -- in this process?

This file is the floor the rest of ``test/integration/`` stands on. If it is
red, every other file here is red for the same reason, so keep it tiny and
keep every assertion about the HARNESS rather than about a feature.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import sys
import types
from pathlib import Path

import pytest
from integration import conftest as harness

from kiro_crew import safety_override
from kiro_crew.slack.gateway import GatewayOrchestrator

try:
    import resource as _resource
except ImportError:  # pragma: no cover -- Windows has no resource module
    _resource = None  # type: ignore[assignment]


def _signal_handlers() -> dict[int, object]:
    return {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}


def _process_is_clean() -> bool:
    return not harness.memory_fence_held() and harness.home_bound_globals_are_clear()


def _live_tasks() -> set["asyncio.Task[object]"]:
    return {t for t in asyncio.all_tasks() if not t.done()}


def _loop_and_limit_state() -> tuple[object, tuple[int, int] | None]:
    """The two process settings a boot changes that live outside any module:
    the running loop's exception handler and the ``RLIMIT_NOFILE`` soft limit."""
    loop = asyncio.get_running_loop()
    limit = _resource.getrlimit(_resource.RLIMIT_NOFILE) if _resource is not None else None
    return loop.get_exception_handler(), limit


def test_shutdown_and_exit_ends_in_os_exit() -> None:
    """Teardown lets ``run()`` walk its real exit path and intercepts only
    ``os._exit`` (``conftest.intercepted_os_exit``). That holds while the exit
    is the LAST statement of ``_shutdown_and_exit`` and the only hard exit
    ``run()`` can reach -- an ``os._exit`` anywhere else, or work after it,
    would end pytest or be skipped. Pin the shape the interception relies on.
    """
    exit_source = inspect.getsource(GatewayOrchestrator._shutdown_and_exit)
    statements = [line.strip() for line in exit_source.splitlines() if line.strip()]
    assert statements[-1].startswith("os._exit("), (
        "_shutdown_and_exit must end in os._exit: the harness intercepts that call as "
        "the end of the exit path"
    )
    assert exit_source.count("os._exit(") == 1
    run_source = inspect.getsource(GatewayOrchestrator.run)
    assert "os._exit(" not in run_source, (
        "run() calls os._exit outside _shutdown_and_exit; the harness only intercepts "
        "the one at the end of the exit path"
    )
    assert inspect.iscoroutinefunction(GatewayOrchestrator.__dict__["run"])


def test_dump_dir_must_be_absolute_and_outside_the_checkout(tmp_path: Path) -> None:
    """A relative or in-checkout dump directory would leave route files in the
    repository; the harness refuses both before anything boots."""
    assert harness.resolve_dump_dir(None) is None
    assert harness.resolve_dump_dir("") is None
    assert harness.resolve_dump_dir(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(pytest.UsageError, match="absolute"):
        harness.resolve_dump_dir("build/integration-hits")
    inside = Path(harness.__file__).resolve().parents[2] / "build" / "integration-hits"
    with pytest.raises(pytest.UsageError, match="inside the repository checkout"):
        harness.resolve_dump_dir(str(inside))


@pytest.mark.asyncio
async def test_boots_and_serves_health(gateway_boot) -> None:
    async with gateway_boot() as gw:
        body = await gw.get_json("/api/health", auth=False)
        assert isinstance(body, dict)
        assert gw.port > 0
        assert gw.state is not None


@pytest.mark.asyncio
async def test_token_guards_the_api(gateway_boot) -> None:
    async with gateway_boot() as gw:
        ok = await gw.get("/api/sessions")
        assert ok.status == 200, await ok.text()
        denied = await gw.get("/api/sessions", auth=False)
        assert denied.status in (401, 403), await denied.text()


@pytest.mark.asyncio
async def test_restart_reboots_on_the_same_home(gateway_boot) -> None:
    """A restart keeps the home on disk but must not keep the process state of
    the first boot: the ``SafetyOverride`` singleton is per boot, so a YOLO
    grant from before the restart cannot survive it."""
    async with gateway_boot() as gw:
        home = gw.home
        marker = home / "integration-restart-marker"
        marker.write_text("survives", encoding="utf-8")
        override_before = safety_override.safety_override()

        await gw.restart()

        assert gw.home == home
        assert marker.read_text(encoding="utf-8") == "survives"
        assert safety_override.safety_override() is not override_before
        body = await gw.get_json("/api/health", auth=False)
        assert isinstance(body, dict)


@pytest.mark.asyncio
async def test_a_boot_leaves_the_process_as_it_found_it(gateway_boot) -> None:
    """Startup writes ``KIROCREW_BOUND_PORT``/``_HOST``, installs signal
    handlers and a loop exception handler, raises the open-file limit, takes
    the memory-preparation fence and fills the home-bound auth globals; a
    finished boot must have undone all of it, or the next test inherits
    another home's gateway address, crash log, fence and signing key."""
    environ_before = dict(os.environ)
    handlers_before = _signal_handlers()
    loop_and_limit_before = _loop_and_limit_state()
    tasks_before = _live_tasks()
    assert _process_is_clean()

    async with gateway_boot() as gw:
        assert os.environ.get("KIROCREW_BOUND_PORT") == str(gw.port)

    # The real exit path ran: it clears the run marker the boot published.
    assert not (gw.home / "run" / f"gateway-{gw.port}.bin").exists()
    assert dict(os.environ) == environ_before
    assert _signal_handlers() == handlers_before
    assert _loop_and_limit_state() == loop_and_limit_before
    leaked = _live_tasks() - tasks_before - {asyncio.current_task()}
    assert not [t.get_name() for t in leaked]
    assert _process_is_clean()


@pytest.mark.asyncio
async def test_a_second_home_gets_its_own_signing_key(tmp_path: Path, monkeypatch) -> None:
    """Two boots on two homes in one process: the token minted for the first
    must not validate against the second, which is only true when the cached
    signing key and revoked-nonce store are dropped between them."""
    from kiro_crew.dashboard import token_secret

    homes = []
    keys = []
    for name in ("first", "second"):
        home = tmp_path / name
        home.mkdir()
        homes.append(home)
        with monkeypatch.context() as patched:
            patched.setenv("KIROCREW_HOME", str(home))
            patched.setenv("KIRO_HOME", str(home / "kiro"))
            patched.setenv("KIROCREW_KIRO_BIN", str(harness.fake_acp_backend.__file__))
            for key in harness.CREDENTIAL_KEYS:
                patched.delenv(key, raising=False)
            async with harness.booted_gateway(home) as gw:
                await gw.get_json("/api/sessions")
                keys.append(token_secret._get_secret())
    assert keys[0] != keys[1]
    assert _process_is_clean()


#: Module globals a second boot is KNOWN to replace or grow, each with the
#: reason it is not residue. The witness below fails on any pair not listed,
#: so a home-derived global startup grows must land on the harness reset list
#: (``conftest._reset_home_bound_globals``) or, with a reason, here.
_KNOWN_SECOND_BOOT_CHANGES: dict[tuple[str, str], str] = {
    # Monotonic counters and clocks: carry no home state.
    ("kiro_crew.config.loader", "_CONFIG_AUTOCOMPACT_ISSUED"): "counter",
    ("kiro_crew.config.loader", "_CONFIG_AUTOCOMPACT_TICKET"): "counter",
    ("kiro_crew.config.loader", "_CONFIG_TIMEZONE_TICKET"): "counter",
    ("kiro_crew.config.loader", "_MATERIALIZED_REFRESH_APPLIED"): "counter",
    ("kiro_crew.config.loader", "_MATERIALIZED_REFRESH_ISSUED"): "counter",
    ("kiro_crew.context", "_store_cache_generation"): "counter",
    ("kiro_crew.crew_log.emit", "_shutdown_deadline"): "clock",
    ("kiro_crew.crew_log.emit", "_shutdown_started"): "clock",
    ("kiro_crew.dashboard.handlers.updates", "_last_update_check"): "clock",
    ("kiro_crew.platform.context", "_GOVERNANCE_GENERATION"): "counter",
    ("kiro_crew.platform.governance_profiles", "_PROFILE_GENERATION"): "counter",
    # Per-boot objects the next boot replaces wholesale before any read; they
    # hold references, not home-derived decisions a later request would act on.
    ("kiro_crew.apps.builtins.auto_improvement.backend.crew", "_runtime"): "replaced per boot",
    ("kiro_crew.apps.hook_reconcile", "_cron_service"): "replaced per boot",
    ("kiro_crew.apps.hooks_integration", "_lifecycle_dispatcher"): "replaced per boot",
    ("kiro_crew.apps.hooks_integration", "_route_registry"): "replaced per boot",
    ("kiro_crew.dashboard.cautious_boot", "_decision"): "replaced per boot",
    ("kiro_crew.dashboard.crash_dump_store", "_active_dump_file"): "replaced per boot",
    ("kiro_crew.diag.recorder", "_recorder"): "replaced per boot",
    ("kiro_crew.hooks", "_BUILTIN_APP_AGENTS"): "replaced per boot",
    ("kiro_crew.hooks", "_global_script_hook_store"): "replaced per boot",
    ("kiro_crew.skill_usage", "_global_skill_read_observer"): "replaced per boot",
    ("kiro_crew.slack.interactions", "_orch"): "replaced per boot",
    ("kiro_crew.taskq.dependency", "_current"): "cleared by _shutdown()",
    # Caches keyed by the thing they cache (a path, a server name), so a stale
    # entry is never returned for a different home.
    ("kiro_crew.agent_discovery", "_LIST_AGENTS_CACHE"): "keyed cache",
    ("kiro_crew.apps.dev_mode", "_dev_apps_cache"): "keyed cache",
    ("kiro_crew.apps.manager", "_orphaned_builtins_cache"): "keyed cache",
    ("kiro_crew.autonudge", "_MAINTENANCE_LOCKS"): "keyed cache",
    ("kiro_crew.dashboard.handlers.mcp", "_mcp_probe_cache"): "keyed cache",
    ("kiro_crew.dashboard.handlers.mcp", "_mcp_probe_ts"): "keyed cache",
    ("kiro_crew.mcp_discovery", "_probe_cache"): "keyed cache",
    ("kiro_crew.agent_discovery", "_PARSED_SPECS_CACHE"): "keyed cache",
    # Process-wide thread pools created on first use, home-independent.
    ("kiro_crew.executors", "_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_subprocess_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_kiro_spawn_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_cron_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_discovery_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_embed_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_recall_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_image_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_stt_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_governance_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_cron_gate_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_path_resolve_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_path_probe_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_path_transfer_pool"): "lazy thread pool",
    ("kiro_crew.executors", "_crew_log_pool"): "lazy thread pool",
    # Task handles of loops the harness reaps at teardown (docstring, item 4);
    # only the dead handle remains.
    ("kiro_crew.apps.builtins.auto_research.handlers", "_watchdog_task"): "reaped task handle",
    ("kiro_crew.apps.builtins.code_review_sage.backend.routes", "_TASKS"): "reaped task handles",
    ("kiro_crew.dashboard.handlers.mcp", "_mcp_probe_task"): "reaped task handle",
    ("kiro_crew.sandbox", "_warm_thread"): "finished probe thread handle",
}

_SCALAR_TYPES = (int, float, str, bool, bytes, type(None), tuple, frozenset)


def _module_globals() -> dict[tuple[str, str], tuple[int, str]]:
    """One comparable value per module-level name of every loaded ``kiro_crew`` module.

    Scalars compare by value; containers by identity plus size; anything else
    by identity. Classes, functions and modules are skipped: a boot does not
    reassign those.
    """
    out: dict[tuple[str, str], tuple[int, str]] = {}
    for module_name, module in list(sys.modules.items()):
        if not module_name.startswith("kiro_crew") or module is None:
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith("__") or isinstance(
                value, (types.ModuleType, type, types.FunctionType)
            ):
                continue
            if isinstance(value, _SCALAR_TYPES):
                out[(module_name, attr)] = (0, repr(value)[:200])
            elif isinstance(value, (dict, list, set)):
                out[(module_name, attr)] = (id(value), f"len={len(value)}")
            else:
                out[(module_name, attr)] = (id(value), type(value).__name__)
    return out


@pytest.mark.asyncio
async def test_a_second_boot_touches_only_known_module_globals(gateway_boot) -> None:
    """The generic witness behind the reset list: diff every loaded
    ``kiro_crew`` module's globals across a SECOND boot (the first warms the
    imports) and require each changed name to be either restored by the
    harness or listed in ``_KNOWN_SECOND_BOOT_CHANGES`` with its reason. A
    home-derived global that startup grows fails here by name."""
    async with gateway_boot() as gw:
        await gw.get_json("/api/health", auth=False)
    before = _module_globals()
    async with gateway_boot() as gw:
        await gw.get_json("/api/health", auth=False)
    after = _module_globals()

    changed = {key for key in before.keys() & after.keys() if before[key] != after[key]}
    unexplained = sorted(changed - _KNOWN_SECOND_BOOT_CHANGES.keys())
    assert not unexplained, (
        "a boot changed module globals the harness neither restores nor documents; "
        "add each to conftest._reset_home_bound_globals or, with its reason, to "
        f"_KNOWN_SECOND_BOOT_CHANGES: {unexplained}"
    )


@pytest.mark.asyncio
async def test_a_failing_teardown_still_restores_the_process(gateway_boot, monkeypatch) -> None:
    """A task that refuses cancellation fails the teardown -- and the process
    snapshot, the reset list and the HTTP client are still put back, or the
    next boot would snapshot this gateway's handlers as its baseline."""
    monkeypatch.setattr(harness, "TASK_REAP_SECS", 0.2)
    environ_before = dict(os.environ)
    handlers_before = _signal_handlers()
    loop_and_limit_before = _loop_and_limit_state()
    release = asyncio.Event()

    async def _stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # the one thing a boot's task must never do

    stubborn: asyncio.Task[None] | None = None
    try:
        with pytest.raises(RuntimeError, match="did not end within"):
            async with gateway_boot() as gw:
                stubborn = asyncio.create_task(_stubborn(), name="stubborn-boot-task")
                assert gw._client.closed is False
        assert dict(os.environ) == environ_before
        assert _signal_handlers() == handlers_before
        assert _loop_and_limit_state() == loop_and_limit_before
        assert gw._client.closed
        assert _process_is_clean()
    finally:
        release.set()
        if stubborn is not None:
            await stubborn


@pytest.mark.asyncio
async def test_a_hard_exit_during_the_test_raises_instead_of_ending_pytest(gateway_boot) -> None:
    """The ``os._exit`` interception is live for the whole boot, not only at
    teardown: a ``run()`` that exits on its own mid-test (a config it refuses
    to serve, an early owner stop) must surface as ``HarnessExit``."""
    async with gateway_boot():
        with pytest.raises(harness.HarnessExit) as raised:
            os._exit(3)
        assert raised.value.code == 3
    assert _process_is_clean()


@pytest.mark.asyncio
async def test_the_stall_watchdog_cannot_end_the_worker(gateway_boot) -> None:
    """pytest enables ``faulthandler``, so the boot arms the loop-stall
    watchdog whose hard timer exits the PROCESS after 25s without a beat.
    While a boot is live that arm is a no-op; afterwards the real one is back
    and nothing is left pending."""
    assert not harness.hard_exit_timer_is_disabled()
    async with gateway_boot() as gw:
        assert harness.hard_exit_timer_is_disabled()
        watchdog = getattr(gw.state, "_loop_watchdog", None)
        assert watchdog is not None
        # ``start()`` is what is gated on faulthandler: its thread runs, and it
        # armed the hard timer (into the harness's no-op) without an error.
        assert watchdog.is_running(), "the boot did not start the loop watchdog under pytest"
        assert watchdog._later_active is True, "the watchdog did not arm its hard-exit timer"
    assert not harness.hard_exit_timer_is_disabled()


@pytest.mark.asyncio
async def test_a_run_that_dies_on_its_own_is_still_shut_down(gateway_boot) -> None:
    """``run()`` ending on an exception never reaches ``_shutdown_and_exit``,
    so its ``_shutdown()`` never ran. Teardown owes it: the dashboard, the
    task-store writer and its SQLite connection are still up. The error that
    ended ``run()`` is the one the test sees."""
    environ_before = dict(os.environ)
    handlers_before = _signal_handlers()
    tasks_before = _live_tasks()
    shutdowns: list[str] = []

    with pytest.raises(RuntimeError, match="boom"):
        async with gateway_boot() as gw:
            orchestrator = gw.orchestrator
            real_shutdown = orchestrator._shutdown

            async def _spied_shutdown() -> None:
                shutdowns.append("called")
                await real_shutdown()

            async def _die(*_a: object, **_k: object) -> None:
                raise RuntimeError("boom")

            orchestrator._shutdown = _spied_shutdown  # type: ignore[method-assign]
            orchestrator._shutdown_and_exit = _die  # type: ignore[method-assign]
            harness.shutdown_event.set()
            with pytest.raises(RuntimeError, match="boom"):
                await asyncio.wait_for(asyncio.shield(gw._run_task), timeout=30)

    assert shutdowns == ["called"]
    assert dict(os.environ) == environ_before
    assert _signal_handlers() == handlers_before
    leaked = _live_tasks() - tasks_before - {asyncio.current_task()}
    assert not [t.get_name() for t in leaked]
    assert _process_is_clean()


@pytest.mark.asyncio
async def test_a_failed_boot_leaves_nothing_running(integration_home) -> None:
    """A boot that misses its deadline reaps its own ``run()`` task and puts
    the process back, so the next test starts clean."""
    environ_before = dict(os.environ)
    handlers_before = _signal_handlers()
    loop_and_limit_before = _loop_and_limit_state()
    tasks_before = {t for t in asyncio.all_tasks() if not t.done()}

    with pytest.raises(RuntimeError, match="did not serve HTTP"):
        async with harness.booted_gateway(integration_home, boot_secs=0.01):
            pass  # pragma: no cover -- the boot must not get this far

    leaked = {t for t in asyncio.all_tasks() if not t.done() and t not in tasks_before} - {
        asyncio.current_task()
    }
    assert not [t.get_name() for t in leaked]
    assert dict(os.environ) == environ_before
    assert _signal_handlers() == handlers_before
    assert _loop_and_limit_state() == loop_and_limit_before
    assert _process_is_clean()
