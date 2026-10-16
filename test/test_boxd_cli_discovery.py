"""Executable discovery is off-loop and shared by a boxd provider's callers."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.boxd_session import cli as boxd_cli
from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
from kiro_crew.coder import retention
from kiro_crew.coder.persistent_vm import SessionResource
from kiro_crew.dashboard.handlers import session_environments as handlers

NAME = "kc-0123456789abcdef"
PROFILE = {"id": "box", "name": "Box", "snapshot": "", "vcpu": 0, "workdir": "/w"}


@pytest.fixture
def discovery(monkeypatch, tmp_path):
    loop_thread = threading.get_ident()
    calls = []
    binary = str(tmp_path / "boxd")

    def resolve():
        assert threading.get_ident() != loop_thread, "boxd discovery ran on the event loop"
        calls.append(binary)
        return binary

    monkeypatch.setattr(boxd_cli, "resolve_boxd_bin", resolve)
    run = AsyncMock(return_value=(0, "{}"))
    monkeypatch.setattr(boxd_cli.BoxdCLI, "_run", run)
    return binary, calls, run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        ("check_ready", (PROFILE,)),
        ("remote_get", (PROFILE, NAME)),
        ("remote_create", (PROFILE, NAME)),
        ("remote_delete", (PROFILE, NAME)),
        ("remote_kept", (PROFILE, NAME)),
        ("remote_archived", (PROFILE, NAME, 3)),
        ("shell", (PROFILE, NAME)),
    ],
)
async def test_provider_callers_resolve_off_loop_and_reuse_cli(tmp_path, discovery, method, args):
    binary, calls, _ = discovery
    environments = BoxdSessionEnvironments(SimpleNamespace(boxd_profiles=[PROFILE]), tmp_path)
    await getattr(environments, method)(*args)
    first, second = await asyncio.wait_for(
        asyncio.gather(environments.cli(), environments.cli()), timeout=30
    )
    assert first is second and first.bin == binary
    assert calls == [binary]


@pytest.mark.asyncio
async def test_concurrent_cli_access_discovers_once(tmp_path, discovery):
    binary, calls, _ = discovery
    environments = BoxdSessionEnvironments(SimpleNamespace(boxd_profiles=[PROFILE]), tmp_path)
    clients = await asyncio.wait_for(
        asyncio.gather(*(environments.cli() for _ in range(4))), timeout=30
    )
    assert all(client is clients[0] for client in clients)
    assert calls == [binary]


@pytest.mark.asyncio
async def test_readiness_constructs_cli_off_loop(discovery):
    binary, calls, run = discovery
    run.return_value = (0, "[]")
    assert await handlers._check_boxd(SimpleNamespace(boxd_profiles=[PROFILE])) == [
        {"id": "box", "ok": True}
    ]
    assert calls == [binary]
    run.assert_awaited_once_with("machine", "list", "--json", timeout=60)


@pytest.mark.asyncio
async def test_shutdown_resolves_cli_off_loop(tmp_path, monkeypatch, discovery):
    from kiro_crew.config import paths

    binary, calls, run = discovery
    section = SimpleNamespace(coder_url="", boxd_profiles=[PROFILE])
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    monkeypatch.setattr(retention, "_section", AsyncMock(return_value=section))
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    environments = BoxdSessionEnvironments(section, tmp_path)
    row = SessionResource("session", "box", NAME, "id", "2026-01-01T00:00:00Z")
    await asyncio.to_thread(environments.registry.put, row)
    run.return_value = (0, '{"id": "id"}')
    assert await retention.stop_for_shutdown() == {"coder": 0, "boxd": 1, "skipped": 0}
    assert calls == [binary]
    assert [invocation.args for invocation in run.await_args_list] == [
        ("machine", "get", NAME, "--json"),
        ("machine", "hibernate", NAME, "--json"),
    ]
    assert [invocation.kwargs for invocation in run.await_args_list] == [{"timeout": 60}, {}]
    assert await asyncio.to_thread(environments.registry.get, "session") == row
