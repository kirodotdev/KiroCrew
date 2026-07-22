"""Tests for heartbeat tool allowlist + HEARTBEAT_KEEP gateway injection.

Covers:
- ``_is_heartbeat_safe_tool`` allowlist + verb-check fallback
- ``HEARTBEAT_SAFE_TOOLS`` membership for the canonical safe tools
- ``_HEARTBEAT_KEEP_INJECTION`` is the literal prefix expected by the agent
- ``GatewayOrchestrator._heartbeat_approval`` approves safe tools and rejects
  unsafe ones with a SEL audit event
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.types import AcpEvent
from kiro_crew.slack.gateway import (
    _HEARTBEAT_KEEP_INJECTION,
    HEARTBEAT_SAFE_TOOLS,
    GatewayOrchestrator,
    _is_heartbeat_safe_tool,
)


def _make_event(title: str, *, tool_kind: str = "mcp", request_id: str = "req-1") -> AcpEvent:
    """Build a minimal AcpEvent for approval-callback tests."""
    return AcpEvent(
        kind="permission_request",
        title=title,
        tool_kind=tool_kind,
        request_id=request_id,
    )


# ── Allowlist membership ──


class TestHeartbeatSafeTools:
    def test_canonical_read_tools_present(self) -> None:
        """Spot-check tools every heartbeat task should reasonably need."""
        for name in [
            "Read", "Grep", "Glob",
            "WorkspaceSearch",
            "learn_list",
            "cron_list",
            "spawn_list",
            "spawn_status",
            "artifact_list",
            "artifact_get",
            "artifact_versions",
            "local_knowledge_search",
        ]:
            assert name in HEARTBEAT_SAFE_TOOLS, f"{name} missing from allowlist"

    def test_write_tools_excluded(self) -> None:
        """Write/mutating tools must NOT be in the allowlist."""
        for name in [
            "send_message",
            "file_send",
            "cron_add",
            "cron_remove",
            "Edit",
            "Write",
            "TaskeiCreateTask",
            "TaskeiUpdateTask",
            "TicketingWriteActions",
            "CodeReviewWriteActions",
            "learn_add",
            "learn_remove",
        ]:
            assert name not in HEARTBEAT_SAFE_TOOLS, f"{name} should NOT be in allowlist"


# ── _is_heartbeat_safe_tool ──


class TestIsHeartbeatSafeTool:
    def test_allowlist_match(self) -> None:
        assert _is_heartbeat_safe_tool("WorkspaceSearch")

    def test_unknown_read_verb_rejected_strict_allowlist(self) -> None:
        """Strict allowlist: unknown tools (even with read-shaped names)
        must NOT auto-approve. Per security-controls deny-by-default and
        threat model — a verb-based fallback could be widened by injected
        names like ``get_all_credentials`` from polled external content."""
        # Read-shaped but not in HEARTBEAT_SAFE_TOOLS
        assert not _is_heartbeat_safe_tool("get_pipeline_status")
        assert not _is_heartbeat_safe_tool("list_artifacts_v2")
        # Adversarial names that would have passed the old verb fallback
        assert not _is_heartbeat_safe_tool("get_all_credentials")
        assert not _is_heartbeat_safe_tool("list_env_secrets")
        assert not _is_heartbeat_safe_tool("read_secret_from_vault")

    def test_unknown_write_verb_rejected(self) -> None:
        assert not _is_heartbeat_safe_tool("send_message")
        assert not _is_heartbeat_safe_tool("create_ticket")
        assert not _is_heartbeat_safe_tool("delete_artifact")

    def test_empty_input(self) -> None:
        assert not _is_heartbeat_safe_tool("")
        assert not _is_heartbeat_safe_tool("   ")

    def test_whitespace_stripped(self) -> None:
        assert _is_heartbeat_safe_tool("  WorkspaceSearch  ")

    def test_mcp_server_prefix_stripped(self) -> None:
        """kiro-cli ACP sends tool names as ``mcp__<server>__<tool>``.
        The prefix must be stripped before matching the allowlist."""
        assert _is_heartbeat_safe_tool("mcp__kirocrew-core__learn_list")
        assert _is_heartbeat_safe_tool("mcp__kirocrew-core__spawn_list")
        assert _is_heartbeat_safe_tool("mcp__kirocrew-core__local_knowledge_search")
        assert _is_heartbeat_safe_tool("mcp__kirocrew-cron__cron_list")

    def test_mcp_prefix_write_tool_still_rejected(self) -> None:
        """MCP prefix stripping must not widen the allowlist — write tools
        with a server prefix must still be rejected."""
        assert not _is_heartbeat_safe_tool("mcp__kirocrew-core__send_message")
        assert not _is_heartbeat_safe_tool("mcp__builder-mcp__CodeReviewWriteActions")
        assert not _is_heartbeat_safe_tool("mcp__kirocrew-core__cron_add")

    def test_running_prefix_with_at_server_slash_tool(self) -> None:
        """Runtime titles arrive as ``Running: @server/Tool`` — the status
        prefix and @server/ must be stripped to reach the bare tool name."""
        assert _is_heartbeat_safe_tool("Running: @kirocrew-core/learn_list")
        assert _is_heartbeat_safe_tool("Running: @kirocrew-core/spawn_list")
        assert _is_heartbeat_safe_tool("Running: @kirocrew-cron/cron_list")

    def test_at_server_slash_tool_without_running_prefix(self) -> None:
        """@server/Tool without a status prefix must also normalize."""
        assert _is_heartbeat_safe_tool("@kirocrew-core/artifact_list")
        assert _is_heartbeat_safe_tool("@kirocrew-core/local_knowledge_search")

    def test_running_prefix_bare_tool_name(self) -> None:
        """Running: <bare tool> (no server prefix) must also match."""
        assert _is_heartbeat_safe_tool("Running: WorkspaceSearch")
        assert _is_heartbeat_safe_tool("Running: Read")
        assert _is_heartbeat_safe_tool("Running: Grep")

    def test_running_prefix_write_tool_still_rejected(self) -> None:
        """Normalization must not widen the allowlist — write tools with
        the runtime title format must still be rejected."""
        assert not _is_heartbeat_safe_tool("Running: @kirocrew-core/send_message")
        assert not _is_heartbeat_safe_tool("Running: @builder-mcp/ToolReactivationTool")
        assert not _is_heartbeat_safe_tool("Running: @builder-mcp/CodeReviewWriteActions")

    def test_at_server_slash_write_tool_rejected(self) -> None:
        """@server/WriteTool (no Running prefix) must still be rejected."""
        assert not _is_heartbeat_safe_tool("@builder-mcp/get_all_credentials")
        assert not _is_heartbeat_safe_tool("@kirocrew-core/send_message")
        assert not _is_heartbeat_safe_tool("@kirocrew-core/cron_add")


class TestEditionHeartbeatAllowlist:
    """The CPP ``SlackEnterpriseGate.heartbeat_safe_tools()`` seam.

    A companion may ADD tool names; the public Default is empty (byte-identical).
    A qualified ``@server/Tool`` entry must match ONLY that server's tool, not a
    same-bare-name tool from a different (or compromised) server.
    """

    def _install_gate_extra(self, monkeypatch, extra):
        """Install a platform context whose slack_gate returns *extra*."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context
        from kiro_crew.platform.context import set_context

        base = build_default_context(KiroCrewConfig())

        class _Gate:
            def validate_enterprise(self, *a, **k):
                return True

            def check_message_origin(self, *a, **k):
                return True

            def heartbeat_safe_tools(self):
                return frozenset(extra)

        import dataclasses

        ctx = dataclasses.replace(base, slack_gate=_Gate())
        set_context(ctx)
        monkeypatch.setattr(
            "kiro_crew.platform.bootstrap._BOOTED", True, raising=False
        )

    @pytest.fixture(autouse=True)
    def _reset_ctx(self):
        from kiro_crew.platform.context import set_context

        yield
        set_context(None)

    def test_default_edition_set_is_empty(self, monkeypatch) -> None:
        """No companion → the allowlist is byte-identical to the core set."""
        self._install_gate_extra(monkeypatch, [])
        assert not _is_heartbeat_safe_tool("@builder-mcp/ReadInternalWebsites")

    def test_qualified_entry_matches_only_its_server(self, monkeypatch) -> None:
        """A pinned ``@server/Tool`` entry auto-approves that exact identity…"""
        self._install_gate_extra(monkeypatch, ["@builder-mcp/ReadInternalWebsites"])
        assert _is_heartbeat_safe_tool("@builder-mcp/ReadInternalWebsites")
        assert _is_heartbeat_safe_tool("Running: @builder-mcp/ReadInternalWebsites")
        assert _is_heartbeat_safe_tool("mcp__builder-mcp__ReadInternalWebsites")

    def test_qualified_entry_rejects_same_bare_name_other_server(self, monkeypatch) -> None:
        """…but a DIFFERENT server exposing the same bare tool name is denied.

        This is the collision guard: pinning ``@builder-mcp/ReadInternalWebsites``
        must NOT auto-approve ``@evil-mcp/ReadInternalWebsites``.
        """
        self._install_gate_extra(monkeypatch, ["@builder-mcp/ReadInternalWebsites"])
        assert not _is_heartbeat_safe_tool("@evil-mcp/ReadInternalWebsites")
        assert not _is_heartbeat_safe_tool("Running: @evil-mcp/ReadInternalWebsites")
        assert not _is_heartbeat_safe_tool("mcp__evil-mcp__ReadInternalWebsites")
        # And the bare name alone is not auto-approved when only a qualified
        # entry was allowlisted.
        assert not _is_heartbeat_safe_tool("ReadInternalWebsites")

    def test_bare_edition_entry_never_matches(self, monkeypatch) -> None:
        """A BARE edition entry must NOT auto-approve anything (deny-by-default).

        Entries MUST be server-qualified; a bare name would carry the collision
        risk (a different server's same-named destructive tool auto-approved), so
        the gate ignores it entirely — neither a qualified nor a bare title match.
        """
        self._install_gate_extra(monkeypatch, ["ReadInternalWebsites"])
        assert not _is_heartbeat_safe_tool("@builder-mcp/ReadInternalWebsites")
        assert not _is_heartbeat_safe_tool("ReadInternalWebsites")
        assert not _is_heartbeat_safe_tool("Running: @builder-mcp/ReadInternalWebsites")

    def test_unqualified_title_never_matches_edition_set(self, monkeypatch) -> None:
        """A bare tool-call title (no resolvable server) never matches the edition
        set even when a qualified entry is allowlisted."""
        self._install_gate_extra(monkeypatch, ["@builder-mcp/ReadInternalWebsites"])
        assert not _is_heartbeat_safe_tool("ReadInternalWebsites")
        assert not _is_heartbeat_safe_tool("Running: ReadInternalWebsites")


# ── HEARTBEAT_KEEP injection text ──


class TestKeepInjection:
    def test_injection_mentions_heartbeat_keep(self) -> None:
        assert "HEARTBEAT_KEEP" in _HEARTBEAT_KEEP_INJECTION

    def test_injection_ends_with_blank_line(self) -> None:
        """Trailing blank line separates injection from agent task text."""
        assert _HEARTBEAT_KEEP_INJECTION.endswith("\n\n")

    def test_injection_prepends_cleanly(self) -> None:
        """Concatenating injection + task must yield a parseable two-block string."""
        task = "Check CR-12345 for new comments"
        injected = _HEARTBEAT_KEEP_INJECTION + task
        assert injected.startswith("[HEARTBEAT TASK")
        assert task in injected
        # Agent-visible task body is the suffix after the blank-line separator
        assert injected.split("\n\n", 1)[1] == task


# ── _heartbeat_approval callback ──


@pytest.fixture()
def orchestrator():
    """Bare GatewayOrchestrator instance (bypasses __init__)."""
    return GatewayOrchestrator.__new__(GatewayOrchestrator)


class TestHeartbeatApproval:
    @pytest.mark.asyncio
    async def test_approves_allowlisted_tool(self, orchestrator, monkeypatch) -> None:
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event("WorkspaceSearch")
        assert await orchestrator._heartbeat_approval(event) is True
        # Approvals must also emit a SEL audit event (security-controls
        # guideline: every permission decision is audited).
        sel_mock.log_tool_invocation.assert_called_once()
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "auto_approved"
        assert kwargs["source"] == "heartbeat"
        # Audit must record which agent was making the call so operators can
        # filter heartbeat decisions distinctly from other unattended sessions.
        assert kwargs["agent"] == "kirocrew-heartbeat"
        assert kwargs["tool_name"] == "WorkspaceSearch"
        assert kwargs["metadata"]["reason"] == "in_heartbeat_safe_tools"
        # The approve-path audit MUST be a fail-closed (synchronous, raising)
        # SEL write — otherwise the async queue swallows a write failure and the
        # deny-by-default branch below becomes unreachable (pentest gap).
        assert kwargs["critical"] is True

    @pytest.mark.asyncio
    async def test_rejects_unknown_read_shaped_tool(self, orchestrator, monkeypatch) -> None:
        """Strict allowlist: unknown tool with a read-shaped name still rejects."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event("get_session_status", request_id="req-unknown")
        assert await orchestrator._heartbeat_approval(event) is False
        sel_mock.log_tool_invocation.assert_called_once()
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["metadata"]["reason"] == "not_in_heartbeat_safe_tools"

    @pytest.mark.asyncio
    async def test_rejects_write_tool(self, orchestrator, monkeypatch) -> None:
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event("send_message", request_id="req-write")
        assert await orchestrator._heartbeat_approval(event) is False
        # SEL must record the deny so operators can audit blocked calls
        sel_mock.log_tool_invocation.assert_called_once()
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "heartbeat"
        assert kwargs["agent"] == "kirocrew-heartbeat"
        assert kwargs["tool_name"] == "send_message"
        assert kwargs["request_id"] == "req-write"
        assert kwargs["metadata"]["reason"] == "not_in_heartbeat_safe_tools"

    @pytest.mark.asyncio
    async def test_rejects_empty_title(self, orchestrator, monkeypatch) -> None:
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event("")
        assert await orchestrator._heartbeat_approval(event) is False

    @pytest.mark.asyncio
    async def test_dashboard_log_warning_redacts_llm_title(
        self, orchestrator, monkeypatch, caplog
    ) -> None:
        """LLM-originated tool titles MUST be redacted before reaching any
        external surface, including the ``logger.warning`` on the deny path
        — KiroCrew logs surface in the dashboard. Per security-controls
        guideline: never trust LLM output. (review-bot finding on rev 6.)
        """
        import logging
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        # Title carries an AWS access key (a polled CR comment or ticket
        # body could mention one, and the tool-name path is LLM-controlled).
        bad_title = "tool_with_secret AKIAIOSFODNN7EXAMPLE inside"
        event = _make_event(bad_title)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
            await orchestrator._heartbeat_approval(event)
        # The raw AWS key body must NOT appear in any log record produced
        # by this callback — redact_credentials should have replaced it.
        for record in caplog.records:
            assert "AKIAIOSFODNN7EXAMPLE" not in record.getMessage(), (
                f"Unredacted credential leaked into log: {record.getMessage()}"
            )

    @pytest.mark.asyncio
    async def test_sel_failure_deny_path_still_denies(self, orchestrator, monkeypatch) -> None:
        """SEL outage must not crash the deny path — tool is rejected anyway.

        Deny-path safety property: the tool is NOT approved, so absence of
        the audit log doesn't allow an unaudited tool run. SEL failure is
        tolerated.
        """
        sel_mock = MagicMock()
        sel_mock.log_tool_invocation.side_effect = RuntimeError("sel down")
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        deny_event = _make_event("send_message")
        assert await orchestrator._heartbeat_approval(deny_event) is False

    @pytest.mark.asyncio
    async def test_sel_failure_approve_path_fails_closed(self, orchestrator, monkeypatch) -> None:
        """SEL outage on the approve path MUST deny the tool (deny-by-default).

        Approve-path invariant: every tool that runs has a corresponding
        SEL audit record. If SEL is down, we cannot satisfy the invariant
        and must therefore deny rather than allow an unaudited tool to run
        in an unattended heartbeat session. (review-bot finding on rev 6.)
        """
        sel_mock = MagicMock()
        sel_mock.log_tool_invocation.side_effect = RuntimeError("sel down")
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        approve_event = _make_event("WorkspaceSearch")
        assert await orchestrator._heartbeat_approval(approve_event) is False

    @pytest.mark.asyncio
    async def test_approve_fails_closed_with_real_async_sel_unwritable(
        self, orchestrator, monkeypatch, tmp_path
    ) -> None:
        """End-to-end regression for the pentest gap: with the REAL async SEL
        (not a mock), an unwritable log file must make the approve path deny.

        Before the fix, ``log_tool_invocation`` enqueued to the async writer
        and returned without touching the filesystem, so the ``except`` on the
        approve path was unreachable and the tool auto-approved unaudited. With
        ``critical=True`` the write is synchronous and raises, so we deny.
        """
        from kiro_crew.sel import SecurityEventLog

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        real_sel = SecurityEventLog(base_dir=tmp_path)
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: real_sel)

        real_os_open = os.open

        def _boom(path, *a, **k):
            if str(path).endswith("security_events.jsonl"):
                raise PermissionError("SEL file unwritable (chmod 000)")
            return real_os_open(path, *a, **k)

        monkeypatch.setattr(os, "open", _boom)
        try:
            event = _make_event("WorkspaceSearch")
            assert await orchestrator._heartbeat_approval(event) is False
        finally:
            SecurityEventLog._instance = None
            SecurityEventLog._initialized = False


# ── Heartbeat-scoped HookManager ──


class TestHeartbeatHooks:
    """``_build_heartbeat_hooks`` must not let user ``auto_approve_tools``
    widen the heartbeat allowlist (per code review).

    Without scoped hooks, ``llm_helpers._resolve_permission`` would consult
    ``HookManager.on_tool_call()`` BEFORE ``_heartbeat_approval`` and a user
    config like ``auto_approve_tools=["*"]`` would auto-approve any tool —
    bypassing ``HEARTBEAT_SAFE_TOOLS`` entirely.
    """

    def _user_hooks(self, **cfg):
        from kiro_crew.hooks import HookManager, HooksConfig
        return HookManager(HooksConfig(**cfg))

    def test_drops_user_auto_approve_tools(self) -> None:
        """User's auto_approve_tools must NOT carry into heartbeat hooks."""
        from kiro_crew.hooks import TOOL_AUTO_APPROVE
        from kiro_crew.slack.gateway import _build_heartbeat_hooks

        # User has a wide auto-approve list — this is the threat scenario.
        user = self._user_hooks(auto_approve_tools=["*", "Write*", "cron_*"])
        # Sanity: user's own hooks DO auto-approve the dangerous tools.
        assert user.on_tool_call("Write").action == TOOL_AUTO_APPROVE
        assert user.on_tool_call("cron_add").action == TOOL_AUTO_APPROVE

        # Heartbeat-scoped hooks MUST NOT auto-approve them.
        hb = _build_heartbeat_hooks(user)
        assert hb.on_tool_call("Write").action != TOOL_AUTO_APPROVE
        assert hb.on_tool_call("cron_add").action != TOOL_AUTO_APPROVE
        assert hb.on_tool_call("delete_file").action != TOOL_AUTO_APPROVE
        # And critically, no auto-approve for read tools either — the
        # heartbeat allowlist is the sole approval authority and runs
        # via on_tool_approval, not on_tool_call.
        assert hb.on_tool_call("ReadInternalWebsites").action != TOOL_AUTO_APPROVE

    def test_preserves_user_auto_deny_tools(self) -> None:
        """User's auto_deny_tools narrow what runs — those must carry over.

        Heartbeat denies should be at least as strict as the user config.
        """
        from kiro_crew.hooks import TOOL_DENY
        from kiro_crew.slack.gateway import _build_heartbeat_hooks

        user = self._user_hooks(auto_deny_tools=["dangerous_tool"])
        hb = _build_heartbeat_hooks(user)
        assert hb.on_tool_call("dangerous_tool").action == TOOL_DENY

    def test_does_not_inherit_bundled_auto_approve(self) -> None:
        """The bundled ``kirocrew browse *`` patterns from HooksConfig.from_dict
        must not carry into the heartbeat-scoped hooks.

        Heartbeat does not browse — anything outside HEARTBEAT_SAFE_TOOLS
        should reach _heartbeat_approval.
        """
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig
        from kiro_crew.slack.gateway import _build_heartbeat_hooks

        # User config goes through from_dict → bundled patterns merged in.
        user = HookManager(HooksConfig.from_dict({}))
        hb = _build_heartbeat_hooks(user)
        # The bundled "kirocrew browse *" pattern would auto-approve this in
        # the user-scoped hooks, but must NOT in the heartbeat scope.
        assert hb.on_tool_call("Running: kirocrew browse foo").action != TOOL_AUTO_APPROVE
