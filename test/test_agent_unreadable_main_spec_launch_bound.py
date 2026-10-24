"""During an episode every start is refused, a bare name and a view alike.

A kiro-cli process keeps serving a spec it loaded after the file turns unreadable
(``chmod 000``), and a renamed main agent cannot be learned from a file that cannot
be read: the rename landed after the last rebuild write, so
``_main_spec_declared_name`` still names the old one. So no bare-name open, switch
or KAS start is admitted on the main spec's current unreadability: each is refused
for the episode, with one SEL denial. A view is refused too, whatever file it was
read from: no read can be shown not to be the kept main spec.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew.acp.runtime import AcpRuntimeError
from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW, METHOD_SET_MODE

needs_real_chmod = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 000 does not deny the owner's read on this host",
)


def _renamed_main(tmp_path: Path, agent_mod) -> Path:
    """``kirocrew.json`` declares ``custom-main``; the last rebuild wrote it as ``kirocrew``.

    The rename landed by hand after that write, so ``_main_spec_declared_name``
    (set only by a rebuild write) still says ``kirocrew``. Episode open: the next
    rebuild could not read the file.
    """
    agents = tmp_path / "kiro_agents"
    agents.mkdir()
    spec = agents / "kirocrew.json"
    spec.write_text(
        json.dumps(
            {
                "name": "custom-main",
                "tools": ["@user-server"],
                "allowedTools": ["@user-server"],
                "mcpServers": {"user-server": {"command": "/opt/user-server", "args": []}},
            }
        ),
        encoding="utf-8",
    )
    (agents / "helper.json").write_text(
        json.dumps({"name": "helper", "tools": []}), encoding="utf-8"
    )
    agent_mod._main_spec_declared_name = "kirocrew"
    agent_mod._main_spec_unreadable = True
    agent_mod._main_spec_episode_revision = 1
    return spec


@pytest.fixture
def audited(held_flag, monkeypatch):
    out: list[str] = []
    monkeypatch.setattr(held_flag, "_audit_main_spec_denial", lambda agent, **_: out.append(agent))
    monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
    from kiro_crew.acp.runtime import AcpRuntime

    monkeypatch.setattr(AcpRuntime, "kill", AsyncMock())
    return out


def _unbound_runtime(tmp_path: Path, agent: str):
    rt, _, proc = _make_runtime()
    rt._agent = agent
    rt._launch_unbound = True
    rt._work_dir = str(tmp_path)
    sent = AsyncMock(return_value={"sessionId": "s-1"})
    rt._send_and_await = sent
    return rt, sent


@needs_real_chmod
@pytest.mark.asyncio
async def test_a_warm_unbound_runtime_of_a_renamed_main_is_refused_while_unreadable(
    tmp_path, held_flag, audited
):
    """A runtime launched as the renamed main agent while the spec read, then the episode.

    The process may hold the spec under any name it declared then, so its opens
    are refused although no remembered name matches and the file cannot be read
    now, one denial each. Mutation pin: admitting a bare name on the file's
    current unreadability writes the ``session/new``.
    """
    from kiro_crew.acp.types import METHOD_SESSION_LOAD as _LOAD

    spec = _renamed_main(tmp_path, held_flag)
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
        spec.chmod(0)
        try:
            with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                held_flag.require_main_spec_projected("custom-main")
            rt, sent = _unbound_runtime(tmp_path, "custom-main")
            for method in (METHOD_SESSION_NEW, _LOAD):
                with pytest.raises(AcpRuntimeError, match="may not start"):
                    await rt._send_session_open(
                        method, {"cwd": str(tmp_path), "mcpServers": []}, timeout=5.0
                    )
            sent.assert_not_awaited()
        finally:
            spec.chmod(0o600)
    assert audited == ["custom-main", "custom-main", "custom-main"]


@needs_real_chmod
@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["helper", "custom-main", "kirocrew", "kirocrew-worker"])
async def test_every_bare_name_on_an_unbound_runtime_is_refused_for_the_episode(
    tmp_path, held_flag, audited, agent
):
    """Another agent's bare name and the main spec's own names are refused alike, once each.

    Whether the process started before the file turned unreadable or after, a
    doubt refuses: nothing can show which name the kept file declares. A warm
    process keeps serving a cached main agent after ``chmod 000``, so its
    ``currentModeId`` names the agent and ``_verify_spawn_agent_active`` passes:
    this refusal is what stops the session.
    """
    spec = _renamed_main(tmp_path, held_flag)
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
        spec.chmod(0)
        try:
            rt, sent = _unbound_runtime(tmp_path, agent)
            with pytest.raises(AcpRuntimeError, match="may not start"):
                await rt._send_session_open(METHOD_SESSION_NEW, {"cwd": str(tmp_path)}, timeout=5.0)
            sent.assert_not_awaited()
        finally:
            spec.chmod(0o600)
    assert audited == [agent]


@needs_real_chmod
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", sorted({METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SET_MODE, "session/resume"})
)
async def test_a_direct_client_frame_of_a_renamed_main_is_refused_while_unreadable(
    tmp_path, held_flag, audited, method
):
    """The direct client's checked frames take the launch-bound rule (``_send_request``)."""
    from kiro_crew.acp.client_transport import _CHECKED_METHODS, _send_request
    from kiro_crew.acp.transport_errors import AcpError

    assert method in _CHECKED_METHODS
    spec = _renamed_main(tmp_path, held_flag)
    fake = SimpleNamespace(
        _process=SimpleNamespace(stdin=MagicMock()),
        _agent="custom-main",
        _work_dir=str(tmp_path),
        _launch_unbound=True,
        _launch_view_source=None,
        _launch_view_episode=None,
        _native_skill_projection=None,
        _next_req_id=lambda: 1,
        _write_request_bounded=AsyncMock(),
        _last_activity=0.0,
    )
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
        spec.chmod(0)
        try:
            with pytest.raises(AcpError, match="may not start"):
                await _send_request(fake, method, {"sessionId": "s-1", "modeId": "custom-main"})
            fake._write_request_bounded.assert_not_awaited()
        finally:
            spec.chmod(0o600)
    assert audited == ["custom-main"]


@needs_real_chmod
@pytest.mark.asyncio
async def test_a_bare_name_switch_to_the_renamed_main_is_refused_once(tmp_path, held_flag, audited):
    """``check_mode_frame`` runs the file-keyed start gate first: one denial."""
    from kiro_crew.acp.session_handle import check_mode_frame

    spec = _renamed_main(tmp_path, held_flag)
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
        spec.chmod(0)
        try:
            with pytest.raises(AcpRuntimeError, match="may not start"):
                await check_mode_frame("custom-main", str(tmp_path), None, None)
        finally:
            spec.chmod(0o600)
    assert audited == ["custom-main"]


def test_a_view_is_refused_during_an_episode_whatever_file_it_was_read_from(
    tmp_path, held_flag, audited
):
    """No view's source is classified, so no view is admitted while an episode is open.

    A view built healthy, at the current revision, from another file is still
    refused once an episode opens: no read can be shown not to be the kept main
    spec (a link or a retarget can put it under any name). Mutation pin: admitting
    a view of another file during the episode writes no denial here.
    """
    held_flag._main_spec_episode_revision = 2
    built = held_flag.main_spec_episode()
    bound = held_flag.require_main_spec_launch_bound
    for source in ("/agents/kirocrew.json", "/agents/helper.json", "/gone/link.json"):
        bound("helper", before=built, view_source=source)
    held_flag._main_spec_unreadable = True
    for source in ("/agents/kirocrew.json", "/agents/helper.json", "/gone/link.json"):
        with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
            bound("helper", before=built, view_source=source)
    assert audited == ["helper"] * 3


@pytest.mark.asyncio
async def test_a_direct_client_view_frame_is_refused_while_an_episode_is_open(
    tmp_path, held_flag, audited
):
    """The direct client's launch view is bound to its reading, whatever its source."""
    from kiro_crew.acp.client_transport import _send_request
    from kiro_crew.acp.transport_errors import AcpError

    held_flag._main_spec_episode_revision = 3

    def _client():
        return SimpleNamespace(
            _process=SimpleNamespace(stdin=MagicMock()),
            _agent="helper",
            _work_dir=str(tmp_path),
            _launch_unbound=False,
            _launch_view_source=str(tmp_path / "helper.json"),
            _launch_view_episode=(3, False),
            _native_skill_projection=None,
            _next_req_id=lambda: 1,
            _write_request_bounded=AsyncMock(),
            _last_activity=0.0,
        )

    admitted = _client()
    await _send_request(admitted, METHOD_SESSION_NEW, {"cwd": str(tmp_path)})
    admitted._write_request_bounded.assert_awaited_once()
    held_flag._main_spec_unreadable = True
    refused = _client()
    with pytest.raises(AcpError, match="may not start"):
        await _send_request(refused, METHOD_SESSION_NEW, {"cwd": str(tmp_path)})
    refused._write_request_bounded.assert_not_awaited()
    assert audited == ["helper"]


def test_a_refusal_after_recovery_names_the_process_not_the_file(held_flag, audited):
    """After the episode closes, a pre-episode view is refused once, before
    its holder replaces the process: the message says so, and asks for no read access.

    Mutation pin: reusing the episode's message tells the user to restore read access
    to a file that reads.
    """
    held_flag._main_spec_unreadable = False
    held_flag._main_spec_episode_revision = 2
    with pytest.raises(held_flag.ForkGovernanceUnresolved) as refused:
        held_flag.require_main_spec_launch_bound(
            "kirocrew", before=(1, False), view_source="/agents/kirocrew.json"
        )
    message = str(refused.value)
    assert "being replaced" in message and "Restore read access" not in message
    held_flag._main_spec_unreadable = True
    with pytest.raises(held_flag.ForkGovernanceUnresolved, match="Restore read access"):
        held_flag.require_main_spec_launch_bound(
            "kirocrew", before=(1, False), view_source="/agents/kirocrew.json"
        )
    assert audited == ["kirocrew", "kirocrew"]
