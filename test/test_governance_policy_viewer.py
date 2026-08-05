"""Read-only governance policy viewer — GET /api/governance/policy.

Covers the effective-ceiling snapshot the Settings > Security viewer renders:

* the byte-identical standalone default (no policy + no profile → every scope
  ``ungoverned``, permits);
* a policy that governs several scopes across all four archetypes → the right
  ``source`` + ``detail`` per scope, the rest ungoverned;
* ``HOST_SESSION_KEY`` is the surface used for profile resolution, and a
  host-bound profile intersects the policy (``policy+profile``);
* the endpoint is GET-only / fail-safe for display (a resolution error yields a
  well-formed ``unavailable`` response, never a 500).
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from kiro_crew.dashboard.handlers.security import (
    api_governance_policy,
    build_governance_policy_snapshot,
)
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import _SCOPE_ALIASES, SCOPE_CATALOG, parse_policy


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield d
    gp.reset_store()


@pytest.fixture(autouse=True)
def _reset_ctx():
    yield
    ctx_mod.reset_context()


def _install_ceiling(policy_body):
    """Compose a context carrying the given policy and install it as active."""
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


def _write(d, name, body):
    (d / f"{name}.json").write_text(json.dumps(body))


def _by_scope(snapshot):
    return {row["scope"]: row for row in snapshot["scopes"]}


# ──────────────────────────────────────────────────────────────────────────
# Byte-identical standalone default
# ──────────────────────────────────────────────────────────────────────────
class TestNoPolicyDefault:
    def test_no_policy_no_profile_all_ungoverned(self, profiles_dir):
        _install_ceiling(None)
        snap = build_governance_policy_snapshot()

        assert snap["has_policy"] is False
        assert snap["version"] is None
        assert snap["profile"] is None
        assert snap["unavailable"] is False

        # Every scope reports ungoverned + permits; none is governed.
        assert snap["scopes"], "expected a row per catalog scope"
        assert all(row["source"] == "ungoverned" for row in snap["scopes"])
        assert all(row["governed"] is False for row in snap["scopes"])

    def test_covers_every_catalog_scope_minus_aliases(self, profiles_dir):
        # The viewer iterates SCOPE_CATALOG (never a hardcoded list) so it stays
        # complete + auto-extends; the folders.* aliases are folded into
        # filesystem.* and must not appear as their own rows.
        _install_ceiling(None)
        snap = build_governance_policy_snapshot()

        expected = {s for s in SCOPE_CATALOG if s not in _SCOPE_ALIASES}
        assert {row["scope"] for row in snap["scopes"]} == expected
        assert not any(row["scope"].startswith("folders.") for row in snap["scopes"])


# ──────────────────────────────────────────────────────────────────────────
# A governing policy — per-archetype source + detail
# ──────────────────────────────────────────────────────────────────────────
class TestGoverningPolicy:
    @pytest.fixture
    def snapshot(self, profiles_dir):
        _install_ceiling(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "commands": {"mode": "deny", "deny": ["git push*", "*rm -rf /*"]},
                "tools": {"mode": "allow", "allow": ["read", "grep"]},
                "channels": {
                    "members": {"mode": "allow", "allow": ["slack"]},
                    "posture": {
                        "slack": {"allowed_enterprise_ids": {"mode": "allow", "allow": ["E0123"]}}
                    },
                },
                "sandbox": {"min_level": "cc"},
                "capabilities": {
                    "cron": {"enabled": False},
                    "spawn": {
                        "enabled": True,
                        "scopes": {"agents": {"mode": "allow", "allow": ["researcher"]}},
                    },
                },
            }
        )
        return build_governance_policy_snapshot()

    def test_has_policy_and_version(self, snapshot):
        assert snapshot["has_policy"] is True
        assert snapshot["version"] == 1

    def test_ruleset_allow_detail(self, snapshot):
        row = _by_scope(snapshot)["tools"]
        assert row["governed"] is True
        assert row["source"] == "policy"
        assert row["archetype"] == "ruleset"
        assert row["detail"]["mode"] == "allow"
        # POSTURE only: the count, never the entries (the rule contents are the
        # ceiling the agent is fenced from — see _serialize_ruleset).
        assert row["detail"]["allow_count"] == 2
        assert "allow" not in row["detail"]

    def test_ruleset_deny_detail(self, snapshot):
        row = _by_scope(snapshot)["commands"]
        assert row["source"] == "policy"
        assert row["detail"]["mode"] == "deny"
        assert row["detail"]["deny_count"] == 2
        # The raw deny patterns must NOT leak to the browser-facing endpoint.
        assert "deny" not in row["detail"]

    def test_detail_never_leaks_rule_contents(self, snapshot):
        # Security invariant (GPT HIGH): no serialized detail — at any nesting
        # depth — may carry the raw allow/deny entry LISTS. Walk every scope's
        # detail recursively and assert the string patterns never appear.
        SECRET = {"git push*", "*rm -rf /*", "read", "grep", "researcher", "slack", "E0123"}

        def _walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    # No key should carry a list of rule strings.
                    assert k not in ("allow", "deny"), f"raw rule list leaked under {k!r}"
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    assert v not in SECRET, f"raw rule value {v!r} leaked to browser"
                    _walk(v)

        for row in snapshot["scopes"]:
            _walk(row["detail"])

    def test_ordinal_detail_reports_floor(self, snapshot):
        row = _by_scope(snapshot)["sandbox.min_level"]
        assert row["archetype"] == "ordinal"
        assert row["detail"] == {"scale": "sandbox", "floor": "cc"}

    def test_capability_off_detail(self, snapshot):
        row = _by_scope(snapshot)["capabilities.cron"]
        assert row["archetype"] == "capability"
        assert row["governed"] is True
        assert row["detail"]["enabled"] is False

    def test_capability_on_with_inner_allowlist(self, snapshot):
        row = _by_scope(snapshot)["capabilities.spawn"]
        assert row["detail"]["enabled"] is True
        # The inner scope NAME (agents) is posture; the entry COUNT, not the
        # entries, is serialized.
        assert row["detail"]["inner"]["agents"]["allow_count"] == 1
        assert "allow" not in row["detail"]["inner"]["agents"]

    def test_scopedmap_members_and_posture(self, snapshot):
        row = _by_scope(snapshot)["channels"]
        assert row["archetype"] == "scopedmap"
        assert row["detail"]["members"]["allow_count"] == 1
        assert "allow" not in row["detail"]["members"]
        # The member id keys of `posture` are structural (which channels have a
        # posture), not secret rule contents — those stay.
        assert "slack" in row["detail"]["posture"]

    def test_ungoverned_scopes_remain_ungoverned(self, snapshot):
        # A scope the policy does not name (e.g. mcp / apps / messaging) stays
        # ungoverned and permits — only the named scopes flip to governed.
        by = _by_scope(snapshot)
        for scope in ("mcp", "apps", "capabilities.messaging", "filesystem.read"):
            assert by[scope]["governed"] is False
            assert by[scope]["source"] == "ungoverned"


# ──────────────────────────────────────────────────────────────────────────
# Host-surface profile resolution + intersection
# ──────────────────────────────────────────────────────────────────────────
class TestHostProfileIntersection:
    def test_host_bound_profile_intersects_policy(self, profiles_dir):
        _install_ceiling(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "tools": {"mode": "allow", "allow": ["read", "grep", "code"]},
                "sandbox": {"min_level": "cc"},
            }
        )
        # A profile bound to the HOST surface narrows tools + tightens sandbox.
        _write(
            profiles_dir,
            "host-tight",
            {
                "name": "host-tight",
                "bind": {"type": "surface", "id": "host"},
                "tools": {"mode": "allow", "allow": ["read"]},
                "sandbox": {"min_level": "strict"},
            },
        )
        snap = build_governance_policy_snapshot()
        by = _by_scope(snap)

        # The host-surface profile was resolved (proves HOST_SESSION_KEY is used).
        assert snap["profile"] == "host-tight"

        tools = by["tools"]
        assert tools["source"] == "policy+profile"
        # An allow∩allow that cannot flatten renders as an intersection view.
        assert tools["detail"]["mode"] == "intersect"
        assert len(tools["detail"]["components"]) == 2

        # Ordinal composes strictest-of → the profile's stricter "strict" wins.
        assert by["sandbox.min_level"]["detail"]["floor"] == "strict"

    def test_profile_only_scope_source_is_profile(self, profiles_dir):
        # Policy present but does NOT govern apps; a host profile does → source
        # is "profile" (profile-alone governs this scope).
        _install_ceiling({"version": 1, "boot": {"fail_closed": True}})
        _write(
            profiles_dir,
            "host-tight",
            {
                "name": "host-tight",
                "bind": {"type": "surface", "id": "host"},
                "apps": {"mode": "allow", "allow": ["auto-research"]},
            },
        )
        snap = build_governance_policy_snapshot()
        apps = _by_scope(snap)["apps"]
        assert apps["source"] == "profile"
        assert apps["governed"] is True
        assert apps["detail"]["allow_count"] == 1
        assert "allow" not in apps["detail"]


# ──────────────────────────────────────────────────────────────────────────
# Whose ceiling a row describes (host-surface vs install-wide)
# ──────────────────────────────────────────────────────────────────────────
class TestScopeAttribution:
    """A host-profile pin is ONE surface's posture, and must say so.

    The host profile legitimately disables capabilities the host process never
    performs (cron, messaging, spawn) while the surfaces that DO perform them
    enable those under their own profiles. Rendering such a row as an unqualified
    "Disabled by policy" reports a working feature as switched off, which is the
    defect these tests pin: ``scope_note`` distinguishes a host-only pin from a
    policy-wide one, and ``other_bound_surfaces`` names the surfaces that carry
    their own ceiling.
    """

    def test_host_profile_pin_is_marked_host_scoped(self, profiles_dir):
        # Exactly the shipped shape: a host profile that turns cron OFF because
        # the host process schedules nothing, with no policy at all.
        _install_ceiling(None)
        _write(
            profiles_dir,
            "host",
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "capabilities": {"cron": {"enabled": False}},
            },
        )
        row = _by_scope(build_governance_policy_snapshot())["capabilities.cron"]

        assert row["governed"] is True
        assert row["source"] == "profile"
        assert row["detail"]["enabled"] is False
        # The load-bearing assertion: the row is attributed to the host surface,
        # so the viewer cannot render it as an install-wide "off".
        assert row["scope_note"] == "host_profile"

    def test_policy_pin_is_marked_install_wide(self, profiles_dir):
        # A Level-1 ceiling DOES apply to every surface, so a policy-only row is
        # correctly reported as install-wide — the caveat must not over-apply.
        _install_ceiling(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"cron": {"enabled": False}},
            }
        )
        row = _by_scope(build_governance_policy_snapshot())["capabilities.cron"]

        assert row["source"] == "policy"
        assert row["scope_note"] == "policy_wide"

    def test_composed_row_is_policy_wide_when_policy_is_the_half_that_disables(
        self, profiles_dir
    ):
        """The inverse mislabel: a Level-1 ceiling must not read as host-only.

        ``CapabilityGate.enabled`` composes by AND, so a POLICY gate that is already
        off makes the capability off on EVERY surface no matter how permissive the
        profile is. Reporting that as host-scoped would under-claim an enterprise
        ceiling on a security panel — the same defect as the original, inverted.
        """
        _install_ceiling(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"cron": {"enabled": False}},
            }
        )
        # A permissive host profile ALSO governs the scope, so source is composed.
        _write(
            profiles_dir,
            "host",
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "capabilities": {"cron": {"enabled": True}},
            },
        )
        row = _by_scope(build_governance_policy_snapshot())["capabilities.cron"]

        assert row["source"] == "policy+profile"
        assert row["detail"]["enabled"] is False  # AND → off
        assert row["scope_note"] == "policy_wide"

    def test_composed_row_is_host_scoped(self, profiles_dir):
        # policy ∩ host-profile: the profile half still makes the RESULT specific
        # to the host surface, so it reports host_profile, not policy_wide.
        _install_ceiling(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"spawn": {"enabled": True}},
            }
        )
        _write(
            profiles_dir,
            "host",
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "capabilities": {"spawn": {"enabled": False}},
            },
        )
        row = _by_scope(build_governance_policy_snapshot())["capabilities.spawn"]

        assert row["source"] == "policy+profile"
        assert row["detail"]["enabled"] is False
        assert row["scope_note"] == "host_profile"

    def test_ungoverned_rows_carry_no_caveat(self, profiles_dir):
        _install_ceiling(None)
        snap = build_governance_policy_snapshot()
        assert all(row["scope_note"] == "" for row in snap["scopes"])

    def test_every_row_carries_the_field(self, profiles_dir):
        # The frontend switches on this value, so it must be present on EVERY row
        # (a missing key would silently take the install-wide branch).
        _install_ceiling({"version": 1, "boot": {"fail_closed": True}})
        snap = build_governance_policy_snapshot()
        assert snap["scopes"]
        for row in snap["scopes"]:
            assert "scope_note" in row, row["scope"]
            assert row["scope_note"] in ("", "host_profile", "policy_wide")

    def test_other_bound_surfaces_names_the_sibling_profiles(self, profiles_dir):
        # This is what makes a host-scoped row legible: cron is off for the HOST,
        # and cron-the-surface has its own profile where it is on.
        _install_ceiling(None)
        _write(
            profiles_dir,
            "host",
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "capabilities": {"cron": {"enabled": False}},
            },
        )
        _write(
            profiles_dir,
            "cron",
            {
                "name": "cron",
                "bind": {"type": "surface", "id": "cron"},
                "capabilities": {"cron": {"enabled": True}},
            },
        )
        snap = build_governance_policy_snapshot()

        # host is EXCLUDED (it is the surface being reported), cron is named.
        assert snap["other_bound_surfaces"] == ["cron"]
        assert "host" not in snap["other_bound_surfaces"]

    def test_other_bound_surfaces_excludes_app_and_task_binds(self, profiles_dir):
        # Only surface binds are surfaces; an app/task profile is a different axis
        # and naming it here would misdescribe the runtime.
        _install_ceiling(None)
        _write(
            profiles_dir,
            "app-x",
            {"name": "app-x", "bind": {"type": "app", "id": "some-app"}},
        )
        _write(
            profiles_dir,
            "task-y",
            {"name": "task-y", "bind": {"type": "task", "id": "some-task"}},
        )
        snap = build_governance_policy_snapshot()
        assert snap["other_bound_surfaces"] == []

    def test_unavailable_response_still_carries_the_field(self, profiles_dir, monkeypatch):
        # The fail-safe branch is a separate literal, so it needs its own check —
        # the frontend reads the key unconditionally.
        #
        # Patch the name BOUND IN THE HANDLER: security.py does `from ... import
        # resolve_active_scope`, so patching governance_profiles' own attribute
        # leaves the handler calling the original and the snapshot SUCCEEDS —
        # which would make this test pass vacuously against the success branch.
        import kiro_crew.dashboard.handlers.security as sec

        def _boom(*_a, **_k):
            raise RuntimeError("resolution glitch")

        monkeypatch.setattr(sec, "resolve_active_scope", _boom)
        snap = build_governance_policy_snapshot()
        assert snap["unavailable"] is True
        assert snap["other_bound_surfaces"] == []


# ──────────────────────────────────────────────────────────────────────────
# Endpoint behavior (GET-only, fail-safe for display)
# ──────────────────────────────────────────────────────────────────────────
class TestEndpoint:
    async def _call(self):
        # aiohttp request is unused by the handler (no body / match_info), so a
        # bare sentinel suffices — the handler only offloads the snapshot build.
        return await api_governance_policy(object())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_get_returns_json_snapshot(self, profiles_dir):
        _install_ceiling(None)
        resp = await self._call()
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["has_policy"] is False
        assert payload["unavailable"] is False
        assert all(row["source"] == "ungoverned" for row in payload["scopes"])

    @pytest.mark.asyncio
    async def test_display_fails_safe_on_resolution_error(self, profiles_dir, monkeypatch):
        # A governance-resolution error must NOT 500 the Security page — the
        # snapshot degrades to a well-formed "unavailable" response.
        import kiro_crew.dashboard.handlers.security as sec

        def _boom(*_a, **_k):
            raise RuntimeError("resolution glitch")

        monkeypatch.setattr(sec, "resolve_active_scope", _boom, raising=False)
        # Patch the symbol the function imports lazily inside its body.
        monkeypatch.setattr("kiro_crew.platform.governance_profiles.resolve_active_scope", _boom)
        resp = await self._call()
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["unavailable"] is True
        assert payload["scopes"] == []
        assert payload["has_policy"] is False
