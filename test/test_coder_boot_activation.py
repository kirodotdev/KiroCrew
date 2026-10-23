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


def config(url=""):
    return SimpleNamespace(session_environments=SimpleNamespace(coder_url=url))


@pytest.mark.asyncio
async def test_disabled_coder_does_not_import_or_start_retention(monkeypatch):
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    real_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        assert not name.startswith("kiro_crew.coder"), "disabled Coder imported"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    gateway._start_coder_retention(config())
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
        ("session_environments.coder_url",),
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
