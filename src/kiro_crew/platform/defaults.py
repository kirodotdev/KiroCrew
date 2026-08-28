"""Default adapters — the public open-source behavior for every extension point.

Each ``Default*`` adapter delegates to the existing module-level symbol it
replaces (``agent._MANAGED_MCP_SERVERS``, ``sandbox._STRICT_DIRS``,
``security.redact``, ``sso_status.*``, ``embeddings._MODEL_ID``, …) so the
standalone edition is behaviorally identical to today — the contract adds an
indirection layer, not a behavior change.

The Amazon companion subclasses or replaces these in its composition root.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import ImportSource, McpScope

from kiro_crew import security, sso_status
from kiro_crew.platform.interfaces import (
    PROVIDER_BACKEND_FACTORY_ATTR,
    CapabilityResult,
    InterceptDecision,
    OtlpDestination,
)

# ``agent``, ``sandbox``, ``embeddings``, ``apps.registry`` and ``slack.enterprise``
# import ``kiro_crew.platform`` at module-load time, so importing them at the top
# of this module (loaded during ``platform`` package init via ``bootstrap``)
# would create a cycle — those stay local to each method and carry a
# ``# circular import`` annotation.  ``security`` and ``sso_status`` are imported at
# top level here because neither reaches ``platform`` at MODULE-LOAD time.
# Exception (deferred-only): ``security.scan_exfiltration_urls`` /
# ``redact_exfiltration_urls`` read ``current_context().credentials
# .exempt_exact_hosts()`` through a FUNCTION-LOCAL import of
# ``kiro_crew.platform.context`` (the ``sel.py`` deferred pattern).  Because that
# reach-back is deferred to call time — never at ``security`` module load — the
# top-level ``security`` import above stays cycle-free.


class DefaultProviderRegistry:
    """Public provider registry for Kiro and operator-installed ACP adapters."""

    def create_factory(self, cfg: Any) -> Callable[..., Any]:
        from kiro_crew.acp.types import ACP_BACKEND_KIRO

        configured_backend = cfg.agent.acp_backend
        factory_backend = (
            configured_backend if isinstance(configured_backend, str) else ACP_BACKEND_KIRO
        )
        if factory_backend == ACP_BACKEND_KIRO:
            # H13: the ordinary Kiro path is the direct factory object, not a
            # registry dispatcher that happens to choose Kiro at call time.
            kiro_factory = cfg.create_provider_factory()
            setattr(
                kiro_factory,
                PROVIDER_BACKEND_FACTORY_ATTR,
                lambda backend: self._create_adapter_dispatch_factory(cfg, backend),
            )
            return kiro_factory

        return self._create_adapter_dispatch_factory(cfg, factory_backend)

    def _create_adapter_dispatch_factory(
        self,
        cfg: Any,
        factory_backend: str,
    ) -> Callable[..., Any]:
        """Build the registry dispatcher used only by an opted-in adapter."""

        from kiro_crew.acp import backends as acp_backends
        from kiro_crew.acp.types import ACP_BACKEND_KIRO

        # Programmatic mutation can bypass persisted-value normalization.
        # Validate a configured adapter while the factory is built so a typo
        # fails before the first session tries to start.
        acp_backends.descriptor_for(factory_backend)
        factories: dict[str, Callable[..., Any]] = {}
        kiro_factory: Callable[..., Any] | None = None

        def _registered_factory(*args: Any, **kwargs: Any) -> Any:
            nonlocal kiro_factory
            requested = kwargs.pop("acp_backend", None)
            backend = factory_backend if requested is None else requested
            if backend == ACP_BACKEND_KIRO:
                if kiro_factory is None:
                    kiro_factory = cfg.create_provider_factory()
                return kiro_factory(*args, **kwargs)

            from kiro_crew.acp import backends as acp_backends
            from kiro_crew.providers.acp import AcpProvider, SpecAdapterAcpProvider

            # Programmatic mutation can bypass persisted-value normalization.
            # Refuse a foreign id before constructing any adapter provider.
            acp_backends.descriptor_for(backend)
            provider_types = {
                acp_backends.Dialect.KIRO: AcpProvider,
                acp_backends.Dialect.SPEC: SpecAdapterAcpProvider,
            }
            dialect = acp_backends.dialect_of(backend)
            try:
                provider_type = provider_types[dialect]
            except KeyError as exc:
                raise RuntimeError(
                    f"Unsupported ACP dialect {dialect!r} for backend {backend!r}"
                ) from exc
            factory = factories.get(backend)
            if factory is None:
                factory = cfg._create_adapter_provider_factory(
                    factory_backend=backend,
                    provider_type=provider_type,
                    registry_model_ids=acp_backends.supports(
                        backend, acp_backends.CAP_REGISTRY_MODEL_IDS
                    ),
                    tool_search_supported=acp_backends.supports(
                        backend, acp_backends.CAP_TOOL_SEARCH
                    ),
                )
                factories[backend] = factory
            return factory(*args, **kwargs)

        return _registered_factory

    def register_acp_backends(self) -> None:
        # Registry adapter discovery is lazy and cached by acp.backends. The
        # public provider class is selected in create_factory so registration
        # never mutates the first-class Kiro provider path.
        return None


class DefaultPublishRegistry:
    """Registers no publish provider — the public edition has no artifact-publish
    destination.  The ``publish_provider`` registry stays empty, so
    ``get_provider`` raises ``PublishUnavailableError`` (→ 503) and
    ``list_providers`` returns ``[]`` (dashboard shows "publishing unavailable")
    with no core branching.  A companion registers its concrete providers here
    via the ``publish_provider.register_provider`` side effect — the structural
    twin of ``DefaultProviderRegistry.register_acp_backends``."""

    def register_publish_providers(self) -> None:
        return None


class DefaultAgentRuntime:
    """Today's managed MCP servers + first-run setup."""

    def managed_mcp_servers(self) -> Dict[str, dict]:
        # RESERVED (see context.RESERVED_METHODS['agent_runtime']): no core call
        # site reads this — the agent config is built from the
        # ``agent._MANAGED_MCP_SERVERS`` global directly.  Kept faithful to that
        # global so the method is correct if it is ever wired; contribute extra
        # servers through the WIRED ``McpToolingProvider.extra_mcp_servers()``.
        from kiro_crew import agent  # circular import: agent imports platform

        return dict(agent._MANAGED_MCP_SERVERS)

    def run_first_run_setup(self) -> None:
        # WIRED: ``slack/gateway.py`` gateway boot calls this through the seam.
        # Delegating to the real ``agent.run_first_run_setup`` makes the routing
        # behavior-preserving for the standalone edition — byte-for-byte the same
        # first-run wiring (PATH shim + admission-policy seed + one-time stale
        # managed-MCP purge) the gateway would otherwise invoke directly.  A companion
        # overrides this to add its own one-time provisioning on top (and should
        # call the same underlying function, or super(), to keep the core steps).
        from kiro_crew import agent  # circular import: agent imports platform

        agent.run_first_run_setup()


class DefaultAgentExecutableResolver:
    """Use the executable selected by the public installation unchanged."""

    def resolve_executable(self, executable: str) -> str:
        return executable


class DefaultSandboxPolicy:
    """Today's open-source sensitive-dir lists from ``sandbox.py``."""

    def strict_dirs(self) -> List[str]:
        from kiro_crew import sandbox  # circular import: sandbox imports platform

        return list(sandbox._STRICT_DIRS)

    def cc_dirs(self) -> List[str]:
        from kiro_crew import sandbox  # circular import: sandbox imports platform

        return list(sandbox._CC_DIRS)


class DefaultCredentialPolicy:
    """Today's AKIA/ASIA + exfil redaction passes from ``security.py``."""

    def redact(self, text: str) -> str:
        return security.redact(text)

    def exempt_exact_hosts(self) -> "frozenset[str]":
        # The public edition exempts no hosts from the exfil heuristics — the
        # base64-blob / query-length checks run for every domain, so redaction
        # is byte-identical to today.  The companion returns its trusted-tenant
        # host set (empty = MORE redaction, the safe direction).
        return frozenset()


class DefaultSlackEnterpriseGate:
    """Default-open gate delegating to ``slack/enterprise.py``.

    ``extra_ids`` is accepted for protocol compatibility and IGNORED: the module
    re-reads ``slack.allowed_enterprise_ids`` itself, which is the same key the
    callers derive this value from, so a passed set is at best a duplicate and
    at worst an older copy naming ids the operator removed.
    """

    def validate_enterprise(self, bot_token: str, *, extra_ids: "set[str] | None" = None) -> bool:
        # deferred: defaults.py loads at platform-init (bootstrap imports it);
        # importing slack.enterprise eagerly would pull the slack + config stack
        # into every boot. No import cycle here — kept local for lazy loading.
        from kiro_crew.slack import enterprise

        return enterprise.validate_enterprise(bot_token, extra_ids=extra_ids)

    def check_message_origin(self, event_team_id: str) -> bool:
        # deferred: see validate_enterprise above (lazy-load the slack stack;
        # no cycle).
        from kiro_crew.slack import enterprise

        return enterprise.check_message_origin(event_team_id)

    def heartbeat_safe_tools(self) -> "frozenset[str]":
        # The public edition adds no tools to the heartbeat allowlist — the set
        # stays exactly the core HEARTBEAT_SAFE_TOOLS. The companion returns its
        # internal read-only tool names.
        return frozenset()

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
        # The public edition processes every allowed message inline — no
        # challenge-and-redirect. Returning PROCESS keeps _route_message
        # byte-identical to the pre-seam OSS behavior.
        return InterceptDecision.PROCESS


class DefaultIdentityProvider:
    """No-SSO local identity — the ``sso_status.py`` no-op stubs."""

    def status(self) -> Dict[str, object]:
        return sso_status.sso_status()

    async def status_line(self, prefix: str = "*SSO:*") -> str:
        return await sso_status.get_sso_status_line(prefix)

    def whoami(self) -> Optional[str]:
        # RESERVED (see context.RESERVED_METHODS['identity']): no core call site.
        # The public edition has no SSO principal beyond what kiro-cli reports.
        return None

    def issuer(self) -> Optional[str]:
        # RESERVED (see context.RESERVED_METHODS['identity']): no core call site.
        return None

    def preflight_checks(self) -> List[Callable[[], None]]:
        # The public edition runs no pre-launch checks — gateway/token startup
        # is unchanged.  The companion returns its SSO-session checks here.
        return []

    def credential_watch_paths(self) -> List[Path]:
        # The public edition watches no credential files — the MCP gateway
        # daemon runs with no rotation watcher. A companion returns its
        # rotated-credential file path(s) here.
        return []


class DefaultEmbeddingSource:
    """Bundled in-process model (vendored llama.cpp), unsigned local inference.

    RESERVED slot (see ``context.RESERVED_SLOTS['embeddings']``): the core has no
    HTTP embed path, so NO method here is consumed.  Kept faithful to today's
    model id so the adapter is correct if the slot is ever wired; a companion
    supplying a different runtime composes an ``EmbeddingBackend`` via
    ``embeddings.register_embedding_backend`` instead.
    """

    def registry_model(self) -> str:
        from kiro_crew import embeddings  # circular import: embeddings imports platform

        return embeddings._MODEL_ID

    def endpoint_url(self) -> Optional[str]:
        # In-process runtime — no remote endpoint.
        return None

    def sign_request(
        self, method: str, url: str, headers: dict, body: "bytes | str"
    ) -> Optional[dict]:
        # Unsigned: in-process inference makes no HTTP requests.
        return None


class DefaultMcpToolingProvider:
    """No extra MCP servers, skills, or provider scopes beyond the managed set."""

    def extra_mcp_servers(self) -> Dict[str, dict]:
        return {}

    def extra_skills(self) -> List[Path]:
        return []

    def extra_mcp_scopes(self) -> List["McpScope"]:
        return []


class DefaultAgentCatalogProvider:
    """No edition agent-catalog rows — discovery is the on-disk scan only."""

    def builtin_agents(self) -> List[Dict[str, Any]]:
        return []


class DefaultPromptSourceProvider:
    """No edition prompt/SOP roots — only user-authored prompts are listed."""

    def prompt_source_roots(self) -> List[Path]:
        return []


class DefaultImportSourceProvider:
    """No edition import sources — the onboarding importer offers the builtins only."""

    def import_sources(self) -> List["ImportSource"]:
        return []


class DefaultCapabilityManager:
    """Unavailable capability manager — the public edition ships no external
    package manager, so ``/api/capability/*`` report 503. Every operation is a
    fail-closed no-op that MUST NOT be reached (handlers guard on ``available()``)."""

    def available(self) -> bool:
        return False

    async def list_mcp(self) -> List[Dict[str, Any]]:
        return []

    async def install_mcp(self, server_id: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def uninstall_mcp(self, server_id: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def registry(self) -> List[Dict[str, Any]]:
        return []

    async def list_skills(self) -> List[Dict[str, Any]]:
        return []

    async def install_skill(self, package: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def uninstall_skill(self, package: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def list_agents(self) -> List[Dict[str, Any]]:
        return []

    async def install_agent(self, package: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def uninstall_agent(self, package: str) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")

    async def list_plugins(self) -> List[Dict[str, Any]]:
        return []

    async def plugins_out_of_sync(self) -> List[str]:
        # Empty = "in sync", which is the correct answer for an edition with no
        # plugin concept (not merely a fail-closed stub).
        return []

    async def sync_plugins(self) -> "CapabilityResult":
        return CapabilityResult(ok=False, message="capability manager not available")


class DefaultExternalAccessPolicy:
    """Admits every external service — today's open-source behaviour.

    The public build queries skills.sh and the official MCP registry and offers
    cloud deployment, so the default must stay permissive or an ordinary install
    would lose both browsers and the deploy page. A managed edition overrides this
    to allowlist its own registry and to withhold cloud deployment.
    """

    def admits_registry(self, kind: str, name: str, api_base: str) -> bool:
        return True

    def admits_cloud_deployment(self, target: str) -> bool:
        return True


class DefaultAppRegistryPolicy:
    """Today's public trusted-host set + clone-sandbox-mode decision.

    Delegates to ``apps/registry.py._PUBLIC_GIT_HOSTS`` — the public-forge set
    (github / gitlab / bitbucket / sr.ht / codeberg), with NO internal host
    trusted.  A clone from any host outside that set runs ``strict`` sandbox
    mode.  The Amazon companion's ``AmazonAppRegistryPolicy`` overrides
    ``public_git_hosts()`` to add the internal git hosts so its registry clones
    are trusted; the public Default never trusts an internal host.
    """

    def public_git_hosts(self) -> "frozenset[str]":
        from kiro_crew.apps import registry  # circular import: apps.registry imports platform

        return registry._PUBLIC_GIT_HOSTS

    def clone_sandbox_mode(self, git_url: str, trusted_hosts: "frozenset[str] | None") -> str:
        from kiro_crew.apps import registry  # circular import: apps.registry imports platform

        return registry._clone_sandbox_mode(git_url, trusted_hosts)


class DefaultAppsLoader:
    """The open-source ``apps/builtins/`` set."""

    def bundled_app_names(self) -> List[str]:
        # auto_research + file_explorer ship in the public core.
        return ["auto_research", "file_explorer"]

    def manifest_sources(self) -> List[Path]:
        return []

    def registry_rows(self) -> List[Dict[str, Any]]:
        # The public edition bundles no extra App-Store rows beyond
        # apps/app-registry.json. A companion returns its internal catalog rows.
        return []

    def default_registries(self) -> List[Dict[str, Any]]:
        # The public edition pins no external registry: the only registries are
        # the ones the operator typed into config.registries. A companion returns
        # its organisation's official registry.
        return []


class DefaultPackageManager:
    """Public brew/curl/pip install strategy (delegated to cli_doctor logic).

    RESERVED slot (see ``context.RESERVED_SLOTS['package_manager']``): no core
    call site routes installs through this seam — ``cli_doctor.py`` keeps its
    inline per-tool logic.  Use ``CapabilityManager`` for registry-backed
    installs of MCP servers / skills / agent packages.
    """

    def install_plan(self, tool: str) -> List[str]:
        # The public edition has no managed installer; callers fall back to
        # their existing inline brew/curl/pip logic when the plan is empty.
        return []

    def which(self, tool: str) -> Optional[str]:
        return shutil.which(tool)


class DefaultTunnelProvider:
    """Disabled tunnel — the ``tunnel/manager.py`` stub is a no-op."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def public_url(self) -> str:
        return ""

    def enabled(self) -> bool:
        return False

    def register_callbacks(
        self,
        *,
        on_connect: Optional[Callable[[str], Any]] = None,
        on_disconnect: Optional[Callable[[], Any]] = None,
    ) -> None:
        # No managed tunnel → the connect/disconnect reflection callbacks never
        # fire.  The stub TunnelManager keeps them for import compatibility.
        return None

    def status_snapshot(self) -> Optional[Dict[str, Any]]:
        # None → the stub TunnelManager reports its own local status, so the
        # standalone ``/api/tunnel/status`` payload is byte-identical to today.
        return None

    async def ensure_available(self, *, install: bool = True) -> str:
        # Standalone never auto-provisions a tunnel — pure no-op, so a shared
        # dashboard link falls back to the local host:port exactly as today.
        return "disabled"


class DefaultTelemetryProvider:
    """No-op telemetry; RUM stays disabled (frontend shim already no-op)."""

    def record_event(self, event_type: str, data: dict) -> None:
        return None

    def frontend_rum_config(self) -> Optional[dict]:
        return None

    def otlp_destinations(self, cfg: Any) -> "tuple[OtlpDestination, ...]":
        # Byte-identical to the endpoint-only OTLP exporter this seam replaced:
        # ONE destination when telemetry.otlp_endpoint is a non-empty string,
        # NONE otherwise — so egress stays off by default and the standalone
        # build reaches exactly the collector it reached before. Read with
        # getattr so any telemetry-config shape works, and never logged here:
        # the value can carry credentials in userinfo or query parameters.
        endpoint = str(getattr(cfg, "otlp_endpoint", "") or "").strip()
        if not endpoint:
            return ()
        return (
            OtlpDestination(
                name="telemetry.otlp_endpoint",
                endpoint=endpoint,
                signals=frozenset({"metrics"}),
            ),
        )


class DefaultKnowledgeProvider:
    """No extra connectors — the public edition ships only the built-in set."""

    def extra_connectors(self, cfg: Any) -> Dict[str, Any]:
        return {}


class DefaultDashboardContributor:
    """No-op dashboard contributor — no edition routes, services, or login handler."""

    def contribute_routes(self, app: Any) -> None:
        return None

    async def start_services(self, app: Any) -> None:
        return None

    async def stop_services(self, app: Any) -> None:
        return None

    def sso_login_handler(self) -> Optional[Callable[..., Any]]:
        # None → the dashboard keeps its built-in /api/sso-login stub handler.
        return None

    def on_user_message(self, app: Any, message: str) -> None:
        # The public edition observes no chat messages. A companion uses this to
        # e.g. auto-ingest doc links pasted into chat.
        return None

    def on_token_consumed(
        self,
        user_id: str,
        channel: str,
        session_exp: float,
        thread_ts: "str | None",
    ) -> None:
        # The public edition opens no Slack auth window on token consumption.
        return None

    def decorate_reply(self, text: str, *, channel: str, user_id: str) -> str:
        # The public edition sends replies unchanged (no expiry footer / window
        # refresh — there is no challenge window in OSS).
        return text


class DefaultJailProvider:
    """No jail — the public edition never re-execs into a process isolation jail."""

    def available(self) -> bool:
        return False

    def status_detail(self) -> str:
        return "no jail provider (public edition)"

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        # None → no re-exec; the command runs in-process exactly as today.
        return None
