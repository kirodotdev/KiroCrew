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

from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.config.loader import KiroCrewConfig


# ── boot-layer extension points ──


class ProviderRegistry(Protocol):
    """The LLM-provider factory + ACP-backend registration seam.

    The public edition ships Kiro-CLI-ACP only.  The companion uses
    ``register_acp_backends`` to re-register Claude Code through the dormant
    ``ACP_BACKEND_CLAUDE`` seam without the core changing.
    """

    def create_factory(self, cfg: "KiroCrewConfig") -> Callable[..., Any]:
        """Return the provider factory (Default: ``cfg.create_provider_factory()``).

        WIRED: every factory build site routes through
        ``config.loader.build_provider_factory`` →
        ``current_context().providers.create_factory(cfg)``. The Default returns
        exactly ``cfg.create_provider_factory()`` (identity), so the public
        edition is unchanged; a companion can return an alternate factory (e.g.
        re-registering the Claude Code / Bedrock ACP backend) — but kiro-cli stays
        the default for both editions unless the companion is explicitly opted in.
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
    """The agent runtime: managed MCP servers + first-run setup.

    NOT YET WIRED: neither method is consumed by the core yet — managed servers
    are assembled in ``agent.py`` and first-run setup is called directly via
    ``agent.run_first_run_setup()``. The companion contributes internal MCP
    servers through ``McpToolingProvider.extra_mcp_servers`` instead (which IS
    wired). Staged for a later migration; overriding it has no effect yet.
    """

    def managed_mcp_servers(self) -> Dict[str, dict]: ...

    def run_first_run_setup(self) -> None: ...


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
        ``"@builder-mcp/ReadInternalWebsites"``).

        ADD-only by construction: this can only widen the allowlist with names
        the companion vouches for; it can never remove a core entry or relax the
        exact-match discipline. NEVER sourced from config — an agent-writable set
        would defeat the deny-by-default heartbeat gate, so the companion adapter
        is the only supplier. v1 method addition (no ``CONTRACT_VERSION`` bump).
        """
        ...


class IdentityProvider(Protocol):
    """SSO/identity resolution.

    Public default = local token, no SSO (the ``midway.py`` no-op stubs).  The
    companion resolves through Midway / MCS / Kerberos.

    ``status_line`` is async to match the existing ``get_midway_status_line``
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

    async def status_line(self, prefix: str = "*Midway:*") -> str: ...

    def whoami(self) -> Optional[str]: ...

    def issuer(self) -> Optional[str]: ...

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

    def credential_watch_paths(self) -> List[Path]:
        ...


class EmbeddingSource(Protocol):
    """Where the embedding model comes from + request signing.

    Public default = the bundled in-process model (``qwen3-embedding:0.6b``
    via vendored llama.cpp), no HTTP and no signing.  Since the in-process
    runtime landed the core has no active HTTP embed path, so
    ``endpoint_url``/``sign_request`` are a dormant seam — kept for contract
    stability.  A companion supplying a different runtime should compose an
    ``EmbeddingBackend`` via ``embeddings.register_embedding_backend``.
    """

    def registry_model(self) -> str: ...

    def endpoint_url(self) -> Optional[str]: ...

    def sign_request(
        self, method: str, url: str, headers: dict, body: "bytes | str"
    ) -> Optional[dict]: ...


class McpToolingProvider(Protocol):
    """Extra MCP servers + skill catalog the edition contributes.

    Public default = none beyond the managed servers.  The companion injects
    builder-mcp and the internal AIM skill paths.
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
    the companion contributes internal connectors (e.g. a Quip connector) that
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
    """Install strategy for external tools (ollama, etc.).

    Public default = brew/curl/pip fallbacks.  The companion uses the internal
    toolbox / capability installer.

    NOT YET WIRED: no core call site routes installs through this seam yet
    (callers still use their inline brew/curl/pip logic). Staged for later;
    overriding it has no effect yet.
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
    no SSO login handler (the public ``/api/mwinit`` stays the core stub).  The
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

    def mwinit_handler(self) -> Optional[Callable]:
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
    """One bundled App-Kit app the active profile ships.

    Public default set is empty (or the OSS builtins).  The companion bundles
    mimir / code_reviewer / team_manager / secretary / taskkeeper / quip.

    NOT YET WIRED: the core does not yet read ``PlatformContext.feature_apps``;
    edition apps are registered via ``AppsLoader.manifest_sources`` /
    ``bundled_app_names`` instead. Staged for a later registration path;
    populating it has no effect yet.
    """

    @property
    def name(self) -> str: ...

    def manifest_path(self) -> Path: ...

    def register(self, ctx: Any) -> None: ...
