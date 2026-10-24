"""Boot-time composition root for the public (standalone) edition.

``build_default_context`` assembles a :class:`PlatformContext` whose every
interface field is a ``Default*`` adapter — i.e. today's open-source behavior.
``bootstrap_context`` is the single entry point boot calls: it builds the
default context, resolves the profile, and (for a non-standalone profile) swaps
in the companion-composed context after validating the contract version and the
ADD-only security floor.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Sequence, cast

from kiro_crew.platform.context import (
    CONTRACT_VERSION,
    PROFILE_STANDALONE,
    PlatformCompositionError,
    PlatformContext,
    current_context,
    set_context,
)
from kiro_crew.platform.defaults import (
    DefaultAgentCatalogProvider,
    DefaultAgentExecutableResolver,
    DefaultAgentIdentityProvider,
    DefaultAgentRuntime,
    DefaultAppRegistryPolicy,
    DefaultAppsLoader,
    DefaultCapabilityManager,
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
    from kiro_crew.platform.interfaces import TelemetryProvider

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
        tunnel=DefaultTunnelProvider(),
        telemetry=DefaultTelemetryProvider(),
        dashboard=DefaultDashboardContributor(),
        jail=DefaultJailProvider(),
        mobile_connect=DefaultMobileConnectProvider(),
        remote_provisioners=DefaultRemoteProvisionerProvider(),
        feedback=DefaultFeedbackTrackerProvider(),
        speech=DefaultSpeechProviderRegistry(),
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

    Standalone: returns the all-defaults context.  Non-standalone: discovers the
    companion context (fail-closed), validates the contract version and the
    ADD-only security floor, then installs it as the process-global context.
    """
    eps = plugin_entry_points()
    profile = resolve_profile(cfg, entry_points=eps)
    ctx = build_default_context(cfg, profile=profile)

    if profile != PROFILE_STANDALONE:
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


#: The notice a host on a too-old kiro-cli gets when KAS is configured. Logged at
#: WARNING, and the same reason rides on the KAS row of the dashboard's backend
#: panel through ``agent_sdk.backends.host_unservable_reason``.
KAS_CLI_FLOOR_NOTICE = (
    "KAS is selected, but {reason}. Kiro Crew is using kiro-cli instead. "
    "Update kiro-cli and restart Kiro Crew to use KAS."
)

#: The same notice when KAS is only the default (no backend field that names KAS is
#: stored): the user never chose KAS, so the notice must not say they did.
KAS_CLI_FLOOR_DEFAULT_NOTICE = (
    "KAS is the default backend, but {reason}. Kiro Crew is using kiro-cli instead. "
    "Update kiro-cli and restart Kiro Crew to use KAS."
)


def apply_kas_cli_floor(cfg: "KiroCrewConfig", *, kas_defaulted: bool = False) -> str | None:
    """Fall back to kiro-cli when KAS is configured but this kiro-cli cannot serve it.

    The same ``acp --help`` probe ``kirocrew doctor`` reports from, turned into a
    runtime floor. When the probe RAN and the engine is missing, KAS is marked
    unservable on this host, which drops it from the one selectable registry every
    later config load reads; ``cfg``'s own already-resolved field is re-resolved
    through the same gate. Returns the notice it logged, or ``None`` when nothing
    changed.

    Probes only when KAS is what this install would run — the main or member
    backend — and still selectable. An unknown verdict (no pinned kiro-cli, a
    failed spawn) leaves KAS selected: the floor never acts on a fact it could not
    establish.

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
        mark_backend_unservable,
        resolve_selected_backend,
        selectable_backends,
    )

    agent = cfg.agent
    if ACP_BACKEND_KAS not in (agent.acp_backend, agent.member_acp_backend):
        return None
    if agent.kas_bundle:
        # The bundle route spawns Kiro Crew's own engine and never runs kiro-cli,
        # so kiro-cli's engine support is not a fact about this host's KAS. Acting
        # on it would silently move a bundle-route user back onto kiro-cli.
        return None
    if ACP_BACKEND_KAS not in selectable_backends():
        return None
    from kiro_crew.agent_sdk import kas_engine_unsupported_reason

    reason = kas_engine_unsupported_reason()
    if reason is None:
        return None
    mark_backend_unservable(ACP_BACKEND_KAS, reason)
    agent.acp_backend = resolve_selected_backend(agent.acp_backend)
    template = KAS_CLI_FLOOR_DEFAULT_NOTICE if kas_defaulted else KAS_CLI_FLOOR_NOTICE
    notice = template.format(reason=reason)
    logger.warning(notice)
    return notice
