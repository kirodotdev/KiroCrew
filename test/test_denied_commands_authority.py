"""Task 3 — platform/security_authority.py floor redefinition.

Verifies the deny floor is redefined for the user-configurable-denied-commands
feature: ``BASELINE_DENY`` is now empty, ``assert_security_floor`` keeps only the
structural (isinstance + ``@final``-override) guards, and
``PolicyAuthority.is_denied`` forwards a keyword-only ``denied_regexes`` to
``security.is_denied`` while overlay patterns keep flowing (ADD-only) through
``extra_patterns``.
"""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.platform import PlatformCompositionError
from kiro_crew.platform.security_authority import (
    BASELINE_DENY,
    PolicyAuthority,
    assert_security_floor,
)


class TestBaselineDenyRedefinition:
    """The static OSS deny floor is now empty."""

    def test_baseline_deny_is_empty(self) -> None:
        assert BASELINE_DENY == ()

    def test_effective_patterns_are_overlay_only(self) -> None:
        class _AddOverlay:
            def extra_deny_patterns(self):
                return ("*launch_missiles*",)

        authority = PolicyAuthority(overlay=_AddOverlay())
        # BASELINE_DENY is () so effective == overlay ∪ per-call extra.
        assert authority.effective_patterns() == ("*launch_missiles*",)
        assert authority.effective_patterns(("*extra*",)) == (
            "*launch_missiles*",
            "*extra*",
        )

    def test_no_git_publish_probe_symbol(self) -> None:
        import kiro_crew.platform.security_authority as mod

        assert not hasattr(mod, "_GIT_PUBLISH_PROBE")


class TestAssertSecurityFloor:
    """The floor guard keeps only the structural (ADD-only) guarantees."""

    def test_assert_floor_passes_default_authority(self) -> None:
        assert_security_floor(PolicyAuthority())  # no raise

    def test_assert_floor_rejects_non_authority(self) -> None:
        with pytest.raises(PlatformCompositionError):
            assert_security_floor(object())

    def test_final_override_guard_still_rejects_subclass_override(self) -> None:
        # @final is type-checker-only; a subclass overriding is_denied to
        # always-allow must be rejected at boot by the runtime guard.
        class _WeakeningAuthority(PolicyAuthority):
            def is_denied(  # type: ignore[override]
                self, tool_name, extra_patterns=None, *, denied_regexes=None
            ):
                return None

        with pytest.raises(PlatformCompositionError):
            assert_security_floor(_WeakeningAuthority())

    def test_final_override_guard_rejects_effective_patterns_override(self) -> None:
        class _WeakeningAuthority(PolicyAuthority):
            def effective_patterns(self, extra=()):  # type: ignore[override]
                return ()

        with pytest.raises(PlatformCompositionError):
            assert_security_floor(_WeakeningAuthority())

    def test_assert_floor_no_longer_probes_git_publish(self) -> None:
        # An overlay that adds nothing (empty floor) must still pass the floor
        # check: git-publish is now a disableable built-in enforced in
        # security.py, not a floor probe here.
        class _EmptyOverlay:
            def extra_deny_patterns(self):
                return ()

        assert_security_floor(PolicyAuthority(overlay=_EmptyOverlay()))  # no raise


class TestIsDeniedForwarding:
    """PolicyAuthority.is_denied forwards denied_regexes; overlay is ADD-only."""

    def test_is_denied_forwards_denied_regexes(self) -> None:
        calls: list[dict] = []

        def _fake_is_denied(
            tool_name, extra_patterns=None, *, denied_regexes=None, reason_notes=None
        ):
            calls.append(
                {
                    "tool_name": tool_name,
                    "extra_patterns": extra_patterns,
                    "denied_regexes": denied_regexes,
                    "reason_notes": reason_notes,
                }
            )
            return None

        orig = security.is_denied
        security.is_denied = _fake_is_denied  # type: ignore[assignment]
        try:
            authority = PolicyAuthority()
            authority.is_denied("some cmd", denied_regexes=["aws.*terminate.*"])
        finally:
            security.is_denied = orig  # type: ignore[assignment]

        assert calls == [
            {
                "tool_name": "some cmd",
                "extra_patterns": None,
                "denied_regexes": ["aws.*terminate.*"],
                "reason_notes": None,
            }
        ]

    def test_is_denied_forwards_reason_notes(self) -> None:
        """The operator-note map must reach the gate, or an annotated rule would
        silently fall back to showing its raw regex."""
        calls: list[dict] = []

        def _fake_is_denied(
            tool_name, extra_patterns=None, *, denied_regexes=None, reason_notes=None
        ):
            calls.append({"reason_notes": reason_notes})
            return None

        notes = {"find .*": "use -maxdepth, or rg/fd"}
        orig = security.is_denied
        security.is_denied = _fake_is_denied  # type: ignore[assignment]
        try:
            PolicyAuthority().is_denied(
                "some cmd", denied_regexes=["find .*"], reason_notes=notes
            )
        finally:
            security.is_denied = orig  # type: ignore[assignment]

        assert calls == [{"reason_notes": notes}]

    def test_default_denied_regexes_fails_closed_to_builtins(self) -> None:
        # With no denied_regexes the fail-closed default (all built-ins) applies.
        authority = PolicyAuthority()
        assert authority.is_denied("aws ec2 terminate-instances i-x") is not None
        assert authority.is_denied("ls -la") is None

    def test_empty_denied_regexes_honours_the_git_publish_opt_out(self) -> None:
        # The git-publish floor's GATED branches are consulted against the
        # effective set, so an empty regex tier (disable_all + no pins) disables
        # them too — a toggle the UI offers must not be a silent no-op.
        authority = PolicyAuthority()
        assert authority.is_denied("git push origin main", denied_regexes=[]) is None
        # ``None`` fails closed to all built-ins enabled, so the default denies.
        assert authority.is_denied("git push origin main") is not None

    def test_empty_denied_regexes_still_blocks_unverifiable_push(self) -> None:
        # The anti-obfuscation branch carries no per-rule gate: a push target the
        # shell fuses together cannot be checked against a branch name at all, so
        # no opt-out may reach it.
        authority = PolicyAuthority()
        assert (
            authority.is_denied("git push origin ma$(echo)in", denied_regexes=[]) is not None
        )

    def test_overlay_never_filtered_by_denied_regexes(self) -> None:
        # The ADD-only overlay flows through extra_patterns and is applied even
        # when the regex tier is emptied.
        class _AddOverlay:
            def extra_deny_patterns(self):
                return ("*launch_missiles*",)

        authority = PolicyAuthority(overlay=_AddOverlay())
        assert authority.is_denied("please launch_missiles now", denied_regexes=[]) is not None


class TestSynthesizedTargetOverlayFloor:
    """The ADD-only overlay keeps its command semantics for a synthesized target.

    An overlay pattern is opaque enterprise policy, and one restricting a filesystem
    SCOPE is spelled as bare path text, so it is evaluated through the unscoped
    ``is_denied`` rather than travelling into the synthesized tier.
    """

    TARGET = "file-search path=/mnt/forbidden-share max_depth=3"

    class _ScopeOverlay:
        def extra_deny_patterns(self):
            return ("*forbidden-share*",)

    def test_a_path_only_overlay_pattern_still_denies_a_search(self) -> None:
        authority = PolicyAuthority(overlay=self._ScopeOverlay())
        assert authority.is_denied_synthesized_target(self.TARGET) is not None

    def test_the_overlay_applies_with_no_participating_patterns(self) -> None:
        # Its ADD-only property: nothing the operator does or omits reaches the overlay.
        authority = PolicyAuthority(overlay=self._ScopeOverlay())
        assert authority.is_denied_synthesized_target(self.TARGET, []) is not None

    def test_an_unrelated_overlay_does_not_reinstate_the_collision(self) -> None:
        # The overlay pass must forward an EMPTY regex tier, not `None`: `None` fails
        # closed to every built-in, which would evaluate the whole shipped catalogue
        # against the synthesized target and undo the tier.
        class _UnrelatedOverlay:
            def extra_deny_patterns(self):
                return ("*launch_missiles*",)

        authority = PolicyAuthority(overlay=_UnrelatedOverlay())
        effective = list(
            security.compute_effective_denied(security.BUILTIN_DENIED_RULES, (), False, (), ())
        )
        assert "mkfs.*" in effective  # premise
        assert authority.is_denied_synthesized_target("file-search path=/srv/mkfs-tests") is None

    def test_the_boot_guard_rejects_overriding_the_synthesized_decision(self) -> None:
        # `is_denied` is not the only decision path; a subclass always-allowing every
        # file-search call must not pass boot either.
        class _WeakeningAuthority(PolicyAuthority):
            def is_denied_synthesized_target(  # type: ignore[override]
                self, target, patterns=None, *, extra_patterns=None, reason_notes=None
            ):
                return None

        with pytest.raises(PlatformCompositionError):
            assert_security_floor(_WeakeningAuthority())
