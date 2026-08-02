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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.config.loader import KiroCrewConfig


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

    The public edition ships Kiro-CLI-ACP only.  The companion uses
    ``register_acp_backends`` to re-register a Claude backend through the dormant
    ``ACP_BACKEND_CLAUDE`` seam without the core changing.
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


class TelemetryProvider(Protocol):
    """Backend telemetry sink + the frontend RUM config blob.

    Public default = no-op; ``frontend_rum_config`` returns ``None`` so the
    SPA's RUM shim stays disabled.  The companion records events and returns the
    Cognito/RUM config the internal frontend host consumes.
    """

    def record_event(self, event_type: str, data: dict) -> None: ...

    def frontend_rum_config(self) -> Optional[dict]: ...


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
