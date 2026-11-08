"""An incognito or temporary claude session runs with Claude Code's auto-memory off.

Claude Code's auto-memory reads ``<config dir>/projects/<project key>/memory/MEMORY.md``
into every session and writes memory files there with no permission request, so
Crew's gate never sees them. A restricted session (``memory_mode`` incognito or
temporary) carries ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`` in
``_meta.claudeCode.options.env``, which claude-agent-acp spreads over its own
process env, and in the flag tier's ``settings.env``, which no other settings
tier's ``env`` overrides; a persistent session's envelope is unchanged. A
sub-agent spawned with ``include_memory=false`` carries the same switch in any mode.
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest
import test_acp_session_mcp_client_seam as client_seam
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _write_spec

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.config.loader import AgentConfig, KiroCrewConfig

_OFF = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
_RESTRICTED = ["incognito", "temporary"]


def _client(tmp_path, mode: str, **kw) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)
    client.memory_mode = mode
    return client


def _options(client: AcpClient) -> dict:
    return client._claude_session_meta()["claudeCode"]["options"]


@pytest.mark.parametrize("mode", _RESTRICTED)
def test_a_restricted_session_turns_auto_memory_off(tmp_path, mode):
    """Both the process env and the flag tier carry the switch."""
    assert _options(_client(tmp_path, mode)) == {"env": _OFF, "settings": {"env": _OFF}}


@pytest.mark.parametrize("mode", _RESTRICTED)
def test_a_settings_tier_cannot_turn_the_switch_back_on(tmp_path, mode):
    """Claude Code copies each settings tier's ``env`` over the process env, so a
    user tier forcing the switch off (here carried inline on the exclusion path)
    is overridden in the flag tier, and the inline dict is not mutated."""
    client = _client(tmp_path, mode)
    inline = {"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0", "AWS_REGION": "us-west-2"}}
    client._claude_local_settings_excluded = True
    client._claude_inline_settings = inline
    options = _options(client)
    assert options["settings"]["env"] == {**_OFF, "AWS_REGION": "us-west-2"}
    assert inline["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "0"


def test_a_persistent_session_keeps_its_empty_envelope(tmp_path):
    assert _client(tmp_path, "persistent")._claude_session_meta() == {"claudeCode": {"options": {}}}


@pytest.mark.parametrize("mode", [*_RESTRICTED, "persistent"])
def test_the_switch_composes_with_a_zero_tool_session(tmp_path, mode):
    client = _client(tmp_path, mode)
    client._spec_zero_tools = True
    options = _options(client)
    assert options["strictMcpConfig"] is True
    assert options.get("env") == (None if mode == "persistent" else _OFF)
    assert options.get("settings") == (None if mode == "persistent" else {"env": _OFF})


@pytest.mark.parametrize("mode", [*_RESTRICTED, "persistent"])
def test_the_switch_composes_with_a_side_chat_session(tmp_path, mode):
    client = _client(tmp_path, mode, side_read_only=True)
    client._claude_side_read_denies = ("Read(//secret)", "Read(//secret/**)")
    options = _options(client)
    assert options["strictMcpConfig"] is True
    assert options["allowDangerouslySkipPermissions"] is False
    assert options["settings"]["disableAllHooks"] is True
    assert "Read(//secret)" in options["settings"]["permissions"]["deny"]
    assert options.get("env") == (None if mode == "persistent" else _OFF)
    assert options["settings"].get("env") == (None if mode == "persistent" else _OFF)


@pytest.mark.parametrize("mode", [*_RESTRICTED, "persistent"])
def test_the_switch_composes_with_the_exclusion_path(tmp_path, mode):
    """The inline settings ride beside the switch: their env gains only the switch."""
    client = _client(tmp_path, mode)
    inline = {"env": {"AWS_REGION": "us-west-2"}, "permissions": {"deny": ["Bash(rm:*)"]}}
    client._claude_local_settings_excluded = True
    client._claude_inline_settings = inline
    options = _options(client)
    assert options["settingSources"] == ["user"]
    assert options["allowDangerouslySkipPermissions"] is False
    if mode == "persistent":
        assert options["settings"] == inline
    else:
        assert options["settings"] == {**inline, "env": {"AWS_REGION": "us-west-2", **_OFF}}
    assert inline == {"env": {"AWS_REGION": "us-west-2"}, "permissions": {"deny": ["Bash(rm:*)"]}}
    assert options.get("env") == (None if mode == "persistent" else _OFF)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [*_RESTRICTED, "persistent"])
async def test_the_session_new_request_carries_the_switch(tmp_path, agents_dir, monkeypatch, mode):
    """What goes on the wire for ``session/new`` is the envelope built above."""
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = _client(tmp_path, mode, agent="kirocrew")
    client._write_claude_local_settings()
    sent, killed = client_seam.TestClientSeam._adapter_transport(
        client, monkeypatch, started_mode="default"
    )

    await client._initialize_session()

    assert killed == []
    options = dict(sent)["session/new"]["_meta"]["claudeCode"]["options"]
    assert options.get("env") == (None if mode == "persistent" else _OFF)
    assert (options.get("settings") or {}).get("env") == (None if mode == "persistent" else _OFF)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [*_RESTRICTED, "persistent"])
async def test_the_provider_sets_the_mode_before_the_session_starts(tmp_path, monkeypatch, mode):
    """``AcpProvider.start`` hands its mode to the client before ``session/new``.

    Read at the moment the client starts its process and session, on the first
    start and on a restart of the same provider (a respawn, a model switch).
    """
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, memory_mode=mode)
    client = provider._client
    seen: list[dict] = []

    async def _ensure_ready():
        seen.append(_options(client))

    monkeypatch.setattr(client, "ensure_ready", _ensure_ready)
    monkeypatch.setattr(client, "confirm_member_projection", mock.AsyncMock())
    monkeypatch.setattr(provider, "_apply_effort_overlay", lambda: None)
    monkeypatch.setattr(provider, "_apply_tool_search_overlay", lambda: None)
    monkeypatch.setattr(provider, "_bind_unbound_effort", mock.AsyncMock())
    monkeypatch.setattr(provider, "_apply_initial_effort", mock.AsyncMock())

    await provider.start()
    await provider.start()

    expected = None if mode == "persistent" else _OFF
    assert [options.get("env") for options in seen] == [expected, expected]


# -- A sub-agent spawned with include_memory=false -----------------------------


def _claude_factory(tmp_path):
    """The real provider factory on the claude backend, its config in *tmp_path*."""
    cfg_file = tmp_path / "kirocrew.json"
    cfg_file.write_text(
        json.dumps({"agent": {"provider": "acp", "acp_backend": ACP_BACKEND_CLAUDE}}),
        encoding="utf-8",
    )
    with mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load().create_provider_factory()


def test_a_memory_withholding_subagent_turns_auto_memory_off(tmp_path):
    """The factory names the flag, so its catch-all cannot swallow it, and the
    client's envelope then carries the switch in a persistent session too."""
    provider = _claude_factory(tmp_path)("subagent:s1", cwd=str(tmp_path), include_memory=False)
    assert provider._client.backend == ACP_BACKEND_CLAUDE
    assert provider._client.memory_mode == "persistent"
    assert _options(provider._client) == {"env": _OFF, "settings": {"env": _OFF}}


def _run_kwargs(include_memory: bool) -> dict:
    """The kwargs a dedicated sub-agent run hands ``get_or_create``."""
    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    sessions = mock.MagicMock()
    sessions.get_pid.return_value = None
    sessions.get_approval_policy.return_value = ""
    sessions.get_agent.return_value = ""
    sessions.get_agent_selection.return_value = ("template", "")
    sessions.parent_work_scratch_dir.return_value = None
    ctx_builder = mock.MagicMock()
    ctx_builder.build_message.return_value = ("msg", None)
    ctx_builder.hooks.auto_approve_subagent_tools = False
    client = mock.MagicMock()
    captured: dict = {}

    async def _stream(msg):
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def _get_or_create(key, agent=None, approval_policy="", **kwargs):
        captured.update(kwargs)
        return client, True, False

    client.stream = _stream
    sessions.get_or_create = _get_or_create
    runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="sub1",
        task="test",
        parent_session_key="parent-key",
        include_memory=include_memory,
    )
    runner._log_spawned(info)
    cfg = KiroCrewConfig(agent=AgentConfig())
    with (
        mock.patch.object(runner, "_should_use_session_sharing", return_value=False),
        mock.patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
    ):
        asyncio.run(runner._run_inner(info, "subagent:sub1"))
    return captured


def test_a_memory_withholding_run_hands_the_flag_to_its_session():
    assert _run_kwargs(include_memory=False).get("include_memory") is False
    assert "include_memory" not in _run_kwargs(include_memory=True)


@pytest.mark.asyncio
async def test_a_memory_withholding_session_keeps_the_flag_across_starts():
    """It never takes a warm-pool child, whose ``session/new`` went out without the
    switch, and the identity a reset successor or a compaction restart is rebuilt
    from carries the flag."""
    from kiro_crew.session import SessionManager
    from kiro_crew.session_lifecycle import allocation_identity

    def _provider():
        p = mock.MagicMock()
        p.start = mock.AsyncMock()
        p.shutdown = mock.AsyncMock()
        p.is_process_alive = mock.MagicMock(return_value=True)
        p.exit_code = None
        p.cwd = ""
        return p

    cfg = mock.MagicMock()
    cfg.session.pool_size = 2
    cfg.session.pool_agent = "kirocrew"
    cfg.session.pool_ttl_secs = 1800
    cfg.session.timeout_secs = 3600
    cfg.agent.default_agent = ""
    cfg.agent.model = "auto"
    factory = mock.MagicMock(side_effect=lambda *_a, **_kw: _provider())
    with mock.patch("kiro_crew.session.default_project_dir", return_value=""):
        mgr = SessionManager(cfg, provider_factory=factory)
    mgr._drain_and_claim = mock.AsyncMock(return_value=_provider())
    mgr._schedule_replenish = mock.MagicMock()
    try:
        await mgr.get_or_create("chat-1", agent="kirocrew", include_memory=False)
        identity = allocation_identity(mgr, "chat-1", mgr._sessions["chat-1"])
    finally:
        await mgr.close_all()
    mgr._drain_and_claim.assert_not_awaited()
    assert factory.call_args.kwargs["include_memory"] is False
    assert identity["include_memory"] is False
