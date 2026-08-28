"""Per-backend descriptors for the ACP backends Kiro Crew can drive.

``acp/types.py`` owns the backend *vocabulary* — the id constants, the
``ACP_BACKENDS_KNOWN`` membership gate, the ``ACP_BACKENDS_SELECTABLE`` gate an
operator's ``agent.acp_backend`` is validated against, and the provider labels.
This module adds the layer above it: what each backend can and cannot do, which
protocol dialect it speaks, and how its tool decisions reach Kiro Crew's own
PreToolUse gate.

The point is that adding a backend is a data change here rather than an audit of
call sites. A call site outside a backend's own dialect adapter asks
``supports(backend, CAP_X)`` or ``dialect_of(backend)``; it does not compare ids.

Selectability is deliberately NOT expressed here. It stays in
``ACP_BACKENDS_KNOWN`` / ``ACP_BACKENDS_SELECTABLE`` so there is exactly one
answer to "may an operator persist this value", and a descriptor cannot drift
from it.

``agent.provider`` stays ``enum=["acp"]``. Selection is ``agent.acp_backend``.
Adapters are operator-installed, never bundled, and discovered through the
upstream ACP Registry. This module is the descriptor layer for those backends:
what each can do, which dialect it speaks, and how its tool decisions reach
Kiro Crew's PreToolUse gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_KIRO_CREDITS,
)


class Dialect(str, Enum):
    """Which ACP wire dialect a backend speaks.

    KIRO is kiro-cli's variant: a date-string ``protocolVersion``,
    ``session/set_mode`` for agent activation, ``session/set_model``, and an
    empty ``mcpServers`` in session params because the agent config supplies
    them. SPEC is the public ACP spec: an integer ``protocolVersion``, no
    ``set_mode``, the model set through ``session/set_config_option``, and
    ``mcpServers`` carried in the session params because the adapter reads no
    Kiro Crew config.
    """

    KIRO = "kiro"
    SPEC = "spec"


class Routing(str, Enum):
    """How a backend is made to ask Kiro Crew before running a tool.

    Kiro Crew's PreToolUse gate — the bundled denied-command rules, the
    sensitive-path block, the governance ceiling — runs from exactly one place,
    ``HookManager.on_tool_call``, reached only from the permission-request
    branch of the dispatch parser. A backend that does not send
    ``session/request_permission`` per tool call is a backend where none of
    those controls execute, so how each backend is made to ask is a property
    worth naming rather than assuming.

    AGENT_SPEC: kiro-cli asks because the spawn names an agent.
    SEEDED_SETTINGS: Kiro Crew writes the adapter's own settings file to make it
    ask, so the precondition is confirmable by reading back what was written.
    SESSION_CONFIG: the ACP v1 session advertises a config option whose enforced
    value makes privileged tools ask. Kiro Crew verifies the option and applies
    it before the first prompt.
    PERMISSION_REQUEST: the adapter's ACP path sends ``session/request_permission``
    for privileged tools even when the client does not serve ``fs/*``. The gate
    sees the request, not the bytes. Used for goose and pi. OpenCode is
    SEEDED_SETTINGS: its own default is permissive, so Kiro Crew writes
    ``permission: ask`` into the session work_dir and reads it back.
    UNVERIFIED: Kiro Crew has NOT established how (or whether) this adapter can
    be made to ask. The default for anything discovered through the registry.
    Always resolves INDETERMINATE, so it refuses unless the operator sets the one
    named opt-out. This member exists so that "we do not know" is a state the
    type system forces a caller to handle, rather than an absent case that falls
    through to a permissive branch.
    """

    AGENT_SPEC = "agent_spec"
    SEEDED_SETTINGS = "seeded_settings"
    SESSION_CONFIG = "session_config"
    PERMISSION_REQUEST = "permission_request"
    UNVERIFIED = "unverified"


class Level(str, Enum):
    """How well a backend supports one capability.

    DEGRADED exists so that "works differently" is not recorded as either
    "works" or "missing". UNVERIFIED is distinct from UNAVAILABLE: it records
    that Kiro Crew has not measured the behavior, rather than claiming the
    behavior is absent. Code gates remain fail-closed while disclosure surfaces
    can explain why.
    """

    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"


# Capability keys. Named constants rather than bare strings so a typo is an
# ImportError instead of a silently-False lookup.
CAP_SESSION_SHARING = "session_sharing"
CAP_REASONING_EFFORT = "reasoning_effort"
CAP_TOOL_SEARCH = "mcp_tool_search"
CAP_AGENT_PROFILES = "agent_profiles"
CAP_SLASH_COMMANDS = "slash_commands"
CAP_TURN_USAGE = "turn_usage"
CAP_BILLING = "billing"
CAP_NATIVE_RESUME = "native_resume"
CAP_REGISTRY_MODEL_IDS = "registry_model_ids"
CAP_MID_TURN_STEER = "mid_turn_steer"

# Every key a descriptor must answer for. A descriptor missing one is a
# programming error, not a False.
ALL_CAPABILITIES: tuple[str, ...] = (
    CAP_SESSION_SHARING,
    CAP_REASONING_EFFORT,
    CAP_TOOL_SEARCH,
    CAP_AGENT_PROFILES,
    CAP_SLASH_COMMANDS,
    CAP_TURN_USAGE,
    CAP_BILLING,
    CAP_NATIVE_RESUME,
    CAP_REGISTRY_MODEL_IDS,
    CAP_MID_TURN_STEER,
)


class UnknownAcpBackend(ValueError):
    """Raised for a backend id with no descriptor."""


@dataclass(frozen=True)
class BackendDescriptor:
    """What one ACP backend is, as data.

    ``signin_command`` is surfaced verbatim in the not-authenticated error and
    the doctor row, so a Codex host is never told to run ``kiro-cli login``.
    ``install_command`` is surfaced the same way in the dashboard's switch
    confirmation, and it names the package the RESOLUTION LADDER can actually
    find: the official adapters publish under the ``@agentclientprotocol``
    scope, but a global install of the scoped package puts the UNSCOPED binary
    (``codex-acp``) on PATH, which is what the ladder looks for. A command whose
    resulting binary the ladder cannot resolve is worse than no instruction,
    because it fails after the operator believes they complied.
    ``credential_leaves`` are home-relative paths added to the sensitive-path
    list, which is why they name a file rather than a directory where the
    directory also holds non-secret configuration. ``process_markers`` are
    code-owned compatibility hints for legacy PID entries that predate captured
    process-start identities; registry metadata never contributes kill authority.
    """

    id: str
    label: str
    experimental: bool
    dialect: Dialect
    routing: Routing
    signin_command: str
    install_command: str
    registry_id: str
    credential_leaves: tuple[str, ...]
    process_markers: tuple[str, ...]
    permission_config_id: str
    permission_config_value: str
    capabilities: Mapping[str, Level]


_KIRO = BackendDescriptor(
    id=ACP_BACKEND_KIRO,
    label="Kiro CLI",
    experimental=False,
    dialect=Dialect.KIRO,
    routing=Routing.AGENT_SPEC,
    signin_command="kiro-cli login",
    install_command="",
    registry_id="",
    credential_leaves=(),
    process_markers=("kiro-cli",),
    permission_config_id="",
    permission_config_value="",
    capabilities={cap: Level.SUPPORTED for cap in ALL_CAPABILITIES},
)

_CLAUDE = BackendDescriptor(
    id=ACP_BACKEND_CLAUDE,
    label="Claude Code",
    experimental=True,
    dialect=Dialect.SPEC,
    routing=Routing.SEEDED_SETTINGS,
    signin_command="claude",
    install_command="npm install -g @agentclientprotocol/claude-agent-acp",
    registry_id="claude-acp",
    credential_leaves=(".claude/.credentials.json",),
    process_markers=("claude-agent-acp", "claude"),
    permission_config_id="",
    permission_config_value="",
    capabilities={
        # One process per session: the adapter is served by the legacy
        # AcpClient path, not the multiplexed runtime.
        CAP_SESSION_SHARING: Level.UNAVAILABLE,
        # Pushed through the adapter's own config option when it advertises one.
        CAP_REASONING_EFFORT: Level.DEGRADED,
        CAP_TOOL_SEARCH: Level.UNAVAILABLE,
        # Prompt instructions and skills are injected, but there is no
        # session/set_mode equivalent, so a profile that narrows tools cannot
        # be enforced and is refused.
        CAP_AGENT_PROFILES: Level.DEGRADED,
        # Sent as prompt text rather than a native command dispatch.
        CAP_SLASH_COMMANDS: Level.DEGRADED,
        CAP_TURN_USAGE: Level.DEGRADED,
        # claude-agent-acp ships a real per-turn dollar figure on `usage_update`
        # (`cost: {amount: total_cost_usd, currency: "USD"}`) plus a plan rate-limit
        # block under `_meta["_claude/rateLimit"]`, and Kiro Crew consumes both: the
        # amount into AcpPromptStats.usage_cost, the quota into .rate_limit and on
        # to the dashboard's context popover. DEGRADED, not SUPPORTED, and the
        # remaining gap is specific: the figure is a CUMULATIVE session total, so a
        # per-turn delta cannot be derived from it, and the SDK's overage and
        # credit-purchase flags describe a billing flow Kiro Crew does not drive.
        CAP_BILLING: Level.DEGRADED,
        CAP_NATIVE_RESUME: Level.SUPPORTED,
        CAP_REGISTRY_MODEL_IDS: Level.SUPPORTED,
        CAP_MID_TURN_STEER: Level.UNAVAILABLE,
    },
)

_GOOSE = BackendDescriptor(
    id=ACP_BACKEND_GOOSE,
    label="goose",
    experimental=True,
    dialect=Dialect.SPEC,
    routing=Routing.PERMISSION_REQUEST,
    signin_command="goose configure",
    # goose ships its ACP server in the goose binary itself (`goose acp`), so
    # there is no separate adapter package to install — unlike the npx-distributed
    # claude and codex adapters. The registry lists it as a binary distribution.
    install_command="",
    registry_id="goose",
    # goose holds provider credentials in its own config/keyring and Kiro Crew
    # never reads them, so it names no credential leaf to block.
    credential_leaves=(),
    process_markers=("goose",),
    permission_config_id="",
    permission_config_value="",
    capabilities={
        # One process per session on the AcpClient path, like the other adapters.
        CAP_SESSION_SHARING: Level.UNAVAILABLE,
        CAP_REASONING_EFFORT: Level.UNAVAILABLE,
        CAP_TOOL_SEARCH: Level.UNAVAILABLE,
        # Prompt instructions and skills are injected, but goose runs its own
        # extensions and modes. Tool-restricting profiles cannot be enforced
        # and are refused.
        CAP_AGENT_PROFILES: Level.DEGRADED,
        # goose exposes its own slash commands, not Kiro Crew's set.
        CAP_SLASH_COMMANDS: Level.DEGRADED,
        # Measured in the shipped 1.46.0 binary, which carries its own
        # goose::acp::server::build_usage_updates alongside the `usage_update`
        # serde tag — goose is the best-instrumented of the three spec adapters,
        # not the worst, and an earlier UNAVAILABLE here was simply wrong.
        # DEGRADED rather than SUPPORTED because the rich per-message fields
        # (input/output/cache tokens, accumulated cost, a costSource provenance
        # flag) sit behind a `_goose/unstable/*` method Kiro Crew does not speak,
        # leaving only the spec `used`/`size` pair reachable.
        CAP_TURN_USAGE: Level.DEGRADED,
        CAP_BILLING: Level.UNAVAILABLE,
        # 1.47 advertises loadSession and session/load returns modes without
        # error. Crew still skips load: a successful RPC is not a measured
        # transcript restore, and spawn_continue must not start a blank child.
        CAP_NATIVE_RESUME: Level.UNAVAILABLE,
        # goose model ids come from whichever provider it is configured against,
        # so they are not model_registry keys.
        CAP_REGISTRY_MODEL_IDS: Level.UNAVAILABLE,
        CAP_MID_TURN_STEER: Level.UNAVAILABLE,
    },
)


_CODEX = BackendDescriptor(
    id=ACP_BACKEND_CODEX,
    label="OpenAI Codex",
    experimental=True,
    dialect=Dialect.SPEC,
    routing=Routing.SESSION_CONFIG,
    signin_command="codex login",
    install_command="npm install -g @agentclientprotocol/codex-acp",
    registry_id="codex-acp",
    # The leaf, not the directory: $CODEX_HOME also holds config.toml, and
    # blocking the directory would hide that file from operator diagnosis.
    credential_leaves=(".codex/auth.json",),
    process_markers=("codex-acp", "codex"),
    # codex-acp's default "agent" mode permits writes inside the workspace
    # without asking. Its ACP v1 `mode` selector is the enforceable boundary:
    # read-only still permits reads, but commands and changes request approval.
    permission_config_id="mode",
    permission_config_value="read-only",
    capabilities={
        CAP_SESSION_SHARING: Level.UNAVAILABLE,
        # codex-acp advertises a dedicated `reasoning_effort` config option;
        # Kiro Crew applies it live through session/set_config_option.
        CAP_REASONING_EFFORT: Level.SUPPORTED,
        CAP_TOOL_SEARCH: Level.UNAVAILABLE,
        # Prompt instructions and skills work; restricted tool profiles do not.
        CAP_AGENT_PROFILES: Level.DEGRADED,
        CAP_SLASH_COMMANDS: Level.DEGRADED,
        # codex-acp DOES forward per-turn context tokens: createUsageUpdate emits
        # `{used: lastTokenUsage.totalTokens, size: modelContextWindow}`. An
        # earlier UNAVAILABLE here, with a comment claiming it "forwards neither
        # token counts nor credits", conflated two different things — it forwards
        # tokens and no billing. DEGRADED: tokens only, and `used`/`size` are
        # cumulative context rather than a per-turn delta.
        CAP_TURN_USAGE: Level.DEGRADED,
        # The adapter tracks rate limits for its own /status output but emits no
        # ACP billing/cost update Kiro Crew can consume.
        CAP_BILLING: Level.UNAVAILABLE,
        CAP_NATIVE_RESUME: Level.SUPPORTED,
        # Codex ids are not model_registry keys.
        CAP_REGISTRY_MODEL_IDS: Level.UNAVAILABLE,
        CAP_MID_TURN_STEER: Level.UNAVAILABLE,
    },
)

_OPENCODE = BackendDescriptor(
    id=ACP_BACKEND_OPENCODE,
    label="OpenCode",
    experimental=True,
    dialect=Dialect.SPEC,
    routing=Routing.SEEDED_SETTINGS,
    signin_command="opencode auth login",
    install_command="",
    registry_id="opencode",
    credential_leaves=(),
    process_markers=("opencode",),
    permission_config_id="",
    permission_config_value="",
    capabilities={
        CAP_SESSION_SHARING: Level.UNAVAILABLE,
        CAP_REASONING_EFFORT: Level.UNVERIFIED,
        CAP_TOOL_SEARCH: Level.UNAVAILABLE,
        CAP_AGENT_PROFILES: Level.DEGRADED,
        CAP_SLASH_COMMANDS: Level.DEGRADED,
        CAP_TURN_USAGE: Level.UNVERIFIED,
        CAP_BILLING: Level.UNAVAILABLE,
        CAP_NATIVE_RESUME: Level.UNVERIFIED,
        CAP_REGISTRY_MODEL_IDS: Level.UNAVAILABLE,
        CAP_MID_TURN_STEER: Level.UNAVAILABLE,
    },
)

_PI = BackendDescriptor(
    id=ACP_BACKEND_PI,
    label="pi",
    experimental=True,
    dialect=Dialect.SPEC,
    routing=Routing.PERMISSION_REQUEST,
    signin_command="pi",
    install_command="npm install -g pi-acp",
    registry_id="pi-acp",
    credential_leaves=(),
    process_markers=("pi-acp", "pi"),
    permission_config_id="",
    permission_config_value="",
    capabilities={
        CAP_SESSION_SHARING: Level.UNAVAILABLE,
        CAP_REASONING_EFFORT: Level.UNAVAILABLE,
        CAP_TOOL_SEARCH: Level.UNAVAILABLE,
        CAP_AGENT_PROFILES: Level.DEGRADED,
        CAP_SLASH_COMMANDS: Level.DEGRADED,
        CAP_TURN_USAGE: Level.UNVERIFIED,
        CAP_BILLING: Level.UNAVAILABLE,
        CAP_NATIVE_RESUME: Level.UNVERIFIED,
        CAP_REGISTRY_MODEL_IDS: Level.UNAVAILABLE,
        CAP_MID_TURN_STEER: Level.UNAVAILABLE,
    },
)

_KAS = BackendDescriptor(
    id=ACP_BACKEND_KAS,
    label="Kiro Agent Service",
    experimental=True,
    dialect=Dialect.KIRO,
    routing=Routing.AGENT_SPEC,
    signin_command="kiro-cli login",
    install_command="",
    registry_id="",
    # KAS reads and refreshes the token itself through its own FileAuthProvider;
    # Kiro Crew never handles the credential, so it names no leaf here.
    credential_leaves=(),
    process_markers=("kiro-cli",),
    permission_config_id="",
    permission_config_value="",
    # A descriptor records what a backend does TODAY, not what it ought to do.
    # KAS diverges from kiro-cli at exactly five measured points — four inline
    # branches in AcpRuntime (protocol version, clientCapabilities, spawn argv,
    # the is_kiro_cli sandbox classification) and the skipped local-transcript
    # stat in providers/acp.py — and takes the kiro arm everywhere else. So every
    # level below that is not independently measured mirrors kiro, which is what
    # makes wiring gates to this table behaviour-preserving for KAS. Tightening
    # one of these is a behaviour change and needs its own evidence, not a guess
    # about what a remote service probably supports.
    capabilities={
        # Measured against ACP_BACKENDS_SESSION_SHARING, which excludes KAS.
        # Taking the multiplexed AcpRuntime arm is NOT the same capability:
        # `ACP_BACKENDS_ACP_RUNTIME` is a deliberate superset, since running on
        # the shared runtime is necessary for session sharing but not sufficient
        # — KAS is held out until keep-aware teardown lands. An earlier revision
        # of this row read SUPPORTED because it measured the start arm instead of
        # the advertised capability.
        CAP_SESSION_SHARING: Level.UNAVAILABLE,
        # Wired: AcpRuntime.create_session / load_session send
        # ``_meta.kiro.customAgents`` from ``build_kas_custom_agents`` and
        # activate the injected mode via session/set_mode. DEGRADED because
        # hooks / slashCommand / toolsSettings have no slot on that wire
        # schema. Translation failure fails closed (no privilege-escalation
        # onto KAS's default mode).
        CAP_AGENT_PROFILES: Level.DEGRADED,
        # These paths currently inherit the kiro runtime arm, but have not been
        # independently measured against KAS. Do not present inheritance as
        # backend evidence.
        CAP_REASONING_EFFORT: Level.UNVERIFIED,
        CAP_TOOL_SEARCH: Level.UNVERIFIED,
        CAP_SLASH_COMMANDS: Level.UNVERIFIED,
        CAP_TURN_USAGE: Level.UNVERIFIED,
        CAP_BILLING: Level.UNVERIFIED,
        # Handshake ``loadSession`` has not been captured on a live KAS
        # process. AcpRuntime.load_session is KAS-aware (re-injects custom
        # agents) and is handshake-gated, but KAS teardown maps to session
        # delete, so keep / spawn_continue stay fail-closed. Do not mark
        # SUPPORTED from the kiro-cli front alone.
        CAP_NATIVE_RESUME: Level.UNVERIFIED,
        CAP_REGISTRY_MODEL_IDS: Level.UNVERIFIED,
        # Measured: default spawn is kiro-cli's ACP surface (same
        # ``_session/steer``), and KAS emits the steering_* lifecycle
        # frames Crew already maps. See ACP_BACKENDS_STEER.
        CAP_MID_TURN_STEER: Level.SUPPORTED,
    },
)

_BY_ID: dict[str, BackendDescriptor] = {
    d.id: d for d in (_KIRO, _CLAUDE, _CODEX, _KAS, _GOOSE, _OPENCODE, _PI)
}


def canonical_backend_id(backend: str) -> str:
    """Map a registry id onto the hand-written backend it names, if any.

    The registry's identity for Codex is ``codex-acp``; Kiro Crew persists
    ``codex``. Resolving the registry spelling as a synthesized UNVERIFIED
    descriptor would refuse a backend we already know how to gate.
    """
    for descriptor in _BY_ID.values():
        if descriptor.registry_id and descriptor.registry_id == backend:
            return descriptor.id
    return backend


def descriptor_for_registry_adapter(
    adapter_id: str,
    label: str = "",
    install_command: str = "",
    registry_id: str = "",
) -> BackendDescriptor:
    """Synthesise a descriptor for an adapter Kiro Crew does not hand-describe.

    The registry lists dozens of adapters and the hand-written table covers a
    handful. Rather than a table entry per adapter — a constant, membership
    decisions in every capability set and ten capability levels apiece, all
    guessed — an unrecognised adapter gets this: a descriptor that CLAIMS NOTHING.

    Every field is the conservative answer, and that is the whole design:

    - ``routing=Routing.UNVERIFIED`` so the tool gate resolves INDETERMINATE and
      refuses the session unless the operator sets the one named opt-out. The
      alternative — assuming an unknown adapter asks before running a tool — is
      the exact failure this module exists to prevent, and it fails silently
      because a gate that is never consulted raises nothing.
    - Every capability ``UNVERIFIED``. Nothing has been observed, which is not
      evidence that a feature is absent. Code gates still treat it as off while
      the UI says what is actually known.
    - ``dialect=Dialect.SPEC``, the plain-ACP dialect. Kiro's dialect carries
      ``_meta.kiro`` extensions no third-party adapter implements.
    - No ``credential_leaves``: Kiro Crew cannot know where an unknown adapter
      keeps its credentials, and inventing a path would block a file for no
      reason while leaving the real one exposed.

    The one thing that matters most is what this function does NOT do: it never
    adds the adapter to ``ACP_BACKENDS_INTERNAL_SANDBOX``. That set is the seatbelt
    waiver — membership makes ``sandbox.wrap_argv`` skip Kiro Crew's own
    confinement in favour of the harness's internal one. A registry adapter is
    third-party code of unknown provenance, so it is always wrapped. Since
    membership is by explicit listing in ``acp/types.py`` and a synthesised
    descriptor is not listed anywhere, that holds by construction rather than by
    remembering to check here (harness parity H6/H7).
    """
    return BackendDescriptor(
        id=adapter_id,
        label=label or adapter_id,
        experimental=True,
        dialect=Dialect.SPEC,
        routing=Routing.UNVERIFIED,
        # The vendor CLI owns sign-in and Kiro Crew does not know its command, so
        # it says so rather than naming a command that may not exist.
        signin_command="",
        install_command=install_command,
        registry_id=registry_id or adapter_id,
        credential_leaves=(),
        # Registry data is display/launch metadata, never process-kill authority.
        # New PID records carry an OS process-start identity; legacy records are
        # deliberately not reaped for an adapter Crew did not hand-describe.
        process_markers=(),
        permission_config_id="",
        permission_config_value="",
        capabilities={cap: Level.UNVERIFIED for cap in ALL_CAPABILITIES},
    )


def descriptor_for(
    backend: str,
    *,
    registry_adapters: Mapping[str, Any] | None = None,
) -> BackendDescriptor:
    """Return the descriptor for ``backend``.

    Raises rather than falling back to kiro: an unrecognised id that resolved to
    the default would spawn a different agent than the operator asked for.
    """
    resolved = canonical_backend_id(backend)
    descriptor = _BY_ID.get(resolved)
    if descriptor is not None:
        return descriptor

    from kiro_crew.acp import registry

    adapters = registry.cached() if registry_adapters is None else registry_adapters
    adapter = adapters.get(resolved)
    if adapter is not None:
        return descriptor_for_registry_adapter(
            resolved,
            adapter.name,
            adapter.install_command,
            registry_id=adapter.id,
        )
    raise UnknownAcpBackend(
        f"No descriptor for acp_backend {backend!r}; "
        f"known backends are {sorted(selectable_ids())}"
    )


def level(backend: str, capability: str) -> Level:
    """Return how well ``backend`` supports ``capability``."""
    descriptor = descriptor_for(backend)
    try:
        return descriptor.capabilities[capability]
    except KeyError:
        raise UnknownAcpBackend(
            f"Backend {backend!r} declares no level for capability {capability!r}"
        ) from None


def supports(backend: str, capability: str) -> bool:
    """True only when ``backend`` fully supports ``capability``.

    DEGRADED answers False, so a code gate never treats a partially-working
    capability as working. UNVERIFIED is also fail-closed. Disclosure surfaces
    read ``level()`` so they can distinguish both from known absence.
    """
    return level(backend, capability) is Level.SUPPORTED


def bills_kiro_credits(backend: str) -> bool:
    """True when *backend* draws down the signed-in Kiro account's credit plan.

    Membership, not a capability level: ``CAP_BILLING`` says whether Kiro Crew can
    READ a backend's cost signal at all — claude-agent-acp is ``DEGRADED`` there
    because it reports a real dollar figure — which is a different question from
    whose balance the turn moved. See ``ACP_BACKENDS_KIRO_CREDITS`` for why the
    two must not be conflated.

    Deliberately membership-only rather than a ``descriptor_for`` lookup, so an id
    with no descriptor (a registry adapter absent from the cache) answers False
    instead of raising: the caller is a readout, and hiding a number is a correct
    degraded state where showing another account's balance is not.
    """
    return backend in ACP_BACKENDS_KIRO_CREDITS


def dialect_of(backend: str) -> Dialect:
    """Return the wire dialect ``backend`` speaks."""
    return descriptor_for(backend).dialect


def is_spec_dialect(backend: str) -> bool:
    """True when ``backend`` speaks the public ACP spec rather than kiro's."""
    return dialect_of(backend) is Dialect.SPEC


def known_ids() -> frozenset[str]:
    """The ids that have descriptors.

    Mirrors ``ACP_BACKENDS_KNOWN``; a test pins the two together in both
    directions so the descriptor table cannot drift from the membership gate.
    """
    return frozenset(_BY_ID)


def selectable_ids() -> frozenset[str]:
    """Backends admitted to the initial operator-facing preview.

    Registry data still feeds discovery and descriptors, but launch metadata is
    not validation evidence and cannot widen the reviewed selection set.
    """
    from kiro_crew.acp.types import ACP_BACKENDS_SELECTABLE

    return ACP_BACKENDS_SELECTABLE


def credential_leaves() -> tuple[str, ...]:
    """Home-relative credential paths across every registered backend.

    Consumed when building the sensitive-path list, so registering a backend is
    what protects its credential store rather than a separate hand edit.
    """
    leaves: list[str] = []
    for descriptor in _BY_ID.values():
        for leaf in descriptor.credential_leaves:
            if leaf not in leaves:
                leaves.append(leaf)
    return tuple(leaves)


def process_markers() -> tuple[str, ...]:
    """Process-name markers across every registered backend."""
    markers: list[str] = []
    for descriptor in _BY_ID.values():
        for marker in descriptor.process_markers:
            if marker not in markers:
                markers.append(marker)
    return tuple(markers)


__all__ = [
    "ALL_CAPABILITIES",
    "BackendDescriptor",
    "CAP_AGENT_PROFILES",
    "CAP_BILLING",
    "CAP_MID_TURN_STEER",
    "CAP_NATIVE_RESUME",
    "CAP_REASONING_EFFORT",
    "CAP_REGISTRY_MODEL_IDS",
    "CAP_SESSION_SHARING",
    "CAP_SLASH_COMMANDS",
    "CAP_TOOL_SEARCH",
    "CAP_TURN_USAGE",
    "Dialect",
    "Level",
    "Routing",
    "UnknownAcpBackend",
    "bills_kiro_credits",
    "canonical_backend_id",
    "credential_leaves",
    "descriptor_for",
    "descriptor_for_registry_adapter",
    "dialect_of",
    "is_spec_dialect",
    "known_ids",
    "level",
    "process_markers",
    "selectable_ids",
    "supports",
]
