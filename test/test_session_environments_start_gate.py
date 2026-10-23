"""Placement survives cleared config."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.coder import session_launch
from kiro_crew.config import live, paths
from kiro_crew.providers import acp as acp_facade
from kiro_crew.providers import acp_runtime_start as start
from kiro_crew.session_environment import SessionEnvironmentUnavailable, forget_remote_workspace


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


KEY = "dashboard:chat-gate"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    section = SimpleNamespace(coder_url="", profiles=[])
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=section))
    runtime = MagicMock(spawn=AsyncMock(side_effect=RuntimeError("spawn reached")))
    # Through the facade: AcpRuntime is bound by several mirrored owners.
    monkeypatch.setattr(acp_facade, "AcpRuntime", MagicMock(return_value=runtime))
    provider = MagicMock(memory_mode="persistent", _shared_scratch=tmp_path)
    provider._client._session_key = KEY
    provider._client._resume_session_id = ""
    yield section, provider, runtime
    forget_remote_workspace(KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
@pytest.mark.parametrize(
    "field,choice",
    [
        ("", "absent"),
        ("coder_url", "nofile"),
        ("", "bound"),
        ("", "bad"),
        ("coder_url", "bad"),
        ("profiles", "bad"),
        ("coder_url", "ready"),
    ],
)
async def test_start_gate(setup, tmp_path, monkeypatch, backend, field, choice):
    section, provider, runtime = setup
    provider._client.backend = backend
    if field:
        setattr(section, field, "https://coder.example" if field == "coder_url" else [object()])
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder/session-choices.json")
    launch = MagicMock(workspace_server=AsyncMock(return_value={}))
    if choice == "absent":
        monkeypatch.setattr(
            choices.__class__, "_read", MagicMock(side_effect=AssertionError("read"))
        )
    elif choice == "ready":
        prepared = SimpleNamespace(
            launch=launch, workdir="/workspace/app", profile_name="", created=False
        )
        monkeypatch.setattr(session_launch, "prepare_for_session", AsyncMock(return_value=prepared))
    elif choice == "nofile":
        # Configured: the choices are read, hold no binding, and the chat starts here.
        assert not choices.path.exists()
    else:
        choices.set(KEY, "dev")
        if choice == "bad":
            choices.path.write_text("{", encoding="utf-8")
    spawns = choice in ("absent", "nofile", "ready")
    with pytest.raises(
        RuntimeError if spawns else SessionEnvironmentUnavailable,
        match="spawn reached" if spawns else "not configured|unreadable",
    ):
        await asyncio.wait_for(start._start_kiro_runtime_impl(provider, {}, {}), 30)
    assert runtime.spawn.await_count == spawns
    if choice == "ready":
        runtime.set_remote_workspace.assert_called_once()
        [call] = launch.close_relay_when_ended.call_args_list
        assert call.args[0] is runtime.is_alive
        # Its placement entry leaves with this runtime, and only this runtime's.
        assert call.args[1].keywords == {"owner": runtime}
    else:
        runtime.set_remote_workspace.assert_not_called()


def test_an_unreadable_choices_path_counts_as_present(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    assert start._choices_file_may_exist() is False
    (tmp_path / "coder").write_text("", encoding="utf-8")  # a file where the dir should be
    assert start._choices_file_may_exist() is False
    (tmp_path / "coder").unlink()

    class Unstatable:
        def stat(self):
            raise PermissionError(13, "denied")

    monkeypatch.setattr("kiro_crew.session_environment.choices_path", Unstatable)
    assert start._choices_file_may_exist() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["absent", "local", "bound", "bad"])
async def test_an_adapter_backend_refuses_a_remote_session_before_its_engine(
    tmp_path, monkeypatch, choice
):
    from kiro_crew.acp.runtime import AcpRuntimeError
    from kiro_crew.providers import acp_lifecycle
    from kiro_crew.session_environment import REMOTE_NEEDS_KAS

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder/session-choices.json")
    if choice == "absent":  # an unconfigured gateway never reads the file
        monkeypatch.setattr(type(choices), "_read", MagicMock(side_effect=AssertionError("read")))
    else:
        choices.set("dashboard:other" if choice == "local" else KEY, "dev")
        if choice == "bad":
            choices.path.write_text("{", encoding="utf-8")
    provider = MagicMock(memory_mode="persistent", _disposable_work_dir=False)
    provider.is_acp_runtime_backend = False
    provider._client._session_key = KEY
    provider._client.ensure_ready = AsyncMock(side_effect=RuntimeError("engine started"))
    remote = choice in ("bound", "bad")
    with pytest.raises(AcpRuntimeError if remote else RuntimeError) as raised:
        await acp_lifecycle.start(provider)
    assert str(raised.value) == (REMOTE_NEEDS_KAS if remote else "engine started")
    assert provider._client.ensure_ready.await_count == (not remote)
