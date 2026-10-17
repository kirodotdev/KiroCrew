"""Tests for heartbeat tool allowlist + HEARTBEAT_KEEP gateway injection.

Covers:
- ``_is_heartbeat_safe_tool`` decides on the event's TRUSTED identity, never the title
- the core read mapping and the egress exclusion behind the heartbeat allowlist
- ``_HEARTBEAT_KEEP_INJECTION`` is the literal prefix expected by the agent
- ``GatewayOrchestrator._heartbeat_approval`` approves safe tools and rejects
  unsafe ones with a SEL audit event
- ``_build_heartbeat_hooks`` keeps the gate's read-only tier from deciding ahead of
  ``_heartbeat_approval``
"""

from __future__ import annotations

import dataclasses
import os
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.types import AcpEvent
from kiro_crew.slack.gateway import (
    _HEARTBEAT_KEEP_INJECTION,
    GatewayOrchestrator,
    _build_heartbeat_hooks,
    _is_heartbeat_safe_tool,
)


def _make_event(
    title: str,
    *,
    tool_kind: str = "mcp",
    request_id: str = "req-1",
    tool_name: str = "",
    mcp_server_name: str = "",
    trusted: bool = False,
    is_shell: bool = False,
    command: str | None = None,
) -> AcpEvent:
    """Build a minimal permission event.

    The identity fields default to the UNTRUSTED shape a backend with no ``_meta``
    channel produces; a test that wants an approval must stamp them explicitly.
    """
    return AcpEvent(
        kind="permission_request",
        title=title,
        tool_kind=tool_kind,
        request_id=request_id,
        tool_name=tool_name,
        mcp_server_name=mcp_server_name,
        mcp_identity_trusted=trusted,
        is_shell=is_shell,
        raw_tool_params={"command": command} if command is not None else None,
    )


def _builtin(tool_name: str, *, trusted: bool = True, title: str = "") -> AcpEvent:
    """A host built-in as kiro-cli stamps it: ``_meta.kiro.toolName``, no server."""
    return _make_event(title or tool_name, tool_name=tool_name, trusted=trusted)


def _mcp(server: str, tool: str, *, trusted: bool = True) -> AcpEvent:
    """An MCP-served tool with its owning server stamped."""
    return _make_event(
        f"Running: @{server}/{tool}", tool_name=tool, mcp_server_name=server, trusted=trusted
    )


def _shell(
    command: str | None,
    *,
    tool_name: str = "execute_bash",
    trusted: bool = True,
    server: str = "",
) -> AcpEvent:
    """A shell call: ``is_shell`` from the ACP kind, the command from the tool params."""
    return _make_event(
        "Running: a shell command",
        tool_kind="execute",
        tool_name=tool_name,
        mcp_server_name=server,
        trusted=trusted,
        is_shell=True,
        command=command,
    )


class _Gate:
    """A ``SlackEnterpriseGate`` whose heartbeat additions are *extra*."""

    def __init__(self, extra) -> None:
        self._extra = frozenset(extra)

    def validate_enterprise(self, *a, **k):
        return True

    def check_message_origin(self, *a, **k):
        return True

    def heartbeat_safe_tools(self):
        return self._extra


def _install_context(monkeypatch, **replacements) -> None:
    """Install a standalone platform context with some adapters replaced."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import build_default_context
    from kiro_crew.platform.context import set_context

    ctx = dataclasses.replace(build_default_context(KiroCrewConfig()), **replacements)
    set_context(ctx)
    monkeypatch.setattr("kiro_crew.platform.bootstrap._BOOTED", True, raising=False)


@pytest.fixture()
def _reset_ctx():
    from kiro_crew.platform.context import set_context

    yield
    set_context(None)


# ── Heartbeat-scoped denied-command state ──


class TestHeartbeatHooksCarryDeniedState:
    """``_build_heartbeat_hooks`` must reflect the CURRENT primary manager's
    denied-command opt-out state.

    Rebuilt per heartbeat run (see ``_init_heartbeat``), so a live
    Settings > Security change hot-reloaded into the primary manager reaches
    heartbeat sessions without a gateway restart. A once-at-init snapshot would
    let a heartbeat session keep enforcing a just-disabled rule (or skip a
    just-added user deny) — the cross-surface inconsistency this guards against.
    """

    def test_scoped_hooks_snapshot_live_denied_state(self) -> None:
        from kiro_crew.hooks import HookManager, HooksConfig, UserDeniedPattern

        primary = HookManager(HooksConfig())
        # Simulate a live opt-out mutation hot-reloaded into the primary manager.
        primary.reload(
            HooksConfig(
                denied_commands_disable_all=True,
                denied_commands_disabled_ids=["local-destructive-rm-rf-root"],
                denied_commands_user_added=[UserDeniedPattern(id="u1", pattern="frobnicate.*")],
            )
        )

        scoped = _build_heartbeat_hooks(primary)

        assert scoped._config.denied_commands_disable_all is True
        assert scoped._config.denied_commands_disabled_ids == ["local-destructive-rm-rf-root"]
        assert [p.pattern for p in scoped._config.denied_commands_user_added] == ["frobnicate.*"]
        # The user's auto-approve tools are still dropped (heartbeat safety).
        assert scoped._config.auto_approve_tools == []


# ── Allowlist membership ──


class TestHeartbeatSafeTools:
    def test_canonical_core_reads_are_the_kirocrew_core_set(self) -> None:
        """Spot-check core reads every heartbeat task should reasonably need. Each is
        served by ``kirocrew-core``, the one server the heartbeat spec always mounts;
        ``cron_list`` lives on ``kirocrew-cron``, which heartbeat never mounts."""
        from kiro_crew.slack.gateway import _HEARTBEAT_SAFE_CORE_MCP_TOOLS

        expected = {
            "learn_list",
            "spawn_list",
            "spawn_status",
            "artifact_list",
            "artifact_get",
            "artifact_versions",
            "local_knowledge_search",
        }
        assert expected <= _HEARTBEAT_SAFE_CORE_MCP_TOOLS
        assert "cron_list" not in _HEARTBEAT_SAFE_CORE_MCP_TOOLS

    def test_canonical_builtins_are_trusted(self) -> None:
        """The trusted channel's own built-in spellings (``fs_read``, not the ``Read``
        title) come from the hooks allowlist."""
        from kiro_crew.hooks import _HOST_READ_ONLY_BUILTIN_TOOLS

        for name in ["fs_read", "grep", "glob"]:
            assert name in _HOST_READ_ONLY_BUILTIN_TOOLS, name

    def test_write_tools_excluded(self) -> None:
        """Write/mutating tools must NOT be in the core read mapping."""
        from kiro_crew.slack.gateway import _HEARTBEAT_SAFE_CORE_MCP_TOOLS

        for name in [
            "send_message",
            "file_send",
            "cron_add",
            "cron_remove",
            "fs_write",
            "execute_bash",
            "TaskeiCreateTask",
            "TaskeiUpdateTask",
            "TicketingWriteActions",
            "CodeReviewWriteActions",
            "learn_add",
            "learn_remove",
        ]:
            assert name not in _HEARTBEAT_SAFE_CORE_MCP_TOOLS, f"{name} should NOT be allowlisted"

    def test_title_spellings_are_not_core_names(self) -> None:
        """Display-title spellings are not names the trusted channel ever stamps."""
        from kiro_crew.slack.gateway import _HEARTBEAT_SAFE_CORE_MCP_TOOLS

        for name in ["Read", "Grep", "Glob", "WorkspaceSearch"]:
            assert name not in _HEARTBEAT_SAFE_CORE_MCP_TOOLS, name


# ── _is_heartbeat_safe_tool ──


class TestIsHeartbeatSafeTool:
    def test_trusted_builtin_approves(self) -> None:
        assert _is_heartbeat_safe_tool(_builtin("fs_read"))
        assert _is_heartbeat_safe_tool(_builtin("grep"))
        assert _is_heartbeat_safe_tool(_builtin("glob"))

    def test_kiro_cli_read_alias_approves(self) -> None:
        """kiro-cli stamps its file-read built-in ``read``; the allowlist spells it
        ``fs_read`` and the hooks alias table joins the two."""
        assert _is_heartbeat_safe_tool(_builtin("read"))

    def test_untrusted_identity_denies_even_an_allowlisted_name(self) -> None:
        """No ``_meta`` identity, no approval — whatever the fields or title say."""
        assert not _is_heartbeat_safe_tool(_builtin("fs_read", trusted=False))
        assert not _is_heartbeat_safe_tool(_mcp("kirocrew-core", "learn_list", trusted=False))
        assert not _is_heartbeat_safe_tool(_make_event("Read"))
        assert not _is_heartbeat_safe_tool(_make_event("Running: @kirocrew-core/learn_list"))

    def test_title_never_decides(self) -> None:
        """A forged title over an unrecognised trusted identity is denied, and a
        hostile title over a trusted read is still approved: only identity counts."""
        assert not _is_heartbeat_safe_tool(_make_event("Read", tool_name="fs_write", trusted=True))
        assert _is_heartbeat_safe_tool(_builtin("fs_read", title="delete everything"))

    def test_empty_trusted_identity_denies(self) -> None:
        """A cache hit on two empty strings still counts as trusted (presence, not
        value): the shape a backend with no ``_meta.kiro`` support at all produces."""
        assert not _is_heartbeat_safe_tool(_make_event("fs_read", trusted=True))
        assert not _is_heartbeat_safe_tool(_make_event("", trusted=True))

    def test_builtin_name_with_a_server_attached_denies(self) -> None:
        """No built-in ever carries a server, so a server-qualified ``fs_read`` can only
        be an MCP server claiming that name."""
        assert not _is_heartbeat_safe_tool(_mcp("some-mcp", "fs_read"))
        assert not _is_heartbeat_safe_tool(_mcp("kirocrew-core", "grep"))

    def test_trusted_web_builtins_approve(self) -> None:
        """``web_fetch`` / ``web_search`` are in ``hooks._HOST_READ_ONLY_BUILTIN_TOOLS``,
        so a trusted host built-in by either name approves through the built-in rule."""
        assert _is_heartbeat_safe_tool(_builtin("web_fetch"))
        assert _is_heartbeat_safe_tool(_builtin("web_search"))

    def test_core_mcp_read_on_its_owning_server_approves(self) -> None:
        assert _is_heartbeat_safe_tool(_mcp("kirocrew-core", "learn_list"))
        assert _is_heartbeat_safe_tool(_mcp("kirocrew-core", "spawn_list"))
        assert _is_heartbeat_safe_tool(_mcp("kirocrew-core", "local_knowledge_search"))

    def test_core_mcp_read_on_the_wrong_or_no_server_denies(self) -> None:
        """A name in the core read set alone is not approval: the name must come
        from ``kirocrew-core``, and a same-named tool on another server is denied."""
        assert not _is_heartbeat_safe_tool(_mcp("evil-mcp", "learn_list"))
        assert not _is_heartbeat_safe_tool(_mcp("kirocrew-core", "cron_list"))
        assert not _is_heartbeat_safe_tool(_mcp("kirocrew-cron", "cron_list"))
        assert not _is_heartbeat_safe_tool(_mcp("kirocrew-cron", "learn_list"))
        assert not _is_heartbeat_safe_tool(
            _make_event("learn_list", tool_name="learn_list", trusted=True)
        )

    def test_core_write_tools_denied(self) -> None:
        assert not _is_heartbeat_safe_tool(_mcp("kirocrew-core", "send_message"))
        assert not _is_heartbeat_safe_tool(_mcp("kirocrew-core", "cron_add"))
        assert not _is_heartbeat_safe_tool(_mcp("kirocrew-core", "learn_add"))
        assert not _is_heartbeat_safe_tool(_mcp("builder-mcp", "CodeReviewWriteActions"))

    def test_unknown_read_shaped_names_denied(self) -> None:
        """Strict allowlist: a read-shaped name that is not a known read is denied —
        a verb fallback could be widened by names from polled external content."""
        for name in [
            "get_pipeline_status",
            "list_artifacts_v2",
            "get_all_credentials",
            "list_env_secrets",
        ]:
            assert not _is_heartbeat_safe_tool(_mcp("kirocrew-core", name)), name
            assert not _is_heartbeat_safe_tool(_builtin(name)), name


@pytest.mark.usefixtures("_reset_ctx")
class TestHeartbeatShellApproval:
    """A host shell command is judged on its recovered bytes, never its title."""

    def test_read_only_command_approves(self) -> None:
        assert _is_heartbeat_safe_tool(_shell("ls -la"))
        assert _is_heartbeat_safe_tool(_shell("cat README.md | head -20"))

    def test_mutating_command_denies(self) -> None:
        assert not _is_heartbeat_safe_tool(_shell("rm -rf build"))
        assert not _is_heartbeat_safe_tool(_shell("echo hi > out.txt"))

    def test_unrecoverable_command_denies(self) -> None:
        assert not _is_heartbeat_safe_tool(_shell(None))
        assert not _is_heartbeat_safe_tool(_shell(""))

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git log --oneline -5",
            "pwd; git status",
            "ls && git diff",
            "ls | GIT log",
            "git commit -am wip",
            "FOO=1 git status",
            "(git status)",
            "/usr/bin/git status",
            "ls\ngit status",
        ],
    )
    def test_every_git_invocation_denies(self, command: str) -> None:
        """git reads ``.git/config`` on every invocation and ``core.fsmonitor`` /
        ``core.hooksPath`` name a program there that no text classifier sees; with
        nobody behind this session the read-only subcommands are excluded too."""
        assert not _is_heartbeat_safe_tool(_shell(command))

    def test_untokenizable_command_denies(self) -> None:
        """A command whose program set ``name_grant.program_names`` cannot
        establish is denied before the classifier is consulted."""
        assert not _is_heartbeat_safe_tool(_shell('echo "unclosed'))

    def test_untrusted_or_nameless_shell_denies(self) -> None:
        assert not _is_heartbeat_safe_tool(_shell("ls", trusted=False))
        assert not _is_heartbeat_safe_tool(_shell("ls", tool_name=""))

    def test_mcp_tool_declaring_execute_is_judged_on_identity_not_text(self, monkeypatch) -> None:
        """``is_shell`` follows the agent-declared ACP kind, so an MCP tool can look
        shell-shaped while carrying a real server: it is matched on its pinned
        ``@server/tool`` identity, and a read-only-looking command text buys nothing."""
        _install_context(monkeypatch, slack_gate=_Gate([]))
        assert not _is_heartbeat_safe_tool(_shell("ls", server="edition-mcp", tool_name="run"))
        _install_context(monkeypatch, slack_gate=_Gate(["@edition-mcp/run"]))
        assert _is_heartbeat_safe_tool(_shell("rm -rf /", server="edition-mcp", tool_name="run"))


@pytest.mark.usefixtures("_reset_ctx")
class TestEditionHeartbeatAllowlist:
    """The CPP ``SlackEnterpriseGate.heartbeat_safe_tools()`` seam.

    A companion may ADD tool names; the public Default is empty (byte-identical).
    A qualified ``@server/tool`` entry must match ONLY that server's tool, not a
    same-bare-name tool from a different (or compromised) server.
    """

    def test_default_edition_set_is_empty(self, monkeypatch) -> None:
        """No companion -> the allowlist is byte-identical to the core set."""
        _install_context(monkeypatch, slack_gate=_Gate([]))
        assert not _is_heartbeat_safe_tool(_mcp("builder-mcp", "ReadInternalWebsites"))

    def test_qualified_entry_matches_only_its_server(self, monkeypatch) -> None:
        """A pinned ``@server/tool`` entry auto-approves that exact trusted identity…"""
        _install_context(monkeypatch, slack_gate=_Gate(["@builder-mcp/ReadInternalWebsites"]))
        assert _is_heartbeat_safe_tool(_mcp("builder-mcp", "ReadInternalWebsites"))

    def test_qualified_entry_rejects_same_bare_name_other_server(self, monkeypatch) -> None:
        """…but a DIFFERENT server exposing the same bare tool name is denied.

        This is the collision guard: pinning ``@builder-mcp/ReadInternalWebsites``
        must NOT auto-approve ``@evil-mcp/ReadInternalWebsites``.
        """
        _install_context(monkeypatch, slack_gate=_Gate(["@builder-mcp/ReadInternalWebsites"]))
        assert not _is_heartbeat_safe_tool(_mcp("evil-mcp", "ReadInternalWebsites"))
        # And the bare name alone (no server) is not an edition match either.
        assert not _is_heartbeat_safe_tool(
            _make_event("ReadInternalWebsites", tool_name="ReadInternalWebsites", trusted=True)
        )

    def test_bare_edition_entry_never_matches(self, monkeypatch) -> None:
        """A BARE edition entry must NOT auto-approve anything (deny-by-default)."""
        _install_context(monkeypatch, slack_gate=_Gate(["ReadInternalWebsites"]))
        assert not _is_heartbeat_safe_tool(_mcp("builder-mcp", "ReadInternalWebsites"))
        assert not _is_heartbeat_safe_tool(
            _make_event("ReadInternalWebsites", tool_name="ReadInternalWebsites", trusted=True)
        )

    def test_edition_entry_needs_a_trusted_identity_too(self, monkeypatch) -> None:
        """A title spelled like a pinned entry is still prose."""
        _install_context(monkeypatch, slack_gate=_Gate(["@builder-mcp/ReadInternalWebsites"]))
        assert not _is_heartbeat_safe_tool(
            _make_event("Running: @builder-mcp/ReadInternalWebsites")
        )
        assert not _is_heartbeat_safe_tool(
            _mcp("builder-mcp", "ReadInternalWebsites", trusted=False)
        )

    def test_bare_edition_pin_never_approves_a_host_builtin(self, monkeypatch) -> None:
        """A bare ``fs_write`` pin never reaches the host ``fs_write`` built-in (no
        server to qualify, and the name is outside ``_HOST_READ_ONLY_BUILTIN_TOOLS``),
        while ``@<server>/fs_read`` is that server's own tool named fs_read, approved
        as the edition's pin and never as the host read."""
        _install_context(monkeypatch, slack_gate=_Gate(["fs_write", "@edition-mcp/fs_read"]))
        assert not _is_heartbeat_safe_tool(_builtin("fs_write"))
        assert _is_heartbeat_safe_tool(_mcp("edition-mcp", "fs_read"))


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


def _no_name_grant_refusal(monkeypatch) -> None:
    """Shell approvals run the name-grant check; stub it clean for tests of the rest."""

    async def _ok(event):
        return None

    monkeypatch.setattr("kiro_crew.slack.gateway.name_grant.refusal_for_event", _ok)


class TestHeartbeatApproval:
    @pytest.mark.asyncio
    async def test_approves_allowlisted_tool(self, orchestrator, monkeypatch) -> None:
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _builtin("fs_read", title="Read")
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
        # The row names the identity the decision keyed on, not the display title.
        assert kwargs["tool_name"] == "fs_read"
        assert kwargs["metadata"]["reason"] == "in_heartbeat_safe_tools"
        # The approve-path audit MUST be a fail-closed (synchronous, raising)
        # SEL write — otherwise the async queue swallows a write failure and the
        # deny-by-default branch below becomes unreachable (pentest gap).
        assert kwargs["critical"] is True

    @pytest.mark.asyncio
    async def test_audit_label_is_server_qualified_for_mcp(self, orchestrator, monkeypatch) -> None:
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        assert await orchestrator._heartbeat_approval(_mcp("kirocrew-core", "learn_list")) is True
        assert sel_mock.log_tool_invocation.call_args.kwargs["tool_name"] == (
            "kirocrew-core/learn_list"
        )

    @pytest.mark.asyncio
    async def test_trusted_deny_keeps_the_trusted_label(self, orchestrator, monkeypatch) -> None:
        """A trusted identity is the judged input on the deny path too."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event(
            "Read", tool_name="cron_add", mcp_server_name="kirocrew-core", trusted=True
        )
        assert await orchestrator._heartbeat_approval(event) is False
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["tool_name"] == "kirocrew-core/cron_add"

    @pytest.mark.asyncio
    async def test_rejects_untrusted_allowlisted_title(self, orchestrator, monkeypatch) -> None:
        """A title spelled like a safe tool, with no trusted identity, is denied and
        audited under the (redacted) title it arrived with."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event("Running: @kirocrew-core/learn_list", request_id="req-untrusted")
        assert await orchestrator._heartbeat_approval(event) is False
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["tool_name"] == "Running: @kirocrew-core/learn_list"
        assert kwargs["metadata"]["reason"] == "no_trusted_identity"

    @pytest.mark.asyncio
    async def test_rejects_unknown_read_shaped_tool(self, orchestrator, monkeypatch) -> None:
        """Strict allowlist: unknown tool with a read-shaped name still rejects."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _mcp("kirocrew-core", "get_session_status")
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
        assert kwargs["metadata"]["reason"] == "no_trusted_identity"

    @pytest.mark.asyncio
    async def test_rejects_trusted_write_tool(self, orchestrator, monkeypatch) -> None:
        """A trusted identity off the allowlist is denied as not allowlisted."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _mcp("kirocrew-core", "send_message")
        assert await orchestrator._heartbeat_approval(event) is False
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["tool_name"] == "kirocrew-core/send_message"
        assert kwargs["metadata"]["reason"] == "not_in_heartbeat_safe_tools"

    @pytest.mark.asyncio
    async def test_untrusted_deny_warning_names_the_backend_condition(
        self, orchestrator, monkeypatch, caplog
    ) -> None:
        """With no trusted identity the operator is told every call is denied on
        this backend, not that one name is missing from the allowlist."""
        import logging

        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: MagicMock())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
            assert await orchestrator._heartbeat_approval(_make_event("fs_read")) is False
        messages = [r.getMessage() for r in caplog.records]
        assert any("published no trusted tool identity" in m for m in messages), messages
        assert not any("heartbeat allowlist" in m for m in messages), messages

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
            assert (
                "AKIAIOSFODNN7EXAMPLE" not in record.getMessage()
            ), f"Unredacted credential leaked into log: {record.getMessage()}"

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
        approve_event = _builtin("fs_read")
        assert await orchestrator._heartbeat_approval(approve_event) is False

    @pytest.mark.asyncio
    async def test_approve_fails_closed_with_real_async_sel_unwritable(
        self, orchestrator, monkeypatch, tmp_path
    ) -> None:
        """End-to-end guard for the pentest gap: with the REAL async SEL
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
            event = _builtin("fs_read")
            assert await orchestrator._heartbeat_approval(event) is False
        finally:
            SecurityEventLog._instance = None
            SecurityEventLog._initialized = False


class TestHeartbeatShellNameGrant:
    """A read-only shell command also passes the name-grant check before it runs."""

    @pytest.mark.asyncio
    async def test_clean_shell_approves(self, orchestrator, monkeypatch) -> None:
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        _no_name_grant_refusal(monkeypatch)
        assert await orchestrator._heartbeat_approval(_shell("ls -la")) is True
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "auto_approved"
        assert kwargs["tool_name"] == "execute_bash"

    @pytest.mark.asyncio
    async def test_name_grant_refusal_denies(self, orchestrator, monkeypatch) -> None:
        """A shadowed program name downgrades to deny: this session has no approver
        to fall through to."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        refusal = MagicMock(log_text="ls resolves to an agent-writable path")

        async def _refuse(event):
            return refusal

        declines: list[dict] = []
        monkeypatch.setattr("kiro_crew.slack.gateway.name_grant.refusal_for_event", _refuse)
        monkeypatch.setattr(
            "kiro_crew.slack.gateway.name_grant.log_decline",
            lambda **kw: declines.append(kw),
        )
        assert await orchestrator._heartbeat_approval(_shell("ls -la")) is False
        assert declines and declines[0]["tier"] == "heartbeat_safe_tool"
        assert declines[0]["source"] == "heartbeat"
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["metadata"]["reason"] == "name_grant_refused"

    @pytest.mark.asyncio
    async def test_name_grant_is_not_consulted_for_a_denied_shell(
        self, orchestrator, monkeypatch
    ) -> None:
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        calls: list[object] = []

        async def _record(event):
            calls.append(event)
            return None

        monkeypatch.setattr("kiro_crew.slack.gateway.name_grant.refusal_for_event", _record)
        assert await orchestrator._heartbeat_approval(_shell("git status")) is False
        assert calls == []


# ── Heartbeat-scoped HookManager ──


class TestHeartbeatHooks:
    """``_build_heartbeat_hooks`` must not let user ``auto_approve_tools``
    widen the heartbeat allowlist (per code review).

    Without scoped hooks, ``llm_helpers._resolve_permission`` would consult
    ``HookManager.on_tool_call()`` BEFORE ``_heartbeat_approval`` and a user
    config like ``auto_approve_tools=["*"]`` would auto-approve any tool —
    bypassing the heartbeat allowlist (``_heartbeat_approval``) entirely.
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

        Heartbeat does not browse — anything outside the heartbeat allowlist
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

    @staticmethod
    def _mcp_read(mgr, server: str, tool: str) -> str:
        """An identity-verified MCP frame whose ACP kind says read."""
        return mgr.on_tool_call(
            f"Running: @{server}/{tool}",
            tool_kind="read",
            mcp_server_name=server,
            mcp_tool_name=tool,
            mcp_identity_trusted=True,
        ).action

    def test_the_read_only_tier_gives_no_verdict(self) -> None:
        """Every read-shaped call the user's gate would auto-approve on the ACP kind,
        the title or a read-only shell command reaches ``_heartbeat_approval``
        instead: ``kind`` and the title are agent-influenced and this session has
        no approver behind the gate."""
        from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE
        from kiro_crew.slack.gateway import _build_heartbeat_hooks

        user = self._user_hooks()
        hb = _build_heartbeat_hooks(user)
        assert hb._config.defer_reads_to_approver is True  # noqa: SLF001
        # kind="read" on an MCP tool: approved by the user's gate, deferred here.
        assert self._mcp_read(user, "edition-mcp", "drop_table") == TOOL_AUTO_APPROVE
        assert self._mcp_read(hb, "edition-mcp", "drop_table") == TOOL_ALLOW
        # A read-shaped title with no kind.
        assert user.on_tool_call("read_all_credentials").action == TOOL_AUTO_APPROVE
        assert hb.on_tool_call("read_all_credentials").action == TOOL_ALLOW
        # A read-only shell command: heartbeat judges it itself.
        shell = dict(tool_kind="execute", is_shell=True, command="ls -la")
        assert user.on_tool_call("Running: ls -la", **shell).action == TOOL_AUTO_APPROVE
        assert hb.on_tool_call("Running: ls -la", **shell).action == TOOL_ALLOW

    def test_deferring_reads_never_widens_a_deny(self) -> None:
        from kiro_crew.hooks import TOOL_DENY
        from kiro_crew.slack.gateway import _build_heartbeat_hooks

        hb = _build_heartbeat_hooks(self._user_hooks())
        assert (
            hb.on_tool_call(
                "Running: rm -rf /", tool_kind="execute", is_shell=True, command="rm -rf /"
            ).action
            == TOOL_DENY
        )

    @pytest.mark.parametrize(
        ("server", "tool"), [("kirocrew-core", "artifact_delete"), ("atlassian", "createJiraIssue")]
    )
    def test_preserves_user_confirm_tools(self, server: str, tool: str) -> None:
        """A confirm-listed MCP call labelled read reaches ``_heartbeat_approval``."""
        from kiro_crew.hooks import TOOL_AUTO_APPROVE
        from kiro_crew.slack.gateway import _build_heartbeat_hooks

        hb = _build_heartbeat_hooks(self._user_hooks(confirm_tools=[f"@{server}/{tool}"]))
        assert self._mcp_read(hb, server, tool) != TOOL_AUTO_APPROVE
        assert hb._config.confirm_tools == [f"@{server}/{tool}"]  # noqa: SLF001

    def test_preserves_an_unknown_confirm_list(self) -> None:
        from kiro_crew.hooks import TOOL_AUTO_APPROVE
        from kiro_crew.slack.gateway import _build_heartbeat_hooks

        hb = _build_heartbeat_hooks(self._user_hooks(confirm_tools_unknown=True))
        assert self._mcp_read(hb, "atlassian", "getJiraIssue") != TOOL_AUTO_APPROVE
        assert hb._config.confirm_tools_unknown  # noqa: SLF001

    def test_a_read_the_confirm_list_does_not_name_is_unchanged(self) -> None:
        from kiro_crew.slack.gateway import _build_heartbeat_hooks

        listed = _build_heartbeat_hooks(self._user_hooks(confirm_tools=["@atlassian/create*"]))
        unset = _build_heartbeat_hooks(self._user_hooks())
        assert self._mcp_read(listed, "atlassian", "getJiraIssue") == self._mcp_read(
            unset, "atlassian", "getJiraIssue"
        )


# ── recorded wire: the gate on the events a real backend produces ──


def _recorded_permission_event(fixture: str, monkeypatch) -> AcpEvent:
    """Replay a recorded fixture and return the one permission event it yields.

    Runs the corpus harness's own replay (same parsers, same per-session caches,
    the fixture's declared ``gate_envelope_nonce``), and keeps the ``AcpEvent``
    ``build_permission_event`` returned before the harness flattens it to a dict.
    The event under test is the one the recorded wire produces, never a hand-built
    one, so the identity cache hit a backend's adapter causes is reproduced.
    """
    import acp_frame_replay_harness as harness

    captured: list[AcpEvent] = []
    flatten = harness.event_dict

    def _keep(event: AcpEvent):
        captured.append(event)
        return flatten(event)

    monkeypatch.setattr(harness, "event_dict", _keep)
    meta, frames = harness.read_fixture(harness.CORPUS / fixture)
    harness.replay_frames(frames, gate_envelope_nonce=meta.get("gate_envelope_nonce"))
    permissions = [e for e in captured if e.kind == "permission_request"]
    assert len(permissions) == 1, (fixture, permissions)
    return permissions[0]


class TestRecordedWire:
    """The gate's verdict on permission events replayed from the recorded corpus."""

    @pytest.mark.asyncio
    async def test_kiro_cli_shell_call_approves(self, orchestrator, monkeypatch) -> None:
        """kiro-cli 2.21.4 stamps ``_meta.kiro.toolName``: the read-only shell call
        it recorded (``echo hello-from-corpus``) is auto-approved."""
        event = _recorded_permission_event("kiro/session.jsonl", monkeypatch)
        assert event.mcp_identity_trusted is True
        assert event.tool_name == "shell"
        assert _is_heartbeat_safe_tool(event) is True
        _no_name_grant_refusal(monkeypatch)
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        assert await orchestrator._heartbeat_approval(event) is True
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "auto_approved"
        assert kwargs["metadata"]["reason"] == "in_heartbeat_safe_tools"

    @pytest.mark.parametrize(
        "fixture",
        ["claude/session.jsonl", "opencode/permission-request-live.jsonl"],
    )
    @pytest.mark.asyncio
    async def test_backend_without_meta_identity_denies_as_no_trusted_identity(
        self, orchestrator, monkeypatch, caplog, fixture: str
    ) -> None:
        """claude-agent-acp and opencode stamp no ``_meta`` identity: the cache hit
        on empty names sets the trusted flag with an empty ``tool_name``, and the
        deny is audited as a backend property with the backend warning."""
        import logging

        event = _recorded_permission_event(fixture, monkeypatch)
        assert event.mcp_identity_trusted is True
        assert event.tool_name == ""
        assert _is_heartbeat_safe_tool(event) is False
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
            assert await orchestrator._heartbeat_approval(event) is False
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["metadata"]["reason"] == "no_trusted_identity"
        messages = [r.getMessage() for r in caplog.records]
        assert any("published no trusted tool identity" in m for m in messages), messages
        assert not any("heartbeat allowlist" in m for m in messages), messages

    @pytest.mark.asyncio
    async def test_trusted_flag_with_empty_tool_name_denies_as_no_trusted_identity(
        self, orchestrator, monkeypatch, caplog
    ) -> None:
        """The same shape built directly: a trusted flag with no name behind it
        reads as a missing identity, not as an allowlist miss."""
        import logging

        event = _shell("ls", tool_name="", trusted=True)
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
            assert await orchestrator._heartbeat_approval(event) is False
        assert sel_mock.log_tool_invocation.call_args.kwargs["metadata"]["reason"] == (
            "no_trusted_identity"
        )
        messages = [r.getMessage() for r in caplog.records]
        assert any("published no trusted tool identity" in m for m in messages), messages
