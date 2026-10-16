"""Session starts read the session choices only on a gateway with Session Environments."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session_environment import SessionEnvironmentUnavailable, forget_remote_workspace

KEY = "dashboard:chat-gate"
BACKENDS = pytest.mark.parametrize(
    "backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS], ids=["kiro", "kas"]
)


class _SpawnReached(Exception):
    pass


class _Runtime:
    made: list["_Runtime"] = []

    def __init__(self, **_kwargs):
        self.remote = None
        _Runtime.made.append(self)

    def check_remote_workspace_supported(self) -> None:
        return None

    def set_remote_workspace(self, launch) -> None:
        self.remote = launch

    def is_alive(self) -> bool:
        return False

    async def spawn(self, **_kwargs) -> None:
        raise _SpawnReached


def _start(backend: str) -> None:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock(backend=backend, _work_dir="/workspace/app", _agent="kirocrew")
    provider._client._resume_session_id = ""
    provider._client._session_key = KEY
    provider._client._model = ""
    asyncio.run(asyncio.wait_for(provider._start_kiro_runtime_impl({}, {}), 30))


@pytest.fixture
def section(tmp_path, monkeypatch):
    """A data home whose session choices file is not valid JSON."""
    from kiro_crew.config import live, paths

    (tmp_path / "coder").mkdir()
    (tmp_path / "coder" / "session-choices.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.providers.acp.AcpRuntime", _Runtime)
    _Runtime.made = []
    value = SimpleNamespace(coder_url="", profiles=[])
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=value))
    yield value
    forget_remote_workspace(KEY)


@BACKENDS
def test_unconfigured_gateway_starts_despite_unreadable_choices(section, backend):
    with pytest.raises(_SpawnReached):
        _start(backend)
    assert _Runtime.made[0].remote is None


@pytest.mark.parametrize("field", ["coder_url", "profiles", "fargate_profiles"])
@BACKENDS
def test_configured_gateway_still_fails_closed_on_unreadable_choices(section, field, backend):
    setattr(section, field, "https://coder.example" if field == "coder_url" else [object()])
    with pytest.raises(SessionEnvironmentUnavailable):
        _start(backend)
    assert _Runtime.made[0].remote is None


def test_a_failed_spawn_hands_the_relay_to_the_runtime_lifetime(section, monkeypatch):
    section.coder_url = "https://coder.example"
    launch = MagicMock()

    async def workspace_server(workdir):
        return {"name": "workspace", "command": "python", "args": [], "env": []}

    async def prepare(session_key, *, before_compute):
        before_compute()
        return SimpleNamespace(
            launch=launch, workdir="/workspace/app", profile_name="", created=False
        )

    launch.workspace_server = workspace_server
    monkeypatch.setattr("kiro_crew.coder.session_launch.prepare_for_session", prepare)
    with pytest.raises(_SpawnReached):
        _start(ACP_BACKEND_KAS)
    runtime = _Runtime.made[0]
    assert runtime.remote is not None
    launch.close_relay_when_ended.assert_called_once_with(runtime.is_alive)
