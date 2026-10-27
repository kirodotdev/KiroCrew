"""A healthy host never refuses a session open.

The launch-bound check refuses a view while an unreadable-main-spec episode is
open, so a launch that recorded its view wrongly could refuse sessions on a
healthy host. Each launch path here runs the real
``prepare_native_skill_projection`` (no fake projection), so the launch each
process records is the one production records. ``inheritor`` is an unmapped
custom agent that takes the inherited skill trees, the projection path for
agents with no ``skill://`` entry of their own.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import test_acp_spawn_offload as spawn_offload
from native_skill_projection_helpers import (  # noqa: F401 - fixture
    _end_projections_before_tmp_path_goes,
)

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
    monkeypatch.setattr(
        agent_mod, "_audit_main_spec_denial", lambda agent, **_: audited.append(agent)
    )
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
    agents, _revision, audited = healthy
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
    if agent == "inheritor":
        # Non-vacuous: the view really took the inherited-skill-tree path.
        prepared = runtime._native_skill_projection
        assert projection._SEARCH_TOOL in prepared.specs[agent]["tools"]
        assert agent not in prepared.search_agents

    # Fresh spawn / create_session's early check, and the chokepoint for both verbs.
    await runtime._require_launch_bound_for_session_open()
    runtime._send_and_await = AsyncMock(return_value={"sessionId": "s"})  # type: ignore[method-assign]
    await runtime._send_session_open(METHOD_SESSION_NEW, {}, timeout=1.0)
    await runtime._send_session_open(METHOD_SESSION_LOAD, {}, timeout=1.0)

    # The main spec replaced outside the rebuild, as the dashboard MCP toggle
    # (``mcp_agent_sync._sync_mcp_to_agent_unlocked``) rewrites its mcpServers: the
    # gate on a healthy host reads no spec bytes, so every open below is admitted.
    main_spec = agents / _AGENTS["kirocrew"]
    toggled = json.loads(main_spec.read_text(encoding="utf-8"))
    toggled["mcpServers"] = {"toggled": {"command": "toggled-server", "args": []}}
    replacement = main_spec.with_suffix(".toggled")
    replacement.write_text(json.dumps(toggled), encoding="utf-8")
    replacement.replace(main_spec)
    await runtime._send_session_open(METHOD_SESSION_NEW, {}, timeout=1.0)

    # Full load_session (resume) on the warm process.
    spawn_offload.TestRuntimeProjectedLaunchDocuments._patch_load_session(
        monkeypatch, runtime, agent
    )
    await runtime.load_session("", "resumed-session")

    # Warm reuse after a later projection is adopted (set_mode re-prepare).
    runtime._native_skill_projection = _REAL_PREPARE(runtime._work_dir)
    await runtime._require_launch_bound_for_session_open()
    await runtime._send_session_open(METHOD_SESSION_NEW, {}, timeout=1.0)

    # Three concurrent opens on the warm process.
    await asyncio.gather(*(runtime._require_launch_bound_for_session_open() for _ in range(3)))

    assert audited == []

    # Non-vacuous: the same process is refused once an episode opens, whatever
    # file its view was read from.
    with agent_mod._main_spec_episode_lock:
        agent_mod._main_spec_unreadable = True
        agent_mod._main_spec_episode_revision += 1
    with pytest.raises(Exception, match="may not start"):
        await runtime._require_launch_bound_for_session_open()
    assert audited == [agent]


@pytest.mark.parametrize("agent", list(_AGENTS))
def test_direct_client_every_open_verb_admitted_on_a_healthy_host(healthy, tmp_path, agent):
    import acp_launch_capture as capture_mod

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

    agents, _revision, audited = healthy
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
    agent_mod.require_main_spec_launch_bound(agent, tmp_path, before=episode)
    assert audited == []


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="needs POSIX symlinks")
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["removed", "retargeted"])
async def test_a_view_read_through_a_link_to_the_main_spec_stays_the_main_spec(
    healthy, tmp_path, monkeypatch, change
):
    """A launch view read through a link is refused once an episode opens, link changed or not.

    A renamed main agent is launched through a project symlink to ``kirocrew.json``,
    so its view is built from the main spec. The link is then removed or pointed at
    another spec, and an episode opens. The session open is refused with one
    denial and no process is ended: no path is looked up, so a removed or
    retargeted link changes nothing. Mutation pin: admitting a view on its
    source admits the open.
    """
    agents, _revision, audited = healthy
    _fake_runtime_process_creation(monkeypatch)
    project_agents = tmp_path / "workspace" / ".kiro" / "agents"
    link = project_agents / "custom-main.json"
    listed = [SimpleNamespace(name=n, filename=f, scope="global") for n, f in _AGENTS.items()]
    listed.append(SimpleNamespace(name="custom-main", filename="custom-main.json", scope="project"))

    def before_spawn(runtime):
        from kiro_crew.agent_sdk.drivers import acp as acp_driver

        runtime._agent = "custom-main"
        # The main agent renamed by hand: kirocrew.json now declares custom-main.
        (agents / "kirocrew.json").write_text(
            '{"name": "custom-main", "tools": ["*"], "allowedTools": ["fs_read"]}',
            encoding="utf-8",
        )
        project_agents.mkdir(parents=True, exist_ok=True)
        link.symlink_to(agents / "kirocrew.json")
        monkeypatch.setattr(projection, "list_agents", lambda **kw: listed)
        monkeypatch.setattr(projection, "prepare_native_skill_projection", _REAL_PREPARE)
        monkeypatch.setattr(acp_driver, "inherits_default_resources", lambda work_dir: True)
        # custom-main is not a Crew-owned file name, so the shared-template ceiling
        # check would judge its hand-written grants; this test is about the view's
        # source, not that check.
        monkeypatch.setattr(
            agent_mod, "require_shared_template_within_ceiling", lambda *a, **k: None
        )

    runtime, _ = await spawn_offload.TestRuntimeProjectedLaunchDocuments._drive(
        monkeypatch, tmp_path, inherits=False, before_spawn=before_spawn
    )
    assert runtime._launch_unbound is False
    assert runtime._launch_view_source == link.absolute().as_posix()
    link.unlink()
    if change == "retargeted":
        link.symlink_to(agents / "custom.json")
    with agent_mod._main_spec_episode_lock:
        agent_mod._main_spec_unreadable = True
        agent_mod._main_spec_episode_revision += 1
    from kiro_crew.acp.runtime import AcpRuntime

    killed = AsyncMock()
    monkeypatch.setattr(AcpRuntime, "kill", killed)
    with pytest.raises(Exception, match="may not start"):
        await runtime._require_launch_bound_for_session_open()
    assert audited == ["custom-main"]
    killed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="symlink setup is POSIX here")
async def test_a_main_spec_link_retargeted_during_the_read_still_refuses_the_later_open(
    healthy, tmp_path, monkeypatch
):
    """``kirocrew.json`` is a link retargeted right after the read; the later open is refused.

    A dotfile sync points ``kirocrew.json`` at a new target between the real
    preparation's read of the old one and anything after it, on a healthy host,
    and the real spawn records the launch. A later ceiling-tightening rebuild
    cannot read the replacement, so an episode opens, and the next session open on
    the warm process is refused with one denial, whatever file its view came from,
    and no process is ended. Mutation pin: admitting a view on anything recorded
    about its source admits the open.
    """
    agents, _revision, audited = healthy
    _fake_runtime_process_creation(monkeypatch)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    old, new = dotfiles / "kirocrew.json", dotfiles / "kirocrew-new.json"
    spec = '{"name": "kirocrew", "tools": ["*"], "allowedTools": ["fs_read"]}'
    for target in (old, new):
        target.write_text(spec, encoding="utf-8")
    main = agents / "kirocrew.json"
    retargeted: list[bool] = []

    def before_spawn(runtime):
        from kiro_crew.agent_sdk.drivers import acp as acp_driver

        runtime._agent = "kirocrew"
        main.unlink()
        main.symlink_to(old)
        real_read = projection._read_agent_spec

        def _read_then_retarget(path, **kwargs):
            data = real_read(path, **kwargs)
            if Path(path) == main and not retargeted:
                main.unlink()
                main.symlink_to(new)
                retargeted.append(True)
            return data

        monkeypatch.setattr(projection, "_read_agent_spec", _read_then_retarget)
        monkeypatch.setattr(projection, "prepare_native_skill_projection", _REAL_PREPARE)
        monkeypatch.setattr(acp_driver, "inherits_default_resources", lambda work_dir: True)

    runtime, _ = await spawn_offload.TestRuntimeProjectedLaunchDocuments._drive(
        monkeypatch, tmp_path, inherits=False, before_spawn=before_spawn
    )
    assert retargeted == [True]
    assert runtime._launch_unbound is False
    assert runtime._launch_view_source == main.absolute().as_posix()
    await runtime._require_launch_bound_for_session_open()
    assert audited == []
    with agent_mod._main_spec_episode_lock:
        agent_mod._main_spec_unreadable = True
        agent_mod._main_spec_episode_revision += 1
    from kiro_crew.acp.runtime import AcpRuntime

    killed = AsyncMock()
    monkeypatch.setattr(AcpRuntime, "kill", killed)
    with pytest.raises(Exception, match="may not start"):
        await runtime._require_launch_bound_for_session_open()
    assert audited == ["kirocrew"]
    killed.assert_not_awaited()
