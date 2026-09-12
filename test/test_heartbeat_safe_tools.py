"""Tests for heartbeat tool allowlist + HEARTBEAT_KEEP gateway injection.

Covers:
- ``_is_heartbeat_safe_tool`` trusted-identity allowlist matching
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
    mcp_identity_trusted: bool = False,
    is_shell: bool = False,
    raw_tool_params: dict | None = None,
) -> AcpEvent:
    """Build a minimal AcpEvent for approval-callback tests.

    ``title`` is the LLM-authored display text only — it is never consulted
    by ``_is_heartbeat_safe_tool``, which authorizes on the trusted identity
    fields below. Callers that want an event to actually MATCH the allowlist
    must pass ``tool_name`` (+ ``mcp_server_name`` for an MCP-served tool)
    and ``mcp_identity_trusted=True`` explicitly — mirroring the real
    ``_meta.kiro`` cache-hit population path, never derived from ``title``.

    ``is_shell``/``raw_tool_params`` mirror the real dispatch shape for a
    shell tool_call: the command lives in ``raw_tool_params["command"]`` (see
    ``AcpEvent.shell_command`` / ``_command_from_tool_params``), recovered
    from host-observed bytes, never from ``title``.
    """
    return AcpEvent(
        kind="permission_request",
        title=title,
        tool_kind=tool_kind,
        request_id=request_id,
        tool_name=tool_name,
        mcp_server_name=mcp_server_name,
        mcp_identity_trusted=mcp_identity_trusted,
        is_shell=is_shell,
        raw_tool_params=raw_tool_params,
    )


def _trusted_builtin(name: str, **kw) -> AcpEvent:
    """A trusted event for a host builtin (no MCP server)."""
    return _make_event(name, tool_name=name, mcp_server_name="", mcp_identity_trusted=True, **kw)


def _trusted_mcp(name: str, server: str, **kw) -> AcpEvent:
    """A trusted event for an MCP-served tool."""
    return _make_event(
        name, tool_name=name, mcp_server_name=server, mcp_identity_trusted=True, **kw
    )


def _untrusted(name: str, *, mcp_server_name: str = "", **kw) -> AcpEvent:
    """An event whose identity was NOT read from the trusted cache-hit path —
    e.g. a title parsed from agent-authored text, or a backend that never
    populated ``_meta.kiro``. ``mcp_identity_trusted`` stays False regardless
    of what ``tool_name``/``mcp_server_name`` are set to, matching how a real
    forged title would arrive (the fields a naive parser might derive from
    ``title`` text, with no cache-hit backing them)."""
    return _make_event(
        name, tool_name=name, mcp_server_name=mcp_server_name, mcp_identity_trusted=False, **kw
    )


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
    def test_canonical_read_tools_present(self) -> None:
        """Spot-check tools every heartbeat task should reasonably need.

        Builtin spellings are the REAL trusted-channel ones
        (``hooks._HOST_READ_ONLY_BUILTIN_TOOLS``: ``fs_read``/``glob``/``grep``,
        lowercase snake_case) — not the ACP title-case spellings
        (``Read``/``Grep``/``Glob``) a prior revision hand-maintained here,
        which the trusted ``_meta.kiro`` channel never emits. Design Review on
        kirodotdev/KiroCrew#10158 caught that divergence: see
        ``test_builtin_set_matches_trusted_channel_spellings``. ``web_fetch``/
        ``web_search`` are DELIBERATELY absent — see
        ``test_network_egress_excluded_from_heartbeat_safe_tools_constant``.
        """
        for name in [
            "fs_read",
            "glob",
            "grep",
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

    def test_network_egress_excluded_from_heartbeat_safe_tools_constant(self) -> None:
        """``HEARTBEAT_SAFE_TOOLS`` must not advertise names the runtime
        function never approves. web_fetch/web_search are trusted read-only
        builtins on the INTERACTIVE path but are heartbeat-excluded (First
        Principles Review, kirodotdev/KiroCrew#10158 item 6) — a constant
        documented as "what this module will approve" that still listed them
        would be a lying constant, not just an unused one."""
        assert "web_fetch" not in HEARTBEAT_SAFE_TOOLS
        assert "web_search" not in HEARTBEAT_SAFE_TOOLS

    def test_write_tools_excluded(self) -> None:
        """Write/mutating tools must NOT be in the allowlist."""
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
            assert name not in HEARTBEAT_SAFE_TOOLS, f"{name} should NOT be in allowlist"

    def test_builtin_set_matches_trusted_channel_spellings(self) -> None:
        """``HEARTBEAT_SAFE_TOOLS``' builtin half must be sourced from the SAME
        constant the trusted ``_meta.kiro`` channel actually populates
        (``hooks._HOST_READ_ONLY_BUILTIN_TOOLS``), not a second, independently
        maintained set that can silently diverge from it — MINUS the
        heartbeat-specific network-egress exclusion
        (``_HEARTBEAT_EXCLUDED_BUILTIN_TOOLS``), which is deliberate, not
        drift.

        Design Review BLOCK on kirodotdev/KiroCrew#10158: a prior revision's
        ``_HEARTBEAT_SAFE_BUILTINS`` used ACP title-case spellings
        (``Read``/``Grep``/``Glob``/``WorkspaceSearch``) that
        ``hooks.py:1466``'s real trusted-channel set
        (``fs_read``/``glob``/``grep``/``web_fetch``/``web_search``) never
        emits — every real builtin heartbeat call would have been denied on
        any backend that actually populates ``_meta.kiro``. Pinning identity
        (not just membership) here means a future edit to either set breaks
        this test immediately instead of silently re-diverging.
        """
        from kiro_crew.hooks import _HOST_READ_ONLY_BUILTIN_TOOLS
        from kiro_crew.slack.gateway import _HEARTBEAT_EXCLUDED_BUILTIN_TOOLS

        assert HEARTBEAT_SAFE_TOOLS >= (
            _HOST_READ_ONLY_BUILTIN_TOOLS - _HEARTBEAT_EXCLUDED_BUILTIN_TOOLS
        )
        # The exclusion removes exactly the network-egress names, nothing else.
        assert _HEARTBEAT_EXCLUDED_BUILTIN_TOOLS == frozenset({"web_fetch", "web_search"})
        assert not (HEARTBEAT_SAFE_TOOLS & _HEARTBEAT_EXCLUDED_BUILTIN_TOOLS)
        # None of the old, unverified title-case spellings survive.
        for stale_name in ("Read", "Grep", "Glob", "WorkspaceSearch"):
            assert stale_name not in _HOST_READ_ONLY_BUILTIN_TOOLS


# ── _is_heartbeat_safe_tool ──


class TestIsHeartbeatSafeTool:
    def test_allowlist_match(self) -> None:
        assert _is_heartbeat_safe_tool(_trusted_builtin("fs_read"))

    def test_untrusted_identity_rejected_even_for_allowlisted_name(self) -> None:
        """The core security property this function exists to enforce: an
        event whose identity was NOT read from the trusted ``_meta.kiro``
        cache-hit path is denied outright, even when ``tool_name`` spells an
        allowlisted name exactly — a forged/agent-authored title over a real
        write call must never auto-approve just because the text matches.

        GPT 5.6 Review BLOCK-MERGE (upheld by Opus 4.8 adjudication) on
        kirodotdev/KiroCrew#10158: keying this decision on ``event.title``
        (LLM-authored prose) rather than the trusted identity fields lets a
        heartbeat task that polls untrusted external content (a CR comment,
        a ticket body) forge a title spelled like a trusted tool name over a
        real write call, auto-approving a write the gate never intended to
        allow. This test pins the fix: an untrusted identity denies
        regardless of what ``tool_name``/``mcp_server_name`` happen to hold.
        """
        assert not _is_heartbeat_safe_tool(_untrusted("fs_read"))
        assert not _is_heartbeat_safe_tool(
            _untrusted("artifact_get", mcp_server_name="kirocrew-core")
        )
        assert not _is_heartbeat_safe_tool(_untrusted("grep"))
        # The exact forged-write shape the finding describes: an untrusted
        # event whose tool_name/mcp_server_name spell out a trusted read
        # tool's full identity, but with no cache-hit provenance backing it.
        assert not _is_heartbeat_safe_tool(
            _untrusted("send_message", mcp_server_name="kirocrew-core")
        )

    def test_unknown_read_verb_rejected_strict_allowlist(self) -> None:
        """Strict allowlist: unknown tools (even with read-shaped names)
        must NOT auto-approve. Per security-controls deny-by-default and
        threat model — a verb-based fallback could be widened by injected
        names like ``get_all_credentials`` from polled external content."""
        # Read-shaped but not in HEARTBEAT_SAFE_TOOLS, even when trusted.
        assert not _is_heartbeat_safe_tool(_trusted_builtin("get_pipeline_status"))
        assert not _is_heartbeat_safe_tool(_trusted_builtin("list_artifacts_v2"))
        # Adversarial names that would have passed an old verb fallback.
        assert not _is_heartbeat_safe_tool(_trusted_builtin("get_all_credentials"))
        assert not _is_heartbeat_safe_tool(_trusted_builtin("list_env_secrets"))
        assert not _is_heartbeat_safe_tool(_trusted_builtin("read_secret_from_vault"))

    def test_unknown_write_verb_rejected(self) -> None:
        assert not _is_heartbeat_safe_tool(_trusted_builtin("send_message"))
        assert not _is_heartbeat_safe_tool(_trusted_builtin("create_ticket"))
        assert not _is_heartbeat_safe_tool(_trusted_builtin("delete_artifact"))

    def test_empty_tool_name_rejected(self) -> None:
        assert not _is_heartbeat_safe_tool(_make_event("", tool_name="", mcp_identity_trusted=True))

    def test_mcp_qualified_core_tool_matches_its_owning_server(self) -> None:
        """An MCP-served core tool matches when trusted AND qualified to its
        actual owning server (most are ``kirocrew-core``; ``cron_list`` is
        served by the separate ``kirocrew-cron`` server)."""
        assert _is_heartbeat_safe_tool(_trusted_mcp("learn_list", "kirocrew-core"))
        assert _is_heartbeat_safe_tool(_trusted_mcp("spawn_list", "kirocrew-core"))
        assert _is_heartbeat_safe_tool(_trusted_mcp("local_knowledge_search", "kirocrew-core"))
        assert _is_heartbeat_safe_tool(_trusted_mcp("cron_list", "kirocrew-cron"))

    def test_mcp_qualified_write_tool_still_rejected(self) -> None:
        """A trusted identity must not widen the allowlist — write tools
        with a real, trusted MCP identity must still be rejected."""
        assert not _is_heartbeat_safe_tool(_trusted_mcp("send_message", "kirocrew-core"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("CodeReviewWriteActions", "builder-mcp"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("cron_add", "kirocrew-core"))

    def test_trusted_builtin_matches_by_name_alone(self) -> None:
        """A trusted host builtin (no MCP server) matches on tool_name alone —
        using the REAL trusted-channel spellings, not ACP title-case. Network
        egress (web_fetch/web_search) is heartbeat-excluded — see
        ``test_network_egress_builtins_excluded_from_heartbeat`` below."""
        assert _is_heartbeat_safe_tool(_trusted_builtin("fs_read"))
        assert _is_heartbeat_safe_tool(_trusted_builtin("grep"))
        assert _is_heartbeat_safe_tool(_trusted_builtin("glob"))

    def test_network_egress_builtins_excluded_from_heartbeat(self) -> None:
        """web_fetch/web_search are trusted read-only builtins on the
        INTERACTIVE path (``hooks._HOST_READ_ONLY_BUILTIN_TOOLS``, which has a
        human approver behind it) but heartbeat must not gain them through
        unconditional delegation: heartbeat's allowlist never grants outbound
        network calls. This test pins the heartbeat-specific exclusion."""
        assert not _is_heartbeat_safe_tool(_trusted_builtin("web_fetch"))
        assert not _is_heartbeat_safe_tool(_trusted_builtin("web_search"))

    def test_trusted_read_only_shell_command_approved(self) -> None:
        """A shell tool_call whose RECOVERED command is genuinely read-only
        auto-approves in heartbeat via the SAME deny-by-default bash
        classifier (``is_read_only_bash``) the interactive path already
        trusts for this judgment — not via ``event.tool_kind``/``title``.
        """
        event = _make_event(
            "",
            tool_name="execute_bash",
            mcp_server_name="",
            mcp_identity_trusted=True,
            is_shell=True,
            raw_tool_params={"command": "git status --porcelain"},
        )
        assert _is_heartbeat_safe_tool(event)
        assert _is_heartbeat_safe_tool(
            _make_event(
                "",
                tool_name="execute_bash",
                mcp_server_name="",
                mcp_identity_trusted=True,
                is_shell=True,
                raw_tool_params={"command": "ls -la"},
            )
        )
        assert _is_heartbeat_safe_tool(
            _make_event(
                "",
                tool_name="execute_bash",
                mcp_server_name="",
                mcp_identity_trusted=True,
                is_shell=True,
                raw_tool_params={"command": "grep -rn TODO ."},
            )
        )

    def test_mutating_shell_command_denied(self) -> None:
        """A shell tool_call whose recovered command is NOT read-only must be
        denied — the classifier decides, not a blanket shell auto-approve."""
        event = _trusted_builtin("", is_shell=True, raw_tool_params={"command": "rm -rf /tmp/foo"})
        assert not _is_heartbeat_safe_tool(event)
        assert not _is_heartbeat_safe_tool(
            _trusted_builtin("", is_shell=True, raw_tool_params={"command": "git push origin main"})
        )

    def test_shell_command_unrecoverable_denied(self) -> None:
        """When ``AcpEvent.shell_command`` cannot recover a command (neither
        ``raw_tool_params`` nor ``tool_input`` carries a usable shape), the
        call is denied — same deny-by-default the interactive path applies
        to an unrecoverable shell command, never a silent auto-approve on
        the strength of ``is_shell`` alone."""
        assert not _is_heartbeat_safe_tool(_trusted_builtin("", is_shell=True, raw_tool_params={}))
        assert not _is_heartbeat_safe_tool(_trusted_builtin("", is_shell=True))

    def test_shell_command_empty_tool_name_denied(self) -> None:
        """A cache-hit-on-empty-strings still satisfies
        ``mcp_identity_trusted`` (a backend with no ``_meta.kiro`` support
        still gets both identity caches written as ``""``). An empty
        ``tool_name`` must deny even with a genuinely read-only command and
        no server — the earlier tests in this class already exercise this
        shape via ``_trusted_builtin("", is_shell=True, ...)`` and must keep
        denying; this test names the shape explicitly."""
        event = _make_event(
            "",
            tool_name="",
            mcp_server_name="",
            mcp_identity_trusted=True,
            is_shell=True,
            raw_tool_params={"command": "git status --porcelain"},
        )
        assert not _is_heartbeat_safe_tool(event)

    def test_shell_command_nonempty_tool_name_still_approves(self) -> None:
        """A genuinely non-empty host-shell ``tool_name`` (what a real
        backend populating ``_meta.kiro`` sends) still approves a read-only
        command — the empty-identity check narrows the gap without removing
        heartbeat's ability to run a genuinely safe shell command."""
        event = _make_event(
            "",
            tool_name="execute_bash",
            mcp_server_name="",
            mcp_identity_trusted=True,
            is_shell=True,
            raw_tool_params={"command": "git status --porcelain"},
        )
        assert _is_heartbeat_safe_tool(event)

    def test_shell_command_untrusted_identity_denied(self) -> None:
        """The shell branch is reached only past the ``mcp_identity_trusted``
        gate at the top of this function — an untrusted identity is denied
        before the command is ever inspected, same as every other branch."""
        event = _make_event(
            "",
            tool_name="",
            mcp_identity_trusted=False,
            is_shell=True,
            raw_tool_params={"command": "git status"},
        )
        assert not _is_heartbeat_safe_tool(event)

    def test_mcp_served_shell_event_denied(self) -> None:
        """An MCP-served tool declaring ``is_shell=True`` (a real
        ``mcp_server_name``) must NOT auto-approve on the command string
        alone — only a true host shell tool has no server behind it. The
        shell branch must check server-emptiness the same way
        ``_is_host_read_only_builtin`` does, so an edition MCP tool
        declaring ``kind="execute"`` with a benign-looking command cannot
        bypass the allowlist's server pinning."""
        event = _trusted_mcp(
            "", "some-edition-server", is_shell=True, raw_tool_params={"command": "ls"}
        )
        assert not _is_heartbeat_safe_tool(event)

    def test_bare_core_mcp_tool_name_denied(self) -> None:
        """A CORE MCP tool name with no server identity is denied, not
        approved, even when the event's identity IS trusted — trust alone
        does not satisfy the per-name owning-server requirement.

        Design Review / First Principles Review CONCERNS on
        kirodotdev/KiroCrew#10158 flagged this exact case as undeclared and
        unpinned — this test pins it.
        """
        assert not _is_heartbeat_safe_tool(_trusted_mcp("learn_list", ""))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("cron_list", ""))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("spawn_list", ""))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("artifact_get", ""))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("local_knowledge_search", ""))

    def test_core_mcp_tool_qualified_to_wrong_server_denied(self) -> None:
        """A core MCP tool name qualified to a server OTHER than its real
        owner is denied — the qualification must match the SPECIFIC owning
        server, not merely be present and non-empty.

        ``cron_list`` is served by ``kirocrew-cron``, not ``kirocrew-core``;
        every other name in the core set is the reverse. Both directions are
        pinned so a future edit cannot silently widen either mapping.
        """
        assert not _is_heartbeat_safe_tool(_trusted_mcp("cron_list", "kirocrew-core"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("learn_list", "kirocrew-cron"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("artifact_get", "kirocrew-cron"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("learn_list", "edition-server"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("artifact_get", "edition-server"))

    def test_qualified_builtin_name_denied(self) -> None:
        """A host BUILTIN name (``fs_read``/``grep``/``glob``/etc.) arriving
        WITH a server identity is denied, not approved.

        A real host builtin never carries a server identity, so a trusted
        event using one of these names but WITH a server attached can only be
        an MCP server -- edition or otherwise -- claiming that identity.
        Without this guard, ``@edition/fs_read`` would match on the bare name
        alone and auto-approve as if it were the trusted builtin, regardless
        of which server actually served it.
        """
        assert not _is_heartbeat_safe_tool(_trusted_mcp("fs_read", "edition-server"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("grep", "edition-server"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("glob", "edition-server"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("web_search", "edition-server"))
        # The bare form (no server at all) is the one that DOES match --
        # confirms the guard is specifically "trusted + same name + a server",
        # not a blanket denial of these names.
        assert _is_heartbeat_safe_tool(_trusted_builtin("fs_read"))
        assert _is_heartbeat_safe_tool(_trusted_builtin("grep"))


class TestEditionHeartbeatAllowlist:
    """The CPP ``SlackEnterpriseGate.heartbeat_safe_tools()`` seam.

    A companion may ADD tool names; the public Default is empty (byte-identical).
    A qualified ``@server/tool`` entry must match ONLY that server's tool, not a
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
        monkeypatch.setattr("kiro_crew.platform.bootstrap._BOOTED", True, raising=False)

    @pytest.fixture(autouse=True)
    def _reset_ctx(self):
        from kiro_crew.platform.context import set_context

        yield
        set_context(None)

    def test_default_edition_set_is_empty(self, monkeypatch) -> None:
        """No companion → the allowlist is byte-identical to the core set."""
        self._install_gate_extra(monkeypatch, [])
        assert not _is_heartbeat_safe_tool(_trusted_mcp("ReadInternalWebsites", "builder-mcp"))

    def test_qualified_entry_matches_only_its_server(self, monkeypatch) -> None:
        """A pinned ``@server/tool`` entry auto-approves that exact identity…"""
        self._install_gate_extra(monkeypatch, ["@builder-mcp/ReadInternalWebsites"])
        assert _is_heartbeat_safe_tool(_trusted_mcp("ReadInternalWebsites", "builder-mcp"))

    def test_qualified_entry_rejects_same_bare_name_other_server(self, monkeypatch) -> None:
        """…but a DIFFERENT server exposing the same bare tool name is denied.

        This is the collision guard: pinning ``@builder-mcp/ReadInternalWebsites``
        must NOT auto-approve ``@evil-mcp/ReadInternalWebsites``.
        """
        self._install_gate_extra(monkeypatch, ["@builder-mcp/ReadInternalWebsites"])
        assert not _is_heartbeat_safe_tool(_trusted_mcp("ReadInternalWebsites", "evil-mcp"))
        # And the bare name alone (no server) is not auto-approved when only
        # a qualified entry was allowlisted.
        assert not _is_heartbeat_safe_tool(_trusted_mcp("ReadInternalWebsites", ""))

    def test_bare_edition_entry_never_matches(self, monkeypatch) -> None:
        """A BARE edition entry must NOT auto-approve anything (deny-by-default).

        Entries MUST be server-qualified; a bare name would carry the collision
        risk (a different server's same-named destructive tool auto-approved), so
        the gate ignores it entirely — neither a qualified nor a bare title match.
        """
        self._install_gate_extra(monkeypatch, ["ReadInternalWebsites"])
        assert not _is_heartbeat_safe_tool(_trusted_mcp("ReadInternalWebsites", "builder-mcp"))
        assert not _is_heartbeat_safe_tool(_trusted_mcp("ReadInternalWebsites", ""))

    def test_edition_tool_bare_name_colliding_with_core_tool_still_matches(
        self, monkeypatch
    ) -> None:
        """A core-tool bare name (``learn_list``) is looked up in
        ``_HEARTBEAT_SAFE_CORE_MCP_TOOLS`` first; when an edition server's OWN
        tool happens to share that bare name but is qualified to a DIFFERENT
        server, the core-name lookup must not short-circuit the whole function
        with a deny — it must fall through to the edition lookup below, which
        can still legitimately match the pinned ``@edition-server/learn_list``
        identity. Denying here would be a false negative: an edition server
        that pins the exact trusted identity gets rejected only because its
        bare name happens to collide with an unrelated core tool."""
        self._install_gate_extra(monkeypatch, ["@edition-server/learn_list"])
        assert _is_heartbeat_safe_tool(_trusted_mcp("learn_list", "edition-server"))
        # The real core tool, qualified to ITS real server, still matches too —
        # the fallthrough must not disturb the core-tool path for its own server.
        assert _is_heartbeat_safe_tool(_trusted_mcp("learn_list", "kirocrew-core"))
        # And the core-tool bare name qualified to neither its real owner nor
        # the edition entry's server is still denied.
        assert not _is_heartbeat_safe_tool(_trusted_mcp("learn_list", "some-other-mcp"))

    def test_unqualified_identity_never_matches_edition_set(self, monkeypatch) -> None:
        """A trusted identity with no server never matches the edition set
        even when a qualified entry is allowlisted."""
        self._install_gate_extra(monkeypatch, ["@builder-mcp/ReadInternalWebsites"])
        assert not _is_heartbeat_safe_tool(_trusted_mcp("ReadInternalWebsites", ""))


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
        event = _trusted_builtin("fs_read")
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
        assert kwargs["tool_name"] == "fs_read"
        assert kwargs["metadata"]["reason"] == "in_heartbeat_safe_tools"
        # The approve-path audit MUST be a fail-closed (synchronous, raising)
        # SEL write — otherwise the async queue swallows a write failure and the
        # deny-by-default branch below becomes unreachable (pentest gap).
        assert kwargs["critical"] is True

    @pytest.mark.asyncio
    async def test_audit_label_uses_trusted_tool_name_not_display_title(
        self, orchestrator, monkeypatch
    ) -> None:
        """The audit label must reflect what the approval decision actually
        keyed on (the trusted ``tool_name``), not the LLM-authored display
        ``title`` — a real host builtin naturally has a different title
        (``fs_read`` displays as ``Read``). Approving on one identity and
        auditing under a different name would let an operator's SEL review
        show the wrong tool ran."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event(
            "Read", tool_name="fs_read", mcp_server_name="", mcp_identity_trusted=True
        )
        assert await orchestrator._heartbeat_approval(event) is True
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "fs_read", (
            f"audit recorded {kwargs['tool_name']!r} — the display title — "
            "instead of the trusted tool_name the approval decision used"
        )

    @pytest.mark.asyncio
    async def test_audit_label_uses_trusted_mcp_tool_qualified_to_server(
        self, orchestrator, monkeypatch
    ) -> None:
        """Same divergence, for an MCP-served tool: the audit label must be
        qualified to the owning server, matching what the allowlist actually
        matched against — not the bare display title."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event(
            "List Learned Lessons",
            tool_name="learn_list",
            mcp_server_name="kirocrew-core",
            mcp_identity_trusted=True,
        )
        assert await orchestrator._heartbeat_approval(event) is True
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "kirocrew-core/learn_list"

    @pytest.mark.asyncio
    async def test_audit_label_falls_back_to_redacted_title_when_untrusted(
        self, orchestrator, monkeypatch
    ) -> None:
        """On the deny path (or any untrusted event) the trusted fields are
        not provable, so the audit label must stay the redacted display
        title — that is the (possibly forged) input being rejected, and it
        is what an operator needs to see to decide whether to extend
        HEARTBEAT_SAFE_TOOLS."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _untrusted("get_all_credentials")
        assert await orchestrator._heartbeat_approval(event) is False
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "get_all_credentials"

    @pytest.mark.asyncio
    async def test_rejects_unknown_read_shaped_tool(self, orchestrator, monkeypatch) -> None:
        """Strict allowlist: unknown tool with a read-shaped name still rejects."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _trusted_builtin("get_session_status", request_id="req-unknown")
        assert await orchestrator._heartbeat_approval(event) is False
        sel_mock.log_tool_invocation.assert_called_once()
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["metadata"]["reason"] == "not_in_heartbeat_safe_tools"

    @pytest.mark.asyncio
    async def test_shell_command_still_needs_name_grant_clearance(
        self, orchestrator, monkeypatch
    ) -> None:
        """A read-only-classified shell command still denies when the
        program name is shadowed on PATH — the classifier judges the
        command text, name_grant judges the executable identity behind it.
        Every other unattended shell auto-approve runs this check
        unconditionally; heartbeat must too."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)

        class _FakeRefusal:
            code = "shadowed_path"
            log_text = "a program name could not be vouched for"

        async def _refuse(event):
            return _FakeRefusal()

        monkeypatch.setattr("kiro_crew.slack.gateway.name_grant.refusal_for_event", _refuse)
        log_decline_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.name_grant.log_decline", log_decline_mock)

        event = _make_event(
            "",
            tool_name="execute_bash",
            mcp_server_name="",
            mcp_identity_trusted=True,
            is_shell=True,
            raw_tool_params={"command": "git status --porcelain"},
        )
        assert await orchestrator._heartbeat_approval(event) is False
        log_decline_mock.assert_called_once()
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["metadata"]["reason"] == "name_grant_refused"

    @pytest.mark.asyncio
    async def test_shell_command_approves_when_name_grant_clears(
        self, orchestrator, monkeypatch
    ) -> None:
        """A genuinely read-only shell command with a clean (unshadowed)
        program name still auto-approves — the name-grant check adds a
        gate, it does not remove the capability this PR restored."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)

        async def _clear(event):
            return None

        monkeypatch.setattr("kiro_crew.slack.gateway.name_grant.refusal_for_event", _clear)

        event = _make_event(
            "",
            tool_name="execute_bash",
            mcp_server_name="",
            mcp_identity_trusted=True,
            is_shell=True,
            raw_tool_params={"command": "git status --porcelain"},
        )
        assert await orchestrator._heartbeat_approval(event) is True
        kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "auto_approved"

        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _trusted_builtin("send_message", request_id="req-write")
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
    async def test_rejects_untrusted_identity(self, orchestrator, monkeypatch) -> None:
        """A forged/untrusted identity is rejected outright, even naming an
        allowlisted tool — the core property this whole gate protects."""
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _untrusted("fs_read")
        assert await orchestrator._heartbeat_approval(event) is False

    @pytest.mark.asyncio
    async def test_rejects_empty_title(self, orchestrator, monkeypatch) -> None:
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        event = _make_event("", tool_name="", mcp_identity_trusted=True)
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
        event = _trusted_builtin(bad_title)
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
        deny_event = _trusted_builtin("send_message")
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
        approve_event = _trusted_builtin("fs_read")
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
            event = _trusted_builtin("fs_read")
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
