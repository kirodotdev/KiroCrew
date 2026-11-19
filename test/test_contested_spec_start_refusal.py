"""R-SPEC-13: two differing specs declaring one agent name refuse the start on every host.

kiro-cli and KAS refuse through their own spec resolution. The hosts that read their
servers from ``acp.session_mcp`` -- the per-process adapters ``AcpClient`` spawns
(claude, opencode, goose, pi, copilot, deepseek) and codex on ``AcpRuntime`` -- used
to start on Crew's control plane alone. They now refuse the start through one check,
``session_mcp.refuse_contested_agent_spec``, with the resolver's message naming both
files and the error on the cause chain the dashboard's chooser card reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew import agent as agent_mod
from kiro_crew import agent_spec_pins
from kiro_crew.acp import session_mcp
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KAS
from kiro_crew.agent_discovery import AmbiguousAgentSpecError
from kiro_crew.agent_spec_pin_ops import ambiguity_meta


def _write(directory: Path, file_name: str, **spec: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / file_name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


@pytest.fixture
def pins(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    current: dict[str, str] = {}
    monkeypatch.setattr(agent_spec_pins, "spec_pins", lambda: dict(current))
    return current


@pytest.fixture
def agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pins: dict[str, str]) -> Path:
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: directory)
    return directory


def _contest(agents: Path) -> None:
    _write(agents, "PkgA-helper.json", name="helper", prompt="A")
    _write(agents, "PkgB-helper.json", name="helper", prompt="B")


# -- the shared check --------------------------------------------------------------


def test_differing_specs_for_one_name_are_refused_naming_both_files(agents):
    _contest(agents)
    with pytest.raises(AmbiguousAgentSpecError) as caught:
        session_mcp.refuse_contested_agent_spec("helper")
    message = str(caught.value)
    assert "PkgA-helper.json" in message and "PkgB-helper.json" in message
    assert "kirocrew agent pin" in message


@pytest.mark.parametrize("claimants", [1, 2])
def test_a_pin_naming_no_claimant_is_refused(agents, pins, claimants):
    """A pin left from an earlier contest, its file gone: the session spec read
    refuses it too, so the start would run without the spec."""
    _write(agents, "PkgA-helper.json", name="helper", prompt="A")
    if claimants == 2:
        _write(agents, "PkgB-helper.json", name="helper", prompt="B")
    pins["helper"] = "Gone.json"
    with pytest.raises(AmbiguousAgentSpecError):
        session_mcp.refuse_contested_agent_spec("helper")


@pytest.mark.asyncio
async def test_a_checkout_spec_does_not_exempt_a_mirrored_session(agents, tmp_path):
    """A mirrored session still reads the user level under an untrusted checkout
    spec, so the contest would still drop its servers: the start is refused there too."""
    from kiro_crew.acp.session_handle import AcpRuntimeError

    _contest(agents)
    checkout = tmp_path / "checkout"
    _write(checkout / ".kiro" / "agents", "helper.json", name="helper", prompt="P")
    assert session_mcp._session_spec_and_snapshot_for("helper", checkout)[0]["mcpServers"] == {}
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_CODEX
    rt._work_dir = str(checkout)
    with patch.object(agent_mod, "require_shared_template_within_ceiling", return_value=None):
        with pytest.raises(AcpRuntimeError):
            await rt._refuse_over_ceiling_start("helper")


@pytest.mark.parametrize(
    "case", ["one_file", "identical", "pinned", "no_file", "stale_pin_no_file", "no_dir"]
)
def test_everything_but_a_contest_starts_as_before(agents, pins, case):
    if case == "one_file":
        _write(agents, "helper.json", name="helper", prompt="A")
    elif case == "identical":
        _write(agents, "PkgA-helper.json", name="helper", prompt="A")
        _write(agents, "PkgB-helper.json", name="helper", prompt="A")
    elif case == "pinned":
        _contest(agents)
        pins["helper"] = "PkgB-helper.json"
    elif case == "stale_pin_no_file":
        pins["helper"] = "Gone.json"
    elif case == "no_dir":
        agents.rmdir()
    session_mcp.refuse_contested_agent_spec("helper")


# -- AcpClient: the per-process adapter hosts ----------------------------------------


@pytest.mark.asyncio
async def test_an_adapter_host_start_is_refused_before_the_launch_plan(agents, tmp_path):
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.harness.claude import ClaudeLaunch
    from kiro_crew.acp.transport_errors import AcpError

    _contest(agents)
    client = AcpClient(work_dir=tmp_path / "w", agent="helper", acp_backend=ACP_BACKEND_CLAUDE)
    # Reaching the plan means the start was not refused: fail there, before any launch.
    planned = AsyncMock(side_effect=AssertionError("the start reached the launch plan"))
    with patch.object(ClaudeLaunch, "resolve_spawn", new=planned) as plan:
        with pytest.raises(AcpError) as caught:
            await client._spawn()
    plan.assert_not_called()
    assert isinstance(caught.value.__cause__, AmbiguousAgentSpecError)
    assert "PkgA-helper.json" in str(caught.value)
    meta = ambiguity_meta(caught.value)
    assert meta is not None and meta["agent"] == "helper"


# -- AcpRuntime: codex ---------------------------------------------------------------


@pytest.mark.parametrize("start", ["create", "load"])
@pytest.mark.asyncio
async def test_a_codex_session_start_is_refused_before_the_request(agents, start):
    from kiro_crew.acp.session_handle import AcpRuntimeError

    _contest(agents)
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_CODEX
    rt._agent = "helper"
    rt._can_load_session = True
    rt._send_and_await = AsyncMock()  # type: ignore[method-assign]
    with patch.object(agent_mod, "require_shared_template_within_ceiling", return_value=None):
        with pytest.raises(AcpRuntimeError) as caught:
            if start == "create":
                await rt.create_session(cwd="/w")
            else:
                await rt.load_session("/home/u/.codex/sessions/sid-9.json", "sid-9", cwd="/w")
    rt._send_and_await.assert_not_called()
    assert isinstance(caught.value.__cause__, AmbiguousAgentSpecError)
    meta = ambiguity_meta(caught.value)
    assert meta is not None and meta["agent"] == "helper"


@pytest.mark.parametrize("backend", ["", ACP_BACKEND_KAS])
@pytest.mark.asyncio
async def test_kiro_and_kas_runtimes_keep_their_own_refusal(agents, backend):
    """kiro-cli and KAS refuse in their own resolution; the runtime check skips them."""
    from kiro_crew.acp import runtime_session_setup

    _contest(agents)
    rt, _, _ = _make_runtime()
    rt._acp_backend = backend
    with (
        patch.object(agent_mod, "require_shared_template_within_ceiling", return_value=None),
        patch.object(runtime_session_setup, "refuse_contested_agent_spec") as check,
    ):
        await rt._refuse_over_ceiling_start("helper")
    check.assert_not_called()
