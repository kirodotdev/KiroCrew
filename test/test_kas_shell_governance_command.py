"""The governance row judges a KAS shell call on its recovered command.

KAS titles its shell call ``Run Command``, so a ``commands`` rule read off the
title alone never met the command: a policy deny such as ``*backdoor*`` let
``echo backdoor`` through to the read-only row's auto-approve. The row also asks
the recovered command, so the deny refuses it before any grant or classify row.
"""

from __future__ import annotations

from test_kas_allowed_commands import (  # noqa: F401 - _no_profiles is an autouse fixture
    _install_governance,
    _judge,
    _no_profiles,
    _shell,
)

from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY


def _deny_backdoor() -> None:
    _install_governance({"commands": {"mode": "deny", "deny": ["*backdoor*"]}})


def test_a_policy_deny_refuses_the_command_a_read_only_call_runs() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("echo backdoor"), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_a_policy_deny_refuses_a_granted_chain_holding_the_command() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("git status && echo backdoor"), ["git status", "echo .*"])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_a_harmless_read_only_command_is_still_auto_approved() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("ls"), [])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "read-only")


def test_a_command_the_deny_does_not_name_still_asks() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("git status --porcelain"), ["git status.*"])
    assert verdict.action == TOOL_ALLOW
