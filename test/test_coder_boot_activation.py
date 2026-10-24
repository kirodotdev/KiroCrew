"""Optional Coder retention starts after readiness or a later configuration save."""

from __future__ import annotations

import ast
import asyncio
import builtins
import inspect
import textwrap
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiro_crew.config.live import ConfigChange
from kiro_crew.slack import gateway


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


def config(url="", **lists):
    return SimpleNamespace(session_environments=SimpleNamespace(coder_url=url, **lists))


@pytest.mark.asyncio
async def test_disabled_coder_does_not_import_or_start_retention(monkeypatch):
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    real_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        assert not name.startswith("kiro_crew.coder"), "disabled Coder imported"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    monkeypatch.setattr(gateway.live, "snapshot", lambda: None)
    gateway._start_coder_retention(config())
    if gateway._CODER_RETENTION_TASK is not None:
        await asyncio.wait_for(gateway._CODER_RETENTION_TASK, 30)
    assert gateway._CODER_RETENTION_TASK is None
    await gateway._stop_session_environments()


@pytest.mark.asyncio
async def test_late_configuration_activates_retention_once_after_readiness(monkeypatch):
    from kiro_crew.coder import retention

    started = asyncio.Event()
    parked = asyncio.Event()
    calls = []

    async def sweep():
        calls.append("sweep")
        started.set()
        await parked.wait()

    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    monkeypatch.setattr(retention, "run_sweeper", sweep)
    owner = object.__new__(gateway.GatewayOrchestrator)
    owner._session_environments_ready = False
    owner._test_mode = False
    change = ConfigChange(
        config(),
        config("https://coder.example.test"),
        frozenset({"session_environments.coder_url"}),
    )
    callback = gateway.GatewayOrchestrator._on_session_environment_config_change
    callback(owner, change)
    assert gateway._CODER_RETENTION_TASK is None
    owner._session_environments_ready = True
    try:
        callback(owner, change)
        await asyncio.wait_for(started.wait(), 30)
        task = gateway._CODER_RETENTION_TASK
        callback(owner, change)
        assert gateway._CODER_RETENTION_TASK is task
        assert calls == ["sweep"]
    finally:
        parked.set()
        if gateway._CODER_RETENTION_TASK is not None:
            await asyncio.wait_for(gateway._CODER_RETENTION_TASK, 30)


def test_config_applier_subscribes_the_activation_callback(monkeypatch):
    callbacks = []

    def subscribe(*prefixes, callback, name):
        callbacks.append((prefixes, callback))
        return Mock()

    monkeypatch.setattr(gateway.live, "subscribe", subscribe)
    owner = object.__new__(gateway.GatewayOrchestrator)
    monkeypatch.setattr(owner, "_on_session_environment_config_change", Mock())
    monkeypatch.setattr(owner, "_on_channel_config_change", Mock())
    monkeypatch.setattr(owner, "_on_slack_config_change", Mock())
    monkeypatch.setattr(owner, "_on_default_agent_config_change", Mock())
    gateway.GatewayOrchestrator._register_config_appliers(owner)
    assert (
        (
            "session_environments.coder_url",
            "session_environments.sprites_profiles",
            "session_environments.boxd_profiles",
            "session_environments.fargate_profiles",
        ),
        owner._on_session_environment_config_change,
    ) in callbacks


def test_retention_is_not_imported_on_gateway_boot_and_activation_follows_ready():
    boot = ast.parse(textwrap.dedent(inspect.getsource(gateway.run_gateway)))
    assert not any(
        isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kiro_crew.coder")
        for node in ast.walk(boot)
    )
    source = inspect.getsource(gateway.GatewayOrchestrator.run)
    assert source.index('print(f"KIROCREW_READY:') < source.index("_start_coder_retention(")
    assert "_start_coder_retention(live.snapshot() or self._cfg)" in source


def test_test_mode_never_activates_compute(monkeypatch):
    start = Mock()
    monkeypatch.setattr(gateway, "_start_coder_retention", start)
    owner = object.__new__(gateway.GatewayOrchestrator)
    owner._session_environments_ready = True
    owner._test_mode = True
    gateway.GatewayOrchestrator._on_session_environment_config_change(
        owner, ConfigChange(None, config("https://coder.example.test"), frozenset())
    )
    start.assert_not_called()


@pytest.mark.asyncio
async def test_sprites_profiles_alone_activates_retention(monkeypatch):
    from kiro_crew.coder import retention

    started = asyncio.Event()
    parked = asyncio.Event()

    async def sweep():
        started.set()
        await parked.wait()

    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    monkeypatch.setattr(retention, "run_sweeper", sweep)
    try:
        gateway._start_coder_retention(config(sprites_profiles=[{"id": "p"}]))
        await asyncio.wait_for(started.wait(), 30)
    finally:
        parked.set()
        if gateway._CODER_RETENTION_TASK is not None:
            await asyncio.wait_for(gateway._CODER_RETENTION_TASK, 30)


@pytest.mark.asyncio
async def test_boxd_profiles_alone_activates_retention(monkeypatch):
    from kiro_crew.coder import retention

    started = asyncio.Event()
    parked = asyncio.Event()

    async def sweep():
        started.set()
        await parked.wait()

    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    monkeypatch.setattr(retention, "run_sweeper", sweep)
    try:
        gateway._start_coder_retention(config(boxd_profiles=[{"id": "p"}]))
        await asyncio.wait_for(started.wait(), 30)
    finally:
        parked.set()
        if gateway._CODER_RETENTION_TASK is not None:
            await asyncio.wait_for(gateway._CODER_RETENTION_TASK, 30)


@pytest.mark.asyncio
async def test_fargate_profiles_alone_activates_retention(monkeypatch):
    from kiro_crew.coder import retention

    started = asyncio.Event()
    parked = asyncio.Event()

    async def sweep():
        started.set()
        await parked.wait()

    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    monkeypatch.setattr(retention, "run_sweeper", sweep)
    try:
        gateway._start_coder_retention(config(fargate_profiles=[{"id": "p"}]))
        await asyncio.wait_for(started.wait(), 30)
    finally:
        parked.set()
        if gateway._CODER_RETENTION_TASK is not None:
            await asyncio.wait_for(gateway._CODER_RETENTION_TASK, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["sprites", "boxd"])
@pytest.mark.parametrize("live_id", ["owned-id", "replacement-id"])
async def test_registry_only_boot_sweeps_and_shutdown_keeps_owned_vms(
    tmp_path, monkeypatch, provider, live_id
):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder import persistent_vm, retention
    from kiro_crew.coder.persistent_vm import SessionResource, stamp
    from kiro_crew.config import paths
    from kiro_crew.sprites_session import launch as sprites_launch

    cfg = config(sprites_profiles=[], boxd_profiles=[])
    monkeypatch.setattr(gateway.live, "snapshot", lambda: cfg)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    monkeypatch.setattr(retention, "_last_vm_sweep_error", {})
    monkeypatch.setattr(persistent_vm, "_last_delete_error", {})
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(persistent_vm, "now_utc", lambda: now)
    cli = SimpleNamespace(
        identity=AsyncMock(return_value="acct"),
        get=AsyncMock(return_value={"id": live_id}),
        hibernate=AsyncMock(),
        remove=AsyncMock(),
    )
    monkeypatch.setattr(BoxdSessionEnvironments, "cli", AsyncMock(return_value=cli))
    token_reader = Mock(return_value="example-token")
    monkeypatch.setattr(sprites_launch, "_vault_token", token_reader)
    client = SimpleNamespace(
        get_sprite=AsyncMock(return_value={"id": live_id}), delete_sprite=AsyncMock()
    )
    monkeypatch.setattr(sprites_launch, "SpritesClient", Mock(return_value=client))
    environment_type = (
        sprites_launch.SpritesSessionEnvironments
        if provider == "sprites"
        else BoxdSessionEnvironments
    )
    env = environment_type(cfg.session_environments, tmp_path)
    active = SessionResource(
        "active", "removed-profile", env.name_prefix + "0123456789abcdef", "owned-id"
    )
    archived = SessionResource(
        "archived",
        "removed-profile",
        env.name_prefix + "fedcba9876543210",
        "owned-id",
        deletion_due_at=stamp(now),
        token_secret="ORIGINAL_TOKEN" if provider == "sprites" else "",
    )
    for row in (active, archived):
        await asyncio.to_thread(env.registry.put, row)

    swept = asyncio.Event()
    sweep_once = retention.sweep_once
    run_sweeper = retention.run_sweeper
    counts = []

    async def observe_sweep(**kwargs):
        counts.append(await sweep_once(**kwargs))
        swept.set()
        return counts[-1]

    async def run():
        await run_sweeper(initial_delay=0)

    monkeypatch.setattr(retention, "sweep_once", observe_sweep)
    monkeypatch.setattr(retention, "run_sweeper", run)
    task = None
    try:
        gateway._start_coder_retention(cfg)
        task = gateway._CODER_RETENTION_TASK
        assert task is not None, "saved VMs must keep maintenance enabled without profiles"
        await asyncio.wait_for(swept.wait(), 30)
        assert counts == [int(live_id == "owned-id")]
        assert await asyncio.to_thread(env.registry.get, "active") == active
        assert await asyncio.to_thread(env.registry.get, "archived") == (
            None if live_id == "owned-id" else archived
        )
        await gateway._stop_session_environments()
        assert task.cancelled()
        assert await asyncio.to_thread(env.registry.get, "active") == active
        delete = client.delete_sprite if provider == "sprites" else cli.remove
        if live_id == "owned-id":
            delete.assert_awaited_once_with(archived.name)
        else:
            delete.assert_not_awaited()
        if provider == "boxd" and live_id == "owned-id":
            cli.hibernate.assert_awaited_once_with(active.name)
        else:
            cli.hibernate.assert_not_awaited()
        if provider == "sprites":
            assert token_reader.call_args_list == [
                (("ORIGINAL_TOKEN",), {}),
            ] * (2 if live_id == "owned-id" else 1)
        else:
            token_reader.assert_not_called()
    finally:
        if task is not None:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registry",
    ["sprites/session-sprites.json", "boxd/session-machines.json", "fargate/session-tasks.json"],
)
async def test_registry_metadata_failure_keeps_retention_enabled(tmp_path, monkeypatch, registry):
    import threading
    from pathlib import Path
    from unittest.mock import AsyncMock

    from kiro_crew.coder import retention
    from kiro_crew.config import paths

    cfg = config()
    monkeypatch.setattr(gateway.live, "snapshot", lambda: cfg)
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    sweep = AsyncMock()
    monkeypatch.setattr(retention, "run_sweeper", sweep)
    loop_thread = threading.get_ident()
    stat = Path.stat
    probes = []

    def unreadable(path, *args, **kwargs):
        if path == tmp_path / registry:
            assert threading.get_ident() != loop_thread, "registry probe must stay off-loop"
            probes.append(path)
            raise PermissionError("registry metadata unavailable")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unreadable)
    gateway._start_coder_retention(cfg)
    task = gateway._CODER_RETENTION_TASK
    assert task is not None
    gateway._start_coder_retention(cfg)
    assert gateway._CODER_RETENTION_TASK is task
    await asyncio.wait_for(task, 30)
    assert probes == [tmp_path / registry]
    sweep.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["coder", "sprites", "boxd", "fargate"])
async def test_config_save_during_registry_probe_still_activates_retention(
    tmp_path, monkeypatch, provider
):
    import threading
    from unittest.mock import AsyncMock

    from kiro_crew.coder import retention
    from kiro_crew.config import paths

    cfg = config()
    monkeypatch.setattr(gateway.live, "snapshot", lambda: cfg)
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    sweep = AsyncMock()
    monkeypatch.setattr(retention, "run_sweeper", sweep)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def home():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(30), "configuration save never released the registry probe"
        return tmp_path

    monkeypatch.setattr(paths, "data_home", home)
    task = None
    try:
        gateway._start_coder_retention(cfg)
        task = gateway._CODER_RETENTION_TASK
        assert task is not None
        await asyncio.wait_for(entered.wait(), 30)
        old = cfg
        cfg = (
            config("https://coder.example.test")
            if provider == "coder"
            else config(**{f"{provider}_profiles": [{"id": "saved"}]})
        )
        owner = object.__new__(gateway.GatewayOrchestrator)
        owner._session_environments_ready = True
        owner._test_mode = False
        gateway.GatewayOrchestrator._on_session_environment_config_change(
            owner, ConfigChange(old, cfg, frozenset())
        )
        assert gateway._CODER_RETENTION_TASK is task
    finally:
        release.set()
        if task is not None:
            await asyncio.wait_for(task, 30)
    sweep.assert_awaited_once_with()
