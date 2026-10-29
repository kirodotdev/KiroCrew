"""Tests for chat.py hook integration (validation, fail-closed, audit events)."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _validate_tool_name
from kiro_crew.validation import MAX_TOOL_NAME_LEN


class TestToolNameValidation:
    """Test _validate_tool_name function for security controls."""

    def test_valid_tool_names(self):
        """Valid tool names pass validation."""
        assert _validate_tool_name("ReadFile") == "ReadFile"
        assert _validate_tool_name("builder-mcp--Search") == "builder-mcp--Search"
        assert _validate_tool_name("fs_write") == "fs_write"
        assert _validate_tool_name("git/commit") == "git/commit"

    def test_empty_tool_name(self):
        """Empty tool names are rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_tool_name("")

    def test_max_length_exceeded(self):
        """Tool names exceeding max length are rejected (non-execute)."""
        long_name = "a" * (MAX_TOOL_NAME_LEN + 1)
        with pytest.raises(ValueError, match="exceeds max length"):
            _validate_tool_name(long_name)

    def test_shell_skips_length_check(self):
        """Shell tool titles can be arbitrarily long (bash command lines)."""
        long_cmd = "Running: " + "x" * 1000
        assert _validate_tool_name(long_cmd, is_shell=True) == long_cmd

    def test_long_non_shell_name_still_rejected(self):
        """A long title that is NOT a shell command is still capped."""
        long_name = "Reading " + "a" * (MAX_TOOL_NAME_LEN + 1)
        with pytest.raises(ValueError, match="exceeds max length"):
            _validate_tool_name(long_name, is_shell=False)

    def test_display_titles_accepted(self):
        """Display titles with shell-like characters are accepted (used for hook matching only)."""
        assert _validate_tool_name("Running: echo hello") == "Running: echo hello"
        assert (
            _validate_tool_name("Creating random_2026-03-22.txt")
            == "Creating random_2026-03-22.txt"
        )
        assert _validate_tool_name("Reading /tmp/file.json") == "Reading /tmp/file.json"

    def test_unicode_normalization(self):
        """Hidden Unicode characters are stripped."""
        # Zero-width space
        assert _validate_tool_name("Tool\u200bName") == "ToolName"
        # Direction override
        assert _validate_tool_name("Tool\u202eName") == "ToolName"

    def test_whitespace_trimmed(self):
        """Leading/trailing whitespace is trimmed."""
        assert _validate_tool_name("  ToolName  ") == "ToolName"
        assert _validate_tool_name("\nToolName\n") == "ToolName"

    def test_valid_namespace_separator(self):
        """Forward slash for namespaces is allowed."""
        assert _validate_tool_name("mcp/core/read") == "mcp/core/read"

    def test_valid_underscore_hyphen(self):
        """Underscores and hyphens are allowed."""
        assert _validate_tool_name("my_tool-name") == "my_tool-name"


# A ``read`` title the way kiro-cli builds it for ``image_paths``: the operation
# content (long filenames with spaces) is the title, so its length tracks the
# user's filesystem, not any tool name. Well over MAX_TOOL_NAME_LEN.
_LONG_READ_TITLE = "View image " + " ".join(
    f"/mnt/Sign in with Apple - screenshot {i:02d} of the consent sheet.png" for i in range(6)
)


class TestCanonicalNameExemption:
    """A title travelling with a trusted canonical identity is content, not a name.

    The cap exists for the case where the title is the ONLY identity a hook can
    match on. ``AcpEvent.tool_name`` (the harness's ``_meta`` identity) is that
    identity when present, so the length predicate -- and only that predicate --
    is skipped for it, exactly as it is for ``is_shell``.
    """

    def test_long_title_with_canonical_name_passes(self):
        assert len(_LONG_READ_TITLE) > MAX_TOOL_NAME_LEN
        assert _validate_tool_name(_LONG_READ_TITLE, canonical_name="fs_read") == _LONG_READ_TITLE

    def test_long_title_without_canonical_name_still_rejected(self):
        """A backend that publishes no identity keeps today's loud refusal."""
        with pytest.raises(ValueError, match="exceeds max length"):
            _validate_tool_name(_LONG_READ_TITLE, canonical_name="")

    def test_canonical_name_does_not_skip_sanitisation(self):
        """Only the length predicate is relaxed: hidden characters are still stripped."""
        title = "View image\u200b " + "a" * (MAX_TOOL_NAME_LEN + 1) + "\u202e"
        result = _validate_tool_name(title, canonical_name="fs_read")
        assert "\u200b" not in result and "\u202e" not in result
        assert result == "View image " + "a" * (MAX_TOOL_NAME_LEN + 1)

    def test_canonical_name_does_not_skip_empty_check(self):
        """An empty (or all-hidden) title is refused even with a canonical identity."""
        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_tool_name("", canonical_name="fs_read")
        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_tool_name("\u200b \n", canonical_name="fs_read")

    def test_shell_exemption_unchanged(self):
        """The shell branch neither needs nor is affected by the canonical name."""
        long_cmd = "Running: " + "x" * 1000
        assert _validate_tool_name(long_cmd, is_shell=True, canonical_name="") == long_cmd
        assert _validate_tool_name(long_cmd, is_shell=True, canonical_name="shell") == long_cmd

    def test_every_chat_runner_call_site_passes_the_canonical_name(self):
        """Every permission path validates the title WITH the ``_meta`` identity.

        A path that still validates ``event.title`` alone re-breaks long ``read``
        titles on that path only, so the call sites are enumerated structurally
        rather than one path at a time.
        """
        from pathlib import Path

        source = "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [
                *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
                Path(chat_runner.__file__),
            ]
            if turn_file.name != "__init__.py"
        )
        calls = re.findall(r"_validate_tool_name\([^)]*\)", source)
        assert len(calls) >= 6, f"expected the known permission paths, found {len(calls)}"
        missing = [c for c in calls if "canonical_name=event.tool_name" not in c]
        assert not missing, missing


@pytest.mark.asyncio
async def test_main_chat_tool_hook_includes_native_tool_and_own_session_ids(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state
    from dashboard_chat_helpers import _provider_mock

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHookStore
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TOOL_CALL, LLMEvent

    fixed_turn_id = "fedcba9876543210fedcba9876543210"
    monkeypatch.setattr(
        chat_runner,
        "uuid",
        SimpleNamespace(uuid4=lambda: UUID(hex=fixed_turn_id)),
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state._hook_store = ScriptHookStore(tmp_path / "hooks")
    state._hook_store.create(
        {
            "name": "identity-hook",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "command": "echo test",
        }
    )
    client = _provider_mock()
    client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    client.cwd = ""
    client.settings_withheld_cause = MagicMock(return_value=None)

    async def stream(_message):
        yield LLMEvent(
            kind=EVENT_TOOL_CALL,
            title="ReadFile",
            tool_kind="read",
            tool_input='{"path":"README.md"}',
            tool_call_id="native-tool-call-7",
        )
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = stream
    client.stream_command = stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.get_pid = MagicMock(return_value=None)
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    slot = state.get_or_create_slot("s1")

    hook_result = SimpleNamespace(
        hook_name="identity-hook",
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
    )
    with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = hook_result
        await _run_chat(state, slot, "read the README")

    assert mock_run.await_count == 1
    hook_event = mock_run.await_args.args[2]
    assert hook_event["tool_call_id"] == "native-tool-call-7"
    assert hook_event["session_key"] == "dashboard:s1"
    assert hook_event["turn_id"] == fixed_turn_id


@pytest.mark.asyncio
async def test_dashboard_chat_prompt_and_hook_share_acp_turn_identity(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state
    from dashboard_chat_helpers import _provider_mock

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.types import METHOD_PROMPT, JsonRpcMessage
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.hooks import (
        HOOK_EVENT_USER_PROMPT_SUBMIT,
        ScriptHookResult,
        ScriptHookStore,
    )
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    class DashboardAcpProvider:
        def __init__(self, delegate, metadata_provider):
            self._delegate = delegate
            self._metadata_provider = metadata_provider

        def stream_with_metadata(self, message, *, telemetry_context, allow_image=True):
            return self._metadata_provider.stream_with_metadata(
                message,
                telemetry_context=telemetry_context,
                allow_image=allow_image,
            )

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    fixed_turn_id = "fedcba9876543210fedcba9876543210"
    monkeypatch.setattr(
        chat_runner,
        "uuid",
        SimpleNamespace(uuid4=lambda: UUID(hex=fixed_turn_id)),
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state._hook_store = ScriptHookStore(tmp_path / "hooks")
    state._hook_store.create(
        {"name": "prompt-identity", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "command": "echo test"}
    )
    client = _provider_mock()
    client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    client.cwd = ""
    client.settings_withheld_cause = MagicMock(return_value=None)
    acp_client = AcpClient(acp_backend=ACP_BACKEND_KIRO)
    acp_client._session_id = "acp-session-7"
    acp_client.ensure_ready = AsyncMock()
    acp_client.confirm_member_projection = AsyncMock()
    requests = []

    async def send_request(method, params, **kwargs):
        requests.append((method, params))
        return 1

    async def read_message(timeout=20.0):
        return JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

    acp_client._send_request = send_request
    acp_client._read_message = read_message
    with patch("kiro_crew.providers.acp.AcpClient", return_value=acp_client):
        acp_provider = AcpProvider(acp_backend=ACP_BACKEND_KIRO)
    client = DashboardAcpProvider(client, acp_provider)

    async def generic_stream(_message):
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = generic_stream
    client.stream_command = generic_stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.get_pid = MagicMock(return_value=None)
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    slot = state.get_or_create_slot("s1")

    def successful_result(hook, *args, **kwargs):
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            exit_code=0,
        )

    with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = successful_result
        await _run_chat(state, slot, "send a normal chat prompt")

    prompt_requests = [(method, params) for method, params in requests if method == METHOD_PROMPT]
    assert len(prompt_requests) == 1
    _method, prompt_params = prompt_requests[0]
    telemetry_context = prompt_params["_meta"]["kirocrew"]
    assert telemetry_context == {
        "turn_id": fixed_turn_id,
        "session_key": "dashboard:s1",
    }
    prompt_hook_event = mock_run.await_args_list[0].args[2]
    assert prompt_hook_event["turn_id"] == telemetry_context["turn_id"]
    assert prompt_hook_event["session_key"] == telemetry_context["session_key"]


@pytest.mark.asyncio
async def test_dashboard_chat_keeps_generic_provider_stream_contract(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state
    from dashboard_chat_helpers import _provider_mock

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    class GenericProvider:
        def __init__(self, delegate):
            self._delegate = delegate

        def __getattr__(self, name):
            if name == "stream_with_metadata":
                raise AttributeError(name)
            return getattr(self._delegate, name)

    monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    delegate = _provider_mock()
    client = GenericProvider(delegate)
    client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    client.cwd = ""
    client.settings_withheld_cause = MagicMock(return_value=None)
    streamed_messages = []

    async def stream(message):
        streamed_messages.append(message)
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = stream
    client.stream_command = stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.get_pid = MagicMock(return_value=None)
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    slot = state.get_or_create_slot("s1")

    await _run_chat(state, slot, "generic provider prompt")

    assert len(streamed_messages) == 1
    assert "generic provider prompt" in streamed_messages[0]


@pytest.mark.asyncio
async def test_spec_lifecycle_hooks_share_chat_turn_id(tmp_path):
    from kiro_crew.dashboard.chat_turn.turn_spec_hooks import _fire
    from kiro_crew.hooks import (
        HOOK_EVENT_STOP,
        HOOK_EVENT_USER_PROMPT_SUBMIT,
        ScriptHookResult,
        ScriptHookStore,
    )

    turn_id = "0123456789abcdef0123456789abcdef"
    hook_store = ScriptHookStore(tmp_path)
    hook_store.create(
        {"name": "prompt-hook", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "command": "echo test"}
    )
    hook_store.create({"name": "stop-hook", "event": HOOK_EVENT_STOP, "command": "echo test"})
    state = SimpleNamespace(_hook_store=hook_store)
    slot = SimpleNamespace(key="s1")
    turn = SimpleNamespace(
        session_key="dashboard:s1",
        slot=slot,
        state=state,
        _spec_hooks_unreadable=False,
        _spec_hooks=[],
        _spec_hooks_cwd=None,
        _recovery_turn_id=turn_id,
    )

    def successful_result(hook, *args, **kwargs):
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            exit_code=0,
        )

    with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = successful_result
        await _fire(turn, HOOK_EVENT_USER_PROMPT_SUBMIT, "hello")
        await _fire(turn, HOOK_EVENT_STOP, "goodbye")

    prompt_event = mock_run.await_args_list[0].args[2]
    stop_event = mock_run.await_args_list[1].args[2]
    assert prompt_event["turn_id"] == turn_id
    assert stop_event["turn_id"] == turn_id
    assert prompt_event["session_key"] == "dashboard:s1"
    assert stop_event["session_key"] == "dashboard:s1"
    assert "tool_call_id" not in prompt_event
    assert "tool_call_id" not in stop_event
