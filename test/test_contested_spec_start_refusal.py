"""R-SPEC-13: two differing specs declaring one agent name refuse the start on every host.

kiro-cli and KAS refuse through their own spec resolution. The hosts that read their
servers from ``acp.session_mcp`` -- the per-process adapters ``AcpClient`` spawns
(claude, opencode, goose, pi, copilot, deepseek) and codex on ``AcpRuntime`` -- used
to start on Crew's control plane alone. They now refuse the start through one check,
``session_mcp.refuse_contested_agent_spec``, with the resolver's message naming both
files and the error on the cause chain the dashboard's chooser card reads.

On Windows a spec file held open past the read retry may be a second claimant, so
every host refuses an unpinned start that found a claimant while one was held.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew import agent as agent_mod
from kiro_crew import agent_discovery, agent_spec_pins, atomic_write, platform_compat
from kiro_crew.acp import kas_agents, session_mcp
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KAS
from kiro_crew.agent_discovery import AmbiguousAgentSpecError
from kiro_crew.agent_materialization import fork_refresh
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
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


def _hold(monkeypatch: pytest.MonkeyPatch, file_name: str, winerror: int | None) -> None:
    """Every read of *file_name* fails as a Windows hold past the read retry does."""
    real_open = agent_discovery.open_fenced_for_read

    def held_open(resolved, **kwargs):  # type: ignore[no-untyped-def]
        if Path(resolved).name == file_name:
            exc = PermissionError(13, "being used by another process", str(resolved))
            exc.winerror = winerror  # type: ignore[attr-defined]
            raise exc
        return real_open(resolved, **kwargs)

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(agent_discovery, "open_fenced_for_read", held_open)


@pytest.mark.parametrize(
    ("winerror", "pin", "refused"),
    [
        (32, None, "held"),  # a share-mode hold, which fails the open
        (33, None, "held"),  # a lock violation at the open
        (None, None, "held"),  # a byte-range lock, which fails the read
        (5, None, None),  # an ACL denial refuses every reader alike: not a hold
        (32, "PkgB-helper.json", "pin"),  # a pin on the held file stays broken
        (32, "PkgA-helper.json", None),  # a pin on the readable file runs it
        (32, None, "sole"),  # the only spec held: no claimant, the no-agent-file outcome
    ],
)
def test_a_spec_held_by_another_program_refuses_an_unpinned_start(
    agents, pins, monkeypatch, winerror, pin, refused
):
    if refused == "sole":
        _write(agents, "PkgB-helper.json", name="helper", prompt="B")
        refused = None
    else:
        _contest(agents)
    _hold(monkeypatch, "PkgB-helper.json", winerror)
    if pin is not None:
        pins["helper"] = pin
    if refused is None:
        session_mcp.refuse_contested_agent_spec("helper")
        return
    with pytest.raises(AmbiguousAgentSpecError) as caught:
        session_mcp.refuse_contested_agent_spec("helper")
    if refused == "pin":
        assert caught.value.pin == "PkgB-helper.json"
        return
    message = str(caught.value)
    assert "PkgB-helper.json" in message and "kirocrew agent pin" in message
    # Which agent the held file declares is unknown: no chooser card replaces the text.
    assert ambiguity_meta(caught.value) is None


async def _start(host: str, agents: Path, work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start a session on ``helper`` through *host*'s own spec resolution."""
    if host != "kas":
        launch = "helper" if host == "kiro-cli" else f"{NATIVE_SKILL_ALIAS_PREFIX}abc"
        fork_refresh.require_launch_resolves_one_file("helper", launch)
        return
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp.session_handle import AcpAgentUnavailable
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.mcp_gateway import session_servers

    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers, "injection_server_names", lambda overlay, agent, **_: frozenset()
    )
    try:
        await harness_for(ACP_BACKEND_KAS).session_extras("helper", work_dir=str(work))
    except AcpAgentUnavailable:
        # No file declares the agent: the start's own "no agent file" outcome.
        return


def _resolver_error(exc: BaseException) -> AmbiguousAgentSpecError:
    """The resolver's error on *exc*'s cause chain."""
    seen: BaseException | None = exc
    while seen is not None and not isinstance(seen, AmbiguousAgentSpecError):
        seen = seen.__cause__
    assert seen is not None, exc
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["kiro-cli", "kiro-cli view", "kas"])
@pytest.mark.parametrize(
    ("winerror", "pin", "refused"),
    [
        (32, None, "held"),
        (33, None, "held"),
        (None, None, "held"),
        (5, None, None),
        (32, "PkgB-helper.json", "pin"),
        (32, "PkgA-helper.json", None),
        (32, None, "sole"),
        # Nothing held on Windows: what a skill view built from a scan that missed a file
        # once it was held meets at its start, the contest itself.
        ("released", None, "contest"),
    ],
)
async def test_a_held_spec_refuses_an_unpinned_kiro_cli_or_kas_start(
    agents, pins, monkeypatch, tmp_path, host, winerror, pin, refused
):
    """kiro-cli (handed the authored name or a skill view) and KAS resolve the spec
    themselves, and refuse a held claimant as the adapter-host check does."""
    from kiro_crew.acp.session_handle import AcpRuntimeError

    if refused == "sole":
        _write(agents, "PkgB-helper.json", name="helper", prompt="B")
        refused = None
    else:
        _contest(agents)
    if winerror == "released":
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    else:
        _hold(monkeypatch, "PkgB-helper.json", winerror)
    if pin is not None:
        pins["helper"] = pin
    if refused is None:
        await _start(host, agents, tmp_path / "work", monkeypatch)
        return
    with pytest.raises((fork_refresh.AgentSpecLaunchUnresolved, AcpRuntimeError)) as caught:
        await _start(host, agents, tmp_path / "work", monkeypatch)
    cause = _resolver_error(caught.value)
    if refused == "pin":
        assert cause.pin == "PkgB-helper.json"
        return
    if refused == "contest":
        assert ambiguity_meta(caught.value) is not None
        return
    message = str(caught.value)
    assert "PkgB-helper.json" in message and "kirocrew agent pin" in message
    assert cause.pin is None and ambiguity_meta(caught.value) is None
    # A reader that starts no session (a running session's spec hooks) keeps its answer.
    assert kas_agents.load_agent_spec(agents, "helper")["prompt"] == "A"


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
