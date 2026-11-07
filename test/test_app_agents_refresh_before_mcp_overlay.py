"""The MCP overlay is built from app agent specs that name the running launcher.

An app agent that lists ``@kirocrew-core`` in ``tools`` gets that server's launch spec
copied in when it is materialized. The dashboard's app reconcile, which rewrites the
spec, runs after the boot overlay pass, so the boot refreshes app agents first.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew import session
from kiro_crew.apps import bridges
from kiro_crew.apps.manager import enable_app, install_app
from kiro_crew.metrics import sessions as session_metrics
from kiro_crew.slack import gateway
from kiro_crew.slack.gateway import GatewayOrchestrator

_OLD = {"command": "/old/kirocrew", "args": ["mcp-core"]}
_NEW = {"command": "/new/kirocrew", "args": ["mcp-core"]}


class _ReachedOverlay(Exception):
    pass


@pytest.fixture
def stale_spec(app_env, tmp_path, monkeypatch) -> Path:
    """An enabled app's agent spec, materialized by a version whose launcher moved."""
    src = _make_app_source(tmp_path)
    (src / "agents" / "my-agent.json").write_text(
        json.dumps({"name": "my-agent", "tools": ["@kirocrew-core"]})
    )
    install_app(src)
    enable_app("test-app")
    spec = app_env["kiro_agents"] / "test-app--my-agent.json"
    spec.write_text(json.dumps({"name": "my-agent", "mcpServers": {"kirocrew-core": _OLD}}))
    monkeypatch.setattr(bridges, "_managed_mcp_spec", lambda name: dict(_NEW))
    return spec


@pytest.mark.asyncio
@pytest.mark.parametrize(("stub_servers", "expected"), [(["kirocrew-core"], _NEW), ([], _OLD)])
async def test_boot_refreshes_app_agents_before_the_overlay_pass(
    stale_spec, tmp_path, monkeypatch, stub_servers, expected
) -> None:
    """With nothing routed no overlay is built, so the boot leaves the spec alone."""
    seen: dict = {}

    async def _overlay_pass() -> None:
        seen.update(json.loads(stale_spec.read_text(encoding="utf-8")))
        raise _ReachedOverlay

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._background_tasks = set()
    orch._test_mode = True
    orch._no_dashboard = True
    orch._cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(stub_servers=stub_servers))
    monkeypatch.setattr(orch, "_init_services", AsyncMock())
    monkeypatch.setattr(orch, "_start_embeddings", AsyncMock())
    monkeypatch.setattr(orch, "_init_mcp_gateway", _overlay_pass)
    monkeypatch.setattr(gateway.crash_guard, "install_loop_handler", MagicMock())
    monkeypatch.setattr(gateway.platform_compat, "raise_nofile_soft_limit", MagicMock())
    monkeypatch.setattr(gateway.platform_compat, "probe_file_persistence", lambda home: None)
    monkeypatch.setattr(gateway, "data_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "warm_backend", MagicMock())
    monkeypatch.setattr(gateway.cautious_boot, "initialize", AsyncMock())
    monkeypatch.setattr(gateway.cautious_boot, "pause_before", AsyncMock())
    monkeypatch.setattr(session, "cleanup_orphaned_sessions", MagicMock())
    monkeypatch.setattr(session_metrics, "backfill_crashed_sessions", lambda cutoff: 0)

    try:
        with pytest.raises(_ReachedOverlay):
            await orch.run()
    finally:
        await asyncio.gather(*tuple(orch._background_tasks))

    assert seen["mcpServers"]["kirocrew-core"] == expected
