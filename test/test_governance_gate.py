"""Phase 6 + 8 — the PreToolUse gate enforces governance, and audits denials.

The headline behavior: a profile (or policy) that excludes an MCP tool causes
KiroCrew to refuse the call at its own host gate, EVEN IF the kiro agent config
granted it.  Plus: a governance deny wins over a user auto-approve, and a deny
emits a redacted ``governance_decision`` SEL record.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield d
    gp.reset_store()


def _install_ceiling(monkeypatch, policy_body):
    """Compose a context carrying the given policy and install it as active."""
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx = dataclasses.replace(base, governance=ceiling)
    ctx_mod.set_context(ctx)
    return ctx


@pytest.fixture(autouse=True)
def _reset_ctx():
    yield
    ctx_mod.reset_context()


def _write(d, name, body):
    (d / f"{name}.json").write_text(json.dumps(body))


def test_profile_denies_mcp_tool_kiro_would_grant(profiles_dir, monkeypatch):
    # Policy: ungoverned mcp (deny[] = allow-all).  Profile bound to the cron
    # surface denies the whole @kirocrew-cron server.
    _install_ceiling(monkeypatch, {"version": 1, "boot": {"fail_closed": True}})
    _write(
        profiles_dir,
        "cron",
        {
            "name": "cron",
            "bind": {"type": "surface", "id": "cron"},
            "mcp": {"mode": "deny", "deny": ["@kirocrew-cron"]},
        },
    )
    hooks = HookManager(HooksConfig(auto_approve_tools=["*"]))  # kiro would auto-approve!
    result = hooks.on_tool_call("mcp__kirocrew-cron__cron_add", session_key="cron:job-1:run-1")
    assert result.action == TOOL_DENY
    assert "governance" in result.reason.lower()


def test_governance_deny_beats_user_auto_approve(profiles_dir, monkeypatch):
    _install_ceiling(monkeypatch, {"version": 1, "boot": {"fail_closed": True}})
    _write(
        profiles_dir,
        "dash",
        {
            "name": "dash",
            "bind": {"type": "surface", "id": "dashboard"},
            "tools": {"mode": "allow", "allow": ["read"]},  # only read
        },
    )
    hooks = HookManager(HooksConfig(auto_approve_tools=["*"]))
    # execute_bash is auto-approved by config but NOT in the profile allow-set.
    result = hooks.on_tool_call("execute_bash", session_key="dashboard:slot1")
    assert result.action == TOOL_DENY


def test_allowed_tool_passes(profiles_dir, monkeypatch):
    _install_ceiling(monkeypatch, {"version": 1, "boot": {"fail_closed": True}})
    _write(
        profiles_dir,
        "dash",
        {
            "name": "dash",
            "bind": {"type": "surface", "id": "dashboard"},
            "tools": {"mode": "allow", "allow": ["read", "grep"]},
        },
    )
    hooks = HookManager()
    result = hooks.on_tool_call("read", session_key="dashboard:slot1")
    assert result.action != TOOL_DENY


def test_no_policy_no_profile_is_noop(monkeypatch, tmp_path):
    # Ungoverned standalone host: gate behaves exactly as before.
    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "none")
    gp.reset_store()
    _install_ceiling(monkeypatch, None)
    hooks = HookManager()
    result = hooks.on_tool_call("execute_bash", session_key="cli_chat")
    assert result.action != TOOL_DENY


def test_policy_ceiling_denies_regardless_of_profile(profiles_dir, monkeypatch):
    # Policy denies a command; even with no profile the gate refuses.
    _install_ceiling(
        monkeypatch,
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "commands": {"mode": "deny", "deny": ["terraform destroy*"]},
        },
    )
    hooks = HookManager()
    result = hooks.on_tool_call("Running: terraform destroy -auto-approve", session_key="cli_chat")
    assert result.action == TOOL_DENY


def test_denial_emits_redacted_audit(profiles_dir, monkeypatch):
    events = []

    class _FakeSel:
        def log_governance_decision(self, **kw):
            events.append(kw)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _FakeSel())
    _install_ceiling(
        monkeypatch,
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "commands": {"mode": "deny", "deny": ["*secret*"]},
        },
    )
    hooks = HookManager()
    hooks.on_tool_call("Running: echo my-secret-value", session_key="cli_chat")
    assert events, "expected a governance_decision audit record"
    assert events[0]["outcome"] == "denied"


def test_single_token_command_still_governed(profiles_dir, monkeypatch):
    # An UNPREFIXED single-token command (claude-agent-acp delivers a bare bash
    # title) must still be checked against the commands ceiling — not silently
    # routed to the (absent) tools scope and permitted.
    _install_ceiling(
        monkeypatch,
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "commands": {"mode": "deny", "deny": ["whoami"]},
        },
    )
    hooks = HookManager()
    result = hooks.on_tool_call("whoami", session_key="cli_chat")
    assert result.action == TOOL_DENY


def test_unprefixed_multiword_tool_checked_against_tools(profiles_dir, monkeypatch):
    # An unprefixed multi-word title is checked against BOTH scopes; a tools
    # allow-set that excludes it still denies.
    _install_ceiling(monkeypatch, {"version": 1, "boot": {"fail_closed": True}})
    _write(
        profiles_dir,
        "dash",
        {
            "name": "dash",
            "bind": {"type": "surface", "id": "dashboard"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    hooks = HookManager()
    # "List files" is not in the tools allow-set → denied (would have been
    # misrouted to commands-only under the old single-pair classification).
    result = hooks.on_tool_call("List files", session_key="dashboard:s")
    assert result.action == TOOL_DENY


def test_file_read_title_not_name_gate_governed(profiles_dir, monkeypatch):
    # A "Reading <path>" title is governed at the filesystem chokepoint, not the
    # name gate — so a tools-only profile must not accidentally deny a read here.
    _install_ceiling(monkeypatch, {"version": 1, "boot": {"fail_closed": True}})
    _write(
        profiles_dir,
        "dash",
        {
            "name": "dash",
            "bind": {"type": "surface", "id": "dashboard"},
            "tools": {"mode": "allow", "allow": ["read"]},
        },
    )
    hooks = HookManager()
    result = hooks.on_tool_call("Reading /home/user/project/file.txt", session_key="dashboard:s")
    assert result.action != TOOL_DENY
