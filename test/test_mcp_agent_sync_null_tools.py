"""Enabling an MCP server mounts it when the agent spec says ``"tools": null``.

A hand-edited ``kirocrew.json`` with ``"tools": null`` (or ``"allowedTools": null``)
made the enable sync raise ``TypeError`` (a 500). ``null`` now reads as no list yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import agents
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.dashboard.handlers import mcp_agent_sync


def _agent_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: dict) -> Path:
    p = tmp_path / "kirocrew.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(agents, "_installed_agent_config", lambda: p)
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    monkeypatch.setattr(mcp_mod, "may_skip_gate_now", lambda _ref: True)
    return p


def _enable(batch: bool) -> None:
    try:
        if batch:
            mcp_agent_sync._sync_mcp_to_agent_batch_unlocked(["srv"], True)
        else:
            mcp_agent_sync._sync_mcp_to_agent_unlocked("srv", True)
    except TypeError as exc:  # main iterated the null list and raised (500)
        pytest.fail(f"enable sync raised on a null list: {exc}")


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("key", ["tools", "allowedTools"])
def test_a_null_list_is_read_as_empty_and_the_server_is_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, key: str
) -> None:
    p = _agent_spec(tmp_path, monkeypatch, {"mcpServers": {"srv": {"command": "x"}}, key: None})

    _enable(batch)

    saved = json.loads(p.read_text(encoding="utf-8"))
    assert "@srv" in saved.get(key, [])
