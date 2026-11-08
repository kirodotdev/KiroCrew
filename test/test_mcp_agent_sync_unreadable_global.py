"""An unreadable kiro-global mcp.json is skipped by the agent-spec sync, not raised.

``_sync_mcp_to_agent`` copies a server's spec out of ``~/.kiro/settings/mcp.json``,
a file the user edits by hand. Its own ``except`` says what a file it cannot use
means: skip the copy (single server) or treat it as empty (batch). A file saved
in a legacy code page raises ``UnicodeDecodeError`` and one the gateway may not
read raises ``PermissionError`` -- neither is a ``FileNotFoundError`` or a
``JSONDecodeError``, so both escaped the ``except`` and failed the request that
called the sync after its earlier steps had already been applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# One accented byte in an env value, as Notepad "ANSI" / any latin-1 editor saves it.
_LATIN1_GLOBAL = json.dumps(
    {"mcpServers": {"slack-mcp": {"command": "slack-mcp", "env": {"NOTE": "caf\u00e9"}}}},
    ensure_ascii=False,
).encode("latin-1")


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    """Run as the dashboard owner: the owner boundary has its own coverage."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _unreadable_global(kind: str, tmp_path: Path):
    if kind == "latin-1":
        path = tmp_path / "mcp.json"
        path.write_bytes(_LATIN1_GLOBAL)
        with pytest.raises(UnicodeDecodeError):
            path.read_text(encoding="utf-8")
        return path
    stub = MagicMock(spec=Path)
    stub.read_text.side_effect = PermissionError(13, "Permission denied", "mcp.json")
    return stub


@pytest.fixture(params=["latin-1", "permission-denied"])
def env(request, tmp_path: Path):
    """An agent spec with ``builder-mcp`` switched off, beside an unreadable global file."""
    agent_cfg = tmp_path / "kirocrew.json"
    agent_cfg.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {"builder-mcp": {"command": "builder-mcp", "disabled": True}},
                "tools": [],
                "allowedTools": [],
            }
        ),
        encoding="utf-8",
    )
    with (
        patch(
            "kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON",
            _unreadable_global(request.param, tmp_path),
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents._installed_agent_config", return_value=agent_cfg
        ),
        patch("kiro_crew.dashboard.handlers.mcp.may_skip_gate_now", lambda ref: True),
    ):
        yield agent_cfg


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sync_must_not_raise(fn, *args, **kwargs) -> None:
    """An escaping read error is the bug, so report it as a failed check."""
    try:
        fn(*args, **kwargs)
    except (OSError, UnicodeDecodeError) as exc:
        pytest.fail(f"sync raised on an unreadable global mcp.json: {exc!r}")


def test_single_sync_skips_the_copy(env):
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

    before = _load(env)
    # slack-mcp needs the global spec, so the copy is skipped.
    _sync_must_not_raise(_sync_mcp_to_agent, "slack-mcp", enabled=True)
    assert _load(env) == before


def test_batch_sync_still_syncs_the_servers_it_has(env):
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch

    _sync_must_not_raise(_sync_mcp_to_agent_batch, ["builder-mcp", "slack-mcp"], enabled=True)
    cfg = _load(env)
    # builder-mcp is already in the spec, so the enable lands without the global file.
    assert "disabled" not in cfg["mcpServers"]["builder-mcp"]
    assert "@builder-mcp" in cfg["tools"]
    # slack-mcp's spec lives only in the unreadable file: not copied, not mounted.
    assert "slack-mcp" not in cfg["mcpServers"]
    assert "@slack-mcp" not in cfg["tools"]


@pytest.mark.asyncio
async def test_capability_install_answers_ok(env):
    """The handler that reaches the copy: the package is installed before the sync,
    so a raise there answered 500 for an install that had already happened."""
    from kiro_crew.dashboard.handlers.agents import api_capability_mcp_install
    from kiro_crew.platform.interfaces import CapabilityResult

    mgr = MagicMock()
    mgr.available.return_value = True
    mgr.install_mcp = AsyncMock(return_value=CapabilityResult(ok=True, message=""))
    req = MagicMock()
    req.json = AsyncMock(return_value={"server_id": "slack-mcp"})
    req.app = {"state": MagicMock()}
    with patch("kiro_crew.dashboard.handlers.agents._capability_manager", return_value=mgr):
        resp = await api_capability_mcp_install(req)

    assert resp.status == 200, resp.text
    mgr.install_mcp.assert_awaited_once_with("slack-mcp")
