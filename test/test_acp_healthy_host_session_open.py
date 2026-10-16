"""A healthy host never refuses a session open.

The launch-bound check refuses an unknown episode reading even when no episode
has opened, so a builder change that stops recording the reading would refuse
every session on a healthy host. Each launch path here runs the real
``prepare_native_skill_projection`` (no fake projection), so the reading each
view records is the one production records. ``inheritor`` is an unmapped custom
agent that takes the inherited skill trees, the projection path for agents with
no ``skill://`` entry of their own.
"""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import test_acp_spawn_offload as spawn_offload

import kiro_crew.agent as agent_mod
from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW, METHOD_SESSION_RESUME

_REAL_PREPARE = projection.prepare_native_skill_projection
_AGENTS = {
    "kirocrew": "kirocrew.json",  # the main spec
    "kirocrew-worker": "kirocrew-worker.json",  # a mirror
    "custom": "custom.json",  # another spec
    "inheritor": "inheritor.json",  # unmapped: gets the inherited skill trees
}
#: An explicit tool list with no ``skill://`` resource, so the view takes the trees.
_INHERITOR_TOOLS = '["fs_read"]'


@pytest.fixture(params=[0, 7], ids=["never-an-episode", "after-closed-episodes"])
def healthy(tmp_path, monkeypatch, request):
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    for name, filename in _AGENTS.items():
        tools = _INHERITOR_TOOLS if name == "inheritor" else '["*"]'
        (agents / filename).write_text(
            '{"name": "%s", "tools": %s, "allowedTools": ["fs_read"]}' % (name, tools),
            encoding="utf-8",
        )
    skill = home / "skills" / "deploy-helper"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy-helper\n---\nBody\n", encoding="utf-8")
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr("kiro_crew.config.paths.kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew", raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry", lambda name: {"command": "test-core", "args": []}
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=n, filename=f, scope="global") for n, f in _AGENTS.items()
        ],
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(agent_mod, "_main_spec_unreadable", False)
    monkeypatch.setattr(agent_mod, "_main_spec_episode_revision", request.param)
    # The mirror spec here is hand-written, so the derived-spec freshness check is not exercised.
    monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", lambda *a, **k: None)
    audited: list[str] = []
    monkeypatch.setattr(agent_mod, "_audit_main_spec_denial", audited.append)
    monkeypatch.setattr(agent_mod, "_schedule_main_spec_early_retry", lambda: None)
    return agents, request.param, audited


def _fake_runtime_process_creation(monkeypatch) -> list[tuple[str, ...]]:
    """Replace the runtime's process creation, on every platform, with a fake child.

    ``create_subprocess_limited`` is the name the runtime's launch reads at each
    spawn, and both the POSIX and the Windows creation paths sit below it, so no
    real program starts on any runner. The Windows cleanup admission is replaced
    as ``test_acp_spawn_offload`` replaces it: its native pin contract has its own
    tests, and it must not run against a fake child.
    """
    import kiro_crew.acp.runtime as runtime_mod

    spawned: list[tuple[str, ...]] = []

    async def create(*argv, **kwargs):
        spawned.append(argv)
        process = MagicMock()
        process.pid = spawn_offload._UNALLOCATABLE_PID
        process.returncode = None
        process.stderr = None
        process.stdout = None
        return process

    async def admit(factory):
        return await factory()

    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", create)
    monkeypatch.setattr(runtime_mod.platform_compat, "create_windows_cleanup_owned_process", admit)
    return spawned


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", list(_AGENTS))
async def test_shared_runtime_every_open_path_admitted_on_a_healthy_host(
    healthy, tmp_path, monkeypatch, agent
):
    agents, revision, audited = healthy
    spawned = _fake_runtime_process_creation(monkeypatch)

    def before_spawn(runtime):
        from kiro_crew.agent_sdk.drivers import acp as acp_driver

        runtime._agent = agent
        monkeypatch.setattr(projection, "prepare_native_skill_projection", _REAL_PREPARE)
        # The workspace inherits default resources, so the inheritor's view is
        # built on the inherited-skill-tree path.
        monkeypatch.setattr(acp_driver, "inherits_default_resources", lambda work_dir: True)

    runtime, _ = await spawn_offload.TestRuntimeProjectedLaunchDocuments._drive(
        monkeypatch, tmp_path, inherits=False, before_spawn=before_spawn
    )
    # One launch, through the fake: no real program started.
    assert len(spawned) == 1 and "acp" in spawned[0]
    # The REAL spawn code recorded the launch from the REAL projection.
    assert runtime._launch_unbound is False
    assert runtime._launch_view_source == (agents / _AGENTS[agent]).absolute().as_posix()
    assert runtime._launch_view_episode == (revision, False)
    if agent == "inheritor":
        # Non-vacuous: the view really took the inherited-skill-tree path.
        prepared = runtime._native_skill_projection
        assert projection._SEARCH_TOOL in prepared.specs[agent]["tools"]
        assert agent not in prepared.search_agents

    # Fresh spawn / create_session's early check, and the chokepoint for both verbs.
    await runtime._require_launch_bound_before_setup()
    runtime._send_and_await = AsyncMock(return_value={"sessionId": "s"})  # type: ignore[method-assign]
    await runtime._send_session_open(METHOD_SESSION_NEW, {}, timeout=1.0)
    await runtime._send_session_open(METHOD_SESSION_LOAD, {}, timeout=1.0)

    # Full load_session (resume) on the warm process.
    spawn_offload.TestRuntimeProjectedLaunchDocuments._patch_load_session(
        monkeypatch, runtime, agent
    )
    await runtime.load_session("", "resumed-session")

    # Warm reuse after a later projection is adopted (set_mode re-prepare).
    runtime._native_skill_projection = _REAL_PREPARE(runtime._work_dir)
    await runtime._require_launch_bound_before_setup()
    await runtime._send_session_open(METHOD_SESSION_NEW, {}, timeout=1.0)

    # Three concurrent opens on the warm process.
    await asyncio.gather(*(runtime._require_launch_bound_before_setup() for _ in range(3)))

    assert audited == []
    assert await runtime._is_stale() != "launch_view"
    assert runtime._launch_view_superseded is False

    # Non-vacuous: the same process is refused once an episode opens.
    with agent_mod._main_spec_episode_lock:
        agent_mod._main_spec_unreadable = True
        agent_mod._main_spec_episode_revision += 1
    if agent in {"custom", "inheritor"}:
        await runtime._require_launch_bound_before_setup()  # other-source view is bound
    else:
        with pytest.raises(Exception, match="may not start"):
            await runtime._require_launch_bound_before_setup()
        assert audited == [agent]


@pytest.mark.parametrize("agent", list(_AGENTS))
def test_direct_client_every_open_verb_admitted_on_a_healthy_host(healthy, tmp_path, agent):
    import acp_launch_capture as capture_mod

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

    agents, revision, audited = healthy
    launched: list = []

    def real(work_dir, **kwargs):
        return _REAL_PREPARE(work_dir, **kwargs)

    capture_mod.capture(
        ACP_BACKEND_KIRO,
        tmp_path,
        extra_patches=[
            patch("kiro_crew.acp.skill_projection.prepare_native_skill_projection", real),
            patch.object(
                capture_mod,
                "AcpClient",
                functools.partial(capture_mod.AcpClient, agent=agent),
            ),
        ],
        observe=launched.append,
    )
    client = launched[0]
    assert client._launch_unbound is False
    assert client._launch_view_source == (agents / _AGENTS[agent]).absolute().as_posix()
    assert client._launch_view_episode == (revision, False)
    process = MagicMock()
    process.stdin = MagicMock()
    client._process = process
    client._write_request_bounded = AsyncMock()
    for method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SESSION_RESUME):
        asyncio.run(client._send_request(method, {}))
    assert client._write_request_bounded.await_count == 3
    assert audited == []


@pytest.mark.parametrize("agent", list(_AGENTS))
def test_kas_name_read_admitted_on_a_healthy_host(healthy, tmp_path, agent):
    _agents, _revision, audited = healthy
    episode = agent_mod.main_spec_episode()
    agent_mod.require_main_spec_launch_bound(agent, tmp_path, before=episode, project_first=False)
    assert audited == []


@pytest.mark.asyncio
async def test_three_concurrent_refusals_never_end_a_process_two_opens_are_starting_on(
    unreadable_main_episode, monkeypatch, tmp_path
):
    """A1 (session/new) and A2 (session/load) are on the wire when three refused opens
    race. Nothing is killed until both have left, then the process is ended once."""
    import time

    from acp_runtime_helpers import _make_runtime

    from kiro_crew.acp.session_handle import AcpRuntimeDead, AcpRuntimeError
    from kiro_crew.testing.wait import default_timeout

    agent_mod, audited = unreadable_main_episode
    monkeypatch.setattr(agent_mod, "_main_spec_unreadable", False)
    monkeypatch.setattr(agent_mod, "_main_spec_episode_revision", 0)
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._can_load_session = True
    rt._launch_unbound = False
    rt._launch_view_source = (agents / "kirocrew.json").as_posix()
    rt._launch_view_episode = (0, False)
    rt._spawn_monotonic = time.monotonic()
    loop = asyncio.get_running_loop()
    answers = {METHOD_SESSION_NEW: loop.create_future(), METHOD_SESSION_LOAD: loop.create_future()}
    sent = {m: asyncio.Event() for m in answers}
    first = {m: True for m in answers}

    async def _send(method, params, **kwargs):
        if method in answers and first[method]:
            first[method] = False
            sent[method].set()
            return await answers[method]
        return {}

    async def _kill(**kwargs):
        for fut in answers.values():
            if not fut.done():
                fut.set_exception(AcpRuntimeDead("the runtime was ended"))

    rt._send_and_await = _send  # type: ignore[method-assign]
    rt.kill = AsyncMock(side_effect=_kill)  # type: ignore[method-assign]
    a1 = asyncio.create_task(rt.create_session(cwd="/tmp", agent=None))
    a2 = asyncio.create_task(rt.load_session("t.jsonl", "sid-a2", cwd="/tmp", agent=None))
    for ev in sent.values():
        await asyncio.wait_for(ev.wait(), timeout=default_timeout())
    with agent_mod._main_spec_episode_lock:
        agent_mod._main_spec_unreadable = True
        agent_mod._main_spec_episode_revision += 1
    results = await asyncio.gather(
        rt.create_session(cwd="/tmp", agent=None),
        rt.load_session("t.jsonl", "sid-b", cwd="/tmp", agent=None),
        rt.create_session(cwd="/tmp", agent=None),
        return_exceptions=True,
    )
    assert all(isinstance(r, AcpRuntimeError) and "may not start" in str(r) for r in results)
    rt.kill.assert_not_awaited()
    answers[METHOD_SESSION_NEW].set_result({"sessionId": "sid-a1"})
    answers[METHOD_SESSION_LOAD].set_result({"modes": {"currentModeId": "kirocrew"}})
    h1 = await asyncio.wait_for(a1, timeout=default_timeout())
    h2 = await asyncio.wait_for(a2, timeout=default_timeout())
    assert (h1.session_id, h2.session_id) == ("sid-a1", "sid-a2")
    rt.kill.assert_not_awaited()
    rt.unregister_session("sid-a1")
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await rt.create_session(cwd="/tmp", agent=None)
    rt.kill.assert_not_awaited()  # A2 is still registered
    rt.unregister_session("sid-a2")
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await rt.create_session(cwd="/tmp", agent=None)
    rt.kill.assert_awaited_once()
    # Healthy-host recovery does not re-admit the stale view.
    with agent_mod._main_spec_episode_lock:
        agent_mod._main_spec_unreadable = False
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await rt._require_launch_bound_before_setup()
