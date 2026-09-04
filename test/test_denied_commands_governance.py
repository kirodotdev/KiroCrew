"""Task 2 — governance command force-pins (purely additive pin accessors).

Covers ``COMMANDS_SCOPE``, ``GovernanceCeiling.pinned_command_patterns``,
``Profile.pinned_command_patterns``, the private ``_command_deny_patterns``
helper, and the ``resolve_pinned_commands`` deduped-union resolver.
"""

from __future__ import annotations

from kiro_crew.platform import governance as gov


def _ceiling(commands_control: object) -> gov.GovernanceCeiling:
    """Build a minimal ceiling whose ``commands`` scope carries *control*."""
    return gov.GovernanceCeiling(
        version=gov.POLICY_VERSION,
        boot=gov.BootControls(),
        controls={gov.COMMANDS_SCOPE: commands_control},
    )


def _profile(commands_control: object) -> gov.Profile:
    return gov.Profile(name="p", controls={gov.COMMANDS_SCOPE: commands_control})


class TestCommandsScopeConstant:
    def test_commands_scope_value(self) -> None:
        assert gov.COMMANDS_SCOPE == "commands"

    def test_public_names_exported(self) -> None:
        assert "COMMANDS_SCOPE" in gov.__all__
        assert "resolve_pinned_commands" in gov.__all__


class TestDenyModeCommandsCeilingYieldsPins:
    def test_deny_mode_commands_ceiling_yields_pins(self) -> None:
        ruleset = gov.ScopedRuleset(
            mode=gov.MODE_DENY, deny=("*rm -rf /*", "*mkfs*"), matcher="command"
        )
        ceiling = _ceiling(ruleset)
        assert ceiling.pinned_command_patterns() == ("*rm -rf /*", "*mkfs*")

    def test_profile_deny_mode_yields_pins(self) -> None:
        ruleset = gov.ScopedRuleset(mode=gov.MODE_DENY, deny=("*dd if=*",), matcher="command")
        assert _profile(ruleset).pinned_command_patterns() == ("*dd if=*",)


class TestAllowModeCommandsYieldsNoPins:
    def test_allow_mode_commands_yields_no_pins(self) -> None:
        ruleset = gov.ScopedRuleset(mode=gov.MODE_ALLOW, allow=("ls*", "cat*"), matcher="command")
        assert _ceiling(ruleset).pinned_command_patterns() == ()

    def test_missing_commands_scope_yields_no_pins(self) -> None:
        ceiling = gov.GovernanceCeiling(
            version=gov.POLICY_VERSION, boot=gov.BootControls(), controls={}
        )
        assert ceiling.pinned_command_patterns() == ()

    def test_command_deny_patterns_non_ruleset_returns_empty(self) -> None:
        # An _AndRuleset (only arises from allow-mode combination) is not a
        # ScopedRuleset → no force-deny pins.
        base = gov.ScopedRuleset(mode=gov.MODE_ALLOW, allow=("ls*",), matcher="command")
        combined = gov._AndRuleset(base, base)
        assert gov._command_deny_patterns(combined) == ()

    def test_command_deny_patterns_none_returns_empty(self) -> None:
        assert gov._command_deny_patterns(None) == ()


class TestResolvePinnedCommandsUnionsCeilingAndProfile:
    def test_resolve_pinned_commands_unions_ceiling_and_profile(self) -> None:
        ceiling = _ceiling(
            gov.ScopedRuleset(mode=gov.MODE_DENY, deny=("*rm -rf /*",), matcher="command")
        )
        profile = _profile(
            gov.ScopedRuleset(mode=gov.MODE_DENY, deny=("*mkfs*",), matcher="command")
        )
        assert gov.resolve_pinned_commands(ceiling, profile) == ("*rm -rf /*", "*mkfs*")

    def test_resolve_dedups_preserving_first_seen_order(self) -> None:
        ceiling = _ceiling(
            gov.ScopedRuleset(mode=gov.MODE_DENY, deny=("*rm -rf /*", "*mkfs*"), matcher="command")
        )
        profile = _profile(
            gov.ScopedRuleset(mode=gov.MODE_DENY, deny=("*mkfs*", "*dd if=*"), matcher="command")
        )
        assert gov.resolve_pinned_commands(ceiling, profile) == (
            "*rm -rf /*",
            "*mkfs*",
            "*dd if=*",
        )

    def test_resolve_ceiling_only(self) -> None:
        ceiling = _ceiling(
            gov.ScopedRuleset(mode=gov.MODE_DENY, deny=("*shutdown*",), matcher="command")
        )
        assert gov.resolve_pinned_commands(ceiling) == ("*shutdown*",)

    def test_resolve_profile_only(self) -> None:
        profile = _profile(
            gov.ScopedRuleset(mode=gov.MODE_DENY, deny=("*reboot*",), matcher="command")
        )
        assert gov.resolve_pinned_commands(None, profile) == ("*reboot*",)


class TestSnapshotReflectsProfilePins:
    """The Settings > Security snapshot must mark a rule locked if ANY profile
    pins it — not only the Level-1 ceiling.

    Regression: ``pinned_builtin_command_ids`` resolved only the ceiling, so a
    profile-pinned rule rendered as freely disableable while the per-session gate
    (which resolves the bound profile) still denied it — a confusing no-op
    opt-out.
    """

    def test_all_profile_pinned_commands_unions_loaded_profiles(self, monkeypatch) -> None:
        from kiro_crew.platform import governance_profiles as gp

        rm_rf_root = "aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3(?:\\s+--?[a-z-]+"
        prof = gov.Profile(
            name="p",
            controls={
                gov.COMMANDS_SCOPE: gov.ScopedRuleset(
                    mode=gov.MODE_DENY, deny=(rm_rf_root,), matcher="command"
                )
            },
        )

        class _Store:
            def all_profiles(self):
                return [prof]

        monkeypatch.setattr(gp, "_STORE", _Store())
        assert gp.all_profile_pinned_commands() == (rm_rf_root,)

    def test_snapshot_locks_profile_pinned_builtin(self, monkeypatch) -> None:
        # Pin a REAL built-in pattern via a profile; the snapshot must show that
        # rule as pinned + enabled and set governance_locked, even with no
        # Level-1 ceiling.
        from kiro_crew.platform import governance_profiles as gp
        from kiro_crew.security import BUILTIN_DENIED_RULES

        target = BUILTIN_DENIED_RULES[0]
        prof = gov.Profile(
            name="p",
            controls={
                gov.COMMANDS_SCOPE: gov.ScopedRuleset(
                    mode=gov.MODE_DENY, deny=(target.pattern,), matcher="command"
                )
            },
        )

        class _Store:
            def all_profiles(self):
                return [prof]

        monkeypatch.setattr(gp, "_STORE", _Store())

        # The SNAPSHOT accessor unions all-profile pins; the ENFORCEMENT accessor
        # (ceiling-only) must NOT — a profile-A pin cannot force-enforce globally.
        from kiro_crew.security import (
            pinned_builtin_command_ids,
            pinned_builtin_command_ids_for_snapshot,
        )

        assert target.id in pinned_builtin_command_ids_for_snapshot()
        assert target.id not in pinned_builtin_command_ids()  # ceiling-only, no profile


class TestUngovernedReturnsEmpty:
    def test_ungoverned_returns_empty(self) -> None:
        assert gov.resolve_pinned_commands(None, None) == ()

    def test_ceiling_without_commands_scope_returns_empty(self) -> None:
        ceiling = gov.GovernanceCeiling(
            version=gov.POLICY_VERSION, boot=gov.BootControls(), controls={}
        )
        profile = gov.Profile(name="p", controls={})
        assert gov.resolve_pinned_commands(ceiling, profile) == ()
