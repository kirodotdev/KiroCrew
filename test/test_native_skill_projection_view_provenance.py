"""A view read across an unreadable-main-spec episode is refused, whatever file it came from.

No read is classified as the main spec or another file: a link, a rename or a retarget
can put the kept main spec under any name and change it between any two lookups, so
every view built across an episode is refused, and every start is refused while one
is open. A process keeps its view for life, and a view built before an episode is
admitted again once it closes. A hard link is no way in either: the reader refuses
it, so no view is built.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _make_runtime
from native_skill_projection_helpers import (  # noqa: F401 - fixtures
    _end_projections_before_tmp_path_goes,
    native_tree,
)

from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.runtime import AcpRuntimeError
from kiro_crew.acp.types import METHOD_SESSION_NEW

needs_links = pytest.mark.skipif(
    os.name == "nt", reason="symlink and hard-link setup is POSIX here"
)


@pytest.fixture
def linked_main(native_tree, monkeypatch):  # noqa: F811 - the imported fixture
    """A healthy host whose checkout reaches ``kirocrew.json`` through ``.kiro/agents``.

    ``kirocrew.json`` still carries a grant a later ceiling revokes. The checkout's
    spec of the same name is how the projection finds it (project scope first).
    """
    import kiro_crew.agent as agent_mod

    _home, agents, project = native_tree
    main = agents / "kirocrew.json"
    main.write_text(
        json.dumps({"name": "kirocrew", "allowedTools": ["@revoked/delete"]}), encoding="utf-8"
    )
    proj_agents = project / ".kiro" / "agents"
    proj_agents.mkdir(parents=True)
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(agent_mod, "_main_spec_unreadable", False)
    monkeypatch.setattr(agent_mod, "_main_spec_episode_revision", 0)
    monkeypatch.setattr(agent_mod, "_main_spec_declared_name", "kirocrew")
    audited: list[str] = []
    monkeypatch.setattr(
        agent_mod, "_audit_main_spec_denial", lambda agent, **_: audited.append(agent)
    )
    monkeypatch.setattr(agent_mod, "_schedule_main_spec_early_retry", lambda: None)
    from kiro_crew.acp.runtime import AcpRuntime

    monkeypatch.setattr(AcpRuntime, "kill", AsyncMock())
    return agent_mod, agents, project, proj_agents, audited


def _project_agent(monkeypatch, name: str, filename: str) -> None:
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name=name, filename=filename, scope="project")],
    )


def _prepare(project: Path, name: str):
    prepared = projection.prepare_native_skill_projection(project, per_session_element=False)
    alias = prepared.spawn_agent(name)
    return prepared, alias


def _open_episode(agent_mod, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "_main_spec_unreadable", True)
    monkeypatch.setattr(agent_mod, "_main_spec_episode_revision", 1)


# ── A view read through a link is refused, link changed or not ────────────────


@needs_links
@pytest.mark.parametrize("change", ["removed", "retargeted"])
def test_a_view_built_through_a_project_symlink_to_the_main_spec_stays_refused(
    linked_main, monkeypatch, change
):
    """The view was built from the main spec's bytes before the episode; it stays refused.

    The recorded source is the checkout's link path, unresolved. While the episode
    is open the open is refused whatever the link does, before it changes and once
    it is removed or points elsewhere: no check consults the view's source, and the
    process still runs the same view.
    """
    agent_mod, agents, project, proj_agents, audited = linked_main
    link = proj_agents / "kirocrew.json"
    link.symlink_to(agents / "kirocrew.json")
    _project_agent(monkeypatch, "kirocrew", "kirocrew.json")
    prepared, alias = _prepare(project, "kirocrew")
    source = prepared.view_source(alias)
    assert source == link.absolute().as_posix()  # recorded unresolved
    _open_episode(agent_mod, monkeypatch)
    with pytest.raises(agent_mod.ForkGovernanceUnresolved, match="may not start"):
        agent_mod.require_main_spec_launch_bound("kirocrew", project, view_source=source)
    link.unlink()
    if change == "retargeted":
        other = agents / "other.json"
        other.write_text(json.dumps({"name": "kirocrew", "tools": []}), encoding="utf-8")
        link.symlink_to(other)
    with pytest.raises(agent_mod.ForkGovernanceUnresolved, match="may not start"):
        agent_mod.require_main_spec_launch_bound("kirocrew", project, view_source=source)
    assert audited == ["kirocrew", "kirocrew"]


@needs_links
def test_the_spec_reader_builds_no_view_through_a_hard_link(linked_main, monkeypatch):
    """Not a gap: a hard-linked spec is refused by the reader (nlink), so no view exists.

    Pins why the lazy check's hard-link case is unreachable: ``_read_agent_spec``
    refuses the link, the projection publishes no alias, and the start keeps the
    bare name.
    """
    from kiro_crew.agent_discovery import _read_agent_spec

    _agent_mod, agents, project, proj_agents, _audited = linked_main
    os.link(agents / "kirocrew.json", proj_agents / "kirocrew.json")
    _project_agent(monkeypatch, "kirocrew", "kirocrew.json")
    assert _read_agent_spec(proj_agents / "kirocrew.json") is None
    prepared, alias = _prepare(project, "kirocrew")
    assert prepared.aliases == {} and alias == "kirocrew"


@needs_links
@pytest.mark.asyncio
async def test_a_warm_view_launch_of_the_main_agent_writes_no_open_once_its_link_is_gone(
    linked_main, monkeypatch
):
    """The shared runtime's open runs only the launch bound, no start gate: the main agent.

    A runtime launched as ``kirocrew`` on the view built through the checkout link,
    before the episode. The link is removed (a branch switch, a ``git clean``). The
    next ``session/new`` on the warm process must be refused; on this head the frame
    is written.
    """
    agent_mod, agents, project, proj_agents, audited = linked_main
    link = proj_agents / "kirocrew.json"
    link.symlink_to(agents / "kirocrew.json")
    _project_agent(monkeypatch, "kirocrew", "kirocrew.json")
    prepared, alias = _prepare(project, "kirocrew")
    rt, _, _proc = _make_runtime()
    rt._agent = "kirocrew"
    rt._work_dir = str(project)
    rt._launch_unbound = False
    rt._launch_view_source = prepared.view_source(alias)
    sent = AsyncMock(return_value={"sessionId": "s-1"})
    rt._send_and_await = sent
    _open_episode(agent_mod, monkeypatch)
    link.unlink()
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await rt._send_session_open(METHOD_SESSION_NEW, {"cwd": str(project)}, timeout=5.0)
    sent.assert_not_awaited()


@needs_links
@pytest.mark.asyncio
async def test_a_direct_client_frame_on_a_view_whose_link_is_gone_is_refused(
    linked_main, monkeypatch
):
    """The direct client's ``_send_request`` takes the same lazy check (no start gate)."""
    from kiro_crew.acp.client_transport import _send_request
    from kiro_crew.acp.transport_errors import AcpError

    agent_mod, agents, project, proj_agents, _audited = linked_main
    link = proj_agents / "kirocrew.json"
    link.symlink_to(agents / "kirocrew.json")
    _project_agent(monkeypatch, "kirocrew", "kirocrew.json")
    prepared, alias = _prepare(project, "kirocrew")
    fake = SimpleNamespace(
        _process=SimpleNamespace(stdin=MagicMock()),
        _agent="kirocrew",
        _work_dir=str(project),
        _launch_unbound=False,
        _launch_view_source=prepared.view_source(alias),
        _native_skill_projection=None,
        _next_req_id=lambda: 1,
        _write_request_bounded=AsyncMock(),
        _last_activity=0.0,
    )
    _open_episode(agent_mod, monkeypatch)
    link.unlink()
    with pytest.raises(AcpError, match="may not start"):
        await _send_request(fake, METHOD_SESSION_NEW, {"cwd": str(project)})
    fake._write_request_bounded.assert_not_awaited()


@needs_links
@pytest.mark.asyncio
async def test_a_switch_to_a_view_whose_link_now_reaches_a_declaring_file_is_refused(
    linked_main, monkeypatch
):
    """The switch path runs the start gate first, so the link must reach a declaring file.

    ``helper.json`` in the checkout was a link to ``kirocrew.json`` when the view was
    built. It now points at a readable spec that declares ``helper``: the start gate
    resolves the name to that file and admits it, and the lazy view check admits the
    view built from the main spec's bytes.
    """
    from kiro_crew.acp.session_handle import check_mode_frame

    agent_mod, agents, project, proj_agents, audited = linked_main
    link = proj_agents / "helper.json"
    link.symlink_to(agents / "kirocrew.json")
    _project_agent(monkeypatch, "helper", "helper.json")
    prepared, alias = _prepare(project, "helper")
    _open_episode(agent_mod, monkeypatch)
    decoy = project / "decoy-helper.json"
    decoy.write_text(json.dumps({"name": "helper", "tools": []}), encoding="utf-8")
    link.unlink()
    link.symlink_to(decoy)
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await check_mode_frame("helper", str(project), prepared, alias)


# ── At preparation: the link changes right after the read ─────────────────────


@needs_links
@pytest.mark.parametrize(
    "after_read", ["kept", "gone", "retargeted"], ids=["link-kept", "link-gone", "link-retargeted"]
)
def test_a_link_gone_right_after_the_projection_read_it_still_refuses_the_view(
    linked_main, monkeypatch, after_read
):
    """During an episode the projection refuses the view it reads, whatever the link does next.

    A lookup of the unresolved path after the read would answer "unrelated" once
    the link is removed or retargeted in between, so a check keyed on the view's
    source would publish a view of the main spec's bytes read during the episode
    and admit its launch. ``unprojected_main_spec_source`` consults no source: the
    view is refused with the link kept, gone or retargeted.
    """
    agent_mod, agents, project, proj_agents, _audited = linked_main
    link = proj_agents / "kirocrew.json"
    link.symlink_to(agents / "kirocrew.json")
    _project_agent(monkeypatch, "kirocrew", "kirocrew.json")
    _open_episode(agent_mod, monkeypatch)  # the lock has cleared; no rebuild yet
    real_read = projection._read_agent_spec
    read: list[bool] = []

    def _read_then_unlink(path, **kwargs):
        data = real_read(path, **kwargs)
        if Path(path) == link and link.is_symlink():
            read.append(data is not None)
            if after_read != "kept":
                link.unlink()
            if after_read == "retargeted":
                other = agents / "other.json"
                other.write_text(json.dumps({"name": "kirocrew", "tools": []}), encoding="utf-8")
                link.symlink_to(other)
        return data

    monkeypatch.setattr(projection, "_read_agent_spec", _read_then_unlink)
    prepared = projection.prepare_native_skill_projection(project, per_session_element=False)
    assert read == [True]  # the main spec's bytes were read through the link
    assert "kirocrew" not in prepared.aliases


# ── A Markdown project spec admits no start during an episode ──────────────────


@pytest.mark.asyncio
async def test_a_markdown_project_spec_does_not_admit_a_warm_unbound_open(
    tmp_path, held_flag, monkeypatch
):
    """F1: a project ``helper.md`` is no exemption for a renamed main agent called ``helper``.

    Projection disabled, so the warm runtime was launched by the bare name, on the
    main spec renamed to ``helper`` after the last rebuild (whose recorded name is
    still ``kirocrew``). A ceiling-tightening rebuild then cannot read it. The
    checkout holds only ``.kiro/agents/helper.md``, which kiro-cli may not load, so
    the session would run the cached main spec. ``create_session(agent=None)``
    reaches ``_send_session_open``, which must refuse it.
    """
    agents = tmp_path / "kiro_agents"
    agents.mkdir()
    (agents / "kirocrew.json").write_text(
        json.dumps({"name": "helper", "allowedTools": ["@revoked/delete"]}), encoding="utf-8"
    )
    project = tmp_path / "project"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "helper.md").write_text(
        "---\nname: helper\ndescription: a checkout agent\n---\nPrompt\n", encoding="utf-8"
    )
    audited: list[str] = []
    monkeypatch.setattr(
        held_flag, "_audit_main_spec_denial", lambda agent, **_: audited.append(agent)
    )
    monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
    monkeypatch.setattr(held_flag, "KIRO_AGENTS_DIR", agents)
    held_flag._main_spec_declared_name = "kirocrew"
    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision = 1
    rt, _, _proc = _make_runtime()
    rt._agent = "helper"
    rt._launch_unbound = True
    rt._work_dir = str(project)
    sent = AsyncMock(return_value={"sessionId": "s-1"})
    rt._send_and_await = sent
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await rt._send_session_open(METHOD_SESSION_NEW, {"cwd": str(project)}, timeout=5.0)
    sent.assert_not_awaited()
    assert audited == ["helper"]


# ── The direct client keeps its launch view's source ──────────────────────────


@pytest.mark.asyncio
async def test_the_direct_client_spawn_records_its_launch_views_source(
    tmp_path, held_flag, monkeypatch
):
    """``client_start._spawn`` records the view kiro-cli is handed, for every later open.

    A direct client launched healthy on a view sends its ``session/new`` on a
    healthy host, is refused while an episode is open, whatever file the view
    was read from, and sends again once a rebuild closes it. The spawn is faked
    below the platform split, so no real program starts on any runner.
    """
    from acp_client_helpers import _stop_stderr_drain

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.transport_errors import AcpError
    from kiro_crew.testing.ids import UNALLOCATABLE_PID

    other = (tmp_path / "helper.json").as_posix()
    prepared = projection.NativeSkillProjection(
        aliases={"helper": "helper-view-0000"},
        sources={"helper": other},
    )
    monkeypatch.setattr(
        projection, "prepare_native_skill_projection", lambda work_dir, **_kw: prepared
    )
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: None)
    audited: list[str] = []
    monkeypatch.setattr(
        held_flag, "_audit_main_spec_denial", lambda agent, **_: audited.append(agent)
    )
    monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
    import kiro_crew.acp.client as client_mod

    spawned: list[tuple[str, ...]] = []

    async def create(*argv, **kwargs):
        # ``create_subprocess_limited`` is the name the launch reads from the
        # client at each spawn, and both the POSIX and the Windows creation
        # paths sit below it, so no real program starts on any runner.
        spawned.append(argv)
        process = MagicMock()
        process.pid = UNALLOCATABLE_PID
        process.returncode = None
        return process

    async def admit(factory):
        # The Windows cleanup admission creates the child through the loop's
        # own transport and pins its real handle; hand the factory through, as
        # the sibling spawn tests do. Its pin contract has its own tests.
        return await factory()

    monkeypatch.setattr(client_mod, "create_subprocess_limited", create)
    monkeypatch.setattr(client_mod.platform_compat, "create_windows_cleanup_owned_process", admit)
    client = AcpClient(work_dir=tmp_path, agent="helper")
    with (
        patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
        patch(
            "kiro_crew.acp.client.wrap_argv",
            side_effect=lambda argv, mode, **kwargs: (argv, None),
        ),
        patch("kiro_crew.session._track_pid"),
        patch("kiro_crew.session._track_session_pid"),
    ):
        await client._spawn()
    try:
        assert len(spawned) == 1 and "helper-view-0000" in spawned[0]
        assert client._launch_view_source == other
        client._write_request_bounded = AsyncMock()
        await client._send_request(METHOD_SESSION_NEW, {"cwd": str(tmp_path)})
        client._write_request_bounded.assert_awaited_once()
        assert audited == []
        monkeypatch.setattr(held_flag, "_main_spec_unreadable", True)
        monkeypatch.setattr(held_flag, "_main_spec_episode_revision", 1)
        with pytest.raises(AcpError, match="may not start"):
            await client._send_request(METHOD_SESSION_NEW, {"cwd": str(tmp_path)})
        client._write_request_bounded.assert_awaited_once()
        assert audited == ["helper"]
        monkeypatch.setattr(held_flag, "_main_spec_unreadable", False)  # a rebuild closed it
        await client._send_request(METHOD_SESSION_NEW, {"cwd": str(tmp_path)})
        assert client._write_request_bounded.await_count == 2
        assert audited == ["helper"]
    finally:
        await _stop_stderr_drain(client)
