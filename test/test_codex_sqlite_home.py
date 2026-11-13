"""Each codex runtime gets its own SQLite home.

Every ``codex app-server`` opens the same SQLite files by default and they lock
each other out: Codex Desktop plus a Crew runtime fail new sessions with
``database is locked``. The harness names the need (``SpawnPlan.private_state_env``)
and the runtime points the variable at a per-process directory no other process is
handed. Two seams, asserted separately: what the harness asks for, and what the
runtime does with the ask.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import agent_scratch
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.codex_sqlite import seed_private_state
from kiro_crew.acp.harness import SpawnContext
from kiro_crew.acp.harness import codex as codex_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnPlan
from kiro_crew.acp.harness.codex import _read_codex_mcp_disable_overrides
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


# ── The runtime answers it with a directory of its own ──


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


@pytest.mark.asyncio
async def test_the_sqlite_home_is_not_the_session_trees_work_dir(
    codex_spawn_stubbed, tmp_path, monkeypatch
):
    """Every process spawned for this runtime's sessions mounts its work directory
    writable, so the databases sit in a window this codex process alone is handed."""
    from test_subagent_shared_scratch import TestSpawnersMountTheTreeWindow

    monkeypatch.delenv(SQLITE_HOME, raising=False)  # a host value would be the operator's
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(agent_scratch, "config_dir", lambda: home)
    seeded: list[Path] = []
    monkeypatch.setattr(codex_mod, "seed_private_state", lambda env, path: seeded.append(path))
    wrapped, stop = TestSpawnersMountTheTreeWindow._install_capture(
        monkeypatch, runtime_mod, tmp_path, "create_subprocess_limited"
    )
    runtime = runtime_mod.AcpRuntime(work_dir=tmp_path / "workspace", acp_backend=ACP_BACKEND_CODEX)
    with pytest.raises(stop):
        await runtime.spawn()

    sqlite_home = Path(wrapped["spawn_kwargs"]["env"][SQLITE_HOME])  # type: ignore[index]
    assert sqlite_home.parent == home / "scratch"
    assert str(sqlite_home) in wrapped["kwargs"]["extra_private_dirs"]  # type: ignore[index]
    assert seeded == [sqlite_home]
    assert runtime.work_scratch_dir is not None
    assert sqlite_home != runtime.work_scratch_dir


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


# ── _read_codex_mcp_disable_overrides ──

_REFUSED_FOR_STDIO = (
    "url",
    "bearer_token_env_var",
    "bearer_token",
    "http_headers",
    "oauth",
    "auth",
)
_REFUSED_FOR_HTTP = ("args", "env", "env_vars", "cwd", "bearer_token")


def _codex_accepts(entry: dict) -> bool:
    """codex's ``TryFrom<RawMcpServerConfig>`` transport rule (codex-rs mcp_types.rs).

    ``command`` selects stdio and refuses HTTP-only keys; otherwise ``url``
    selects streamable HTTP and refuses stdio-only keys; neither is
    ``invalid transport``, which fails the whole ``thread/start``.
    """
    if "command" in entry:
        return not any(k in entry for k in _REFUSED_FOR_STDIO)
    if "url" in entry:
        return not any(k in entry for k in _REFUSED_FOR_HTTP)
    return False


def _merge(base: dict, overlay: dict) -> dict:
    """codex's recursive table merge: overlay keys win, tables merge."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def test_read_mcp_overrides_section_headers(tmp_path):
    """Standard [mcp_servers.<name>] headers each get a disabled stdio entry."""
    (tmp_path / "config.toml").write_text(
        "[mcp_servers.my-server]\ncommand = 'npx'\n\n[mcp_servers.another]\ncommand = 'python'\n",
        encoding="utf-8",
    )
    overrides = _read_codex_mcp_disable_overrides(tmp_path / "config.toml")
    assert list(overrides) == ["my-server", "another"]
    assert all(o["enabled"] is False and "command" in o for o in overrides.values())


def test_read_mcp_overrides_dotted_key(tmp_path):
    """Dotted top-level keys mcp_servers.<name> = {...} are also read."""
    (tmp_path / "config.toml").write_text(
        'mcp_servers.evil2 = { command = "/bin/sh" }\n', encoding="utf-8"
    )
    assert "evil2" in _read_codex_mcp_disable_overrides(tmp_path / "config.toml")


def test_read_mcp_overrides_dotted_in_name(tmp_path):
    """A quoted server name containing a dot is preserved as one name."""
    (tmp_path / "config.toml").write_text(
        "[mcp_servers.\"my.server\"]\ncommand = 'npx'\n", encoding="utf-8"
    )
    assert list(_read_codex_mcp_disable_overrides(tmp_path / "config.toml")) == ["my.server"]


def test_read_mcp_overrides_keep_each_servers_transport(tmp_path):
    """An HTTP server gets a url placeholder; one with no transport is left out."""
    (tmp_path / "config.toml").write_text(
        "[mcp_servers.web]\nurl = 'https://example.invalid/mcp'\n"
        "[mcp_servers.local]\ncommand = 'npx'\nargs = ['srv']\n"
        "[mcp_servers.broken]\nenabled = true\n",
        encoding="utf-8",
    )
    overrides = _read_codex_mcp_disable_overrides(tmp_path / "config.toml")
    assert set(overrides) == {"web", "local"}
    assert "url" in overrides["web"] and "command" not in overrides["web"]
    assert "command" in overrides["local"] and "url" not in overrides["local"]


def test_read_mcp_overrides_malformed_toml(tmp_path):
    """Malformed TOML returns no overrides, so none is injected."""
    (tmp_path / "config.toml").write_text("[mcp_servers.x\n", encoding="utf-8")
    assert _read_codex_mcp_disable_overrides(tmp_path / "config.toml") == {}


def test_read_mcp_overrides_empty_config(tmp_path):
    """A config with no mcp_servers returns no overrides."""
    (tmp_path / "config.toml").write_text('model = "gpt-4o"\n', encoding="utf-8")
    assert _read_codex_mcp_disable_overrides(tmp_path / "config.toml") == {}


def test_read_mcp_overrides_missing_file(tmp_path):
    """A missing config.toml returns no overrides without raising."""
    assert _read_codex_mcp_disable_overrides(tmp_path / "config.toml") == {}


def test_override_still_parses_after_its_server_is_removed(tmp_path):
    """A server removed from config.toml after spawn leaves a valid, disabled entry.

    codex re-reads config.toml on every thread/start while CODEX_CONFIG stays as
    it was at spawn. An entry carrying only ``enabled`` would then have no
    transport, and codex would fail every background session/new.
    """
    at_spawn = (
        "[mcp_servers.local]\ncommand = 'npx'\nargs = ['srv']\nenv = { K = 'v' }\n"
        "[mcp_servers.web]\nurl = 'https://example.invalid/mcp'\n"
        "bearer_token_env_var = 'TOKEN'\n"
    )
    (tmp_path / "config.toml").write_text(at_spawn, encoding="utf-8")
    overrides = _read_codex_mcp_disable_overrides(tmp_path / "config.toml")
    live = tomllib.loads(at_spawn)["mcp_servers"]

    # Unchanged config: every merged entry is valid and disabled.
    merged = _merge(live, overrides)
    assert all(_codex_accepts(e) and e["enabled"] is False for e in merged.values())

    # Both servers removed after spawn: the override stands alone.
    merged = _merge({}, overrides)
    assert set(merged) == {"local", "web"}
    assert all(_codex_accepts(e) and e["enabled"] is False for e in merged.values())


# ── apply_spawn_env injects CODEX_CONFIG for background sessions ──


@pytest.mark.asyncio
async def test_background_spawn_env_sets_codex_config_with_servers(codex_spawn_stubbed, tmp_path):
    """After a background-runtime spawn, apply_spawn_env sets CODEX_CONFIG."""
    codex_home = tmp_path / "fake_codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "[mcp_servers.srv1]\ncommand='npx'\n[mcp_servers.srv2]\ncommand='python'\n",
        encoding="utf-8",
    )
    harness = harness_for(ACP_BACKEND_CODEX)
    # populates self._bg_codex_config
    await harness.resolve_spawn(_bg_ctx(tmp_path, {"CODEX_HOME": str(codex_home)}))

    env: dict[str, str] = {}
    harness.apply_spawn_env(env)

    assert "CODEX_CONFIG" in env
    config = json.loads(env["CODEX_CONFIG"])
    assert config["mcp_servers"]["srv1"]["enabled"] is False
    assert config["mcp_servers"]["srv2"]["enabled"] is False


@pytest.mark.asyncio
async def test_background_spawn_env_does_not_overwrite_existing_codex_config(
    codex_spawn_stubbed, tmp_path, caplog
):
    """apply_spawn_env does not overwrite an operator-set CODEX_CONFIG, and warns."""
    codex_home = tmp_path / "fake_codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("[mcp_servers.srv1]\ncommand='npx'\n", encoding="utf-8")
    harness = harness_for(ACP_BACKEND_CODEX)
    await harness.resolve_spawn(_bg_ctx(tmp_path, {"CODEX_HOME": str(codex_home)}))

    env: dict[str, str] = {"CODEX_CONFIG": '{"model":"custom"}'}
    with caplog.at_level(logging.WARNING, logger=codex_mod.logger.name):
        harness.apply_spawn_env(env)

    # Operator's CODEX_CONFIG is preserved unchanged, and the skip is logged.
    assert env["CODEX_CONFIG"] == '{"model":"custom"}'
    assert any("CODEX_CONFIG is already set" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_foreground_apply_spawn_env_does_not_set_codex_config(codex_spawn_stubbed, tmp_path):
    """Foreground sessions: apply_spawn_env does not inject CODEX_CONFIG."""
    codex_home = tmp_path / "fake_codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("[mcp_servers.srv1]\ncommand='npx'\n", encoding="utf-8")
    ctx = _ctx(tmp_path, environ={"CODEX_HOME": str(codex_home)})
    harness = harness_for(ACP_BACKEND_CODEX)
    await harness.resolve_spawn(ctx)  # agent="a", not background

    env: dict[str, str] = {}
    harness.apply_spawn_env(env)
    assert "CODEX_CONFIG" not in env
