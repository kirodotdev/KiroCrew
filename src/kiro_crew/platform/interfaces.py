"""Extension-point interfaces for the Composed Platform Providers contract.

Each Protocol here is one extension point — one place where behavior differs
between the public edition and the Amazon companion.  The public core ships a
``Default*`` implementation of every one (see ``defaults.py``); the companion
supplies an Amazon implementation for the subset it overrides.

These are ``Protocol`` types (structural) rather than ABCs so an adapter need
not import-inherit — it only has to match the shape.  ``PolicyAuthority`` is the
one exception: it is a concrete class in ``security_authority.py`` because its
deny decision must be ``@final`` to enforce the ADD-only floor.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
)

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
    from kiro_crew.security import DeniedCommandRule
    from kiro_crew.skill_providers.base import SkillProvider


class InterceptDecision(enum.Enum):
    """Verdict an edition returns for an inbound Slack message at the route gate.

    Returned by ``SlackEnterpriseGate.intercept_message`` (called at the top of
    ``slack/events.py::_route_message``). The public default is always
    ``PROCESS`` — the OSS build processes every allowed message inline, so this
    seam changes nothing until a companion overrides the gate.

    - ``PROCESS`` — handle the message inline as usual (the OSS behavior).
    - ``REDIRECTED`` — the gate has handled the message out-of-band (e.g. posted a
      presigned dashboard-session challenge link) and inline processing MUST be
      short-circuited. The gate owns any user-facing reply.
    - ``DROPPED`` — the message is denied and must not be processed. Used as the
      fail-closed outcome: a companion whose challenge mint/post fails returns
      ``DROPPED`` rather than falling through to inline processing.

    ``REDIRECTED`` and ``DROPPED`` both short-circuit ``_route_message``; they
    differ only in intent (redirected = a challenge was issued; dropped = denied),
    which the gate records in its own audit trail.
    """

    PROCESS = "process"
    REDIRECTED = "redirected"
    DROPPED = "dropped"


# ── boot-layer extension points ──


class ProviderRegistry(Protocol):
    """The LLM-provider factory + ACP-backend registration seam.

    The public edition ships every KNOWN ACP backend as selectable, so this seam
    exists for a harness the core does NOT ship: an edition calls
    ``register_acp_backends`` and its id becomes selectable without the core
    changing.
    """

    def create_factory(self, cfg: "KiroCrewConfig") -> Callable[..., Any]:
        """Return the provider factory (Default: ``cfg.create_provider_factory()``).

        WIRED: every factory build site routes through
        ``config.loader.build_provider_factory`` →
        ``current_context().providers.create_factory(cfg)``. The Default returns
        exactly ``cfg.create_provider_factory()`` (identity), so the public
        edition is unchanged; a companion can return an alternate factory (e.g.
        re-registering an alternate ACP backend) — but kiro-cli stays the default
        for both editions unless the companion is explicitly opted in.
        """
        ...

    def register_acp_backends(self) -> None:
        """Register any extra ACP backends (no-op in the public edition).

        Consumed at boot by ``bootstrap_context`` after the context installs.

        An implementation MUST also call
        ``acp_backends.register_selectable_backend(<id>)`` for anything an operator
        should be able to choose. Registering the provider alone leaves the harness
        runnable but unreachable: the dashboard's backend switch, its PATCH
        allowlist and the config load path all derive from that registry, so an
        unregistered id is not offered in the dashboard at all and is coerced back
        to the default on load.
        """
        ...


class PublishRegistry(Protocol):
    """The artifact-publish-provider registration seam.

    The public edition registers NO publish provider — the
    ``publish_provider`` registry stays empty, so ``get_provider`` raises
    ``PublishUnavailableError`` (→ 503) and ``list_providers`` returns ``[]``,
    which the dashboard renders as "publishing unavailable" with no core
    branching.  A companion uses ``register_publish_providers`` to register its
    concrete providers (e.g. an internal artifact store) through the
    ``publish_provider`` module-level registry — the exact structural twin of
    ``ProviderRegistry.register_acp_backends``; the core never imports a
    companion provider.

    Whether a publish to a given destination is *permitted* is a separate,
    orthogonal decision owned by the governance ceiling
    (``capabilities.publish``) — this seam only decides WHO implements the
    transfer, never WHETHER it is allowed.
    """

    def register_publish_providers(self) -> None:
        """Register any publish providers (no-op in the public edition).

        Consumed at boot by ``bootstrap_context`` after the context installs,
        alongside ``ProviderRegistry.register_acp_backends``.
        """
        ...


class AgentRuntime(Protocol):
    """The agent runtime: managed MCP servers + first-run setup."""

    def managed_mcp_servers(self) -> Dict[str, dict]:
        """**RESERVED — not consumed by the core.** Overriding has NO effect.

        The agent config is built from the ``agent._MANAGED_MCP_SERVERS`` module
        global directly. Contribute extra servers through the WIRED
        ``McpToolingProvider.extra_mcp_servers()``, which merges ADD-only into
        the same map. Declared in ``context.RESERVED_METHODS`` and asserted by
        ``test_platform_cpp_seam_coverage.py``, which fails if this gains a
        caller without the reservation being removed.
        """
        ...

    def run_first_run_setup(self) -> None:
        """One-time install-time provisioning, run once per gateway start.

        WIRED: the gateway boot path (``slack/gateway.py``) calls this through
        the seam instead of importing ``agent.run_first_run_setup`` directly, so
        an edition can add its own one-time provisioning. The public Default
        delegates to exactly that function (PATH shim + admission-policy seed +
        one-time stale managed-MCP purge), so the standalone build is unchanged.

        Best-effort by contract: the call site keeps a failure non-fatal to
        gateway startup, so an implementation SHOULD swallow and log its own
        errors rather than relying on the caller. An override SHOULD keep the
        core steps (delegate to the same underlying function) — dropping them
        would leave desktop installs without the PATH shim.
        """
        ...


class AgentExecutableResolver(Protocol):
    """Resolve an edition-provided agent launcher to its direct executable.

    ``sandbox.py`` calls this before applying KiroCrew's OS-level sandbox so an
    edition can replace a managed launcher with the executable it ultimately
    invokes, avoiding nested isolation. The public Default is identity: ordinary
    ``kiro-cli`` paths and the explicit ``KIROCREW_KIRO_BIN`` override are used
    unchanged. Implementations must return the input unchanged when they do not
    recognize it and must never weaken or disable the outer sandbox.
    """

    def resolve_executable(self, executable: str) -> str: ...


class SandboxPolicy(Protocol):
    """The sandbox *data*: which dirs/files to hide/expose.

    The ``wrap_argv`` mechanism stays in ``sandbox.py``; only the directory and
    file lists are the extension point. Public default = the open-source
    credential-directory lists; a companion may add edition-specific paths.
    """

    def strict_dirs(self) -> List[str]: ...

    def cc_dirs(self) -> List[str]: ...


class CredentialPolicy(Protocol):
    """Redaction passes + the credential/exfil regex bundle.

    Public default = the AKIA/ASIA credential patterns and exfil URL patterns in
    ``security.py``.  The companion adds internal token/cookie regexes.
    """

    def redact(self, text: str) -> str: ...

    def exempt_exact_hosts(self) -> "frozenset[str]":
        """Exact-match hosts that skip ONLY the exfil *heuristics*.

        WIRED: ``security.scan_exfiltration_urls`` /
        ``security.redact_exfiltration_urls`` read this set (via a function-local
        deferred import of the platform context) and, for a URL whose domain is
        an EXACT member, skip the base64-blob / query-length heuristics.  This is
        **narrow-only**: it can only relax the heuristics, never the hard-credential
        floor — the S3-presigned fast-path and the unconditional
        ``_HARD_CREDENTIAL_RE`` path+query scan still run first, so a real AWS key /
        SSH-or-PEM header / Slack token on an exempted host is still flagged and
        redacted.  Matched exactly (not by suffix) so a shared multi-tenant domain
        (``*.sharepoint.com``) does not exempt every tenant.

        Public default = ``frozenset()`` (no exemptions — redaction unchanged); the
        companion supplies its own trusted-tenant host list.  The set is NEVER
        sourced from ``config.json`` — an agent-writable exemption would be a hole
        in the redaction ceiling, so the companion adapter is the only supplier.
        """
        ...


class SlackEnterpriseGate(Protocol):
    """Slack enterprise/workspace allowlist + per-message origin gate.

    Public default = open (opt-in allowlist via ``slack.allowed_enterprise_ids``).
    The companion supplies the fail-closed Amazon workspace allowlist.

    Signatures mirror ``slack/enterprise.py``: ``validate_enterprise`` is called
    once at startup with the bot token; ``check_message_origin`` is the per-
    message in-memory check.

    ``extra_ids`` is advisory, and an implementation MAY ignore it. The public
    default gate DOES ignore it: its callers derive the value from the same
    ``slack.allowed_enterprise_ids`` key that gate re-reads itself, so the value
    can only be an older copy of what the gate already has, and honouring it
    would re-admit ids the operator removed. Do not rely on this parameter to
    add ids the config does not list -- against the default gate they are
    dropped. It stays in the signature because an edition whose allowlist comes
    from somewhere other than that config key can still use it.
    """

    def validate_enterprise(
        self, bot_token: str, *, extra_ids: "set[str] | None" = None
    ) -> bool: ...

    def check_message_origin(self, event_team_id: str) -> bool: ...

    def heartbeat_safe_tools(self) -> "frozenset[str]":
        """Extra tool names an edition allows during unattended heartbeat polling.

        WIRED: ``slack/gateway.py::_is_heartbeat_safe_tool`` checks this set after
        the core ``HEARTBEAT_SAFE_TOOLS`` exact-name match. The public default is
        ``frozenset()`` (no additions — the heartbeat allowlist is byte-identical
        to today). A companion returns its own read-only tool names so its
        heartbeat polls can auto-approve them.

        **Entries MUST be server-qualified ``"@server/Tool"``.** An entry is
        honored ONLY when it matches that qualified identity; a bare tool name
        never matches (and a tool-call title with no resolvable server is never
        auto-approved from this set). This is deliberate: a bare-name allowlist
        entry would let a *different* (or compromised) MCP server expose a
        destructive tool with the same bare name as an allowlisted read-only one
        and get it auto-approved during an unattended heartbeat — a hole in the
        deny-by-default boundary. Pin the exact server (e.g.
        ``"@example-mcp/SomeReadOnlyTool"``).

        ADD-only by construction: this can only widen the allowlist with names
        the companion vouches for; it can never remove a core entry or relax the
        exact-match discipline. NEVER sourced from config — an agent-writable set
        would defeat the deny-by-default heartbeat gate, so the companion adapter
        is the only supplier. v1 method addition (no ``CONTRACT_VERSION`` bump).
        """
        ...

    def intercept_message(
        self,
        orch: Any,
        *,
        channel: str,
        sender_id: str,
        clean_text: str,
        thread_ts: "str | None",
        msg_ts: str,
    ) -> "InterceptDecision":
        """Decide the fate of an inbound Slack message before inline processing.

        WIRED: called at the TOP of ``slack/events.py::_route_message`` (after the
        user-allowlist check, before the busy/queue path). The public default
        returns ``InterceptDecision.PROCESS`` for every message, so the OSS build
        is byte-identical — every allowed message is handled inline as today.

        A companion may return ``REDIRECTED`` (it handled the message out-of-band,
        e.g. minted + posted a presigned dashboard-session challenge link) or
        ``DROPPED`` (denied) to short-circuit inline processing. This is the seam an
        enterprise edition's "challenge-and-redirect" security posture composes
        against: the first message from an unverified (user, channel) is turned into
        a one-time dashboard link instead of reaching the agent, and follow-up
        traffic flows inline only while a bounded auth window (opened at token
        consumption via ``DashboardContributor.on_token_consumed``) is live. The
        core ships no such posture; any organization can supply one through this
        seam (device-verification gate, SSO step-up, custom allow policy, …).

        **Fail-closed contract.** A gate that cannot complete its decision (e.g.
        the challenge mint/post raised) MUST return ``DROPPED``, never ``PROCESS``
        — a mis-composed or erroring gate must never silently degrade to inline
        processing. ``_route_message`` enforces this at the call site too: the seam
        is invoked through ``safe_context_call(..., fallback=DROPPED)``, so a raised
        adapter error (any non-``PlatformCompositionError``) degrades to ``DROPPED``,
        not ``PROCESS`` — deny-by-default. A ``PlatformCompositionError`` is re-raised
        (boot invariant, never degraded). The public ``Default`` returns ``PROCESS``
        and cannot raise, so a standalone build is byte-identical (its contract IS
        "process inline") and never reaches the fallback; only a composed edition
        that has declared a stricter posture can trip it.

        Receives the orchestrator plus the resolved message fields so a companion
        can key its auth window on ``(sender_id, channel)`` and thread the
        challenge back to the right ``thread_ts``. Returns quickly and does not
        block the event loop on network I/O beyond the challenge post. v1 method
        addition (no ``CONTRACT_VERSION`` bump).
        """
        ...


class IdentityProvider(Protocol):
    """SSO/identity resolution.

    Public default = local token, no SSO (the ``sso_status.py`` no-op stubs).  The
    companion resolves through an enterprise SSO / credential provider.

    ``status_line`` is async to match the existing ``get_sso_status_line``
    coroutine the dashboard awaits.

    ``credential_watch_paths`` lists credential files whose rotation should
    drain pooled MCP backends (blue-green cutover).  Public default = ``[]``
    (no watcher).  The already-booted gateway process resolves this and
    threads each path to the separately-spawned gateway daemon as a
    ``--credential-watch-path`` argv flag — the daemon itself never boots the
    platform and never hardcodes a credential path.  v1 method addition (no
    ``CONTRACT_VERSION`` bump).
    """

    def status(self) -> Dict[str, object]: ...

    async def status_line(self, prefix: str = "*SSO:*") -> str: ...

    def whoami(self) -> Optional[str]:
        """**RESERVED — not consumed by the core.** Overriding has NO effect.

        Nothing in the core displays the resolved principal or authorizes against
        it. Return it inside the WIRED ``status()`` payload instead (the dashboard
        renders that). Declared in ``context.RESERVED_METHODS`` and asserted by
        ``test_platform_cpp_seam_coverage.py``.
        """
        ...

    def issuer(self) -> Optional[str]:
        """**RESERVED — not consumed by the core.** Overriding has NO effect.

        Nothing in the core branches on or displays the identity issuer. Surface
        it through the WIRED ``status()`` payload instead. Declared in
        ``context.RESERVED_METHODS`` and asserted by
        ``test_platform_cpp_seam_coverage.py``.

        (Not to be confused with ``GovernanceCeiling.identity_issuer``, which is
        parsed from the signed security policy and IS consumed.)
        """
        ...

    def preflight_checks(self) -> List[Callable[[], None]]:
        """Already-resolved pre-launch checks for ``gateway``/``token``.

        WIRED: ``kiro_crew.preflight.run_preflight_checks`` (called from the
        ``gateway`` dispatch in ``cli.py`` and ``_token`` in ``cli_server.py``)
        runs each callable in order.  ``SystemExit`` from a check aborts the
        launch; other exceptions are logged and swallowed per check.  Public
        default = ``[]`` (no checks, startup unchanged); the companion returns
        e.g. an SSO-session freshness prompt.  Callables only — checks are
        never resolved from config strings (code-exec escalation).
        """
        ...

    def credential_watch_paths(self) -> List[Path]: ...


@dataclass(frozen=True)
class WorkloadIdentity:
    """The agent process's registered workload (name + ARN).

    Public Default never has one. A companion fills this from the edition's
    workload registration; core code must not invent an ARN.
    """

    name: str
    arn: str


@dataclass(frozen=True)
class SessionPrincipal:
    """Trusted caller for agent-identity token vending.

    Core-derived (surface + already-partitioned subject + session key). Never
    taken from tool input. ``user_jwt`` is set only by a companion after IdP
    verify; the public Default leaves it ``None``.
    """

    surface: str
    subject: str
    session_key: str
    user_jwt: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class InboundToken:
    """A short-lived inbound credential a companion vends for Gateway MCP.

    ``token`` is bearer material: it must never enter ``status()``, SEL
    payloads, or transcripts. Public Default never vends one.
    """

    scheme: str
    token: str = field(repr=False)
    expires_at: float
    audience: str


class AgentIdentityProvider(Protocol):
    """Agent workload identity and token vending — not operator SSO.

    ``IdentityProvider`` is the operator-SSO slot (status line, preflight,
    credential watch). This slot is the edition concern for a process-level
    workload identity and for vending tokens *as that workload*. Same reason
    ``AgentCatalogProvider`` is not folded into ``McpToolingProvider``.

    Public Default is disabled (``enabled() -> False``; every other method
    returns ``None`` / ``{}`` / the input principal unchanged) so a standalone
    process stays byte-identical. ``IdentityProvider.whoami`` / ``issuer`` stay
    RESERVED — do not consume them to satisfy this seam.

    v1 field addition (no ``CONTRACT_VERSION`` bump); same landing as
    ``knowledge`` / ``dashboard`` / ``jail``.
    """

    def enabled(self) -> bool: ...

    def workload_identity(self) -> WorkloadIdentity | None: ...

    def status(self) -> dict[str, object]: ...

    def gateway_mcp_spec(self) -> dict[str, object] | None: ...

    async def annotate_principal(self, principal: SessionPrincipal) -> SessionPrincipal: ...

    async def vend_workload_access_token(self, principal: SessionPrincipal) -> str | None: ...

    async def vend_gateway_inbound_token(
        self, principal: SessionPrincipal
    ) -> InboundToken | None: ...


class EmbeddingSource(Protocol):
    """**RESERVED extension point — not consumed by the core.**

    Composing an ``EmbeddingSource`` into ``PlatformContext.embeddings`` has NO
    effect: since the in-process runtime landed (bundled ``qwen3-embedding:0.6b``
    via vendored llama.cpp) the core has no HTTP embed path, so there is nothing
    to source a model id, endpoint, or request signature from. All three methods
    are inert.

    **Swap runtimes through ``embeddings.register_embedding_backend()`` instead**
    — that is the live seam (an ``EmbeddingBackend`` owns the whole embed call,
    including any remote transport and signing it needs).

    The slot is kept for contract stability and is declared in
    ``context.RESERVED_SLOTS``: a non-default value logs a loud warning at boot,
    and ``test_platform_cpp_seam_coverage.py`` fails if a consumption site is
    added without the reservation being removed.
    """

    def registry_model(self) -> str: ...

    def endpoint_url(self) -> Optional[str]: ...

    def sign_request(
        self, method: str, url: str, headers: dict, body: "bytes | str"
    ) -> Optional[dict]: ...


@dataclass(frozen=True)
class McpScope:
    """A provider-specific global MCP config scope contributed by an edition.

    ``id`` is the short scope key used in the ``/api/mcp/apply`` request body
    (``f"{id}Global"``, e.g. ``ccGlobal``). ``global_json`` is the provider's
    global MCP config file. ``agent_mcp_file`` is the rendered per-agent MCP file
    to strip on uninstall, or ``None`` if the provider has none. ``label`` is the
    human display name the dashboard shows on the scope badge (e.g. ``Claude``);
    it defaults to ``id`` when empty.
    """

    id: str
    global_json: Path
    agent_mcp_file: Optional[Path] = None
    label: str = ""


class McpToolingProvider(Protocol):
    """Extra MCP servers, skill roots, and provider MCP scopes the edition adds.

    This seam is scoped to MCP tooling ONLY. Agent-catalog contributions live on
    ``AgentCatalogProvider``, prompt/SOP roots on ``PromptSourceProvider``, and
    external package management on ``CapabilityManager`` — each a distinct edition
    concern with its own Protocol rather than accreting onto this one.

    Public default = none beyond the managed servers.  The companion injects the
    internal MCP server, internal skill paths, and its provider MCP scope(s).
    """

    def extra_mcp_servers(self) -> Dict[str, dict]: ...

    def extra_skills(self) -> List[Path]:
        """Extra SKILL.md source roots the edition contributes.

        WIRED: ``SkillsLoader.__init__`` appends these as lowest-precedence extra
        skill paths (after local + AIM), each sensitivity- and existence-checked
        like a configured ``skills.extra_paths`` entry, so an edition's SKILL.md
        catalog (e.g. a PromptFarm-synced tree, an internal recommendation-engine
        skill) is discoverable by trigger matching and the ``$skill`` picker. The
        public Default returns ``[]`` (no extra paths — discovery is unchanged).
        """
        ...

    def extra_mcp_scopes(self) -> List["McpScope"]:
        """Provider-specific global MCP config scopes the edition manages.

        WIRED: ``/api/mcp/apply``, the MCP uninstall path, AND MCP discovery
        (``mcp_discovery._extra_scope_sources``) write/scan each returned scope's
        ``global_json`` (and strip its ``agent_mcp_file``) IN ADDITION to the
        core Kiro global, keyed by ``f"{scope.id}Global"`` in the request body.
        ``GET /api/mcp/scopes`` surfaces them to the dashboard's Globals column.
        The public Default returns ``[]`` so the core writes the **Kiro scope
        only**; a companion returns e.g. the Claude Code scope
        (``~/.claude.json``) to keep that provider's config in sync.
        """
        ...


class AgentCatalogProvider(Protocol):
    """Extra agent-catalog rows the edition contributes.

    A distinct edition concern with its own interface (not folded into
    ``McpToolingProvider``) so each edition hook lands on its own interface.
    """

    def builtin_agents(self) -> List[Dict[str, Any]]:
        """Extra agent-catalog rows the edition contributes.

        WIRED: ``agent_discovery.list_agents()`` merges these AFTER the on-disk
        ``~/.kiro/agents`` scan — ADD-only and de-duped by name, so an on-disk
        agent of the same name wins. Each row is a plain dict with the
        ``AgentInfo`` fields (``name`` required; ``filename``, ``description``,
        ``model``, ``skills``, ``mcp_servers``, ``source``, ``package``
        optional).

        EXECUTABLE INVARIANT: every returned agent MUST be spawnable — the
        edition guarantees a resolvable agent configuration exists for its
        ``name`` (materialized on disk under ``~/.kiro/agents`` or otherwise
        resolvable by the ACP backend). ``list_agents()`` is the single source
        consumed as an executable-agent allowlist by ``_do_agents_sync()``
        (which PERSISTS rows into ``config.json``'s ``cfg.agents``),
        ``subagent._validate_agent()`` (spawn allowlist), and conductor
        generation, so a catalog-only row with no config behind it would be
        persisted and offered for spawning yet fail at ACP ``session/set_mode``.
        Do NOT return non-executable/catalog-only rows here. The public Default
        returns ``[]`` (discovery is the on-disk scan only).
        """
        ...


class PromptSourceProvider(Protocol):
    """Edition-contributed prompt/SOP source roots the dashboard lists.

    A distinct edition concern with its own interface (not folded into
    ``McpToolingProvider``).
    """

    def prompt_source_roots(self) -> List[Path]:
        """Edition-contributed prompt/SOP source roots the dashboard lists.

        WIRED: the dashboard prompt discovery (``_list_aim_prompts``) walks each
        returned root generically (``rglob('*.sop.md')``) to surface agent SOPs,
        emitting them with ``source: "package"``. The public Default returns
        ``[]`` so the OSS build lists NO package SOPs (only the user-authored
        ``~/.kiro/prompts`` files it already discovers); a companion returns its
        resolved package prompt/SOP roots so its installed SOPs appear in the
        ``/api/prompts`` catalog and ``@agent-sop:`` mentions. Read fail-closed
        through ``safe_context_call`` at the call site (fallback ``[]``).
        """
        ...


class SkillDiscoveryProvider(Protocol):
    """Edition-contributed skill discovery providers for the multi-provider search.

    A distinct edition concern with its own interface (not folded into
    ``McpToolingProvider``) so each edition hook lands on its own interface.
    """

    def skill_providers(self) -> List["SkillProvider"]:
        """Extra skill discovery providers the edition contributes.

        WIRED: the dashboard discover registry
        (``handlers/discover._build_registry``) registers each returned provider
        AFTER the built-in one, gated per provider by the same
        ``external_access.admits_registry("skill", name, api_base)`` decision the
        built-in provider passes through — so an edition provider is subject to
        the composed discovery policy uniformly, and a managed allowlist needs no
        edition-specific carve-out. ADD-only and de-duped by name: a provider
        whose ``name`` collides with an already-registered one is skipped with a
        warning (the built-in wins), so an edition cannot silently replace the
        catalog identity a policy admitted.

        Each returned object implements
        ``kiro_crew.skill_providers.base.SkillProvider`` (async ``search`` /
        ``fetch_skill_content`` plus ``is_available()``, so an unconfigured
        provider is skipped by the registry rather than erroring) and SHOULD
        expose an ``api_base`` naming its catalog endpoint — that is the identity
        an allowlist is written against; a provider without one is gated on an
        empty base. Read fail-closed through ``safe_context_call`` at the call
        site (fallback ``[]``). The public Default returns ``[]`` (discovery
        offers the built-in catalog only). v1 addition (no ``CONTRACT_VERSION``
        bump).
        """
        ...


class DeniedRuleProvider(Protocol):
    """Edition-contributed DENIED-COMMAND RULES that the user can switch off.

    A distinct edition concern with its own interface (not folded into
    ``SecurityOverlay``) because the two answer opposite questions.
    ``SecurityOverlay.extra_deny_patterns`` is the un-weakenable floor: its
    patterns travel the GLOB tier via ``extra_patterns`` and no user opt-out can
    reach them. This seam is the other half — rules an edition wants ON by
    default but wants the operator to be ABLE to turn off, which the overlay
    structurally cannot express.
    """

    def denied_rules(self) -> List["DeniedCommandRule"]:
        """Extra denied-command rules the edition contributes, DISABLEABLE.

        WIRED: ``hooks.resolve_effective_denied_regexes`` unions these into the
        rule list it hands ``security.compute_effective_denied``, so each rule is
        subject to the SAME opt-out resolution as a built-in — an operator can
        disable one by id, or clear the lot with ``disable_all``, through the
        existing ``denied_commands.json`` keystone and the existing
        ``/api/security/denied-commands`` endpoints. ``handlers/security`` lists
        them in the Settings panel (tagged ``source="edition"``) and accepts a
        toggle for them, so a rule contributed here is discoverable rather than
        an invisible block.

        TIER: ``pattern`` is a Python REGEX matched via ``re.search`` with
        ``re.IGNORECASE`` — the same tier and the same ReDoS-bounded matcher as a
        built-in rule. It is NOT an fnmatch glob. An edition moving a pattern
        here from ``extra_deny_patterns`` MUST rewrite it: ``*ada credentials*``
        is a glob, and as a regex its ``*`` are quantifiers on ``a`` and ``s``.

        SCOPE — the surfaces these rules reach, and the one they do not.  They
        are enforced wherever the *effective* denied set is resolved from the
        keystone opt-out state: the agent tool gate (``hooks.on_tool_call``) and
        the cron/MCP gate (``mcp_cron`` via
        ``effective_denied_regexes_from_config``).  They deliberately do NOT
        reach ``computer_use.policy.check_text_input``, which calls
        ``is_denied`` with ``denied_regexes=None`` to fail closed to the BUILT-IN
        catalogue alone — that surface ignores opt-out state on purpose (typing
        into somebody else's window is not the same decision as running a command
        under the tool gate), and honouring an edition contribution there without
        its opt-out would make the seam mean two different things on two
        surfaces.  An edition needing a rule enforced on the computer-use
        surface must express it as an un-weakenable
        ``SecurityOverlay.extra_deny_patterns`` glob instead.

        IDENTITY: ``id`` is the opt-out key and the SEL ``rule_id``, so it MUST be
        namespaced by the edition (``<edition>-<slug>``). ``disabled_ids`` is one
        flat set: a rule whose id collides with a built-in id is SKIPPED with a
        warning (the built-in wins), because a collision would make one rule's
        toggle silently move the other.

        NOT PINNABLE (v1): governance ``commands``-scope pins resolve a pattern to
        a rule id against the STATIC catalog, so a pin naming a rule from this
        seam matches nothing. An edition needing an un-opt-out-able pattern keeps
        using ``extra_deny_patterns`` — that is still the floor, unchanged.

        Read fail-closed through ``safe_context_call`` at the call site (fallback
        ``[]``): a raising provider degrades to the built-in catalog rather than
        wedging the deny gate. Losing an additive, user-disableable rule is the
        safe direction; the overlay floor is unaffected either way. The public
        Default returns ``[]`` (the built-in catalog only). v1 addition (no
        ``CONTRACT_VERSION`` bump).
        """
        ...


@dataclass(frozen=True)
class ImportSource:
    """One foreign agent installation the onboarding importer can read from.

    A descriptor says WHERE an install is. It does not supply reader code, and it
    does not choose a reader: the engine does all reading with its own gated
    helpers, which is what keeps credential redaction, prompt-injection screening,
    sensitive-path refusal, size caps and symlink rejection applying to a
    registered source exactly as they do to a built-in one. A seam that accepted a
    scanner callable would hand an edition the engine's internal accumulator and
    depend on it to re-implement every one of those invariants correctly.

    The engine reads a registered source as an install of **this product's own
    on-disk layout** — a predecessor, a rename, or a fork, which is the case an
    edition actually has. A genuinely novel foreign format needs a reader in the
    core, because only the core can read it through the content gates; when a
    second layout exists, naming one becomes an additive default-valued field
    here rather than a reason to accept arbitrary reader code now.

    ``env_vars`` are consulted in order and the first non-empty one wins;
    ``home_dir`` is the fallback directory name under the user's home. A source
    declaring only ``env_vars``, with none of them set, is left unresolved rather
    than defaulting to the home root.
    """

    id: str
    display_name: str
    env_vars: tuple[str, ...] = ()
    home_dir: str = ""
    #: MCP server names this agent manages itself. Importing one would hand the
    #: user a server pointing at a foreign runtime, so they are always skipped.
    managed_mcp_names: tuple[str, ...] = ()
    #: Whether this product REPLACES the agent — a predecessor or a rename, not
    #: a peer the user may still be running. Only a superseded agent's leftovers
    #: are reclaimed from the user's global provider config: doing that to a live
    #: foreign agent would delete servers it is still using.
    superseded: bool = False
    #: Basenames of this agent's own launcher, used with ``superseded``. An MCP
    #: entry whose command is one of these was written by that agent and is
    #: purgeable once it is gone. Matched on the command, never on the server
    #: name, so an entry the user created themselves is left alone. A shared
    #: runtime (``node``, ``python``, a Windows shell, any versioned spelling of
    #: one) is refused: it would reclaim every server that happens to run on it.
    stale_mcp_binaries: tuple[str, ...] = ()


class ImportSourceProvider(Protocol):
    """Edition-contributed onboarding-import sources.

    The public edition ships the foreign agents any user may plausibly have
    installed. An edition whose users are migrating from a predecessor of its
    own registers that predecessor here instead of the core naming it, which is
    what keeps an edition-specific product name out of the public tree.
    """

    def import_sources(self) -> List[ImportSource]:
        """Return extra import sources (Default: ``[]``).

        WIRED: ``onboarding_import._sources()`` unions these over the core
        builtins for every scan, apply, and id-validation path, so a registered
        source appears in ``/api/onboarding/import/scan`` and is accepted by
        ``/api/onboarding/import/apply`` with no core branching. Read
        fail-closed through ``safe_context_call`` (fallback: builtins only), and
        a duplicate or malformed descriptor is dropped rather than shadowing a
        builtin.
        """
        ...


@dataclass(frozen=True)
class CapabilityResult:
    """Outcome of a ``CapabilityManager`` mutation.

    ``message`` is ALREADY human-friendly — the manager owns error translation,
    so the core never matches package-manager-specific error strings.
    """

    ok: bool
    message: str = ""


class CapabilityManager(Protocol):
    """Operations-based external package/capability manager (CPP seam).

    An operations-based seam rather than a binary-name one: rather than naming a
    binary whose exact CLI grammar the core then hardcodes, the edition
    implements these OPERATIONS and OWNS its own invocation grammar, output
    parsing, and error translation. The core calls an operation and only
    serializes the result / applies side effects (config sync, agent rebuild).

    The public Default is unavailable (``available()`` → ``False``), so the
    ``/api/capability/*`` handlers report "not available" (HTTP 503); a companion
    implements registry-backed MCP/skill/agent management. Every op is ``async``
    (it does I/O) and MUST NOT be called when ``available()`` is ``False``.

    LIVENESS: operations MUST be internally time-bounded — a slow companion op
    must not stall MCP handlers, so an edition's ops must not block
    indefinitely. As defense-in-depth the core also wraps every async op with
    ``asyncio.wait_for`` (differentiated bounds: tight for uninstall, generous
    for install so a legitimate cold package-manager download is not cancelled
    mid-mutation, and a tight read bound on ``list_*``/``registry`` — the
    dashboard polls those, so a stalled unbounded read would accumulate pending
    gateway tasks, the same wedge class this prevents). Only the sync
    ``available()`` probe is unwrapped (it does no I/O). That wrapper
    (``platform.capability_bound.BoundedCapabilityManager``)
    is applied at CONTEXT COMPOSITION — ``PlatformContext.__post_init__`` binds
    every ``CapabilityManager`` once — so EVERY reader of
    ``current_context().capability_manager`` inherits the bound, whether it routes
    through the dashboard ``_capability_manager()`` accessor or reads the context
    directly (a subagent, conductor, MCP-tool, or apps-backend consumer). The bind
    is idempotent, so the companion's ``dataclasses.replace`` composition path does
    not double-wrap. The core additionally runs ``/api/mcp/apply`` companion
    ``uninstall_mcp`` calls in a phase BEFORE it takes the process-wide MCP file
    lock, so no companion op is awaited while the lock is held.
    """

    def available(self) -> bool:
        """True when this edition provides a capability manager."""
        ...

    async def list_mcp(self) -> List[Dict[str, Any]]:
        """Installed MCP servers as structured entries (the manager parses its
        own output). Conventional keys: ``name``, ``server_id``, ``package``,
        ``version``."""
        ...

    async def install_mcp(self, server_id: str) -> "CapabilityResult": ...

    async def uninstall_mcp(self, server_id: str) -> "CapabilityResult": ...

    async def registry(self) -> List[Dict[str, Any]]:
        """Available MCP servers from the registry.

        The manager parses its own registry output into entries; the core passes
        them through verbatim as ``{"servers": [...]}``. Conventional keys the
        dashboard consumes: ``id``, ``installed``, ``title``, ``tier``,
        ``description`` (plus any extra fields the edition includes).
        """
        ...

    async def list_skills(self) -> List[Dict[str, Any]]:
        """Installed skill packages as structured rows (the manager parses its
        own output AND resolves paths — the core keeps no text grammar). Each row
        carries the skill-catalog fields the dashboard merges: ``key``, ``name``,
        ``description``, ``path``, ``dir``, ``always``, ``source``, ``package``.

        CONTAINMENT INVARIANT: every skill returned here MUST live under one of
        the roots reported by ``McpToolingProvider.extra_skills()``. The skill
        *browser* endpoints (``/api/skills/package/<name>/tree`` and detail)
        resolve a skill's on-disk path by searching those roots, so a row whose
        ``path``/``dir`` falls outside every ``extra_skills()`` root will list in
        ``/api/skills`` but 404 on tree/detail. An edition that satisfies both
        Protocols independently MUST keep them consistent (list only skills that
        are under an advertised root). The public ``Default`` returns ``[]`` so
        the standalone build has no mismatch. The core enforces this at runtime:
        ``collect_skills_blocking`` logs a loud warning for any listed row whose
        ``path``/``dir`` falls outside every ``extra_skills()`` root, turning an
        otherwise silent browse-404 into an actionable log line.
        """
        ...

    async def install_skill(self, package: str) -> "CapabilityResult":
        """Install a skill package by name.

        The edition resolves any version/source internally — NO Amazon-internal
        version-set concept is exposed on this operation or the public API.
        """
        ...

    async def uninstall_skill(self, package: str) -> "CapabilityResult": ...

    async def list_agents(self) -> List[Dict[str, Any]]:
        """Installed agent packages as structured entries (the manager parses its
        own output). Conventional keys: ``name``, ``package``, ``version``."""
        ...

    async def install_agent(self, package: str) -> "CapabilityResult":
        """Install an agent package by name.

        Symmetric with :meth:`install_skill`: the edition resolves version and
        source internally, so no registry-specific concept reaches the public API.
        An agent package typically carries agents PLUS its own skills and prompt
        sources, so a caller should refresh the agent catalog after this returns
        ``ok`` (the dashboard handler rebuilds the agent config and pushes a
        refresh).
        """
        ...

    async def uninstall_agent(self, package: str) -> "CapabilityResult": ...

    async def list_plugins(self) -> List[Dict[str, Any]]:
        """Installed editor/CLI plugin packages, as structured rows.

        A "plugin" here is an agent-client integration an edition's package
        manager can install alongside the agent packages themselves — the rows
        are informational for the dashboard, which pairs them with
        :meth:`plugins_out_of_sync` to offer a one-click reconcile. Conventional
        keys: ``name``, ``package``, ``version``.

        Deliberately generic: the core neither knows nor names any particular
        client. An edition with no plugin concept returns ``[]`` and the
        dashboard's reconcile affordance stays hidden.
        """
        ...

    async def plugins_out_of_sync(self) -> List[str]:
        """Packages installed as agents but MISSING their plugin counterpart.

        The drift set — non-empty means the two installers have diverged (an
        agent package landed without its plugin registration), which presents as
        an agent the client cannot see. Returning ``[]`` means "in sync" and is
        also the correct answer for an edition with no plugin concept.

        A READ op: it must not mutate. :meth:`sync_plugins` is the writer.
        """
        ...

    async def sync_plugins(self) -> "CapabilityResult":
        """Reconcile the plugin set with the installed agent packages.

        Installs the plugin counterpart for every package
        :meth:`plugins_out_of_sync` reports, so the two installers agree. Bounded
        as an INSTALL op — it may shell a package manager once per drifted
        package.
        """
        ...


# ── install / structural extension points ──


class ExternalAccessPolicy(Protocol):
    """Which external services this deployment may reach, beyond the deny floor.

    The core offers three things unconditionally that a managed deployment may
    have to withhold, and none of them had a composition point:

    * **Installable-content registries.** Skill discovery (``skill_providers/``,
      skills.sh) and MCP server discovery (``mcp_providers/``, the official MCP
      registry) fetch from the public internet and then offer to INSTALL what
      they return. Both hardcoded their public provider at registration time.
    * **Cloud deployment.** ``kiro_crew/deploy/`` provisions real infrastructure
      (S3, CloudFront, IAM roles, a reaper Lambda) in the operator's own cloud
      account and hands back a public URL. It carries no capability gate at all,
      so ``capabilities.publish`` — which bounds publish-provider destinations —
      does not reach it.

    One policy object rather than three, because they answer the same question
    ("may this deployment use an external service the core offers by default?")
    and because a single object gives ONE audited admission chokepoint and one
    thing for an operator to implement, instead of a decision that is logged in
    some places and not others.

    Both decisions take the concrete target as well as a label. A name is
    something a provider chooses for itself; the URL or target is what determines
    where bytes go. An allowlist pinned to the target stops admitting a provider
    that later repoints at a different host, rather than letting it inherit trust
    from its name.

    The public default admits everything, so open-source behaviour is unchanged.
    """

    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        """Whether the *kind* registry *name* serving ``api_base`` may be queried.

        *kind* is the catalog the provider belongs to — ``"skill"`` or ``"mcp"``.
        Returning ``False`` means the provider is never registered.
        """
        ...

    def admits_cloud_deployment(self, target: str) -> bool:
        """Whether this deployment may provision infrastructure in a cloud account.

        *target* names the deployment backend (``"aws"`` today). Returning
        ``False`` makes the deploy surface report itself disabled and refuse every
        mutating request.
        """
        ...


class AppRegistryPolicy(Protocol):
    """Trusted git hosts + clone-sandbox-mode decision for the app registry.

    Public default = the open-source public-forge host set.  The companion adds
    internal git hosts as trusted.
    """

    def public_git_hosts(self) -> "frozenset[str]": ...

    def clone_sandbox_mode(self, git_url: str, trusted_hosts: "frozenset[str] | None") -> str: ...


class AppsLoader(Protocol):
    """Discovery of bundled (builtin) apps + manifest sources.

    Public default = the open-source ``apps/builtins/`` set (``auto_research``,
    ``file_explorer``).  The companion bundles the internal feature apps.
    """

    def bundled_app_names(self) -> List[str]: ...

    def manifest_sources(self) -> List[Path]: ...

    def registry_rows(self) -> List[Dict[str, Any]]:
        """Extra App-Store registry rows the edition bundles (ADD-only merge).

        WIRED: ``apps/registry.py::_load_registry_file`` appends these to the
        rows parsed from the bundled ``app-registry.json``, de-duplicated by the
        row ``name`` (a bundled core row wins over a same-named edition row, so a
        companion can only ADD catalog entries, never silently repoint a core
        one). The public Default returns ``[]`` (catalog unchanged). A companion
        returns its internal App-Store rows (e.g. dev-fleet, pipeline-health)
        pointing at internal git hosts — which its ``AppRegistryPolicy`` already
        trusts. Each row is the same dict shape as an ``app-registry.json`` entry.
        v1 method addition (no ``CONTRACT_VERSION`` bump).
        """
        ...

    def default_registries(self) -> List[Dict[str, Any]]:
        """External app registries the edition ships as defaults (ADD-only merge).

        WIRED: ``apps/registry.py::_effective_registries`` merges these with the
        operator's ``config.registries`` for EVERY consumer of the registry list —
        index fetch/refresh, the trusted-host allowlist, row lookup, install, and
        the blob-proxy allowlist. Merging at the consumption sites rather than
        inside ``KiroCrewConfig`` is deliberate: an edition default must never be
        written back into the operator's ``config.json`` by a config save, and must
        never be shadowed by a stale copy that a past save persisted.

        Each row is the field shape of ``config.loader.ExternalRegistryConfig``
        (``{"name", "repo", "branch", "trust"}``); dicts, not dataclass instances,
        so a companion need not import the config module. Missing keys take the
        dataclass default.

        An edition default WINS on a ``name`` collision with an operator entry.
        That direction is the fail-closed one: a registry the edition pins carries
        a ``trust`` tier, and letting a same-named operator entry replace it would
        let a hand-edited ``config.json`` inherit that tier while repointing
        ``repo`` somewhere else. An operator can still add registries freely — just
        not silently repoint one the edition pinned.

        The public Default returns ``[]``, so standalone behaviour is byte-identical
        to reading ``config.registries`` alone. v1 method addition (no
        ``CONTRACT_VERSION`` bump).
        """
        ...


class KnowledgeProvider(Protocol):
    """Extra knowledge-base connectors the edition contributes.

    The knowledge sync engine keys connectors by ``source_type`` (the public
    core ships ``local_folder`` / ``obsidian_vault``).  Public default = none;
    the companion contributes internal connectors (e.g. an internal document
    connector) that
    the sync scheduler merges into its connector map.
    """

    def extra_connectors(self, cfg: "KiroCrewConfig") -> Dict[str, Any]:
        """Return ``{source_type: BaseConnector}`` to merge into the sync map.

        The values are ``kiro_crew.knowledge.connectors.base.BaseConnector``
        instances (typed ``Any`` here to avoid importing the knowledge package
        into the platform contract).  Returning ``{}`` (the Default) leaves the
        public connector set unchanged.
        """
        ...


class PackageManager(Protocol):
    """**RESERVED extension point — not consumed by the core.**

    Composing a ``PackageManager`` into ``PlatformContext.package_manager`` has
    NO effect: the external-tool install paths (ollama, ffmpeg, whisper) are
    inline step-by-step brew/curl/pip logic in ``cli_doctor.py``, not a single
    plan-resolution point this seam could own. Both methods are inert.

    For registry-backed installation of MCP servers / skills / agent packages,
    use ``CapabilityManager`` — the live, operations-based seam.

    Declared in ``context.RESERVED_SLOTS``: a non-default value logs a loud
    warning at boot, and ``test_platform_cpp_seam_coverage.py`` fails if a
    consumption site is added without the reservation being removed.
    """

    def install_plan(self, tool: str) -> List[str]: ...

    def which(self, tool: str) -> Optional[str]: ...


# ── runtime-service / frontend extension points ──


class TunnelProvider(Protocol):
    """Public-URL tunnel lifecycle.

    Public default = disabled/no-op (the ``tunnel/manager.py`` stub).  The
    companion supplies the internal tunnel supervisor (the Tunnels primitive
    itself is owned by PartyRock and out of scope).

    WIRED: the ``tunnel/manager.py`` stub ``TunnelManager`` delegates its
    ``start`` / ``stop`` / ``public_url`` UNCONDITIONALLY to
    ``current_context().tunnel`` (no edition/identity branch).  The Default is a
    no-op so the standalone build is byte-identical; a companion drives a real
    tunnel through this same surface.  ``dashboard/server.py`` gates enablement
    on ``enabled()`` (OR-ed with ``cfg.tunnel.enabled``), and
    ``tunnel/setup.py`` evaluates its token-auth deny gate BEFORE ``start()`` is
    reached, so a companion tunnel cannot start without dashboard token auth.

    ``register_callbacks`` and ``status_snapshot`` are the MINIMAL edition-neutral
    additions the CORS-reflection wrapper and ``/api/tunnel/status`` need so they
    can stay wrapped AROUND the provider (v1 addition; no ``CONTRACT_VERSION``
    bump).
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def public_url(self) -> str: ...

    def enabled(self) -> bool: ...

    def register_callbacks(
        self,
        *,
        on_connect: Optional[Callable[[str], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        """Register the connect/disconnect callbacks the wrapper wires in.

        ``on_connect(url)`` reflects the live public URL into the dashboard CORS
        allow-list + ``set_tunnel_url`` (presigned-link source); ``on_disconnect()``
        tears that reflection down.  The Default no-op provider never fires them.
        Called by ``TunnelManager.start`` before it delegates ``start()``.
        """
        ...

    def status_snapshot(self) -> Optional[Dict[str, Any]]:
        """Return a live status view, or ``None`` to defer to the caller's own.

        Edition-neutral primitives only (no import of ``kiro_crew.tunnel``): keys
        ``state`` (a ``TunnelState`` value string), ``url``, ``error``,
        ``started_at``, ``connected_at``, ``reconnect_attempt``.  The Default
        returns ``None`` so the stub ``TunnelManager`` keeps reporting its own
        local status (byte-identical standalone); a companion returns its live
        snapshot so ``/api/tunnel/status`` reflects the real tunnel.
        """
        ...

    async def ensure_available(self, *, install: bool = True) -> str:
        """Zero-touch provision: ensure a tunnel CAN serve a remote link, then
        report the resulting state. Distinct from ``start()`` (which only starts
        an ALREADY-enabled tunnel) — this may install the tunnel toolchain and
        flip enablement before starting, for surfaces that mint a remote link on
        demand (the ``/kirocrew dashboard`` command, the setup wizard).

        WIRED: ``slack/allowlist.py::send_dashboard_link`` calls this (via
        ``safe_context_call``) when the box is localhost-only and no live public
        URL exists, so a shared dashboard link can bring the tunnel up on first
        use. Returns a state string: ``"connected"`` (URL live), ``"starting"``
        (provisioning in progress), ``"disabled"`` (not configured / opted out),
        or ``"unavailable"`` (install or start failed). ``install=False`` skips
        any toolchain install (start-only). Only ``"connected"`` guarantees a
        live public URL — the caller adopts a tunnel URL solely on ``"connected"``
        and otherwise keeps the local link, re-issuing later once connected.

        Public default = ``"disabled"`` — a pure no-op, so the standalone build
        never provisions anything and behaves byte-identically. A companion
        drives its real install-and-enable pipeline here (v1 addition; no
        ``CONTRACT_VERSION`` bump).
        """
        ...


@dataclass(frozen=True)
class MobileConnectMethod:
    """One way the current deployment hands a phone a live dashboard session.

    A descriptor says WHICH method exists — never how to mint the credential.
    Minting stays on the method's own endpoint (the tailnet QR handler, the
    one-time mobile-link handler, a companion's own route), where the caller
    bounds, owner checks, restricted-session refusals and SEL audits already
    live; a seam that returned URLs or tokens would move credential minting
    behind an interface the core cannot audit.

    ``id`` is the governed identifier: the ``methods`` ruleset of the
    ``capabilities.mobile_connect`` scope narrows on it, and the methods
    endpoint drops a denied id before the dashboard ever sees it.  ``kind``
    names the frontend renderer; the dashboard skips a kind it does not
    recognise (an older frontend renders an edition's new method as absent,
    never as a broken panel).
    """

    id: str
    kind: str


class MobileConnectProvider(Protocol):
    """Edition-contributed phone-connection methods.

    Public default = the personal-install pair (tailnet QR + one-time login
    link), reproducing today's behavior.  An enterprise companion replaces the
    list with its own method(s) — or returns ``[]``, which hides the
    dashboard's "Connect your phone" entry entirely (governance can also pin
    the capability off without an edition swap; both paths converge on an
    empty methods list).
    """

    def connect_methods(self) -> List[MobileConnectMethod]:
        """Return the deployment's methods, BEFORE governance filtering.

        WIRED: ``dashboard/handlers/mobile_connect.py::api_mobile_connect_methods``
        reads this via ``safe_context_call`` (fallback: ``[]``, hiding the
        entry rather than guessing) and filters each id through the
        ``capabilities.mobile_connect`` governance scope; the mint endpoints
        re-check the same scope per id, so the filtered list is presentation,
        never the control.
        """
        ...


@dataclass(frozen=True)
class OtlpDestination:
    """One OTLP/HTTP collector this edition sends telemetry to.

    A destination says WHERE telemetry goes and how to authenticate to it —
    never WHAT may be sent.  The core keeps the signal shape, the consent gate,
    attribute sanitisation, the histogram views, batching and retry; a
    destination cannot widen any of them.

    Deliberately signal-agnostic.  ``signals`` names which OTLP signals this
    collector accepts (``"metrics"`` today), so a later core that also emits
    traces or logs reads the SAME descriptor rather than needing a second
    protocol method — the OTLP/HTTP metric, span and log exporters take the same
    constructor arguments, so one descriptor already describes all three.  A
    signal the core does not emit is simply never built.

    ``name`` is a short, NON-SECRET label used in logs.  It exists because
    ``endpoint`` must never be logged (a URL can carry credentials in userinfo or
    query parameters), which would otherwise leave a multi-destination host with
    no way to tell which collector failed.

    ``session`` is an authenticated transport handed to the exporter (a
    ``requests.Session``; typed loosely so this contract does not depend on the
    HTTP client).  It is the seam a ROTATING credential composes against:
    ``Session.auth`` is re-evaluated on every request, so a short-lived OIDC/SSO
    id_token or STS credential is re-read on each export instead of being frozen
    at construction — which is exactly what ``OTEL_EXPORTER_OTLP_HEADERS``, the
    only injection point before this seam, cannot avoid.  Static per-destination
    headers need no field of their own: ``Session.headers`` already carries them
    per destination, where that env var is process-wide.
    """

    name: str
    endpoint: str
    signals: "frozenset[str]"
    session: Optional[Any] = None


class TelemetryProvider(Protocol):
    """Backend telemetry sink + the frontend RUM config blob.

    Public default = no-op; ``frontend_rum_config`` returns ``None`` so the
    SPA's RUM shim stays disabled.  The companion records events and returns the
    Cognito/RUM config the internal frontend host consumes.
    """

    def record_event(self, event_type: str, data: dict) -> None: ...

    def frontend_rum_config(self) -> Optional[dict]: ...

    def otlp_destinations(self, cfg: "TelemetryConfig") -> Sequence[OtlpDestination]:
        """OTLP collectors this edition sends telemetry to (WHERE, never WHAT).

        WIRED: ``metrics/provider.py::_otlp_destinations`` calls this once per
        recorder build — after the consent gate, before any reader is
        constructed — and builds one ``PeriodicExportingMetricReader`` per
        returned destination that names ``"metrics"`` in its ``signals``,
        ALONGSIDE (never instead of) the built-in local JSONL reader.  An empty
        sequence means local-only, with no network egress.

        The public default returns one destination when
        ``telemetry.otlp_endpoint`` is a non-empty string and nothing when it is
        not, so a standalone build stays byte-identical to the hardcoded
        endpoint-only exporter this replaced.  An edition overrides it to reach
        its own collector — including one whose credential ROTATES during
        process lifetime, which the once-at-construction
        ``OTEL_EXPORTER_OTLP_HEADERS`` injection cannot express.

        ADD-only by construction: destinations are appended to the core's reader
        list, so this can never remove or replace the local sink, and it cannot
        relax consent, attribute sanitisation, or the histogram views.  A
        destination with an empty ``endpoint``, or one that does not name the
        signal being built, is DROPPED rather than trusted — deny-by-default, so
        a half-populated descriptor cannot start an unintended egress.

        Best-effort: the build path reads it through ``safe_context_call``
        (fallback ``()``), so a provider that raises contributes no destinations
        and telemetry keeps working local-only instead of failing.

        MUST be cheap and side-effect-free per call. It is read once per recorder
        build AND on every egress-posture read — the Privacy panel's status and
        each ``telemetry.enabled`` config write ask it so their disclosure cannot
        disagree with what gets attached. So do not acquire a token, mint a
        credential, or perform any one-shot side effect inside it: build the
        transport once and hand back the same object. v1 method addition (no
        ``CONTRACT_VERSION`` bump).
        """
        ...


class DashboardContributor(Protocol):
    """Edition-contributed dashboard routes + background services + login handler.

    Public default = no-op: contributes no routes, runs no services, and supplies
    no SSO login handler (the public ``/api/sso-login`` stays the core stub).  The
    companion mounts internal gateway routes (e.g. the secretary / taskkeeper
    services) and the real SSO PTY login handler.

    The methods are called once, during ``dashboard.server.start_dashboard``,
    against the live aiohttp application — ``contribute_routes`` BEFORE the SPA
    static catch-all and ``AppRunner.setup()`` freezes the route table, and the
    ``start_services`` / ``stop_services`` pair via ``app.on_startup`` /
    ``app.on_cleanup`` so edition services share the gateway lifecycle.
    """

    def contribute_routes(self, app: "web.Application") -> None:
        """Mount edition routes on the application (Default: no routes)."""
        ...

    async def start_services(self, app: "web.Application") -> None:
        """Start edition background services (Default: no-op)."""
        ...

    async def stop_services(self, app: "web.Application") -> None:
        """Stop edition background services on gateway shutdown (Default: no-op).

        Receives the same ``app`` handle as ``start_services`` (symmetric), so a
        companion can retrieve services/tasks it stashed on the app at startup
        without resorting to process-global state (which would break with more
        than one dashboard app in a process).
        """
        ...

    def sso_login_handler(self) -> Optional[Callable]:
        """Return the SSO-login WS handler, or ``None`` to keep the core stub."""
        ...

    def on_user_message(self, app: "web.Application", message: str) -> None:
        """Observe an inbound user chat message (Default: no-op).

        WIRED: ``dashboard/chat_handlers.py::api_chat`` calls this once per user
        message, just before the turn is scheduled, inside a fail-safe guard so
        an observer error never blocks the chat.  Fire-and-forget by contract:
        the observer must not block (schedule its own task if it needs to do
        work).  The public Default is a no-op; a companion uses it e.g. to
        auto-ingest document links pasted into chat.  It is an
        OBSERVER — its return value is ignored and it MUST NOT mutate the message
        or the turn.
        """
        ...

    def on_token_consumed(
        self,
        user_id: str,
        channel: str,
        session_exp: float,
        thread_ts: "str | None",
    ) -> None:
        """Observe a dashboard-session token being consumed on a verified device.

        WIRED: ``dashboard/token_auth.py::bind_token_ip`` calls this once, right
        after it binds the token to the consuming IP, inside a fail-safe guard so
        an observer error never blocks token consumption. Fire-and-forget: the
        observer must not block.

        This is the anchor the Slack challenge-and-redirect posture needs: when a
        user opens a presigned challenge link on a real device, the token is
        consumed here, and the companion opens the bounded per-``(user, channel)``
        auth window that lets subsequent Slack traffic flow inline (see
        ``SlackEnterpriseGate.intercept_message``). ``session_exp`` is the token's
        session expiry (the window's hard ceiling); ``thread_ts`` threads the
        window to the originating Slack thread. The public Default is a no-op — no
        window is opened, matching today's OSS behavior. v1 method addition (no
        ``CONTRACT_VERSION`` bump).
        """
        ...

    def decorate_reply(self, text: str, *, channel: str, user_id: str) -> str:
        """Transform an outbound Slack reply just before it is sent (Default: identity).

        WIRED: ``slack/handler.py`` calls this on the finalized reply text on the
        outbound path, inside a fail-safe guard (a raising decorator falls back to
        the undecorated text). The public Default returns ``text`` unchanged.

        The challenge posture uses it to append a "<5 min left" expiry footer when
        the auth window for ``(user_id, channel)`` is close to lapsing, and to
        refresh the window's activity clock on each reply. It is a PURE
        transform — it returns the (possibly-decorated) text and must not block on
        network I/O. v1 method addition (no ``CONTRACT_VERSION`` bump).
        """
        ...


class JailProvider(Protocol):
    """Process-isolation (jail) lifecycle for agent-bearing commands.

    Public default = no-op: ``available()`` is ``False`` and
    ``maybe_reexec_into_jail`` returns ``None`` (no isolation; the command runs
    in-process exactly as today).  The companion supplies the internal jail
    orchestration that re-execs the process into an isolated namespace before any
    agent work starts, mirroring the ``TunnelProvider`` lifecycle shape.

    **Re-entry contract.** Before a successful re-exec the core gate sets the
    ``KIROCREW_JAILED`` env marker; the jailed CHILD re-runs the gate, sees the
    marker, and short-circuits, so the backend is NOT asked to re-jail an
    already-jailed process.  A companion that spawns the child with a fresh
    environment MUST set this marker to ANY non-empty value (re-entry is detected
    by PRESENCE, not truthiness — ``"1"``, ``"jailed"``, a namespace id all work)
    so the child does not deadlock: under ``mode == "on"`` the child would
    otherwise probe again, get an "already jailed" ``None``, and be refused by the
    on-mode floor.
    """

    def available(self) -> bool:
        """Whether a working jail backend is present on this host.

        SHOULD NOT raise — push any probing ``try/except`` into the adapter and
        return ``False`` on a probe failure.  (The core gate is defensive: it
        treats a raised probe as "availability unknown" and fails closed under
        ``mode == "on"``, but a clean ``bool`` keeps the two consumers — the gate
        and ``doctor`` — in agreement.)
        """
        ...

    def status_detail(self) -> str:
        """One-line human status for ``doctor`` output."""
        ...

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        """Re-exec the process into the jail; return the child exit code.

        Returns ``None`` when no re-exec happens (disabled, unavailable, or
        already jailed) — the caller then falls through and runs in-process.
        A non-``None`` int is the jailed child's exit status; the caller exits
        with it.  The Default always returns ``None``.

        **mode contract:** ``"off"`` / already-jailed / disabled → ``None``.
        ``"auto"`` → re-exec if a backend is available, else degrade to ``None``.
        ``"on"`` → the operator demanded isolation: the implementation MUST either
        re-exec (returning the child rc) or fail closed (raise / exit non-zero);
        it MUST NOT return ``None`` when isolation could not be established.  The
        core gate additionally treats a ``None`` return (or a swallowed error)
        under ``mode == "on"`` as fail-closed, so a defensive backend cannot
        accidentally downgrade an on-mode host to an un-jailed run.  (The
        "already jailed → ``None``" case does not deadlock the child because the
        gate's ``KIROCREW_JAILED`` re-entry guard short-circuits before this is
        called a second time — see the class docstring's re-entry contract.)
        """
        ...


# ── feature apps ──


class FeatureApp(Protocol):
    """**RESERVED item type — not consumed by the core.**

    Populating ``PlatformContext.feature_apps`` has NO effect: bundled apps are
    discovered through ``AppsLoader.manifest_sources()`` / ``bundled_app_names()``
    and registered by ``apps/manager.py``. Neither ``manifest_path`` nor
    ``register`` is ever called; the tuple is a provenance record only.

    **Bundle apps through ``AppsLoader`` instead** — the live seam.

    Unlike every other Protocol here this one ships NO ``Default*`` adapter, and
    deliberately so: ``FeatureApp`` describes ONE app (an item), not a policy the
    core asks a question of, so there is no behavior for a default to reproduce.
    The default for the *slot* is the empty tuple ``()`` that
    ``build_default_context`` already composes — a ``DefaultFeatureApp`` would
    have to invent a fictional app to be instantiable, which is worse than
    nothing. Declared in ``context.RESERVED_SLOTS``; a non-empty tuple logs a
    loud warning at boot, and ``test_platform_cpp_seam_coverage.py`` fails if a
    consumption site appears without the reservation being removed.
    """

    @property
    def name(self) -> str: ...

    def manifest_path(self) -> Path: ...

    def register(self, ctx: Any) -> None: ...
