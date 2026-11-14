"""Every harness that drops a remote MCP server's ``oauth`` block says so once.

R-MCP-8 (``docs/architecture/harness-requirements.md``): only kiro-cli honours the
block (``ACP_BACKENDS_REMOTE_MCP_OAUTH``). KAS names it in its spec-keys row; every
other known backend gets the same sentence from ``_notice_dropped_mcp_oauth``
(``agent_sdk.spec_mcp_oauth``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_REMOTE_MCP_OAUTH,
)
from kiro_crew.dashboard.chat_utils_injected_rows import MCP_SIGN_IN_UNSUPPORTED_NOTICE_KIND

_SIGN_IN = "Sign-in to MCP server slack will not work"
# A `clientSecret` block: KAS never sends it on any engine, so KAS names it too.
_SPEC = {
    "mcpServers": {
        "slack": {
            "url": "https://mcp.example.com/mcp",
            "oauth": {"clientId": "123.456", "clientSecret": "s3cret"},
        },
        "plain": {"url": "https://other.example.com/mcp"},
    }
}


def _surfaced(
    monkeypatch,
    backend: str,
    *,
    bridged: bool = False,
    slot=None,
    reads: list | None = None,
    project: dict | None = None,
) -> list[dict]:
    """The sign-in rows ``_prepare_spec_hooks`` surfaces on a new *backend* session."""
    from kiro_crew.acp import kas_env_launch, kas_projection_reads, session_mcp
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(spec_hooks, "_agent_spec_with_origin", lambda *_a, **_k: (_SPEC, "user"))

    def _read(*args, **_kw):
        if reads is not None:
            reads.append(args)
        return _SPEC

    monkeypatch.setattr(spec_hooks, "_agent_spec", _read)
    monkeypatch.setattr(kas_projection_reads, "_registry_governed", lambda: False)
    monkeypatch.setattr(
        session_mcp, "project_agent_spec", lambda *_a: (project is not None, project)
    )
    monkeypatch.setattr(
        kas_env_launch, "crew_connection_slug", lambda *_a: "slack" if bridged else ""
    )
    rows: list[dict] = []

    def _capture(_state, _slot, role, content, cls="", *, meta=None, broadcast_user=False):
        rows.append({"role": role, "content": content, "meta": meta})
        return {}

    async def _nothing(*_a, **_kw):
        return None

    async def _no_hooks(*_a, **_kw):
        return [], False, None

    monkeypatch.setattr(chat_runner, "append_and_surface", _capture)
    # Only the sign-in row is under test; the other session-start notices read the
    # filesystem and are covered by their own tests.
    monkeypatch.setattr(chat_runner, "_notice_unread_steering", _nothing)
    monkeypatch.setattr(chat_runner, "_notice_spec_hooks_run_nowhere", _nothing)
    monkeypatch.setattr(chat_runner, "_notice_tools_settings_applied_nowhere", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_overlong_hook_commands", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_withheld_project_hooks", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_withheld_project_hook_files", _nothing)
    monkeypatch.setattr(chat_runner, "_surface_kas_project_hooks", _nothing)
    monkeypatch.setattr(chat_runner, "_prepare_observer_spec_hooks", _no_hooks)
    client = SimpleNamespace(
        capabilities=capabilities_for(backend),
        cwd="",
        client=SimpleNamespace(initialize_result={"agentCapabilities": {}}),
    )
    asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), slot or SimpleNamespace(), client, "kirocrew", is_new=True
        )
    )
    return [r for r in rows if _SIGN_IN in r["content"]]


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN - ACP_BACKENDS_REMOTE_MCP_OAUTH))
def test_a_harness_without_remote_mcp_oauth_names_the_server_once(monkeypatch, backend) -> None:
    (row,) = _surfaced(monkeypatch, backend)
    assert row["role"] == "notice"
    assert "mcpServers.slack.oauth" in row["content"]
    assert "plain" not in row["content"]
    assert row["meta"] == {"kind": MCP_SIGN_IN_UNSUPPORTED_NOTICE_KIND}


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_REMOTE_MCP_OAUTH))
def test_a_harness_that_honours_the_block_gets_no_notice(monkeypatch, backend) -> None:
    assert _surfaced(monkeypatch, backend) == []


def test_kiro_cli_is_the_harness_that_honours_the_block() -> None:
    """The parametrized sets above are not empty on either side."""
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_REMOTE_MCP_OAUTH
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_REMOTE_MCP_OAUTH


@pytest.mark.parametrize("backend", ["claude", "goose"])
def test_a_server_crew_signs_in_through_its_bridge_is_not_named(monkeypatch, backend) -> None:
    assert _surfaced(monkeypatch, backend, bridged=True) == []


def test_a_crewmate_worker_is_read_as_its_session_merged_spec(monkeypatch) -> None:
    """The slot's session key reaches the spec reader, as the KAS hooks read uses it."""
    reads: list = []
    slot = SimpleNamespace(_active_turn_session_key="mate-session-1")
    (_row,) = _surfaced(monkeypatch, "claude", slot=slot, reads=reads)
    assert reads == [("kirocrew", None, "mate-session-1")]


def test_a_reconnected_chat_does_not_repeat_the_notice(monkeypatch) -> None:
    """A second new session of the same chat shows no second row."""
    slot = SimpleNamespace(_session_notice_marks=[])
    assert len(_surfaced(monkeypatch, "claude", slot=slot)) == 1
    assert _surfaced(monkeypatch, "claude", slot=slot) == []


def test_a_server_the_checkout_mutes_is_not_named(monkeypatch) -> None:
    """The checkout's switch-off reaches the reader, as the session projection layers it."""
    project = {"mcpServers": {"slack": {"disabled": True, "url": "https://evil.example/mcp"}}}
    assert _surfaced(monkeypatch, "opencode", project=project) == []
    # A checkout key that mounts or grants something never widens what is read.
    assert len(_surfaced(monkeypatch, "opencode", project={"mcpServers": {"x": {"url": "u"}}})) == 1
