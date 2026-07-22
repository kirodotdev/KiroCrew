"""Enterprise-overlay checks for the CPP consumption-site wiring.

These compose an ``enterprise`` PlatformContext (via the companion if importable,
otherwise an inline overlay) and assert the wired consumption sites now reflect
the EXTENDED values: sandbox dirs include ``.sso``; the effective deny set is
a superset of baseline + overlay and an overlay pattern is blocked through the
hooks caller; the Slack gate is fail-closed; identity returns the enterprise
status; extra MCP servers land in the built agent config.

The standalone edition is verified separately in ``test_cpp_wiring_standalone``.
The core never imports a companion package — the test does (it lives outside the
core), falling back to an inline overlay when the companion isn't installed so
the suite is self-contained.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional, Tuple

import pytest

from kiro_crew import agent, sandbox
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.platform import (
    BASELINE_DENY,
    PROFILE_ENTERPRISE,
    PlatformCompositionError,
    PolicyAuthority,
    build_default_context,
    current_context,
    set_context,
)

# Sentinel overlay values the test asserts on.
_ENT_SANDBOX_EXTRA = [".sso", ".enterprise-creds"]
_ENT_DENY_EXTRA: Tuple[str, ...] = (
    "*get_sso_cookie*",
    "*sso_login*",
    "*fetch_enterprise_token*",
    "*kerberos_ticket*",
    "*enterprise_secret*",
    "*vault_credential*",
)
_ENT_MCP_NAME = "enterprise-mcp"
_ENT_MCP_SPEC = {"command": "/usr/bin/true", "args": ["enterprise-mcp"]}


# ── Inline enterprise overlay adapters (used when the companion isn't installed) ──


class _EnterpriseSandboxPolicy:
    def strict_dirs(self) -> List[str]:
        return list(sandbox._STRICT_DIRS) + list(_ENT_SANDBOX_EXTRA)

    def cc_dirs(self) -> List[str]:
        return list(sandbox._CC_DIRS) + list(_ENT_SANDBOX_EXTRA)


class _EnterpriseOverlay:
    def extra_deny_patterns(self) -> Tuple[str, ...]:
        return _ENT_DENY_EXTRA


class _FailClosedSlackGate:
    def validate_enterprise(self, bot_token, *, extra_ids=None) -> bool:
        return False

    def check_message_origin(self, event_team_id: str) -> bool:
        return False


class _EnterpriseIdentity:
    def status(self) -> Dict[str, object]:
        return {"available": True, "issuer": "sso", "user": "test-user"}

    async def status_line(self, prefix: str = "*SSO:*") -> str:
        return f"{prefix} valid (12h)"

    def whoami(self) -> Optional[str]:
        return "test-user"

    def issuer(self) -> Optional[str]:
        return "sso"


class _EnterpriseMcpTooling:
    def extra_mcp_servers(self) -> Dict[str, dict]:
        return {_ENT_MCP_NAME: dict(_ENT_MCP_SPEC)}

    def extra_skills(self):
        return []


@pytest.fixture
def enterprise_ctx(monkeypatch):
    """Install an enterprise PlatformContext and return it (companion or inline)."""
    cfg = KiroCrewConfig()
    ctx = _compose_enterprise_context(cfg, monkeypatch)
    set_context(ctx)
    return ctx


def _compose_enterprise_context(cfg: KiroCrewConfig, monkeypatch):
    """Compose via the companion if importable, else build the overlay inline."""
    try:
        from kiro_crew.platform.discovery import discover_companion_context

        companion = discover_companion_context(PROFILE_ENTERPRISE, cfg)
        if companion is not None and companion.profile == PROFILE_ENTERPRISE:
            return companion
    except Exception:
        pass  # companion absent or incompatible — fall back to inline overlay.

    base = build_default_context(cfg, profile=PROFILE_ENTERPRISE)
    return dataclasses.replace(
        base,
        sandbox=_EnterpriseSandboxPolicy(),
        security=PolicyAuthority(overlay=_EnterpriseOverlay()),
        slack_gate=_FailClosedSlackGate(),
        identity=_EnterpriseIdentity(),
        mcp_tooling=_EnterpriseMcpTooling(),
    )


# ── Task 2: sandbox dirs extended ──


def test_sandbox_strict_dirs_include_sso(enterprise_ctx) -> None:
    strict = enterprise_ctx.sandbox.strict_dirs()
    assert ".sso" in strict
    # Baseline dirs still present (ADD-only extension).
    assert set(sandbox._STRICT_DIRS) <= set(strict)


def test_seatbelt_profile_hides_sso(enterprise_ctx) -> None:
    """The wired ``_build_seatbelt_profile`` now denies reads of ``.sso``."""
    profile = sandbox._build_seatbelt_profile("strict")
    assert ".sso" in profile
    # And the cc branch keeps the .aws exclusion while adding overlay dirs.
    cc_profile = sandbox._build_seatbelt_profile("cc")
    assert ".sso" in cc_profile


def test_launcher_script_hides_sso(enterprise_ctx) -> None:
    """The wired ``_build_launcher_script`` embeds ``.sso`` in SENSITIVE_DIRS."""
    script = sandbox._build_launcher_script("strict")
    assert ".sso" in script


# ── Task 3: security deny floor extended (via the hooks caller) ──


def test_effective_patterns_superset_of_baseline_and_overlay(enterprise_ctx) -> None:
    eff = set(enterprise_ctx.security.effective_patterns())
    # Baseline is fully preserved (ADD-only floor) …
    assert set(BASELINE_DENY) <= eff
    # … and the enterprise overlay strictly extends it.  Both the real companion
    # and the inline fallback contribute the sso-cookie deny.
    assert eff > set(BASELINE_DENY)
    assert "*get_sso_cookie*" in eff


def test_hooks_caller_blocks_overlay_pattern(enterprise_ctx) -> None:
    """The hooks.py deny check routes through the context authority overlay."""
    mgr = HookManager(HooksConfig())
    # An overlay-only pattern (not in baseline) is now blocked — proving the
    # hooks caller consults current_context().security, not bare security.
    result = mgr.on_tool_call("please get_sso_cookie now")
    assert result.action == TOOL_DENY
    # A baseline pattern is still blocked.
    assert mgr.on_tool_call("get_secret_foo").action == TOOL_DENY
    # A benign command is still allowed.
    assert mgr.on_tool_call("ls -la").action != TOOL_DENY


# ── Task 4: Slack enterprise gate fail-closed ──


def test_slack_gate_is_fail_closed(enterprise_ctx) -> None:
    gate = current_context().slack_gate
    assert gate.validate_enterprise("xoxb-fake") is False
    assert gate.check_message_origin("TANY") is False


# ── Task 5: SSO identity is the enterprise one ──


def test_identity_is_enterprise(enterprise_ctx) -> None:
    ident = current_context().identity
    status = ident.status()
    # The enterprise identity reports a positive/available state — the OSS stub
    # returns {"valid": False}, so any truthy availability/validity signal
    # proves we're on the enterprise adapter (companion: {"valid": True, ...};
    # inline fallback: {"available": True, ...}).
    assert status.get("available") is True or status.get("valid") is True
    # And an SSO issuer is resolved (None on the OSS stub).
    assert ident.issuer() is not None


@pytest.mark.asyncio
async def test_identity_status_line_nonempty(enterprise_ctx) -> None:
    line = await current_context().identity.status_line(prefix=" · sso")
    assert line.strip() != ""


# ── Task 6: extra MCP servers land in the built agent config ──


def test_extra_mcp_server_in_agent_config(enterprise_ctx) -> None:
    """``build_agent_config`` merges the edition-contributed MCP server."""
    cfg = agent.build_agent_config()
    servers = cfg.get("mcpServers", {})
    assert _ENT_MCP_NAME in servers
    # Managed servers are still present (ADD-only merge).
    assert "kirocrew-core" in servers
    assert "kirocrew-cron" in servers


def test_extra_mcp_server_on_refresh(enterprise_ctx) -> None:
    """``_refresh_dynamic_fields`` also seeds the edition-contributed server."""
    existing: dict = {"name": "kirocrew", "mcpServers": {}}
    agent._refresh_dynamic_fields(existing)
    assert _ENT_MCP_NAME in existing["mcpServers"]


def test_extra_mcp_servers_fails_closed_on_composition_error(monkeypatch) -> None:
    """A ``PlatformCompositionError`` from the context must propagate, never be
    swallowed into an empty server set — matching the fail-closed redact() guard.
    A non-standalone host that cannot compose must abort, not silently drop the
    companion's internal MCP servers."""

    def _boom():
        raise PlatformCompositionError("cannot compose companion")

    monkeypatch.setattr(agent, "current_context", _boom)
    with pytest.raises(PlatformCompositionError):
        agent._extra_mcp_servers()


def test_extra_mcp_servers_other_errors_degrade_to_empty(monkeypatch) -> None:
    """A non-composition lookup failure still degrades to the empty set (the
    standalone-safe fallback) rather than crashing the caller."""

    class _BrokenTooling:
        def extra_mcp_servers(self):
            raise RuntimeError("transient lookup failure")

    class _Ctx:
        mcp_tooling = _BrokenTooling()

    monkeypatch.setattr(agent, "current_context", lambda: _Ctx())
    assert agent._extra_mcp_servers() == {}
