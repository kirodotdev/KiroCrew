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

import base64
import hashlib
import hmac
import json
import logging

import pytest

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    CAPABILITY,
    MODE_ALLOW,
    MODE_DENY,
    SCOPE_CATALOG,
    SIGNATURE_UNCHECKED,
    SIGNATURE_UNSIGNED,
    SIGNATURE_UNVERIFIED,
    SIGNATURE_VERIFIED,
    TIER_ENV,
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

    def test_from_dict_rejects_non_boolean_enabled(self):
        # bool("false") is True — a stringly-typed disable must not permit.
        # A present null is not "absent": default-ON scopes must not stay on.
        for bogus in ("false", "true", 1, 0, ["yes"], None):
            with pytest.raises(PlatformCompositionError, match="boolean"):
                CapabilityGate.from_dict({"enabled": bogus}, default_enabled=True)

    def test_known_capability_rejects_non_boolean_enabled(self):
        # Default-ON siblings (memory_writes, browse, …) used to coerce
        # enabled: "false" through bool() and stay on.
        with pytest.raises(PlatformCompositionError, match="boolean"):
            parse_profile(
                {"name": "host", "capabilities": {"memory_writes": {"enabled": "false"}}}
            )

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
        """Env still outranks the bundled resource — but bundled IS now resolved.

        The bundled loader used to be skipped entirely when the env tier was set,
        and this test asserted that by failing if it ran.  It is resolved
        unconditionally now, deliberately: the CENTRAL tier outranks env, and the
        source it fetches from may be DECLARED by a lower tier's ``distribution``
        block, so the bundled document has to be read for its declaration to be
        seen even when env will win the subordinate slot.

        The contract that still holds — and the one this pins — is the precedence
        itself: tiers 3–5 are mutually exclusive with env first, so the env
        document is the one that becomes the ceiling.  Asserted on identity rather
        than on whether the loader ran, because "who won" is the invariant; "was
        bundled consulted" is an implementation detail that just changed.
        """
        monkeypatch.delenv("KIROCREW_POLICY_URL", raising=False)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(_policy_body(identity={"issuer": "env-tier"})))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        resolved = {}

        def bundled():
            resolved["yes"] = True
            return _policy_body(identity={"issuer": "bundled-tier"})

        ceiling = load_security_policy(bundled_loader=bundled)
        assert ceiling is not None
        # Resolved, so a ``distribution`` block declared here would be seen…
        assert resolved.get("yes")
        # …but it does not win: env is the first present subordinate tier.
        assert ceiling.identity_issuer == "env-tier"
        assert ceiling.tier == TIER_ENV

    def test_wrong_version_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy({"version": 99, "boot": {"fail_closed": True}})

    def test_missing_boot_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy({"version": 1})

    def test_unknown_governed_key_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy_body(bogus_scope={"mode": "allow"}))

    def test_typod_sandbox_child_fails_closed(self):
        """A typo'd ``min_level`` must RAISE, not vanish into the reserved scope.

        ``sandbox`` used to accept ANY child into the write-only
        ``sandbox._flags`` scope, so ``min_levl`` parsed clean and left the
        floor absent — green validation, zero enforcement, on the ordinal with
        the widest blast radius.  The message names the key so the operator can
        see WHICH key was rejected.
        """
        with pytest.raises(PlatformCompositionError) as exc:
            parse_policy(_policy_body(sandbox={"min_levl": "strict"}))
        assert "sandbox.min_levl" in str(exc.value)
        assert "fail-closed" in str(exc.value)

    def test_typod_sandbox_child_does_not_silently_drop_the_floor(self):
        """The regression's CONSEQUENCE: it must not parse to an absent floor.

        Pinned separately from the raise so a future change that re-tolerates
        the key cannot pass by merely raising somewhere else — what must never
        happen again is a ceiling that reports success while
        ``sandbox.min_level`` is unset.
        """
        try:
            ceiling = parse_policy(_policy_body(sandbox={"min_levl": "strict"}))
        except PlatformCompositionError:
            return
        pytest.fail(f"parsed clean with sandbox.min_level={ceiling.get('sandbox.min_level')!r}")

    def test_reserved_sandbox_boot_flags_still_parse(self):
        """The compatibility half: the documented reserved flags must still load."""
        ceiling = parse_policy(
            _policy_body(
                sandbox={
                    "min_level": "strict",
                    "require_isolation": True,
                    "env_scrub_prefixes": ["AWS_SECRET"],
                }
            )
        )
        assert isinstance(ceiling.get("sandbox.min_level"), OrdinalControl)
        flags = ceiling.controls["sandbox._flags"]
        assert flags == {"require_isolation": True, "env_scrub_prefixes": ["AWS_SECRET"]}

    def test_sandbox_min_level_alone_still_parses(self):
        """The overwhelmingly common shape must be untouched by the new check."""
        ceiling = parse_policy(_policy_body(sandbox={"min_level": "strict"}))
        assert isinstance(ceiling.get("sandbox.min_level"), OrdinalControl)
        assert "sandbox._flags" not in ceiling.controls


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

    def test_e7_channel_posture_enterprise_id(self, ceiling, profile):
        assert resolve(ceiling, profile, "channels", "slack/allowed_enterprise_ids:E0123").permitted
        assert not resolve(
            ceiling, profile, "channels", "slack/allowed_enterprise_ids:E9999"
        ).permitted

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


class TestProfileUnknownCapabilityTolerance:
    """A PROFILE tolerates an unregistered ``capabilities.*`` child, ASYMMETRICALLY.

    Only ``{"enabled": true}`` is tolerated — it is inert, because a profile only
    narrows and the intersection happens in ``resolve``. An unknown NARROWING
    (``enabled: false``) or a malformed child still fails closed, because it is
    indistinguishable from a typo'd narrowing of a CORE capability and honoring the
    operator's intent means denying.

    The cross-edition case this serves: an edition that ``register_scope``s extra
    capability rows seeds a profile naming them with ``enabled: true``, and a build
    WITHOUT those rows reads the same data home.
    """

    def test_unknown_capability_child_enabled_true_does_not_invalidate_the_profile(self):
        prof = parse_profile(
            {
                "name": "host",
                "capabilities": {
                    "capability_install": {"enabled": True},
                    "external_access": {"enabled": True},
                    "spawn": {"enabled": False},
                },
            }
        )
        assert prof.name == "host"
        # The unknown children are recorded, not enforced.
        assert prof.unknown_scopes == (
            "capabilities.capability_install",
            "capabilities.external_access",
        )
        assert prof.get("capabilities.capability_install") is None
        # …and the KNOWN sibling in the same block still parses and still governs.
        assert prof.get("capabilities.spawn") == CapabilityGate(enabled=False)

    def test_unknown_capability_child_enabled_false_fails_closed(self):
        # THE TYPO-PROTECTION CASE. ``spwan`` is a typo for ``spawn``; tolerating it
        # would silently PERMIT the capability the operator tried to disable. It must
        # raise so the loader substitutes deny-all instead.
        with pytest.raises(PlatformCompositionError):
            parse_profile({"name": "host", "capabilities": {"spwan": {"enabled": False}}})

    def test_unknown_capability_child_without_enabled_fails_closed(self):
        # Intent is unreadable (it may carry scopes meant to narrow), so deny.
        with pytest.raises(PlatformCompositionError):
            parse_profile(
                {"name": "host", "capabilities": {"vaulted": {"scopes": {"x": {"mode": "allow"}}}}}
            )

    def test_unknown_capability_child_non_dict_fails_closed(self):
        for bogus in (True, False, "enabled", 1, [], None):
            with pytest.raises(PlatformCompositionError):
                parse_profile({"name": "host", "capabilities": {"vaulted": bogus}})

    def test_enabled_must_be_exactly_true_not_merely_truthy(self):
        # Guards against a widened predicate: only the boolean True is inert.
        for truthy in (1, "true", ["yes"]):
            with pytest.raises(PlatformCompositionError):
                parse_profile({"name": "host", "capabilities": {"vaulted": {"enabled": truthy}}})

    def test_unknown_capability_child_with_extra_keys_fails_closed(self):
        # ENABLE-PLUS-NARROWING. A capability payload can carry inner narrowing
        # rulesets (``spawn`` has ``agents``); a typo'd ``spwan`` declared
        # ``{"enabled": true, "agents": {...allowlist...}}`` is an operator
        # enabling spawn AND restricting which agents may be spawned. Skipping
        # it would drop the inner narrowing, so any key beyond ``enabled``
        # must fail closed. Only the exact one-key ``{"enabled": true}`` is
        # provably inert.
        with pytest.raises(PlatformCompositionError):
            parse_profile(
                {
                    "name": "host",
                    "capabilities": {
                        "spwan": {
                            "enabled": True,
                            "agents": {"mode": "allow", "allow": []},
                        }
                    },
                }
            )

    def test_unknown_capability_child_is_logged_with_profile_and_key(self, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.governance"):
            parse_profile({"name": "host", "capabilities": {"vaulted": {"enabled": True}}})
        assert any(
            "host" in r.getMessage() and "capabilities.vaulted" in r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )

    def test_same_key_in_a_policy_still_fails_closed(self):
        # Tamper-evidence on the ceiling is unchanged (Rule 8): only the profile
        # path is tolerant, and even there only for enabled:true.
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy_body(capabilities={"capability_install": {"enabled": True}}))

    def test_policy_fallback_object_still_fails_closed(self):
        # The policy's top-level ``fallback`` parses as a narrow-only profile but is
        # NOT a profile FILE, so it gets no tolerance even for enabled:true.
        with pytest.raises(PlatformCompositionError):
            parse_policy(
                _policy_body(fallback={"capabilities": {"capability_install": {"enabled": True}}})
            )

    def test_unknown_top_level_family_in_a_profile_still_fails_closed(self):
        with pytest.raises(PlatformCompositionError):
            parse_profile({"name": "host", "vault": {"mode": "allow", "allow": ["read"]}})

    def test_a_profile_with_no_unknown_keys_records_nothing(self):
        prof = parse_profile({"name": "host", "capabilities": {"spawn": {"enabled": True}}})
        assert prof.unknown_scopes == ()

    def test_extends_composition_preserves_the_record(self):
        parent = parse_profile({"name": "base", "capabilities": {"vaulted": {"enabled": True}}})
        child = parse_profile({"name": "leaf", "capabilities": {"othered": {"enabled": True}}})
        merged = compose_profiles(parent, child)
        assert merged.unknown_scopes == ("capabilities.vaulted", "capabilities.othered")

    def test_a_registered_capability_parses_normally_again(self):
        # Guards the append-only contract from the other side: once the row IS
        # registered, the key stops being tolerated and starts being enforced.
        register_scope("capabilities.capability_install", ScopeSpec(CAPABILITY))
        try:
            prof = parse_profile(
                {"name": "host", "capabilities": {"capability_install": {"enabled": False}}}
            )
            assert prof.unknown_scopes == ()
            assert prof.get("capabilities.capability_install") == CapabilityGate(enabled=False)
        finally:
            SCOPE_CATALOG.pop("capabilities.capability_install", None)


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


# ──────────────────────────────────────────────────────────────────────────
# Capability omission — the settled contract
# ──────────────────────────────────────────────────────────────────────────
def _capability_scopes() -> list[str]:
    return [name for name, spec in SCOPE_CATALOG.items() if spec.kind == CAPABILITY]


class TestCapabilityOmissionIsUngoverned:
    """An unnamed capability is UNGOVERNED and therefore PERMITTED. On purpose.

    This class exists because the opposite is an inviting mistake. Several
    ``SCOPE_CATALOG`` comments once described ``capability_default`` as applying
    when a policy "governs ``capabilities.*`` but omits" a row, which does not
    match the code. The tempting fix is to make the code match the comments by
    filling unnamed rows with their registered defaults.

    That fix was implemented, measured, and rejected. Three reasons, recorded here
    so the next person does not repeat it:

    1. It breaks the model's central invariant. Omission means
       ungoverned-and-permitted for `mcp`, `tools`, `commands`, `filesystem.*` and
       `network.egress`; making `capabilities` the one archetype that infers a
       value is a per-control special case in an evaluator whose stated contract
       is that it dispatches on archetype and never on scope name.
    2. It requires a namespace-specific branch in ``_parse_controls``, whose
       documented contract is that a newly ``register_scope``'d family parses with
       NO loader edit. A companion's own capability family would not get the same
       treatment, so the behaviour would not even be uniform.
    3. It destroys an audit signal. ``Decision.layer == "default"`` means "nothing
       governed this", which is what operators are told to alert on to find
       missing controls. A row filled from a catalog default resolves at
       ``layer="policy"`` and so becomes indistinguishable from one a human
       actually wrote.

    The real defect was documentation, and the protection is
    ``kirocrew policy validate`` reporting a partially-governed block.
    """

    def test_unnamed_capability_is_permitted_and_reads_as_ungoverned(self):
        ceiling = parse_policy(_policy_body(capabilities={"script_hooks": {"enabled": False}}))
        for scope in _capability_scopes():
            if scope == "capabilities.script_hooks":
                continue
            decision = resolve(ceiling, None, scope, "")
            assert decision.permitted, f"{scope} must be permitted by omission"
            # Load-bearing: `default` is the audit signal for "nobody governed
            # this". A filled-in row would report `policy` and hide the gap.
            assert decision.layer == "default", f"{scope} must read as ungoverned"

    def test_named_row_is_still_governed(self):
        ceiling = parse_policy(_policy_body(capabilities={"script_hooks": {"enabled": False}}))
        decision = resolve(ceiling, None, "capabilities.script_hooks", "")
        assert not decision.permitted
        assert decision.layer == "policy"

    def test_omission_behaves_identically_across_archetypes(self):
        """The consistency argument, asserted rather than assumed."""
        ceiling = parse_policy(_policy_body(capabilities={"cron": {"enabled": False}}))
        for scope, item in (
            ("mcp", "@anything/tool"),
            ("tools", "anything"),
            ("commands", "echo hi"),
            ("filesystem.read", "/etc/hosts"),
            ("network.egress", "example.com"),
            ("capabilities.messaging", ""),
        ):
            decision = resolve(ceiling, None, scope, item)
            assert decision.permitted, f"unnamed {scope} must permit"
            assert decision.layer == "default", f"unnamed {scope} must read as ungoverned"

    def test_no_capabilities_block_leaves_every_capability_ungoverned(self):
        ceiling = parse_policy(_policy_body(commands={"mode": MODE_DENY, "deny": ["nc *"]}))
        for scope in _capability_scopes():
            assert resolve(ceiling, None, scope, "").permitted

    def test_present_key_without_enabled_uses_the_registered_default(self):
        """The case ``capability_default`` DOES cover — and the only one.

        Naming a capability to configure its inner scopes, without saying whether
        it is on, resolves to the registered default: off for the exfil surfaces,
        on for the benign ones.
        """
        ceiling = parse_policy(
            _policy_body(
                capabilities={
                    "publish": {"scopes": {"destinations": {"mode": MODE_ALLOW, "allow": ["x"]}}},
                    "spawn": {"scopes": {"agents": {"mode": MODE_ALLOW, "allow": ["a"]}}},
                }
            )
        )
        assert not resolve(ceiling, None, "capabilities.publish", "").permitted
        assert SCOPE_CATALOG["capabilities.publish"].capability_default is False
        assert resolve(ceiling, None, "capabilities.spawn", "").permitted
        assert SCOPE_CATALOG["capabilities.spawn"].capability_default is True


class TestValidateReportsUngovernedCapabilities:
    """`kirocrew policy validate` must surface a partially-governed block.

    Since omission cannot deny, the only protection against an author believing
    otherwise is telling them which rows they left open.
    """

    def _validate(self, capsys, ceiling):
        import argparse
        from unittest.mock import patch

        from kiro_crew import cli_commands

        args = argparse.Namespace(policy_action="validate")
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=type("Ctx", (), {"governance": ceiling})(),
        ):
            cli_commands._policy(args)
        return capsys.readouterr().out

    def test_partial_block_lists_the_ungoverned_rows(self, capsys):
        ceiling = parse_policy(_policy_body(capabilities={"script_hooks": {"enabled": False}}))
        out = self._validate(capsys, ceiling)
        assert "UNGOVERNED" in out
        assert "Omission does not deny" in out
        assert "capabilities.cron" in out
        # The row the author DID name must not be reported as a gap.
        assert "capabilities.script_hooks\n" not in out.split("UNGOVERNED", 1)[1]

    def test_fully_enumerated_block_reports_no_gap(self, capsys):
        body = {scope.split(".", 1)[1]: {"enabled": True} for scope in _capability_scopes()}
        # agentcore requires a known inner posture when enabled.
        body["agentcore"] = {"enabled": True, "posture": "workload"}
        out = self._validate(capsys, parse_policy(_policy_body(capabilities=body)))
        assert "UNGOVERNED" not in out
        assert "✅ valid" in out

    def test_policy_that_never_mentions_capabilities_reports_no_gap(self, capsys):
        """Silence about capabilities entirely is not a partial statement."""
        ceiling = parse_policy(_policy_body(commands={"mode": MODE_DENY, "deny": ["nc *"]}))
        out = self._validate(capsys, ceiling)
        assert "UNGOVERNED" not in out


# ──────────────────────────────────────────────────────────────────────────
# Asymmetric (Ed25519) policy signatures — checked BEFORE the shared secret
# ──────────────────────────────────────────────────────────────────────────
def _ed25519_pair():
    """A real Ed25519 key pair as ``(private_key, base64_public_key)``.

    The base64 form of the raw 32-byte point is exactly what an operator pastes
    into the admission policy's ``trust_public_keys``.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private = ed25519.Ed25519PrivateKey.generate()
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(raw_public).decode("ascii")


def _sign_policy_ed25519(body: dict, private_key) -> dict:
    """Return *body* with a valid Ed25519 ``identity.signature`` over its payload."""
    signed = json.loads(json.dumps(body))  # deep copy; body may be reused
    signature = private_key.sign(policy_signing_payload(signed))
    signed.setdefault("identity", {})["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


def _patch_asymmetric(monkeypatch, *, public_keys: dict, require: bool = False):
    """Point the loader's asymmetric trust settings at fixed values (no file I/O)."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance._policy_asymmetric_settings",
        lambda: (dict(public_keys), require),
    )


class TestAsymmetricPolicySignatureState:
    """``_policy_signature_state`` — the pure classifier, asymmetric-first.

    Why asymmetric wins the ordering: with a shared secret the verifier holds the
    same key the signer does, so anyone who can read the trust root can mint a
    ceiling that verifies.  An Ed25519 public key cannot be used to re-sign, so a
    fleet migrating to it must not have the weaker proof accepted as a
    consolation prize when the strong one fails.
    """

    def test_public_key_verifies_and_the_detail_names_ed25519(self):
        from kiro_crew.platform.governance import _policy_signature_state

        private, public = _ed25519_pair()
        body = _sign_policy_ed25519(_policy_body(identity={"issuer": "fleet-control"}), private)
        state, detail = _policy_signature_state(
            body, {}, public_keys={"fleet-control": public}
        )
        assert state == SIGNATURE_VERIFIED
        # The verdict alone cannot tell an operator WHICH proof held; the detail is
        # what lands in the SEL record, so it has to distinguish the two.
        assert "ed25519" in detail
        assert "fleet-control" in detail

    def test_hex_public_key_form_also_verifies(self):
        # ``openssl`` and several MDM consoles emit keys as hex, not base64.
        from kiro_crew.platform.governance import _policy_signature_state

        private, public = _ed25519_pair()
        hex_public = base64.b64decode(public).hex()
        body = _sign_policy_ed25519(_policy_body(identity={"issuer": "fleet-control"}), private)
        state, _ = _policy_signature_state(body, {}, public_keys={"fleet-control": hex_public})
        assert state == SIGNATURE_VERIFIED

    def test_hmac_still_verifies_when_the_issuer_has_no_public_key(self):
        # Back-compat: a fleet that already signs with a shared secret keeps
        # verifying byte-for-byte, and the HMAC success detail is deliberately
        # UNCHANGED so nothing parsing it has to be updated.
        from kiro_crew.platform.governance import _policy_signature_state

        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        state, detail = _policy_signature_state(
            body, {"fleet-control": secret}, public_keys={"someone-else": _ed25519_pair()[1]}
        )
        assert state == SIGNATURE_VERIFIED
        assert detail == "issuer 'fleet-control'"

    def test_hmac_detail_is_identical_with_and_without_the_new_arguments(self):
        # The keyword-only additions are inert by default: a two-argument caller
        # must get byte-identical behaviour, state AND detail.
        from kiro_crew.platform.governance import _policy_signature_state

        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        assert _policy_signature_state(body, {"fleet-control": secret}) == _policy_signature_state(
            body, {"fleet-control": secret}, public_keys={}, require_asymmetric=False
        )

    def test_require_asymmetric_refuses_a_correct_hmac(self):
        # The document really is correctly signed. It is refused anyway, because a
        # symmetric proof is re-mintable by anyone who can read the verifying key —
        # exactly what this flag says is not good enough.
        from kiro_crew.platform.governance import _policy_signature_state

        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        # Same document, same key: verified without the flag…
        assert (
            _policy_signature_state(body, {"fleet-control": secret})[0] == SIGNATURE_VERIFIED
        )
        # …and unverified with it, so the refusal is about the PROOF TYPE and not a
        # broken signature.
        state, detail = _policy_signature_state(
            body, {"fleet-control": secret}, require_asymmetric=True
        )
        assert state == SIGNATURE_UNVERIFIED
        assert "symmetric" in detail

    def test_require_asymmetric_still_accepts_an_ed25519_signature(self):
        from kiro_crew.platform.governance import _policy_signature_state

        private, public = _ed25519_pair()
        body = _sign_policy_ed25519(_policy_body(identity={"issuer": "fleet-control"}), private)
        state, _ = _policy_signature_state(
            body,
            {},
            public_keys={"fleet-control": public},
            require_asymmetric=True,
        )
        assert state == SIGNATURE_VERIFIED

    def test_failed_asymmetric_check_does_not_fall_back_to_the_hmac_key(self):
        """The migration invariant: a public key is a COMMITMENT, not a preference.

        The document carries a signature that is a perfectly correct HMAC for the
        issuer's shared secret, and that secret is present in ``trust_keys``.  Because
        the issuer ALSO has a public key, the asymmetric check runs and fails, and the
        verdict must stay UNVERIFIED — falling through to the symmetric key would make
        an in-progress migration meaningless, since an insider who can read
        ``trust_keys`` could keep minting ceilings for an issuer that had supposedly
        moved to asymmetric signing.
        """
        from kiro_crew.platform.governance import _policy_signature_state

        secret = "trust-key"
        _private, public = _ed25519_pair()
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)

        # Control: with no public key for this issuer the same document verifies, so
        # the HMAC really is correct and the test is not vacuous.
        assert (
            _policy_signature_state(body, {"fleet-control": secret})[0] == SIGNATURE_VERIFIED
        )

        state, detail = _policy_signature_state(
            body, {"fleet-control": secret}, public_keys={"fleet-control": public}
        )
        assert state == SIGNATURE_UNVERIFIED
        assert "asymmetric" in detail
        assert "fleet-control" in detail

    def test_wrong_ed25519_signature_is_unverified_not_a_crash(self):
        # A signature minted by a DIFFERENT Ed25519 key is well-formed, so this
        # exercises the real verify path rather than the malformed-input shortcut.
        from kiro_crew.platform.governance import _policy_signature_state

        _private, public = _ed25519_pair()
        attacker, _attacker_public = _ed25519_pair()
        body = _sign_policy_ed25519(_policy_body(identity={"issuer": "fleet-control"}), attacker)
        state, _ = _policy_signature_state(body, {}, public_keys={"fleet-control": public})
        assert state == SIGNATURE_UNVERIFIED

    def test_tampered_document_invalidates_an_ed25519_signature(self):
        # The core threat, on the asymmetric path: WIDEN a governed scope after
        # signing and the signature no longer covers the bytes.
        from kiro_crew.platform.governance import _policy_signature_state

        private, public = _ed25519_pair()
        body = _sign_policy_ed25519(
            _policy_body(
                identity={"issuer": "fleet-control"},
                commands={"mode": "deny", "deny": ["git push*"]},
            ),
            private,
        )
        body["commands"] = {"mode": "deny", "deny": []}  # ceiling widened
        state, _ = _policy_signature_state(body, {}, public_keys={"fleet-control": public})
        assert state == SIGNATURE_UNVERIFIED

    def test_malformed_public_key_is_unverified_and_never_falls_back(self):
        # A junk key must not degrade into "no public key for this issuer" and hand
        # the verdict to the shared secret; ``ed25519_verify`` returns False and the
        # asymmetric branch owns the outcome.
        from kiro_crew.platform.governance import _policy_signature_state

        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        state, detail = _policy_signature_state(
            body, {"fleet-control": secret}, public_keys={"fleet-control": "not-a-key"}
        )
        assert state == SIGNATURE_UNVERIFIED
        assert "asymmetric" in detail

    def test_unsigned_and_issuerless_verdicts_are_unchanged(self):
        from kiro_crew.platform.governance import _policy_signature_state

        _private, public = _ed25519_pair()
        keys = {"fleet-control": public}
        assert (
            _policy_signature_state(_policy_body(), {}, public_keys=keys)[0]
            == SIGNATURE_UNSIGNED
        )
        state, _ = _policy_signature_state(
            _policy_body(identity={"signature": "abc"}), {}, public_keys=keys
        )
        assert state == SIGNATURE_UNVERIFIED


class TestAsymmetricPolicySignatureThroughTheLoader:
    """The same behaviour end to end, where a real host reads it off the ceiling."""

    def test_ed25519_signed_env_policy_loads_verified(self, monkeypatch, tmp_path):
        private, public = _ed25519_pair()
        body = _sign_policy_ed25519(_policy_body(identity={"issuer": "fleet-control"}), private)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={})
        _patch_asymmetric(monkeypatch, public_keys={"fleet-control": public})
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_VERIFIED

    def test_ed25519_signature_survives_reserialization(self, monkeypatch, tmp_path):
        # Coverage is byte-canonical over the PARSED json, so re-indenting or
        # reordering the file does not invalidate the signature.
        private, public = _ed25519_pair()
        body = _sign_policy_ed25519(_policy_body(identity={"issuer": "fleet-control"}), private)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body, indent=4, sort_keys=True) + "\n")
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={})
        _patch_asymmetric(monkeypatch, public_keys={"fleet-control": public})
        assert load_security_policy().signature_state == SIGNATURE_VERIFIED

    def test_hmac_only_fleet_is_unaffected_by_the_new_settings(self, monkeypatch, tmp_path):
        # The upgrade contract: a fleet with no public keys and the flag off sees
        # exactly its previous verdict.
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        _patch_asymmetric(monkeypatch, public_keys={}, require=False)
        assert load_security_policy().signature_state == SIGNATURE_VERIFIED

    def test_require_asymmetric_downgrades_an_hmac_only_ceiling(self, monkeypatch, tmp_path):
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        _patch_asymmetric(monkeypatch, public_keys={}, require=True)
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNVERIFIED

    def test_require_asymmetric_with_the_opt_in_aborts_boot(self, monkeypatch, tmp_path):
        # An HMAC-only ceiling under both flags must FAIL CLOSED at the boot gate
        # rather than degrade to an unauthenticated (and therefore ungoverned) host.
        secret = "trust-key"
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={"fleet-control": secret})
        _patch_asymmetric(monkeypatch, public_keys={}, require=True)
        with pytest.raises(PlatformCompositionError):
            _load_and_enforce()

    def test_ed25519_signature_satisfies_the_opt_in(self, monkeypatch, tmp_path):
        # The other half: the strong proof passes the same gate.
        private, public = _ed25519_pair()
        body = _sign_policy_ed25519(_policy_body(identity={"issuer": "fleet-control"}), private)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _real_trust_file(monkeypatch, tmp_path, require=True, keys={})
        _patch_asymmetric(monkeypatch, public_keys={"fleet-control": public}, require=True)
        ceiling = _load_and_enforce()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_VERIFIED

    def test_public_key_issuer_does_not_fall_back_to_its_hmac_key(self, monkeypatch, tmp_path):
        # End-to-end form of the migration invariant: both key maps are populated
        # for this issuer and the HMAC is correct, but the asymmetric check owns the
        # verdict, so the ceiling loads UNVERIFIED.
        secret = "trust-key"
        _private, public = _ed25519_pair()
        body = _sign_policy(_policy_body(identity={"issuer": "fleet-control"}), secret)
        p = tmp_path / "policy.json"
        p.write_text(json.dumps(body))
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(p))
        _patch_trust(monkeypatch, require=False, keys={"fleet-control": secret})
        _patch_asymmetric(monkeypatch, public_keys={"fleet-control": public})
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.signature_state == SIGNATURE_UNVERIFIED

    def test_asymmetric_settings_reader_never_raises(self, monkeypatch, tmp_path):
        # Contract mirror of ``_policy_trust_settings``: an unreadable admission
        # policy yields no keys and no opt-in, so an admission-domain problem cannot
        # make the security ceiling unloadable through a second path.
        from kiro_crew.platform.governance import _policy_asymmetric_settings

        bad = tmp_path / "admission_policy.json"
        bad.write_text("{ not json")
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(bad))
        assert _policy_asymmetric_settings() == ({}, False)

    def test_asymmetric_settings_read_from_a_real_admission_file(self, monkeypatch, tmp_path):
        # The keys live in the admission policy (already keystone-fenced, already the
        # fleet-controlled trust root) rather than in a second key store.
        from kiro_crew.platform.governance import _policy_asymmetric_settings

        _private, public = _ed25519_pair()
        adm = tmp_path / "admission_policy.json"
        adm.write_text(
            json.dumps(
                {
                    "trust_public_keys": {"fleet-control": public},
                    "require_asymmetric_policy_signature": True,
                }
            )
        )
        monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
        assert _policy_asymmetric_settings() == ({"fleet-control": public}, True)
