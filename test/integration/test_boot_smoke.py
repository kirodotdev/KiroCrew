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
import time
import types
from pathlib import Path
from typing import Any, Awaitable

import pytest
from integration import conftest as harness

from kiro_crew import identity_stores, onboarding_sources, safety_override
from kiro_crew.agent_sdk import host_auth
from kiro_crew.dashboard.handlers import kiro_usage_api
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
        user_home = tmp_path / f"{name}-user-home"
        user_home.mkdir()
        with monkeypatch.context() as patched:
            patched.setenv("KIROCREW_HOME", str(home))
            patched.setenv("KIRO_HOME", str(home / "kiro"))
            # What integration_home does for its one home: the developer's
            # home, channel credentials, and the AWS and GitHub identity.
            harness.isolate_user_home(patched, user_home)
            harness.isolate_credentials(patched, home)
            patched.setenv("KIROCREW_KIRO_BIN", str(harness.fake_acp_backend.__file__))
            async with harness.booted_gateway(home) as gw:
                await gw.get_json("/api/sessions")
                keys.append(token_secret._get_secret())
    assert keys[0] != keys[1]
    assert _process_is_clean()


#: Audit events that read a path, recorded by ``_record_path_reads``.
_PATH_READ_EVENTS = frozenset({"open", "os.listdir", "os.scandir", "sqlite3.connect"})
#: Lost-run ceilings for the outer-home test's two requests, measured at 0.02 s
#: (scan) and 1.0 s (refresh) on a loaded 32-CPU host; with the harness's 60 s
#: boot bound they stay under the repository's default 120 s item timeout
#: (``setup.cfg``; the CI integration lane allows 300 s).
_SCAN_SECS = 20.0
_REFRESH_SECS = 30.0
#: The credential reader as production binds it, before any test patches it.
_PRODUCTION_CANDIDATE_TOKENS = kiro_usage_api._candidate_tokens
#: Where ``_record_path_reads`` appends while armed; ``None`` keeps it inert.
_path_reads: list[str] | None = None
_path_read_hook_installed = False


def _record_path_reads(event: str, args: tuple) -> None:
    """Audit hook: while armed, keep the path every read event names.

    Installed at most once per process (an audit hook cannot be removed) and
    inert while ``_path_reads`` is ``None``. It runs on every thread, so reads
    the boot hands to an executor are recorded too.
    """
    sink = _path_reads
    if sink is None or event not in _PATH_READ_EVENTS or not args:
        return
    try:
        name = os.fsdecode(args[0])
    except TypeError:
        return
    if event == "sqlite3.connect" and name.startswith("file:"):
        name = name[len("file:") :].split("?", 1)[0]
    sink.append(name)


def _outer_credentials() -> set[str]:
    """Every AWS and GitHub credential name ``isolate_credentials`` drops."""
    github = {
        key
        for key in harness.github_runner.GH_ENV_PASSTHROUGH
        if key.startswith(("GH_", "GITHUB_"))
    }
    return {
        *harness.acp_client.CREDENTIAL_POINTER_ENV_VARS,
        *harness._AWS_IDENTITY_ENV,
        *github,
    } - {"AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"}


def _outer_home_overrides() -> set[str]:
    """Every home-override variable ``isolate_user_home`` drops."""
    return {
        *host_auth.home_override_env_vars(),
        *(key for source in onboarding_sources._core_sources() for key in source.env_vars),
        *harness._UNDECLARED_HOME_OVERRIDES,
    } - {"XDG_CONFIG_HOME"}


@pytest.fixture
def outer_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A developer's home, as the environment hands it to the test process.

    Request it BEFORE ``gateway_boot``: ``integration_home`` then runs after it,
    the way it runs after the real environment. Every home-root and override
    variable ``isolate_user_home`` sets or drops points in here, and kiro-cli's
    credential stores sit where a developer's would.
    ``XDG_CONFIG_HOME`` is left alone: the rootdir conftest has already moved
    it. Every AWS and GitHub credential name ``isolate_credentials`` drops
    carries a stand-in value, and the AWS CLI's two files point in here. The
    usage reader's own store paths are not moved: they are a security anchor
    bound at import, so the test pins the reader instead.
    """
    assert not (
        tmp_path / "home"
    ).exists(), "request outer_home before gateway_boot: integration_home has already run"
    root = tmp_path / "outer-home"
    trusted = identity_stores.sqlite_dbs(identity_stores.Trust.TRUSTED, home=root)
    other = identity_stores.sqlite_dbs(identity_stores.Trust.OTHER, home=root)
    for db in (*trusted, *other):
        db.parent.mkdir(parents=True, exist_ok=True)
        # An empty SQLite database: opening it is the read under test, and it
        # holds no token a refresh could send anywhere.
        db.write_bytes(b"")
    transcript = root / ".claude" / "projects" / "outer" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type": "user"}\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    platform_roots = {
        "APPDATA": "AppData/Roaming",
        "LOCALAPPDATA": "AppData/Local",
        "XDG_DATA_HOME": ".local/share",
        "XDG_CACHE_HOME": ".cache",
        "XDG_STATE_HOME": ".local/state",
    }
    for key, relative in platform_roots.items():
        target = root.joinpath(*relative.split("/"))
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(key, str(target))
    hermes = root / "AppData" / "Local" / "hermes"
    hermes.mkdir()
    (hermes / "config.yaml").write_text("model: outer\n", encoding="utf-8")
    for key in sorted(_outer_home_overrides() - set(platform_roots)):
        if key == "OPENCLAW_PROFILE":
            monkeypatch.setenv(key, "outer")
            continue
        target = root / "overrides" / key
        target.mkdir(parents=True)
        (target / "config.json").write_text("{}", encoding="utf-8")
        if key == "OPENCLAW_CONFIG_PATH":
            target = target / "config.json"
        monkeypatch.setenv(key, str(target))
    for key, relative in platform_roots.items():
        assert os.environ[key] == str(root.joinpath(*relative.split("/"))), key
    # The developer's AWS and GitHub identity, as the environment carries it.
    for name in ("config", "credentials"):
        (root / ".aws").mkdir(exist_ok=True)
        (root / ".aws" / name).write_text("[default]\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(root / ".aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(root / ".aws" / "credentials"))
    for key in sorted(_outer_credentials()):
        monkeypatch.setenv(key, f"outer-credential-{key}")
    return root


@pytest.mark.asyncio
async def test_a_boot_reads_nothing_from_the_outer_home(
    outer_home: Path, gateway_boot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process gateway must not read the home the test process was given.

    ``outer_home`` stands in for the developer's: their foreign agents' homes
    and kiro-cli's credential stores. The onboarding import scan and a credit
    refresh are the routes that read those, so the test boots, drives both, and
    records every path the process opens or lists while it does. It also
    checks every root the import scan resolves, and that the harness replaced
    the credit refresh's credential reader, whose store paths import bound to
    the real home where no stand-in can move them.
    """
    global _path_reads, _path_read_hook_installed
    roots = tuple(
        {os.path.normcase(str(outer_home)), os.path.normcase(os.path.realpath(outer_home))}
    )

    def beneath(path: str) -> bool:
        normalized = os.path.normcase(os.path.abspath(path))
        return any(normalized == root or normalized.startswith(root + os.sep) for root in roots)

    def outer(paths: list[str]) -> list[str]:
        return sorted({path for path in paths if beneath(path)})

    reached: list[bool] = []
    reader = kiro_usage_api._candidate_tokens
    # Checked before anything boots: with the real reader live, the refresh
    # below would read the store paths import bound to the real home.
    assert (
        reader is not _PRODUCTION_CANDIDATE_TOKENS
    ), "integration_home left kiro-cli's real credential reader live"
    # Every root the import scan resolves, and OpenClaw's config and workspace
    # paths, lie outside the outer home. A dropped override whose adapter only
    # stats the files it looks for would not show up as a read.
    scan_home, scan_roots = onboarding_sources._source_roots(None, None)
    resolved = [str(scan_home), *(str(root) for root in scan_roots.values())]
    for source_id, root in scan_roots.items():
        configs, workspaces = onboarding_sources._source_context(
            source_id, root, scan_home, os.environ
        )
        resolved += [*(str(path) for path in configs), *(str(path) for path in workspaces)]
    assert not outer(resolved), f"the import scan resolves into the outer home: {outer(resolved)}"
    # No AWS or GitHub identity from the outer environment reaches the boot.
    # Checked before booting, so a regression sends nothing anywhere.
    carried = sorted(key for key, value in os.environ.items() if value.startswith("outer-"))
    assert not carried, f"credentials from the outer environment reach the boot: {carried}"
    aws_files = [os.environ["AWS_CONFIG_FILE"], os.environ["AWS_SHARED_CREDENTIALS_FILE"]]
    assert not outer(aws_files), f"the aws CLI would read the outer home: {outer(aws_files)}"
    # The fake backend runs as ``#!/usr/bin/env python3``: under the new home it
    # must reach this interpreter, not a version-manager shim further down.
    assert os.environ["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable)

    def counting_reader():
        reached.append(True)
        return reader()

    async def answered(what: str, ceiling: str, request: Awaitable[Any]) -> Any:
        """*request*'s response, or a failure naming the ceiling it outlived."""
        started = time.monotonic()
        try:
            return await request
        except asyncio.TimeoutError:
            pytest.fail(
                f"{what} did not answer within {ceiling} "
                f"(waited {time.monotonic() - started:.1f}s)"
            )

    monkeypatch.setattr(kiro_usage_api, "_candidate_tokens", counting_reader)
    if not _path_read_hook_installed:
        sys.addaudithook(_record_path_reads)
        _path_read_hook_installed = True
    reads: list[str] = []
    _path_reads = reads
    try:
        # The recorder sees a read under the outer home before it is trusted
        # to report none.
        (outer_home / ".claude" / "projects" / "outer" / "session.jsonl").read_bytes()
        assert outer(reads), "the audit hook recorded no read under the outer home"
        reads.clear()
        async with gateway_boot() as gw:
            scan = await answered(
                "the import scan",
                f"_SCAN_SECS={_SCAN_SECS:.0f}s",
                gw.get("/api/onboarding/import/scan", timeout=_SCAN_SECS),
            )
            assert scan.status == 200, await scan.text()
            refresh = await answered(
                "the credit refresh",
                f"_REFRESH_SECS={_REFRESH_SECS:.0f}s",
                gw.post("/api/sessions/usage/refresh", timeout=_REFRESH_SECS),
            )
            assert refresh.status == 200, await refresh.text()
    finally:
        _path_reads = None
    assert reached, "the credit refresh never reached the credential reader"
    assert not outer(reads), f"the boot read the outer home: {outer(reads)}"
    anchored = {
        os.path.normcase(str(db))
        for db in (*kiro_usage_api._CLI_SQLITE_DBS, *kiro_usage_api._OTHER_SQLITE_DBS)
    }
    opened = sorted({path for path in reads if os.path.normcase(path) in anchored})
    assert not opened, f"the refresh opened kiro-cli's own credential stores: {opened}"


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
    ("kiro_crew.dashboard.handlers.updates", "_last_update_check"): "clock",
    # When telemetry consent was last re-read, and whether a re-read is in
    # flight: a boot that outlasts the recheck window starts one. The consent
    # itself (``_built_consent``) does not change here.
    ("kiro_crew.metrics.provider", "_check_in_flight"): "clock-driven recheck",
    ("kiro_crew.metrics.provider", "_consent_checked_at"): "clock",
    ("kiro_crew.platform.context", "_GOVERNANCE_GENERATION"): "counter",
    ("kiro_crew.platform.governance_profiles", "_PROFILE_GENERATION"): "counter",
    # Host facts re-probed on their own TTL or backoff: the user login session
    # behind cgroup scopes and the machine's own host names. A boot that
    # outlasts the window probes again, or a lookup started in one boot lands
    # in the next; neither is derived from the home.
    ("kiro_crew.sandbox", "_CGROUP_SCOPE_PROBE_AT"): "host probe clock",
    ("kiro_crew.sandbox", "_CPU_DELEGATED"): "host probe, re-read after a re-probe",
    ("kiro_crew.security.ssh_self_floor", "_OWN_HOST_NAMES_CACHE"): "host names",
    ("kiro_crew.security.ssh_self_floor", "_OWN_HOST_RESOLVE_DONE"): "host names",
    ("kiro_crew.security.ssh_self_floor", "_OWN_HOST_RESOLVE_IN_FLIGHT"): "host names",
    ("kiro_crew.security.ssh_self_floor", "_OWN_HOST_RESOLVE_NEXT_TRY"): "host probe clock",
    ("kiro_crew.security.ssh_self_floor", "_OWN_HOST_RESOLVE_STAMP"): "host probe clock",
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
async def test_a_boot_inherits_no_cooldown_or_cached_count(gateway_boot) -> None:
    """A path-resolver stall cooldown, a load-probe count, a thread's wait
    allowance and the cached status counts are what a fresh process starts
    without, so a boot here must start without them too. The witness above
    cannot see most of that: an inherited dict entry changes no identity and,
    once the boot adds its own, no size. So one of each is seeded under a key
    no boot uses, and both reset points must drop it."""
    from kiro_crew.dashboard import status_counts
    from kiro_crew.security import paths

    prefix = paths._stall_prefix(os.path.join(os.sep, "kc-integration-unresolved", "leaf"))
    synthetic_tid = -1  # thread idents are positive, so no real caller collides
    seeded_counts = (987_654, 876_543)

    def _seed() -> None:
        until = paths._path_resolve_clock() + 1_000.0
        with paths._path_resolve_lock:
            paths._path_resolve_degraded[prefix] = (until, 1)
            paths._path_resolve_load_probes[prefix] = (until, 1)
            paths._path_resolve_thread_waits[synthetic_tid] = (until, 1.0)
        status_counts._counts_cache = seeded_counts
        status_counts._counts_cache_ts = time.monotonic()

    def _still_seeded() -> list[str]:
        held = {
            "_path_resolve_degraded": prefix in paths._path_resolve_degraded,
            "_path_resolve_load_probes": prefix in paths._path_resolve_load_probes,
            "_path_resolve_thread_waits": synthetic_tid in paths._path_resolve_thread_waits,
            "_counts_cache": status_counts._counts_cache == seeded_counts,
        }
        return [name for name, present in held.items() if present]

    def _unseed() -> None:
        """Drop whatever seed is left, so a failure here cannot reach the next test."""
        with paths._path_resolve_lock:
            paths._path_resolve_degraded.pop(prefix, None)
            paths._path_resolve_load_probes.pop(prefix, None)
            paths._path_resolve_thread_waits.pop(synthetic_tid, None)
        if status_counts._counts_cache == seeded_counts:
            status_counts._counts_cache = (None, None)
            status_counts._counts_cache_ts = float("-inf")

    try:
        async with gateway_boot():
            _seed()
        kept = _still_seeded()
        assert not kept, f"teardown kept the ended boot's state: {kept}"
        _seed()
        async with gateway_boot():
            inherited = _still_seeded()
            assert not inherited, f"the boot started with the previous boot's state: {inherited}"
    finally:
        _unseed()


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
