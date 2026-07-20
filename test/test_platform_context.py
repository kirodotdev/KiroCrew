"""Tests for the Composed Platform Providers contract (kiro_crew.platform)."""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import (
    BASELINE_DENY,
    CONTRACT_VERSION,
    PROFILE_AMAZON,
    PROFILE_STANDALONE,
    PlatformCompositionError,
    PolicyAuthority,
    assert_security_floor,
    bootstrap_context,
    build_default_context,
    resolve_profile,
)
from kiro_crew.platform.context import PlatformContext


@pytest.fixture
def cfg() -> KiroCrewConfig:
    return KiroCrewConfig()


class TestDefaultContext:
    """The standalone edition composes an all-defaults context unchanged."""

    def test_build_default_context_is_standalone(self, cfg: KiroCrewConfig) -> None:
        ctx = build_default_context(cfg)
        assert isinstance(ctx, PlatformContext)
        assert ctx.profile == PROFILE_STANDALONE
        assert ctx.contract_version == CONTRACT_VERSION
        assert ctx.cfg is cfg

    def test_default_adapters_match_legacy_behavior(self, cfg: KiroCrewConfig) -> None:
        ctx = build_default_context(cfg)
        # Each Default* adapter reproduces today's module-level value.
        from kiro_crew import agent, embeddings, sandbox
        from kiro_crew.apps import registry

        assert ctx.embeddings.registry_model() == embeddings._OLLAMA_MODEL
        assert ctx.sandbox.strict_dirs() == list(sandbox._STRICT_DIRS)
        assert ctx.sandbox.cc_dirs() == list(sandbox._CC_DIRS)
        assert set(ctx.agent_runtime.managed_mcp_servers()) == set(agent._MANAGED_MCP_SERVERS)
        assert ctx.agent_executable.resolve_executable("/usr/bin/kiro-cli") == "/usr/bin/kiro-cli"
        assert ctx.registry.public_git_hosts() == registry._PUBLIC_GIT_HOSTS
        assert ctx.tunnel.enabled() is False
        assert ctx.telemetry.frontend_rum_config() is None
        assert ctx.feature_apps == ()

    def test_default_security_is_baseline_only(self, cfg: KiroCrewConfig) -> None:
        ctx = build_default_context(cfg)
        assert isinstance(ctx.security, PolicyAuthority)
        assert set(ctx.security.effective_patterns()) == set(BASELINE_DENY)

    def test_default_credential_redaction_delegates(self, cfg: KiroCrewConfig) -> None:
        ctx = build_default_context(cfg)
        text = "key AKIAIOSFODNN7EXAMPLE here"
        assert ctx.credentials.redact(text) == security.redact(text)


class TestPolicyAuthorityAddOnly:
    """The deny floor is ADD-only: an overlay can add but never weaken."""

    def test_overlay_adds_patterns(self) -> None:
        class _AddOverlay:
            def extra_deny_patterns(self):
                return ("*launch_missiles*",)

        authority = PolicyAuthority(overlay=_AddOverlay())
        eff = set(authority.effective_patterns())
        # baseline preserved …
        assert set(BASELINE_DENY) <= eff
        # … and the overlay pattern is added.
        assert "*launch_missiles*" in eff
        assert authority.is_denied("please launch_missiles now") is not None

    def test_overlay_cannot_remove_baseline(self) -> None:
        # An overlay that returns () cannot shrink the baseline — union only.
        class _EmptyOverlay:
            def extra_deny_patterns(self):
                return ()

        authority = PolicyAuthority(overlay=_EmptyOverlay())
        assert set(BASELINE_DENY) <= set(authority.effective_patterns())

    def test_is_denied_and_effective_patterns_are_final(self) -> None:
        # Subclassing to override the decision must be impossible at type-check
        # time; at runtime the @final methods still resolve to the base impl.
        assert "is_denied" in PolicyAuthority.__dict__
        assert "effective_patterns" in PolicyAuthority.__dict__

    def test_assert_security_floor_rejects_non_authority(self) -> None:
        with pytest.raises(PlatformCompositionError):
            assert_security_floor(object())

    def test_assert_security_floor_accepts_baseline(self) -> None:
        assert_security_floor(PolicyAuthority())  # no raise

    def test_assert_security_floor_rejects_runtime_override(self) -> None:
        # @final is type-checker-only; a subclass that overrides is_denied to
        # always-allow while keeping effective_patterns intact would pass the
        # superset check. The runtime guard must reject it (fail-closed).
        class _WeakeningAuthority(PolicyAuthority):
            def is_denied(self, tool_name, extra_patterns=None):  # type: ignore[override]
                return None  # allow everything — must be rejected at boot

        with pytest.raises(PlatformCompositionError):
            assert_security_floor(_WeakeningAuthority())

    def test_baseline_deny_still_blocks_known_patterns(self) -> None:
        authority = PolicyAuthority()
        assert authority.is_denied("get_secret_foo") is not None
        assert authority.is_denied("ls -la") is None


class TestProfileResolution:
    def test_env_override_standalone(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
        assert resolve_profile(cfg, entry_points=[object()]) == PROFILE_STANDALONE

    def test_env_override_amazon(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PROFILE", "amazon")
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_AMAZON

    def test_unknown_env_falls_back_to_standalone(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        # An unknown KIROCREW_PROFILE value returns standalone immediately,
        # before any identity/entry-point signal is consulted.
        monkeypatch.setenv("KIROCREW_PROFILE", "bogus")
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_STANDALONE

    def test_entry_points_take_precedence_over_midway(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        # A present companion (entry points) is the authoritative signal and is
        # checked BEFORE the ~/.midway stat — no subprocess is spawned.
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        assert resolve_profile(cfg, entry_points=[object()]) == PROFILE_AMAZON

    def test_midway_stat_ignored_without_probe_optin(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        # A stray ~/.midway must NOT force the amazon profile by default: the
        # public edition has no companion to compose, so forcing amazon would
        # brick every command at boot. The identity heuristic is opt-in only.
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        monkeypatch.delenv("KIROCREW_MIDWAY_PROFILE_PROBE", raising=False)
        monkeypatch.setattr("kiro_crew.platform.profile.Path.home", lambda: _FakeHome(True))
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_STANDALONE

    def test_midway_stat_triggers_amazon_when_probe_opted_in(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        # With the opt-in set (the companion's managed launcher), a ~/.midway
        # host with no companion resolves amazon so discovery fails closed
        # (rather than running open defaults).
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        monkeypatch.setenv("KIROCREW_MIDWAY_PROFILE_PROBE", "1")
        monkeypatch.setattr("kiro_crew.platform.profile.Path.home", lambda: _FakeHome(True))
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_AMAZON

    def test_no_signals_is_standalone(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        monkeypatch.setenv("KIROCREW_MIDWAY_PROFILE_PROBE", "1")
        monkeypatch.setattr("kiro_crew.platform.profile.Path.home", lambda: _FakeHome(False))
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_STANDALONE


class _FakeHome:
    """A fake home dir whose ``/ ".midway"`` existence is controllable."""

    def __init__(self, midway_exists: bool):
        self._exists = midway_exists

    def __truediv__(self, _other):
        exists = self._exists

        class _Path:
            def exists(self):
                return exists

        return _Path()


class TestBootstrapAndDiscovery:
    def test_bootstrap_standalone(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
        ctx = bootstrap_context(cfg)
        assert ctx.profile == PROFILE_STANDALONE
        # current_context() now returns this context.
        from kiro_crew.platform import current_context

        assert current_context() is ctx

    def test_bootstrap_amazon_without_companion_fails_closed(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_PROFILE", "amazon")
        # No companion entry point installed → must raise (fail-closed).
        monkeypatch.setattr("kiro_crew.platform.bootstrap.plugin_entry_points", lambda: [])
        monkeypatch.setattr("kiro_crew.platform.discovery.plugin_entry_points", lambda: [])
        with pytest.raises(PlatformCompositionError):
            bootstrap_context(cfg)

    def test_contract_version_mismatch_rejected(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        import dataclasses

        from kiro_crew.platform import bootstrap as bootstrap_mod

        bad = dataclasses.replace(
            build_default_context(cfg, profile=PROFILE_AMAZON),
            contract_version=CONTRACT_VERSION + 99,
        )
        monkeypatch.setenv("KIROCREW_PROFILE", "amazon")
        monkeypatch.setattr(bootstrap_mod, "plugin_entry_points", lambda: [object()])
        monkeypatch.setattr(bootstrap_mod, "discover_companion_context", lambda profile, cfg: bad)
        with pytest.raises(PlatformCompositionError):
            bootstrap_context(cfg)

    def test_none_companion_on_amazon_fails_closed(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        # Defense in depth: if discovery ever returns None for a non-standalone
        # profile, bootstrap must STILL refuse to boot rather than install an
        # amazon-labeled context with open defaults.
        from kiro_crew.platform import bootstrap as bootstrap_mod

        monkeypatch.setenv("KIROCREW_PROFILE", "amazon")
        monkeypatch.setattr(bootstrap_mod, "plugin_entry_points", lambda: [object()])
        monkeypatch.setattr(bootstrap_mod, "discover_companion_context", lambda profile, cfg: None)
        with pytest.raises(PlatformCompositionError):
            bootstrap_context(cfg)
