"""The enterprise built-in baseline: an unnamed scope answers its minimal set.

RFC ``docs/request-for-change/rfc-crew-enterprise-edition.md`` decision D3: in an
enterprise build a scope the remote policy does not name is locked to its
minimal set, or off when it has none. The remote policy may widen past it; a
profile only narrows. The ``oss`` edition is unchanged.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from kiro_crew.platform import governance
from kiro_crew.platform.governance import (
    _PERMIT_NOT_GOVERNED,
    _SCOPE_ALIASES,
    SCOPE_CATALOG,
    _query_level,
    gate_decision,
    parse_policy,
    parse_profile,
    resolve,
)
from kiro_crew.platform.governance_parts.baseline import BASELINE_CONTROLS, BASELINE_UNGATED

_WORKSPACE_FILE = os.path.join("~", "workplace", "kirocrew-workspace", "notes.txt")

# scope -> (items the baseline permits, items it denies). One row per catalog scope.
_BASELINE_TABLE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "tools": ((), ("fs_read", "glob", "grep", "fs_write", "execute_bash", "web_fetch")),
    "approval_modes": (("normal", "trust", "trust_reads"), ("yolo",)),
    "mcp": (("@kirocrew-core/local_knowledge_search",), ("@other/tool", "@other")),
    "apps": ((), ("any-app",)),
    "commands": ((), ("ls", "git status")),
    "filesystem.read": ((_WORKSPACE_FILE,), ("/etc/passwd", os.path.join("~", "notes.txt"))),
    "filesystem.write": ((), (_WORKSPACE_FILE,)),
    "folders.read": ((_WORKSPACE_FILE,), ("/etc/passwd",)),
    "folders.write": ((), (_WORKSPACE_FILE,)),
    "network.egress": ((), ("example.com",)),
    "channels": ((), ("slack",)),
    "approval_mode": (("interactive",), ()),
    "sandbox.min_level": (("strict",), ()),
    "yolo_duration": ((), ("permanent", "until_shutdown", "1h")),
    "agent_backend": (("kiro",), ("claude", "kas")),
    "capabilities.publish": (("", "destinations:default"), ("destinations:s3",)),
}


def _row(scope: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if scope in _BASELINE_TABLE:
        return _BASELINE_TABLE[scope]
    assert scope.startswith("capabilities."), scope
    return (), ("", "agents:researcher")


def _ceiling(**controls: object) -> governance.GovernanceCeiling:
    return parse_policy({"version": 1, "boot": {}, **controls})


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def test_every_catalog_scope_has_a_baseline_entry() -> None:
    """A new catalog scope fails here until it gets a baseline row or an ungated reason."""
    canonical = set(SCOPE_CATALOG) - set(_SCOPE_ALIASES)
    assert set(BASELINE_CONTROLS).isdisjoint(BASELINE_UNGATED)
    assert set(BASELINE_CONTROLS) | set(BASELINE_UNGATED) == canonical
    assert all(BASELINE_UNGATED.values())


def test_every_catalog_scope_has_a_table_row() -> None:
    for scope in SCOPE_CATALOG:
        if not scope.startswith("capabilities.") or scope == "capabilities.publish":
            assert scope in _BASELINE_TABLE, scope


@pytest.mark.parametrize("ceiling_kind", ["none", "empty"])
@pytest.mark.parametrize("scope", sorted(SCOPE_CATALOG))
def test_enterprise_unnamed_scope_answers_the_baseline(
    enterprise: None, scope: str, ceiling_kind: str
) -> None:
    ceiling = None if ceiling_kind == "none" else _ceiling()
    permitted, denied = _row(scope)
    for item in permitted:
        assert resolve(ceiling, None, scope, item).permitted, (scope, item)
    for item in denied:
        decision = resolve(ceiling, None, scope, item)
        assert not decision.permitted, (scope, item)
        assert decision.layer == "policy"
        assert "enterprise baseline" in decision.reason


def test_enterprise_scope_with_no_baseline_row_is_denied(enterprise: None) -> None:
    assert not resolve(_ceiling(), None, "made.up", "x").permitted


def test_enterprise_policy_naming_a_scope_answers_and_may_widen(enterprise: None) -> None:
    ceiling = _ceiling(
        tools={"mode": "allow", "allow": ["fs_write"]},
        capabilities={"browse": {"enabled": True}},
        commands={"mode": "deny", "deny": ["rm *"]},
    )
    assert resolve(ceiling, None, "tools", "fs_write").permitted
    assert not resolve(ceiling, None, "tools", "fs_read").permitted
    assert resolve(ceiling, None, "capabilities.browse", "").permitted
    assert resolve(ceiling, None, "commands", "ls").permitted
    assert not resolve(ceiling, None, "commands", "rm -rf x").permitted
    # A scope the policy leaves out still answers the baseline.
    assert not resolve(ceiling, None, "network.egress", "example.com").permitted


@pytest.mark.parametrize("scope", ["filesystem.read", "folders.read"])
def test_enterprise_alias_query_finds_the_policy_rule(enterprise: None, scope: str) -> None:
    """A ``folders.*`` query answers from the ``filesystem.*`` rule it was stored under."""
    # Built with the host's own separators: the path matcher absolutizes the item.
    root = os.path.abspath(os.path.join(os.sep, "project"))
    ceiling = _ceiling(filesystem={"read": {"mode": "allow", "allow": [os.path.join(root, "**")]}})
    assert resolve(ceiling, None, scope, os.path.join(root, "notes.txt")).permitted
    assert not resolve(ceiling, None, scope, "/etc/passwd").permitted


def test_enterprise_profile_not_naming_a_scope_keeps_the_remote_widening(
    enterprise: None,
) -> None:
    ceiling = _ceiling(tools={"mode": "allow", "allow": ["fs_write"]})
    profile = parse_profile({"name": "narrow", "commands": {"mode": "deny", "deny": ["rm *"]}})
    assert resolve(ceiling, profile, "tools", "fs_write").permitted


def test_enterprise_alias_group_keeps_the_remote_grant_under_a_profile(enterprise: None) -> None:
    """The per-layer alias pass asks the profile alone, so no baseline is added there."""
    ceiling = _ceiling(
        tools={"mode": "deny", "deny": []},
        commands={"mode": "deny", "deny": []},
    )
    profile = parse_profile({"name": "narrow", "mcp": {"mode": "deny", "deny": ["@x"]}})
    groups = (("shell", "execute_bash"),)
    assert gate_decision(ceiling, profile, "shell", alias_groups=groups).permitted
    assert not gate_decision(_ceiling(), profile, "shell", alias_groups=groups).permitted


def test_enterprise_may_skip_gate_asked_about_a_profile_adds_no_baseline(
    enterprise: None,
) -> None:
    """``governance_profiles`` asks ``may_skip_gate`` about one profile in the ceiling slot."""
    profile = parse_profile({"name": "narrow", "mcp": {"mode": "deny", "deny": ["@x"]}})
    assert governance.may_skip_gate("web_fetch", profile) is True  # type: ignore[arg-type]
    assert governance.may_skip_gate("@x", profile) is False  # type: ignore[arg-type]


def test_enterprise_profile_narrows_the_baseline(enterprise: None) -> None:
    ceiling = _ceiling(tools={"mode": "allow", "allow": ["fs_read", "grep"]})
    profile = parse_profile({"name": "narrow", "tools": {"mode": "deny", "deny": ["grep"]}})
    assert not resolve(ceiling, profile, "tools", "grep").permitted
    assert resolve(ceiling, profile, "tools", "fs_read").permitted
    profile = parse_profile({"name": "narrow", "agent_backend": {"mode": "deny", "deny": ["kiro"]}})
    assert not resolve(_ceiling(), profile, "agent_backend", "kiro").permitted


def test_enterprise_gate_refuses_a_shell_command_with_an_empty_policy(enterprise: None) -> None:
    assert not gate_decision(_ceiling(), None, "Running: ls").permitted


@pytest.mark.parametrize("ceiling_kind", ["none", "empty"])
@pytest.mark.parametrize("ref", ["web_fetch", "web_search", "use_subagent", "@other", "@other/x"])
def test_enterprise_auto_approve_cannot_skip_the_gate_for_an_unnamed_scope(
    enterprise: None, ref: str, ceiling_kind: str
) -> None:
    ceiling = None if ceiling_kind == "none" else _ceiling()
    assert governance.may_skip_gate(ref, ceiling) is False


@pytest.mark.parametrize("ref", ["web_fetch", "web_search", "use_subagent", "@other"])
def test_oss_auto_approve_is_unchanged(ref: str) -> None:
    assert governance.may_skip_gate(ref, None) is True
    assert governance.may_skip_gate(ref, _ceiling()) is True


@pytest.mark.parametrize("scope", sorted(SCOPE_CATALOG))
def test_oss_unnamed_scope_is_not_governed(scope: str) -> None:
    assert _query_level(None, scope, "x") is _PERMIT_NOT_GOVERNED
    for ceiling in (None, _ceiling()):
        decision = resolve(ceiling, None, scope, "x")
        assert decision.permitted
        assert decision.layer == "default"


# ──────────────────────────────────────────────────────────────────────────
# The tier fold: a local tier narrows the baseline, never widens it
# ──────────────────────────────────────────────────────────────────────────
def _tier(tier: str, **controls: object) -> governance.GovernanceCeiling:
    return dataclasses.replace(_ceiling(**controls), tier=tier)


_WS = os.path.abspath(os.path.join(os.sep, "ws"))


def test_enterprise_local_curl_deny_stays_denied(enterprise: None) -> None:
    central = _tier(governance.TIER_CENTRAL, commands={"mode": "deny", "deny": []})
    home = _tier(governance.TIER_HOME, commands={"mode": "deny", "deny": ["curl *"]})
    composed = governance.compose_tier_ladder(central, home)
    assert not resolve(composed, None, "commands", "curl https://example.com").permitted
    assert resolve(composed, None, "commands", "ls").permitted


def test_enterprise_local_write_allowlist_still_narrows_writes(enterprise: None) -> None:
    central = _tier(
        governance.TIER_CENTRAL,
        filesystem={"write": {"mode": "allow", "allow": [os.path.join(_WS, "**")]}},
    )
    home = _tier(
        governance.TIER_HOME,
        filesystem={"write": {"mode": "allow", "allow": [os.path.join(_WS, "sub", "**")]}},
    )
    composed = governance.compose_tier_ladder(central, home)
    assert resolve(composed, None, "filesystem.write", os.path.join(_WS, "sub", "a")).permitted
    assert not resolve(composed, None, "filesystem.write", os.path.join(_WS, "b")).permitted


@pytest.mark.parametrize("local_tier", ["env", "home"])
def test_enterprise_local_tier_cannot_permit_what_the_baseline_denies(
    enterprise: None, local_tier: str
) -> None:
    central = _tier(governance.TIER_CENTRAL)
    local = _tier(
        local_tier,
        capabilities={"browse": {"enabled": True}},
        network={"egress": {"mode": "deny", "deny": []}},
        tools={"mode": "allow", "allow": ["fs_read", "fs_write"]},
    )
    composed = governance.compose_tier_ladder(central, local)
    assert not resolve(composed, None, "capabilities.browse", "").permitted
    assert not resolve(composed, None, "network.egress", "example.com").permitted
    assert not resolve(composed, None, "tools", "fs_write").permitted
    assert not resolve(composed, None, "tools", "fs_read").permitted
    # Named by the authority, the scope answers from the remote policy.
    central = _tier(governance.TIER_CENTRAL, capabilities={"browse": {"enabled": True}})
    assert resolve(
        governance.compose_tier_ladder(central, local), None, "capabilities.browse", ""
    ).permitted


def test_oss_tier_fold_is_unchanged() -> None:
    central = _tier(governance.TIER_CENTRAL)
    home = _tier(governance.TIER_HOME, capabilities={"browse": {"enabled": False}})
    assert governance.compose_tier_ladder(central) is central
    composed = governance.compose_tier_ladder(central, home)
    assert set(composed.controls) == {"capabilities.browse"}
    assert resolve(composed, None, "network.egress", "example.com").permitted


@pytest.mark.parametrize("local_tier", ["env", "bundled", "home"])
def test_enterprise_local_authority_with_no_central_only_narrows_the_baseline(
    enterprise: None, local_tier: str
) -> None:
    """With no central document the top tier is local, so it may not replace the baseline."""
    local = _tier(
        local_tier,
        network={"egress": {"mode": "deny", "deny": []}},
        commands={"mode": "deny", "deny": []},
        capabilities={"browse": {"enabled": True}},
        agent_backend={"mode": "deny", "deny": ["kiro"]},
    )
    for composed in (
        governance.compose_tier_ladder(None, local),
        governance.compose_tier_ladder(local, _tier(governance.TIER_HOME)),
    ):
        assert not resolve(composed, None, "network.egress", "example.com").permitted
        assert not resolve(composed, None, "commands", "ls").permitted
        assert not resolve(composed, None, "capabilities.browse", "").permitted
        # Narrowing still holds: the baseline's ``kiro`` is denied by the local tier.
        assert not resolve(composed, None, "agent_backend", "kiro").permitted
        assert resolve(composed, None, "mcp", "@kirocrew-core/x").permitted
