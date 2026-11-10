"""Placement survives cleared config."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.coder import session_launch
from kiro_crew.config import live, paths
from kiro_crew.connected_computers.launch import ConnectedComputerLaunch
from kiro_crew.providers import acp as acp_facade
from kiro_crew.providers import acp_runtime_start as start
from kiro_crew.remote_session import relay as relay_mod
from kiro_crew.session_environment import SessionEnvironmentUnavailable, forget_remote_workspace
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


KEY = "dashboard:chat-gate"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    section = SimpleNamespace(coder_url="", profiles=[], sprites_profiles=[], boxd_profiles=[])
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
        ("sprites_profiles", "bad"),
        ("boxd_profiles", "bad"),
        ("kiro_cloud_profiles", "bad"),
        ("coder_url", "ready"),
    ],
)
async def test_start_gate(setup, tmp_path, monkeypatch, backend, field, choice):
    section, provider, runtime = setup
    provider._client.backend = backend
    if field:
        value = {
            "coder_url": "https://coder.example",
            "profiles": [SimpleNamespace(id="other")],
        }.get(field, [{"id": "other"}])
        setattr(section, field, value)
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
    provider._recover_effort_overlay = AsyncMock()
    remote = choice in ("bound", "bad")
    with pytest.raises(AcpRuntimeError if remote else RuntimeError) as raised:
        await acp_lifecycle.start(provider)
    assert str(raised.value) == (REMOTE_NEEDS_KAS if remote else "engine started")
    assert provider._client.ensure_ready.await_count == (not remote)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
@pytest.mark.parametrize("remote_provider", ["sprites", "boxd"])
async def test_removing_the_last_profile_never_starts_its_session_locally(
    setup, tmp_path, backend, remote_provider
):
    section, provider, runtime = setup
    provider._client.backend = backend
    field = f"{remote_provider}_profiles"
    setattr(section, field, [{"id": "saved-vm"}])
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder/session-choices.json")
    choices.set(KEY, "saved-vm", remote_provider)
    assert choices.resolve_choice(KEY)[2] == remote_provider
    setattr(section, field, [])
    with pytest.raises(SessionEnvironmentUnavailable, match="is no longer configured"):
        await asyncio.wait_for(start._start_kiro_runtime_impl(provider, {}, {}), 30)
    assert choices.get(KEY) == "saved-vm"
    runtime.spawn.assert_not_awaited()
    runtime.set_remote_workspace.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cap_first", "cap_second", "cancel", "install", "workspace"])
async def test_connected_attachment_failure_releases_only_its_relays(setup, monkeypatch, failure):
    section, provider, runtime = setup
    section.coder_url = "https://coder.example"
    provider._client.backend = ACP_BACKEND_KAS
    monkeypatch.setattr(relay_mod, "_LIVE", set())
    launch = ConnectedComputerLaunch(
        "laptop", "Laptop", session_key=KEY, exposed_mcp=("one", "two"), broker=MagicMock()
    )
    prepared = SimpleNamespace(
        launch=launch, workdir="/workspace/app", profile_name="Laptop", created=False
    )
    monkeypatch.setattr(session_launch, "prepare_for_session", AsyncMock(return_value=prepared))
    watcher = MagicMock()
    monkeypatch.setattr(launch, "close_relay_when_ended", watcher)
    unrelated = relay_mod.WorkspaceRelay(MagicMock(), [], b"test-only", prestart=False)
    opened = []
    idle_tasks = []
    reached = asyncio.Event()
    blocked = asyncio.Event()
    original_relay = launch._relay
    error = RuntimeError("attachment failed")

    async def open_relay(kind):
        relay, port = await original_relay(kind)
        opened.append(relay)
        idle_tasks.append(relay._idle_task)
        return relay, port

    monkeypatch.setattr(launch, "_relay", open_relay)
    original_bridge = launch._bridge_entry

    def bridge_entry(name, port):
        if failure == "workspace":
            raise error
        return original_bridge(name, port)

    monkeypatch.setattr(launch, "_bridge_entry", bridge_entry)
    if failure == "install":
        runtime.set_remote_workspace.side_effect = error
    if failure == "cancel":
        original_mcp = launch.local_mcp_servers

        async def partial_mcp():
            launch.exposed_mcp = ("one",)
            await original_mcp()
            reached.set()
            await asyncio.wait_for(blocked.wait(), FALLBACK_TIMEOUT_SECS)
            raise AssertionError("cancelled attachment must not continue")

        monkeypatch.setattr(launch, "local_mcp_servers", partial_mcp)
    task = None
    try:
        await asyncio.wait_for(unrelated.start(), FALLBACK_TIMEOUT_SECS)
        idle_tasks.append(unrelated._idle_task)
        if failure.startswith("cap_"):
            # One unrelated relay, the workspace, then optionally the first MCP.
            monkeypatch.setattr(relay_mod, "_MAX_LIVE_RELAYS", 2 if failure == "cap_first" else 3)
        task = asyncio.create_task(start._start_kiro_runtime_impl(provider, {}, {}))
        if failure == "cancel":
            await asyncio.wait_for(reached.wait(), FALLBACK_TIMEOUT_SECS)
            assert len(opened) == 2
            assert set(opened) <= relay_mod._LIVE
            task.cancel()
        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected) as raised:
            await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
        if failure in ("install", "workspace"):
            assert raised.value is error
        elif failure.startswith("cap_"):
            assert str(raised.value) == "too many remote workspace relays are open"
        assert (
            len(opened)
            == {
                "cap_first": 1,
                "cap_second": 2,
                "cancel": 2,
                "install": 3,
                "workspace": 1,
            }[failure]
        )
        assert relay_mod._LIVE == {unrelated}
        assert all(relay._server is None for relay in opened)
        assert launch.own_relays() == ()
        runtime.spawn.assert_not_awaited()
        watcher.assert_not_called()
        # Capacity is usable immediately, without advancing the idle reaper.
        replacement = relay_mod.WorkspaceRelay(MagicMock(), [], b"test-only", prestart=False)
        opened.append(replacement)
        await asyncio.wait_for(replacement.start(), FALLBACK_TIMEOUT_SECS)
        idle_tasks.append(replacement._idle_task)
        assert replacement in relay_mod._LIVE
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS
            )
        for relay in [*opened, unrelated]:
            await asyncio.wait_for(relay.close(), FALLBACK_TIMEOUT_SECS)
        await asyncio.wait_for(
            asyncio.gather(
                *(task for task in idle_tasks if task is not None), return_exceptions=True
            ),
            FALLBACK_TIMEOUT_SECS,
        )


@pytest.mark.asyncio
async def test_other_workspace_attachment_failure_keeps_existing_cleanup_path(setup, monkeypatch):
    section, provider, runtime = setup
    section.coder_url = "https://coder.example"
    provider._client.backend = ACP_BACKEND_KAS
    launch = MagicMock(workspace_server=AsyncMock(return_value={}), close_relay=AsyncMock())
    prepared = SimpleNamespace(
        launch=launch, workdir="/workspace/app", profile_name="Coder", created=False
    )
    monkeypatch.setattr(session_launch, "prepare_for_session", AsyncMock(return_value=prepared))
    error = RuntimeError("attachment failed")
    runtime.set_remote_workspace.side_effect = error
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(
            start._start_kiro_runtime_impl(provider, {}, {}), FALLBACK_TIMEOUT_SECS
        )
    assert raised.value is error
    launch.close_relay.assert_not_awaited()
    launch.close_relay_when_ended.assert_not_called()
    runtime.spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
@pytest.mark.parametrize("choice_state", ["readable", "missing", "unreadable"])
async def test_disabled_connected_computer_never_spawns_local_runtime(
    setup, tmp_path, monkeypatch, developer, preview, backend, choice_state
):
    section, provider, runtime = setup
    provider._client.backend = backend
    monkeypatch.setenv("KIROCREW_DEV_MODE", "1" if developer else "0")
    cfg = SimpleNamespace(
        session_environments=section,
        dashboard=SimpleNamespace(feature_previews={"connected_computers": preview}),
    )
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder/session-choices.json")
    choices.set(KEY, "laptop", session_launch.CONNECTED_COMPUTER_PROVIDER_ID)
    if choice_state == "missing":
        choices.path.unlink()
    elif choice_state == "unreadable":
        choices.path.write_text("{not json", encoding="utf-8")
    assert start._choices_file_may_exist(KEY)
    with pytest.raises(
        SessionEnvironmentUnavailable, match="turned off|Session Environments|missing|unreadable"
    ):
        await asyncio.wait_for(
            start._start_kiro_runtime_impl(provider, {}, {}), FALLBACK_TIMEOUT_SECS
        )
    runtime.spawn.assert_not_awaited()
    runtime.set_remote_workspace.assert_not_called()
    if choice_state == "readable":
        assert choices.resolve_choice(KEY) == ("laptop", KEY, "connected_computer")
    else:
        with pytest.raises(SessionEnvironmentUnavailable):
            choices.resolve_choice(KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_provider", [None, "coder", "sprites", "boxd", "kiro_cloud"])
async def test_developer_off_keeps_local_start_for_other_placements(
    setup, tmp_path, monkeypatch, remote_provider
):
    section, provider, runtime = setup
    section.coder_url = "https://coder.example"
    provider._client.backend = ACP_BACKEND_KAS
    monkeypatch.setenv("KIROCREW_DEV_MODE", "0")
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder/session-choices.json")
    if remote_provider is not None:
        choices.set(KEY, "dev", remote_provider)
    else:
        no_read = MagicMock(side_effect=AssertionError("unbound placement read"))
        monkeypatch.setattr(session_launch.SessionEnvironmentChoices, "_read", no_read)
    prepare = AsyncMock(side_effect=AssertionError("local chat prepared a workspace"))
    monkeypatch.setattr(session_launch, "prepare_for_session", prepare)
    with pytest.raises(RuntimeError, match="spawn reached"):
        await asyncio.wait_for(
            start._start_kiro_runtime_impl(provider, {}, {}), FALLBACK_TIMEOUT_SECS
        )
    runtime.spawn.assert_awaited_once()
    runtime.set_remote_workspace.assert_not_called()
    prepare.assert_not_awaited()
