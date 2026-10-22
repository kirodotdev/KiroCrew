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
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, List, Optional, Protocol

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.security import DeniedCommandRule
    from kiro_crew.skill_providers.base import SkillProvider
    from kiro_crew.tips_pool import TipsPool


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

    def agent_runtime_policy(self, engine_identity: str) -> dict[str, Any] | None:
        """Return advisory catalog metadata, or None in the public edition.

        The key is the agent the member RUNS: its ``kiro_agent`` through
        ``config.loader.dispatch_kiro_agent``, so a row that recorded an agent's
        file name keys on the name that file declares, falling back to its
        roster alias when the binding is empty. Only owner-visible member rows
        request this metadata.
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

        That call REFUSES a harness whose ``ACP_BACKEND_ROUTING`` entry is
        ``Routing.UNVERIFIED``, and there is no way to override it. Being in
        ``ACP_BACKENDS_KNOWN`` is not enough, and that is deliberate: known means a
        build can spell the id, which is what lets a governance rule deny it, while
        selectable means sessions start — and for an unverified harness nothing
        establishes that its tool calls reach the host permission gate, its routing
        verdict refuses no session, and its spawn path applies no compensating
        credential mask. An edition offering a new harness therefore establishes how
        it routes first, and declares that in ``ACP_BACKEND_ROUTING``.
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


class GatewayLifecycleProvider(Protocol):
    """Edition-owned gateway launch selection, independent of update policy."""

    def restart_launcher(self) -> str | None:
        """Return an absolute stable launcher, or None for the core Python path.

        The launcher receives the original CLI arguments without Python's ``-m``
        prefix. It owns selecting the installed version and rebuilding its import
        environment. Preserve its pathname: a symlink basename may select the app.
        Return a native executable on Windows, not a shell command or batch file.
        Implementations must be cheap and have their dependencies loaded at boot:
        an update can remove the running package tree before this method is called.
        Errors refuse restart; they must not silently select the old interpreter.
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

        WIRED: the gateway boot path (``slack/gateway_runtime/service_boot.py``) calls this through
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

    ``sandbox_argv.py`` calls this before applying Kiro Crew's OS-level sandbox so an
    edition can replace a managed launcher with the executable it ultimately
    invokes, avoiding nested isolation. The public Default is identity: ordinary
    ``kiro-cli`` paths and the explicit ``KIROCREW_KIRO_BIN`` override are used
    unchanged. Implementations must return the input unchanged when they do not
    recognize it and must never weaken or disable the outer sandbox.
    """

    def resolve_executable(self, executable: str) -> str: ...


class SandboxPolicy(Protocol):
    """The sandbox *data*: which dirs/files to hide/expose.

    The ``wrap_argv`` mechanism stays in ``sandbox_wrap.py``; only the directory and
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

    def token_param_exempt_url_prefixes(self) -> "frozenset[str]":
        """``https://host/path`` prefixes whose ``?token=`` value pass 4 leaves alone.

        WIRED: pass 4 skips only a literal ``?token=`` immediately after a member
        whose local boundary and preceding text prove it is not nested in another
        destination (any earlier URL, backslash, scheme, ``&`` or ``<`` is a signal,
        scanned raw and never decoded), whose value is one URL-safe/base64 run plus
        closers, and which no earlier pass claimed. A companion must list the host in
        ``exempt_exact_hosts()`` too, or earlier base64/query heuristics can redact
        the URL before pass 4. For user-opened approval links whose token is a lookup
        key rather than a bearer credential. Streaming windows never apply this
        exemption because they lack the complete preceding destination context.

        Members without a path are ignored (a bare host prefix would match
        ``host.evil.example``). Public default = ``frozenset()``. Like
        ``exempt_exact_hosts`` this is NEVER sourced from ``config.json``.
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

    An implementation that replaces ``validate_enterprise`` must also record the
    bot's own identity through ``slack.enterprise.set_validated_self_identity``:
    clear it before re-validating, set it from ``auth.test`` on success. The
    trusted-bot admission reads it and trusts no peer bot while it is empty.
    """

    def validate_enterprise(
        self, bot_token: str, *, extra_ids: "set[str] | None" = None
    ) -> bool: ...

    def check_message_origin(self, event_team_id: str) -> bool: ...

    def heartbeat_safe_tools(self) -> "frozenset[str]":
        """Extra tool names an edition allows during unattended heartbeat polling.

        WIRED: ``slack/gateway_runtime/tool_policy.py::_is_heartbeat_safe_tool`` checks this set after
        the core heartbeat allowlist's identity match. The public default is
        ``frozenset()`` (no additions — the heartbeat allowlist is byte-identical
        to today). A companion returns its own read-only tool names so its
        heartbeat polls can auto-approve them. The ``@server/`` prefix of every pin
        is also what ``agent_materialization.worker_agent.heartbeat_mirrored_servers``
        mounts on the heartbeat agent (copied from the default spec, ``autoApprove``
        dropped; a prefix the default spec does not mount is skipped), so one list
        names both the servers and the tools.

        **Entries MUST be server-qualified ``"@server/Tool"``.** An entry is
        honored ONLY when it matches the event's trusted ``mcp_server_name`` /
        ``tool_name`` pair (``_meta.kiro``); a bare tool name never matches, and
        the model-authored title is never consulted. This is deliberate: a bare-name allowlist
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
        ``gateway`` dispatch in ``cli.py`` and ``_token`` in ``cli_server_requests.py``)
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

    def kas_relayed_env_references(self) -> FrozenSet[str]:
        """Variable names an MCP ``env`` value may reference on the KAS backend.

        WIRED: ``acp.kas_agents`` keeps an entry's ``env`` value that is exactly
        ``${NAME}`` for a returned *NAME*, on any server, and withholds every
        other reference. KAS resolves it from its own environment when it
        spawns the server, so only the name crosses the wire. Return only names
        whose values carry no secret, such as a launcher's loopback address.
        The public Default returns an empty set.
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


class TipsProvider(Protocol):
    """The edition's feature-tip pool — the one REPLACE-capable edition seam.

    Every other edition hook in this contract is ADD-only: an edition UNIONS its
    contribution into the public set (``McpToolingProvider.extra_mcp_servers``,
    ``SkillDiscoveryProvider.skill_providers``, ``DeniedRuleProvider``…).  Tips
    cannot work that way.  A tip asserts that a feature exists and is worth
    using, so a public tip shown on an edition build advertises a capability that
    build may not have or may deliberately not expose — and the failure is
    silent, because a tip is an unprompted suggestion nobody asked to verify.

    Subtraction is not an alternative: a deny-list has to enumerate the public
    pool to subtract from it, so every tip added to the public pool afterwards
    would surface on the edition build until someone remembered to deny it — the
    default would be leak, not withhold.  Hence full replacement: when an edition
    supplies a pool, the public curated file and the public docs-scan catalog are
    not consulted at all.
    """

    def tips_pool(self) -> "Optional[TipsPool]":
        """The edition's COMPLETE tip pool, or ``None`` to keep the public one.

        WIRED: ``tips.get_tips_cache`` calls this once per process when it builds
        the tips cache.  ``None`` (the public Default) leaves today's behavior
        byte-for-byte — bundled catalog, docs-scan fallback, bundled curated
        file.  A :class:`TipsPool` REPLACES both the curated pool and the feature
        catalog, so neither the public curated file nor ``docs/*.md`` is read and
        no public feature is offered to the tip generator.  An empty pool is legal
        and means this build shows no tips.

        Scope of that guarantee: the prompt is built from the pool's catalog
        alone, so a public feature is never put in front of the model, and a
        generated tip whose ``doc`` is outside that catalog is dropped at parse
        time (``tips._parse_tips``).  What remains is narrow: a tip could cite a
        valid pool doc while its prose names something absent, because generated
        text is checked for shape and provenance, not for subject.  Constrain the
        pool; treat a generated tip's wording as unverified.

        Pool entries are re-validated on the way into the cache through the same
        checks the public pool's entries pass (``tips._sanitize_pool``); a
        malformed entry is dropped and logged rather than served.

        The pool's ``pool_id`` is persisted and compared on load: when it differs
        from the stamp in ``tips_state.json``, tips generated against the previous
        pool are discarded rather than re-served, which is what closes the leak on
        a host that gains (or loses) the companion after tips were already
        generated.  User dismissals and snoozes are deliberately NOT discarded —
        they are keyed by tip id and doc, so an entry from another pool simply
        never matches, and dropping them would silently un-dismiss tips the user
        already refused.

        Read fail-closed through ``safe_context_call`` at the call site, and the
        degrade is profile-dependent: on ``standalone`` a failure falls back to the
        public pool, because there is no other pool to prefer; on a non-standalone
        build it WITHHOLDS tips (an empty pool stamped ``edition-unavailable``),
        because falling back to public there would serve exactly the tips this
        seam exists to keep off that build. A ``PlatformCompositionError`` is
        re-raised on both. v1 addition (no ``CONTRACT_VERSION`` bump).
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

    async def registry(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Available MCP servers from the registry.

        The manager parses its own registry output into entries; the core passes
        them through verbatim as ``{"servers": [...]}``. Conventional keys the
        dashboard consumes: ``id``, ``installed``, ``title``, ``tier``,
        ``description`` (plus any extra fields the edition includes).

        ``query`` is an OPTIONAL free-text filter HINT, passed only by MCP
        discovery search (``mcp_providers.capability``); the browse endpoint
        ``GET /api/capability/mcp/registry`` omits it and still gets the full
        listing. A manager MAY use it to filter server-side, and SHOULD when its
        registry is large: filtering at the source spares it materializing and
        returning the whole catalog on every search. Ignoring the hint stays
        CORRECT — the caller filters again. Because the hint is feature-detected
        on the signature, an older zero-arg implementation keeps working
        unchanged.
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


#: The id AND kind of the provisioner the core ships: EC2 in the user's own
#: AWS account, driven by ``cloud.launch_engine.RealLaunchEngine``. A companion
#: may return a list without it (hiding the AWS lane) but may not register a
#: second descriptor under this id.
BUILTIN_PROVISIONER_ID = "aws_ec2"


# The boot-layer extension points above stay in this module. The other two kinds live in
# sibling modules and are re-exported here, so ``kiro_crew.platform.interfaces`` stays the
# one import path: ``interfaces_runtime`` (runtime-service / frontend) and
# ``interfaces_structural`` (install / structural, and the feature apps).
from kiro_crew.platform.interfaces_runtime import (  # noqa: E402,F401 - re-exported
    PROVIDER_ACTION_LOGIN,
    AudioChunk,
    DashboardContributor,
    FeedbackTrackerProvider,
    JailProvider,
    MobileConnectMethod,
    MobileConnectProvider,
    OtlpDestination,
    ProviderAction,
    ProviderStatus,
    RemoteProvisioner,
    RemoteProvisionerProvider,
    SpeechProviderRegistry,
    SttProvider,
    SttStreamSession,
    TelemetryProvider,
    TtsProvider,
    TunnelProvider,
)
from kiro_crew.platform.interfaces_structural import (  # noqa: E402,F401 - re-exported
    AppRegistryPolicy,
    AppsLoader,
    ExternalAccessPolicy,
    FeatureApp,
    KnowledgeProvider,
    MemoryEntry,
    MemoryFiles,
    MemoryFilesProvider,
    MemoryRoots,
    PackageManager,
)
