"""Tests for the Composed Platform Providers contract (kiro_crew.platform)."""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import (
    BASELINE_DENY,
    CONTRACT_VERSION,
    PROFILE_ENTERPRISE,
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

        assert ctx.embeddings.registry_model() == embeddings._MODEL_ID
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
        """``BASELINE_DENY`` is now ``()`` — the built-ins are no longer the
        compiled floor.  A default ``PolicyAuthority`` (no overlay) therefore
        contributes an EMPTY floor via ``effective_patterns``, but its
        ``is_denied`` still fails closed to the full built-in rule set when the
        caller passes ``denied_regexes=None`` (the hooks gate always passes the
        resolved effective set, but the fail-closed default must stay safe).
        """
        authority = PolicyAuthority()
        # Floor is empty: built-ins live in the disableable regex tier now.
        assert authority.effective_patterns() == ()
        # …but the decision still fails closed to built-ins for a real
        # destructive command, and allows a benign read-only one.
        assert authority.is_denied("aws ec2 terminate-instances --instance-ids i-1") is not None
        assert authority.is_denied("ls -la") is None


class TestProfileResolution:
    def test_env_override_standalone(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
        assert resolve_profile(cfg, entry_points=[object()]) == PROFILE_STANDALONE

    def test_env_override_enterprise(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PROFILE", "enterprise")
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_ENTERPRISE

    def test_env_override_legacy_alias(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        # A legacy edition value still resolves to the enterprise profile
        # (back-compat alias) rather than falling back to standalone.
        monkeypatch.setenv("KIROCREW_PROFILE", "amazon")
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_ENTERPRISE

    def test_unknown_env_falls_back_to_standalone(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        # An unknown KIROCREW_PROFILE value returns standalone immediately,
        # before any identity/entry-point signal is consulted.
        monkeypatch.setenv("KIROCREW_PROFILE", "bogus")
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_STANDALONE

    def test_entry_points_take_precedence_over_marker(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        # A present companion (entry points) is the authoritative signal and is
        # checked BEFORE the SSO-marker stat — no subprocess is spawned.
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        assert resolve_profile(cfg, entry_points=[object()]) == PROFILE_ENTERPRISE

    def test_marker_stat_ignored_without_probe_optin(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        # A stray SSO marker must NOT force the enterprise profile by default:
        # the public edition has no companion to compose, so forcing enterprise
        # would brick every command at boot. The heuristic is opt-in only.
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        monkeypatch.delenv("KIROCREW_SSO_PROFILE_PROBE", raising=False)
        monkeypatch.setattr("kiro_crew.platform.profile.Path.home", lambda: _FakeHome(True))
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_STANDALONE

    def test_marker_stat_triggers_enterprise_when_probe_opted_in(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        # With the opt-in set (a companion's managed launcher), a marker-present
        # host with no companion resolves enterprise so discovery fails closed
        # (rather than running open defaults).
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        monkeypatch.setenv("KIROCREW_SSO_PROFILE_PROBE", "1")
        monkeypatch.setattr("kiro_crew.platform.profile.Path.home", lambda: _FakeHome(True))
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_ENTERPRISE

    def test_legacy_probe_env_still_triggers_enterprise(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        # An already-deployed managed launcher still sets the LEGACY probe env
        # var. It must keep triggering the fail-closed marker check — dropping it
        # would let a marker-present enterprise host with a missing/broken
        # companion resolve standalone and boot WITHOUT the security overlay.
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        monkeypatch.delenv("KIROCREW_SSO_PROFILE_PROBE", raising=False)
        monkeypatch.setenv("KIROCREW_MIDWAY_PROFILE_PROBE", "1")
        monkeypatch.setattr("kiro_crew.platform.profile.Path.home", lambda: _FakeHome(True))
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_ENTERPRISE

    def test_no_signals_is_standalone(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PROFILE", raising=False)
        monkeypatch.setenv("KIROCREW_SSO_PROFILE_PROBE", "1")
        monkeypatch.setattr("kiro_crew.platform.profile.Path.home", lambda: _FakeHome(False))
        assert resolve_profile(cfg, entry_points=[]) == PROFILE_STANDALONE


class _FakeHome:
    """A fake home dir whose ``/ <marker>`` existence is controllable."""

    def __init__(self, marker_exists: bool):
        self._exists = marker_exists

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

    def test_bootstrap_enterprise_without_companion_fails_closed(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_PROFILE", "enterprise")
        # No companion entry point installed → must raise (fail-closed).
        monkeypatch.setattr("kiro_crew.platform.bootstrap.plugin_entry_points", lambda: [])
        monkeypatch.setattr("kiro_crew.platform.discovery.plugin_entry_points", lambda: [])
        with pytest.raises(PlatformCompositionError):
            bootstrap_context(cfg)

    def test_contract_version_mismatch_rejected(self, cfg: KiroCrewConfig, monkeypatch) -> None:
        import dataclasses

        from kiro_crew.platform import bootstrap as bootstrap_mod

        bad = dataclasses.replace(
            build_default_context(cfg, profile=PROFILE_ENTERPRISE),
            contract_version=CONTRACT_VERSION + 99,
        )
        monkeypatch.setenv("KIROCREW_PROFILE", "enterprise")
        monkeypatch.setattr(bootstrap_mod, "plugin_entry_points", lambda: [object()])
        monkeypatch.setattr(bootstrap_mod, "discover_companion_context", lambda profile, cfg: bad)
        with pytest.raises(PlatformCompositionError):
            bootstrap_context(cfg)

    def test_none_companion_on_enterprise_fails_closed(
        self, cfg: KiroCrewConfig, monkeypatch
    ) -> None:
        # Defense in depth: if discovery ever returns None for a non-standalone
        # profile, bootstrap must STILL refuse to boot rather than install an
        # enterprise-labeled context with open defaults.
        from kiro_crew.platform import bootstrap as bootstrap_mod

        monkeypatch.setenv("KIROCREW_PROFILE", "enterprise")
        monkeypatch.setattr(bootstrap_mod, "plugin_entry_points", lambda: [object()])
        monkeypatch.setattr(bootstrap_mod, "discover_companion_context", lambda profile, cfg: None)
        with pytest.raises(PlatformCompositionError):
            bootstrap_context(cfg)


class TestRedactLogViaContext:
    """``redact_log_via_context`` -- the spelling a gate-side LOG line must use.

    Two properties, and they are in tension by design: it must reach the
    companion's redaction (so a log line is not scanned with the weaker OSS
    baseline), and it must never raise (a log site cannot afford the fail-closed
    raise that an egress sink wants -- see the helper's docstring for the MCP
    stderr drain that a raise would wedge).
    """

    #: A shape the OSS baseline knows nothing about; only a companion policy
    #: scrubs it. Mirrors ``_EnterpriseCredentialPolicy`` in the CPP wiring
    #: tests, so both suites describe the companion's extra reach the same way.
    COMPANION_SHAPE = "SSO-COOKIE"

    #: Matched by the baseline's own credential patterns, so it proves the
    #: companion pass is applied ON TOP of the baseline rather than instead.
    BASELINE_SHAPE = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _install(policy, cfg: KiroCrewConfig):
        import dataclasses

        from kiro_crew.platform import set_context

        set_context(
            dataclasses.replace(
                build_default_context(cfg, profile=PROFILE_ENTERPRISE),
                credentials=policy,
            )
        )

    @pytest.fixture(autouse=True)
    def _restore_context(self):
        """Never leave a stub context installed for the rest of the session."""
        from kiro_crew.platform import reset_context

        yield
        reset_context()

    def test_companion_shape_is_scrubbed_on_top_of_the_baseline(self, cfg: KiroCrewConfig) -> None:
        from kiro_crew.platform.context import redact_log_via_context

        class _Policy:
            def redact(self, text: str) -> str:
                return security.redact(text).replace(
                    TestRedactLogViaContext.COMPANION_SHAPE, "[REDACTED-SSO]"
                )

        self._install(_Policy(), cfg)
        out = redact_log_via_context(
            f"boot failed {self.COMPANION_SHAPE} using {self.BASELINE_SHAPE}"
        )
        # The companion's extra reach is the whole reason this helper exists.
        assert self.COMPANION_SHAPE not in out
        # ...and it did not come at the cost of the baseline pass.
        assert self.BASELINE_SHAPE not in out

    def test_composition_failure_withholds_the_text_instead_of_raising(
        self, cfg: KiroCrewConfig
    ) -> None:
        from kiro_crew.platform.context import (
            LOG_WITHHELD_PLACEHOLDER,
            redact_log_via_context,
        )

        class _Unprovable:
            def redact(self, text: str) -> str:
                raise PlatformCompositionError("companion could not be composed")

        self._install(_Unprovable(), cfg)
        out = redact_log_via_context(f"boot failed {self.BASELINE_SHAPE}")
        assert out == LOG_WITHHELD_PLACEHOLDER
        # The point of withholding: nothing unscanned survives into the line.
        assert self.BASELINE_SHAPE not in out

    def test_the_bare_shim_still_raises_so_egress_stays_fail_closed(
        self, cfg: KiroCrewConfig
    ) -> None:
        """The log spelling must not have softened the egress spelling.

        Both read the same policy, so a caller could have been "fixed" by making
        ``redact_via_context`` stop raising -- which would silently convert every
        egress sink to fail-open. Pin the difference to the call site.
        """
        from kiro_crew.platform.context import redact_via_context

        class _Unprovable:
            def redact(self, text: str) -> str:
                raise PlatformCompositionError("companion could not be composed")

        self._install(_Unprovable(), cfg)
        with pytest.raises(PlatformCompositionError):
            redact_via_context("anything")

    def test_a_transient_policy_error_still_degrades_to_the_baseline(
        self, cfg: KiroCrewConfig
    ) -> None:
        """Only a composition failure withholds; a flaky adapter must not.

        Withholding every transient error would silently blank operational logs
        on a host whose companion is merely misbehaving, so the inherited
        degrade-to-baseline path has to survive underneath the new catch.
        """
        from kiro_crew.platform.context import (
            LOG_WITHHELD_PLACEHOLDER,
            redact_log_via_context,
        )

        class _Flaky:
            def redact(self, text: str) -> str:
                raise RuntimeError("adapter blew up")

        self._install(_Flaky(), cfg)
        out = redact_log_via_context(f"boot failed {self.BASELINE_SHAPE} here")
        assert out != LOG_WITHHELD_PLACEHOLDER
        assert self.BASELINE_SHAPE not in out
        # Text still present around the scrubbed credential -- degraded, not blanked.
        assert "boot failed" in out

    def test_no_installed_context_keeps_the_baseline_without_resolving_one(self) -> None:
        """No context at all means baseline, NOT a withheld line.

        The two no-companion states are different and must not be conflated. With
        nothing installed there is no evidence a companion exists, the full OSS
        pass still runs, and withholding would DESTROY diagnostics in a process
        that deliberately never composes one -- ``mcp_gateway.gatewayd`` is
        exactly that process. Withholding is reserved for the case where a
        context IS installed and its policy failed, which is the only state where
        the baseline would be a real downgrade.

        Also pins the no-I/O guarantee: resolution must not be attempted at all,
        since ``current_context()`` never memoizes its fail-closed verdict on a
        non-standalone profile.
        """
        from kiro_crew.platform import context as context_mod
        from kiro_crew.platform.context import (
            LOG_WITHHELD_PLACEHOLDER,
            redact_log_via_context,
            reset_context,
        )

        reset_context()

        def _explode() -> None:
            raise AssertionError("redact_log_via_context resolved a context")

        original = context_mod.current_context
        context_mod.current_context = _explode  # type: ignore[assignment]
        try:
            out = redact_log_via_context(f"boot failed {self.BASELINE_SHAPE} here")
        finally:
            context_mod.current_context = original  # type: ignore[assignment]
        # Diagnostics preserved, credential still scrubbed by the baseline pass.
        assert out != LOG_WITHHELD_PLACEHOLDER
        assert self.BASELINE_SHAPE not in out
        assert "boot failed" in out

    #: Gate-side log/audit sites converged onto the context spelling, each running
    #: inside the composition process (the gateway) where a companion policy is
    #: actually reachable. Deliberately NOT a list of every redaction call site --
    #: `security_posture.NON_EGRESS_REDACTION_MODULES` owns that axis. This one
    #: only pins that a site already converged cannot silently drift back, which
    #: is how the class grew in the first place. A site born on the baseline
    #: tomorrow is a different question, and a list cannot answer it:
    #: `test_security_posture.TestGateSideLogRedactorSpelling` owns that half by
    #: scanning for the property instead.
    CONVERGED_LOG_SITES = (
        "platform/update_provider.py",
        "task_planner.py",
        "name_grant.py",
    )

    def test_converged_log_sites_do_not_drift_back_to_the_baseline(self) -> None:
        """Each converged site must CALL the helper and not the raw components.

        An omission-detecting check, because the failure mode is silent: the two
        spellings look equally deliberate at a call site, and nothing about a
        `redact_credentials`/`redact_exfiltration_urls` pair reads as wrong until
        someone asks which process it runs in.

        Both halves are load-bearing, and the first is not enough on its own -- a
        plain substring search for the helper's NAME is satisfied by the lingering
        import even after the call has drifted back, so this counts CALL sites
        (paren form, import lines excluded) and separately requires the component
        spelling to be absent.
        """
        import pathlib

        import kiro_crew

        root = pathlib.Path(kiro_crew.__file__).parent
        for rel in self.CONVERGED_LOG_SITES:
            lines = (root / rel).read_text(encoding="utf-8").splitlines()
            body = [ln for ln in lines if not ln.lstrip().startswith(("import ", "from "))]
            calls = sum(1 for ln in body if "redact_log_via_context(" in ln)
            assert calls >= 1, (
                f"{rel} no longer CALLS redact_log_via_context (an import alone does not "
                "count); if this site intentionally moved back to the baseline, say why "
                "at the call site and drop it from CONVERGED_LOG_SITES"
            )
            leftovers = [
                ln.strip()
                for ln in body
                if "redact_credentials(" in ln or "redact_exfiltration_urls(" in ln
            ]
            assert not leftovers, (
                f"{rel} still reaches the baseline components directly: {leftovers}. "
                "A converged site routes its gate-side text through the context spelling, "
                "so a companion host is not scanned with the weaker pass."
            )
