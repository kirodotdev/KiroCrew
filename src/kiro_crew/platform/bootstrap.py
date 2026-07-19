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
from typing import TYPE_CHECKING

from kiro_crew.platform.context import (
    CONTRACT_VERSION,
    PROFILE_STANDALONE,
    PlatformCompositionError,
    PlatformContext,
    current_context,
    set_context,
)
from kiro_crew.platform.defaults import (
    DefaultAgentRuntime,
    DefaultAppRegistryPolicy,
    DefaultAppsLoader,
    DefaultCredentialPolicy,
    DefaultDashboardContributor,
    DefaultEmbeddingSource,
    DefaultIdentityProvider,
    DefaultJailProvider,
    DefaultKnowledgeProvider,
    DefaultMcpToolingProvider,
    DefaultPackageManager,
    DefaultProviderRegistry,
    DefaultPublishRegistry,
    DefaultSandboxPolicy,
    DefaultSlackEnterpriseGate,
    DefaultTelemetryProvider,
    DefaultTunnelProvider,
)
from kiro_crew.platform.discovery import discover_companion_context, plugin_entry_points
from kiro_crew.platform.governance import (
    assert_governance_paths_protected,
    load_security_policy,
)
from kiro_crew.platform.profile import resolve_profile
from kiro_crew.platform.security_authority import PolicyAuthority, assert_security_floor

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

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
        sandbox=DefaultSandboxPolicy(),
        credentials=DefaultCredentialPolicy(),
        security=PolicyAuthority(),  # _NullOverlay → baseline only
        slack_gate=DefaultSlackEnterpriseGate(),
        identity=DefaultIdentityProvider(),
        embeddings=DefaultEmbeddingSource(),
        mcp_tooling=DefaultMcpToolingProvider(),
        registry=DefaultAppRegistryPolicy(),
        apps_loader=DefaultAppsLoader(),
        package_manager=DefaultPackageManager(),
        knowledge=DefaultKnowledgeProvider(),
        tunnel=DefaultTunnelProvider(),
        telemetry=DefaultTelemetryProvider(),
        dashboard=DefaultDashboardContributor(),
        jail=DefaultJailProvider(),
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
        companion = discover_companion_context(profile, cfg)  # may raise (fail-closed)
        if companion is None:
            # Defense in depth: discovery is expected to raise rather than return
            # None for a non-standalone profile.  If it ever returns None (e.g. a
            # future "present but disabled" path), refuse to boot here too — never
            # silently install an amazon-labeled context with open defaults and no
            # security overlay.  The fail-closed guarantee must not depend solely
            # on discovery's raising behavior.
            raise PlatformCompositionError(
                f"profile={profile!r} resolved no companion context; refusing to "
                "boot with open-source defaults (fail-closed)."
            )
        _assert_contract(companion)
        assert_security_floor(companion.security)
        ctx = companion
        logger.info("Composed %r platform context from companion", profile)

    # Keystone integrity check: the governance trust-root files must be on the
    # sensitive-path floor so a prompt-injected agent cannot rewrite its own
    # ceiling.  Independent of the deny-pattern floor (which assert_security_floor
    # covers) — fail closed at boot if a refactor ever dropped them.
    assert_governance_paths_protected()

    # Governance floor gate (Validation rules 3 & 7 / combined-order ABORT step):
    # when an enterprise ceiling is present, every bound profile must be at least
    # as strict as it for every ordinal control.  A looser-ordinal profile raises
    # PlatformCompositionError and ABORTS boot fail-closed here — rather than only
    # being silently re-tightened at runtime.  No-op on a standalone host with no
    # ceiling.
    from kiro_crew.platform.governance_profiles import assert_profiles_within_ceiling

    assert_profiles_within_ceiling(ctx.governance)

    set_context(ctx)

    # Register any edition-contributed ACP backends now that the context is
    # installed.  The Default ProviderRegistry.register_acp_backends() is a
    # no-op (standalone ships Kiro-CLI-ACP only), so this is a no-op for the
    # public edition; the Amazon companion re-registers Claude Code through the
    # dormant ACP_BACKEND_CLAUDE seam here.  Best-effort — a backend-registration
    # failure must not abort boot (the provider factory still resolves).
    try:
        ctx.providers.register_acp_backends()
    except Exception:
        logger.warning("register_acp_backends failed; continuing", exc_info=True)

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
