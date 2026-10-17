"""Each codex runtime gets its own SQLite home.

Every ``codex app-server`` opens the same SQLite files by default and they lock
each other out: Codex Desktop plus a Crew runtime fail new sessions with
``database is locked``. The harness names the need (``SpawnPlan.private_state_env``)
and the runtime points the variable at its own per-process scratch directory. Two
seams, asserted separately: what the harness asks for, and what the runtime does
with the ask.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.codex_sqlite import seed_private_state
from kiro_crew.acp.harness import SpawnContext
from kiro_crew.acp.harness import codex as codex_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnPlan
from kiro_crew.acp.harness.codex import _read_codex_mcp_server_names
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KIRO

SQLITE_HOME = "CODEX_SQLITE_HOME"


def _ctx(tmp_path: Path, environ: dict[str, str] | None = None) -> SpawnContext:
    return SpawnContext(
        agent="a", work_dir=str(tmp_path), model=None, environ=environ or {}, home=tmp_path
    )


@pytest.fixture
def codex_spawn_stubbed(monkeypatch):
    """The codex spawn's disk and sandbox work, pinned so only the plan is exercised."""
    monkeypatch.setattr(
        client_mod, "_resolve_codex_acp_bin", lambda: (["/n/node", "/p/index.js"], "/s")
    )
    monkeypatch.setattr(codex_mod, "resolve_spawn_masks", AsyncMock(return_value=((), ())))
    monkeypatch.setattr(codex_mod, "_sandbox_wrapper_generations", lambda mode: 0)


@pytest.fixture
def kiro_spawn_stubbed(monkeypatch):
    """The kiro spawn's binary search and pre-spawn gates, all answering "go"."""

    async def _bin(*, environ, home):
        return "/pinned/kiro-cli"

    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(
        sandbox_mod, "delegated_workspace_exposes_sealed_target", lambda work_dir: ""
    )


# ── The harness names the need ──


@pytest.mark.asyncio
async def test_codex_asks_for_a_private_sqlite_home(codex_spawn_stubbed, tmp_path):
    plan = await harness_for(ACP_BACKEND_CODEX).resolve_spawn(_ctx(tmp_path))
    assert plan.private_state_env == SQLITE_HOME
    assert plan.private_state_seed is seed_private_state


@pytest.mark.asyncio
async def test_codex_leaves_an_operator_set_sqlite_home_alone(codex_spawn_stubbed, tmp_path):
    """The operator chose that location; the variable reaches the child as set."""
    ctx = _ctx(tmp_path, environ={SQLITE_HOME: "/operator/sqlite"})
    plan = await harness_for(ACP_BACKEND_CODEX).resolve_spawn(ctx)
    assert plan.private_state_env is None


@pytest.mark.asyncio
async def test_kiro_asks_for_no_private_state(kiro_spawn_stubbed, tmp_path):
    """kiro-cli tolerates concurrent processes: the Kiro path gains nothing (H13)."""
    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(
        dataclasses.replace(_ctx(tmp_path), model="m")
    )
    assert plan.private_state_env is None
    assert plan.private_state_seed is None


# ── The runtime answers it with its scratch directory ──


def test_the_variable_points_at_the_scratch_dir(tmp_path):
    env: dict[str, str] = {"PATH": "/bin"}
    runtime_mod._point_private_state_at_scratch(env, SQLITE_HOME, tmp_path / "scratch")
    assert env[SQLITE_HOME] == str(tmp_path / "scratch")
    assert env["PATH"] == "/bin"


def test_a_value_already_in_the_env_is_kept(tmp_path):
    """An operator's or an ``extra_env`` value names a location they chose."""
    env = {SQLITE_HOME: "/operator/sqlite"}
    runtime_mod._point_private_state_at_scratch(env, SQLITE_HOME, tmp_path / "scratch")
    assert env[SQLITE_HOME] == "/operator/sqlite"


def test_an_empty_value_is_not_a_choice(tmp_path):
    env = {SQLITE_HOME: ""}
    runtime_mod._point_private_state_at_scratch(env, SQLITE_HOME, tmp_path / "scratch")
    assert env[SQLITE_HOME] == str(tmp_path / "scratch")


def test_a_host_that_asks_for_nothing_gets_nothing(tmp_path):
    env: dict[str, str] = {}
    runtime_mod._point_private_state_at_scratch(env, None, tmp_path / "scratch")
    assert env == {}


def test_no_scratch_sets_nothing_and_warns_once(caplog):
    """No directory to offer: the child gets the host's shared default, said once."""
    env: dict[str, str] = {}
    with caplog.at_level(logging.WARNING, logger=runtime_mod.logger.name):
        runtime_mod._point_private_state_at_scratch(env, SQLITE_HOME, None)
    assert SQLITE_HOME not in env
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert SQLITE_HOME in warnings[0].getMessage()


# ── The runtime lets the host seed that directory first ──


def _seeding_plan(calls: list, env_var: str | None = SQLITE_HOME) -> SpawnPlan:
    return SpawnPlan(
        argv=["codex"],
        private_state_env=env_var,
        private_state_seed=lambda env, scratch: calls.append((dict(env), scratch)),
    )


def test_the_seed_runs_with_the_env_and_scratch(tmp_path):
    calls: list = []
    env = {"CODEX_HOME": "/h"}
    runtime_mod._seed_private_state(env, _seeding_plan(calls), tmp_path)
    assert calls == [({"CODEX_HOME": "/h"}, tmp_path)]


@pytest.mark.parametrize(
    "env, env_var, has_scratch",
    [
        ({SQLITE_HOME: "/operator/sqlite"}, SQLITE_HOME, True),
        ({}, None, True),
        ({}, SQLITE_HOME, False),
    ],
    ids=["operator-location", "no-private-state", "no-scratch"],
)
def test_the_seed_is_skipped(tmp_path, env, env_var, has_scratch):
    calls: list = []
    scratch = tmp_path if has_scratch else None
    runtime_mod._seed_private_state(env, _seeding_plan(calls, env_var), scratch)
    assert calls == []


def test_a_plan_without_a_seed_is_a_no_op(tmp_path):
    plan = SpawnPlan(argv=["codex"], private_state_env=SQLITE_HOME)
    runtime_mod._seed_private_state({}, plan, tmp_path)


# ── Background sessions: per-server MCP disable via CODEX_CONFIG ──

_BACKGROUND = "kirocrew-lite"  # session.BACKGROUND_AGENT


def _bg_ctx(tmp_path: Path, environ: dict[str, str] | None = None) -> SpawnContext:
    return SpawnContext(
        agent=_BACKGROUND, work_dir=str(tmp_path), model=None, environ=environ or {}, home=tmp_path
    )


@pytest.mark.asyncio
async def test_background_codex_asks_for_sqlite_home_with_bg_seed(
    codex_spawn_stubbed, tmp_path
):
    """Background sessions use CODEX_SQLITE_HOME isolation and the MCP-disable seed."""
    plan = await harness_for(ACP_BACKEND_CODEX).resolve_spawn(_bg_ctx(tmp_path))
    assert plan.private_state_env == SQLITE_HOME
    assert plan.private_state_seed is seed_private_state


@pytest.mark.asyncio
async def test_foreground_codex_still_uses_standard_seed(codex_spawn_stubbed, tmp_path):
    """Foreground sessions keep the standard sqlite seed; CODEX_CONFIG unchanged."""
    plan = await harness_for(ACP_BACKEND_CODEX).resolve_spawn(_ctx(tmp_path))
    assert plan.private_state_env == SQLITE_HOME
    assert plan.private_state_seed is seed_private_state


# ── _read_codex_mcp_server_names ──

def test_read_mcp_server_names_section_headers(tmp_path):
    """Standard [mcp_servers.<name>] headers are extracted."""
    (tmp_path / "config.toml").write_text(
        "[mcp_servers.my-server]\ncommand = 'npx'\n\n[mcp_servers.another]\ncommand = 'python'\n",
        encoding="utf-8",
    )
    assert _read_codex_mcp_server_names(tmp_path / "config.toml") == ["my-server", "another"]


def test_read_mcp_server_names_dotted_key(tmp_path):
    """Dotted top-level keys mcp_servers.<name> = {...} are also extracted."""
    (tmp_path / "config.toml").write_text(
        'mcp_servers.evil2 = { command = "/bin/sh" }\n', encoding="utf-8"
    )
    names = _read_codex_mcp_server_names(tmp_path / "config.toml")
    assert "evil2" in names


def test_read_mcp_server_names_dotted_in_name(tmp_path):
    """Server names containing dots are preserved as-is."""
    (tmp_path / "config.toml").write_text(
        "[mcp_servers.my.server]\ncommand = 'npx'\n", encoding="utf-8"
    )
    assert "my.server" in _read_codex_mcp_server_names(tmp_path / "config.toml")


def test_read_mcp_server_names_empty_config(tmp_path):
    """A config with no mcp_servers returns an empty list."""
    (tmp_path / "config.toml").write_text('model = "gpt-4o"\n', encoding="utf-8")
    assert _read_codex_mcp_server_names(tmp_path / "config.toml") == []


def test_read_mcp_server_names_missing_file(tmp_path):
    """A missing config.toml returns an empty list without raising."""
    assert _read_codex_mcp_server_names(tmp_path / "config.toml") == []


# ── _seed_bg_disable_mcp ──


# ── apply_spawn_env injects CODEX_CONFIG for background sessions ──

@pytest.mark.asyncio
async def test_background_spawn_env_sets_codex_config_with_servers(
    codex_spawn_stubbed, tmp_path
):
    """After a background-runtime spawn, apply_spawn_env sets CODEX_CONFIG."""
    codex_home = tmp_path / "fake_codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "[mcp_servers.srv1]\ncommand='npx'\n[mcp_servers.srv2]\ncommand='python'\n",
        encoding="utf-8",
    )
    ctx = SpawnContext(
        agent="kirocrew-lite",  # session.BACKGROUND_AGENT
        work_dir=str(tmp_path),
        model=None,
        environ={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
    )
    harness = harness_for(ACP_BACKEND_CODEX)
    await harness.resolve_spawn(ctx)  # populates self._bg_codex_config

    env: dict[str, str] = {}
    harness.apply_spawn_env(env)

    import json
    assert "CODEX_CONFIG" in env
    config = json.loads(env["CODEX_CONFIG"])
    assert config["mcp_servers"]["srv1"]["enabled"] is False
    assert config["mcp_servers"]["srv2"]["enabled"] is False


@pytest.mark.asyncio
async def test_background_spawn_env_does_not_overwrite_existing_codex_config(
    codex_spawn_stubbed, tmp_path
):
    """apply_spawn_env does not overwrite an operator-set CODEX_CONFIG."""
    codex_home = tmp_path / "fake_codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "[mcp_servers.srv1]\ncommand='npx'\n", encoding="utf-8"
    )
    ctx = SpawnContext(
        agent="kirocrew-lite",
        work_dir=str(tmp_path),
        model=None,
        environ={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
    )
    harness = harness_for(ACP_BACKEND_CODEX)
    await harness.resolve_spawn(ctx)

    env: dict[str, str] = {"CODEX_CONFIG": '{"model":"custom"}'}
    harness.apply_spawn_env(env)

    # Operator's CODEX_CONFIG is preserved unchanged
    assert env["CODEX_CONFIG"] == '{"model":"custom"}'


@pytest.mark.asyncio
async def test_foreground_apply_spawn_env_does_not_set_codex_config(
    codex_spawn_stubbed, tmp_path
):
    """Foreground sessions: apply_spawn_env does not inject CODEX_CONFIG."""
    codex_home = tmp_path / "fake_codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "[mcp_servers.srv1]\ncommand='npx'\n", encoding="utf-8"
    )
    ctx = _ctx(tmp_path, environ={"CODEX_HOME": str(codex_home)})
    harness = harness_for(ACP_BACKEND_CODEX)
    await harness.resolve_spawn(ctx)  # agent="a", not background

    env: dict[str, str] = {}
    harness.apply_spawn_env(env)
    assert "CODEX_CONFIG" not in env
