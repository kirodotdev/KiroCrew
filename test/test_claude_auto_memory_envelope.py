"""An incognito or temporary claude session runs with Claude Code's auto-memory off.

Claude Code's auto-memory reads ``<config dir>/projects/<project key>/memory/MEMORY.md``
into every session and writes memory files there with no permission request, so
Crew's gate never sees them. A restricted session (``memory_mode`` incognito or
temporary) carries ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`` in
``_meta.claudeCode.options.env``, which claude-agent-acp spreads over its own
process env, and in the flag tier's ``settings.env``, which no other settings
tier's ``env`` overrides; a persistent session's envelope is unchanged.
"""

from __future__ import annotations

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
