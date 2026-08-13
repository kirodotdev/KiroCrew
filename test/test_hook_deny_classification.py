"""``ToolHookResult.security_deny`` must distinguish the two deny classes.

A cron counts security-blocked tools against a durable auto-pause budget, so the
distinction has to hold at its source: a governance ceiling denial is policy
state that a later loosening reverses, while a sensitive-path or deny-pattern
block is a defect in what was attempted.

These tests drive a REAL ``HookManager`` against a real policy rather than a
mock, because the value under test is chosen inside ``hooks.on_tool_call`` — a
suite that stubs that method stays green whichever constructor it calls, which
leaves the shared-type half of the contract unpinned.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.hooks import TOOL_DENY, HookManager
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy


def _install(policy_body) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def test_a_governance_ceiling_denial_is_not_a_security_deny() -> None:
    """Policy state, so a caller counting toward auto-pause must skip it.

    Denies on the ``commands`` scope rather than a filesystem path: a path glob
    resolves against host path semantics, so the same assertion would describe a
    different code path on Windows than on POSIX.
    """
    _install(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "commands": {"mode": "deny", "deny": ["*backdoor*"]},
        }
    )

    result = HookManager().on_tool_call("Running: install-backdoor --now", session_key="cli_chat")

    assert result.action == TOOL_DENY
    assert result.security_deny is False, (
        "a governance denial reported as a security block would durably "
        "auto-pause a healthy cron"
    )


def test_a_sensitive_path_denial_is_a_security_deny() -> None:
    """The unconditional keystone: the attempt itself is the problem."""
    result = HookManager().on_tool_call("Reading /home/u/.ssh/id_rsa", session_key="cli_chat")

    assert result.action == TOOL_DENY
    assert result.security_deny is True


def test_a_deny_pattern_block_is_a_security_deny() -> None:
    """The route that produced the original silent-cost bug."""
    result = HookManager().on_tool_call("Running: rm -rf /", session_key="cli_chat")

    assert result.action == TOOL_DENY
    assert result.security_deny is True
