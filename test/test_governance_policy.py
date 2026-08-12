"""Phase 1 + Phase 4 — governance archetypes, loader, and the resolution engine.

Covers:
* the four archetypes (ScopedRuleset / OrdinalControl / CapabilityGate / ScopedMap)
  and their single composition algebra each;
* ``load_security_policy`` precedence + fail-closed behavior (mirrors admission);
* the ``resolve`` evaluator truth table + the E1–E13 conformance vectors;
* the **extensibility / decoupling acceptance criterion**: a synthetic scope is
  registered and resolved end-to-end with ZERO evaluator edits.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    MODE_ALLOW,
    MODE_DENY,
    POSTURE_SEP,
    SCOPE_CATALOG,
    SIGNATURE_UNCHECKED,
    SIGNATURE_UNSIGNED,
    SIGNATURE_UNVERIFIED,
    SIGNATURE_VERIFIED,
    Bind,
    CapabilityGate,
    GovernanceCeiling,
    OrdinalControl,
    Profile,
    ScopedMap,
    ScopedRuleset,
    ScopeSpec,
    assert_governance_floor,
    assert_policy_signature_satisfied,
    compose_profiles,
    deny_all_profile,
    load_security_policy,
    mcp_title_to_ref,
    parse_policy,
    parse_profile,
    policy_signing_payload,
    register_matcher,
    register_scope,
    resolve,
    resolve_ordinal,
)


# ── A minimal, valid policy body reused across tests ──
def _policy_body(**overrides) -> dict:
    body = {
        "version": 1,
        "boot": {"fail_closed": True},
    }
    body.update(overrides)
    return body


# ──────────────────────────────────────────────────────────────────────────
# Archetype 1 — ScopedRuleset (Rule 1)
# ──────────────────────────────────────────────────────────────────────────
class TestScopedRuleset:
    def test_allow_mode_permits_only_listed(self):
        r = ScopedRuleset(mode=MODE_ALLOW, allow=("read", "grep"))
        assert r.permits("read").permitted
        assert r.permits("grep").permitted
        assert not r.permits("execute_bash").permitted

    def test_empty_allow_is_deny_all_not_unconstrained(self):
        r = ScopedRuleset(mode=MODE_ALLOW, allow=())
        assert not r.permits("anything").permitted

    def test_deny_mode_permits_everything_except_listed(self):
        r = ScopedRuleset(mode=MODE_DENY, deny=("rm -rf*",), matcher="command")
        assert r.permits("ls -la").permitted
        assert not r.permits("rm -rf /").permitted

    def test_rule1_allow_beats_deny(self):
        # mode=allow ignores deny entirely (Rule 1).
        r = ScopedRuleset(mode=MODE_ALLOW, allow=("read",), deny=("read",))
        assert r.permits("read").permitted

    def test_invalid_mode_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            ScopedRuleset.from_dict({"mode": "maybe"})

    def test_identifier_matcher_is_case_insensitive(self):
        r = ScopedRuleset(mode=MODE_ALLOW, allow=("Researcher",), matcher="identifier")
        assert r.permits("researcher").permitted

    def test_command_matcher_is_case_sensitive(self):
        r = ScopedRuleset(mode=MODE_DENY, deny=("GIT push*",), matcher="command")
        # case-sensitive: lowercase 'git' is NOT denied by an uppercase pattern.
        assert r.permits("git push origin").permitted
        assert not r.permits("GIT push origin").permitted

    def test_deny_compose_is_union(self):
        a = ScopedRuleset(mode=MODE_DENY, deny=("x",), matcher="command")
        b = ScopedRuleset(mode=MODE_DENY, deny=("y",), matcher="command")
        composed = a.compose(b)
        assert isinstance(composed, ScopedRuleset)
        assert not composed.permits("x").permitted
        assert not composed.permits("y").permitted
        assert composed.permits("z").permitted

    def test_allow_compose_is_intersection(self):
        ceiling = ScopedRuleset(mode=MODE_ALLOW, allow=("read", "grep", "code"))
        profile = ScopedRuleset(mode=MODE_ALLOW, allow=("read", "glob"))
        composed = ceiling.compose(profile)
        # only items both permit survive (just "read").
        assert composed.permits("read").permitted
        assert not composed.permits("grep").permitted  # ceiling yes, profile no
        assert not composed.permits("glob").permitted  # profile yes, ceiling no

    def test_allow_intersect_deny(self):
        # ceiling allow ∩ profile deny: permit iff in ceiling allow AND not denied.
        ceiling = ScopedRuleset(mode=MODE_ALLOW, allow=("read", "grep"))
        profile = ScopedRuleset(mode=MODE_DENY, deny=("grep",))
        composed = ceiling.compose(profile)
        assert composed.permits("read").permitted
        assert not composed.permits("grep").permitted
        assert not composed.permits("code").permitted  # not in ceiling allow


class TestMcpMatcher:
    def test_server_grant_covers_all_tools(self):
        r = ScopedRuleset(mode=MODE_DENY, deny=("@kirocrew-cron",), matcher="mcp")
        assert not r.permits("@kirocrew-cron/cron_add").permitted
        assert not r.permits("@kirocrew-cron").permitted
        assert r.permits("@kirocrew-core/spawn_run").permitted

    def test_tool_level_deny_is_specific(self):
        r = ScopedRuleset(mode=MODE_DENY, deny=("@kirocrew-cron/cron_remove_all",), matcher="mcp")
        assert not r.permits("@kirocrew-cron/cron_remove_all").permitted
        assert r.permits("@kirocrew-cron/cron_add").permitted

    def test_title_to_ref_conversion(self):
        assert mcp_title_to_ref("mcp__kirocrew-cron__cron_add") == "@kirocrew-cron/cron_add"
        assert mcp_title_to_ref("mcp__builder-mcp") == "@builder-mcp"
        assert mcp_title_to_ref("execute_bash") == "execute_bash"

    def test_title_to_ref_server_name_with_double_underscore(self):
        # A server name containing '__' (e.g. npm__playwright_mcp) must split on
        # the LAST '__' so the whole server name is preserved — else a
        # server-level deny never matches and the tool is wrongly permitted.
        assert (
            mcp_title_to_ref("mcp__npm__playwright_mcp__browser_click")
            == "@npm__playwright_mcp/browser_click"
        )

    def test_double_underscore_server_deny_matches(self):
        r = ScopedRuleset(mode=MODE_DENY, deny=("@npm__playwright_mcp",), matcher="mcp")
        ref = mcp_title_to_ref("mcp__npm__playwright_mcp__browser_click")
        assert not r.permits(ref).permitted


# ──────────────────────────────────────────────────────────────────────────
# Archetype 2 — OrdinalControl
# ──────────────────────────────────────────────────────────────────────────
class TestOrdinalControl:
    def test_rank_orders_by_strictness(self):
        off = OrdinalControl("sandbox", "off")
        strict = OrdinalControl("sandbox", "strict")
        assert strict.rank() > off.rank()

    def test_compose_takes_stricter(self):
        cc = OrdinalControl("sandbox", "cc")
        standard = OrdinalControl("sandbox", "standard")
        assert cc.compose(standard).value == "cc"
        assert standard.compose(cc).value == "cc"

    def test_unknown_scale_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            OrdinalControl("nonexistent", "x")

    def test_value_not_in_scale_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            OrdinalControl("sandbox", "ultra")

    def test_at_least_as_strict(self):
        assert OrdinalControl("approval", "interactive").is_at_least_as_strict_as(
            OrdinalControl("approval", "auto")
        )
        assert not OrdinalControl("approval", "yolo").is_at_least_as_strict_as(
            OrdinalControl("approval", "interactive")
        )

    def test_scale_is_not_document_overridable(self):
        # The enforcer owns the order; a value string cannot reorder it.
        assert OrdinalControl("approval", "yolo").rank() < OrdinalControl("approval", "auto").rank()
        assert (
            OrdinalControl("approval", "auto").rank()
            < OrdinalControl("approval", "interactive").rank()
        )


# ──────────────────────────────────────────────────────────────────────────
# Archetype 3 — CapabilityGate
# ──────────────────────────────────────────────────────────────────────────
class TestCapabilityGate:
    def test_enabled_composes_by_and(self):
        on = CapabilityGate(enabled=True)
        off = CapabilityGate(enabled=False)
        assert not on.compose(off).enabled
        assert not off.compose(on).enabled
        assert on.compose(on).enabled

    def test_disabled_denies_scope_item(self):
        g = CapabilityGate(enabled=False, scopes={"agents": ScopedRuleset(MODE_ALLOW, ("x",))})
        assert not g.permits_scope_item("agents", "x").permitted

    def test_scope_item_within_enabled_gate(self):
        g = CapabilityGate(
            enabled=True, scopes={"agents": ScopedRuleset(MODE_ALLOW, ("researcher",))}
        )
        assert g.permits_scope_item("agents", "researcher").permitted
        assert not g.permits_scope_item("agents", "deployer").permitted

    def test_unconstrained_scope_when_enabled(self):
        g = CapabilityGate(enabled=True)
        assert g.permits_scope_item("agents", "anything").permitted

    def test_from_dict_default_enabled(self):
        # absence of "enabled" uses the registered default.
        g = CapabilityGate.from_dict({}, default_enabled=True)
        assert g.enabled
        g2 = CapabilityGate.from_dict({}, default_enabled=False)
        assert not g2.enabled

    def test_scopes_compose_independently(self):
        a = CapabilityGate(
            enabled=True,
            scopes={
                "agents": ScopedRuleset(MODE_ALLOW, ("r", "d")),
                "cwd_roots": ScopedRuleset(MODE_ALLOW, ("/a", "/b")),
            },
        )
        b = CapabilityGate(enabled=True, scopes={"agents": ScopedRuleset(MODE_ALLOW, ("r",))})
        composed = a.compose(b)
        assert composed.permits_scope_item("agents", "r").permitted
        assert not composed.permits_scope_item("agents", "d").permitted
        # cwd_roots present only on a → carries through.
        assert composed.permits_scope_item("cwd_roots", "/a").permitted


# ──────────────────────────────────────────────────────────────────────────
# Archetype 4 — ScopedMap
# ──────────────────────────────────────────────────────────────────────────
class TestScopedMap:
    def test_members_allowlist(self):
        m = ScopedMap(members=ScopedRuleset(MODE_ALLOW, ("slack",)))
        assert m.permits_member("slack").permitted
        assert not m.permits_member("discord").permitted

    def test_posture_policy_only_rejected_in_profile(self):
        body = {
            "members": {"mode": "allow", "allow": ["slack"]},
            "posture": {"slack": {"allowed_enterprise_ids": {"mode": "allow", "allow": ["E1"]}}},
        }
        # allow_posture=True (policy) parses; False (profile) rejects.
        ScopedMap.from_dict(body, allow_posture=True)
        with pytest.raises(PlatformCompositionError):
            ScopedMap.from_dict(body, allow_posture=False)

    def test_posture_permits(self):
        m = ScopedMap.from_dict(
            {
                "members": {"mode": "allow", "allow": ["slack"]},
                "posture": {
                    "slack": {"allowed_enterprise_ids": {"mode": "allow", "allow": ["E0123ABCD"]}}
                },
            },
            allow_posture=True,
        )
        assert m.posture_permits("slack", "allowed_enterprise_ids", "E0123ABCD").permitted
        assert not m.posture_permits("slack", "allowed_enterprise_ids", "E9999").permitted

    def test_members_intersect_posture_from_ceiling(self):
        ceiling = ScopedMap.from_dict(
            {
                "members": {"mode": "allow", "allow": ["slack", "discord"]},
                "posture": {"slack": {"allowed_team_ids": {"mode": "allow", "allow": ["T1"]}}},
            },
            allow_posture=True,
        )
        profile = ScopedMap.from_dict(
            {"members": {"mode": "allow", "allow": ["slack"]}}, allow_posture=False
        )
        composed = ceiling.compose(profile)
        assert composed.permits_member("slack").permitted
        assert not composed.permits_member("discord").permitted  # profile narrowed
        # posture is policy-only → preserved from ceiling.
        assert composed.posture_permits("slack", "allowed_team_ids", "T1").permitted


# ──────────────────────────────────────────────────────────────────────────
# Loader — precedence + fail-closed (mirrors admission)
# ──────────────────────────────────────────────────────────────────────────
class TestLoader:
    def test_absent_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        _nope = tmp_path / "nope.json"
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: _nope
        )
        assert load_security_policy() is None

    def test_env_path_wins(self, monkeypatch, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(approval_mode="interactive")))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.version == 1

    def test_unreadable_env_fails_closed(self, monkeypatch, tmp_path):
        bad = tmp_path / "policy.json"
        bad.write_text("{ this is not json")
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(bad))
        with pytest.raises(PlatformCompositionError):
            load_security_policy()

    def test_missing_env_path_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(tmp_path / "gone.json"))
        with pytest.raises(PlatformCompositionError):
            load_security_policy()

    def test_home_path_used_when_no_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        home = tmp_path / "security_policy.json"
        home.write_text(json.dumps(_policy_body()))
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: home
        )
        ceiling = load_security_policy()
        assert ceiling is not None

    def test_bundled_loader_precedence(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        _nope = tmp_path / "nope.json"
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: _nope
        )
        called = {}

        def bundled():
            called["yes"] = True
            return _policy_body(commands={"mode": "deny", "deny": ["git push*"]})

        ceiling = load_security_policy(bundled_loader=bundled)
        assert called.get("yes")
        assert ceiling is not None
        assert "commands" in ceiling.controls

    def test_env_beats_bundled(self, monkeypatch, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        # bundled_loader must NOT be consulted when env wins.
        ceiling = load_security_policy(bundled_loader=lambda: pytest.fail("should not call"))
        assert ceiling is not None

    def test_wrong_version_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy({"version": 99, "boot": {"fail_closed": True}})

    def test_missing_boot_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy({"version": 1})

    def test_unknown_governed_key_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy_body(bogus_scope={"mode": "allow"}))


# ──────────────────────────────────────────────────────────────────────────
# Policy / Profile parsing
# ──────────────────────────────────────────────────────────────────────────
class TestParsing:
    def test_full_policy_parses(self):
        body = _policy_body(
            approval_mode="interactive",
            sandbox={"min_level": "cc", "require_isolation": True},
            filesystem={
                "read": {"mode": "deny", "deny": ["~/.ssh/**"]},
                "write": {"mode": "allow", "allow": ["~/workspace/**"]},
            },
            commands={"mode": "deny", "deny": ["git push *"]},
            apps={"mode": "allow", "allow": ["auto-research"]},
            network={"egress": {"mode": "allow", "allow": ["*.amazonaws.com"]}},
            channels={
                "members": {"mode": "allow", "allow": ["slack"]},
                "posture": {
                    "slack": {"allowed_enterprise_ids": {"mode": "allow", "allow": ["E0123ABCD"]}}
                },
            },
            capabilities={
                "spawn": {
                    "enabled": True,
                    "scopes": {"agents": {"mode": "allow", "allow": ["researcher"]}},
                },
                "memory_writes": {"enabled": False},
            },
            identity={"issuer": "fleet-control", "signature": "sig"},
        )
        ceiling = parse_policy(body)
        assert isinstance(ceiling.get("approval_mode"), OrdinalControl)
        assert isinstance(ceiling.get("sandbox.min_level"), OrdinalControl)
        assert isinstance(ceiling.get("filesystem.read"), ScopedRuleset)
        assert isinstance(ceiling.get("channels"), ScopedMap)
        assert isinstance(ceiling.get("capabilities.spawn"), CapabilityGate)
        assert ceiling.identity_issuer == "fleet-control"

    def test_profile_parses_with_bind(self):
        body = {
            "name": "app-deploy-web",
            "bind": {"type": "app", "id": "deploy-web"},
            "tools": {"mode": "allow", "allow": ["read", "grep"]},
            "capabilities": {"spawn": {"enabled": False}},
        }
        profile = parse_profile(body)
        assert profile.name == "app-deploy-web"
        assert profile.bind == Bind(type="app", id="deploy-web")
        assert isinstance(profile.get("tools"), ScopedRuleset)

    def test_profile_rejects_channel_posture(self):
        body = {
            "name": "p",
            "channels": {
                "members": {"mode": "allow", "allow": ["slack"]},
                "posture": {
                    "slack": {"allowed_enterprise_ids": {"mode": "allow", "allow": ["E1"]}}
                },
            },
        }
        with pytest.raises(PlatformCompositionError):
            parse_profile(body)

    def test_profile_requires_name(self):
        with pytest.raises(PlatformCompositionError):
            parse_profile({"tools": {"mode": "allow"}})

    def test_profile_bad_bind_type_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_profile({"name": "p", "bind": {"type": "galaxy"}})


# ──────────────────────────────────────────────────────────────────────────
# Evaluator — truth table + E1..E13 conformance vectors
# ──────────────────────────────────────────────────────────────────────────
class TestResolveTruthTable:
    """The worked truth table from the spec (single ScopedRuleset item)."""

    def _ceiling(self, control) -> GovernanceCeiling:
        return (
            parse_policy(_policy_body())
            if control is None
            else GovernanceCeiling(
                version=1, boot=parse_policy(_policy_body()).boot, controls={"tools": control}
            )
        )

    def _profile(self, control) -> Profile:
        return Profile(name="p", controls={} if control is None else {"tools": control})

    def test_allow_allow_permitted(self):
        c = self._ceiling(ScopedRuleset(MODE_ALLOW, ("read",)))
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("read",)))
        assert resolve(c, p, "tools", "read").permitted

    def test_allow_deny_narrows(self):
        c = self._ceiling(ScopedRuleset(MODE_ALLOW, ("read",)))
        p = self._profile(ScopedRuleset(MODE_DENY, deny=("read",)))
        assert not resolve(c, p, "tools", "read").permitted

    def test_allow_notlisted_narrows(self):
        c = self._ceiling(ScopedRuleset(MODE_ALLOW, ("read",)))
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("grep",)))
        assert not resolve(c, p, "tools", "read").permitted

    def test_deny_allow_ceiling_wins(self):
        c = self._ceiling(ScopedRuleset(MODE_DENY, deny=("read",)))
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("read",)))
        d = resolve(c, p, "tools", "read")
        assert not d.permitted
        assert d.layer == "policy"

    def test_deny_deny(self):
        c = self._ceiling(ScopedRuleset(MODE_DENY, deny=("read",)))
        p = self._profile(ScopedRuleset(MODE_DENY, deny=("read",)))
        assert not resolve(c, p, "tools", "read").permitted

    def test_notgoverned_allow(self):
        c = self._ceiling(None)
        p = self._profile(ScopedRuleset(MODE_ALLOW, ("read",)))
        assert resolve(c, p, "tools", "read").permitted

    def test_notgoverned_notgoverned_default_allow(self):
        c = self._ceiling(None)
        p = self._profile(None)
        d = resolve(c, p, "tools", "read")
        assert d.permitted
        assert d.layer == "default"


class TestConformanceVectors:
    """E1–E13: end-to-end vectors over a representative policy + profile."""

    @pytest.fixture
    def ceiling(self):
        return parse_policy(
            _policy_body(
                approval_mode="auto",
                sandbox={"min_level": "standard"},
                commands={"mode": "deny", "deny": ["git push*", "*rm -rf /*"]},
                tools={"mode": "deny", "deny": []},
                mcp={"mode": "deny", "deny": ["@kirocrew-cron/cron_remove_all"]},
                apps={"mode": "allow", "allow": ["auto-research", "deploy-web"]},
                network={"egress": {"mode": "allow", "allow": ["*.amazonaws.com"]}},
                channels={
                    "members": {"mode": "allow", "allow": ["slack"]},
                    "posture": {
                        "slack": {"allowed_enterprise_ids": {"mode": "allow", "allow": ["E0123"]}}
                    },
                },
                capabilities={
                    "spawn": {
                        "enabled": True,
                        "scopes": {"agents": {"mode": "allow", "allow": ["researcher"]}},
                    },
                    "memory_writes": {"enabled": True},
                    "cron": {"enabled": False},
                },
            )
        )

    @pytest.fixture
    def profile(self):
        return parse_profile(
            {
                "name": "restricted",
                "bind": {"type": "app", "id": "deploy-web"},
                "tools": {"mode": "allow", "allow": ["read", "grep", "code"]},
                "approval_mode": "interactive",
                "apps": {"mode": "allow", "allow": ["deploy-web"]},
                "capabilities": {"spawn": {"enabled": False}, "cron": {"enabled": False}},
            }
        )

    def test_e1_command_denied_by_policy(self, ceiling, profile):
        assert not resolve(ceiling, profile, "commands", "git push origin main").permitted

    def test_e2_benign_command_allowed(self, ceiling, profile):
        assert resolve(ceiling, profile, "commands", "ls -la").permitted

    def test_e3_mcp_tool_deny_specific(self, ceiling, profile):
        assert not resolve(ceiling, profile, "mcp", "@kirocrew-cron/cron_remove_all").permitted
        assert resolve(ceiling, profile, "mcp", "@kirocrew-cron/cron_add").permitted

    def test_e4_app_within_policy_and_profile(self, ceiling, profile):
        assert resolve(ceiling, profile, "apps", "deploy-web").permitted
        # auto-research is in policy but profile narrows to deploy-web only.
        assert not resolve(ceiling, profile, "apps", "auto-research").permitted

    def test_e5_egress_allowlist(self, ceiling, profile):
        assert resolve(ceiling, profile, "network.egress", "api.amazonaws.com").permitted
        assert not resolve(ceiling, profile, "network.egress", "evil.example.com").permitted

    def test_e6_channel_member(self, ceiling, profile):
        assert resolve(ceiling, profile, "channels", "slack").permitted
        assert not resolve(ceiling, profile, "channels", "discord").permitted

    def test_e6b_channel_member_is_queried_per_connection(self, ceiling, profile):
        # A ``channels`` member is a CONNECTION. A policy naming the bare
        # transport keeps covering every connection on it, which is what makes
        # this vector's own ``allow: ["slack"]`` ceiling still admit the traffic.
        assert resolve(ceiling, profile, "channels", "slack/default").permitted
        assert resolve(ceiling, profile, "channels", "slack/second-workspace").permitted
        assert not resolve(ceiling, profile, "channels", "discord/default").permitted

    def test_e7_channel_posture_enterprise_id(self, ceiling, profile):
        # ``#`` separates the posture leaf from the member: a member id may itself
        # contain ``/`` (``slack/default``), so ``/`` cannot also mean "leaf".
        assert resolve(
            ceiling, profile, "channels", f"slack{POSTURE_SEP}allowed_enterprise_ids:E0123"
        ).permitted
        assert not resolve(
            ceiling, profile, "channels", f"slack{POSTURE_SEP}allowed_enterprise_ids:E9999"
        ).permitted

    def test_e7b_transport_posture_reaches_a_qualified_connection(self, ceiling, profile):
        # The posture in this vector is authored for the whole transport
        # (``posture: {"slack": …}``) while a live gate queries the qualified
        # connection — so container resolution has to find it, or the ceiling is
        # silently skipped.
        item = f"slack/default{POSTURE_SEP}allowed_enterprise_ids:"
        assert resolve(ceiling, profile, "channels", item + "E0123").permitted
        assert not resolve(ceiling, profile, "channels", item + "E9999").permitted

    def test_e8_capability_spawn_disabled_by_profile(self, ceiling, profile):
        # policy enables spawn; profile disables it → AND = disabled.
        assert not resolve(ceiling, profile, "capabilities.spawn", "researcher").permitted

    def test_e9_capability_agents_scope(self, ceiling):
        # with a profile that keeps spawn enabled, the agents scope still bounds it.
        p = parse_profile({"name": "x", "capabilities": {"spawn": {"enabled": True}}})
        assert resolve(ceiling, p, "capabilities.spawn", "agents:researcher").permitted
        assert not resolve(ceiling, p, "capabilities.spawn", "agents:deployer").permitted

    def test_e10_tools_intersection(self, ceiling, profile):
        # ceiling deny[] (allow-all) ∩ profile allow[read,grep,code].
        assert resolve(ceiling, profile, "tools", "read").permitted
        assert not resolve(ceiling, profile, "tools", "execute_bash").permitted

    def test_e11_approval_ordinal_strictest(self, ceiling, profile):
        # policy=auto, profile=interactive → effective interactive (stricter).
        eff = resolve_ordinal(ceiling, profile, "approval_mode")
        assert eff is not None and eff.value == "interactive"

    def test_e12_sandbox_ordinal_from_policy_only(self, ceiling, profile):
        # profile doesn't set sandbox → policy's standard stands.
        eff = resolve_ordinal(ceiling, profile, "sandbox.min_level")
        assert eff is not None and eff.value == "standard"

    def test_e13_cron_capability_off_both(self, ceiling, profile):
        assert not resolve(ceiling, profile, "capabilities.cron", "anything").permitted


# ──────────────────────────────────────────────────────────────────────────
# assert_governance_floor — boot-time anti-weakening
# ──────────────────────────────────────────────────────────────────────────
class TestFloor:
    def test_profile_looser_ordinal_aborts(self):
        ceiling = parse_policy(_policy_body(approval_mode="interactive"))
        profile = parse_profile({"name": "p", "approval_mode": "auto"})
        with pytest.raises(PlatformCompositionError):
            assert_governance_floor(ceiling, profile)

    def test_profile_stricter_ordinal_ok(self):
        ceiling = parse_policy(_policy_body(approval_mode="auto"))
        profile = parse_profile({"name": "p", "approval_mode": "interactive"})
        assert_governance_floor(ceiling, profile)  # no raise

    def test_none_ceiling_imposes_no_floor(self):
        profile = parse_profile({"name": "p", "approval_mode": "yolo"})
        assert_governance_floor(None, profile)  # no raise

    def test_sandbox_floor_violation(self):
        ceiling = parse_policy(_policy_body(sandbox={"min_level": "strict"}))
        profile = parse_profile({"name": "p", "sandbox": {"min_level": "off"}})
        with pytest.raises(PlatformCompositionError):
            assert_governance_floor(ceiling, profile)


# ──────────────────────────────────────────────────────────────────────────
# deny_all fallback + inheritance
# ──────────────────────────────────────────────────────────────────────────
class TestDenyAllAndInheritance:
    def test_deny_all_profile_denies_everything(self):
        p = deny_all_profile()
        assert not resolve(None, p, "tools", "read").permitted
        assert not resolve(None, p, "capabilities.spawn", "researcher").permitted
        assert not resolve(None, p, "channels", "slack").permitted

    def test_extends_narrows_monotonically(self):
        parent = parse_profile(
            {"name": "base", "tools": {"mode": "allow", "allow": ["read", "grep", "code"]}}
        )
        child = parse_profile(
            {"name": "child", "extends": "base", "tools": {"mode": "allow", "allow": ["read"]}}
        )
        merged = compose_profiles(parent, child)
        assert resolve(None, merged, "tools", "read").permitted
        assert not resolve(None, merged, "tools", "grep").permitted

    def test_extends_cannot_widen(self):
        parent = parse_profile({"name": "base", "tools": {"mode": "allow", "allow": ["read"]}})
        child = parse_profile(
            {"name": "child", "tools": {"mode": "allow", "allow": ["read", "execute_bash"]}}
        )
        merged = compose_profiles(parent, child)
        # execute_bash is in child but NOT parent → intersection drops it.
        assert not resolve(None, merged, "tools", "execute_bash").permitted


# ──────────────────────────────────────────────────────────────────────────
# THE EXTENSIBILITY / DECOUPLING ACCEPTANCE CRITERION
# ──────────────────────────────────────────────────────────────────────────
class TestExtensibility:
    """A brand-new governed scope is added and resolved with ZERO evaluator edits.

    Proves the decoupling requirement: ``resolve`` never branches on a scope
    name, so new MCP servers / channels / capabilities / domains are pure data.
    """

    def test_register_new_ruleset_scope_resolves_without_evaluator_change(self):
        # A hypothetical future "clipboard" domain — never named in the engine.
        register_scope("clipboard", ScopeSpec("ruleset", matcher="identifier"))
        try:
            ceiling = GovernanceCeiling(
                version=1,
                boot=parse_policy(_policy_body()).boot,
                controls={"clipboard": ScopedRuleset(MODE_ALLOW, ("paste",))},
            )
            assert resolve(ceiling, None, "clipboard", "paste").permitted
            assert not resolve(ceiling, None, "clipboard", "copy").permitted
        finally:
            SCOPE_CATALOG.pop("clipboard", None)

    def test_register_new_matcher(self):
        def _prefix(item: str, pattern: str) -> bool:
            return item.startswith(pattern)

        register_matcher("prefix_only", _prefix)
        r = ScopedRuleset(MODE_DENY, deny=("danger",), matcher="prefix_only")
        assert not r.permits("danger-zone").permitted
        assert r.permits("safe").permitted

    def test_new_capability_is_additive(self):
        # A new capability (e.g. voice_outbound) plugs into the same gate algebra.
        register_scope(
            "capabilities.voice_outbound", ScopeSpec("capability", capability_default=False)
        )
        try:
            ceiling = GovernanceCeiling(
                version=1,
                boot=parse_policy(_policy_body()).boot,
                controls={"capabilities.voice_outbound": CapabilityGate(enabled=True)},
            )
            assert resolve(ceiling, None, "capabilities.voice_outbound", "anything").permitted
            # profile-absence for a capability = the registered default (False here)
            # only matters at parse time; resolve with policy-on + no profile = on.
        finally:
            SCOPE_CATALOG.pop("capabilities.voice_outbound", None)

    def test_register_scope_rejects_conflicting_redefinition(self):
        with pytest.raises(ValueError):
            register_scope("tools", ScopeSpec("ordinal", ordinal_scale="approval"))

    def test_register_scope_rejects_unknown_matcher(self):
        with pytest.raises(ValueError):
            register_scope("weird", ScopeSpec("ruleset", matcher="nonexistent_matcher"))

    def test_registered_nested_scope_parses_via_parse_policy(self):
        # The extensibility contract: a newly registered DOTTED/nested family
        # must parse through the loader with NO _parse_controls edit. Authored in
        # the natural nested shape {"vault": {"read": {...}}}.
        register_scope("vault.read", ScopeSpec("ruleset", matcher="path"))
        try:
            ceiling = parse_policy(
                _policy_body(vault={"read": {"mode": "deny", "deny": ["~/.ssh/**"]}})
            )
            assert isinstance(ceiling.get("vault.read"), ScopedRuleset)
            assert not resolve(ceiling, None, "vault.read", "~/.ssh/id_rsa").permitted
        finally:
            SCOPE_CATALOG.pop("vault.read", None)

    def test_registered_flat_scope_parses_via_parse_policy(self):
        register_scope("clipboard", ScopeSpec("ruleset", matcher="identifier"))
        try:
            ceiling = parse_policy(_policy_body(clipboard={"mode": "allow", "allow": ["paste"]}))
            assert resolve(ceiling, None, "clipboard", "paste").permitted
            assert not resolve(ceiling, None, "clipboard", "copy").permitted
        finally:
            SCOPE_CATALOG.pop("clipboard", None)

    def test_unknown_nested_child_still_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy_body(filesystem={"bogus": {"mode": "allow"}}))

    def test_unknown_capability_key_aborts(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy_body(capabilities={"bogus": {"enabled": True}}))


class TestSchemaStrictness:
    """FIX-C: leaf additionalProperties:false + name regex + Rule-1 warning + posture-member."""

    def test_scopedruleset_rejects_unknown_key(self):
        with pytest.raises(PlatformCompositionError):
            ScopedRuleset.from_dict({"mode": "allow", "allowww": ["read"]})

    def test_scopedruleset_deny_typo_is_rejected_not_allow_everything(self):
        # The dangerous case: a 'deney' typo must NOT silently become an empty
        # deny list (= allow-everything). It must raise.
        with pytest.raises(PlatformCompositionError):
            ScopedRuleset.from_dict({"mode": "deny", "deney": ["secret_tool"]})

    def test_capabilitygate_rejects_unknown_key(self):
        with pytest.raises(PlatformCompositionError):
            CapabilityGate.from_dict({"enabled": True, "scopez": {}}, default_enabled=False)

    def test_scopedmap_rejects_unknown_key(self):
        with pytest.raises(PlatformCompositionError):
            ScopedMap.from_dict(
                {"members": {"mode": "allow", "allow": ["slack"]}, "postures": {}},
                allow_posture=True,
            )

    def test_allow_mode_deny_warns(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            ScopedRuleset.from_dict({"mode": "allow", "allow": ["read"], "deny": ["grep"]})
        assert any("Rule 1" in r.message or "allow beats deny" in r.message for r in caplog.records)

    def test_posture_key_must_be_admitted_member(self):
        # posture for a member not in the members allow-set is rejected.
        with pytest.raises(PlatformCompositionError):
            ScopedMap.from_dict(
                {
                    "members": {"mode": "allow", "allow": ["slack"]},
                    "posture": {"discord": {"allowed_guild_ids": {"mode": "allow", "allow": ["G"]}}},
                },
                allow_posture=True,
            )

    def test_posture_key_admitted_member_ok(self):
        m = ScopedMap.from_dict(
            {
                "members": {"mode": "allow", "allow": ["slack"]},
                "posture": {"slack": {"allowed_enterprise_ids": {"mode": "allow", "allow": ["E1"]}}},
            },
            allow_posture=True,
        )
        assert m.posture_permits("slack", "allowed_enterprise_ids", "E1").permitted

    @pytest.mark.parametrize("bad", ["Foo_Bar", "UPPER", "has spaces", "-leading", "under_score"])
    def test_profile_name_pattern_rejected(self, bad):
        with pytest.raises(PlatformCompositionError):
            parse_profile({"name": bad})

    @pytest.mark.parametrize("ok", ["app-deploy-web", "cron", "a1", "x"])
    def test_profile_name_pattern_accepted(self, ok):
        prof = parse_profile({"name": ok})
        assert prof.name == ok


# ──────────────────────────────────────────────────────────────────────────
# Policy signature verification (identity.signature) — mirrors admission
# ──────────────────────────────────────────────────────────────────────────
def _sign_policy(body: dict, secret: str) -> dict:
    """Return *body* with a valid ``identity.signature`` for its issuer."""
    signed = json.loads(json.dumps(body))  # deep copy; body may be reused
    sig = hmac.new(
        secret.encode("utf-8"), policy_signing_payload(signed), hashlib.sha256
    ).hexdigest()
    signed.setdefault("identity", {})["signature"] = sig
    return signed


def _patch_trust(monkeypatch, *, require: bool, keys: dict):
    """Point the loader's trust root at fixed settings (no admission file I/O)."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance._policy_trust_settings",
        lambda: (require, dict(keys)),
    )


def _real_trust_file(monkeypatch, tmp_path, *, require: bool, keys: dict):
    """Write a REAL admission file so both trust-root readers agree.

    ``_patch_trust`` stubs ``_policy_trust_settings``, which the loader uses for keys,
    but the enforcement gate reads the opt-in from the admission file directly. Tests
    that exercise the gate therefore need an actual file, not the stub.
    """
    adm = tmp_path / "admission_policy.json"
    adm.write_text(json.dumps({"require_policy_signature": require, "trust_keys": dict(keys)}))
    monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))


def _load_and_enforce(**kwargs):
    """Load the policy and apply the boot gate — the real two-step boot sequence.

    Load computes each tier's verdict; the gate judges the one that survived
    precedence.  Tests assert on the pair because that is what a host actually runs;
    asserting on ``load_security_policy`` alone would pin an intermediate state and
    miss the tier-precedence bug that split them apart.
    """
    ceiling = load_security_policy(**kwargs)
    assert_policy_signature_satisfied(ceiling)
    return ceiling


class TestPolicySigningPayload:
    def test_signature_field_is_excluded_but_issuer_is_covered(self):
        base = _policy_body(identity={"issuer": "fleet-control"})
        with_sig = _policy_body(identity={"issuer": "fleet-control", "signature": "deadbeef"})
        # Adding the signature must not change the payload (it cannot cover itself)…
        assert policy_signing_payload(base) == policy_signing_payload(with_sig)
        # …but the issuer IS inside it, so a validly-signed policy cannot be
        # re-labelled as issued by someone else.
        other = _policy_body(identity={"issuer": "attacker", "signature": "deadbeef"})
        assert policy_signing_payload(other) != policy_signing_payload(with_sig)

    def test_payload_is_stable_across_key_order_and_whitespace(self):
        a = {"version": 1, "boot": {"fail_closed": True}}
        b = {"boot": {"fail_closed": True}, "version": 1}
        assert policy_signing_payload(a) == policy_signing_payload(b)

    def test_payload_covers_unknown_forward_compatible_keys(self):
        # Signing the raw document (not a projection of the parsed ceiling) is what
        # makes a companion-registered or future scope tamper-evident on a build
        # that does not know the key.
        a = _policy_body()
        b = _policy_body(future_scope={"mode": "allow"})
        assert policy_signing_payload(a) != policy_signing_payload(b)

    def test_shares_admission_canonicalization(self):
        # One canonicalizer for both trust roots — a divergence here is exactly how
        # a signer and a verifier drift apart.
        from kiro_crew.platform.admission import canonical_signing_bytes

        body = {"version": 1, "boot": {"fail_closed": True}}
        assert policy_signing_payload(body) == canonical_signing_bytes(body)


class TestPolicySignatureStates:
    def test_verified_good_signature(self, monkeypatch, tmp_path):
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_VERIFIED
        assert "verified" in ceiling.signature_summary()

    def test_verified_survives_reserialization(self, monkeypatch, tmp_path):
        # The signature covers the canonical form of the parsed JSON, so an
        # operator re-indenting or reordering the file does not invalidate it.
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body, indent=4, sort_keys=True) + "\n")
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        assert load_security_policy().signature_state == SIGNATURE_VERIFIED

    def test_non_ascii_signature_is_unverified_not_a_crash(self, monkeypatch, tmp_path):
        """A non-ASCII signature must be an ordinary UNVERIFIED verdict.

        ``hmac.compare_digest`` raises TypeError on a str carrying any non-ASCII
        character, and a policy file's signature is attacker- or paste-controlled
        text (a smart-quote or NBSP is enough).  A raised TypeError is NOT a
        denial: it escapes ``load_security_policy`` as a plain exception, the boot
        handler does not treat it as fatal (it only re-raises
        ``PlatformCompositionError``), and the later ``safe_context_call``
        degrades to the no-ceiling fallback — so a tampered policy would yield an
        UNGOVERNED host even with ``require_policy_signature`` on, inverting the
        flag.  Both sides are encoded before comparison.
        """
        for signature in ("abc\u2013def", "abc\u00a0def", "\u00e9" * 64):
            body = _policy_body(identity={"issuer": "fleet-control", "signature": signature})
            p = tmp_path / "policy.json"
            p.write_text(json.dumps(body))
            monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
            _patch_trust(monkeypatch, require=False, keys={"fleet-control": "trust-key"})
            ceiling = load_security_policy()
            assert ceiling is not None
            assert ceiling.signature_state == SIGNATURE_UNVERIFIED

    def test_non_ascii_signature_fails_closed_when_required(self, monkeypatch, tmp_path):
        """...and with the opt-in ON it must ABORT, not degrade to ungoverned."""
        body = _policy_body(
            identity={"issuer": "fleet-control", "signature": "tamper\u2013ed"}
        )
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "trust-key"})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_verified_bad_signature_is_unverified(self, monkeypatch, tmp_path):
        body = _policy_body(identity={"issuer": "fleet-control", "signature": "not-the-mac"})
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": "trust-key"})
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNVERIFIED

    def test_tampered_payload_invalidates_a_good_signature(self, monkeypatch, tmp_path):
        # The core threat: an attacker edits a governed scope to WIDEN the ceiling
        # but cannot re-sign it.
        secret = "trust-key"
        body = _sign_policy(
            _policy_body(
                identity={"issuer": "fleet-control"},
                commands={"mode": "deny", "deny": ["git push*"]},
            ),
            secret,
        )
        body["commands"] = {"mode": "deny", "deny": []}  # ceiling widened in place
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        assert load_security_policy().signature_state == SIGNATURE_UNVERIFIED

    def test_signature_without_issuer_is_unverified(self, monkeypatch, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(identity={"signature": "abc"})))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": "k"})
        assert load_security_policy().signature_state == SIGNATURE_UNVERIFIED

    def test_no_trust_key_for_issuer_is_unverified(self, monkeypatch, tmp_path):
        body = _sign_policy(_policy_body(identity={"issuer": "unknown-issuer"}), "k")
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": "k"})
        assert load_security_policy().signature_state == SIGNATURE_UNVERIFIED

    def test_wrong_trust_key_is_unverified(self, monkeypatch, tmp_path):
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), "real-key")
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": "other-key"})
        assert load_security_policy().signature_state == SIGNATURE_UNVERIFIED


class TestPolicySignatureOptIn:
    """The load-bearing design decision: verification is opt-in and advisory."""

    def test_unsigned_with_require_off_still_loads_and_governs(self, monkeypatch, tmp_path):
        # Backward-compatibility guarantee: every existing policy file keeps
        # working unchanged, with no signature and no trust key provisioned.
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(commands={"mode": "deny", "deny": ["git push*"]})))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={})
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNSIGNED
        # …and the ceiling is still ENFORCED, not degraded to ungoverned.
        assert not resolve(ceiling, None, "commands", "git push origin").permitted

    def test_unverified_with_require_off_still_loads(self, monkeypatch, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(identity={"issuer": "x", "signature": "bogus"})))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={})
        assert load_security_policy() is not None

    def test_unsigned_with_require_on_fails_closed(self, monkeypatch, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "k"})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_tampered_with_require_on_fails_closed(self, monkeypatch, tmp_path):
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        body["version"] = 1
        body["boot"] = {"fail_closed": False}  # tampered after signing
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": secret})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_verified_with_require_on_boots(self, monkeypatch, tmp_path):
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=True, keys={"fleet-control": secret})
        assert load_security_policy().signature_state == SIGNATURE_VERIFIED

    def test_require_on_marks_governance_health_incident(self, monkeypatch, tmp_path):
        from kiro_crew.platform import governance_health

        governance_health.reset()
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()
        inc = governance_health.last_incident()
        assert inc is not None and inc["kind"] == "failed_closed"
        governance_health.reset()

    def test_home_tier_is_verified_too(self, monkeypatch, tmp_path):
        secret = "trust-key"
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        home = tmp_path / "security_policy.json"
        home.write_text(
            json.dumps(_sign_policy(_policy_body(identity={"issuer": "operator"}), secret))
        )
        monkeypatch.setattr("kiro_crew.platform.governance._policy_home_path", lambda: home)
        _patch_trust(monkeypatch, require=False, keys={"operator": secret})
        assert load_security_policy().signature_state == SIGNATURE_VERIFIED

    def test_home_tier_unsigned_with_require_on_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        home = tmp_path / "security_policy.json"
        home.write_text(json.dumps(_policy_body()))
        monkeypatch.setattr("kiro_crew.platform.governance._policy_home_path", lambda: home)
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={})
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_signed_bundle_outranks_an_unsigned_home_file(self, monkeypatch, tmp_path):
        # GPT finding: enforcing at LOAD time raised on whichever tier a given pass
        # happened to reach. The core's loader-less pass falls through to the HOME
        # file, so an enterprise host with an unsigned home file and a correctly
        # signed companion BUNDLE aborted — even though the bundle outranks home and
        # is what the final ceiling comes from. Enforcement moved to the composed
        # result so precedence decides which verdict is judged.
        secret = "trust-key"
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        home = tmp_path / "security_policy.json"
        home.write_text(json.dumps(_policy_body()))  # unsigned, lower precedence
        monkeypatch.setattr("kiro_crew.platform.governance._policy_home_path", lambda: home)
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": secret})
        # The core's loader-less pass must not abort on the lower-precedence tier…
        assert load_security_policy() is not None
        # …and the edition's signed bundle is what gets judged, so boot proceeds.
        bundle = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        assert _load_and_enforce(bundled_loader=lambda: bundle).signature_state == (
            SIGNATURE_VERIFIED
        )

    def test_bundled_tier_is_advisory_when_require_off(self, monkeypatch, tmp_path):
        # With require OFF (the Amazon edition ships no require), an unsigned
        # bundled policy still loads — advisory, exactly like the file tiers.
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        _patch_trust(monkeypatch, require=False, keys={})
        ceiling = load_security_policy(bundled_loader=lambda: _policy_body())
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNSIGNED

    def test_bundled_tier_is_NOT_exempt_when_required(self, monkeypatch, tmp_path):
        # GPT-review finding: the plugin-admission manifest signature covers only
        # name/publisher/version/capabilities, NOT the packaged
        # security_policy.json bytes, so "covered by admission" never protected
        # the resource — a tampered bundled policy loaded unchecked. Under
        # require_policy_signature the bundled tier must verify like any other.
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "trust-key"})
        # An unsigned bundled policy under require must abort, not load.
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce(
                bundled_loader=lambda: _policy_body(identity={"issuer": "fleet-control"})
            )
        # A correctly-signed bundled policy verifies and loads.
        signed = _sign_policy(
            _policy_body(identity={"issuer": "fleet-control"}), "trust-key"
        )
        ceiling = load_security_policy(bundled_loader=lambda: signed)
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_VERIFIED

    def test_no_policy_stays_none_when_require_off(self, monkeypatch, tmp_path):
        # require OFF: an ungoverned standalone host stays ungoverned.
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        _patch_trust(monkeypatch, require=False, keys={})
        assert load_security_policy() is None

    def test_loader_never_raises_on_absence_whatever_the_optin(self, monkeypatch, tmp_path):
        # The loader is not the authority on absence: it runs once per composition
        # pass (core without a bundled_loader, edition with one) and cannot tell
        # whether it is the last word. Raising inside it either aborts a bundle-only
        # enterprise host before its edition is consulted, or — keyed on the loader
        # being present — misses a standalone host entirely (the GPT finding). So
        # absence always yields None here; the refusal lives in the boot gate.
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        adm = tmp_path / "admission_policy.json"
        adm.write_text(json.dumps({"require_policy_signature": True,
                                   "trust_keys": {"fleet-control": "trust-key"}}))
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        assert load_security_policy() is None  # core's loader-less pass
        assert load_security_policy(bundled_loader=lambda: None) is None  # edition's pass


class TestPolicySignatureAbsenceGate:
    """``assert_policy_signature_satisfied`` — absence must not satisfy the mandate."""

    def test_no_policy_fails_closed_when_genuinely_required(self, monkeypatch, tmp_path):
        # With require ON and NO policy at any tier, handing back an ungoverned host
        # bypasses the requirement precisely when it matters (a mandated-signature
        # fleet that lost or never shipped its policy). Boot must abort instead.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(json.dumps({"require_policy_signature": True,
                                   "trust_keys": {"fleet-control": "trust-key"}}))
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        with pytest.raises(PlatformCompositionError):
            assert_policy_signature_satisfied(None)

    def test_standalone_no_loader_is_covered_too(self, monkeypatch, tmp_path):
        # GPT finding on the previous shape: gating the refusal on "a bundled_loader
        # was consulted" meant a STANDALONE host (no edition, so no loader ever) with
        # a genuine opt-in and no policy ran with no ceiling. The gate runs on the
        # composed context, so it does not depend on a loader existing at all.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(json.dumps({"require_policy_signature": True}))
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
        )
        ceiling = load_security_policy()  # standalone: no bundled_loader, ever
        assert ceiling is None
        with pytest.raises(PlatformCompositionError):
            assert_policy_signature_satisfied(ceiling)

    def test_verified_ceiling_satisfies_the_gate(self, monkeypatch, tmp_path):
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "trust-key"})
        signed = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), "trust-key")
        assert_policy_signature_satisfied(
            parse_policy(signed, signature_state=SIGNATURE_VERIFIED)
        )

    def test_present_but_unverified_ceiling_does_NOT_satisfy_the_gate(
        self, monkeypatch, tmp_path
    ):
        # Presence alone is not enough — the gate is the enforcement point for the
        # verdict too, now that load time only computes it. A tampered or unsigned
        # ceiling that survived precedence must abort here.
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": "trust-key"})
        for state in (SIGNATURE_UNSIGNED, SIGNATURE_UNVERIFIED, SIGNATURE_UNCHECKED):
            with pytest.raises(PlatformCompositionError):
                assert_policy_signature_satisfied(
                    parse_policy(_policy_body(), signature_state=state)
                )

    def test_unverified_ceiling_is_fine_when_require_off(self, monkeypatch, tmp_path):
        # The compatibility contract: with the flag off an unsigned ceiling loads
        # AND governs, so the gate must not touch it.
        _real_trust_file(monkeypatch, tmp_path, require=False, keys={})
        assert_policy_signature_satisfied(
            parse_policy(_policy_body(), signature_state=SIGNATURE_UNSIGNED)
        )

    def test_require_off_is_a_noop(self, monkeypatch, tmp_path):
        # The default and what the amazon edition ships: an ungoverned standalone
        # host stays ungoverned rather than being refused boot.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(json.dumps({"mode": "open"}))
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        assert_policy_signature_satisfied(None)

    @pytest.mark.parametrize(
        "shape", ['{ "mode": "open",  <-- typo', "[]", "null", '"a string"', "123"]
    )
    def test_a_broken_trust_root_reads_as_no_optin_by_design(
        self, monkeypatch, tmp_path, shape
    ):
        """A corrupt/malformed admission file does NOT fail closed. Deliberate.

        An attacker who can write this file is outside the policy-signature threat
        model — they would set the flag to ``false``, which parses fine — so
        fail-closing on a *malformed* file catches only a clumsy version of an attack
        the design concedes, while turning a non-atomic fleet push or a hand-edit
        typo into an unbootable host. Corruption here is a reliability event: logged,
        predictable, reported by ``kirocrew doctor``.
        """
        bad = tmp_path / "admission_policy.json"
        bad.write_text(shape)
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(bad))
        assert_policy_signature_satisfied(None)
        assert_policy_signature_satisfied(
            parse_policy(_policy_body(), signature_state=SIGNATURE_UNSIGNED)
        )

    def test_absent_admission_file_is_a_noop(self, monkeypatch, tmp_path):
        # No trust root: nobody opted in, so an unsigned policy still loads and
        # governs (the compatibility contract) and no policy stays ungoverned.
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(tmp_path / "missing.json"))
        assert_policy_signature_satisfied(None)
        assert_policy_signature_satisfied(
            parse_policy(_policy_body(), signature_state=SIGNATURE_UNSIGNED)
        )

    def test_absent_admission_policy_keeps_verification_advisory(self, monkeypatch, tmp_path):
        # An admission-policy problem is handled loudly in admission's OWN domain;
        # it must not additionally make the security ceiling unloadable here.
        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.admission._policy_default_path",
            lambda: tmp_path / "no-admission.json",
        )
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNSIGNED

    def test_raising_trust_root_keeps_verification_advisory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "kiro_crew.platform.admission.read_policy_trust_root",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        assert load_security_policy().signature_state == SIGNATURE_UNSIGNED

    def test_trust_settings_read_from_admission_policy_file(self, monkeypatch, tmp_path):
        # One key store: the flag + keys come from the admission policy file, not a
        # second bespoke file, and NOT from the security policy itself.
        adm = tmp_path / "admission_policy.json"
        adm.write_text(
            json.dumps(
                {"require_policy_signature": True, "trust_keys": {"fleet-control": "k"}}
            )
        )
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        from kiro_crew.platform.governance import _policy_trust_settings

        require, keys = _policy_trust_settings()
        assert require is True
        assert keys == {"fleet-control": "k"}

    def test_security_policy_cannot_self_declare_the_requirement(self, monkeypatch, tmp_path):
        # A require_policy_signature key inside security_policy.json is NOT a
        # governed scope, so it fails closed as an unknown key rather than being
        # honored — a document must not be the authority on its own authenticity.
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(require_policy_signature=True)))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={})
        with pytest.raises(PlatformCompositionError):
            load_security_policy()

    def test_loading_does_not_disturb_admission_health_signal(self, monkeypatch, tmp_path):
        # The trust-root read must NOT run the audited admission loader: that
        # records posture + a critical SEL, and gatewayd re-loads the security
        # policy per app call.
        from kiro_crew.platform import governance_health

        governance_health.reset()
        monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_crew.platform.admission._policy_default_path",
            lambda: tmp_path / "no-admission.json",
        )
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body()))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        load_security_policy()
        assert governance_health.governance_status() == "unknown"
        assert governance_health.last_incident() is None
        governance_health.reset()


class TestParsePolicySignatureState:
    def test_direct_parse_is_unchecked_not_unsigned(self):
        # parse_policy has the document but no trust root; claiming "unsigned"
        # would conflate "we looked" with "nobody looked".
        ceiling = parse_policy(_policy_body())
        assert ceiling.signature_state == SIGNATURE_UNCHECKED
        assert "not checked" in ceiling.signature_summary()

    def test_identity_fields_still_parsed(self):
        ceiling = parse_policy(_policy_body(identity={"issuer": "fleet-control", "signature": "s"}))
        assert ceiling.identity_issuer == "fleet-control"
        assert ceiling.identity_signature == "s"


class TestPolicyShowReporting:
    """`kirocrew policy show` must distinguish the three provenance states."""

    def _show(self, capsys, ceiling):
        import argparse
        from unittest.mock import patch

        from kiro_crew import cli_commands

        args = argparse.Namespace(policy_action="show")
        # _policy imports current_context lazily from platform.context, so patch
        # it at the definition site.
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=type("Ctx", (), {"governance": ceiling})(),
        ):
            cli_commands._policy(args)
        return capsys.readouterr().out

    def _ceiling(self, state, issuer="fleet-control", signature="sig"):
        return GovernanceCeiling(
            version=1,
            boot=parse_policy(_policy_body()).boot,
            controls={},
            identity_issuer=issuer,
            identity_signature=signature,
            signature_state=state,
        )

    def test_show_reports_verified(self, capsys):
        out = self._show(capsys, self._ceiling(SIGNATURE_VERIFIED))
        assert "signed and verified" in out
        assert "fleet-control" in out

    def test_show_reports_signed_but_unverified(self, capsys):
        out = self._show(capsys, self._ceiling(SIGNATURE_UNVERIFIED))
        assert "UNVERIFIED" in out
        # An unproven issuer must NOT be presented as an established fact.
        assert "signed and verified" not in out

    def test_show_reports_unsigned(self, capsys):
        out = self._show(capsys, self._ceiling(SIGNATURE_UNSIGNED, issuer="", signature=""))
        assert "unsigned" in out
        assert "verified" not in out

    def test_show_no_policy_unchanged(self, capsys):
        out = self._show(capsys, None)
        assert "No enterprise security policy is active" in out
