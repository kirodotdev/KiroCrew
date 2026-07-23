"""The PlatformContext composition object and the active-context accessor.

KiroCrew uses the Composed Platform Providers (CPP) model to share one core
between the public open-source edition and the Amazon-internal companion.  The
public core defines a set of *extension points* — interfaces in
``kiro_crew.platform.interfaces`` — and ships a ``Default*`` adapter for each
that reproduces today's open-source behavior.  An internal companion package
supplies Amazon adapters for the same interfaces.

The :class:`PlatformContext` is the frozen object, built once at boot, that
holds the chosen adapter for every extension point.  Core code reads only from
the context (directly when it has it, or via :func:`current_context` for
module-level functions), so the public core never names an Amazon class.

See ``docs/system-specs/modules/platform-context.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Tuple, TypeVar

if TYPE_CHECKING:  # avoid import cycles — config.loader imports heavy modules
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.governance import GovernanceCeiling
    from kiro_crew.platform.interfaces import (
        AgentCatalogProvider,
        AgentExecutableResolver,
        AgentRuntime,
        AppRegistryPolicy,
        AppsLoader,
        CapabilityManager,
        CredentialPolicy,
        DashboardContributor,
        EmbeddingSource,
        FeatureApp,
        IdentityProvider,
        JailProvider,
        KnowledgeProvider,
        McpToolingProvider,
        PackageManager,
        PromptSourceProvider,
        ProviderRegistry,
        PublishRegistry,
        SandboxPolicy,
        SlackEnterpriseGate,
        TelemetryProvider,
        TunnelProvider,
    )
    from kiro_crew.platform.security_authority import PolicyAuthority

# Bumped on any field add/rename or interface-semantics change.  A companion
# built against a different CONTRACT_VERSION refuses to compose (see
# bootstrap._assert_contract).
#
# PINNED AT 1 PRE-LAUNCH: there is no shipped release yet, and the companion is
# always rebuilt in lockstep with the core from the same source, so the
# composition-time mismatch guard always compares 1 == 1.  Bumping per-field
# would only churn the seam without protecting any deployed companion.  Start
# incrementing this only after the first public release, when a separately-built
# companion can pin against a frozen contract.  (Every seam added pre-launch —
# the ``governance`` carrier, then the ``knowledge``/``dashboard``/``jail``
# extension points — landed under this same v1, no bump.)
CONTRACT_VERSION = 1

# Valid profiles.  ``standalone`` is the public default; ``enterprise`` loads a
# companion package that composes a non-default context (e.g. an SSO overlay).
PROFILE_STANDALONE = "standalone"
PROFILE_ENTERPRISE = "enterprise"


class PlatformCompositionError(RuntimeError):
    """Raised when the platform context cannot be composed safely.

    This is a *fail-closed* signal: a non-standalone profile that cannot find
    its companion, a contract-version mismatch, or a companion that would
    weaken the security floor all abort boot rather than silently downgrade.
    """


@dataclass(frozen=True)
class PlatformContext:
    """Immutable bundle of the chosen adapter for every extension point.

    Built once at boot by :func:`kiro_crew.platform.bootstrap.bootstrap_context`
    and never mutated.  The public edition composes a context whose every
    interface field is a ``Default*`` adapter; the Amazon companion replaces a
    subset via ``dataclasses.replace`` in its composition root.
    """

    # ── carriers (not interfaces) ──
    contract_version: int
    profile: str
    cfg: "KiroCrewConfig"

    # ── boot-layer extension points ──
    providers: "ProviderRegistry"
    publish: "PublishRegistry"
    agent_runtime: "AgentRuntime"
    agent_executable: "AgentExecutableResolver"
    sandbox: "SandboxPolicy"
    credentials: "CredentialPolicy"
    security: "PolicyAuthority"
    slack_gate: "SlackEnterpriseGate"
    identity: "IdentityProvider"
    embeddings: "EmbeddingSource"
    mcp_tooling: "McpToolingProvider"
    agent_catalog: "AgentCatalogProvider"
    prompt_sources: "PromptSourceProvider"
    capability_manager: "CapabilityManager"

    # ── install / structural extension points ──
    registry: "AppRegistryPolicy"
    apps_loader: "AppsLoader"
    package_manager: "PackageManager"
    knowledge: "KnowledgeProvider"

    # ── runtime-service / frontend extension points ──
    tunnel: "TunnelProvider"
    telemetry: "TelemetryProvider"
    dashboard: "DashboardContributor"
    jail: "JailProvider"

    # ── bundled feature apps ──
    feature_apps: "Tuple[FeatureApp, ...]"

    # ── governance carrier (Level 1 enterprise security ceiling) ──
    # Frozen at boot from the trust-root policy path; ``None`` on a standalone
    # host with no policy present (editable secure-defaults).  Read at every
    # enforcement chokepoint via ``current_context().governance``.  Defaulted so
    # the single constructor and the companion's ``dataclasses.replace`` paths
    # need no change beyond opting in.
    governance: "Optional[GovernanceCeiling]" = None

    @property
    def is_enterprise(self) -> bool:
        return self.profile == PROFILE_ENTERPRISE


# ── Active-context accessor ──
# Module-level functions that cannot easily take a ``ctx`` argument (e.g.
# security.is_denied, sandbox arg builders) read the process-global context set
# once at boot.  Tests that compose a non-default context must set_context()
# and reset around the test (see the reset_platform_context fixture).
_ACTIVE: Optional[PlatformContext] = None


def set_context(ctx: PlatformContext) -> None:
    """Install the process-global active context (called once at boot)."""
    global _ACTIVE
    _ACTIVE = ctx


def current_context() -> PlatformContext:
    """Return the active context, lazily building the standalone default.

    A lazy default keeps import-time and test call sites working even when boot
    has not run (e.g. a unit test that imports ``security`` directly).  Normally
    boot installs the real context via :func:`set_context` at process start, so
    the lazy path runs at most once before that.

    Cost / ordering note: while ``_ACTIVE`` is None the lazy path loads config +
    resolves the profile (a cheap SSO-marker stat).  On the STANDALONE happy path it
    runs once and memoizes into ``_ACTIVE``, so subsequent hot-path callers
    (``hooks.on_tool_call``, ``redact_via_context``) pay only an attribute read.
    A NON-standalone profile re-raises every call (it never caches a fail-open
    state).  Callers that drive a process/worker without ``boot_platform`` should
    install a context first to avoid the unbooted resolution; the lazy default is
    a fallback, not the intended boot path.

    Fail-closed guard: the lazy default is only safe when the host actually
    resolves to the standalone profile.  If the profile resolves to a
    non-standalone edition (e.g. a host with the opt-in SSO-identity
    probe or ``KIROCREW_PROFILE=enterprise``) but no context was installed — meaning
    boot failed/was-skipped and a caller would otherwise get open-source defaults
    with no security overlay or credential redaction — refuse to compose and
    raise :class:`PlatformCompositionError`.  Defense-in-depth so a future
    swallowing caller cannot reintroduce the silent fail-open.
    """
    global _ACTIVE
    if _ACTIVE is None:
        # deferred (not a cycle): keep config import off the module-load path so
        # importing kiro_crew.platform stays cheap; only the lazy-default path needs it.
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform.bootstrap import (  # circular import: bootstrap imports context
            build_default_context,
        )
        from kiro_crew.platform.discovery import (  # circular import: discovery imports context
            plugin_entry_points,
        )
        from kiro_crew.platform.profile import (
            resolve_profile,  # circular import: profile imports context
        )

        cfg = KiroCrewConfig.load()
        profile = resolve_profile(cfg, entry_points=plugin_entry_points())
        if profile != PROFILE_STANDALONE:
            raise PlatformCompositionError(
                f"current_context() reached with no installed context but "
                f"profile resolved to {profile!r}; refusing to compose "
                "open-source defaults (fail-closed). Boot did not run or failed "
                "to compose the companion."
            )
        _ACTIVE = build_default_context(cfg, profile=PROFILE_STANDALONE)
    return _ACTIVE


def reset_context() -> None:
    """Clear the active context (test helper)."""
    global _ACTIVE
    _ACTIVE = None


_T = TypeVar("_T")
_logger = logging.getLogger(__name__)


_UNSET: Any = object()


def _require_fallback(
    fallback: Any,
    fallback_factory: "Optional[Callable[[], Any]]",
    *,
    where: str = "safe_context_call",
) -> None:
    """Fail loudly at the call site if NEITHER fallback form was supplied.

    Without this guard a caller that forgot both would, on the first transient
    adapter error, silently return the ``_UNSET`` sentinel object — surfacing as
    a confusing ``AttributeError``/``TypeError`` far from the seam.  Checked
    BEFORE running ``fn`` so it raises regardless of whether ``fn`` would error.
    ``where`` names the calling helper so the error points at the function the
    developer actually called (sync vs async sibling).
    """
    if fallback is _UNSET and fallback_factory is None:
        raise TypeError(f"{where} requires either fallback= or fallback_factory=")


def _context_degrade(
    fallback: "_T",
    fallback_factory: "Optional[Callable[[], _T]]",
    log_message: "str | None",
) -> "_T":
    """Shared degrade-path policy for both safe_context_call variants.

    Centralized so a future change (log level, lazy-vs-eager handling) cannot
    diverge between the sync and async siblings.  Called only from inside their
    ``except Exception`` blocks, after ``PlatformCompositionError`` has already
    been re-raised.

    Because this runs INSIDE the caller's ``except`` handler, a raise from
    ``fallback_factory()`` would otherwise escape ``safe_context_call`` uncaught.
    So the factory call is guarded here, keeping the SAME fail-closed discipline
    as the primary thunk: a :class:`PlatformCompositionError` from the factory
    still propagates; any other factory error degrades to the eager ``fallback``
    when one was also supplied, else re-raises (there is no usable value).
    """
    if log_message is not None:
        _logger.debug(log_message, exc_info=True)
    if fallback_factory is not None:
        try:
            return fallback_factory()
        except PlatformCompositionError:
            raise
        except Exception:
            _logger.debug("fallback_factory itself failed", exc_info=True)
            if fallback is _UNSET:
                raise
            return fallback
    return fallback


def safe_context_call(
    fn: "Callable[[], _T]",
    *,
    fallback: _T = _UNSET,
    fallback_factory: "Optional[Callable[[], _T]]" = None,
    log_message: "str | None" = None,
) -> _T:
    """Run a context-reading thunk fail-closed, degrading to a fallback.

    The CPP fail-closed invariant: a :class:`PlatformCompositionError` (a
    non-standalone host that could not compose its companion) MUST abort rather
    than silently degrade to open-source defaults — so it is always re-raised.
    Any *other* exception (a transient adapter failure) degrades to the fallback
    so a best-effort lookup never breaks the caller.

    Centralizing the idiom here means a call site cannot accidentally swallow
    ``PlatformCompositionError`` by writing a bare ``except Exception`` (the bug
    that previously recurred in several hand-written shims).

    The fallback is supplied EITHER eagerly via ``fallback`` OR lazily via
    ``fallback_factory`` (at least one is REQUIRED — passing neither raises
    ``TypeError`` at the call site rather than leaking the ``_UNSET`` sentinel).
    Prefer ``fallback_factory`` when building the fallback is itself expensive or
    fallible: it is invoked ONLY on the degrade path and INSIDE the ``except``
    block, so (a) a happy-path call never pays to build a fallback it discards,
    and (b) an exception raised while building the fallback is still handled here
    rather than escaping the helper uncaught.

    ``log_message`` is logged at debug on the degrade path; pass ``None`` for
    callers that must not log (e.g. a stdio MCP server whose stray writes would
    corrupt the JSON-RPC stream).
    """
    _require_fallback(fallback, fallback_factory)
    try:
        return fn()
    except PlatformCompositionError:
        raise
    except Exception:
        return _context_degrade(fallback, fallback_factory, log_message)


async def async_safe_context_call(
    fn: "Callable[[], Awaitable[_T]]",
    *,
    fallback: _T = _UNSET,
    fallback_factory: "Optional[Callable[[], _T]]" = None,
    log_message: "str | None" = None,
) -> _T:
    """Async sibling of :func:`safe_context_call` (same fail-closed contract).

    For coroutine context calls (e.g. an aiohttp ``on_startup`` / ``on_cleanup``
    hook).  ``fn`` returns an awaitable; everything else — the required-fallback
    guard, re-raise ``PlatformCompositionError``, degrade any other error via the
    shared :func:`_context_degrade` (lazy-vs-eager fallback + debug-log) — is the
    same as the sync version, so a fail-closed policy change is made in one place
    for both.
    """
    _require_fallback(fallback, fallback_factory, where="async_safe_context_call")
    try:
        return await fn()
    except PlatformCompositionError:
        raise
    except Exception:
        return _context_degrade(fallback, fallback_factory, log_message)


def redact_via_context(text: str) -> str:
    """Redact credentials/exfil from *text* through the active PlatformContext.

    The single, canonical credential-redaction shim every egress site should
    import — instead of hand-writing the ``try current_context().credentials
    .redact / except PlatformCompositionError: raise / except Exception:
    fallback`` idiom (the bug that previously recurred in several copies).

    Routes through ``current_context().credentials.redact`` so a loaded Amazon
    companion's extra credential/cookie regexes apply.  The Default
    ``CredentialPolicy.redact`` delegates to ``security.redact``, so a standalone
    process gets byte-for-byte today's redaction.  Recursion-safe: the Default
    delegates to the bare ``security.redact``, which never calls back into the
    context — only *callers* route through this shim.

    Fail-closed: a :class:`PlatformCompositionError` (a non-standalone host that
    could not compose its companion) is re-raised, never swallowed, so such a
    host does NOT silently downgrade redaction to the OSS baseline.  Any other
    (transient) adapter failure degrades to the bare ``security.redact`` so the
    security pass never silently disappears.

    No logging on the degrade path: this shim runs inside stdio MCP servers
    (``mcp_core`` / ``mcp_cron``) whose stray writes would corrupt the JSON-RPC
    stream.
    """
    # Deferred import: keep ``security`` (which pulls the redaction regex stack)
    # off the platform module-load path; only the fallback path needs it, and
    # the happy path never imports it.
    try:
        return current_context().credentials.redact(text)
    except PlatformCompositionError:
        raise
    except Exception:
        from kiro_crew.security import redact as _security_redact

        return _security_redact(text)
