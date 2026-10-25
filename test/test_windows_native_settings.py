"""Real Windows sharing checks around protected settings; all data is synthetic."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading

import pytest

from kiro_crew import windows_settings
from kiro_crew.atomic_write import atomic_write
from kiro_crew.sandbox_windows_launcher import pinned_boundaries

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")


@pytest.fixture
def settings_home(monkeypatch, tmp_path):
    home = tmp_path / "synthetic-crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(windows_settings, "_home", lambda: home)
    return home


def test_competing_gateway_cannot_replace_live_transition_or_publish_stale_status(settings_home):
    first = windows_settings.claim_transition(
        {"transition_id": "first", "status": "stopping", "starts_blocked": True}
    )
    with pytest.raises(windows_settings.NativeSettingsTransitionBusy):
        windows_settings.claim_transition({"transition_id": "second", "status": "stopping"})
    assert windows_settings.status() == first
    windows_settings.record_status(
        {**first, "status": "active", "starts_blocked": False}, expected_transition="first"
    )
    second = windows_settings.claim_transition(
        {"transition_id": "second", "status": "stopping", "starts_blocked": True}
    )
    with pytest.raises(windows_settings.NativeSettingsSuperseded):
        windows_settings.record_status(
            {**first, "status": "activation_failed"}, expected_transition="first"
        )
    assert windows_settings.status() == second


def test_abandoned_owner_generation_can_be_recovered_without_killing_foreign_pid(
    settings_home, monkeypatch
):
    from kiro_crew import platform_compat

    windows_settings.record_status(
        {
            "transition_id": "abandoned",
            "status": "saved",
            "starts_blocked": True,
            "owner_pid": 12345,
            "owner_start": "old-generation",
        }
    )
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "new-generation")
    recovered = windows_settings.claim_transition(
        {"transition_id": "recovery", "status": "stopping", "starts_blocked": True}
    )
    assert recovered["transition_id"] == "recovery"


@pytest.mark.parametrize("name", windows_settings.DOCUMENTS)
def test_live_read_lease_refuses_publish_without_losing_old_document(settings_home, name):
    path = settings_home / name
    atomic_write(path, '{"old":true}')
    gate = windows_settings.prepare_gate()
    with windows_settings.read_gate(gate):
        with pytest.raises(windows_settings.NativeSettingsRestartRequired):
            atomic_write(path, '{"new":true}')
        assert json.loads(path.read_text()) == {"old": True}
        assert not list(settings_home.glob("*.tmp"))
    atomic_write(path, '{"new":true}')
    assert json.loads(path.read_text()) == {"new": True}


def test_legacy_no_delete_pin_is_reported_before_replace(settings_home):
    path = settings_home / "config.json"
    atomic_write(path, '{"old":true}')
    with pinned_boundaries([str(path)]):
        with pytest.raises(windows_settings.NativeSettingsRestartRequired) as error:
            atomic_write(path, '{"new":true}')
        assert error.value.step == "release_file_pins"
    atomic_write(path, '{"new":true}')
    assert json.loads(path.read_text()) == {"new": True}


def test_writer_prevents_new_native_launch_and_only_its_context_can_publish(settings_home):
    gate = windows_settings.prepare_gate()
    with windows_settings.write_transaction():
        with pytest.raises(windows_settings.NativeSettingsRestartRequired):
            with windows_settings.read_gate(gate):
                pytest.fail("reader entered a publication transaction")
        atomic_write(settings_home / "agent_model_state.json", "{}")
    with windows_settings.read_gate(gate):
        assert not windows_settings.permitted()


def test_stale_prepared_launch_is_rejected_after_settings_commit(settings_home):
    atomic_write(settings_home / "config.json", '{"generation":1}')
    old = windows_settings.snapshot()
    atomic_write(settings_home / "config.json", '{"generation":2}')
    gate = windows_settings.prepare_gate()
    with (
        windows_settings.read_gate(gate),
        pytest.raises(windows_settings.NativeSettingsRestartRequired) as error,
    ):
        windows_settings.verify_snapshot(gate, old)
    assert error.value.step == "stale_start"


def test_activation_failure_and_corrupt_state_keep_native_admission_closed(settings_home):
    gate = windows_settings.prepare_gate()
    windows_settings.record_status({"status": "activation_failed", "starts_blocked": True})
    with pytest.raises(windows_settings.NativeSettingsRestartRequired):
        with windows_settings.read_gate(gate):
            pass
    atomic_write(settings_home / "run" / windows_settings.STATUS_LEAF, "broken json")
    assert windows_settings.status()["failed_step"] == "read_activation_state"
    with pytest.raises(windows_settings.NativeSettingsRestartRequired):
        with windows_settings.read_gate(gate):
            pass
    windows_settings.record_status({"status": "active", "starts_blocked": False})
    with windows_settings.read_gate(gate):
        pass


@pytest.mark.asyncio
async def test_transaction_permit_does_not_leak_to_concurrent_writer(settings_home):
    gate = windows_settings.prepare_gate()
    ready, release = asyncio.Event(), asyncio.Event()

    async def owner():
        with windows_settings.write_transaction():
            ready.set()
            await release.wait()
            await asyncio.to_thread(atomic_write, settings_home / "config.json", "{}")

    task = asyncio.create_task(owner())
    await ready.wait()
    try:
        with pytest.raises(windows_settings.NativeSettingsRestartRequired):
            await asyncio.to_thread(atomic_write, settings_home / "agent_model_state.json", "{}")
        assert not windows_settings.permitted()
    finally:
        release.set()
        await task
    with windows_settings.read_gate(gate):
        pass


def test_pending_activation_refuses_all_ordinary_writers(settings_home):
    windows_settings.record_status({"status": "saved", "starts_blocked": True})
    for name in windows_settings.DOCUMENTS:
        with pytest.raises(windows_settings.NativeSettingsRestartRequired) as error:
            atomic_write(settings_home / name, "{}")
        assert error.value.step == "activation"
        assert not (settings_home / name).exists()
    with windows_settings.write_transaction():
        atomic_write(settings_home / "config.json", '{"owned":true}')
    assert json.loads((settings_home / "config.json").read_text()) == {"owned": True}


@pytest.mark.asyncio
async def test_async_transaction_workers_inherit_permit_but_other_request_does_not(settings_home):
    windows_settings.record_status({"status": "saved", "starts_blocked": True})
    ready, release = asyncio.Event(), asyncio.Event()

    async def owner():
        async with windows_settings.async_write_transaction():
            ready.set()
            await release.wait()
            await asyncio.to_thread(atomic_write, settings_home / "config.json", "{}")

    task = asyncio.create_task(owner())
    await ready.wait()
    try:
        with pytest.raises(windows_settings.NativeSettingsRestartRequired):
            await asyncio.to_thread(atomic_write, settings_home / "agent_model_state.json", "{}")
    finally:
        release.set()
        await task
    assert not windows_settings.permitted()
    assert (settings_home / "config.json").read_text() == "{}"


@pytest.mark.asyncio
async def test_cancelled_activation_settles_rebuild_before_snapshot_and_lease_close(
    settings_home, monkeypatch
):
    from types import SimpleNamespace

    from kiro_crew import agent
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard import native_settings as transition

    cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(enabled=False), to_dict=lambda: {})
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    windows_settings.record_status(
        {"status": "saved", "starts_blocked": True, "documents": windows_settings.snapshot()}
    )
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def rebuild(**kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        atomic_write(settings_home / "agent_model_state.json", '{"bootstrap":true}')

    monkeypatch.setattr(agent, "rebuild_agent_config", rebuild)

    async def boot():
        async with transition.activation_boot(cfg):
            await transition.finish_activation(cfg, None, frozenset())

    task = asyncio.create_task(boot())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(windows_settings.NativeSettingsRestartRequired):
            atomic_write(settings_home / "config.json", "{}")
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    record = windows_settings.status()
    assert record["documents"] == windows_settings.snapshot()
    assert record["starts_blocked"] is True
    assert json.loads((settings_home / "agent_model_state.json").read_text()) == {"bootstrap": True}


def test_transition_claim_is_exclusive_across_real_processes(settings_home):
    program = (
        "import sys; from pathlib import Path; from kiro_crew import windows_settings as w; "
        "w.claim_transition({'status':'stopping','starts_blocked':True,'transition_id':'child'},Path(sys.argv[1])); "
        "print('claimed',flush=True); sys.stdin.readline()"
    )
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", program, str(settings_home)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert child.stdout.readline().strip() == "claimed"
        with pytest.raises(windows_settings.NativeSettingsTransitionBusy):
            windows_settings.claim_transition({"status": "stopping", "transition_id": "parent"})
        assert windows_settings.status()["transition_id"] == "child"
        child.stdin.close()
        assert child.wait(timeout=5) == 0
        recovered = windows_settings.claim_transition(
            {"status": "stopping", "transition_id": "parent"}
        )
        assert recovered["transition_id"] == "parent"
        assert recovered["prior_admission_blocked"] is True
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
        if not child.stdin.closed:
            child.stdin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["save", "broker"])
async def test_confirmed_transition_releases_pins_before_save_and_fences_failed_activation(
    settings_home, monkeypatch, failure
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiohttp import web

    from kiro_crew import agent
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard import native_settings as transition
    from kiro_crew.dashboard.handlers import updates

    path = settings_home / "config.json"
    atomic_write(path, '{"old":true}')
    base = windows_settings.snapshot()
    cfg = SimpleNamespace(
        mcp_gateway=SimpleNamespace(enabled=False, stub_servers=["kirocrew-core"]),
        to_dict=lambda: {"synthetic": True},
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(
        agent,
        "rebuild_agent_config",
        lambda **kwargs: atomic_write(settings_home / "agent_model_state.json", '{"rebuilt":true}'),
    )
    state = SimpleNamespace()
    pins = contextlib.ExitStack()
    pins.enter_context(windows_settings.read_gate(windows_settings.prepare_gate()))
    pins.enter_context(pinned_boundaries([str(path)]))

    async def mutation():
        if failure == "save":
            raise PermissionError("synthetic save refusal")
        await asyncio.to_thread(atomic_write, path, '{"new":true}')
        return web.json_response({"ok": True})

    async def restart(state, *, before_exec, strict_cleanup):
        assert strict_cleanup
        state._native_settings_stop_started = True
        pins.close()  # the mocked exec boundary has retired its real file pins
        await before_exec()
        async with transition.activation_boot(cfg):
            await transition.finish_activation(
                cfg,
                SimpleNamespace(is_running=True, ping=AsyncMock(return_value=False)),
                frozenset({"kirocrew-core"}),
            )
        return True

    monkeypatch.setattr(updates, "_restart_gateway", restart)
    try:
        await transition._apply(state, mutation, base, "synthetic-request", object(), "transition")
    finally:
        pins.close()
    record = windows_settings.status()
    assert record["status"] == "activation_failed"
    assert record["saved"] is (failure != "save")
    assert record["starts_blocked"] is True
    assert record["failed_step"] == ("save" if failure == "save" else "broker_activation")
    assert json.loads(path.read_text()) == ({"old": True} if failure == "save" else {"new": True})
    with pytest.raises(windows_settings.NativeSettingsRestartRequired):
        with windows_settings.read_gate(windows_settings.prepare_gate()):
            pass
