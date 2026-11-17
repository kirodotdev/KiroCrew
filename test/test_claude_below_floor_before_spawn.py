"""A Claude session that needs the session options, on an adapter too old to honour them.

claude-agent-acp below ``CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION`` may ignore
``_meta.claudeCode.options``, so a session that depends on them (a zero-tool
background session, a Side Chat, a narrowed worker) is refused. When the installed
package's version is known before the spawn, the refusal comes before any adapter
or Claude Code process starts, and it is never retried with a fresh process.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import SpawnContext
from kiro_crew.acp.harness import claude as claude_mod
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution


@pytest.fixture
def _adapter(monkeypatch):
    """The installed adapter: its entry script and, per test, its package version."""
    monkeypatch.setattr(
        claude_mod, "_resolve_claude_acp_bin", lambda: (["/opt/bin/claude-agent-acp"], "")
    )
    forget_cached_resolution(ACP_BACKEND_CLAUDE)
    versions: list[str] = ["0.33.1"]
    monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", lambda _argv: versions[0])
    yield versions
    forget_cached_resolution(ACP_BACKEND_CLAUDE)


def _client(tmp_path, monkeypatch, *, zero_tools: bool) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)

    async def project_array() -> None:
        # What the spec projection records for a ``"tools": []`` agent.
        client._spec_zero_tools = zero_tools

    monkeypatch.setattr(client, "_seed_session_settings", AsyncMock())
    monkeypatch.setattr(client, "_prepare_session_mcp", project_array)
    return client


def _resolve(client: AcpClient, tmp_path) -> None:
    ctx = SpawnContext(
        agent="kirocrew-lite",
        work_dir=tmp_path,
        model=None,
        environ={},
        home=tmp_path,
        session=client,
    )
    asyncio.run(process_adapter_for(ACP_BACKEND_CLAUDE).resolve_spawn(ctx))


def test_a_zero_tool_session_is_refused_before_its_process_starts(
    tmp_path, monkeypatch, _adapter
) -> None:
    client = _client(tmp_path, monkeypatch, zero_tools=True)

    with pytest.raises(AcpToolGateUnroutable, match="0.33.1") as refused:
        _resolve(client, tmp_path)

    # Worded for the person: chat surfaces show only such a message, with its remedy.
    assert refused.value.user_worded is True
    assert "npm i -g" in str(refused.value)


@pytest.mark.parametrize("version", ["0.84.0", "0.107.0", ""])
def test_a_floor_version_or_an_unread_one_starts_the_process(
    tmp_path, monkeypatch, _adapter, version
) -> None:
    # An unread version keeps the handshake check, which judges the version the
    # adapter itself reports.
    _adapter[0] = version
    client = _client(tmp_path, monkeypatch, zero_tools=True)

    _resolve(client, tmp_path)


def test_a_remembered_handshake_of_a_wrapper_is_left_to_the_handshake_check(
    tmp_path, monkeypatch, _adapter
) -> None:
    # A wrapper has no readable package version. Its earlier handshake still feeds
    # the settings floor, but it never refuses before spawn: the refused session
    # would never reach the handshake that records an upgrade.
    _adapter[0] = ""
    monkeypatch.setattr(
        claude_mod,
        "_claude_adapter_handshake_versions",
        {("/opt/bin/claude-agent-acp",): "0.33.1"},
    )
    client = _client(tmp_path, monkeypatch, zero_tools=True)

    _resolve(client, tmp_path)

    assert client._claude_adapter_disk_version == "0.33.1"


def test_a_chat_that_needs_no_options_starts_on_an_old_adapter(
    tmp_path, monkeypatch, _adapter
) -> None:
    client = _client(tmp_path, monkeypatch, zero_tools=False)

    _resolve(client, tmp_path)


@pytest.mark.asyncio
async def test_the_refusal_starts_no_process_and_is_not_retried(
    tmp_path, monkeypatch, _adapter
) -> None:
    client = _client(tmp_path, monkeypatch, zero_tools=True)
    resolves: list[object] = []
    plans: list[object] = []
    real_resolve = claude_mod.ClaudeLaunch.resolve_spawn

    async def counted_resolve(self, ctx):
        # ``_spawn`` launches only with the plan this returns, so no plan means no process.
        resolves.append(ctx)
        plan = await real_resolve(self, ctx)
        plans.append(plan)
        return plan

    monkeypatch.setattr(claude_mod.ClaudeLaunch, "resolve_spawn", counted_resolve)
    # The workspace preparation reads the checkout with git, which is not what is tested.
    monkeypatch.setattr(client, "_prepare_spawn_workspace", lambda: None)

    with pytest.raises(AcpToolGateUnroutable):
        await client.ensure_ready()

    assert plans == []
    assert len(resolves) == 1
