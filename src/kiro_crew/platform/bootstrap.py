"""Boot-time composition root for the public (standalone) edition.

``build_default_context`` assembles a :class:`PlatformContext` whose every
interface field is a ``Default*`` adapter — i.e. today's open-source behavior.
``bootstrap_context`` is the single entry point boot calls: it builds the
default context, resolves the profile, and (for a non-standalone profile) swaps
in the companion-composed context after validating the contract version and the
ADD-only security floor. An LTS build with no companion installed keeps
the default context under the enterprise profile (see
:func:`_lts_on_core_defaults`).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Sequence, cast

from kiro_crew import edition
from kiro_crew.platform.context import (
    CONTRACT_VERSION,
    PROFILE_STANDALONE,
    PlatformCompositionError,
    PlatformContext,
    current_context,
    set_context,
)
from kiro_crew.platform.defaults import (
    DefaultAcceptanceCheckProvider,
    DefaultAgentCatalogProvider,
    DefaultAgentExecutableResolver,
    DefaultAgentIdentityProvider,
    DefaultAgentRuntime,
    DefaultAppRegistryPolicy,
    DefaultAppsLoader,
    DefaultBrowserCredentialProvider,
    DefaultCapabilityManager,
    DefaultCloudInstancePolicyProvider,
    DefaultCredentialPolicy,
    DefaultDashboardContributor,
    DefaultDeniedRuleProvider,
    DefaultEmbeddingSource,
    DefaultExternalAccessPolicy,
    DefaultFeedbackTrackerProvider,
    DefaultGatewayLifecycleProvider,
    DefaultIdentityProvider,
    DefaultImportSourceProvider,
    DefaultJailProvider,
    DefaultKnowledgeProvider,
    DefaultMcpToolingProvider,
    DefaultMemoryFilesProvider,
    DefaultMobileConnectProvider,
    DefaultPackageManager,
    DefaultPromptSourceProvider,
    DefaultProviderRegistry,
    DefaultPublishRegistry,
    DefaultRemoteProvisionerProvider,
    DefaultSandboxPolicy,
    DefaultSkillDiscoveryProvider,
    DefaultSlackEnterpriseGate,
    DefaultSpeechProviderRegistry,
    DefaultTelemetryProvider,
    DefaultTipsProvider,
    DefaultTunnelProvider,
)
from kiro_crew.platform.discovery import discover_companion_context, plugin_entry_points
from kiro_crew.platform.governance import (
    assert_governance_paths_protected,
    assert_policy_signature_satisfied,
    load_security_policy,
)
from kiro_crew.platform.profile import resolve_profile
from kiro_crew.platform.security_authority import PolicyAuthority, assert_security_floor

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.interfaces import TelemetryProvider, TunnelProvider

logger = logging.getLogger(__name__)

# Set True by ``boot_platform`` after the first successful composition so a
# second entry point (e.g. ``cli.main`` dispatching into ``run_gateway`` in the
# same process) does not re-resolve the profile or re-install the context.  The
# standalone default that ``current_context`` lazily builds does NOT set this —
# a real boot still runs once even if a call site touched ``current_context``
# first, swapping the lazy default for the properly-resolved context.
_BOOTED = False

# Guards the check-then-act on ``_BOOTED`` / ``set_context`` so two threads (or a
# thread racing the lazy ``current_context`` first-touch) cannot both run
# ``bootstrap_context`` — which would re-resolve the profile, reload the
# admission policy, re-run the companion ``ep.load()``, and call
# ``register_acp_backends`` twice.  Re-entrant because ``bootstrap_context`` and
# the lazy default both ultimately set the same global context.
_BOOT_LOCK = threading.RLock()


def boot_platform(cfg: "KiroCrewConfig") -> PlatformContext:
    """Idempotently bootstrap + install the platform context (call at startup).

    The single entry point process-launch code should call.  Safe to invoke
    more than once: only the first call resolves the profile and installs the
    context; later calls return the already-installed context unchanged.  This
    guards the case where both ``cli.main`` and ``run_gateway`` would otherwise
    each call ``bootstrap_context`` in one process.  The lock makes the
    check-then-act atomic so concurrent first-boots compose exactly once.
    """
    global _BOOTED
    with _BOOT_LOCK:
        if _BOOTED:
            return current_context()
        ctx = bootstrap_context(cfg)
        _BOOTED = True
        return ctx


def compose_telemetry_provider(cfg: "KiroCrewConfig") -> "TelemetryProvider":
    """Compose ONLY the edition's telemetry provider, installing no context.

    For a Kiro Crew process that deliberately does not boot the platform -- the
    MCP gateway daemon -- but must still send its metrics where the edition's
    collector is. Without it such a process resolves OTLP destinations through
    ``current_context()``, which on a non-standalone profile raises
    ``PlatformCompositionError`` (nothing was installed), so its metrics stay in
    the local sink and never reach the edition's collector.

    Nothing is installed: ``set_context`` is not called, so every other
    ``current_context()`` reader in the calling process keeps exactly the
    behavior it had (fail-closed on a non-standalone profile). Discovery still
    runs the fleet admission gate before any companion code loads, and the
    contract version is checked, and the security floor is asserted exactly as
    boot asserts it, so this accepts only a companion boot itself would accept
    (the calling process does not use its security authority, but a companion
    that fails the floor is refused here too rather than trusted for egress).

    Raises ``PlatformCompositionError`` the same way boot does; the caller
    decides what that means for it (the daemon degrades to local-only).
    """
    eps = plugin_entry_points()
    profile = resolve_profile(cfg, entry_points=eps)
    if profile == PROFILE_STANDALONE:
        return DefaultTelemetryProvider()
    if _lts_on_core_defaults(eps):
        # No companion carries a ceiling here, so read the policy the core-defaults
        # boot would install, live: it names the collector
        # (``capabilities.telemetry.otlp_endpoint``) and its host list.
        return cast(
            "TelemetryProvider",
            _LivePolicyTelemetry(DefaultTelemetryProvider(), DefaultCredentialPolicy().redact),
        )
    companion = _checked_companion(profile, cfg, eps)
    assert_security_floor(companion.security)
    # The calling process installs no context, so the core cannot read a ceiling
    # for these destinations: the provider carries the companion's own.
    return cast(
        "TelemetryProvider",
        _PolicyBoundTelemetry(
            companion.telemetry,
            getattr(companion, "governance", None),
            getattr(getattr(companion, "credentials", None), "redact", None),
        ),
    )


class _PolicyBoundTelemetry:
    """The companion's telemetry provider, plus the ceiling its OTLP export obeys.

    ``governance_ceiling`` is what ``metrics.provider`` holds destinations and each
    export to in a process with no context, and ``governance_redact`` is the
    companion's credential redaction its SEL rows go through there. Every other
    attribute, including ``otlp_destinations`` (or its absence), is the wrapped
    provider's own.
    """

    def __init__(self, inner: object, ceiling: object | None, redact: object = None) -> None:
        self._inner = inner
        self.governance_ceiling = ceiling
        self.governance_redact = redact

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


#: How long :class:`_LivePolicyTelemetry` reuses one policy read: the same window
#: ``metrics.provider._CONSENT_RECHECK_SECS`` gives a config change.
_LIVE_POLICY_TTL_SECS = 30.0


class _LivePolicyTelemetry:
    """The core telemetry provider for a process with no context and no companion.

    ``governance_ceiling`` re-reads the security policy at most once per
    :data:`_LIVE_POLICY_TTL_SECS`, so a policy that turns telemetry off or moves its
    collector stops this process's export too, as it does in a process with a
    context. A read that fails answers ``otlp_governance.UNEVALUABLE``, which
    refuses every host.
    """

    def __init__(self, inner: object, redact: object) -> None:
        self._inner = inner
        self.governance_redact = redact
        self._lock = threading.Lock()
        self._read_at: "float | None" = None
        self._ceiling: object = None

    @property
    def governance_ceiling(self) -> object:
        with self._lock:
            now = time.monotonic()
            if self._read_at is None or now - self._read_at >= _LIVE_POLICY_TTL_SECS:
                self._ceiling = _read_policy_now()
                self._read_at = now
            return self._ceiling

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _read_policy_now() -> object:
    """The installed policy, signature mandate checked, or ``UNEVALUABLE`` on failure."""
    from kiro_crew.metrics.otlp_governance import UNEVALUABLE

    try:
        ceiling = load_security_policy()
        if ceiling is not None:
            assert_policy_signature_satisfied(ceiling)
        return ceiling
    except Exception:
        # ``gate_exporter`` logs the export stopping; this read repeats per TTL.
        logger.debug("security policy unreadable; OTLP export refused", exc_info=True)
        return UNEVALUABLE


def _lts_on_core_defaults(eps: "Sequence[Any]") -> bool:
    """Does this LTS build boot on the core defaults, with no companion?

    True only for an LTS build (the build-time edition, which no env var or
    config changes) with no ``kirocrew.plugins`` entry point at all. The governance
    fetch and every LTS gate live in the core and key off the edition, so a
    companion adds nothing such a build needs (RFC rfc-crew-lts-edition,
    D1). *eps* is the answer of a scan that succeeded: in an
    LTS build ``plugin_entry_points`` raises rather than answer ``[]`` when
    it cannot read the metadata. Any installed entry point still goes through discovery,
    so a broken, rejected, ambiguous or contract-mismatched companion fails closed
    exactly as before. An ``oss`` build never takes this path: there a
    non-standalone profile with no companion still fails closed.
    """
    return edition.is_lts() and not eps


def _checked_companion(
    profile: str, cfg: "KiroCrewConfig", eps: "Sequence[Any]"
) -> PlatformContext:
    """Discover the companion for a non-standalone *profile* and check its contract.

    The one copy of these steps, shared by :func:`bootstrap_context` and
    :func:`compose_telemetry_provider` so the two cannot drift. Discovery runs the
    fleet admission gate before any companion code loads and builds the
    companion's full context; it raises ``PlatformCompositionError`` fail-closed.
    """
    companion = discover_companion_context(
        profile, cfg, entry_points=eps
    )  # may raise (fail-closed)
    if companion is None:
        # Defense in depth: discovery is expected to raise rather than return
        # None for a non-standalone profile.  If it ever returns None (e.g. a
        # future "present but disabled" path), refuse to boot here too -- never
        # silently install an edition-labeled context with open defaults and no
        # security overlay.  The fail-closed guarantee must not depend solely
        # on discovery's raising behavior.
        raise PlatformCompositionError(
            f"profile={profile!r} resolved no companion context; refusing to "
            "boot with open-source defaults (fail-closed)."
        )
    _assert_contract(companion)
    return companion


def _reset_boot_state() -> None:
    """Test helper — clear the one-shot boot guard so a test can boot again."""
    global _BOOTED
    with _BOOT_LOCK:
        _BOOTED = False


def _public_tunnel_provider(cfg: "KiroCrewConfig") -> "TunnelProvider":
    """The tunnel slot's provider in the public composition.

    The no-op :class:`DefaultTunnelProvider`, unless ``tunnel.relay_enabled`` turns
    on the Kiro relay client. A companion composes its own context and its own
    provider, so it never reaches this choice. The relay client is imported only
    when the setting is on, so a process composing the defaults with the relay off,
    a short-lived hook among them, never loads it.
    """
    from kiro_crew.edition_locks import relay_selected

    if not relay_selected(cfg):
        return DefaultTunnelProvider()
    from kiro_crew.relay_client.provider import public_tunnel_provider

    provider: "TunnelProvider" = public_tunnel_provider(cfg)
    return provider


def build_default_context(
    cfg: "KiroCrewConfig", *, profile: str = PROFILE_STANDALONE
) -> PlatformContext:
    """Compose the all-defaults context (the public/standalone edition).

    The single composition chokepoint that backs BOTH a real boot
    (``bootstrap_context``) and the lazy ``current_context`` default — so loading
    the enterprise security ceiling HERE means every path (including a stray
    stdio subprocess that reaches the lazy default without routing through
    ``cli.main``) carries the same frozen ceiling.  A present-but-unreadable
    policy raises ``PlatformCompositionError`` (fail-closed); a standalone host
    with no policy gets ``governance=None`` (editable secure-defaults).  The
    companion supplies its bundled policy via the discovery/composition root, so
    the standalone load does not consult a bundled resource here.
    """
    governance = load_security_policy()
    return PlatformContext(
        contract_version=CONTRACT_VERSION,
        profile=profile,
        cfg=cfg,
        providers=DefaultProviderRegistry(),
        publish=DefaultPublishRegistry(),
        agent_runtime=DefaultAgentRuntime(),
        agent_executable=DefaultAgentExecutableResolver(),
        gateway_lifecycle=DefaultGatewayLifecycleProvider(),
        sandbox=DefaultSandboxPolicy(),
        credentials=DefaultCredentialPolicy(),
        security=PolicyAuthority(),  # _NullOverlay → baseline only
        slack_gate=DefaultSlackEnterpriseGate(),
        identity=DefaultIdentityProvider(),
        agent_identity=DefaultAgentIdentityProvider(),
        embeddings=DefaultEmbeddingSource(),
        mcp_tooling=DefaultMcpToolingProvider(),
        agent_catalog=DefaultAgentCatalogProvider(),
        prompt_sources=DefaultPromptSourceProvider(),
        skill_discovery=DefaultSkillDiscoveryProvider(),
        tips=DefaultTipsProvider(),
        denied_rules=DefaultDeniedRuleProvider(),
        import_sources=DefaultImportSourceProvider(),
        capability_manager=DefaultCapabilityManager(),
        external_access=DefaultExternalAccessPolicy(),
        registry=DefaultAppRegistryPolicy(),
        apps_loader=DefaultAppsLoader(),
        package_manager=DefaultPackageManager(),
        knowledge=DefaultKnowledgeProvider(),
        memory_files=DefaultMemoryFilesProvider(),
        tunnel=_public_tunnel_provider(cfg),
        telemetry=DefaultTelemetryProvider(),
        dashboard=DefaultDashboardContributor(),
        jail=DefaultJailProvider(),
        mobile_connect=DefaultMobileConnectProvider(),
        remote_provisioners=DefaultRemoteProvisionerProvider(),
        cloud_instance_policy=DefaultCloudInstancePolicyProvider(),
        feedback=DefaultFeedbackTrackerProvider(),
        browser_credentials=DefaultBrowserCredentialProvider(),
        speech=DefaultSpeechProviderRegistry(),
        acceptance_checks=DefaultAcceptanceCheckProvider(),
        feature_apps=(),
        governance=governance,
    )


def _assert_contract(ctx: PlatformContext) -> None:
    """Reject a companion context built against a different contract version."""
    if ctx.contract_version != CONTRACT_VERSION:
        raise PlatformCompositionError(
            f"companion contract_version={ctx.contract_version} != core "
            f"CONTRACT_VERSION={CONTRACT_VERSION}; rebuild the companion against this core."
        )


def _refresh_stt_for_edition(cfg: "KiroCrewConfig") -> None:
    """Re-resolve ``stt.provider`` once an edition's speech registry is installed.

    The config every boot path is handed was loaded BEFORE any context existed,
    so its ``stt.provider`` was resolved against the built-ins alone: a stored
    registered name degraded to ``off`` and an absent key took ``local``. The
    validated-config cache holds that same answer. Drop the cache and re-read the
    section so this ``cfg`` and every later load see the edition's providers. A
    no-op for the Default registry, so a standalone boot does nothing here.
    Best-effort: a failure leaves the built-in answer, which is a working
    (conservative) setting, rather than aborting boot.
    """
    from kiro_crew.speech_providers import edition_changes_stt

    if not edition_changes_stt():
        return
    try:
        from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache

        _invalidate_config_cache()
        cfg.stt = KiroCrewConfig.load().stt
    except Exception:
        logger.warning(
            "re-resolving stt.provider for the edition failed; continuing", exc_info=True
        )


def bootstrap_context(cfg: "KiroCrewConfig") -> PlatformContext:
    """Build, validate, install, and return the active platform context.

    Standalone: returns the all-defaults context.  LTS build with no
    companion: the all-defaults context under the enterprise profile.  Otherwise
    non-standalone: discovers the companion context (fail-closed), validates the
    contract version and the ADD-only security floor, then installs it as the
    process-global context.
    """
    eps = plugin_entry_points()
    profile = resolve_profile(cfg, entry_points=eps)
    ctx = build_default_context(cfg, profile=profile)

    if _lts_on_core_defaults(eps):
        logger.info("LTS build with no companion: composed the core defaults")
    elif profile != PROFILE_STANDALONE:
        companion = _checked_companion(profile, cfg, eps)
        assert_security_floor(companion.security)
        ctx = companion
        logger.info("Composed %r platform context from companion", profile)

    # Keystone integrity check: the governance trust-root files must be on the
    # sensitive-path floor so a prompt-injected agent cannot rewrite its own
    # ceiling.  Independent of the deny-pattern floor (which assert_security_floor
    # covers) — fail closed at boot if a refactor ever dropped them.
    assert_governance_paths_protected()

    # Mandated-signature absence gate: a fleet that set require_policy_signature
    # must actually HAVE a policy. Runs on the FINAL context — after the companion
    # supplied its bundled ceiling — because that is the first point at which "no
    # policy at any tier" is decidable; load_security_policy() runs once per
    # composition pass and cannot tell whether it is the last word.
    assert_policy_signature_satisfied(ctx.governance)

    # Governance floor gate (Validation rules 3 & 7 / combined-order ABORT step):
    # when an enterprise ceiling is present, every bound profile must be at least
    # as strict as it for every ordinal control.  A looser-ordinal profile raises
    # PlatformCompositionError and ABORTS boot fail-closed here — rather than only
    # being silently re-tightened at runtime.  No-op on a standalone host with no
    # ceiling.
    from kiro_crew.platform.governance_profiles import assert_profiles_within_ceiling

    assert_profiles_within_ceiling(ctx.governance)

    # Speech providers are ADD-only: a registered name that would shadow a
    # built-in (or ``off``), a duplicate, or a default naming no provider aborts
    # boot here, fail-closed, before anything can select it. The Default registry
    # passes trivially.
    from kiro_crew.speech_providers import validate_speech_registry

    validate_speech_registry(ctx.speech)

    set_context(ctx)

    from kiro_crew.config.sections_stt import warn_deferred_stt_providers

    warn_deferred_stt_providers()
    _refresh_stt_for_edition(cfg)

    # Register any edition-contributed ACP backends now that the context is
    # installed.  The Default ProviderRegistry.register_acp_backends() is a
    # no-op: the public edition's selectable set is the baseline in
    # ``acp_backends``, which already covers every KNOWN backend, so an edition
    # only needs this seam for a harness the core does not ship.  Best-effort — a
    # backend-registration failure must not abort boot (the provider factory
    # still resolves).
    try:
        ctx.providers.register_acp_backends()
    except Exception:
        logger.warning("register_acp_backends failed; continuing", exc_info=True)

    # Apply the ``agent_backend`` governance scope by narrowing that same registry —
    # HERE, and only here, because this is the one point where both halves are true:
    # the context is installed (so resolving the ceiling cannot re-enter the config
    # load, harness-parity H3) and every edition has finished widening (so nothing
    # registered later escapes the policy). Narrowing the registry rather than
    # checking downstream is what keeps selectability at ONE gate (H4) and leaves the
    # Kiro construction path free of an adapter-driven conditional (H13).
    #
    # Best-effort like the registration above: a policy that cannot be evaluated
    # denies the harness it was evaluating and leaves the floor, which is a startable
    # deployment — aborting boot would not be.
    try:
        from kiro_crew.agent_backend_governance import narrow_selectable_backends

        if narrow_selectable_backends():
            # The cfg passed into this function was normalized BEFORE the narrowing,
            # so a value the policy just removed is still on this instance while
            # every later ``KiroCrewConfig.load()`` (which re-reads from disk) sees
            # the narrowed answer. Re-run the SAME single gate on that one field
            # rather than adding a second one anywhere. An undeclared field holds the
            # load's resolved DEFAULT, not a user choice, so it goes back in as absent:
            # passing it through would warn about a value nobody wrote.
            from kiro_crew.acp_backends import resolve_selected_backend
            from kiro_crew.config.loader import acp_backend_declared

            declared = acp_backend_declared()
            cfg.agent.acp_backend = resolve_selected_backend(
                cfg.agent.acp_backend if declared else None
            )
    except Exception:
        logger.warning("agent_backend policy narrowing failed; continuing", exc_info=True)

    # Register any edition-contributed artifact-publish providers now that the
    # context is installed.  The Default PublishRegistry.register_publish_providers()
    # is a no-op (the public edition ships no publish destination), so the
    # publish_provider registry stays empty and get_provider() → 503; the Amazon
    # companion registers its concrete providers here.  Best-effort — a
    # registration failure must not abort boot (publish just stays unavailable).
    try:
        ctx.publish.register_publish_providers()
    except Exception:
        logger.warning("register_publish_providers failed; continuing", exc_info=True)

    return ctx


# The floor's two KAS fallback notices (``KAS_CLI_FLOOR_NOTICE`` and
# ``KAS_CLI_FLOOR_DEFAULT_NOTICE``) live in ``agent_sdk.backends``: that leaf logs
# them too, for a process outside the gateway, and never imports this package.

#: The notice when the user picked Kiro CLI: the pick runs on KAS only while
#: kiro-cli offers its v3 engine, so on this kiro-cli it keeps the v2 launch.
KAS_CLI_FLOOR_KIRO_NOTICE = (
    "Kiro CLI is selected and {reason}, so Kiro Crew keeps running kiro-cli's v2 "
    "engine. kiro-cli is retiring v2: run `kiro-cli update` and restart Kiro Crew."
)

#: The notice when the user picked Kiro CLI and the probe could not answer. The
#: pick stays on the v2 launch, as before; the notice only makes that visible.
KAS_CLI_FLOOR_KIRO_UNKNOWN_NOTICE = (
    "Kiro CLI is selected, but Kiro Crew could not check whether kiro-cli offers its "
    "v3 engine ({reason}), so it keeps running kiro-cli's v2 engine."
)


def _unknown_verdict_reason(*, start: bool = False) -> str:
    """Why the floor's probe had no answer, as one clause. Stats only; never raises.

    *start* is whether the help probe answered and the v3 start after it did not.
    """
    from kiro_crew.agent_sdk.drivers.acp import kiro_cli_spawn_binary

    binary = kiro_cli_spawn_binary()
    if binary is None:
        return "no trusted kiro-cli was found"
    if start:
        return f"`{binary} acp --agent-engine v3` did not finish"
    return f"`{binary} acp --help` did not run"


def apply_kas_cli_floor(
    cfg: "KiroCrewConfig", *, kas_defaulted: bool = False, kiro_selected: bool = False
) -> str | None:
    """Fall back to kiro-cli when KAS is configured but this kiro-cli cannot serve it.

    The same ``acp --help`` probe ``kirocrew doctor`` reports from, turned into a
    runtime floor. When the probe RAN and the engine is missing, KAS is marked
    unservable on this host, which drops it from the one selectable registry every
    later config load reads; ``cfg``'s own already-resolved field is re-resolved
    through the same gate. Returns the notice it logged, or ``None`` when nothing
    changed.

    Probes only when KAS is what this install would run — the main or member
    backend — or the main backend is a stored Kiro CLI pick (*kiro_selected*), and
    KAS is still selectable. An unknown verdict (no pinned kiro-cli, a failed spawn)
    leaves KAS selected and a Kiro CLI pick on kiro-cli's v2 launch: the floor never
    acts on a fact it could not establish. For a Kiro CLI pick that unknown is still
    logged (:data:`KAS_CLI_FLOOR_KIRO_UNKNOWN_NOTICE`), so a pick that stays on v2
    says why. A Kiro CLI pick moves onto KAS
    (``confirm_kiro_cli_offers_kas``) only after a probe that ran and names the v3
    engine AND one start of that engine kiro-cli did not refuse
    (``kas_engine_start_verdict``): kiro-cli lists v3 in its help even on a host
    where it then refuses to run it, and that refusal is a gap like any other, so
    KAS is marked unservable and the pick keeps the v2 launch. The start runs once
    per kiro-cli install, on the pinned install only (a kiro-cli reached only
    through ``PATH`` is never started outside the sandbox, so its help verdict
    stands until a refusal is on record for it); its verdict, and every refusal a
    KAS runtime met at its own start, are kept in the data home
    (``acp.kas_engine_refusal``). With no stored pick at stake nothing is started:
    a refusal on record for this kiro-cli is a gap the same way, so a defaulted or
    chosen KAS falls back before the first chat, and otherwise the help probe
    alone confirms and a later refusal at a KAS start withdraws it. Neither gap
    marks anything for a KAS whose next spawn takes the bundle route chosen
    automatically (``agent_sdk.kas_bundle.kas_route``, asked only before marking):
    that spawn never runs kiro-cli. A Kiro CLI pick is not asked, as the route
    rule always keeps it on the relay.

    Called by the gateway alone, after :func:`bootstrap_context`, so it reads the
    policy-narrowed set. Not part of ``bootstrap_context`` itself: that runs in
    every ``kirocrew`` process (each MCP server, app server and script cron), and
    the KAS sessions this floor protects are spawned only by the gateway. Keeping
    it out also leaves ``kirocrew doctor`` reading the configured backend, so its
    KAS block still prints the engine diagnosis. Blocking: the gateway runs it in a
    background thread, off the boot path.
    """
    from kiro_crew.acp_backends import (
        ACP_BACKEND_KAS,
        ACP_BACKEND_KIRO,
        governance_floor_backend,
        mark_backend_unservable,
        resolve_selected_backend,
        selectable_backends,
    )
    from kiro_crew.agent_sdk.backends import (
        KAS_CLI_FLOOR_DEFAULT_NOTICE,
        KAS_CLI_FLOOR_NOTICE,
        confirm_kiro_cli_offers_kas,
        kiro_cli_pick_backend,
    )
    from kiro_crew.crewmate_harness import bots_may_run_on_kas

    agent = cfg.agent
    named = ACP_BACKEND_KAS in (agent.acp_backend, agent.member_acp_backend)
    # A Bot can also land on KAS with neither field naming it (an inherited
    # member harness its worker spec cannot use, or a pin to one Bots cannot run on).
    # ``process_entry._start_kas_cli_floor`` arms the member spawn wait on the same
    # rule, so such a Bot does not start KAS before this probe lands.
    kas_roles = named or bots_may_run_on_kas(cfg)
    kiro_pick = kiro_selected and agent.acp_backend == ACP_BACKEND_KIRO
    if not (kas_roles or kiro_pick):
        return None
    if agent.kas_bundle:
        # The bundle route spawns Kiro Crew's own engine and never runs kiro-cli,
        # so kiro-cli's engine support is not a fact about this host's KAS. Acting
        # on it would silently move a bundle-route user back onto kiro-cli, and a
        # Kiro CLI pick there stays on kiro-cli rather than on the bundled engine.
        return None
    if ACP_BACKEND_KAS not in selectable_backends():
        return None
    from kiro_crew.agent_sdk import kas_bundle, kas_engine_start_verdict, kas_engine_verdict

    offers_kas, reason = kas_engine_verdict()
    start_unknown = False
    if offers_kas:
        # The help lists v3 even where kiro-cli then refuses to run it. The user's
        # own pick moves only on proof the engine runs here (one start per kiro-cli
        # install, kept in the data home); without a pick, a refusal already on
        # record for this kiro-cli is enough, and nothing is started for it.
        started, refusal = kas_engine_start_verdict(start=kiro_pick)
        if kiro_pick or started is False:
            offers_kas, reason = started, refusal
            start_unknown = started is None
    if offers_kas:
        # Positive evidence only: from here a Kiro CLI pick moves to the KAS path
        # (``KIRO_SELECTION_BACKEND``), including one the user makes later in this
        # process, so the verdict is recorded whatever the boot backend was.
        confirm_kiro_cli_offers_kas()
        if kiro_pick:
            agent.acp_backend = kiro_cli_pick_backend()
        return None
    if reason is None:
        if not kiro_pick:
            return None
        notice = KAS_CLI_FLOOR_KIRO_UNKNOWN_NOTICE.format(
            reason=_unknown_verdict_reason(start=start_unknown)
        )
        logger.warning(notice)
        return notice
    if governance_floor_backend() == ACP_BACKEND_KAS:
        # LTS (RFC D6): KAS is the floor, so it is never marked unservable and
        # nothing falls back to kiro-cli. ``lts_boot.require_kas_harness`` already
        # refused a start whose main sessions run KAS, or recorded the verdict so each
        # session on KAS is refused at its own start; only a Kiro CLI pick's notice holds.
        if not kiro_pick:
            return None
        notice = KAS_CLI_FLOOR_KIRO_NOTICE.format(reason=reason)
        logger.warning(notice)
        return notice
    if not kiro_pick and kas_bundle.kas_route().bundle:
        # The next KAS spawn takes Kiro Crew's own engine, chosen automatically
        # because kiro-cli cannot run a session (``acp.kas_route``). That spawn never
        # runs kiro-cli, so neither its help gap nor a refusal on record is a fact
        # about this host's KAS. A Kiro CLI pick always keeps the relay (rule 2).
        return None
    mark_backend_unservable(ACP_BACKEND_KAS, reason)
    agent.acp_backend = resolve_selected_backend(agent.acp_backend)
    parts = []
    if kiro_pick:
        parts.append(KAS_CLI_FLOOR_KIRO_NOTICE.format(reason=reason))
    if kas_roles:
        # A Bot's KAS fallback is a default nobody stored, so it gets the default wording.
        template = (
            KAS_CLI_FLOOR_DEFAULT_NOTICE if kas_defaulted or not named else KAS_CLI_FLOOR_NOTICE
        )
        parts.append(template.format(reason=reason))
    notice = " ".join(parts)
    logger.warning(notice)
    return notice
