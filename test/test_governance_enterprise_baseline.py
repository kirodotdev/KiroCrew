"""The enterprise built-in baseline: an unnamed scope answers its minimal set.

RFC ``docs/request-for-change/rfc-crew-enterprise-edition.md`` decision D3: in an
enterprise build a scope the remote policy does not name is locked to its
minimal set, or off when it has none. The remote policy may widen past it; a
profile only narrows. The open controls of D3 keep Crew's
own default when the policy is silent, and a policy that names one still wins.
The ``oss`` edition is unchanged.
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

# Built with the host's own separators: the path matcher absolutizes the item.
_ETC = os.path.abspath(os.path.join(os.sep, "etc"))
_ETC_FILE = os.path.join(_ETC, "passwd")
_ETC_GLOB = os.path.join(_ETC, "**")

# RFC D3: these keep Crew's own default when the remote
# policy does not name them. ``folders.*`` are aliases of ``filesystem.*``.
_OPEN_SCOPES: dict[str, str] = {
    "mcp": "@other/tool",
    "tools": "fs_write",
    "commands": "ls",
    "filesystem.read": _ETC_FILE,
    "filesystem.write": _WORKSPACE_FILE,
    "folders.read": _ETC_FILE,
    "folders.write": _WORKSPACE_FILE,
    "capabilities.spawn": "",
    "capabilities.cron": "",
    "apps": "any-app",
    # Local-only, low-risk capabilities.
    "capabilities.memory_writes": "",
    "capabilities.theme_persona": "",
    "capabilities.milestones": "",
    "capabilities.decisions_local": "",
}

# scope -> (items the baseline permits, items it denies). One row per catalog scope.
_BASELINE_TABLE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "approval_modes": (("normal", "trust", "trust_reads"), ("yolo",)),
    "app_versions": ((), ("any-app@1.0.0",)),
    "network.egress": ((), ("example.com",)),
    "channels": ((), ("slack",)),
    "approval_mode": (("interactive",), ()),
    "sandbox.min_level": (("strict",), ()),
    "yolo_duration": ((), ("permanent", "until_shutdown", "1h")),
    "agent_backend": (("kas",), ("claude", "kiro")),
    "webhooks": ((), ("agent", "ops-mission-control")),
    "capabilities.publish": (
        ("", "destinations:default"),
        ("destinations:s3", "regions:us-east-1", "audience:private", "accounts:123456789012"),
    ),
    "bind_address": (("127.0.0.1",), ("0.0.0.0", "::", "10.0.0.5")),
    "dashboard_origins": ((), ("https://crew.example.com", "http://localhost:3000")),
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
    assert set(_OPEN_SCOPES).isdisjoint(_BASELINE_TABLE)
    for scope in SCOPE_CATALOG:
        if scope in _OPEN_SCOPES:
            continue
        if not scope.startswith("capabilities.") or scope == "capabilities.publish":
            assert scope in _BASELINE_TABLE, scope


def test_open_scopes_are_ungated_and_kept_off_scopes_are_not() -> None:
    """The owner's split: the open scopes keep Crew's default, the rest stay off."""
    canonical_open = {_SCOPE_ALIASES.get(s, s) for s in _OPEN_SCOPES}
    assert canonical_open <= set(BASELINE_UNGATED)
    for kept_off in (
        "app_versions",
        "capabilities.extension_install",
        "channels",
        "approval_modes",
        "yolo_duration",
        # Kept off by the owner (D3).
        "capabilities.app_dev_mode",
        # ``capabilities.decisions_local`` only narrows this row, so it stays inert.
        "capabilities.decisions",
    ):
        assert kept_off in BASELINE_CONTROLS, kept_off


@pytest.mark.parametrize("ceiling_kind", ["none", "empty"])
@pytest.mark.parametrize("scope", sorted(set(SCOPE_CATALOG) - set(_OPEN_SCOPES)))
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


@pytest.mark.parametrize("ceiling_kind", ["none", "empty"])
@pytest.mark.parametrize("scope", sorted(_OPEN_SCOPES))
def test_enterprise_unnamed_open_scope_keeps_crews_default(
    enterprise: None, scope: str, ceiling_kind: str
) -> None:
    """Policy silent: the open scope answers as in ``oss``, ungoverned."""
    ceiling = None if ceiling_kind == "none" else _ceiling()
    decision = resolve(ceiling, None, scope, _OPEN_SCOPES[scope])
    assert decision.permitted, scope
    assert decision.layer == "default"


_OPEN_DENIES: dict[str, dict[str, object]] = {
    "mcp": {"mcp": {"mode": "deny", "deny": ["@other"]}},
    "tools": {"tools": {"mode": "deny", "deny": ["fs_write"]}},
    "commands": {"commands": {"mode": "allow", "allow": []}},
    "filesystem.read": {"filesystem": {"read": {"mode": "deny", "deny": [_ETC_GLOB]}}},
    "filesystem.write": {"filesystem": {"write": {"mode": "allow", "allow": []}}},
    "folders.read": {"filesystem": {"read": {"mode": "deny", "deny": [_ETC_GLOB]}}},
    "folders.write": {"filesystem": {"write": {"mode": "allow", "allow": []}}},
    "capabilities.spawn": {"capabilities": {"spawn": {"enabled": False}}},
    "capabilities.cron": {"capabilities": {"cron": {"enabled": False}}},
    "apps": {"apps": {"mode": "deny", "deny": ["any-app"]}},
    "capabilities.memory_writes": {"capabilities": {"memory_writes": {"enabled": False}}},
    "capabilities.theme_persona": {"capabilities": {"theme_persona": {"enabled": False}}},
    "capabilities.milestones": {"capabilities": {"milestones": {"enabled": False}}},
    "capabilities.decisions_local": {"capabilities": {"decisions_local": {"enabled": False}}},
}


@pytest.mark.parametrize("scope", sorted(_OPEN_SCOPES))
def test_enterprise_policy_deny_still_closes_an_open_scope(enterprise: None, scope: str) -> None:
    decision = resolve(_ceiling(**_OPEN_DENIES[scope]), None, scope, _OPEN_SCOPES[scope])
    assert not decision.permitted, scope
    assert decision.layer == "policy"


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
    # ``commands`` and ``tools`` are open: an empty policy keeps Crew's default,
    # and a policy that turns them off still refuses.
    assert gate_decision(_ceiling(), profile, "shell", alias_groups=groups).permitted
    closed = _ceiling(tools={"mode": "allow", "allow": []}, commands={"mode": "allow", "allow": []})
    assert not gate_decision(closed, profile, "shell", alias_groups=groups).permitted


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


def test_enterprise_gate_leaves_a_shell_command_to_crews_default(enterprise: None) -> None:
    """``commands`` is open: the gate passes it on to the approval prompt."""
    assert gate_decision(_ceiling(), None, "Running: ls").permitted
    closed = _ceiling(commands={"mode": "allow", "allow": []})
    assert not gate_decision(closed, None, "Running: ls").permitted


@pytest.mark.parametrize("ceiling_kind", ["none", "empty"])
@pytest.mark.parametrize("ref", ["web_fetch", "web_search", "@other", "@other/x"])
def test_enterprise_auto_approve_cannot_skip_the_gate_for_an_unnamed_scope(
    enterprise: None, ref: str, ceiling_kind: str
) -> None:
    ceiling = None if ceiling_kind == "none" else _ceiling()
    assert governance.may_skip_gate(ref, ceiling) is False


@pytest.mark.parametrize("ceiling_kind", ["none", "empty"])
def test_enterprise_auto_approve_skips_the_gate_for_an_open_scope(
    enterprise: None, ceiling_kind: str
) -> None:
    """``capabilities.spawn`` is open, so a sub-agent call skips as in ``oss``."""
    ceiling = None if ceiling_kind == "none" else _ceiling()
    assert governance.may_skip_gate("use_subagent", ceiling) is True
    assert (
        governance.may_skip_gate(
            "use_subagent", _ceiling(capabilities={"spawn": {"enabled": False}})
        )
        is False
    )


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


def test_enterprise_local_tier_narrows_an_open_scope(enterprise: None) -> None:
    """With the central policy silent on ``commands``, a local deny still narrows it."""
    central = _tier(governance.TIER_CENTRAL)
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
        agent_backend={"mode": "allow", "allow": ["kas", "kiro", "claude"]},
    )
    composed = governance.compose_tier_ladder(central, local)
    assert not resolve(composed, None, "capabilities.browse", "").permitted
    assert not resolve(composed, None, "network.egress", "example.com").permitted
    assert not resolve(composed, None, "agent_backend", "claude").permitted
    # Kiro CLI is outside the enterprise baseline (RFC D6), so a local tier
    # cannot add it either.
    assert not resolve(composed, None, "agent_backend", "kiro").permitted
    # In the baseline and in the local allow-set, so both levels permit it.
    assert resolve(composed, None, "agent_backend", "kas").permitted
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
        # ``commands`` is open, so the local tier's own answer stands.
        assert resolve(composed, None, "commands", "ls").permitted
        assert not resolve(composed, None, "capabilities.browse", "").permitted
        # Narrowing still holds: the baseline's ``kiro`` is denied by the local tier.
        assert not resolve(composed, None, "agent_backend", "kiro").permitted
        assert resolve(composed, None, "mcp", "@kirocrew-core/x").permitted


@pytest.mark.parametrize("doc", [None, {}], ids=["no-policy", "empty"])
def test_enterprise_process_switch_keeps_an_open_scope(
    enterprise: None, monkeypatch: pytest.MonkeyPatch, doc: "dict[str, object] | None"
) -> None:
    """``policy_permits`` (milestones) lets an open scope's default permit stand."""
    from types import SimpleNamespace

    from kiro_crew import edition_locks

    ctx = SimpleNamespace(governance=None if doc is None else _ceiling(**doc))
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: ctx)
    assert edition_locks.policy_permits("capabilities.milestones")
    # A scope the baseline holds still needs a policy permit.
    assert not edition_locks.policy_permits("capabilities.computer_use")
    assert not edition_locks.policy_permits("capabilities.app_dev_mode")
    off = SimpleNamespace(governance=_ceiling(capabilities={"milestones": {"enabled": False}}))
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: off)
    assert not edition_locks.policy_permits("capabilities.milestones")
