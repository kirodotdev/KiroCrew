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

_logger = logging.getLogger(__name__)

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


# ── Reserved (declared-inert) slots ──
#
# A field listed here is part of the published contract but has NO consumption
# site in the core: overriding it changes NOTHING.  The declaration lives HERE,
# next to the dataclass an implementor actually reads, because the previous
# "NOT YET WIRED" notes were buried in ``interfaces.py``/``defaults.py``
# docstrings — invisible from the composition root where an edition author
# writes ``dataclasses.replace(ctx, package_manager=MyPackageManager())`` and
# gets silence.
#
# Two enforcement arms keep this honest and rot-proof, both in
# ``test_platform_cpp_seam_coverage.py``:
#
#   1. Every ``PlatformContext`` field must be EITHER consumed by non-platform
#      core code OR listed here.  A new field with no consumption site fails the
#      build instead of becoming the next dead seam.
#   2. A field listed here must have NO non-platform consumption site.  So
#      wiring a reserved slot fails the build until its entry is deliberately
#      REMOVED from this map — the marker cannot silently outlive the inertness
#      it documents.
#
# At runtime, ``PlatformContext.__post_init__`` logs one loud warning per
# reserved slot that carries a non-default value (see ``_warn_reserved_slots``).
# It WARNS rather than raises on purpose: an edition may legitimately compose an
# adapter in anticipation of a slot being wired (its own composition root is
# built in lockstep with the core), and refusing to compose would turn a
# forward-looking, harmless override into a boot failure — a breaking change for
# an out-of-tree companion that composes this seam today.  The warning is the
# signal; the test suite is the gate.
RESERVED_SLOTS: "dict[str, str]" = {
    "embeddings": (
        "no core call site: the public embedding runtime is the bundled "
        "in-process llama.cpp model, so there is no HTTP embed path to source a "
        "model/endpoint/signature from. Swap runtimes via "
        "embeddings.register_embedding_backend() instead."
    ),
    "package_manager": (
        "no core call site: install paths (ollama, ffmpeg, whisper) are inline "
        "step-by-step brew/curl/pip logic in cli_doctor.py, not a single "
        "plan-resolution point this seam could own."
    ),
    "feature_apps": (
        "no core call site: bundled apps are discovered through "
        "AppsLoader.manifest_sources()/bundled_app_names() and registered by "
        "apps/manager.py. This slot is a provenance record only."
    ),
}

# Reserved METHODS on an otherwise-live field.  Same contract as
# ``RESERVED_SLOTS`` but one level finer: the field IS consumed, only these
# methods are not.  Declared here (rather than only in an ``interfaces.py``
# docstring) so the reserved surface of the contract is readable in ONE place,
# and asserted by the same coverage test — which fails if a listed method gains
# a non-platform caller, forcing the entry to be removed deliberately.
RESERVED_METHODS: "dict[str, dict[str, str]]" = {
    "agent_runtime": {
        "managed_mcp_servers": (
            "no core call site: the agent config is built from the "
            "agent._MANAGED_MCP_SERVERS module global directly. Contribute extra "
            "servers via McpToolingProvider.extra_mcp_servers(), which IS wired "
            "and merges ADD-only into the same map. (The sibling "
            "run_first_run_setup IS wired — see slack/gateway.py.)"
        ),
    },
    "identity": {
        "whoami": (
            "no core call site: the resolved principal is not displayed or "
            "authorized against anywhere in the core. IdentityProvider.status() "
            "is the wired surface — return the principal in its payload."
        ),
        "issuer": (
            "no core call site: nothing in the core branches on or displays the "
            "identity issuer. Surface it through status() instead."
        ),
    },
}


def _reserved_slot_is_default(field_name: str, value: Any) -> bool:
    """True when *value* is the stock ``Default*`` adapter for a reserved slot.

    Compares by the adapter CLASS actually composed by
    ``bootstrap.build_default_context``, resolved lazily from ``defaults`` so
    this module stays import-light and cycle-free.  Class identity (not
    ``isinstance``) is deliberate: a companion SUBCLASS of a ``Default*``
    adapter is a real override — it can change behavior — so it must still warn.

    Unknown/unresolvable adapters are treated as NON-default (warn), keeping the
    signal loud rather than silently swallowing an override.
    """
    if field_name == "feature_apps":
        # Not an adapter — the default is the empty tuple.
        return value == ()
    try:
        # circular import: this module deliberately carries NO runtime
        # ``kiro_crew`` import — every one above is inside ``TYPE_CHECKING`` —
        # because it is the leaf the rest of the platform imports.  ``defaults``
        # pulls in ``kiro_crew.security`` and ``sso_status``, which reach back
        # into ``platform.context`` for ``current_context()`` (the documented
        # ``sel.py`` deferred pattern), so importing it at module scope here
        # would close that cycle at import time.
        from kiro_crew.platform import defaults as _defaults
    except Exception:  # pragma: no cover - defensive; defaults always importable
        return False
    default_cls = _RESERVED_DEFAULT_ADAPTERS.get(field_name)
    if default_cls is None:
        return False
    return type(value) is getattr(_defaults, default_cls, None)


# Reserved slot → the name of its stock ``Default*`` adapter class in
# ``platform.defaults``.  Kept as NAMES (not imports) so ``context`` never
# imports ``defaults`` at module load.  The coverage test asserts this map's
# keys match ``RESERVED_SLOTS`` minus the non-adapter ``feature_apps`` slot, so
# a new reserved slot cannot forget its default and silently warn on every boot.
_RESERVED_DEFAULT_ADAPTERS: "dict[str, str]" = {
    "embeddings": "DefaultEmbeddingSource",
    "package_manager": "DefaultPackageManager",
}

# One warning per (slot, adapter type) per process.  ``dataclasses.replace`` may
# rebuild the context several times during a composition root, and
# ``current_context()``'s lazy default can run in short-lived subprocesses — a
# per-construction warning would spam the log for one override.  Keyed by the
# adapter type as well as the slot so a LATER, different override still reports.
_RESERVED_WARNED: "set[Tuple[str, str]]" = set()


def _warn_reserved_slots(ctx: "PlatformContext") -> None:
    """Log one loud warning per reserved slot carrying a non-default adapter.

    The runtime half of the reserved-slot contract: an edition that composes an
    adapter into an inert slot gets a visible boot-time line naming the slot, the
    adapter, why it is inert, and the wired alternative — instead of the silence
    that made these seams look live.  Deduped per process (see
    ``_RESERVED_WARNED``).

    Never raises: this is diagnostics, and a composition-time crash here would
    break the very boot path it is meant to annotate.  Called from
    ``PlatformContext.__post_init__``, so it also covers the companion's
    ``dataclasses.replace`` path.
    """
    try:
        for field_name, reason in RESERVED_SLOTS.items():
            value = getattr(ctx, field_name, None)
            if _reserved_slot_is_default(field_name, value):
                continue
            key = (field_name, f"{type(value).__module__}.{type(value).__qualname__}")
            if key in _RESERVED_WARNED:
                continue
            _RESERVED_WARNED.add(key)
            _logger.warning(
                "PlatformContext.%s is a RESERVED slot: %s composed a non-default "
                "value into it, which the core NEVER reads, so it has NO effect. %s",
                field_name,
                key[1],
                reason,
            )
    except Exception:  # pragma: no cover - diagnostics must never break boot
        _logger.debug("reserved-slot warning pass failed", exc_info=True)


@dataclass(frozen=True)
class PlatformContext:
    """Immutable bundle of the chosen adapter for every extension point.

    Built once at boot by :func:`kiro_crew.platform.bootstrap.bootstrap_context`
    and never mutated.  The public edition composes a context whose every
    interface field is a ``Default*`` adapter; the Amazon companion replaces a
    subset via ``dataclasses.replace`` in its composition root.

    **Reserved slots.**  Fields marked ``[RESERVED]`` below are published
    contract but have NO consumption site in the core — overriding one changes
    nothing.  ``RESERVED_SLOTS`` (above) carries the reason and the wired
    alternative for each, ``RESERVED_METHODS`` does the same for individual
    methods on otherwise-live fields, and composing a non-default value into a
    reserved slot logs one loud warning at boot.  Both maps are asserted against
    real consumption sites by ``test_platform_cpp_seam_coverage.py``, so a slot
    cannot stay marked once it is wired (nor be added without a marker).
    """

    # ── carriers (not interfaces) ──
    contract_version: int
    profile: str
    cfg: "KiroCrewConfig"

    # ── boot-layer extension points ──
    providers: "ProviderRegistry"
    publish: "PublishRegistry"
    # ``run_first_run_setup`` is wired (slack/gateway.py); ``managed_mcp_servers``
    # is [RESERVED] — see RESERVED_METHODS.
    agent_runtime: "AgentRuntime"
    agent_executable: "AgentExecutableResolver"
    sandbox: "SandboxPolicy"
    credentials: "CredentialPolicy"
    security: "PolicyAuthority"
    slack_gate: "SlackEnterpriseGate"
    # ``whoami``/``issuer`` are [RESERVED] — see RESERVED_METHODS.
    identity: "IdentityProvider"
    embeddings: "EmbeddingSource"  # [RESERVED] — see RESERVED_SLOTS
    mcp_tooling: "McpToolingProvider"
    agent_catalog: "AgentCatalogProvider"
    prompt_sources: "PromptSourceProvider"
    capability_manager: "CapabilityManager"

    # ── install / structural extension points ──
    registry: "AppRegistryPolicy"
    apps_loader: "AppsLoader"
    package_manager: "PackageManager"  # [RESERVED] — see RESERVED_SLOTS
    knowledge: "KnowledgeProvider"

    # ── runtime-service / frontend extension points ──
    tunnel: "TunnelProvider"
    telemetry: "TelemetryProvider"
    dashboard: "DashboardContributor"
    jail: "JailProvider"

    # ── bundled feature apps ──
    feature_apps: "Tuple[FeatureApp, ...]"  # [RESERVED] — see RESERVED_SLOTS

    # ── governance carrier (Level 1 enterprise security ceiling) ──
    # Frozen at boot from the trust-root policy path; ``None`` on a standalone
    # host with no policy present (editable secure-defaults).  Read at every
    # enforcement chokepoint via ``current_context().governance``.  Defaulted so
    # the single constructor and the companion's ``dataclasses.replace`` paths
    # need no change beyond opting in.
    governance: "Optional[GovernanceCeiling]" = None

    def __post_init__(self) -> None:
        """Bind the ``CapabilityManager`` bound + warn on reserved-slot overrides.

        Two composition-time concerns, both of which must run on the single
        constructor AND on the companion's ``dataclasses.replace`` path (which
        re-invokes ``__init__``).

        **Reserved-slot warning.** ``_warn_reserved_slots`` emits one loud
        warning per :data:`RESERVED_SLOTS` field that carries a non-default
        adapter, turning a silently-ignored override into a visible log line at
        boot. It warns rather than raises — see the ``RESERVED_SLOTS`` comment
        for why refusing to compose would be a breaking change.

        **``CapabilityManager`` LIVENESS bound.** Every ``CapabilityManager`` is wrapped ONCE here in
        ``BoundedCapabilityManager`` so that EVERY reader of
        ``current_context().capability_manager`` — the dashboard handlers AND any
        future non-dashboard consumer that follows the documented CPP pattern of
        reading the context directly — inherits the ``asyncio.wait_for`` mutation
        bound. Enforcing it at the seam (where the Protocol declares the contract)
        rather than at a leaf dashboard accessor closes the gap where a new call
        site could obtain an unbounded manager and reintroduce the hang class the
        bound exists to prevent.

        Runs on the single constructor AND the companion's ``dataclasses.replace``
        path (``replace`` re-invokes ``__init__``); ``bind_capability_manager`` is
        idempotent, so a ``replace`` that carries an already-bounded manager
        forward is not double-wrapped. The frozen dataclass forbids normal
        assignment, so the wrap is installed via ``object.__setattr__``.
        """
        # Deferred import: keep this module import-light and avoid any chance of
        # a cycle through the interfaces/adapters at platform package load.
        from kiro_crew.platform.capability_bound import bind_capability_manager

        object.__setattr__(
            self, "capability_manager", bind_capability_manager(self.capability_manager)
        )
        _warn_reserved_slots(self)

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
