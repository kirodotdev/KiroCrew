"""The unsaved-work read must still match the saved Coder identity before any SSH."""

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


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


pytestmark = pytest.mark.asyncio

SESSION = "dashboard:remote"
URL = "https://coder.example"
GIT_STATUS = {"git": True, "output": "## main...origin/main [ahead 1]\n"}


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

    def answer(argv, *_):
        if "whoami" in argv:
            return json.dumps([{"username": "dev", "user_id": "owner-id"}]).encode()
        return json.dumps([record]).encode()

    runner = AsyncMock(side_effect=answer)
    client = CoderClient("coder", URL, "", tmp_path, runner=runner)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=config))
    monkeypatch.setattr(session_launch, "resolve_coder_bin", lambda: "coder")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: client)
    guest = AsyncMock(return_value=json.dumps(GIT_STATUS).encode())
    monkeypatch.setattr(session_browse, "run_collect", guest)
    return config, record, runner, guest


async def _read():
    return await session_browse.unsaved_work(SESSION)


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "replacement"), ("owner_id", "another-owner"), ("template_name", "another-template")],
)
async def test_rebound_workspace_never_executes(browse_target, field, value):
    _config, record, runner, guest = browse_target
    record[field] = value
    with pytest.raises(SessionEnvironmentUnavailable, match="identity has changed"):
        await _read()
    assert runner.call_args_list[0].args[0] == ["coder", "list", "--output", "json"]
    guest.assert_not_awaited()


async def test_original_workspace_is_read_with_one_lookup(browse_target, monkeypatch):
    monkeypatch.setenv("CODER_URL", "https://unverified-coder.example")
    _config, record, runner, guest = browse_target
    assert await _read() == "1 unpushed commit"
    runner.assert_awaited_once()
    assert runner.call_args.args[0] == ["coder", "list", "--output", "json"]
    assert guest.call_args.args[0][:6] == ssh_argv(record["name"])
    assert json.loads(guest.call_args.args[1]) == {"op": "git_status", "path": "."}


@pytest.mark.parametrize("owner", ["dev", "someone-else"])
async def test_a_record_without_owner_id_is_matched_by_owner_name(browse_target, owner):
    # Older Coder omits owner_id in `coder list`; the record then names the owner.
    _config, record, _runner, guest = browse_target
    del record["owner_id"]
    record["owner_name"] = owner
    if owner == "dev":
        await _read()
    else:
        with pytest.raises(SessionEnvironmentUnavailable, match="identity has changed"):
            await _read()
        guest.assert_not_awaited()


async def test_deleted_workspace_is_not_recreated(browse_target):
    _config, _record, runner, guest = browse_target
    runner.side_effect = None
    runner.return_value = b"[]"
    with pytest.raises(SessionEnvironmentUnavailable, match="identity has changed"):
        await _read()
    runner.assert_awaited_once()
    guest.assert_not_awaited()


async def test_failed_identity_lookup_never_executes(browse_target):
    _config, _record, runner, guest = browse_target
    runner.side_effect = CoderClientError("offline")
    with pytest.raises(SessionEnvironmentUnavailable, match="could not verify"):
        await _read()
    runner.assert_awaited_once()
    guest.assert_not_awaited()


async def test_changed_deployment_is_refused_before_lookup(browse_target):
    config, _record, runner, guest = browse_target
    config.coder_url = "https://other-coder.example"
    with pytest.raises(SessionEnvironmentUnavailable, match="deployment has changed"):
        await _read()
    runner.assert_not_awaited()
    guest.assert_not_awaited()


async def test_stopped_workspace_is_not_started(browse_target):
    _config, record, runner, guest = browse_target
    record["status"] = "stopped"
    await _read()
    runner.assert_awaited_once()
    assert runner.call_args.args[0] == ["coder", "list", "--output", "json"]
    assert guest.call_args.args[0][:6] == ssh_argv(record["name"])
