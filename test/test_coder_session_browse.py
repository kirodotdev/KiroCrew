"""Browse and terminal targets must still match the saved Coder identity."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.coder import session_browse, session_launch
from kiro_crew.coder.client import CoderClient, CoderClientError
from kiro_crew.config import live, paths
from kiro_crew.session_environment import SessionEnvironmentUnavailable

SESSION = "dashboard:remote"
URL = "https://coder.example"


def ssh_argv(name):
    return ["coder", "--url", URL, "ssh", "--disable-autostart", name]


@pytest.fixture
def browse_target(tmp_path, monkeypatch):
    config = SimpleNamespace(
        coder_url=URL,
        profiles=[SimpleNamespace(id="dev", template="micro", workdir="/workspace/app")],
    )
    provider = session_launch.CoderSessionEnvironments(config, tmp_path)
    provider.choices.set(SESSION, "dev")
    binding = provider.registry.allocate(
        SESSION, template="micro", preset="", prefix="crewenv", owner_name="dev", deployment_id=URL
    )
    provider.registry.replace(
        replace(binding, workspace_uuid="workspace-id", owner_id="owner-id", state="running")
    )
    record = {
        "id": "workspace-id",
        "name": binding.workspace_name,
        "owner_id": "owner-id",
        "template_name": "micro",
        "status": "running",
    }
    runner = AsyncMock(side_effect=lambda *_: json.dumps([record]).encode())
    client = CoderClient("coder", URL, "", tmp_path, runner=runner)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=config))
    monkeypatch.setattr(session_launch, "resolve_coder_bin", lambda: "coder")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: client)
    guest = AsyncMock(return_value=b'{"ok": true}')
    monkeypatch.setattr(session_browse, "run_collect", guest)
    return config, record, runner, guest


async def _access(operation):
    if operation == "browse":
        return await session_browse.browse_session(SESSION, "list", ".")
    if operation == "write":
        return await session_browse.write_session_file(SESSION, "a.txt", "new", "old")
    return await session_browse.remote_terminal_argv(SESSION)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["browse", "write", "terminal"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "replacement"), ("owner_id", "another-owner"), ("template_name", "another-template")],
)
async def test_rebound_workspace_never_executes(browse_target, operation, field, value):
    _config, record, runner, guest = browse_target
    record[field] = value
    with pytest.raises(SessionEnvironmentUnavailable, match="identity has changed"):
        await _access(operation)
    runner.assert_awaited_once()
    assert runner.call_args.args[0] == ["coder", "list", "--output", "json"]
    guest.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["browse", "write", "terminal"])
async def test_original_workspace_is_accessible_with_one_read(
    browse_target, operation, monkeypatch
):
    monkeypatch.setenv("CODER_URL", "https://unverified-coder.example")
    _config, record, runner, guest = browse_target
    result = await _access(operation)
    runner.assert_awaited_once()
    assert runner.call_args.args[0] == ["coder", "list", "--output", "json"]
    if operation == "terminal":
        assert result == ssh_argv(record["name"])
        guest.assert_not_awaited()
    else:
        assert result == {"ok": True}
        assert guest.call_args.args[0][:6] == ssh_argv(record["name"])


@pytest.mark.asyncio
async def test_deleted_workspace_is_not_recreated(browse_target):
    _config, _record, runner, guest = browse_target
    runner.side_effect = None
    runner.return_value = b"[]"
    with pytest.raises(SessionEnvironmentUnavailable, match="identity has changed"):
        await _access("browse")
    runner.assert_awaited_once()
    guest.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_identity_lookup_never_executes(browse_target):
    _config, _record, runner, guest = browse_target
    runner.side_effect = CoderClientError("offline")
    with pytest.raises(SessionEnvironmentUnavailable, match="could not verify"):
        await _access("write")
    runner.assert_awaited_once()
    guest.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_deployment_is_refused_before_lookup(browse_target):
    config, _record, runner, guest = browse_target
    config.coder_url = "https://other-coder.example"
    with pytest.raises(SessionEnvironmentUnavailable, match="deployment has changed"):
        await _access("terminal")
    runner.assert_not_awaited()
    guest.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_workspace_is_not_started(browse_target):
    _config, record, runner, guest = browse_target
    record["status"] = "stopped"
    result = await _access("terminal")
    assert result == ssh_argv(record["name"])
    runner.assert_awaited_once()
    assert runner.call_args.args[0] == ["coder", "list", "--output", "json"]
    guest.assert_not_awaited()
