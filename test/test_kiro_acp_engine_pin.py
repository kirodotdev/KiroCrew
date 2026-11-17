"""The Kiro CLI harness pins ``--agent-engine v2`` instead of trusting kiro-cli's default.

kiro-cli 3.0.0 answers a bare ``acp`` on its v3 engine while its help still says
"v2 when neither is set", so a launch without the flag is driven as v2 while v3
answers. These tests pin the probe that decides the flag and both launch sites
that use it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import kiro_cli
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import managed_agent_shadow as managed_shadow_mod
from kiro_crew.acp.harness import SpawnContext, harness_for
from kiro_crew.acp.types import ACP_BACKEND_KIRO

#: The ``--agent-engine`` block of kiro-cli 3.0.0's ``acp --help`` (trailing spaces dropped).
HELP_3_0_0 = """Usage: kiro-cli acp [OPTIONS]

Options:
      --agent <AGENT>
          Name of the agent to use when starting the first session

      --agent-engine <ENGINE>
          Agent engine to use: "v1", "v2", or "v3". Overrides the acp.agentEngine setting; v2 when
          neither is set

          [possible values: v2, v1, v3]

      --auth-method <METHOD>
          Authentication owner for the v3 engine.
"""
#: A kiro-cli older than the engine selector.
HELP_NO_SELECTOR = """Usage: kiro-cli acp [OPTIONS]

Options:
      --agent <AGENT>
          Name of the agent to use when starting the first session
"""
#: A kiro-cli that has dropped v2: pinning it would fail the spawn.
HELP_NO_V2 = HELP_3_0_0.replace("[possible values: v2, v1, v3]", "[possible values: v3]")


@pytest.fixture
def binary(tmp_path, monkeypatch) -> str:
    """A stand-in kiro-cli that is the pinned install."""
    path = tmp_path / "kiro-cli"
    path.write_bytes(b"")
    path.chmod(0o755)
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(path), False))
    return str(path)


@pytest.fixture
def help_runs(monkeypatch):
    """Answer ``acp --help`` with the given text; record every sandboxed probe.

    A ``raise`` answer stands for a probe that timed out, as the runner reports it.
    """
    from kiro_crew import kiro_prerequisite
    from kiro_crew.kiro_prerequisite_process import ProcessResult

    calls: list[list[str]] = []
    answer = {"stdout": HELP_3_0_0, "returncode": 0}

    async def _run(command, args, **kwargs):
        calls.append([command, *args])
        if isinstance(answer.get("raise"), BaseException):
            return ProcessResult(ok=False, timed_out=True, error="process timed out")
        code = answer["returncode"]
        return ProcessResult(ok=code == 0, output=answer["stdout"], returncode=code)

    monkeypatch.setattr(kiro_prerequisite, "_run_process", _run)
    # ``raising=False`` so a tree without the cache still reaches the launch
    # assertions below instead of erroring in setup.
    monkeypatch.setattr(kiro_cli, "_acp_engines_cache", {}, raising=False)
    return SimpleNamespace(calls=calls, answer=answer)


def test_a_kiro_cli_listing_v2_gets_the_explicit_engine(binary, help_runs):
    assert kiro_cli.kiro_acp_engine_args(binary) == ["--agent-engine", "v2"]
    assert help_runs.calls == [[binary, "acp", "--help"]]


@pytest.mark.parametrize("text", [HELP_NO_SELECTOR, HELP_NO_V2], ids=["no-flag", "no-v2"])
def test_a_kiro_cli_without_v2_on_the_flag_keeps_the_bare_argv(binary, help_runs, text):
    """An unknown flag or value fails the whole spawn, so it is never sent."""
    help_runs.answer["stdout"] = text
    assert kiro_cli.kiro_acp_engine_args(binary) == []


def test_a_failed_or_missing_probe_keeps_the_bare_argv(binary, help_runs, tmp_path):
    help_runs.answer.update(stdout="error: unrecognized subcommand 'acp'\n", returncode=2)
    assert kiro_cli.kiro_acp_engine_args(binary) == []
    assert kiro_cli.kiro_acp_engine_args(str(tmp_path / "absent")) == []
    help_runs.answer["raise"] = subprocess.TimeoutExpired("kiro-cli", 15)
    kiro_cli._acp_engines_cache.clear()
    assert kiro_cli.kiro_acp_engine_args(binary) == []


def test_a_binary_that_is_not_the_pinned_install_is_never_probed(binary, help_runs, tmp_path):
    """A PATH-only kiro-cli could be agent-planted, so the launch keeps the bare argv."""
    other = tmp_path / "bin" / "kiro-cli"
    other.parent.mkdir()
    other.write_bytes(b"")
    assert kiro_cli.kiro_acp_engine_args(str(other)) == []
    assert help_runs.calls == []


def test_a_probe_that_did_not_run_is_retried(binary, help_runs):
    help_runs.answer["raise"] = subprocess.TimeoutExpired("kiro-cli", 15)
    assert kiro_cli.kiro_acp_engine_args(binary) == []
    help_runs.answer.pop("raise")
    assert kiro_cli.kiro_acp_engine_args(binary) == ["--agent-engine", "v2"]
    assert len(help_runs.calls) == 2


def test_the_probe_runs_once_per_binary_identity(binary, help_runs):
    """Every spawn asks; only the first one per (path, mtime) spawns ``--help``."""
    for _ in range(3):
        assert kiro_cli.kiro_acp_engine_args(binary) == ["--agent-engine", "v2"]
    assert len(help_runs.calls) == 1


# ── The two launch sites ──


@pytest.fixture
def kiro_spawn_ready(monkeypatch, binary):
    async def _bin(*, environ, home):
        return binary

    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(
        managed_shadow_mod, "managed_agent_shadow_refusal", lambda agent, work_dir: None
    )
    monkeypatch.setattr(
        sandbox_mod, "delegated_workspace_exposes_sealed_target", lambda work_dir: None
    )
    return binary


@pytest.mark.asyncio
async def test_the_kiro_harness_launch_pins_v2_before_the_agent(
    kiro_spawn_ready, help_runs, tmp_path
):
    ctx = SpawnContext(
        agent="a", work_dir=str(tmp_path), model="m", environ={}, home=Path(tmp_path)
    )
    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(ctx)
    assert plan.argv == [
        kiro_spawn_ready,
        "acp",
        "--agent-engine",
        "v2",
        "--agent",
        "a",
        "--model",
        "m",
    ]


@pytest.mark.asyncio
async def test_the_kiro_harness_launch_stays_bare_on_a_kiro_cli_without_the_flag(
    kiro_spawn_ready, help_runs, tmp_path
):
    help_runs.answer["stdout"] = HELP_NO_SELECTOR
    ctx = SpawnContext(
        agent="a", work_dir=str(tmp_path), model=None, environ={}, home=Path(tmp_path)
    )
    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(ctx)
    assert plan.argv == [kiro_spawn_ready, "acp", "--agent", "a"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the empty stand-in binary is not a Win32 executable for the spawn's own probes",
)
@pytest.mark.asyncio
async def test_the_single_process_client_launch_pins_v2_before_the_agent(
    binary, help_runs, tmp_path
):
    """``AcpClient``'s own kiro-cli launch takes the same pin as the harness."""
    from unittest.mock import AsyncMock, patch

    from kiro_crew.acp.client import AcpClient

    wrapped: dict[str, object] = {}

    def capture_wrap(argv, mode, **kwargs):
        wrapped["argv"] = list(argv)
        return list(argv), None

    with (
        patch.object(client_mod, "_resolve_kiro_bin", return_value=binary),
        patch.object(client_mod, "wrap_argv", side_effect=capture_wrap),
        patch.object(client_mod, "assert_voice_runtime_outside_agent_workspace"),
        patch.object(client_mod, "cgroup_scope_argv", side_effect=lambda argv: argv),
        patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=RuntimeError("stop"))),
    ):
        client = AcpClient(work_dir=tmp_path / "workspace")
        with pytest.raises(RuntimeError, match="stop"):
            await client._spawn()

    # The agent name may be a per-spawn skill-view alias, so only the prefix is pinned.
    assert wrapped["argv"][:5] == [binary, "acp", "--agent-engine", "v2", "--agent"]
    assert len(wrapped["argv"]) == 6
