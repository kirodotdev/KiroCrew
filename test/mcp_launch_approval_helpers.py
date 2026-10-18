"""Builders and fixtures the launch-approval test modules share.

Holds the approved and attack commands, the ``launch_approval`` module loader,
the target-spec, agent-spec, rewrite and running-target helpers, the one-launch
approval store, the pool-key builder, the ``toggle_home`` fixture, and the
declare, stub-request, toggle-on and next-boot-rewrite helpers that drive the
toggle and the next gateway start against that home.
``test_mcp_launch_approval.py`` and its ``_toggle``, ``_store_records``,
``_gateway`` and ``_rebind`` siblings import them.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import rewriter

_APPROVED_CMD = sys.executable
_ATTACK_CMD = os.environ.get("COMSPEC") or ("cmd.exe" if os.name == "nt" else "/bin/sh")


def _target_spec(command: str, *args: str) -> str:
    return " ".join(shlex.quote(part) for part in (command, *args))


def _approval_module():
    try:
        return importlib.import_module("kiro_crew.mcp_gateway.launch_approval")
    except ImportError:
        return None


def _write_agent(source: Path, servers: dict) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "a.json").write_text(json.dumps({"name": "a", "mcpServers": servers}))


def _rewrite(tmp_path: Path, stub_servers: frozenset[str], approvals) -> dict[str, str]:
    kwargs = {}
    if "approvals" in inspect.signature(rewriter.rewrite_agents).parameters:
        kwargs["approvals"] = approvals
    _results, target_env = rewriter.rewrite_agents(
        source_dir=tmp_path / "agents",
        overlay_dir=tmp_path / "home" / "mcp-gateway" / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=stub_servers,
        **kwargs,
    )
    return target_env


def _approvals_for(mod, name: str, command: str, args: list[str]):
    """An approval store holding exactly one approved launch for *name*."""
    if mod is None:
        return None
    approvals = mod.LaunchApprovals()
    stem = mod.target_stem(name)
    fingerprint = mod.launch_fingerprint(command, args, {})
    approvals.approved[stem] = {fingerprint}
    approvals.approved_pairs[stem] = {
        mod.launch_pair(rewriter.hash_command(command, args), mod.env_fingerprint({}))
    }
    return approvals


def _targets_running(target_env: dict[str, str], command: str) -> list[str]:
    return [k for k, v in target_env.items() if shlex.split(v)[0] == command]


def _pool_key(server: str, command: str = _APPROVED_CMD, args: list[str] | None = None):
    from kiro_crew.mcp_gateway.pool import PoolKey

    params = inspect.signature(PoolKey).parameters
    values = {name: "" for name in params}
    values.update(
        server_name=server,
        command_args_hash=rewriter.hash_command(command, args or []),
        effective_env_hash=_approval_module().env_fingerprint({}),
    )
    return PoolKey(**values)


def _stub_request(body: dict):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from aiohttp import web
    from body_stream_helpers import attach_body
    from dashboard_owner_helpers import owner_claims

    request = MagicMock(spec=web.Request)
    request.app = {"state": SimpleNamespace(_mcp_gateway_apply_stub=None)}
    attach_body(request, body)
    return owner_claims(request)


@pytest.fixture(name="toggle_home")
def toggle_home_fixture(tmp_path, monkeypatch):
    """A config file, an approval store and an agents dir the toggle and the rewrite share."""
    import kiro_crew.config.loader as loader_mod
    import kiro_crew.config.paths as paths_mod
    from kiro_crew import agent
    from kiro_crew.config.paths import kiro_agents_dir
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(loader_mod, "config_path", lambda: cfg)
    monkeypatch.setattr(mcp_mod, "_local_overlay_section", lambda: {})
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    # The agent spec IS the rebuilt config here; rebuilding it would need the
    # whole managed-server install this hermetic home does not carry.
    monkeypatch.setattr(agent, "rebuild_agent_config", lambda *a, **k: None)
    # Per test: the settings file beside the agents dir is read too.
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: tmp_path / "kiro" / "agents")
    agents = kiro_agents_dir()
    assert agents == tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    return agents


def _declare(agents: Path, where: str, command: str, args: list[str]) -> None:
    entry = {"command": command, "args": args}
    if where == "agent spec":
        (agents / "a.json").write_text(json.dumps({"name": "a", "mcpServers": {"srv": entry}}))
    else:
        (agents / "a.json").write_text(json.dumps({"name": "a", "mcpServers": {}}))
        settings = agents.parent / "settings" / "mcp.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"mcpServers": {"srv": entry}}))


def _next_boot_rewrite(tmp_path: Path, agents: Path):
    """The rewrite the next gateway start runs, against the persisted store."""
    mod = _approval_module()
    approvals = mod.load_approvals()
    _results, target_env = rewriter.rewrite_agents(
        source_dir=agents,
        overlay_dir=tmp_path / "boot" / "mcp-gateway" / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"srv"}),
        approvals=approvals,
    )
    return target_env, approvals


_AUTO_EXPECTED = object()


async def _toggle_on(name: str = "srv", *, expected_launch: object = _AUTO_EXPECTED):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    body = {"name": name, "stub": True}
    if expected_launch is _AUTO_EXPECTED:
        launches = await asyncio.to_thread(
            mcp_mod.launch_resolve.resolve_launches, [name], refresh_specs=False
        )
        if launches.get(name):
            expected_launch = mcp_mod.display_launches(name, launches[name]).get("expected_launch")
    if expected_launch is not None and expected_launch is not _AUTO_EXPECTED:
        body["expected_launch"] = expected_launch
    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request(body))
    return resp.status, json.loads(resp.body)
