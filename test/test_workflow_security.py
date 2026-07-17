"""Tests for the always-deny-check in _resolve_permission and related security fixes.

Covers:
- _extract_tool_input_strings helper
- _resolve_permission deny-by-default for empty title
- _resolve_permission deny for sensitive paths (title and tool_input)
- _resolve_permission deny for sensitive bash commands (title and tool_input)
- _resolve_permission deny for BUILTIN_DENY_PATTERNS
- _resolve_permission passes through safe tools (no false positives)
- max_turns enforcement in stream_and_collect
- Output redaction in agent_exec.build_agent_fn
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.llm_helpers import (
    ToolApprovalPolicy,
    _extract_tool_input_strings,
    stream_and_collect,
)

# ── Fixtures ──


@dataclass
class FakeEvent:
    """Minimal LLMEvent stand-in for _resolve_permission tests."""

    kind: str = "permission_request"
    title: str = ""
    tool_kind: str = ""
    tool_input: str = ""
    request_id: str = "req-1"
    options: list = field(default_factory=list)
    text: str = ""


class FakeProvider:
    """Minimal LLMProvider that records approve/reject calls and yields events."""

    def __init__(self, events: list | None = None):
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self._events = events or []

    async def approve_tool(self, request_id: str, **kwargs) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str, **kwargs) -> None:
        self.rejected.append(request_id)

    async def stream(self, message: str) -> AsyncIterator:
        for e in self._events:
            yield e

    async def cancel(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


# ── _extract_tool_input_strings tests ──


class TestExtractToolInputStrings:
    def test_empty_string(self):
        assert _extract_tool_input_strings("") == []

    def test_none_coerced(self):
        # In practice tool_input is "" not None, but defend anyway
        assert _extract_tool_input_strings("") == []

    def test_plain_string_not_json(self):
        assert _extract_tool_input_strings("/home/user/.aws/credentials") == [
            "/home/user/.aws/credentials"
        ]

    def test_json_string_value(self):
        assert _extract_tool_input_strings('"~/.ssh/id_rsa"') == ["~/.ssh/id_rsa"]

    def test_json_dict_extracts_string_values(self):
        inp = json.dumps({"path": "/home/user/.aws/credentials", "encoding": "utf-8"})
        result = _extract_tool_input_strings(inp)
        assert "/home/user/.aws/credentials" in result
        assert "utf-8" in result

    def test_json_dict_skips_non_strings(self):
        inp = json.dumps({"path": "/tmp/safe.txt", "lines": 100, "flag": True})
        result = _extract_tool_input_strings(inp)
        assert result == ["/tmp/safe.txt"]

    def test_json_dict_empty_values_skipped(self):
        inp = json.dumps({"path": "", "command": "ls"})
        result = _extract_tool_input_strings(inp)
        assert result == ["ls"]

    def test_json_array_of_ints_returns_empty(self):
        # Arrays of non-strings return nothing
        assert _extract_tool_input_strings("[1, 2, 3]") == []

    def test_json_array_of_strings_extracts(self):
        inp = json.dumps(["/tmp/a.txt", "/tmp/b.txt"])
        result = _extract_tool_input_strings(inp)
        assert "/tmp/a.txt" in result
        assert "/tmp/b.txt" in result

    def test_invalid_json_returns_raw(self):
        raw = "cat ~/.aws/credentials && echo done"
        assert _extract_tool_input_strings(raw) == [raw]

    def test_nested_dict_extracts_deep_strings(self):
        inp = json.dumps({"args": {"path": "~/.aws/credentials", "mode": "r"}})
        result = _extract_tool_input_strings(inp)
        assert "~/.aws/credentials" in result
        assert "r" in result

    def test_nested_list_extracts_strings(self):
        inp = json.dumps({"files": ["/home/user/.ssh/id_rsa", "/tmp/safe.txt"]})
        result = _extract_tool_input_strings(inp)
        assert "/home/user/.ssh/id_rsa" in result
        assert "/tmp/safe.txt" in result

    def test_deeply_nested_structure(self):
        inp = json.dumps({"a": {"b": {"c": "~/.gnupg/secring.gpg"}}})
        result = _extract_tool_input_strings(inp)
        assert "~/.gnupg/secring.gpg" in result

    def test_mixed_nested_types(self):
        inp = json.dumps({"cmd": "ls", "opts": ["-la", {"dir": "/tmp"}], "n": 5})
        result = _extract_tool_input_strings(inp)
        assert "ls" in result
        assert "-la" in result
        assert "/tmp" in result


# ── _resolve_permission tests ──


@pytest.fixture
def mock_sel():
    """Patch SEL to avoid real log writes.

    _resolve_permission does a lazy import: ``from kiro_crew.sel import sel``
    then calls ``sel().log_tool_invocation(...)``. The top-level import in
    llm_helpers is ``_sel``, but the lazy import inside the function binds
    a fresh ``sel`` name each call. We patch the source module so both paths
    get the mock.
    """
    sel_instance = MagicMock()
    with patch("kiro_crew.sel.sel", return_value=sel_instance):
        # Also patch the module-level _sel used by stream_and_collect's
        # EVENT_TOOL_CALL branch.
        with patch("kiro_crew.llm_helpers._sel", return_value=sel_instance):
            yield sel_instance


class TestResolveDenyByDefault:
    """Empty/missing title must be denied."""

    @pytest.mark.asyncio
    async def test_empty_title_rejected(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected

    @pytest.mark.asyncio
    async def test_none_title_rejected(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title=None)  # type: ignore[arg-type]
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected


class TestResolveSensitivePath:
    """Sensitive paths blocked via title and tool_input."""

    @pytest.mark.asyncio
    async def test_sensitive_path_in_title(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="~/.aws/credentials")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected

    @pytest.mark.asyncio
    async def test_sensitive_path_in_tool_input(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(
            title="Read",  # Generic title
            tool_input=json.dumps({"path": "~/.aws/credentials"}),
        )
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected

    @pytest.mark.asyncio
    async def test_safe_path_passes(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="Reading /tmp/output.txt")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is True
        assert "req-1" in provider.approved


class TestResolveSensitiveBashCommand:
    """Sensitive bash commands blocked via title and tool_input."""

    @pytest.mark.asyncio
    async def test_sensitive_bash_in_title(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="Running: cat ~/.aws/credentials")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected

    @pytest.mark.asyncio
    async def test_sensitive_bash_in_tool_input(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(
            title="Bash",
            tool_input=json.dumps({"command": "cat ~/.ssh/id_rsa"}),
        )
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected

    @pytest.mark.asyncio
    async def test_safe_bash_passes(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="Running: ls /tmp")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is True
        assert "req-1" in provider.approved


class TestResolveDenyPatternsToolInput:
    """BUILTIN_DENY_PATTERNS enforcement via tool_input (post #9 fix)."""

    @pytest.mark.asyncio
    async def test_deny_pattern_in_tool_input(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(
            title="Bash",
            tool_input=json.dumps({"command": "get_secret_value --secret-id prod-db"}),
        )
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected

    @pytest.mark.asyncio
    async def test_deny_pattern_in_nested_tool_input(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(
            title="Execute",
            tool_input=json.dumps({"args": {"cmd": "get_secret_value my-secret"}}),
        )
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected


class TestResolveDenyPatterns:
    """BUILTIN_DENY_PATTERNS enforcement."""

    @pytest.mark.asyncio
    async def test_get_secret_denied(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="get_secret_value")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected

    @pytest.mark.asyncio
    async def test_delete_stack_denied(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="aws cloudformation delete_stack --stack-name prod")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected

    @pytest.mark.asyncio
    async def test_safe_tool_passes(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="InternalCodeSearch")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.AUTO_APPROVE, hooks=None
        )
        assert result is True
        assert "req-1" in provider.approved


class TestResolveRejectAll:
    """REJECT_ALL policy still works."""

    @pytest.mark.asyncio
    async def test_reject_all_rejects_everything(self, mock_sel):
        from kiro_crew.llm_helpers import _resolve_permission

        provider = FakeProvider()
        event = FakeEvent(title="safe tool")
        result = await _resolve_permission(
            provider, event, ToolApprovalPolicy.REJECT_ALL, hooks=None
        )
        assert result is False
        assert "req-1" in provider.rejected


# ── max_turns tests ──


class TestMaxTurns:
    """stream_and_collect breaks when max_turns exceeded."""

    @pytest.mark.asyncio
    async def test_max_turns_breaks_loop(self, mock_sel):
        """When tool_call count exceeds max_turns, streaming stops."""
        events = [
            FakeEvent(kind="tool_call", title="safe_tool", tool_input=""),
            FakeEvent(kind="tool_call", title="safe_tool", tool_input=""),
            FakeEvent(kind="tool_call", title="safe_tool", tool_input=""),
            FakeEvent(kind="text_chunk", title="", text="hello"),
            FakeEvent(kind="complete", title=""),
        ]
        provider = FakeProvider(events)
        # max_turns=2: should break after the 3rd tool_call
        result = await stream_and_collect(provider, "test", max_turns=2)
        # The text_chunk after the break should not be collected
        assert result == ""

    @pytest.mark.asyncio
    async def test_no_max_turns_unlimited(self, mock_sel):
        """Without max_turns, all tool calls proceed."""
        events = [
            FakeEvent(kind="tool_call", title="safe_tool", tool_input=""),
            FakeEvent(kind="tool_call", title="safe_tool", tool_input=""),
            FakeEvent(kind="tool_call", title="safe_tool", tool_input=""),
            FakeEvent(kind="text_chunk", title="", text="result"),
            FakeEvent(kind="complete", title=""),
        ]
        provider = FakeProvider(events)
        result = await stream_and_collect(provider, "test", max_turns=None)
        assert result == "result"

    @pytest.mark.asyncio
    async def test_max_turns_exact_boundary(self, mock_sel):
        """Exactly max_turns tool calls is allowed; max_turns+1 breaks."""
        events = [
            FakeEvent(kind="tool_call", title="t1", tool_input=""),
            FakeEvent(kind="tool_call", title="t2", tool_input=""),
            FakeEvent(kind="text_chunk", title="", text="done"),
            FakeEvent(kind="complete", title=""),
        ]
        provider = FakeProvider(events)
        # max_turns=2 allows exactly 2 tool calls
        result = await stream_and_collect(provider, "test", max_turns=2)
        assert result == "done"


# ── agent_exec redaction tests ──


class TestAgentExecRedaction:
    """Output redaction in agent_exec.build_agent_fn."""

    @pytest.mark.asyncio
    async def test_credentials_redacted_from_output(self):
        """Credential-like patterns in agent output are redacted."""
        from kiro_crew.workflows.agent_exec import build_agent_fn

        fake_sessions = MagicMock()
        fake_provider = FakeProvider(
            [
                FakeEvent(kind="text_chunk", title="", text="key=AKIAIOSFODNN7EXAMPLE"),
                FakeEvent(kind="complete", title=""),
            ]
        )
        fake_sessions.get_or_create = AsyncMock(return_value=(fake_provider,))
        fake_sessions.release = MagicMock()

        agent_fn = build_agent_fn(fake_sessions, run_id="test-run")
        result = await agent_fn("show me creds", {})
        # The AWS key should be redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    @pytest.mark.asyncio
    async def test_safe_output_unchanged(self):
        """Normal output without credentials passes through."""
        from kiro_crew.workflows.agent_exec import build_agent_fn

        fake_sessions = MagicMock()
        fake_provider = FakeProvider(
            [
                FakeEvent(kind="text_chunk", title="", text="Hello, world!"),
                FakeEvent(kind="complete", title=""),
            ]
        )
        fake_sessions.get_or_create = AsyncMock(return_value=(fake_provider,))
        fake_sessions.release = MagicMock()

        agent_fn = build_agent_fn(fake_sessions, run_id="test-run")
        result = await agent_fn("say hello", {})
        assert result == "Hello, world!"
